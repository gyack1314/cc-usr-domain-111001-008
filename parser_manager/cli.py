"""命令行入口：登记候选、查看差异、灰度启用、回滚与证据导出。

所有数据路径都通过参数传入；离线重放只读本地存档，不访问网站。
在 python -I 隔离模式下运行时，由本模块自行把工作区根加入 sys.path。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# -I 隔离模式下 sys.path 不含脚本目录，显式引导包根。
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from parser_manager.gate import ReleasePolicy  # noqa: E402
from parser_manager.manager import ManagerError, ReleaseManager  # noqa: E402
from parser_manager.rules import RuleError, RuleRegistry  # noqa: E402
from parser_manager.store import Store  # noqa: E402


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def _build_manager(args: argparse.Namespace) -> ReleaseManager:
    registry = RuleRegistry(args.rules_dir)
    registry.load_directory()
    store = Store(args.db)
    policy = ReleasePolicy.load(args.policy)
    manager = ReleaseManager(
        store=store,
        registry=registry,
        archive_dir=args.archive,
        confirmed_path=args.confirmed,
        policy=policy,
        source_id=args.source,
    )
    if args.bootstrap and store.get_current_version(args.source) is None:
        manager.set_initial_version(args.bootstrap)
    return manager


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, help="SQLite 状态库路径")
    parser.add_argument("--rules-dir", required=True, help="受信规则目录")
    parser.add_argument("--archive", required=True, help="存档 HTML 目录（离线重放）")
    parser.add_argument("--confirmed", required=True, help="人工确认 JSON")
    parser.add_argument("--policy", required=True, help="启用门槛配置 JSON")
    parser.add_argument("--source", required=True, help="来源标识")
    parser.add_argument("--bootstrap", help="首次运行时设置的基线规则版本")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="解析规则灰度生命周期管理")
    sub = parser.add_subparsers(dest="command", required=True)

    p_rules = sub.add_parser("rules", help="列出已登记规则版本")
    _add_common(p_rules)

    p_register = sub.add_parser("register", help="登记候选并离线重放")
    _add_common(p_register)
    p_register.add_argument("version", help="候选规则版本号")

    p_candidates = sub.add_parser("candidates", help="查看候选及门槛结论")
    _add_common(p_candidates)

    p_diff = sub.add_parser("diff", help="查看某候选相对当前规则的字段差异")
    _add_common(p_diff)
    p_diff.add_argument("candidate_id")

    p_canary = sub.add_parser("canary", help="设置/查看灰度，并预览分桶")
    _add_common(p_canary)
    p_canary.add_argument("candidate_id", nargs="?")
    p_canary.add_argument("--percent", type=int)

    p_enable = sub.add_parser("enable", help="全量原子启用候选")
    _add_common(p_enable)
    p_enable.add_argument("candidate_id")

    p_rollback = sub.add_parser("rollback", help="原子回滚到上一版本（证据保留）")
    _add_common(p_rollback)
    p_rollback.add_argument("--reason", default="manual rollback")

    p_run = sub.add_parser("run-batch", help="对存档页面跑一轮灰度解析")
    _add_common(p_run)
    p_run.add_argument("--run-id", required=True)

    p_view = sub.add_parser("view", help="查看对外当前视图与隔离区")
    _add_common(p_view)

    p_prov = sub.add_parser("provenance", help="导出某条结果的字段来源")
    _add_common(p_prov)
    p_prov.add_argument("record_id")

    p_events = sub.add_parser("events", help="查看启用/回滚事件")
    _add_common(p_events)

    p_reject = sub.add_parser("reject", help="显式拒绝候选")
    _add_common(p_reject)
    p_reject.add_argument("candidate_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manager = _build_manager(args)
        store = manager.store

        if args.command == "rules":
            _print({"rule_versions": store.list_rule_versions(),
                    "current_version": store.get_current_version(args.source)})
        elif args.command == "register":
            _print(manager.register_candidate(args.version))
        elif args.command == "candidates":
            _print({"candidates": manager.list_candidates()})
        elif args.command == "diff":
            candidate = manager.get_candidate(args.candidate_id)
            report = candidate["report"]
            _print({
                "candidate_id": args.candidate_id,
                "rule_version": candidate["rule_version"],
                "status": candidate["status"],
                "field_diffs": report["field_diffs"],
                "status_changes": report["status_changes"],
                "confirmed_match_rate": report["confirmed_match_rate"],
                "confirmed_misses": report["confirmed_misses"],
                "quarantine_ratio": report["quarantine_ratio"],
                "baseline_quarantine_ratio": report["baseline_quarantine_ratio"],
                "gate": candidate["gate"],
            })
        elif args.command == "canary":
            if args.candidate_id:
                result = manager.set_canary(args.candidate_id, args.percent)
                _print({"canary": result, "buckets": manager.canary_preview(
                    result["canary_percent"])})
            else:
                _print({"canary": manager.get_canary(),
                        "buckets": manager.canary_preview(
                            args.percent if args.percent is not None
                            else manager.policy.canary_percent)})
        elif args.command == "enable":
            _print(manager.enable(args.candidate_id))
        elif args.command == "rollback":
            _print(manager.rollback(args.reason))
        elif args.command == "run-batch":
            results = manager.run_batch(args.run_id)
            _print({"run_id": args.run_id, "results": [r.to_dict() for r in results]})
        elif args.command == "view":
            _print(manager.current_view())
        elif args.command == "provenance":
            _print(manager.export_provenance(args.record_id))
        elif args.command == "events":
            _print({"events": store.list_release_events(args.source)})
        elif args.command == "reject":
            manager.reject_candidate(args.candidate_id)
            _print({"candidate_id": args.candidate_id, "status": "rejected"})
        else:  # pragma: no cover - argparse 已保证
            return 2
        return 0
    except (ManagerError, RuleError, FileNotFoundError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
