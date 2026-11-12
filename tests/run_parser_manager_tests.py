"""解析规则管理模块的可复现检查。

覆盖需求中的硬性场景：
- 页面改版导致的人数串位必须被发现，坏候选被门槛拒绝；
- 好候选离线重放通过门槛，隔离比例按页面数计算；
- 灰度分桶确定性、重跑不换组；
- 在途作业绑定领取时版本，切换不影响在途；
- 启用/回滚原子切换，回滚后视图恢复旧版但保留新版本证据；
- 缺字段、时间倒挂、跨记录地点冲突一律隔离，不输出半条正常记录；
- 规则版本不可变；网页代码不会被执行；离线重放不访问网络。

直接以文件方式运行（兼容 python -I 隔离模式）：
    python -I tests/run_parser_manager_tests.py
"""
import json
import os
import pathlib
import socket
import sys
import tempfile
import unittest

# 隔离模式下显式引导包根。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from parser_manager.canary import bucket_of, use_candidate  # noqa: E402
from parser_manager.dom import parse_html, select_all, select_one  # noqa: E402
from parser_manager.fields import (  # noqa: E402
    ConversionError,
    RecordResult,
    convert_non_negative_int,
    detect_cross_record_conflicts,
    validate_record,
)
from parser_manager.gate import ReleasePolicy, evaluate_gate  # noqa: E402
from parser_manager.manager import ManagerError, ReleaseManager  # noqa: E402
from parser_manager.replay import (  # noqa: E402
    load_archived_pages,
    load_confirmed,
    replay,
)
from parser_manager.rules import RuleError, RuleRegistry  # noqa: E402
from parser_manager.store import Store  # noqa: E402

SOURCE = "science_station"
ARCHIVE = ROOT / "sources"
CONFIRMED = ARCHIVE / "confirmed.json"
POLICY = ARCHIVE / "release-policy.json"
RULES_DIR = ROOT / "trusted_rules"


def build_manager(temp_dir: pathlib.Path) -> ReleaseManager:
    registry = RuleRegistry(RULES_DIR)
    registry.load_directory()
    store = Store(temp_dir / "state.db")
    policy = ReleasePolicy.load(POLICY)
    manager = ReleaseManager(
        store=store,
        registry=registry,
        archive_dir=ARCHIVE,
        confirmed_path=CONFIRMED,
        policy=policy,
        source_id=SOURCE,
    )
    manager.set_initial_version("v1-legacy")
    return manager


class DomTests(unittest.TestCase):
    def test_selector_engine_features(self):
        root = parse_html((ARCHIVE / "new.html").read_bytes())
        self.assertEqual(select_one(root, "span.number").text_content(), "2026")
        self.assertEqual(select_one(root, "p[data-field='participants']").text_content(), "36")
        self.assertEqual(select_one(root, "time:nth-of-type(2)").attrs["data-role"], "end")
        # 逗号分组
        joined = select_all(root, "span.place, p[data-field='place']")
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0].text_content(), "操场")
        # DOM 位置稳定
        node = select_one(root, "p[data-field='participants']")
        self.assertEqual(node.dom_path(), "article[0] > p[1]")
        self.assertIn('data-field="participants"', node.css_path())

    def test_dom_path_stable_across_reparse(self):
        paths = set()
        for _ in range(3):
            root = parse_html((ARCHIVE / "old.html").read_bytes())
            paths.add(select_one(root, "span.number").dom_path())
        self.assertEqual(paths, {"article[0] > span[1]"})


