"""批量编排测试（T1.3）。

设计：docs/analysis/explorer-track/2026-08-22-t1-3-implementation-plan.md
（含评审 R-1~R-10 修订）。FakeOrchestrator 与改后真实行为同构：
成功路径 = update_run(completed) + manifest stages 追加含 requests_used
的 ai_analysis 条目；失败路径覆盖"异常收敛 failed"与"直接抛异常"两种。
"""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app.assets.batch import BatchOrchestrator
from app.assets.registry import AssetRegistry
from app.config import BatchSettings, Settings, SourceAnalysisSettings, StorageSettings
from app.runs.storage import RunStorage
from app.shared.errors import ConflictError, NotFoundError, ValidationError
from app.shared.repository import SQLiteRepository

NOW = "2026-08-22T00:00:00+00:00"


def _apk_bytes(seed: str) -> bytes:
    """按 seed 构造互异的最小合法 APK ZIP（sha256 不同，注册不冲突）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("AndroidManifest.xml", f"<manifest package='{seed}'/>".encode())
        zf.writestr("classes.dex", b"dex\n035\x00" + seed.encode())
    return buffer.getvalue()


def _make_stack(
    tmp_path: Path,
    batch_settings: BatchSettings | None = None,
) -> tuple[Settings, SQLiteRepository, RunStorage, AssetRegistry]:
    settings = Settings(
        database_path=tmp_path / "tracer.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "data"),
        source_analysis=SourceAnalysisSettings(enabled=False),
        batch=batch_settings or BatchSettings(),
    )
    repository = SQLiteRepository(settings.database_path)
    repository.initialize()
    storage = RunStorage(settings.resolved_data_root(), settings.storage)
    registry = AssetRegistry(repository, storage, tmp_path / "data" / "assets")
    return settings, repository, storage, registry


def _register_assets(registry: AssetRegistry, count: int) -> list[dict]:
    return [
        registry.register(io.BytesIO(_apk_bytes(f"pkg-{index}")), f"demo-{index}.apk", f"com.example.p{index}")
        for index in range(count)
    ]


class FakeOrchestrator:
    """可控行为的编排替身（与真实 scan 的可观察行为同构）。"""

    def __init__(
        self,
        storage: RunStorage,
        repository: SQLiteRepository,
        *,
        behavior: str = "ok",
        requests_used: int = 1,
        delay: float = 0.0,
        tracker: dict[str, int] | None = None,
    ) -> None:
        self._storage = storage
        self._repository = repository
        self._behavior = behavior
        self._requests_used = requests_used
        self._delay = delay
        self._tracker = tracker

    async def scan(self, run_id: str) -> None:
        if self._tracker is not None:
            self._tracker["active"] += 1
            self._tracker["peak"] = max(self._tracker["peak"], self._tracker["active"])
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._behavior == "raise":
                raise RuntimeError("fake scan failure")
            if self._behavior == "converge_fail":
                # 同构真实 scan：未处理异常收敛为 failed 状态而非向上抛
                self._repository.update_run(run_id, status="failed", stage="failed", error_code="FAKE")
                return
            self._repository.update_run(run_id, status="completed", stage="completed")
            manifest = self._storage.read_manifest(run_id)
            manifest.setdefault("stages", []).append(
                {
                    "name": "ai_analysis",
                    "status": "completed",
                    "summary": {"requests_used": self._requests_used},
                }
            )
            self._storage.write_manifest(run_id, manifest)
        finally:
            if self._tracker is not None:
                self._tracker["active"] -= 1


def _factory_with_behaviors(
    storage: RunStorage,
    repository: SQLiteRepository,
    behaviors: list[str],
    *,
    requests_used: int = 1,
    delay: float = 0.0,
    tracker: dict[str, int] | None = None,
):
    """按调用顺序分配行为的工厂（串行编排下顺序确定）。"""
    calls = itertools.count()

    def factory() -> FakeOrchestrator:
        index = next(calls) % len(behaviors)
        return FakeOrchestrator(
            storage,
            repository,
            behavior=behaviors[index],
            requests_used=requests_used,
            delay=delay,
            tracker=tracker,
        )

    return factory


def _make_orchestrator(
    settings: Settings,
    repository: SQLiteRepository,
    storage: RunStorage,
    registry: AssetRegistry,
    factory,
) -> BatchOrchestrator:
    return BatchOrchestrator(settings, repository, storage, None, registry, orchestrator_factory=factory)


async def _run(batch_orchestrator: BatchOrchestrator, batch_id: str) -> None:
    await batch_orchestrator.run_batch(batch_id)


# ----------------------------------------------------------------------
# 创建（A-1 / N-1）
# ----------------------------------------------------------------------


def test_create_batch_persists_snapshot(tmp_path: Path) -> None:
    settings, repository, storage, registry = _make_stack(
        tmp_path, BatchSettings(max_concurrent_runs=1, max_ai_calls=7, max_wall_seconds=99)
    )
    assets = _register_assets(registry, 2)
    factory = _factory_with_behaviors(storage, repository, ["ok"])
    orchestrator = _make_orchestrator(settings, repository, storage, registry, factory)

    batch = orchestrator.create_batch([assets[0]["id"], assets[1]["id"]])

    assert batch["status"] == "pending"
    assert batch["max_ai_calls"] == 7
    assert batch["max_wall_seconds"] == 99
    assert batch["ai_skipped_count"] == 0
    assert batch["assets"] == [
        {"asset_id": assets[0]["id"], "package_name": "com.example.p0", "apk_sha256": assets[0]["apk_sha256"]},
        {"asset_id": assets[1]["id"], "package_name": "com.example.p1", "apk_sha256": assets[1]["apk_sha256"]},
    ]

    # 资产不存在 → NotFoundError
    with pytest.raises(NotFoundError):
        orchestrator.create_batch(["missing_asset"])
    # 空列表 → ValidationError（N-1；重复 id 去重后单元素非空 → 走资产校验 NotFoundError）
    with pytest.raises(ValidationError):
        orchestrator.create_batch([])
    with pytest.raises(NotFoundError):
        orchestrator.create_batch(["a", "a"])


def test_create_batch_dedupes_preserving_order(tmp_path: Path) -> None:
    settings, repository, storage, registry = _make_stack(tmp_path)
    assets = _register_assets(registry, 2)
    factory = _factory_with_behaviors(storage, repository, ["ok"])
    orchestrator = _make_orchestrator(settings, repository, storage, registry, factory)

    batch = orchestrator.create_batch([assets[1]["id"], assets[0]["id"], assets[1]["id"]])

    assert [item["asset_id"] for item in batch["assets"]] == [assets[1]["id"], assets[0]["id"]]


# ----------------------------------------------------------------------
# 执行：全流程 / 部分失败 / 预算 / 墙钟 / 防重 / 缺失 / 并发（A-2~A-8）
# ----------------------------------------------------------------------


def test_run_batch_full_flow(tmp_path: Path) -> None:
    settings, repository, storage, registry = _make_stack(tmp_path)
    assets = _register_assets(registry, 3)
    factory = _factory_with_behaviors(storage, repository, ["ok"])
    orchestrator = _make_orchestrator(settings, repository, storage, registry, factory)

    batch = orchestrator.create_batch([asset["id"] for asset in assets])
    asyncio.run(_run(orchestrator, batch["id"]))

    final = orchestrator.get_batch(batch["id"])
    assert final["status"] == "completed"
    assert (final["total_runs"], final["completed_runs"], final["failed_runs"]) == (3, 3, 0)
    assert final["ai_skipped"] == 0 and final["ai_skipped_by_budget"] == 0
    assert final["completed_at"] is not None
    # runs 关联列 + 资产状态 + last_run_id（A-2）
    with repository.connect() as db:
        rows = db.execute(
            "SELECT asset_id, batch_id, ai_skipped_by_batch_budget FROM runs WHERE batch_id=? ORDER BY asset_id",
            (batch["id"],),
        ).fetchall()
    assert len(rows) == 3
    assert all(row[1] == batch["id"] and row[2] == 0 for row in rows)
    assert {row[0] for row in rows} == {asset["id"] for asset in assets}
    for asset in assets:
        latest = registry.get(asset["id"])
        assert latest["status"] == "ready"
        assert latest["last_run_id"] is not None


def test_run_batch_partial_failure(tmp_path: Path) -> None:
    settings, repository, storage, registry = _make_stack(
        tmp_path, BatchSettings(max_concurrent_runs=1)
    )
    assets = _register_assets(registry, 3)
    # 串行顺序：资产2 收敛 failed、资产3 直接抛异常（协程兜底，评审 R-6：不取消其余）
    factory = _factory_with_behaviors(storage, repository, ["ok", "converge_fail", "raise"])
    orchestrator = _make_orchestrator(settings, repository, storage, registry, factory)

    batch = orchestrator.create_batch([asset["id"] for asset in assets])
    asyncio.run(_run(orchestrator, batch["id"]))

    final = orchestrator.get_batch(batch["id"])
    assert final["status"] == "partial"
    assert (final["completed_runs"], final["failed_runs"]) == (1, 2)
    statuses = {registry.get(asset["id"])["status"] for asset in assets}
    assert statuses == {"ready", "error"}


def test_run_batch_all_failed_marks_failed(tmp_path: Path) -> None:
    settings, repository, storage, registry = _make_stack(tmp_path)
    assets = _register_assets(registry, 2)
    factory = _factory_with_behaviors(storage, repository, ["converge_fail"])
    orchestrator = _make_orchestrator(settings, repository, storage, registry, factory)

    batch = orchestrator.create_batch([asset["id"] for asset in assets])
    asyncio.run(_run(orchestrator, batch["id"]))

    assert orchestrator.get_batch(batch["id"])["status"] == "failed"


def test_run_batch_budget_degradation(tmp_path: Path) -> None:
    # 串行保证判定顺序（预算测试确定性前提）
    settings, repository, storage, registry = _make_stack(
        tmp_path, BatchSettings(max_concurrent_runs=1, max_ai_calls=2)
    )
    assets = _register_assets(registry, 3)
    factory = _factory_with_behaviors(storage, repository, ["ok"], requests_used=1)
    orchestrator = _make_orchestrator(settings, repository, storage, registry, factory)

    batch = orchestrator.create_batch([asset["id"] for asset in assets])
    asyncio.run(_run(orchestrator, batch["id"]))

    final = orchestrator.get_batch(batch["id"])
    assert final["status"] == "completed"  # 降级 run 仍走确定性主链并完成
    assert final["ai_skipped"] == 1
    assert final["ai_skipped_by_budget"] == 1 and final["ai_skipped_by_wall_clock"] == 0
    # 第三个 run 降级：标记列 + config（A-4）
    with repository.connect() as db:
        rows = db.execute(
            """SELECT ai_skipped_by_batch_budget, config_json FROM runs
            WHERE batch_id=? ORDER BY created_at""",
            (batch["id"],),
        ).fetchall()
    assert [row[0] for row in rows] == [0, 0, 1]
    degraded_config = json.loads(rows[2][1])
    assert degraded_config["ai"]["enabled"] is False
    assert degraded_config["ai"]["skip_reason"] == "batch_budget"
    assert degraded_config["ai"]["model"] == settings.ai.model  # 其余 AI 元数据保留


def test_run_batch_budget_degradation_cap_one(tmp_path: Path) -> None:
    """max_ai_calls=1 字面场景（T1.6 评审 R-4）：预算耗尽后后续 run 连续降级、批次继续。"""
    settings, repository, storage, registry = _make_stack(
        tmp_path, BatchSettings(max_concurrent_runs=1, max_ai_calls=1)
    )
    assets = _register_assets(registry, 3)
    factory = _factory_with_behaviors(storage, repository, ["ok"], requests_used=1)
    orchestrator = _make_orchestrator(settings, repository, storage, registry, factory)

    batch = orchestrator.create_batch([asset["id"] for asset in assets])
    asyncio.run(_run(orchestrator, batch["id"]))

    final = orchestrator.get_batch(batch["id"])
    assert final["status"] == "completed"
    assert final["ai_skipped"] == 2  # run2/3 连续降级
    assert final["ai_skipped_by_budget"] == 2
    with repository.connect() as db:
        rows = db.execute(
            "SELECT ai_skipped_by_batch_budget FROM runs WHERE batch_id=? ORDER BY created_at",
            (batch["id"],),
        ).fetchall()
    assert [row[0] for row in rows] == [0, 1, 1]


def test_run_batch_wall_clock_degradation(tmp_path: Path) -> None:
    settings, repository, storage, registry = _make_stack(
        tmp_path, BatchSettings(max_concurrent_runs=1, max_wall_seconds=1)
    )
    assets = _register_assets(registry, 2)
    # 第一个 run 耗时 1.2s 越过墙钟 → 第二个 run 启动前降级（D3）
    factory = _factory_with_behaviors(storage, repository, ["ok"], requests_used=1, delay=1.2)
    orchestrator = _make_orchestrator(settings, repository, storage, registry, factory)

    batch = orchestrator.create_batch([asset["id"] for asset in assets])
    asyncio.run(_run(orchestrator, batch["id"]))

    final = orchestrator.get_batch(batch["id"])
    assert final["ai_skipped"] == 1
    assert final["ai_skipped_by_wall_clock"] == 1 and final["ai_skipped_by_budget"] == 0
    with repository.connect() as db:
        rows = db.execute(
            "SELECT ai_skipped_by_batch_budget, config_json FROM runs WHERE batch_id=? ORDER BY created_at",
            (batch["id"],),
        ).fetchall()
    assert rows[1][0] == 1
    assert json.loads(rows[1][1])["ai"]["skip_reason"] == "batch_wall_clock"


def test_run_batch_rejects_non_pending(tmp_path: Path) -> None:
    settings, repository, storage, registry = _make_stack(tmp_path)
    assets = _register_assets(registry, 1)
    factory = _factory_with_behaviors(storage, repository, ["ok"])
    orchestrator = _make_orchestrator(settings, repository, storage, registry, factory)

    batch = orchestrator.create_batch([assets[0]["id"]])
    asyncio.run(_run(orchestrator, batch["id"]))

    with pytest.raises(ConflictError) as error:
        asyncio.run(_run(orchestrator, batch["id"]))
    assert error.value.code == "BATCH_NOT_PENDING"

    with pytest.raises(NotFoundError):
        asyncio.run(_run(orchestrator, "missing_batch"))


def test_run_batch_missing_asset_skipped(tmp_path: Path) -> None:
    settings, repository, storage, registry = _make_stack(tmp_path)
    assets = _register_assets(registry, 2)
    factory = _factory_with_behaviors(storage, repository, ["ok"])
    orchestrator = _make_orchestrator(settings, repository, storage, registry, factory)

    batch = orchestrator.create_batch([assets[0]["id"], assets[1]["id"]])
    registry.delete(assets[0]["id"])  # 执行前删除（N-3：缺失跳过不建 run）
    asyncio.run(_run(orchestrator, batch["id"]))

    final = orchestrator.get_batch(batch["id"])
    assert final["status"] == "completed"
    assert final["total_runs"] == 1  # 仅存活资产建 run


def test_run_batch_concurrency_limit(tmp_path: Path) -> None:
    settings, repository, storage, registry = _make_stack(
        tmp_path, BatchSettings(max_concurrent_runs=1)
    )
    assets = _register_assets(registry, 3)
    tracker: dict[str, int] = {"active": 0, "peak": 0}
    factory = _factory_with_behaviors(
        storage, repository, ["ok"], requests_used=0, delay=0.02, tracker=tracker
    )
    orchestrator = _make_orchestrator(settings, repository, storage, registry, factory)

    batch = orchestrator.create_batch([asset["id"] for asset in assets])
    asyncio.run(_run(orchestrator, batch["id"]))

    assert tracker["peak"] == 1  # 信号量实证：扫描并发峰值 = max_concurrent_runs
    assert orchestrator.get_batch(batch["id"])["status"] == "completed"


def test_get_batch_summary_from_runs(tmp_path: Path) -> None:
    """汇总走 runs 聚合（D7）：篡改 runs 后 get_batch 重算正确（非内存态）。"""
    settings, repository, storage, registry = _make_stack(tmp_path)
    assets = _register_assets(registry, 2)
    factory = _factory_with_behaviors(storage, repository, ["ok"])
    orchestrator = _make_orchestrator(settings, repository, storage, registry, factory)

    batch = orchestrator.create_batch([asset["id"] for asset in assets])
    asyncio.run(_run(orchestrator, batch["id"]))
    assert orchestrator.get_batch(batch["id"])["completed_runs"] == 2

    with repository.connect() as db:
        db.execute("UPDATE runs SET status='failed' WHERE batch_id=?", (batch["id"],))

    tampered = orchestrator.get_batch(batch["id"])
    assert (tampered["completed_runs"], tampered["failed_runs"]) == (0, 2)


def test_batch_real_pipeline_degradation(tmp_path: Path) -> None:
    """真实 pipeline 降级端到端（T1.6 评审 R-1/R-2/R-4）：

    3 资产真实 ScanOrchestrator + 墙钟 1s：run1 正常（真实 decompile 耗时越墙钟），
    run2/3 启动前降级——降级必须真正跳过 AI 阶段（修复前 orchestrator 不消费
    run config 的 ai 段，降级只落审计元数据、预算帽仍会被超耗）。
    """
    settings, repository, storage, registry = _make_stack(
        tmp_path, BatchSettings(max_concurrent_runs=1, max_wall_seconds=1)
    )
    assets = _register_assets(registry, 3)
    # 不注入 factory：使用默认真实 ScanOrchestrator（无 AI key 时 preflight
    # 跳过，单 run 秒级；jadx 真实反编译确保 run1 耗时 > 1s 触发墙钟）
    orchestrator = BatchOrchestrator(settings, repository, storage, None, registry)

    batch = orchestrator.create_batch([asset["id"] for asset in assets])
    asyncio.run(_run(orchestrator, batch["id"]))

    final = orchestrator.get_batch(batch["id"])
    assert final["status"] == "completed"
    assert final["total_runs"] == 3
    assert final["ai_skipped"] == 2
    assert final["ai_skipped_by_wall_clock"] == 2

    with repository.connect() as db:
        rows = db.execute(
            """SELECT id, status, ai_skipped_by_batch_budget FROM runs
            WHERE batch_id=? ORDER BY created_at""",
            (batch["id"],),
        ).fetchall()
    assert [row[2] for row in rows] == [0, 1, 1]  # run1 正常、run2/3 降级
    assert all(row[1] == "completed" for row in rows)  # 降级 run 仍完成确定性主链

    # R-1 核心：降级 run 的 AI 阶段真实跳过（manifest 断言，非仅元数据）
    for run_id, degraded in [(rows[0][0], False), (rows[1][0], True), (rows[2][0], True)]:
        manifest = storage.read_manifest(run_id)
        ai_stages = [s for s in manifest["stages"] if s["name"] == "ai_analysis"]
        assert ai_stages, f"{run_id} 缺 ai_analysis 阶段"
        summary = ai_stages[0]["summary"]
        assert summary["requests_used"] == 0
        if degraded:
            assert ai_stages[0]["status"] == "skipped"
            assert "batch 预算/墙钟降级" in summary["reason"]

    # 资产联动（3-APK 真实批次：方案 L160 验收要素）
    for asset in assets:
        latest = registry.get(asset["id"])
        assert latest["status"] == "ready"
        assert latest["last_run_id"] is not None


# ----------------------------------------------------------------------
# run config 共享（A-10：golden 期望 dict，评审 R-5）
# ----------------------------------------------------------------------


def test_run_config_golden(tmp_path: Path) -> None:
    settings, _, _, _ = _make_stack(tmp_path)
    from app.runs.run_config import build_run_config

    # golden：按提取前 routes 内联构造固化（source_analysis_enabled=False 分支）
    golden = {
        "analysis_platform_api": settings.analysis_platform_api,
        "source_analysis": {
            **settings.source_analysis.model_dump(mode="json"),
            "enabled": False,
        },
        "ai": {
            "enabled": settings.ai.enabled,
            "allow_external_code": settings.ai.allow_external_code,
            "provider_kind": "openai-compatible",
            "model": settings.ai.model,
        },
        # explorer-run-toggle：explorer 段随快照恒存在；未显式传参时沿用 settings
        "explorer": {
            **settings.explorer.model_dump(mode="json"),
            "enabled": settings.explorer.enabled,
        },
    }
    assert build_run_config(settings, source_analysis_enabled=False) == golden

    # 降级分支（D4）：ai_enabled=False + skip_reason 注入，其余 AI 元数据保留
    degraded = build_run_config(
        settings, source_analysis_enabled=True, ai_enabled=False, ai_skip_reason="batch_budget"
    )
    assert degraded["source_analysis"]["enabled"] is True
    assert degraded["ai"]["enabled"] is False
    assert degraded["ai"]["skip_reason"] == "batch_budget"
    assert degraded["ai"]["model"] == settings.ai.model
    # 默认分支：不携带 skip_reason 键
    assert "skip_reason" not in build_run_config(settings)["ai"]


# ----------------------------------------------------------------------
# 迁移 v5（A-11：v4 库构造法仿 test_repository_v4_migration 手法，评审 R-8）
# ----------------------------------------------------------------------


def test_migrate_v5_upgrade_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "v4-to-v5.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    with sqlite3.connect(path) as db:
        # 回退到 v4 形状：删 v5 记录 + 回退版本 + 重建无 assets_json 的 batches
        db.execute("DELETE FROM schema_migrations WHERE version=5")
        db.execute("PRAGMA user_version=4")
        db.execute("DROP TABLE batches")
        db.execute(
            "CREATE TABLE batches (id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',"
            " max_ai_calls INTEGER, max_wall_seconds INTEGER, ai_skipped_count INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT)"
        )
        db.execute(
            "INSERT INTO batches (id, created_at, updated_at) VALUES ('batch_old', ?, ?)",
            (NOW, NOW),
        )
        db.commit()

    repository.initialize()  # v4 → v5 升级

    with sqlite3.connect(path) as db:
        versions = [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == [1, 2, 3, 4, 5]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        columns = {row[1] for row in db.execute("PRAGMA table_info(batches)").fetchall()}
        assert "assets_json" in columns
        # 既有行获得 DEFAULT '[]'
        assert db.execute("SELECT assets_json FROM batches WHERE id='batch_old'").fetchone()[0] == "[]"

    repository.initialize()  # 幂等重跑（N-6）
    with sqlite3.connect(path) as db:
        versions = [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == [1, 2, 3, 4, 5]
        assert db.execute("SELECT COUNT(*) FROM batches WHERE id='batch_old'").fetchone()[0] == 1


def test_migrate_v5_fresh_database(tmp_path: Path) -> None:
    from app.shared.repository import DATABASE_SCHEMA_VERSION

    repository = SQLiteRepository(tmp_path / "fresh.sqlite3")
    repository.initialize()
    with sqlite3.connect(tmp_path / "fresh.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SCHEMA_VERSION == 5
        columns = {row[1] for row in db.execute("PRAGMA table_info(batches)").fetchall()}
        assert "assets_json" in columns


# ----------------------------------------------------------------------
# SQL 注入安全（机制防退化断言）
# ----------------------------------------------------------------------


def test_batch_sql_injection_safety(tmp_path: Path) -> None:
    settings, repository, storage, registry = _make_stack(tmp_path)
    factory = _factory_with_behaviors(storage, repository, ["ok"])
    orchestrator = _make_orchestrator(settings, repository, storage, registry, factory)

    for bad_id in ["x'; DROP TABLE batches;--", "y\" OR 1=1--"]:
        with pytest.raises(NotFoundError):
            orchestrator.get_batch(bad_id)
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='batches'").fetchone()[0] == 1
