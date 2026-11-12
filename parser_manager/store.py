"""SQLite 持久化与原子版本切换。

设计要点：
- 规则版本、候选登记、重放报告、门槛决策、灰度配置、作业与抽取结果全部落库；
- 启用/回滚是单事务内的 current_versions 行替换，外部观察不到中间态；
- 在途作业在领取时绑定具体规则版本，之后切换不影响已领取作业；
- 回滚只切换对外当前视图，新版本产生的证据（结果/报告/作业）原样保留。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .fields import RecordResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_versions (
    version       TEXT PRIMARY KEY,
    description   TEXT NOT NULL,
    sources       TEXT NOT NULL,
    page_features TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS current_versions (
    source_id      TEXT PRIMARY KEY,
    rule_version   TEXT NOT NULL,
    switched_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id    TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    rule_version    TEXT NOT NULL,
    baseline_version TEXT,
    status          TEXT NOT NULL,  -- registered | approved | rejected | enabled | rolled_back
    report_json     TEXT NOT NULL,
    gate_json       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canary_config (
    source_id       TEXT PRIMARY KEY,
    candidate_id    TEXT NOT NULL,
    canary_percent  INTEGER NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    record_id       TEXT NOT NULL,
    page            TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    rule_version    TEXT NOT NULL,  -- 领取时绑定，切换后不变
    bucket          INTEGER NOT NULL,
    status          TEXT NOT NULL,  -- claimed | completed
    claimed_at      TEXT NOT NULL,
    completed_at    TEXT
);
CREATE TABLE IF NOT EXISTS extraction_results (
    job_id       TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL,
    record_id    TEXT NOT NULL,
    page         TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    status       TEXT NOT NULL,  -- ok | quarantined
    result_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
CREATE TABLE IF NOT EXISTS release_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    TEXT NOT NULL,
    action       TEXT NOT NULL,  -- enable | rollback
    from_version TEXT,
    to_version   TEXT NOT NULL,
    detail_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- 规则版本 -----------------------------------------------------------

    def insert_rule_version(self, pack, content_hash: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO rule_versions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    pack.version,
                    pack.description,
                    json.dumps(list(pack.applies_to_sources), ensure_ascii=False),
                    json.dumps(pack.page_features, ensure_ascii=False),
                    content_hash,
                    _now(),
                ),
            )

    def list_rule_versions(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM rule_versions ORDER BY version").fetchall()
        return [dict(row) for row in rows]

    # -- 当前版本（原子切换） -----------------------------------------------

    def get_current_version(self, source_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT rule_version FROM current_versions WHERE source_id = ?", (source_id,)
        ).fetchone()
        return row["rule_version"] if row else None

    def set_current_version(self, source_id: str, version: str, action: str, detail: dict) -> None:
        """单事务完成版本切换与事件记录。"""
        with self.transaction() as conn:
            previous = conn.execute(
                "SELECT rule_version FROM current_versions WHERE source_id = ?", (source_id,)
            ).fetchone()
            previous_version = previous["rule_version"] if previous else None
            conn.execute(
                """INSERT INTO current_versions (source_id, rule_version, switched_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET rule_version=excluded.rule_version,
                                                       switched_at=excluded.switched_at""",
                (source_id, version, _now()),
            )
            conn.execute(
                "INSERT INTO release_events (source_id, action, from_version, to_version, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (source_id, action, previous_version, version, json.dumps(detail, ensure_ascii=False), _now()),
            )

    def list_release_events(self, source_id: str | None = None) -> list[dict]:
        if source_id is None:
            rows = self._conn.execute("SELECT * FROM release_events ORDER BY event_id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM release_events WHERE source_id = ? ORDER BY event_id", (source_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # -- 候选 ---------------------------------------------------------------

    def insert_candidate(self, candidate_id: str, source_id: str, rule_version: str,
                         baseline_version: str | None, report: dict, gate: dict) -> None:
        now = _now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id, source_id, rule_version, baseline_version, "registered",
                    json.dumps(report, ensure_ascii=False), json.dumps(gate, ensure_ascii=False),
                    now, now,
                ),
            )

    def update_candidate_status(self, candidate_id: str, status: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE candidates SET status = ?, updated_at = ? WHERE candidate_id = ?",
                (status, _now(), candidate_id),
            )

    def get_candidate(self, candidate_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["report"] = json.loads(data.pop("report_json"))
        data["gate"] = json.loads(data.pop("gate_json"))
        return data

    def list_candidates(self, source_id: str | None = None) -> list[dict]:
        if source_id is None:
            rows = self._conn.execute("SELECT * FROM candidates ORDER BY created_at").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM candidates WHERE source_id = ? ORDER BY created_at", (source_id,)
            ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["report"] = json.loads(data.pop("report_json"))
            data["gate"] = json.loads(data.pop("gate_json"))
            result.append(data)
        return result

    # -- 灰度配置 ------------------------------------------------------------

    def set_canary_config(self, source_id: str, candidate_id: str, percent: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO canary_config (source_id, candidate_id, canary_percent, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET candidate_id=excluded.candidate_id,
                       canary_percent=excluded.canary_percent, updated_at=excluded.updated_at""",
                (source_id, candidate_id, percent, _now()),
            )

    def get_canary_config(self, source_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM canary_config WHERE source_id = ?", (source_id,)
        ).fetchone()
        return dict(row) if row else None

    # -- 在途作业（领取时绑定版本） ------------------------------------------

    def claim_job(self, source_id: str, record_id: str, page: str, run_id: str,
                  rule_version: str, bucket: int) -> str:
        job_id = uuid.uuid4().hex
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, source_id, record_id, page, run_id, rule_version, bucket,
                 "claimed", _now(), None),
            )
        return job_id

    def complete_job(self, job_id: str, result: RecordResult) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'completed', completed_at = ? WHERE job_id = ?",
                (_now(), job_id),
            )
            conn.execute(
                "INSERT INTO extraction_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id, result.source_id, result.record_id, result.page, result.run_id,
                    result.rule_version, result.status,
                    json.dumps(result.to_dict(), ensure_ascii=False), _now(),
                ),
            )

    def overwrite_result_status(self, job_id: str, result: RecordResult) -> None:
        """跨记录冲突复检后，按最终结果覆盖状态与证据 JSON。"""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE extraction_results SET status = ?, result_json = ? WHERE job_id = ?",
                (result.status, json.dumps(result.to_dict(), ensure_ascii=False), job_id),
            )

    def list_jobs(self, source_id: str | None = None) -> list[dict]:
        if source_id is None:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY claimed_at").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE source_id = ? ORDER BY claimed_at", (source_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # -- 结果与当前视图 ------------------------------------------------------

    def get_result_by_record(self, source_id: str, record_id: str) -> dict | None:
        row = self._conn.execute(
            """SELECT * FROM extraction_results
               WHERE source_id = ? AND record_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (source_id, record_id),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["result"] = json.loads(data.pop("result_json"))
        return data

    def list_results(self, source_id: str | None = None, current_version: str | None = None) -> list[dict]:
        sql = "SELECT * FROM extraction_results WHERE 1=1"
        params: list[object] = []
        if source_id is not None:
            sql += " AND source_id = ?"
            params.append(source_id)
        if current_version is not None:
            sql += " AND rule_version = ?"
            params.append(current_version)
        sql += " ORDER BY created_at"
        rows = self._conn.execute(sql, params).fetchall()
        output = []
        for row in rows:
            data = dict(row)
            data["result"] = json.loads(data.pop("result_json"))
            output.append(data)
        return output