class FieldValidationTests(unittest.TestCase):
    def test_participants_must_be_non_negative_int(self):
        self.assertEqual(convert_non_negative_int("80"), 80)
        self.assertEqual(convert_non_negative_int("0"), 0)
        for bad in ("约三十", "-1", "1.5", "", "36人"):
            with self.assertRaises(ConversionError):
                convert_non_negative_int(bad)

    def test_mixed_page_quarantines_without_half_record(self):
        registry = RuleRegistry(RULES_DIR)
        registry.load_directory()
        pack = registry.get("v2-layout")
        root = parse_html((ARCHIVE / "mixed.html").read_bytes())
        result = pack.extract(
            root, source_id=SOURCE, record_id="a3", page="mixed.html",
            page_features={"layout": "new"},
        )
        self.assertEqual(result.status, "quarantined")
        issues = " ".join(result.issues)
        self.assertIn("参与人数不是非负整数", issues)
        self.assertIn("结束时间", issues)
        self.assertIn("早于开始时间", issues)
        # 没有默认值填充
        self.assertIsNone(result.fields["participants"].value)

    def test_cross_record_place_conflict_quarantines_both(self):
        def make_result(record_id: str, place: str) -> RecordResult:
            result = RecordResult(source_id=SOURCE, record_id=record_id,
                                  page=f"{record_id}.html", rule_version="vX")
            from parser_manager.fields import FieldEvidence
            result.fields = {
                "activity_id": FieldEvidence("activity_id", "a9", "p", "c", "a9"),
                "place": FieldEvidence("place", place, "p", "c", place),
            }
            result.status = "ok"
            return result

        r1 = make_result("r1", "东门")
        r2 = make_result("r2", "西门")
        detect_cross_record_conflicts([r1, r2])
        self.assertEqual(r1.status, "quarantined")
        self.assertEqual(r2.status, "quarantined")
        self.assertTrue(any("冲突" in issue for issue in r1.issues))

        # 同地点不冲突
        r3 = make_result("r3", "东门")
        r4 = make_result("r4", "东门")
        detect_cross_record_conflicts([r3, r4])
        self.assertEqual(r3.status, "ok")
        self.assertEqual(r4.status, "ok")


class RulePackTests(unittest.TestCase):
    def test_version_immutability_rejects_changed_content(self):
        registry = RuleRegistry(RULES_DIR)
        registry.load_directory()
        pack = registry.get("v1-legacy")
        # 同版本号同内容重复登记是幂等的
        registry.register(pack)
        # 篡改描述后同版本号再登记必须拒绝
        pack.description = "被篡改的描述"
        with self.assertRaises(RuleError):
            registry.register(pack)

    def test_pack_must_declare_source(self):
        from parser_manager.rules import RulePack, FieldSpec
        from dataclasses import dataclass, field

        @dataclass
        class Orphan(RulePack):
            version: str = "v-orphan"
            description: str = "x"
            applies_to_sources: tuple = ()
            page_features: dict = field(default_factory=dict)

            def parse(self, root):
                return {"place": FieldSpec("span.place")}

        registry = RuleRegistry(RULES_DIR)
        with self.assertRaises(RuleError):
            registry.register(Orphan())

    def test_html_is_never_executed_as_code(self):
        # 即便页面中出现可导入名与内置表达式，也只被当文本解析。
        malicious = (
            '<article data-id="a9" data-layout="old">'
            '<span class="place">__import__("os").system("echo pwned")</span>'
            '<span class="number">12</span>'
            '<time class="start">2026-11-12T09:00:00+08:00</time>'
            '<time class="end">2026-11-12T11:00:00+08:00</time>'
            '</article>'
        )
        registry = RuleRegistry(RULES_DIR)
        registry.load_directory()
        result = registry.get("v1-legacy").extract(
            parse_html(malicious), source_id=SOURCE, record_id="a9",
            page="evil.html", page_features={"layout": "old"},
        )
        self.assertEqual(result.status, "ok")
        self.assertIn("__import__", result.fields["place"].raw)
        self.assertEqual(result.fields["place"].value, '__import__("os").system("echo pwned")')

    def test_registry_refuses_files_outside_trusted_dir(self):
        registry = RuleRegistry(RULES_DIR)
        outside = pathlib.Path(tempfile.mkdtemp()) / "evil.py"
        outside.write_text("class X: pass\n", encoding="utf-8")
        with self.assertRaises(RuleError):
            registry._load_file(outside)

    def test_registry_rejects_missing_directory(self):
        with self.assertRaises(RuleError):
            RuleRegistry(pathlib.Path(tempfile.mkdtemp()) / "no_such_dir").load_directory()


