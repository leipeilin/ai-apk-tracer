from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.shared.errors import ConflictError, NotFoundError
from app.shared.repository import DATABASE_SCHEMA_VERSION, SQLiteRepository


def _run(run_id: str = "run_one") -> dict:
    return {
        "id": run_id,
        "trace_id": f"trace-{run_id}",
        "status": "completed",
        "stage": "completed",
        "apk_filename": "sample.apk",
        "apk_sha256": "a" * 64,
        "config": {"source_analysis": {"enabled": True}},
        "manifest_path": f"/tmp/{run_id}/manifest.json",
    }


def _finding(title: str = "original") -> dict:
    return {
        "id": "finding_same",
        "rule_ids": ["TEST_RULE"],
        "title": title,
        "component": "activity",
        "component_name": "com.example.Demo",
        "severity": "pending",
        "confidence": "medium",
        "evidence_level": "L2",
        "analysis_status": "ai_completed",
        "evidence_decision": "supported",
    }


def _create_legacy_database(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, status TEXT NOT NULL,
            stage TEXT NOT NULL, apk_filename TEXT NOT NULL, apk_sha256 TEXT NOT NULL,
            authorized INTEGER NOT NULL, config_json TEXT NOT NULL, manifest_path TEXT NOT NULL,
            error_code TEXT, error_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE findings (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            rule_ids_json TEXT NOT NULL, title TEXT NOT NULL, component TEXT NOT NULL,
            component_name TEXT, severity TEXT NOT NULL, confidence TEXT NOT NULL,
            evidence_level TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'pending_ai',
            review_reason TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE review_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
            old_status TEXT NOT NULL, new_status TEXT NOT NULL, reason TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    # 真实 v1 数据使用未加 run scope 的 base ID，payload 也没有 base_id。
    payload = _finding()
    db.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, NULL, ?, ?)",
        (
            "run_one", "trace", "completed", "completed", "sample.apk", "a" * 64,
            "{}", "/tmp/manifest.json", "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    db.execute(
        "INSERT INTO findings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            payload["id"], "run_one", '["TEST_RULE"]', payload["title"], "activity",
            "com.example.Demo", "high", "medium", "L2", "confirmed", "legacy review",
            json.dumps(payload), "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
        ),
    )
    db.execute(
        "INSERT INTO review_history(finding_id, old_status, new_status, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (payload["id"], "pending_manual", "confirmed", "legacy review", "2026-01-02T00:00:00+00:00"),
    )
    db.commit()
    db.close()


def test_legacy_database_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _create_legacy_database(path)
    repository = SQLiteRepository(path)

    repository.initialize()
    repository.initialize()

    with sqlite3.connect(path) as db:
        versions = db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        assert versions == [(1,), (2,), (3,), (4,), (5,)]
        assert db.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SCHEMA_VERSION
        run_columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
        finding_columns = {row[1] for row in db.execute("PRAGMA table_info(findings)")}
        history_columns = {row[1] for row in db.execute("PRAGMA table_info(review_history)")}
        assert {"pipeline_version", "schema_version"} <= run_columns
        assert "deleted_at" in finding_columns
        assert {
            "actor", "request_id", "basis", "expected_status", "previous_status",
            "request_payload_json",
        } <= history_columns
        migrated = db.execute(
            "SELECT finding_id, actor, previous_status, new_status FROM review_history"
        ).fetchone()
        assert migrated == (
            "run_one_finding_same", "legacy", "pending_manual", "confirmed"
        )
        finding_row = db.execute(
            "SELECT id, review_status, review_reason, created_at, updated_at, payload_json "
            "FROM findings"
        ).fetchone()
        assert finding_row[:5] == (
            "run_one_finding_same", "confirmed", "legacy review",
            "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
        )
        migrated_payload = json.loads(finding_row[5])
        assert migrated_payload["id"] == "run_one_finding_same"
        assert migrated_payload["base_id"] == "finding_same"

    old_run = repository.get_run("run_one")
    assert old_run["pipeline_version"] == "1.0.0"
    assert old_run["schema_version"] == "1.0.0"


