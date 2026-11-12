"""启用门槛评估（配置驱动，禁止无样本自动启用）。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .replay import ReplayReport


@dataclass(frozen=True)
class ReleasePolicy:
    minimum_confirmed_match: float
    maximum_quarantine_ratio: float
    canary_percent: int

    @classmethod
    def load(cls, path: str | Path) -> "ReleasePolicy":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        policy = cls(
            minimum_confirmed_match=float(payload["minimum_confirmed_match"]),
            maximum_quarantine_ratio=float(payload["maximum_quarantine_ratio"]),
            canary_percent=int(payload["canary_percent"]),
        )
        policy._validate()
        return policy

    def _validate(self) -> None:
        if not 0.0 <= self.minimum_confirmed_match <= 1.0:
            raise ValueError("minimum_confirmed_match 必须在 0..1 之间")
        if not 0.0 <= self.maximum_quarantine_ratio <= 1.0:
            raise ValueError("maximum_quarantine_ratio 必须在 0..1 之间")
        if not 0 <= self.canary_percent <= 100:
            raise ValueError("canary_percent 必须在 0..100 之间")


@dataclass(frozen=True)
class GateDecision:
    approved: bool
    reasons: tuple[str, ...]
    metrics: dict[str, object]

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "reasons": list(self.reasons),
            "metrics": self.metrics,
        }


def evaluate_gate(report: ReplayReport, policy: ReleasePolicy) -> GateDecision:
    """按配置门槛评估重放报告。

    启用条件（任一不满足即拒绝）：
    1. 必须存在人工确认样本，否则禁止自动启用；
    2. 确认字段匹配率 >= minimum_confirmed_match（实际缺失算不匹配）；
    3. 隔离比例（按来源页面数）<= maximum_quarantine_ratio。
    """
    reasons: list[str] = []
    metrics = {
        "confirmed_match_rate": round(report.confirmed_match_rate, 6),
        "confirmed_total": report.confirmed_total,
        "quarantine_ratio": round(report.quarantine_ratio, 6),
        "page_count": report.page_count,
        "minimum_confirmed_match": policy.minimum_confirmed_match,
        "maximum_quarantine_ratio": policy.maximum_quarantine_ratio,
    }

    if not report.has_confirmed_samples or report.confirmed_total == 0:
        reasons.append("没有人工确认样本，禁止自动启用")

    if report.has_confirmed_samples:
        if report.confirmed_match_rate < policy.minimum_confirmed_match:
            reasons.append(
                f"确认字段匹配率 {report.confirmed_match_rate:.4f} "
                f"低于下限 {policy.minimum_confirmed_match:.4f}"
            )

    if report.quarantine_ratio > policy.maximum_quarantine_ratio:
        reasons.append(
            f"隔离比例 {report.quarantine_ratio:.4f} "
            f"超过上限 {policy.maximum_quarantine_ratio:.4f}"
        )

    return GateDecision(approved=not reasons, reasons=tuple(reasons), metrics=metrics)