class ReplayAndGateTests(unittest.TestCase):
    def setUp(self):
        self.pages = load_archived_pages(ARCHIVE, SOURCE)
        self.confirmed = load_confirmed(CONFIRMED)
        self.registry = RuleRegistry(RULES_DIR)
        self.registry.load_directory()
        self.policy = ReleasePolicy.load(POLICY)

    def test_naive_candidate_silently_shifts_participants_and_gets_rejected(self):
        baseline = self.registry.get("v1-legacy")
        naive = self.registry.get("v2-naive")
        report = replay(naive, self.pages, self.confirmed, baseline=baseline)
        # 串位被人工确认抓住：new.html 人数 36 被抽成 2026
        miss = next(m for m in report.confirmed_misses if m["page"] == "new.html"
                    and m["field"] == "participants")
        self.assertEqual(miss["expected"], 36)
        self.assertEqual(miss["actual"], 2026)
        self.assertEqual(miss["raw"], "2026")
        # 分母 6 = 两份样本各 3 个非空确认字段
        self.assertEqual(report.confirmed_total, 6)
        self.assertEqual(report.confirmed_matched, 5)
        self.assertAlmostEqual(report.confirmed_match_rate, 5 / 6)
        decision = evaluate_gate(report, self.policy)
        self.assertFalse(decision.approved)
        self.assertTrue(any("匹配率" in reason for reason in decision.reasons))

    def test_layout_aware_candidate_passes_gate(self):
        baseline = self.registry.get("v1-legacy")
        good = self.registry.get("v2-layout")
        report = replay(good, self.pages, self.confirmed, baseline=baseline)
        self.assertEqual(report.confirmed_total, 6)
        self.assertEqual(report.confirmed_matched, 6)
        self.assertEqual(report.confirmed_match_rate, 1.0)
        # 隔离比例按页面数：mixed 一页隔离 => 1/3
        self.assertEqual(report.candidate_quarantined, 1)
        self.assertAlmostEqual(report.quarantine_ratio, 1 / 3)
        self.assertLessEqual(report.quarantine_ratio, self.policy.maximum_quarantine_ratio)
        decision = evaluate_gate(report, self.policy)
        self.assertTrue(decision.approved, decision.reasons)
        # 基线隔离 2 页 -> 候选 1 页，报告隔离变化
        self.assertEqual(report.baseline_quarantined, 2)
        change = next(c for c in report.status_changes if c["page"] == "new.html")
        self.assertEqual(change, {"page": "new.html", "baseline": "quarantined",
                                  "candidate": "ok"})

    def test_missing_confirmed_sample_blocks_auto_enable(self):
        good = self.registry.get("v2-layout")
        report = replay(good, self.pages, confirmed={}, baseline=None)
        self.assertFalse(report.has_confirmed_samples)
        decision = evaluate_gate(report, self.policy)
        self.assertFalse(decision.approved)
        self.assertIn("没有人工确认样本，禁止自动启用", decision.reasons)

    def test_actual_missing_field_counts_as_mismatch(self):
        # 构造一份要求 place 的确认，但规则在该页抽不到 place
        confirmed = {"new.html": {"place": "操场"}}
        legacy = self.registry.get("v1-legacy")
        report = replay(legacy, self.pages, confirmed, baseline=None)
        self.assertEqual(report.confirmed_total, 1)
        self.assertEqual(report.confirmed_matched, 0)
        miss = report.confirmed_misses[0]
        self.assertEqual(miss["reason"], "missing")

    def test_replay_never_opens_network(self):
        # 断网开关：任何 socket 使用都会失败，离线重放必须照常完成。
        def deny(*_args, **_kwargs):
            raise AssertionError("离线重放不允许访问网络")

        original = socket.socket
        socket.socket = deny
        try:
            report = replay(self.registry.get("v2-layout"), self.pages,
                            self.confirmed, baseline=self.registry.get("v1-legacy"))
            self.assertEqual(report.page_count, 3)
        finally:
            socket.socket = original