def test_v3_migration_deterministically_merges_base_and_scoped_rows(tmp_path: Path) -> None:
    path = tmp_path / "v2-conflict.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.create_run(_run())
    scoped = _finding("new automatic title")
    repository.replace_findings("run_one", [scoped])

    legacy = _finding("old reviewed title")
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM schema_migrations WHERE version=3")
        db.execute("PRAGMA user_version=2")
        db.execute(
            """INSERT INTO findings
            (id, run_id, rule_ids_json, title, component, component_name, severity,
             confidence, evidence_level, review_status, review_reason, payload_json,
             created_at, updated_at, deleted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "finding_same", "run_one", '["TEST_RULE"]', legacy["title"], "activity",
                "com.example.Demo", "high", "medium", "L2", "manual_false_positive",
                "verified safe", json.dumps(legacy), "2025-12-31T00:00:00+00:00",
                "2026-01-03T00:00:00+00:00", "2026-01-04T00:00:00+00:00",
            ),
        )
        db.execute(
            """INSERT INTO review_history
            (finding_id, old_status, new_status, reason, actor, previous_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "finding_same", "pending_manual", "manual_false_positive", "verified safe",
                "human", "pending_manual", "2026-01-03T00:00:00+00:00",
            ),
        )
        db.commit()

    repository.initialize()
    repository.initialize()

    migrated = repository.get_finding("run_one_finding_same")
    assert migrated["title"] == "new automatic title"
    assert migrated["review_status"] == "manual_false_positive"
    assert migrated["review_reason"] == "verified safe"
    assert migrated["updated_at"] == "2026-01-03T00:00:00+00:00"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 1
        assert db.execute(
            "SELECT finding_id, new_status, reason FROM review_history"
        ).fetchone() == (
            "run_one_finding_same", "manual_false_positive", "verified safe"
        )


def test_replace_findings_preserves_review_and_removed_history(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _create_legacy_database(path)
    repository = SQLiteRepository(path)
    repository.initialize()
    replacement = _finding("updated automatic title")

    repository.replace_findings("run_one", [replacement])

    current = repository.get_finding(replacement["id"])
    assert current["title"] == "updated automatic title"
    assert current["review_status"] == "confirmed"
    assert current["review_reason"] == "legacy review"
    assert current["updated_at"] == "2026-01-02T00:00:00+00:00"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM review_history").fetchone()[0] == 1

    repository.replace_findings("run_one", [])

    assert repository.list_findings("run_one") == []
    with pytest.raises(NotFoundError):
        repository.get_finding(replacement["id"])
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM review_history").fetchone()[0] == 1
        assert db.execute(
            "SELECT deleted_at IS NOT NULL FROM findings WHERE id=?", (replacement["id"],)
        ).fetchone()[0] == 1


def test_review_request_is_idempotent_and_optimistically_locked(tmp_path: Path) -> None:
    path = tmp_path / "review.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.create_run(_run())
    finding = _finding()
    repository.replace_findings("run_one", [finding])
    request = {
        "actor": "analyst",
        "request_id": "review-request-1",
        "basis": "manual evidence check",
        "expected_status": "pending_ai",
    }

    first = repository.review_finding(
        finding["id"], "confirmed", "verified", **request
    )
    repeated = repository.review_finding(
        finding["id"], "confirmed", "verified", **request
    )

    assert first["review_status"] == repeated["review_status"] == "confirmed"
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT actor, request_id, basis, expected_status, previous_status, new_status "
            "FROM review_history"
        ).fetchone()
        assert row == (
            "analyst", "review-request-1", "manual evidence check", "pending_ai",
            "pending_ai", "confirmed",
        )
        assert db.execute("SELECT COUNT(*) FROM review_history").fetchone()[0] == 1

    with pytest.raises(ConflictError) as reused:
        repository.review_finding(
            finding["id"], "manual_false_positive", "different", **request
        )
    assert reused.value.code == "REVIEW_REQUEST_ID_CONFLICT"

    with pytest.raises(ConflictError) as stale:
        repository.review_finding(
            finding["id"],
            "manual_false_positive",
            "stale client",
            request_id="review-request-2",
            expected_status="pending_ai",
        )
    assert stale.value.code == "REVIEW_STATUS_CONFLICT"
