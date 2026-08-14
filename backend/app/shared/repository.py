"""封装扫描任务、发现项及复核记录的 SQLite 持久化操作。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from app.shared.errors import ConflictError, NotFoundError


DATABASE_SCHEMA_VERSION = 3
MANUAL_REVIEW_STATUSES = {"confirmed", "manual_false_positive"}


def scope_finding_id(run_id: str, finding: dict[str, Any]) -> str:
    """规范化 finding 的语义 ID 与 run 作用域 API ID。"""

    prefix = f"{run_id}_"
    finding_id = str(finding["id"])
    base_id = str(finding.get("base_id") or "")
    if base_id.startswith(prefix):
        base_id = base_id[len(prefix):]
    if not base_id:
        base_id = finding_id[len(prefix):] if finding_id.startswith(prefix) else finding_id
    scoped_id = f"{prefix}{base_id}"
    finding["base_id"] = base_id
    finding["id"] = scoped_id
    return scoped_id


def utc_now() -> str:
    """返回带 UTC 时区的 ISO 8601 时间字符串。"""

    return datetime.now(UTC).isoformat()


class SQLiteRepository:
    """以短连接事务封装任务、发现项和复核历史的持久化。"""

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """提供启用外键与 WAL 的短连接事务，退出时提交并关闭连接。"""

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """幂等创建基础表，并按已记录版本逐步迁移旧库。

        每步先检查现有列/数据形状，完成后才记录版本；中断后重复启动可从未记录步骤继续，
        ``user_version`` 只在全部步骤完成后推进。连接事务负责回滚尚未提交的数据改写。
        """

        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    apk_filename TEXT NOT NULL,
                    apk_sha256 TEXT NOT NULL,
                    authorized INTEGER NOT NULL CHECK (authorized = 1),
                    config_json TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    pipeline_version TEXT NOT NULL DEFAULT '1.0.0',
                    schema_version TEXT NOT NULL DEFAULT '1.0.0',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC);
                CREATE TABLE IF NOT EXISTS findings (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    rule_ids_json TEXT NOT NULL,
                    title TEXT NOT NULL,
                    component TEXT NOT NULL,
                    component_name TEXT,
                    severity TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    evidence_level TEXT NOT NULL,
                    review_status TEXT NOT NULL DEFAULT 'pending_ai',
                    review_reason TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_findings_run_id ON findings(run_id);
                CREATE TABLE IF NOT EXISTS review_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
                    old_status TEXT NOT NULL,
                    new_status TEXT NOT NULL,
                    reason TEXT,
                    actor TEXT NOT NULL DEFAULT 'human',
                    request_id TEXT,
                    basis TEXT,
                    expected_status TEXT,
                    previous_status TEXT,
                    request_payload_json TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            applied = {
                row[0] for row in db.execute("SELECT version FROM schema_migrations").fetchall()
            }
            if 1 not in applied:
                self._record_migration(db, 1)
            if 2 not in applied:
                self._migrate_review_v2(db)
                self._record_migration(db, 2)
            if 3 not in applied:
                self._migrate_scoped_finding_ids_v3(db)
                self._record_migration(db, 3)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_findings_run_active "
                "ON findings(run_id, deleted_at)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_review_history_request_id "
                "ON review_history(request_id) WHERE request_id IS NOT NULL"
            )
            db.execute(f"PRAGMA user_version={DATABASE_SCHEMA_VERSION}")

    @staticmethod
    def _record_migration(db: sqlite3.Connection, version: int) -> None:
        db.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, utc_now()),
        )

    @staticmethod
    def _migrate_review_v2(db: sqlite3.Connection) -> None:
        """为旧库补充软删除和可幂等复核字段，不重建或删除已有表。"""

        run_columns = {
            row[1] for row in db.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "pipeline_version" not in run_columns:
            db.execute(
                "ALTER TABLE runs ADD COLUMN pipeline_version TEXT NOT NULL DEFAULT '1.0.0'"
            )
        if "schema_version" not in run_columns:
            db.execute(
                "ALTER TABLE runs ADD COLUMN schema_version TEXT NOT NULL DEFAULT '1.0.0'"
            )

        finding_columns = {
            row[1] for row in db.execute("PRAGMA table_info(findings)").fetchall()
        }
        if "deleted_at" not in finding_columns:
            db.execute("ALTER TABLE findings ADD COLUMN deleted_at TEXT")

        history_columns = {
            row[1] for row in db.execute("PRAGMA table_info(review_history)").fetchall()
        }
        additions = {
            "actor": "TEXT NOT NULL DEFAULT 'legacy'",
            "request_id": "TEXT",
            "basis": "TEXT",
            "expected_status": "TEXT",
            "previous_status": "TEXT",
            "request_payload_json": "TEXT",
        }
        for column, definition in additions.items():
            if column not in history_columns:
                db.execute(f"ALTER TABLE review_history ADD COLUMN {column} {definition}")
        db.execute(
            "UPDATE review_history SET previous_status=old_status "
            "WHERE previous_status IS NULL"
        )

    @staticmethod
    def _migrate_scoped_finding_ids_v3(db: sqlite3.Connection) -> None:
        """将 v1/v2 base ID 迁移为 run-scoped ID，并确定性合并重复记录。

        canonical payload 优先保留，人工复核状态/较新记录决定 review winner；历史外键及其
        request payload 一并改写后才删除旧 ID。整步在初始化事务内执行，可安全回滚。
        """

        rows = db.execute("SELECT * FROM findings ORDER BY run_id, id").fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            prefix = f"{row['run_id']}_"
            base_id = str(payload.get("base_id") or "")
            if base_id.startswith(prefix):
                base_id = base_id[len(prefix):]
            if not base_id:
                base_id = row["id"][len(prefix):] if row["id"].startswith(prefix) else row["id"]
            groups.setdefault(f"{prefix}{base_id}", []).append(row)

        for scoped_id, members in groups.items():
            run_id = members[0]["run_id"]
            prefix = f"{run_id}_"
            base_id = scoped_id[len(prefix):]
            canonical = next((row for row in members if row["id"] == scoped_id), None)
            winner = max(
                members,
                key=lambda row: (
                    row["review_status"] in MANUAL_REVIEW_STATUSES,
                    row["updated_at"],
                    row["id"] == scoped_id,
                    row["id"],
                ),
            )
            payload_source = canonical or winner
            payload = json.loads(payload_source["payload_json"])
            payload["id"] = scoped_id
            payload["base_id"] = base_id
            created_at = min(row["created_at"] for row in members)
            deleted_values = [row["deleted_at"] for row in members]
            deleted_at = None if any(value is None for value in deleted_values) else max(deleted_values)

            if canonical is None:
                db.execute(
                    """INSERT INTO findings
                    (id, run_id, rule_ids_json, title, component, component_name, severity,
                     confidence, evidence_level, review_status, review_reason, payload_json,
                     created_at, updated_at, deleted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        scoped_id, run_id, payload_source["rule_ids_json"], payload_source["title"],
                        payload_source["component"], payload_source["component_name"],
                        payload_source["severity"], payload_source["confidence"],
                        payload_source["evidence_level"], winner["review_status"],
                        winner["review_reason"], json.dumps(payload, ensure_ascii=False),
                        created_at, winner["updated_at"], deleted_at,
                    ),
                )
            else:
                db.execute(
                    """UPDATE findings SET review_status=?, review_reason=?, payload_json=?,
                    created_at=?, updated_at=?, deleted_at=? WHERE id=?""",
                    (
                        winner["review_status"], winner["review_reason"],
                        json.dumps(payload, ensure_ascii=False), created_at,
                        winner["updated_at"], deleted_at, scoped_id,
                    ),
                )

            member_ids = [row["id"] for row in members]
            placeholders = ",".join("?" for _ in member_ids)
            history_rows = db.execute(
                f"SELECT id, request_payload_json FROM review_history WHERE finding_id IN ({placeholders})",
                member_ids,
            ).fetchall()
            db.execute(
                f"UPDATE review_history SET finding_id=? WHERE finding_id IN ({placeholders})",
                (scoped_id, *member_ids),
            )
            for history in history_rows:
                if not history["request_payload_json"]:
                    continue
                request_payload = json.loads(history["request_payload_json"])
                if request_payload.get("finding_id") in member_ids:
                    request_payload["finding_id"] = scoped_id
                    db.execute(
                        "UPDATE review_history SET request_payload_json=? WHERE id=?",
                        (
                            json.dumps(
                                request_payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            history["id"],
                        ),
                    )
            obsolete_ids = [finding_id for finding_id in member_ids if finding_id != scoped_id]
            if obsolete_ids:
                obsolete_placeholders = ",".join("?" for _ in obsolete_ids)
                db.execute(
                    f"DELETE FROM findings WHERE id IN ({obsolete_placeholders})",
                    obsolete_ids,
                )

    def ping(self) -> bool:
        """执行轻量查询验证数据库连接可用性。"""

        with self.connect() as db:
            return db.execute("SELECT 1").fetchone()[0] == 1

    def create_run(self, run: dict[str, Any]) -> dict[str, Any]:
        """持久化已确认授权的扫描任务并返回规范化记录。"""

        now = utc_now()
        with self.connect() as db:
            db.execute(
                """INSERT INTO runs
                (id, trace_id, status, stage, apk_filename, apk_sha256, authorized,
                 config_json, manifest_path, pipeline_version, schema_version,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
                (
                    run["id"], run["trace_id"], run["status"], run["stage"],
                    run["apk_filename"], run["apk_sha256"],
                    json.dumps(run["config"], ensure_ascii=False), run["manifest_path"],
                    run.get("pipeline_version", "2.0.0"),
                    run.get("schema_version", "2.0.0"), now, now,
                ),
            )
        return self.get_run(run["id"])

    def update_run(self, run_id: str, **changes: Any) -> dict[str, Any]:
        """仅更新白名单内的运行状态字段并返回最新记录。"""

        allowed = {"status", "stage", "error_code", "error_message"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return self.get_run(run_id)
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in values)
        with self.connect() as db:
            cursor = db.execute(
                f"UPDATE runs SET {assignments} WHERE id=?",
                [*values.values(), run_id],
            )
            if cursor.rowcount == 0:
                raise NotFoundError("run", run_id)
        return self.get_run(run_id)

    def list_runs(self) -> list[dict[str, Any]]:
        """按创建时间倒序列出任务及其发现项数量。"""

        with self.connect() as db:
            rows = db.execute(
                "SELECT runs.*, (SELECT COUNT(*) FROM findings "
                "WHERE findings.run_id = runs.id AND findings.deleted_at IS NULL) AS findings_count "
                "FROM runs ORDER BY created_at DESC"
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        """读取单个任务及其发现项数量，不存在时抛出 ``NotFoundError``。"""

        with self.connect() as db:
            row = db.execute(
                "SELECT runs.*, (SELECT COUNT(*) FROM findings "
                "WHERE findings.run_id = runs.id AND findings.deleted_at IS NULL) AS findings_count "
                "FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("run", run_id)
        return self._run_row(row)

    def delete_run_record(self, run_id: str) -> None:
        """删除任务数据库记录，并由外键级联清理关联数据。"""

        with self.connect() as db:
            db.execute("DELETE FROM runs WHERE id=?", (run_id,))

    def replace_findings(self, run_id: str, findings: list[dict[str, Any]]) -> None:
        """事务 upsert 当前自动结果，保留同 ID 的人工复核状态和历史。

        冲突更新只替换机器可重算字段，不覆盖 review_status/reason、created_at 或历史；同 run
        本轮未出现的 finding 仅软删除，再次出现则恢复。run-scoped ID 防止跨 run 主键冲突，
        ``WHERE findings.run_id=excluded.run_id`` 仍作为最终隔离边界。
        """

        now = utc_now()
        incoming_ids: list[str] = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for finding in findings:
                # Finding 语义哈希在相同 APK 的不同 run 中可能一致；API ID 必须包含 run 作用域，
                # 否则 findings.id 全局主键会阻止同一样本复测。
                finding_id = scope_finding_id(run_id, finding)
                finding.setdefault("pipeline_version", "2.0.0")
                finding.setdefault("schema_version", "2.0.0")
                incoming_ids.append(finding_id)
                db.execute(
                    """INSERT INTO findings
                    (id, run_id, rule_ids_json, title, component, component_name, severity,
                     confidence, evidence_level, review_status, review_reason, payload_json,
                     created_at, updated_at, deleted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(id) DO UPDATE SET
                        rule_ids_json=excluded.rule_ids_json,
                        title=excluded.title,
                        component=excluded.component,
                        component_name=excluded.component_name,
                        severity=excluded.severity,
                        confidence=excluded.confidence,
                        evidence_level=excluded.evidence_level,
                        payload_json=excluded.payload_json,
                        deleted_at=NULL
                    WHERE findings.run_id=excluded.run_id""",
                    (
                        finding_id, run_id, json.dumps(finding["rule_ids"]), finding["title"],
                        finding["component"], finding.get("component_name"), finding["severity"],
                        finding["confidence"], finding["evidence_level"],
                        finding.get("review_status", "pending_ai"), finding.get("review_reason"),
                        json.dumps(finding, ensure_ascii=False), now, now,
                    ),
                )
            if incoming_ids:
                placeholders = ",".join("?" for _ in incoming_ids)
                db.execute(
                    f"UPDATE findings SET deleted_at=? WHERE run_id=? AND deleted_at IS NULL "
                    f"AND id NOT IN ({placeholders})",
                    (now, run_id, *incoming_ids),
                )
            else:
                db.execute(
                    "UPDATE findings SET deleted_at=? WHERE run_id=? AND deleted_at IS NULL",
                    (now, run_id),
                )

    def list_findings(self, run_id: str) -> list[dict[str, Any]]:
        """校验任务存在后返回其全部发现项。"""

        self.get_run(run_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM findings WHERE run_id=? AND deleted_at IS NULL "
                "ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
        return [self._finding_row(row) for row in rows]

    def clear_findings(self, run_id: str) -> None:
        """清除任务发现项及其级联复核历史。"""

        with self.connect() as db:
            db.execute("DELETE FROM findings WHERE run_id=?", (run_id,))

    def get_finding(self, finding_id: str) -> dict[str, Any]:
        """读取未移除的发现项，不存在时抛出 ``NotFoundError``。"""

        with self.connect() as db:
            row = self._find_finding_row(db, finding_id)
        if row is None:
            raise NotFoundError("finding", finding_id)
        return self._finding_row(row)

    def review_finding(
        self,
        finding_id: str,
        status: str,
        reason: str | None,
        *,
        actor: str = "human",
        request_id: str | None = None,
        basis: str | None = None,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        """在单个立即写事务中实现复核幂等、乐观并发和审计记录。

        相同 request_id 与相同规范请求负载返回当前 finding 而不重复写历史；负载不同时冲突。
        expected_status 在插入审计前比较，确保状态更新与 history 同生共回滚。
        """

        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._find_finding_row(db, finding_id)
            if row is None:
                raise NotFoundError("finding", finding_id)
            canonical_id = row["id"]
            request_payload = json.dumps(
                {
                    "finding_id": canonical_id,
                    "status": status,
                    "reason": reason,
                    "actor": actor,
                    "basis": basis,
                    "expected_status": expected_status,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if request_id:
                existing = db.execute(
                    "SELECT * FROM review_history WHERE request_id=?", (request_id,)
                ).fetchone()
                if existing is not None:
                    if existing["request_payload_json"] != request_payload:
                        raise ConflictError(
                            "request_id 已用于不同的复核请求",
                            "REVIEW_REQUEST_ID_CONFLICT",
                        )
                    return self._finding_row(row)
            previous_status = row["review_status"]
            if expected_status is not None and previous_status != expected_status:
                raise ConflictError(
                    f"finding 当前状态为 {previous_status}，与 expected_status 不匹配",
                    "REVIEW_STATUS_CONFLICT",
                )
            db.execute(
                """INSERT INTO review_history
                (finding_id, old_status, new_status, reason, actor, request_id, basis,
                 expected_status, previous_status, request_payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    canonical_id, previous_status, status, reason, actor, request_id, basis,
                    expected_status, previous_status, request_payload, now,
                ),
            )
            db.execute(
                "UPDATE findings SET review_status=?, review_reason=?, updated_at=? WHERE id=?",
                (status, reason, now, canonical_id),
            )
            updated_row = db.execute(
                "SELECT * FROM findings WHERE id=?", (canonical_id,)
            ).fetchone()
        return self._finding_row(updated_row)

    @staticmethod
    def _find_finding_row(
        db: sqlite3.Connection, finding_id: str
    ) -> sqlite3.Row | None:
        row = db.execute(
            "SELECT * FROM findings WHERE id=? AND deleted_at IS NULL", (finding_id,)
        ).fetchone()
        if row is not None:
            return row
        legacy_rows = db.execute(
            "SELECT * FROM findings WHERE deleted_at IS NULL "
            "AND json_extract(payload_json, '$.base_id')=? LIMIT 2",
            (finding_id,),
        ).fetchall()
        # 仅在语义 ID 全局唯一时兼容旧链接；多个 run 同 ID 时必须使用 scoped ID。
        return legacy_rows[0] if len(legacy_rows) == 1 else None

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["authorized"] = bool(result["authorized"])
        result["config"] = json.loads(result.pop("config_json"))
        result["filename"] = result["apk_filename"]
        result["file_name"] = result["apk_filename"]
        result["source_analysis_enabled"] = result["config"].get("source_analysis", {}).get("enabled", True)
        result["error"] = result.get("error_message")
        return result

    @staticmethod
    def _finding_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        payload = json.loads(result.pop("payload_json"))
        payload.setdefault("pipeline_version", "1.0.0")
        payload.setdefault("schema_version", "1.0.0")
        payload.update({
            "id": result["id"],
            "review_status": result["review_status"],
            "review_reason": result["review_reason"],
            "updated_at": result["updated_at"],
            "run_id": result["run_id"],
        })
        payload.setdefault("base_id", result["id"].removeprefix(f"{result['run_id']}_"))
        payload["status_layers"] = {
            "funnel": payload.get("funnel_disposition"),
            "analysis": payload.get("analysis_status", "rule_only"),
            "evidence": payload.get("evidence_decision", "unresolved"),
            "review": result["review_status"],
        }
        return payload
