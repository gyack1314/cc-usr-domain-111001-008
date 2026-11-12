#!/usr/bin/env python3
"""端到端可复现演示：改版页面人数串位的发现、灰度、在途切换与回滚。

在隔离模式下运行（不读环境变量、不读用户站点包）：
    python3 -I examples/release_demo.py

脚本只访问 /workspace/sources 下的本地存档与配置，全程不发起网络请求；
状态写入临时目录，结束后打印完整生命周期证据。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from parser_manager.gate import ReleasePolicy
from parser_manager.manager import ReleaseManager
from parser_manager.rules import RuleRegistry
from parser_manager.store import Store

SOURCE = "science_station"


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    policy = ReleasePolicy.load(ROOT / "sources" / "release-policy.json")
    print(f"门槛配置: 匹配率下限={policy.minimum_confirmed_match} "
          f"隔离比例上限={policy.maximum_quarantine_ratio} 灰度={policy.canary_percent}%")

    temp = tempfile.TemporaryDirectory()
    try:
        registry = RuleRegistry(ROOT / "trusted_rules")
        registry.load_directory()
        store = Store(pathlib.Path(temp.name) / "demo.db")
        manager = ReleaseManager(
            store=store,
            registry=registry,
            archive_dir=ROOT / "sources",
            confirmed_path=ROOT / "sources" / "confirmed.json",
            policy=policy,
            source_id=SOURCE,
        )
        manager.set_initial_version("v1-legacy")

        banner("步骤 1 登记错误候选 v2-naive（旧选择器统一套用）")
        bad = manager.register_candidate("v2-naive")
        miss = bad["report"]["confirmed_misses"][0]
        print(f"结论: {'拒绝' if bad['rejected'] else '通过'}")
        print(f"发现串位: 页面 {miss['page']} 字段 {miss['field']} "
              f"人工确认={miss['expected']} 实际抽取={miss['actual']} 原文={miss['raw']!r}")
        print(f"匹配率 {bad['report']['confirmed_match_rate']:.4f} "
              f"(分母 {bad['report']['confirmed_total']} 个人工非空字段)")
        print(f"门槛原因: {bad['gate']['reasons']}")
        assert bad["rejected"], "坏候选必须被拒绝"

        banner("步骤 2 登记版式感知候选 v2-layout")
        good = manager.register_candidate("v2-layout")
        print(f"结论: {'拒绝' if good['rejected'] else '通过'}")
        print(f"匹配率={good['report']['confirmed_match_rate']:.4f} "
              f"隔离比例={good['report']['quarantine_ratio']:.4f} "
              f"(基线隔离比例={good['report']['baseline_quarantine_ratio']:.4f})")
        print("字段差异（相对当前规则）:")
        for diff in good["report"]["field_diffs"]:
            print(f"  {diff['page']} {diff['field']}: {diff['baseline']} -> {diff['candidate']}")
        assert not good["rejected"]
        cid = good["candidate_id"]

        banner("步骤 3 灰度 50%，检查确定性分桶（重跑不换组）")
        manager.set_canary(cid, 50)
        preview_1 = manager.canary_preview(50)
        preview_2 = manager.canary_preview(50)
        assert preview_1 == preview_2
        for row in preview_1:
            print(f"  记录 {row['record_id']} 桶号={row['bucket']:02d} 路由={row['route']}")

        banner("步骤 4 跑一轮灰度解析")
        results = manager.run_batch("demo-round-1")
        for result in results:
            participants = result.fields["participants"]
            print(f"  {result.record_id} 版本={result.rule_version} 状态={result.status} "
                  f"人数={participants.value} 原文={participants.raw!r}")
            for issue in result.issues:
                print(f"      隔离原因: {issue}")

        banner("步骤 5 在途作业绑定：先领取再全量启用")
        claim = manager.claim_job("a1", "old.html", "demo-inflight")
        print(f"  启用前领取 a1 -> 绑定 {claim['rule_version']} (job {claim['job_id'][:8]})")
        manager.enable(cid)
        print(f"  原子启用后当前版本: {store.get_current_version(SOURCE)}")
        inflight = manager.complete_job_with_html(
            claim["job_id"], (ROOT / "sources" / "old.html").read_bytes())
        print(f"  在途作业完成仍用: {inflight.rule_version}，状态={inflight.status}")
        assert inflight.rule_version == "v2-layout"

        banner("步骤 6 导出 a2 的字段来源证据")
        manager.run_batch("demo-round-2")
        provenance = manager.export_provenance("a2")
        for field_source in provenance["field_sources"]:
            print(f"  {field_source['field']:13s} 原文={field_source['raw']!r:42s} "
                  f"位置={field_source['dom_path']} 错误={field_source['error']}")

        banner("步骤 7 回滚：当前视图恢复旧版，新版本证据保留")
        rollback = manager.rollback("演示回滚")
        print(f"  {rollback}")
        view = manager.current_view()
        print(f"  当前视图版本={view['current_version']}")
        print(f"  保留的其他版本证据: "
              f"{[(r['record_id'], r['rule_version']) for r in view['retained_other_version_evidence']]}")
        post_claim = manager.claim_job("a2", "new.html", "demo-after-rollback")
        print(f"  回滚后新领取 a2 -> {post_claim['rule_version']}")
        assert post_claim["rule_version"] == "v1-legacy"

        banner("步骤 8 发布事件审计链")
        for event in store.list_release_events(SOURCE):
            detail = json.loads(event["detail_json"])
            print(f"  #{event['event_id']} {event['action']:9s} "
                  f"{event['from_version']} -> {event['to_version']} {detail if detail else ''}")

        print("\n演示完成：串位被发现、坏候选被拒绝、好候选灰度启用、在途版本固定、回滚可追溯。")
        return 0
    finally:
        temp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