class CanaryTests(unittest.TestCase):
    def test_bucket_matches_spec(self):
        import hashlib
        for record_id in ("a1", "a2", "a3"):
            expected = int(hashlib.sha256(record_id.encode()).hexdigest()[:8], 16) % 100
            self.assertEqual(bucket_of(record_id), expected)

    def test_deterministic_and_stable(self):
        first = [bucket_of(rid) for rid in ("a1", "a2", "a3")]
        second = [bucket_of(rid) for rid in ("a1", "a2", "a3")]
        self.assertEqual(first, second)
        self.assertEqual(first, [47, 61, 26])

    def test_routing_thresholds(self):
        self.assertTrue(use_candidate("a3", 27))   # 桶 26 < 27
        self.assertFalse(use_candidate("a3", 26))  # 边界：小于才走新版本
        self.assertFalse(use_candidate("a1", 25))
        self.assertTrue(use_candidate("a1", 100))
        self.assertFalse(use_candidate("a1", 0))
        with self.assertRaises(ValueError):
            use_candidate("a1", 101)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.temp_dir = pathlib.Path(self.temp.name)
        self.manager = build_manager(self.temp_dir)

    def tearDown(self):
        self.manager.store.close()
        self.temp.cleanup()

    def _register(self, version: str) -> dict:
        return self.manager.register_candidate(version)

    def test_bad_candidate_rejected_and_does_not_touch_current(self):
        before = self.manager.store.get_current_version(SOURCE)
        outcome = self._register("v2-naive")
        self.assertTrue(outcome["rejected"])
        candidate = self.manager.get_candidate(outcome["candidate_id"])
        self.assertEqual(candidate["status"], "rejected")
        # 当前正常输出不受候选失败影响
        self.assertEqual(self.manager.store.get_current_version(SOURCE), before)
        with self.assertRaises(ManagerError):
            self.manager.set_canary(outcome["candidate_id"], 10)

    def test_good_candidate_canary_stable_buckets(self):
        outcome = self._register("v2-layout")
        self.assertFalse(outcome["rejected"])
        cid = outcome["candidate_id"]
        self.manager.set_canary(cid, 50)
        first = self.manager.canary_preview(50)
        second = self.manager.canary_preview(50)
        self.assertEqual(first, second)  # 重跑不换组
        routes = {row["record_id"]: row["route"] for row in first}
        self.assertEqual(routes, {"a1": "candidate", "a2": "baseline", "a3": "candidate"})
        # 25% 时全部留在旧版（最小桶号 26）
        self.assertTrue(all(row["route"] == "baseline"
                            for row in self.manager.canary_preview(25)))

    def test_inflight_job_keeps_claimed_version_after_switch(self):
        cid = self._register("v2-layout")["candidate_id"]
        self.manager.set_canary(cid, 50)
        claim = self.manager.claim_job("a1", "old.html", "round-inflight")
        self.assertEqual(claim["rule_version"], "v2-layout")
        # 全量启用：当前版本原子切换
        self.manager.enable(cid)
        self.assertEqual(self.manager.store.get_current_version(SOURCE), "v2-layout")
        # 在途作业仍绑定领取时版本
        result = self.manager.complete_job_with_html(
            claim["job_id"], (ARCHIVE / "old.html").read_bytes())
        self.assertEqual(result.rule_version, "v2-layout")
        self.assertEqual(result.status, "ok")

    def test_enable_is_atomic_and_recorded(self):
        cid = self._register("v2-layout")["candidate_id"]
        self.manager.enable(cid)
        events = self.manager.store.list_release_events(SOURCE)
        enable = [e for e in events if e["action"] == "enable"][-1]
        self.assertEqual(enable["from_version"], "v1-legacy")
        self.assertEqual(enable["to_version"], "v2-layout")
        self.assertEqual(self.manager.store.get_current_version(SOURCE), "v2-layout")

    def test_rollback_restores_view_but_keeps_new_evidence(self):
        cid = self._register("v2-layout")["candidate_id"]
        self.manager.enable(cid)
        results = self.manager.run_batch("round-new")
        self.assertTrue(all(r.rule_version == "v2-layout" for r in results))
        new_ok = [r for r in results if r.status == "ok"]
        self.assertEqual({r.record_id for r in new_ok}, {"a1", "a2"})

        rollback = self.manager.rollback("复现回滚")
        self.assertEqual(rollback["current_version"], "v1-legacy")
        view = self.manager.current_view()
        self.assertEqual(view["current_version"], "v1-legacy")
        # 新版本证据仍保留在库中，但不属于当前视图
        retained_versions = {row["rule_version"] for row in view["retained_other_version_evidence"]}
        self.assertIn("v2-layout", retained_versions)
        # 回滚后领取的作业走旧版
        claim = self.manager.claim_job("a2", "new.html", "round-old-again")
        self.assertEqual(claim["rule_version"], "v1-legacy")
        # 回滚事件可追溯
        self.assertTrue(any(e["action"] == "rollback" for e in
                            self.manager.store.list_release_events(SOURCE)))

    def test_batch_routes_by_bucket_and_quarantines_bad_page(self):
        cid = self._register("v2-layout")["candidate_id"]
        self.manager.set_canary(cid, 50)
        results = self.manager.run_batch("round-50")
        by_id = {r.record_id: r for r in results}
        self.assertEqual(by_id["a1"].rule_version, "v2-layout")  # 桶 47
        self.assertEqual(by_id["a2"].rule_version, "v1-legacy")  # 桶 61
        self.assertEqual(by_id["a3"].rule_version, "v2-layout")  # 桶 26
        # 混用页面在新规则下仍因人数文本与时间倒挂被隔离
        self.assertEqual(by_id["a3"].status, "quarantined")
        self.assertEqual(by_id["a1"].status, "ok")
        # 没有半条正常记录：隔离记录不进 ok
        self.assertNotIn("a3", {r.record_id for r in results if r.status == "ok"})

    def test_provenance_export_contains_raw_dom_and_error(self):
        cid = self._register("v2-layout")["candidate_id"]
        # a3 桶号 26，灰度 27% 使其走新版本，才能取到原文“约三十”。
        self.manager.set_canary(cid, 27)
        self.manager.run_batch("round-prov")
        provenance = self.manager.export_provenance("a3")
        self.assertEqual(provenance["record_id"], "a3")
        sources = {f["field"]: f for f in provenance["field_sources"]}
        self.assertEqual(sources["participants"]["raw"], "约三十")
        self.assertIsNotNone(sources["participants"]["error"])
        self.assertEqual(sources["place"]["dom_path"], "article[0] > p[0]")
        self.assertEqual(provenance["rule_version"], "v2-layout")

    def test_rollback_without_history_is_rejected(self):
        with self.assertRaises(ManagerError):
            self.manager.rollback("无历史回滚")

    def test_double_rollback_is_rejected_without_noop_event(self):
        cid = self._register("v2-layout")["candidate_id"]
        self.manager.enable(cid)
        self.manager.rollback("第一次回滚")
        with self.assertRaises(ManagerError):
            self.manager.rollback("重复回滚必须拒绝")
        events = self.manager.store.list_release_events(SOURCE)
        self.assertFalse(
            any(e["action"] == "rollback" and e["from_version"] == e["to_version"]
                for e in events))

    def test_reject_candidate_explicitly(self):
        outcome = self._register("v2-layout")
        self.manager.reject_candidate(outcome["candidate_id"])
        self.assertEqual(self.manager.get_candidate(outcome["candidate_id"])["status"],
                         "rejected")

    def test_same_activity_different_place_conflict_quarantines_full_records(self):
        # 合成两页：同来源同活动编号 a9，但地点不同；数据本身各自合法。
        page_template = (
            '<article data-id="a9" data-layout="old"><h1>x</h1>'
            '<time class="start">2026-11-12T09:00:00+08:00</time>'
            '<time class="end">2026-11-12T11:00:00+08:00</time>'
            '<span class="place">{place}</span><span class="number">10</span></article>'
        )
        synth_dir = self.temp_dir / "synth_archive"
        synth_dir.mkdir()
        (synth_dir / "p1.html").write_text(page_template.format(place="东门"), encoding="utf-8")
        (synth_dir / "p2.html").write_text(page_template.format(place="西门"), encoding="utf-8")
        confirmed_path = self.temp_dir / "confirmed.json"
        confirmed_path.write_text("[]", encoding="utf-8")

        registry = RuleRegistry(RULES_DIR)
        registry.load_directory()
        store = Store(self.temp_dir / "conflict.db")
        manager = ReleaseManager(
            store=store, registry=registry, archive_dir=synth_dir,
            confirmed_path=confirmed_path, policy=self.manager.policy, source_id=SOURCE,
        )
        manager.set_initial_version("v1-legacy")
        results = manager.run_batch("conflict-round")
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.status == "quarantined" for r in results))
        self.assertTrue(all(
            any("冲突" in issue and "a9" in issue for issue in r.issues) for r in results))
        # 库中状态与返回一致，冲突记录不进入任何正常视图
        view = manager.current_view()
        self.assertEqual(view["ok_records"], [])
        self.assertEqual(len(view["quarantine"]), 2)
        store.close()


