"""受信解析规则包（部署方维护，不从网页加载任何代码）。

三版规则对应改版迁移故事：
- v1-legacy:  旧选择器，未感知新版式；对新版式会把年份 2026 当成参与人数
- v2-naive:   候选「朴素统一」规则，错误复用旧选择器，串位静默失真，
              离线重放时应被人工确认样本和门槛拒绝
- v2-layout:  候选「版式感知」规则，区分 old/new 选择器，通过门槛并灰度启用
"""
from dataclasses import dataclass, field

from parser_manager.dom import Element, select_one
from parser_manager.rules import FieldSpec, RulePack

SOURCE = "science_station"


def _article_id() -> FieldSpec:
    return FieldSpec("article", attr="data-id")


@dataclass
class LegacyV1(RulePack):
    version: str = "v1-legacy"
    description: str = "旧版固定选择器，未感知新版式"
    applies_to_sources: tuple[str, ...] = (SOURCE,)
    page_features: dict[str, str] = field(default_factory=dict)

    def parse(self, root: Element):
        return {
            "activity_id": _article_id(),
            "start_time": FieldSpec("time.start"),
            "end_time": FieldSpec("time.end"),
            "place": FieldSpec("span.place"),
            # 旧版式人数；新版式里 span.number 装的是年份 2026 —— 串位根源。
            "participants": FieldSpec("span.number"),
        }


@dataclass
class NaiveV2(RulePack):
    """错误候选：一套选择器走天下，新版式人数取到年份，校验却仍然通过。"""

    version: str = "v2-naive"
    description: str = "朴素统一选择器候选，未感知版式，预期被门槛拒绝"
    applies_to_sources: tuple[str, ...] = (SOURCE,)
    page_features: dict[str, str] = field(default_factory=dict)

    def parse(self, root: Element):
        # 全部沿用旧版式思路：新版式下 span.number 装的是年份 2026（人数串位），
        # 地点用逗号分组兼容两种版式，时间取第二个 time 当结束时间。
        return {
            "activity_id": _article_id(),
            "start_time": FieldSpec("time"),
            "end_time": FieldSpec("time:nth-of-type(2)"),
            "place": FieldSpec("span.place, p[data-field='place']"),
            "participants": FieldSpec("span.number"),
        }


@dataclass
class LayoutAwareV2(RulePack):
    """正确候选：按版式切换选择器，旧新版式都能定位到真实字段。"""

    version: str = "v2-layout"
    description: str = "版式感知选择器，同时适配 old/new 版式"
    applies_to_sources: tuple[str, ...] = (SOURCE,)
    page_features: dict[str, str] = field(default_factory=dict)

    def parse(self, root: Element):
        article = select_one(root, "article")
        layout = article.attrs.get("data-layout") if article else None
        if layout == "old":
            return {
                "activity_id": _article_id(),
                "start_time": FieldSpec("time.start"),
                "end_time": FieldSpec("time.end"),
                "place": FieldSpec("span.place"),
                "participants": FieldSpec("span.number"),
            }
        # 新版式（new 与混用页面均为 data-layout=new）
        return {
            "activity_id": _article_id(),
            "start_time": FieldSpec("time[data-role='start']"),
            "end_time": FieldSpec("time[data-role='end']"),
            "place": FieldSpec("p[data-field='place']"),
            "participants": FieldSpec("p[data-field='participants']"),
        }
