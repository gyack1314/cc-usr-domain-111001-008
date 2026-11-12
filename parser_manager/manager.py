"""解析规则生命周期编排门面。

对外能力：
- register_candidate：登记候选并离线重放（不访问网络），产出差异/匹配率/隔离报告；
- reject / approve：按门槛处置候选，未过门槛起草为拒绝；
- set_canary / enable / rollback：按来源灰度、原子全量启用、原子回滚；
- claim_job / complete_job：在途作业领取时绑定版本，切换不影响在途；
- current_view：回滚后对外视图恢复旧版本，新版本证据仍可查；
- export_provenance：导出某条结果的逐字段来源。
"""
from __future__ import annotations

import uuid
from pathlib import Path

from .canary import bucket_of, use_candidate
from .dom import parse_html, select_one
from .fields import RecordResult, detect_cross_record_conflicts
from .gate import ReleasePolicy, evaluate_gate
from .replay import load_archived_pages, load_confirmed, replay
from .rules import RuleError, RulePack, RuleRegistry
from .store import Store


class ManagerError(RuntimeError):
    pass


class ReleaseManager:
    def __init__(
        self,
        *,
        store: Store,
        registry: RuleRegistry,
        archive_dir: str | Path,
        confirmed_path: str | Path,
        policy: ReleasePolicy,
        source_id: str,
    ):
        self.store = store
        self.registry = registry
        self.archive_dir = Path(archive_dir)
        self.confirmed_path = Path(confirmed_path)
        self.policy = policy
        self.source_id = source_id
        self._pages = load_archived_pages(self.archive_dir, source_id)
        self._confirmed = load_confirmed(self.confirmed_path)
        for pack in self.registry.all_packs():
            self.store.insert_rule_version(pack, pack.content_hash())

    # ------------------------------------------------------------------
    # 候选登记 + 离线重放
    # ------------------------------------------------------------------

    def register_candidate(self, candidate_version: str) -> dict:
        """对存档网页离线重放候选规则，落库报告与门槛决策。

        候选自身的任何失败都不会改动当前版本，不影响正常输出。
        """
        candidate = self._pack_for_source(candidate_version)
        baseline_version = self.store.get_current_version(self.source_id)
        baseline = self.registry.get(baseline_version) if baseline_version else None

        report = replay(
            candidate,
            self._pages,
            self._confirmed,
            baseline=baseline,
            run_id=f"replay-{candidate_version}-{uuid.uuid4().hex[:8]}",
        )
        decision = evaluate_gate(report, self.policy)
        candidate_id = uuid.uuid4().hex
        self.store.insert_candidate(
            candidate_id=candidate_id,
            source_id=self.source_id,
            rule_version=candidate_version,
            baseline_version=baseline_version,
            report=report.to_dict(),
            gate=decision.to_dict(),
        )
        if not decision.approved:
            self.store.update_candidate_status(candidate_id, "rejected")
        return {
            "candidate_id": candidate_id,
            "report": report.to_dict(),
            "gate": decision.to_dict(),
            "rejected": not decision.approved,
        }

    def get_candidate(self, candidate_id: str) -> dict:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise ManagerError(f"候选不存在: {candidate_id}")
        return candidate

    def list_candidates(self) -> list[dict]:
        return self.store.list_candidates(self.source_id)

    def reject_candidate(self, candidate_id: str) -> None:
        """显式拒绝（即使已通过门槛也可人工拒绝）。"""
        self.get_candidate(candidate_id)
        self.store.update_candidate_status(candidate_id, "rejected")

    def approve_candidate(self, candidate_id: str) -> dict:
        """确认门槛通过并允许进入灰度。未通过门槛一律拒绝。"""
        candidate = self.get_candidate(candidate_id)
        if not candidate["gate"]["approved"]:
            self.store.update_candidate_status(candidate_id, "rejected")
            raise ManagerError(
                f"候选 {candidate_id} 未通过门槛: {candidate['gate']['reasons']}"
            )
        self.store.update_candidate_status(candidate_id, "approved")
        return candidate

    # ------------------------------------------------------------------
    # 灰度、启用、回滚（全部按来源，单事务原子切换）
    # ------------------------------------------------------------------

    def set_canary(self, candidate_id: str, percent: int | None = None) -> dict:
        candidate = self.approve_candidate(candidate_id)
        percent = self.policy.canary_percent if percent is None else percent
        if not 0 <= percent <= 100:
            raise ManagerError(f"灰度百分比越界: {percent}")
        self.store.set_canary_config(self.source_id, candidate_id, percent)
        return {
            "source_id": self.source_id,
            "candidate_id": candidate_id,
            "rule_version": candidate["rule_version"],
            "canary_percent": percent,
        }

    def get_canary(self) -> dict | None:
        return self.store.get_canary_config(self.source_id)

    def canary_preview(self, percent: int | None = None) -> list[dict]:
        """按确定性桶号预览每个存档页面的分桶，重跑结果稳定不变。"""
        percent = self.policy.canary_percent if percent is None else percent
        rows = []
        for page in self._pages:
            bucket = bucket_of(page.record_id)
            rows.append(
                {
                    "record_id": page.record_id,
                    "page": page.page,
                    "bucket": bucket,
                    "route": "candidate" if bucket < percent else "baseline",
                }
            )
        return rows

    def enable(self, candidate_id: str) -> dict:
        """门槛通过后全量启用：单事务把当前版本切到候选（percent=100）。"""
        candidate = self.approve_candidate(candidate_id)
        version = candidate["rule_version"]
        self.store.set_current_version(
            self.source_id,
            version,
            action="enable",
            detail={"candidate_id": candidate_id, "report": candidate["report"]},
        )
        self.store.set_canary_config(self.source_id, candidate_id, 100)
        self.store.update_candidate_status(candidate_id, "enabled")
        return {"source_id": self.source_id, "current_version": version}

    def rollback(self, reason: str) -> dict:
        """原子回滚到上一版本。

        新版本已经产生的作业、结果和重放证据全部保留，仅对外当前视图恢复。
        已经回滚过（当前版本不再是任何启用事件的目标）时显式拒绝，避免
        产生 from==to 的空转事件。
        """
        current = self.store.get_current_version(self.source_id)
        events = self.store.list_release_events(self.source_id)
        # 只找“把当前版本切上去”的那次启用，保证回滚目标明确。
        enable_events = [
            event for event in events
            if event["action"] == "enable" and event["to_version"] == current
        ]
        if not enable_events:
            raise ManagerError("当前版本没有可回滚的启用记录")
        latest = enable_events[-1]
        target = latest["from_version"]
        if not target:
            raise ManagerError("首次启用的规则没有可回滚的旧版本")
        self.store.set_current_version(
            self.source_id,
            target,
            action="rollback",
            detail={"reason": reason, "from_enable_event": latest["event_id"]},
        )
        canary = self.store.get_canary_config(self.source_id)
        if canary:
            self.store.set_canary_config(self.source_id, canary["candidate_id"], 0)
        # 候选状态标记为 rolled_back，但证据行不删除。
        for candidate in self.store.list_candidates(self.source_id):
            if candidate["rule_version"] == latest["to_version"]:
                self.store.update_candidate_status(candidate["candidate_id"], "rolled_back")
        return {
            "source_id": self.source_id,
            "current_version": target,
            "rolled_back_from": latest["to_version"],
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # 在途作业：领取时绑定版本，路由由确定性灰度决定
    # ------------------------------------------------------------------

    def _route_version(self, record_id: str) -> tuple[str, int, str]:
        """返回 (规则版本, 桶号, 路由原因)。"""
        baseline = self.store.get_current_version(self.source_id)
        if baseline is None:
            raise ManagerError(f"来源 {self.source_id} 尚未设置当前规则版本")
        canary = self.store.get_canary_config(self.source_id)
        bucket = bucket_of(record_id)
        if canary and use_candidate(record_id, canary["canary_percent"]):
            candidate = self.store.get_candidate(canary["candidate_id"])
            return candidate["rule_version"], bucket, "canary"
        return baseline, bucket, "baseline"

    def set_initial_version(self, version: str) -> None:
        """初始化来源的当前规则（基线），不记 enable 事件、不可作为回滚目标。"""
        self._pack_for_source(version)
        if self.store.get_current_version(self.source_id) is None:
            self.store.set_current_version(
                self.source_id, version, action="bootstrap", detail={}
            )

    def claim_job(self, record_id: str, page: str, run_id: str) -> dict:
        version, bucket, route = self._route_version(record_id)
        job_id = self.store.claim_job(
            source_id=self.source_id,
            record_id=record_id,
            page=page,
            run_id=run_id,
            rule_version=version,
            bucket=bucket,
        )
        return {"job_id": job_id, "rule_version": version, "bucket": bucket, "route": route}

    def complete_job_with_html(self, job_id: str, html: bytes) -> RecordResult:
        """用存档 HTML 完成作业。HTML 仅作为数据解析，绝不执行其中任何内容。

        记录级校验（缺字段/转换/时间倒挂）在抽取时完成；跨记录地点冲突
        需要同轮全量视角，由 run_batch 统一检测。
        """
        jobs = [job for job in self.store.list_jobs(self.source_id) if job["job_id"] == job_id]
        if not jobs:
            raise ManagerError(f"作业不存在: {job_id}")
        job = jobs[0]
        pack = self.registry.get(job["rule_version"])  # 领取时绑定的版本
        root = parse_html(html)
        article = select_one(root, "article")
        features = {"layout": article.attrs.get("data-layout", "")} if article else {}
        result = pack.extract(
            root,
            source_id=self.source_id,
            record_id=job["record_id"],
            page=job["page"],
            page_features=features,
            run_id=job["run_id"],
        )
        self.store.complete_job(job_id, result)
        return result

    def run_batch(self, run_id: str) -> list[RecordResult]:
        """对全部存档页面跑一轮：逐记录领取（绑定版本）后解析，再做跨记录冲突检测。

        冲突双方一并隔离；冲突导致的状态变化会回写到结果库，保证库里的
        状态与返回结果一致，不输出半条正常记录。
        """
        jobs = []
        for page in self._pages:
            claim = self.claim_job(page.record_id, page.page, run_id)
            jobs.append((claim["job_id"], page))
        results = [self.complete_job_with_html(job_id, page.html) for job_id, page in jobs]
        detect_cross_record_conflicts(results)
        # 跨记录检测可能把 ok 改成 quarantined，按最终状态回写证据行。
        for job_id, result in zip((job_id for job_id, _ in jobs), results):
            self.store.overwrite_result_status(job_id, result)
        return results

    # ------------------------------------------------------------------
    # 当前视图与证据导出
    # ------------------------------------------------------------------

    def current_view(self) -> dict:
        """对外当前视图：只暴露当前版本产生的正常记录；隔离记录走隔离区。"""
        version = self.store.get_current_version(self.source_id)
        rows = self.store.list_results(self.source_id)
        current = [row for row in rows if row["rule_version"] == version]
        return {
            "source_id": self.source_id,
            "current_version": version,
            "ok_records": [row["result"] for row in current if row["status"] == "ok"],
            "quarantine": [row["result"] for row in current if row["status"] == "quarantined"],
            "retained_other_version_evidence": [
                {"record_id": row["record_id"], "rule_version": row["rule_version"], "status": row["status"]}
                for row in rows
                if row["rule_version"] != version
            ],
        }

    def export_provenance(self, record_id: str) -> dict:
        """导出某条结果的逐字段来源（原文、DOM 位置、转换错误、规则版本）。"""
        row = self.store.get_result_by_record(self.source_id, record_id)
        if row is None:
            raise ManagerError(f"没有该记录的抽取结果: {record_id}")
        result = row["result"]
        current_version = self.store.get_current_version(self.source_id)
        return {
            "source_id": result["source_id"],
            "record_id": result["record_id"],
            "page": result["page"],
            "run_id": result["run_id"],
            "rule_version": result["rule_version"],
            "is_current_view": result["rule_version"] == current_version,
            "status": result["status"],
            "issues": result["issues"],
            "field_sources": result["fields"],
        }

    # ------------------------------------------------------------------

    def _pack_for_source(self, version: str) -> RulePack:
        pack = self.registry.get(version)
        if self.source_id not in pack.applies_to_sources:
            raise RuleError(f"规则 {version} 不适用于来源 {self.source_id}")
        return pack