class CliSmokeTests(unittest.TestCase):
    """以子进程方式验证 CLI 在 python -I 隔离模式下完整可用。"""

    def test_cli_full_lifecycle_subprocess(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "cli.db"
            common = [
                "--db", str(db),
                "--rules-dir", str(RULES_DIR),
                "--archive", str(ARCHIVE),
                "--confirmed", str(CONFIRMED),
                "--policy", str(POLICY),
                "--source", SOURCE,
                "--bootstrap", "v1-legacy",
            ]
            cli = [sys.executable, "-I", str(ROOT / "parser_manager" / "cli.py")]

            def run(*extra):
                proc = subprocess.run(cli + list(extra) + common, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return json.loads(proc.stdout)

            bad = run("register", "v2-naive")
            self.assertTrue(bad["rejected"])
            good = run("register", "v2-layout")
            self.assertFalse(good["rejected"])
            cid = good["candidate_id"]
            canary = run("canary", cid, "--percent", "50")
            self.assertEqual({r["record_id"]: r["route"] for r in canary["buckets"]},
                             {"a1": "candidate", "a2": "baseline", "a3": "candidate"})
            batch = run("run-batch", "--run-id", "cli-round")
            self.assertEqual(len(batch["results"]), 3)
            run("enable", cid)
            view = run("view")
            self.assertEqual(view["current_version"], "v2-layout")
            run("rollback", "--reason", "cli 回滚")
            view_after = run("view")
            self.assertEqual(view_after["current_version"], "v1-legacy")
            # 回滚只切视图：新版本产生的证据行仍保留
            self.assertTrue(view_after["retained_other_version_evidence"])
            provenance = run("provenance", "a2")
            self.assertEqual(provenance["record_id"], "a2")
            self.assertTrue(provenance["field_sources"])


class PolicyTests(unittest.TestCase):
    def test_policy_loaded_from_config(self):
        policy = ReleasePolicy.load(POLICY)
        self.assertEqual(policy.minimum_confirmed_match, 1.0)
        self.assertEqual(policy.maximum_quarantine_ratio, 0.4)
        self.assertEqual(policy.canary_percent, 25)

    def test_quarantine_ratio_uses_page_count_denominator(self):
        pages = load_archived_pages(ARCHIVE, SOURCE)
        confirmed = load_confirmed(CONFIRMED)
        registry = RuleRegistry(RULES_DIR)
        registry.load_directory()
        report = replay(registry.get("v2-layout"), pages, confirmed)
        self.assertEqual(report.quarantine_ratio, report.candidate_quarantined / report.page_count)


if __name__ == "__main__":
    unittest.main(verbosity=2)
