"""字段抽取结果类型、转换规则与校验。

约束（需求硬性要求，禁止用默认值掩盖问题）：
- 参与人数必须是非负整数，'约三十' 这类文本转换失败即隔离；
- 结束时间不得早于开始时间；
- 同一来源同一轮中，活动编号相同但地点不同 → 冲突隔离；
- 缺少任一必要字段 → 整条记录隔离，不输出半条正常记录。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import REQUIRED_FIELDS


class ConversionError(ValueError):
    """字段原文无法转换为目标类型。"""


@dataclass
class FieldEvidence:
    """单个字段的抽取证据：原文 + DOM 位置 + 转换结果。"""

    name: str
    raw: str | None
    dom_path: str | None
    css_path: str | None
    value: object = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "field": self.name,
            "raw": self.raw,
            "dom_path": self.dom_path,
            "css_path": self.css_path,
            "value": _jsonable(self.value),
            "error": self.error,
        }


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# 转换函数：失败一律抛 ConversionError，绝不回退默认值。
# ---------------------------------------------------------------------------

def convert_text(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise ConversionError("文本为空")
    return text


def convert_non_negative_int(raw: str) -> int:
    text = raw.strip()
    if not text.isdigit():
        raise ConversionError(f"参与人数不是非负整数: {raw!r}")
    return int(text)


def convert_datetime(raw: str) -> datetime:
    text = raw.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConversionError(f"时间无法解析: {raw!r}") from exc


CONVERTERS = {
    "activity_id": convert_text,
    "place": convert_text,
    "participants": convert_non_negative_int,
    "start_time": convert_datetime,
    "end_time": convert_datetime,
}


@dataclass
class RecordResult:
    """一次抽取的完整结果（正常记录或隔离记录共用此结构）。"""

    source_id: str
    record_id: str
    page: str
    rule_version: str
    fields: dict[str, FieldEvidence] = field(default_factory=dict)
    status: str = "ok"  # ok | quarantined
    issues: list[str] = field(default_factory=list)
    run_id: str | None = None

    def values(self) -> dict[str, object]:
        return {name: evidence.value for name, evidence in self.fields.items()}

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "record_id": self.record_id,
            "page": self.page,
            "rule_version": self.rule_version,
            "run_id": self.run_id,
            "status": self.status,
            "issues": list(self.issues),
            "fields": [evidence.to_dict() for evidence in self.fields.values()],
        }


def validate_record(result: RecordResult) -> None:
    """记录级校验：缺字段、转换错误、时间顺序。就地写入 issues 并改状态。"""
    for name in REQUIRED_FIELDS:
        evidence = result.fields.get(name)
        if evidence is None or evidence.raw is None:
            result.issues.append(f"缺少必要字段: {name}")
        elif evidence.error is not None:
            result.issues.append(f"字段转换失败 {name}: {evidence.error}")

    start = result.fields.get("start_time")
    end = result.fields.get("end_time")
    if (
        start is not None
        and end is not None
        and start.error is None
        and end.error is None
        and end.value < start.value
    ):
        result.issues.append(
            f"结束时间 {end.value.isoformat()} 早于开始时间 {start.value.isoformat()}"
        )

    if result.issues:
        result.status = "quarantined"


def detect_cross_record_conflicts(results: list[RecordResult]) -> None:
    """跨记录冲突：同来源同活动编号在同轮出现不同地点。

    仅比较通过记录级校验的记录；冲突双方（全部相关记录）一并隔离。
    """
    groups: dict[tuple[str, str], list[RecordResult]] = {}
    for result in results:
        place = result.fields.get("place")
        activity = result.fields.get("activity_id")
        if (
            result.status == "ok"
            and place is not None
            and activity is not None
            and place.error is None
        ):
            key = (result.source_id, str(activity.value))
            groups.setdefault(key, []).append(result)

    for (source_id, activity_id), bucket in groups.items():
        places = {str(item.fields["place"].value) for item in bucket}
        if len(places) > 1:
            for item in bucket:
                item.status = "quarantined"
                item.issues.append(
                    f"冲突: 来源 {source_id} 活动编号 {activity_id} "
                    f"在同轮出现不同地点 {sorted(places)}"
                )
