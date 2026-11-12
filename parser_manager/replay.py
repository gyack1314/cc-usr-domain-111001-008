"""对已存档网页的离线重放。

硬性约束：
- 只读取本地存档目录中的 HTML 与人工确认 JSON，绝不发起任何网络请求；
- 报告候选规则相对当前规则的逐字段差异、人工确认样本逐字段匹配率，
  以及隔离比例的变化；
- 确认字段分母 = 样本中人工给出的非空字段数；实际缺失算不匹配；
- 隔离比例按来源页面数计算；无确认样本时禁止自动启用。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .dom import parse_html, select_one
from .fields import RecordResult, detect_cross_record_conflicts
from .rules import RulePack


class ReplayError(ValueError):
    pass


@dataclass
class ArchivedPage:
    source_id: str
    record_id: str
    page: str
    path: Path
    features: dict[str, str]
    html: bytes


@dataclass
class ReplayReport:
    candidate_version: str
    baseline_version: str | None
    source_id: str
    page_count: int
    # 人工确认匹配
    confirmed_total: int = 0
    confirmed_matched: int = 0
    confirmed_misses: list[dict] = field(default_factory=list)
    has_confirmed_samples: bool = False
    # 隔离统计（按页面数）
    candidate_quarantined: int = 0
    baseline_quarantined: int = 0
    quarantine_ratio: float = 0.0
    baseline_quarantine_ratio: float = 0.0
    # 与当前规则的逐字段差异（仅比较成功抽取的字段值）
    field_diffs: list[dict] = field(default_factory=list)
    # 页面级状态变化
    status_changes: list[dict] = field(default_factory=list)
    candidate_results: list[RecordResult] = field(default_factory=list)
    baseline_results: list[RecordResult] = field(default_factory=list)

    @property
    def confirmed_match_rate(self) -> float:
        if self.confirmed_total == 0:
            return 0.0
        return self.confirmed_matched / self.confirmed_total

    def to_dict(self) -> dict:
        return {
            "candidate_version": self.candidate_version,
            "baseline_version": self.baseline_version,
            "source_id": self.source_id,
            "page_count": self.page_count,
            "has_confirmed_samples": self.has_confirmed_samples,
            "confirmed_total": self.confirmed_total,
            "confirmed_matched": self.confirmed_matched,
            "confirmed_match_rate": round(self.confirmed_match_rate, 6),
            "confirmed_misses": self.confirmed_misses,
            "candidate_quarantined": self.candidate_quarantined,
            "baseline_quarantined": self.baseline_quarantined,
            "quarantine_ratio": round(self.quarantine_ratio, 6),
            "baseline_quarantine_ratio": round(self.baseline_quarantine_ratio, 6),
            "field_diffs": self.field_diffs,
            "status_changes": self.status_changes,
        }


def load_archived_pages(archive_dir: str | Path, source_id: str) -> list[ArchivedPage]:
    """扫描存档目录下的 HTML，提取 record_id 与页面特征。全程只读本地文件。"""
    archive_dir = Path(archive_dir)
    if not archive_dir.is_dir():
        raise ReplayError(f"存档目录不存在: {archive_dir}")
    pages: list[ArchivedPage] = []
    for path in sorted(archive_dir.glob("*.html")):
        html = path.read_bytes()
        root = parse_html(html)
        article = select_one(root, "article")
        if article is None:
            raise ReplayError(f"存档页面缺少 article 根节点: {path.name}")
        record_id = article.attrs.get("data-id")
        if not record_id:
            raise ReplayError(f"存档页面缺少 data-id: {path.name}")
        features = {"layout": article.attrs.get("data-layout", "")}
        pages.append(
            ArchivedPage(
                source_id=source_id,
                record_id=record_id,
                page=path.name,
                path=path,
                features=features,
                html=html,
            )
        )
    if not pages:
        raise ReplayError(f"存档目录没有 HTML 页面: {archive_dir}")
    return pages


def load_confirmed(confirmed_path: str | Path) -> dict[str, dict[str, object]]:
    """返回 {页面文件名: {字段: 人工确认值}}；只收非空确认字段。"""
    path = Path(confirmed_path)
    if not path.is_file():
        raise ReplayError(f"人工确认文件不存在: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    confirmed: dict[str, dict[str, object]] = {}
    for entry in raw:
        fields = {name: value for name, value in entry.get("fields", {}).items() if value is not None}
        confirmed[entry["page"]] = fields
    return confirmed


def _same_value(expected: object, actual: object) -> bool:
    if isinstance(actual, str) and isinstance(expected, str):
        return actual == expected
    # 数值型（参与人数）要求类型与值都一致，避免 2026 与 "2026" 混淆。
    if isinstance(expected, int) and not isinstance(expected, bool):
        return isinstance(actual, int) and actual == expected
    if hasattr(actual, "isoformat"):
        return actual.isoformat() == str(expected)
    return actual == expected


def _run_pack(pack: RulePack, pages: list[ArchivedPage], run_id: str) -> list[RecordResult]:
    results = []
    for archived in pages:
        root = parse_html(archived.html)
        result = pack.extract(
            root,
            source_id=archived.source_id,
            record_id=archived.record_id,
            page=archived.page,
            page_features=archived.features,
            run_id=run_id,
        )
        results.append(result)
    detect_cross_record_conflicts(results)
    return results


def replay(
    candidate: RulePack,
    pages: list[ArchivedPage],
    confirmed: dict[str, dict[str, object]],
    *,
    baseline: RulePack | None = None,
    run_id: str = "replay",
) -> ReplayReport:
    """离线重放候选规则，并与基线规则及人工确认样本比对。"""
    source_id = pages[0].source_id
    candidate_results = _run_pack(candidate, pages, run_id)
    baseline_results = _run_pack(baseline, pages, run_id) if baseline is not None else []
    baseline_by_page = {result.page: result for result in baseline_results}

    report = ReplayReport(
        candidate_version=candidate.version,
        baseline_version=baseline.version if baseline else None,
        source_id=source_id,
        page_count=len(pages),
        candidate_results=candidate_results,
        baseline_results=baseline_results,
    )

    # -- 人工确认逐字段匹配 -------------------------------------------------
    pages_with_confirmation = [page for page in pages if page.page in confirmed]
    report.has_confirmed_samples = bool(pages_with_confirmation)
    for result in candidate_results:
        expected_fields = confirmed.get(result.page)
        if not expected_fields:
            continue
        for name, expected in expected_fields.items():
            report.confirmed_total += 1
            evidence = result.fields.get(name)
            actual = evidence.value if evidence is not None else None
            if evidence is not None and evidence.error is None and _same_value(expected, actual):
                report.confirmed_matched += 1
            else:
                report.confirmed_misses.append(
                    {
                        "page": result.page,
                        "field": name,
                        "expected": expected,
                        "actual": actual.isoformat() if hasattr(actual, "isoformat") else actual,
                        "raw": evidence.raw if evidence is not None else None,
                        "reason": "missing" if actual is None or (evidence and evidence.error) else "mismatch",
                    }
                )

    # -- 隔离比例（按来源页面数） -------------------------------------------
    report.candidate_quarantined = sum(1 for r in candidate_results if r.status == "quarantined")
    report.quarantine_ratio = report.candidate_quarantined / report.page_count
    if baseline is not None:
        report.baseline_quarantined = sum(1 for r in baseline_results if r.status == "quarantined")
        report.baseline_quarantine_ratio = report.baseline_quarantined / report.page_count

    # -- 与基线的逐字段差异与状态变化 ---------------------------------------
    for result in candidate_results:
        base = baseline_by_page.get(result.page)
        if base is None:
            continue
        if result.status != base.status:
            report.status_changes.append(
                {"page": result.page, "baseline": base.status, "candidate": result.status}
            )
        for name, evidence in result.fields.items():
            base_evidence = base.fields.get(name)
            current = evidence.value if evidence.error is None else None
            old = base_evidence.value if base_evidence and base_evidence.error is None else None
            current_cmp = current.isoformat() if hasattr(current, "isoformat") else current
            old_cmp = old.isoformat() if hasattr(old, "isoformat") else old
            if current_cmp != old_cmp:
                report.field_diffs.append(
                    {
                        "page": result.page,
                        "field": name,
                        "baseline": old_cmp,
                        "candidate": current_cmp,
                        "candidate_raw": evidence.raw,
                        "candidate_dom_path": evidence.dom_path,
                    }
                )
    return report
