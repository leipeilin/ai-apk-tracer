"""批量编排：资产副本 → run 创建（降级判定）→ 并发扫描 → 状态汇总（T1.3）。

设计依据：docs/analysis/2026-08-22-t1-3-implementation-plan.md
（含评审 R-1~R-10 修订：to_thread ingest / create_run 三列一次落库 /
gather(return_exceptions) / 条件 UPDATE 抢占 / skip_reason 分解计数）。

关键语义：
- batch 状态机 pending→running→completed/partial/failed；
- 预算/墙钟降级（run 启动前判定）：config ai.enabled=false + runs.
  ai_skipped_by_batch_budget=1 + config_json.ai.skip_reason（原因可审计）；
- 预算计数 = 已完成 run 的 manifest stages[ai_analysis].summary.requests_used
  累加（持久化事实源，D2）；
- 崩溃恢复为 Phase 1 已知限制（D6）：running batch 悬挂，恢复路径为按
  runs 聚合人工判定 + 失败资产子集新建 batch。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.assets.registry import AssetRegistry
from app.runs.run_config import build_run_config
from app.shared.errors import ConflictError, NotFoundError, ValidationError
from app.shared.repository import SQLiteRepository

LOGGER = logging.getLogger(__name__)

BATCH_TERMINAL_STATUSES = ("completed", "partial", "failed")


class BatchOrchestrator:
    """批量编排器：创建快照 + 并发执行 + runs 聚合汇总。

    SQL 全参数绑定（领域模块自持 SQL，registry 先例）；事务复用
    repository.connect()；不做 API 门禁（T1.4）。
    """

    def __init__(
        self,
        settings: Any,
        repository: SQLiteRepository,
        storage: Any,
        ai_runtime: Any,
        registry: AssetRegistry,
        orchestrator_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._storage = storage
        self._ai_runtime = ai_runtime
        self._registry = registry
        # 测试注入 fake（默认每资产 new 一个 ScanOrchestrator：构造轻量，
        # ai_runtime 显式传入不会误关共享运行时）
        if orchestrator_factory is None:
            from app.analysis.orchestrator import ScanOrchestrator

            self._orchestrator_factory = lambda: ScanOrchestrator(
                settings, repository, storage, ai_runtime
            )
        else:
            self._orchestrator_factory = orchestrator_factory
        self._ai_used = 0  # batch 生命周期内累计 AI 请求数（D2 软帽计数）

    # ------------------------------------------------------------------
    # 创建（API 请求内，秒回：仅 DB 行，无文件操作——D1）
    # ------------------------------------------------------------------

    def create_batch(self, asset_ids: list[str]) -> dict[str, Any]:
        """创建批量扫描（pending）：校验资产 → 预算快照 + 资产清单快照落库。"""

        if not isinstance(asset_ids, list):
            raise ValidationError("asset_ids 必须为列表", "INVALID_ASSET_IDS")
        # 重复 id 去重保序（评审 R-10 定死）
        deduped: list[str] = []
        for asset_id in asset_ids:
            if asset_id not in deduped:
                deduped.append(asset_id)
        if not deduped:
            raise ValidationError("asset_ids 至少包含 1 项", "BATCH_ASSETS_REQUIRED")

        # 快照对象数组（评审 R-3：资产删除后审计信息可回溯）
        assets_snapshot: list[dict[str, str]] = []
        for asset_id in deduped:
            asset = self._registry.get(asset_id)  # 不存在 → NotFoundError
            assets_snapshot.append(
                {
                    "asset_id": asset["id"],
                    "package_name": asset["package_name"],
                    "apk_sha256": asset["apk_sha256"],
                }
            )

        batch_id = (
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:12]}_{uuid.uuid4().hex[:8]}"
        )
        now = datetime.now(UTC).isoformat()
        with self._repository.connect() as db:
            db.execute(
                """INSERT INTO batches
                (id, status, max_ai_calls, max_wall_seconds, ai_skipped_count,
                 assets_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?)""",
                (
                    batch_id,
                    "pending",
                    int(self._settings.batch.max_ai_calls),
                    int(self._settings.batch.max_wall_seconds),
                    json.dumps(assets_snapshot, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get_batch(batch_id)

    # ------------------------------------------------------------------
    # 查询（runs 聚合事实源 + 降级原因分解，D7）
    # ------------------------------------------------------------------

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        """读取 batch 行 + runs 聚合汇总（total/completed/failed/ai_skipped
        及 skip_reason 分解：by_budget/by_wall_clock）。"""

        with self._repository.connect() as db:
            row = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise NotFoundError("batch", batch_id)
            aggregate = db.execute(
                """SELECT COUNT(*),
                          COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0),
                          COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0),
                          COALESCE(SUM(CASE WHEN ai_skipped_by_batch_budget=1 THEN 1 ELSE 0 END), 0)
                FROM runs WHERE batch_id=?""",
                (batch_id,),
            ).fetchone()
            # 降级原因分解（评审 R-4）：解析该批 runs 的 config_json.ai.skip_reason
            by_budget = by_wall_clock = 0
            for (config_json,) in db.execute(
                "SELECT config_json FROM runs WHERE batch_id=? AND ai_skipped_by_batch_budget=1",
                (batch_id,),
            ).fetchall():
                reason = self._skip_reason_of(config_json)
                if reason == "batch_budget":
                    by_budget += 1
                elif reason == "batch_wall_clock":
                    by_wall_clock += 1

        batch = dict(row)
        batch["assets"] = json.loads(batch.get("assets_json") or "[]")
        batch["total_runs"], batch["completed_runs"], batch["failed_runs"], batch["ai_skipped"] = (
            int(value) for value in aggregate
        )
        batch["ai_skipped_by_budget"] = by_budget
        batch["ai_skipped_by_wall_clock"] = by_wall_clock
        return batch

    # ------------------------------------------------------------------
    # 执行（BackgroundTask 内）
    # ------------------------------------------------------------------

    async def run_batch(self, batch_id: str) -> None:
        """编排主流程：抢占 → 逐资产并发执行 → 汇总终态（状态机 §3.2）。"""

        self.get_batch(batch_id)  # 不存在 → NotFoundError
        self._claim_batch(batch_id)  # 非唯一 pending → ConflictError（评审 R-7）

        batch = self.get_batch(batch_id)
        assets: list[dict[str, str]] = batch["assets"]
        started = time.monotonic()
        semaphore = asyncio.Semaphore(int(self._settings.batch.max_concurrent_runs))

        async def process_asset(asset_snapshot: dict[str, str]) -> None:
            asset_id = asset_snapshot["asset_id"]
            try:
                await self._process_asset(batch, asset_snapshot, semaphore, started)
            except Exception:
                # 资产级兜底（评审 R-6）：单点异常不取消其余（partial 语义由汇总判定）
                LOGGER.exception("batch 资产处理失败", extra={"batch_id": batch_id})
                try:
                    self._registry.update_status(asset_id, "error")
                except Exception:
                    LOGGER.exception("资产失败状态回写出错", extra={"asset_id": asset_id})

        try:
            await asyncio.gather(*(process_asset(item) for item in assets), return_exceptions=True)
        finally:
            self._finalize_batch(batch_id)

    async def _process_asset(
        self,
        batch: dict[str, Any],
        asset_snapshot: dict[str, str],
        semaphore: asyncio.Semaphore,
        started: float,
    ) -> None:
        """单资产处理：ingest → 建行 → 扫描 → 终态联动（全程持信号量）。

        信号量包整个流程（而非仅 scan）：降级判定在 scan 启动前进行，
        判定顺序必须与 scan 完成顺序一致（预算计数依赖前序 run 的
        requests_used 已累加），故资产级串行化（批内并发 = 同时处理的
        资产数，max_concurrent_runs 语义），磁盘拷贝并发随之受约束。
        """

        asset_id = asset_snapshot["asset_id"]
        asset = self._registry.get(asset_id)  # 已删资产 → NotFoundError → 协程兜底跳过
        run_id: str | None = None
        try:
            async with semaphore:
                degraded, skip_reason = self._degrade_decision(batch, started)
                config = build_run_config(
                    self._settings,
                    source_analysis_enabled=True,
                    ai_enabled=False if degraded else None,
                    ai_skip_reason=skip_reason,
                )
                # 同步文件拷贝（含 open）整体走线程池（评审 R-1）：不阻塞事件循环
                def _ingest_copy() -> dict[str, Any]:
                    with open(asset["apk_path"], "rb") as source:
                        return self._storage.ingest(
                            source,
                            asset["apk_filename"],
                            batch["id"],  # trace_id = batch_id（D5：按批次聚合审计）
                            config,
                        )

                ingested = await asyncio.to_thread(_ingest_copy)
                run_id = ingested["id"]
                run = self._repository.create_run(
                    {
                        "id": run_id,
                        "trace_id": batch["id"],
                        "status": "queued",
                        "stage": "queued",
                        "apk_filename": asset["apk_filename"],
                        "apk_sha256": ingested["sha256"],
                        "config": config,
                        "manifest_path": str(self._storage.run_dir(run_id) / "manifest.json"),
                        "asset_id": asset_id,
                        "batch_id": batch["id"],
                        "ai_skipped_by_batch_budget": 1 if degraded else 0,
                    }
                )
                self._registry.update_status(asset_id, "scanning")
                self._registry.link_run(asset_id, run["id"])

                await self._orchestrator_factory().scan(run_id)

                # 终态联动 + 预算计数（容错：不因计数中断批次）
                final_run = self._repository.get_run(run_id)
                self._registry.update_status(
                    asset_id, "ready" if final_run["status"] == "completed" else "error"
                )
                if not degraded:
                    self._ai_used += self._requests_used_of(run_id)
        except Exception:
            # run 已创建但未收敛终态（scan 外意外）→ 置 failed（runs 为事实源，
            # 不留悬挂 queued 行）；资产状态由协程兜底统一回写
            if run_id is not None:
                try:
                    self._repository.update_run(
                        run_id,
                        status="failed",
                        stage="failed",
                        error_code="BATCH_ASSET_PROCESSING_ERROR",
                    )
                except Exception:
                    LOGGER.exception("run 失败收敛写出错", extra={"run_id": run_id})
            raise

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _degrade_decision(self, batch: dict[str, Any], started: float) -> tuple[bool, str | None]:
        """run 启动前降级判定（D2/D3）：预算耗尽或墙钟超限 → 跳过 AI 仅主链。"""

        max_ai_calls = int(batch.get("max_ai_calls") or 0)
        if max_ai_calls > 0 and self._ai_used >= max_ai_calls:
            return True, "batch_budget"
        limit = int(batch.get("max_wall_seconds") or 0)
        if limit > 0 and (time.monotonic() - started) >= limit:
            return True, "batch_wall_clock"
        return False, None

    def _requests_used_of(self, run_id: str) -> int:
        """从 manifest 的 ai_analysis 阶段读取 AI 请求数（读不到按 0 + 告警，N-5）。"""

        try:
            manifest = self._storage.read_manifest(run_id)
            for stage in manifest.get("stages", []):
                if stage.get("name") == "ai_analysis":
                    return int(stage.get("summary", {}).get("requests_used") or 0)
        except Exception:  # noqa: BLE001 - 容错边界：任何读取失败均按 0 计入（N-5），不中断批次
            LOGGER.warning("读取 run AI 请求数失败，按 0 计入 batch 预算", extra={"run_id": run_id})
        return 0

    @staticmethod
    def _skip_reason_of(config_json: str | None) -> str | None:
        try:
            ai_section = (json.loads(config_json or "{}")).get("ai") or {}
            reason = ai_section.get("skip_reason")
            return str(reason) if reason else None
        except Exception:  # noqa: BLE001 - 解析容错：异常 config_json 一律按无降级原因处理
            return None

    def _claim_batch(self, batch_id: str) -> None:
        """抢占（评审 R-7）：条件 UPDATE 判 rowcount，并发双触发仅一方成功。"""

        now = datetime.now(UTC).isoformat()
        with self._repository.connect() as db:
            cursor = db.execute(
                "UPDATE batches SET status='running', updated_at=? WHERE id=? AND status='pending'",
                (now, batch_id),
            )
            if cursor.rowcount == 0:
                raise ConflictError(
                    "batch 不处于 pending 状态，拒绝重复执行",
                    code="BATCH_NOT_PENDING",
                    details={"batch_id": batch_id},
                )

    def _finalize_batch(self, batch_id: str) -> None:
        """按 runs 聚合写入 batch 终态（D7：completed/partial/failed + 汇总）。"""

        batch = self.get_batch(batch_id)
        total, completed, failed = batch["total_runs"], batch["completed_runs"], batch["failed_runs"]
        if total == 0 or failed == 0:
            status = "completed"
        elif completed == 0:
            status = "failed"
        else:
            status = "partial"
        now = datetime.now(UTC).isoformat()
        with self._repository.connect() as db:
            db.execute(
                """UPDATE batches
                SET status=?, ai_skipped_count=?, completed_at=?, updated_at=?
                WHERE id=?""",
                (status, batch["ai_skipped"], now, now, batch_id),
            )
        LOGGER.info(
            "batch 执行结束",
            extra={
                "batch_id": batch_id,
                "stage": "batch_finalized",
                "error_code": status,
                "duration_ms": 0,
            },
        )
