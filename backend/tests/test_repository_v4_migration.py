"""schema_migrations v4（assets/batches + runs 关联列）迁移测试（T1.1）。

设计稿：docs/analysis/2026-08-22-t0-8-implementation-plan.md；
实施方案：docs/analysis/2026-08-22-t1-1-implementation-plan.md（含评审 R-1~R-6 修订）：
- 迁移逐条 execute（保持挂起事务原子性，评审 R-1）；
- FK 行为测试须在 PRAGMA foreign_keys=ON 连接上执行（评审 R-5）；
- 叠加路径构造：initialize 后回退 v4 记录 + DROP 两表（评审 R-2 定稿）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.shared.repository import DATABASE_SCHEMA_VERSION, SQLiteRepository

NOW = "2026-08-22T00:00:00+00:00"

V4_RUN_COLUMNS = {"asset_id", "batch_id", "ai_skipped_by_batch_budget"}


def _create_v1_database(path: Path) -> None:
    """构造 v1 形状库（无 schema_migrations 表，含 runs/findings 样例数据）。"""
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
        """
    )
    db.execute(
        "INSERT INTO runs VALUES ('run_one','trace','completed','completed','sample.apk',?,1,'{}','/tmp/m.json',NULL,NULL,?,?)",
        ("a" * 64, NOW, NOW),
    )
    db.execute(
        "INSERT INTO findings VALUES ('finding_same','run_one','[\"TEST_RULE\"]','t','activity',"
        "'com.example.Demo','high','medium','L2','confirmed',NULL,?,?,?)",
        ('{"id":"finding_same"}', NOW, NOW),
    )
    db.commit()
    db.close()


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _table_columns(db: sqlite3.Connection, name: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({name})").fetchall()}


def test_v4_fresh_database_creates_assets_batches(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()

    with sqlite3.connect(path) as db:
        versions = [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == [1, 2, 3, 4]
        assert db.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SCHEMA_VERSION == 4
        assert _table_exists(db, "assets") and _table_exists(db, "batches")
        assert V4_RUN_COLUMNS <= _table_columns(db, "runs")
        # assets 表核心约束在位（apk_sha256 唯一）
        asset_columns = _table_columns(db, "assets")
        assert {"id", "package_name", "apk_sha256", "status", "last_run_id"} <= asset_columns
        batch_columns = _table_columns(db, "batches")
        assert {"id", "status", "max_ai_calls", "ai_skipped_count"} <= batch_columns


def test_v4_upgrade_from_v1_legacy_preserves_data(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v1.sqlite3"
    _create_v1_database(path)
    repository = SQLiteRepository(path)
    repository.initialize()

    with sqlite3.connect(path) as db:
        versions = [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == [1, 2, 3, 4]
        assert V4_RUN_COLUMNS <= _table_columns(db, "runs")
        assert _table_exists(db, "assets") and _table_exists(db, "batches")
        # 既有数据完好：v1 base id 经 v3 迁移为 run-scoped id（run_one_finding_same）
        run_row = db.execute("SELECT id, apk_sha256 FROM runs").fetchone()
        assert run_row == ("run_one", "a" * 64)
        finding_row = db.execute("SELECT id, run_id, title FROM findings").fetchone()
        assert finding_row == ("run_one_finding_same", "run_one", "t")
        # 旧行新列默认值（评审 N-3）
        defaults = db.execute(
            "SELECT asset_id, batch_id, ai_skipped_by_batch_budget FROM runs WHERE id='run_one'"
        ).fetchone()
        assert defaults == (None, None, 0)


def test_v4_upgrade_from_v3_with_migration_records(tmp_path: Path) -> None:
    """叠加路径（评审 R-2 定稿构造）：模拟"v3 库含 v1/v2/v3 迁移记录"升级 v4。"""
    path = tmp_path / "v3-with-records.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    with sqlite3.connect(path) as db:
        # 回退到 v3 形状：删 v4 记录 + 回退版本 + 删两新表（runs 三新列保留）
        db.execute("DELETE FROM schema_migrations WHERE version=4")
        db.execute("PRAGMA user_version=3")
        db.execute("DROP TABLE assets")
        db.execute("DROP TABLE batches")
        db.commit()

    repository.initialize()
    with sqlite3.connect(path) as db:
        versions = [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == [1, 2, 3, 4]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert _table_exists(db, "assets") and _table_exists(db, "batches")
        assert V4_RUN_COLUMNS <= _table_columns(db, "runs")


def test_v4_interrupted_migration_reruns_idempotently(tmp_path: Path) -> None:
    """中断恢复：v4 已执行但未记录（崩溃窗口）→ 重跑自愈。"""
    path = tmp_path / "interrupted.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM schema_migrations WHERE version=4")
        db.commit()

    repository.initialize()  # 不报错、幂等补记录

    with sqlite3.connect(path) as db:
        versions = [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == [1, 2, 3, 4]
        assert _table_exists(db, "assets") and _table_exists(db, "batches")
        assert V4_RUN_COLUMNS <= _table_columns(db, "runs")


def test_v4_foreign_key_set_null_on_asset_delete(tmp_path: Path) -> None:
    """ON DELETE SET NULL 生效（评审 R-5：须在 FK=ON 连接上断言）。"""
    path = tmp_path / "fk.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()

    with repository.connect() as db:
        db.execute(
            "INSERT INTO assets (id, package_name, apk_filename, apk_sha256, created_at, updated_at) "
            "VALUES ('asset_one','com.example','a.apk',?, ?, ?)",
            ("b" * 64, NOW, NOW),
        )
        db.execute(
            "INSERT INTO batches (id, created_at, updated_at) VALUES ('batch_one', ?, ?)",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO runs (id, trace_id, status, stage, apk_filename, apk_sha256, authorized, "
            "config_json, manifest_path, created_at, updated_at, asset_id, batch_id) "
            "VALUES ('run_fk','t','completed','completed','a.apk',?,1,'{}','/tmp/m.json',?,?, 'asset_one','batch_one')",
            ("b" * 64, NOW, NOW),
        )
        db.execute("DELETE FROM assets WHERE id='asset_one'")
        db.execute("DELETE FROM batches WHERE id='batch_one'")

    with sqlite3.connect(path) as db:
        row = db.execute("SELECT asset_id, batch_id FROM runs WHERE id='run_fk'").fetchone()
        assert row == (None, None)


def test_v4_duplicate_asset_sha256_rejected(tmp_path: Path) -> None:
    """apk_sha256 UNIQUE 防重复注册（T0.8 设计稿 §3.2）。"""
    import pytest

    path = tmp_path / "unique.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    with repository.connect() as db:
        insert_sql = (
            "INSERT INTO assets (id, package_name, apk_filename, apk_sha256, created_at, updated_at) "
            "VALUES (?, 'com.example', 'a.apk', ?, ?, ?)"
        )
        db.execute(insert_sql, ("asset_one", "c" * 64, NOW, NOW))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert_sql, ("asset_two", "c" * 64, NOW, NOW))


def test_v4_half_migration_columns_missing_backfilled(tmp_path: Path) -> None:
    """半迁移（表已建、runs 列未加、无 v4 记录）→ initialize 幂等补列（N-1）。"""
    path = tmp_path / "half.sqlite3"
    _create_v1_database(path)
    db = sqlite3.connect(path)
    # 模拟 v4 在建表后、加列前中断：手工建两表但不加 runs 列、不记录 v4
    db.execute(
        "CREATE TABLE assets (id TEXT PRIMARY KEY, package_name TEXT NOT NULL, apk_filename TEXT NOT NULL,"
        " apk_sha256 TEXT NOT NULL UNIQUE, source TEXT NOT NULL DEFAULT 'local_upload',"
        " status TEXT NOT NULL DEFAULT 'ready', last_run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE batches (id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',"
        " max_ai_calls INTEGER, max_wall_seconds INTEGER, ai_skipped_count INTEGER NOT NULL DEFAULT 0,"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT)"
    )
    db.commit()
    db.close()

    repository = SQLiteRepository(path)
    repository.initialize()

    with sqlite3.connect(path) as db:
        assert V4_RUN_COLUMNS <= _table_columns(db, "runs")
        versions = [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == [1, 2, 3, 4]


def test_v4_get_run_returns_new_columns(tmp_path: Path) -> None:
    """get_run 返回三新键（固化 T0.8 R-5 的 _run_row 兼容声明；T1.1 评审灰色点）。"""
    path = tmp_path / "public.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.create_run(
        {
            "id": "run_pub",
            "trace_id": "t",
            "status": "completed",
            "stage": "completed",
            "apk_filename": "a.apk",
            "apk_sha256": "d" * 64,
            "config": {},
            "manifest_path": "/tmp/m.json",
        }
    )
    run = repository.get_run("run_pub")
    assert run["asset_id"] is None
    assert run["batch_id"] is None
    assert run["ai_skipped_by_batch_budget"] == 0


def test_v4_large_runs_table_migrates_correctly(tmp_path: Path) -> None:
    """大表迁移：1,000 行 runs 数据完好 + 新列默认值（ADD COLUMN 为 O(1) 元数据变更）。"""
    path = tmp_path / "large.sqlite3"
    _create_v1_database(path)
    rows = [
        (
            f"run_bulk_{index}", f"trace-{index}", "completed", "completed", "sample.apk",
            f"{index:064d}", 1, "{}", "/tmp/m.json", None, None, NOW, NOW,
        )
        for index in range(1000)
    ]
    db = sqlite3.connect(path)
    db.executemany(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    db.commit()
    db.close()

    repository = SQLiteRepository(path)
    repository.initialize()

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1001
        bad_defaults = db.execute(
            "SELECT COUNT(*) FROM runs WHERE asset_id IS NOT NULL OR batch_id IS NOT NULL "
            "OR ai_skipped_by_batch_budget != 0"
        ).fetchone()[0]
        assert bad_defaults == 0
        assert db.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 1
