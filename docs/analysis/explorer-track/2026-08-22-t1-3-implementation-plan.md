# 任务实施方案：T1.3（批量编排 batch.py）

> **任务编号**：T1.3
> **日期**：2026-08-22
> **依据大纲**：
> - 方案：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan.md` Phase 1（批量扫描、batch 预算帽）+ §4.12/§4.9 语义
> - 实施计划：`docs/analysis/explorer-track/2026-08-21-explorer-track-implementation-plan.md` T1.3（串行/并发、失败重试、预算帽与降级、`ai_skipped_by_batch_budget` 标记）
> - T0.8 设计稿：batches 表 + runs 关联列（v4 已落地）
> **状态**：起草
> **前置依赖**：T1.2（AssetRegistry）、T1.1（v4 迁移：runs.asset_id/batch_id/ai_skipped_by_batch_budget）

---

## 1. 任务目标与范围

- **目标**：实现 `backend/app/assets/batch.py`——批量编排：资产副本 → run 创建（预算/墙钟降级判定）→ 并发扫描（`batch.max_concurrent_runs`）→ batch 状态机与可审计汇总。
- **范围**：
  - `BatchOrchestrator`（create_batch/get_batch/run_batch）+ batches 表 CRUD（SQL 全参数绑定，领域模块自持 SQL——registry 先例）；
  - 迁移 v5：batches 加 `assets_json` 快照列（见 §3.3 决策 D1）；
  - run config 构造提取共享（`app/runs/run_config.py`，routes 复用去重）；
  - orchestrator AI 阶段 summary 补 `requests_used`（manifest 持久化——batch 预算计数来源）；
  - `backend/tests/test_batch.py`。
- **非范围**：API 端点（T1.4）、前端（T1.5）、资产状态之外的重试策略细化（"失败单独重跑"= 对失败资产子集新建 batch，T1.4/T1.5 承载交互）。

## 2. 现状锚点（2026-08-22 复核）

- **batches 表**（repository.py:234-246）：`id/status('pending')/max_ai_calls/max_wall_seconds/ai_skipped_count(0)/created_at/updated_at/completed_at`——**无资产清单存储**（T0.8 缺口，见 D1）。
- **runs 三列已落地**（repository.py:247-260）：`asset_id`/`batch_id`（FK SET NULL）+ `ai_skipped_by_batch_budget`（default 0）。
- **BatchSettings**（config.py:222-227）：`max_concurrent_runs(2)`/`max_ai_calls(0=run 级)`/`max_wall_seconds(0=不限)`。
- **run 启动链路**（routes.py:108-139）：config 构造（analysis_platform_api/source_analysis/ai 三段）→ `storage.ingest` → `repository.create_run` → `background_tasks.add_task(orchestrator.scan, run_id)`。
- **run 级 AI 预算已存在**（orchestrator.py:847-850）：`_ai_requests_used` 计数 + `budget.max_requests_per_run` 熔断；AI 阶段 summary（L547-553）写 manifest stages（`ai_analysis`），**无 requests_used 字段**（本任务补）。
- **AI 禁用行为**：config `ai.enabled=false` → 跳过 AI 阶段（降级复用此行为，方案 L154 明确）。
- **降级语义**（方案 L152-154）："`max_ai_calls`>0 超限：**未启动的 run** 降级为跳过 AI 仅确定性主链，run 记录 `ai_skipped_by_batch_budget` 标记，batch 汇总可审计"。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/assets/batch.py` | 新增 | BatchOrchestrator + batches CRUD |
| `backend/app/shared/repository.py` | 修改 | 迁移 v5（batches.assets_json）+ `DATABASE_SCHEMA_VERSION=5` + `create_run` INSERT 扩展可选三列（asset_id/batch_id/ai_skipped_by_batch_budget，评审 R-2：一次落库消除两步不一致窗口） |
| `backend/app/runs/run_config.py` | 新增 | `build_run_config()`（routes/batch 共享） |
| `backend/app/api/routes.py` | 修改 | create_run 改用共享 config 构造（行为等价，golden 测试先行，评审 R-5） |
| `backend/app/analysis/orchestrator.py` | 修改 | AI summary 补 `requests_used` |
| `backend/tests/test_batch.py` | 新增 | 编排/降级/汇总/迁移测试 |
| `backend/tests/test_api.py`（或 config 快照归属文件） | 修改 | 固化 golden config 期望断言（改 routes 前先行，评审 R-5） |

> 受控越界标注（评审确认）：routes/orchestrator/repository 变更超出实施计划 T1.3 行的文件清单（仅 batch.py），属共享重构 + 最小扩展。

### 3.2 `BatchOrchestrator` 设计

```python
class BatchOrchestrator:
    """批量编排：资产副本 → run 创建（降级判定）→ 并发扫描 → 状态汇总（T1.3）。

    SQL 全参数绑定（领域模块自持 SQL，registry 先例）；事务复用
    repository.connect()；不做 API 门禁（T1.4）。
    """

    def __init__(self, settings, repository, storage, ai_runtime, registry,
                 orchestrator_factory=None) -> None:
        # orchestrator_factory: Callable[[], AsyncScanProtocol]——默认 new
        # ScanOrchestrator(settings, repository, storage, ai_runtime)；测试注入 fake。

    # --- 创建（API 请求内，秒回：仅 DB 行，无文件操作） ---
    def create_batch(self, asset_ids: list[str]) -> dict:
        """校验资产存在（registry.get 逐个，NotFoundError 即 404）→ INSERT batches
        （pending + 预算快照 settings.batch.max_ai_calls/max_wall_seconds +
        assets_json 顺序快照）→ 返回记录。幂等性由调用方保证（重复创建=新 batch）。

        **不在创建时 ingest**（D1）：APK 拷贝是重文件操作且无法与 DB 事务对齐，
        放执行期逐资产做。
        """

    # --- 查询（含 runs 聚合汇总） ---
    def get_batch(self, batch_id: str) -> dict:
        """batch 行 + 汇总：SELECT COUNT(*), SUM(status='completed'),
        SUM(status='failed'), SUM(ai_skipped_by_batch_budget=1) FROM runs
        WHERE batch_id=?（参数绑定）→ total/completed/failed/ai_skipped。"""

    # --- 执行（BackgroundTask 内） ---
    async def run_batch(self, batch_id: str) -> None:
        """编排主流程（状态机 pending→running→终态）：

        1. get_batch 校验（不存在 → NotFoundError）；**抢占**（评审 R-7）：
           `UPDATE batches SET status='running' WHERE id=? AND status='pending'`，
           rowcount=0 → ConflictError（并发双触发仅一方成功）；
        2. 逐资产协程（assets_json 顺序）+ `asyncio.gather(..., return_exceptions=True)`
           （评审 R-6：单协程异常不取消其余）：
           a. registry.get：资产已删 → 该项跳过（记日志，继续）；
           b. 降级判定（见 §3.4）→ build_run_config（降级时 ai_enabled=False +
              ai_skip_reason）；
           c. `await asyncio.to_thread(storage.ingest, ...)`（评审 R-1：同步
              文件 IO 不阻塞事件循环——拷贝期间在跑 scan 的 async 推进不受影响；
              不入信号量：`max_concurrent_runs` 语义为扫描并发）；
           d. `repository.create_run(..., asset_id=?, batch_id=?,
              ai_skipped_by_batch_budget=?)`——INSERT 一次落库三列（评审 R-2，
              消除 ingest→UPDATE 两步不一致窗口）；
           e. registry.update_status(asset_id,'scanning') + link_run；
           f. `async with semaphore: await orchestrator_factory().scan(run_id)`
              （Semaphore(batch.max_concurrent_runs)）；
           g. run 终态联动：repository.get_run → completed→'ready' / 其他→'error'；
              非降级 run 从 manifest stages[ai_analysis].summary.requests_used
              累加 self._ai_used（容错：读不到按 0 + 告警日志，评审 N-5）；
           h. 协程内 try/except 兜底（评审 R-6）：资产级异常 → 该资产 error +
              记日志，不向上传播（partial 语义由汇总判定）；
        3. 汇总终态：全部 completed→'completed'；部分 failed→'partial'；
           全 failed→'failed'；ai_skipped_count=runs 聚合（WHERE
           ai_skipped_by_batch_budget=1）；completed_at 落库。
        """
```

**并发模型（评审 R-1 修订）**：每资产一个协程，协程内 `to_thread(ingest)`（磁盘拷贝走线程池，事件循环不阻塞）→ 建行 → `async with semaphore` 内 `await scan`（信号量仅约束扫描并发 = `batch.max_concurrent_runs` 语义）。磁盘并发拷贝上限 = 资产数（线程池默认容量内），单机批量场景可接受（见风险表）。

### 3.3 关键设计决策

**D1：batch 资产清单存储——迁移 v5 加 `batches.assets_json`（TEXT NOT NULL DEFAULT '[]'）**
- T0.8 batches 表无清单列（隐含"run 关联即事实源"，但其假设 batch 创建即建 run）；
- 备选 A（创建时同步 ingest 全部 → runs 即清单）否决：create_batch 请求耗时=N×APK 拷贝（10×500MB 级请求超时风险）；文件操作无法纳入 DB 事务，部分失败回滚复杂；
- 备选 B（新表 batch_items）否决：单值顺序快照无关联完整性需求，加表过重；
- 备选 C（创建时仅预建 runs 行、不 ingest——纯 DB 原子，评审 R-3 补记）否决：run_id 由 ingest 生成（时间戳_sha_uuid，storage.py:71），预建需先行成 id 与 manifest_path 但 manifest/目录延后写入——中间态"幽灵 queued run"（有行无目录）污染 run 列表与既有 run 生命周期语义（状态机/清理/前端轮询均假设行目录一致）；
- **采纳**：`assets_json` 顺序快照，元素为对象 `{asset_id, package_name, apk_sha256}`（评审 R-3：资产删除后包名/sha256 审计信息可回溯）；创建秒回、清单可审计；资产删除后 batch 清单保留快照——执行时跳过（SET NULL 类似语义）；batch 汇总仍以 runs 表为执行后事实源；
- 迁移 v5：`PRAGMA table_info` 幂等加列（v4 风格）；`DATABASE_SCHEMA_VERSION` 4→5；旧库（v4 含 batches）升级路径测试。

**D2：预算降级判定时机与计数来源**
- 判定：**每 run 启动前**（ingest 前）——`max_ai_calls>0 and self._ai_used >= max_ai_calls` → 该 run 降级（config `ai.enabled=false` + `ai_skip_reason='batch_budget'` + runs.ai_skipped_by_batch_budget=1）；
- 计数：run 完成后读 manifest `stages[ai_analysis].summary.requests_used`（orchestrator 微改补此字段，一行）——持久化事实源（进程内属性不可靠：崩溃恢复/审计重算均可走 manifest）；
- 预算耗尽语义 = "后续未启动 run 全部降级"（不预估单 run 用量——方案 L154"未启动的 run 降级"原文语义，简单可审计）。

**D3：墙钟降级同构预算降级**
- `max_wall_seconds>0` 且 elapsed（monotonic，run_batch 起点起算）超限 → 未启动 run 降级为"跳过 AI 仅确定性主链"（`ai_skip_reason='batch_wall_clock'`，同一标记列）；
- 在跑 run 不中断（kill 语义破坏证据留存，违背 retention 原则）；
- 理由：与预算降级统一"可用性优先"——超时仍产出确定性主链结果；AI 跳过是最大耗时项的止损。

**D4：run config 提取共享（`app/runs/run_config.py`）**
- routes.create_run 内联构造（L109-121）与 batch 构造重复（工程化原则：复用工具代码）；
- `build_run_config(settings, source_analysis_enabled=True, ai_enabled=None, ai_skip_reason=None) -> dict`：`ai_enabled=None` 用 settings.ai.enabled；降级时 False + `ai.skip_reason` 附注（进 manifest 可审计）；
- routes 改为调用（行为等价——config JSON 逐字段对比测试保障）。

**D5：trace_id = batch_id**
- routes 场景 trace_id 来自请求 contextvar（logging.py:16）；batch 后台执行无请求上下文；
- batch 内全部 run 的 trace_id=batch_id：manifest/日志按批次聚合检索（审计友好）。

**D6：崩溃恢复为已知限制（Phase 1）**
- 进程重启 → running batch 悬挂（状态不再推进）；恢复路径：按 runs 聚合人工判定 + 对未完成资产新建 batch（重跑语义）；
- 不做自动恢复（需持久化执行游标，收益/复杂度不匹配 Phase 1）；文档记录。

**D7：batches 状态机与终态判定**
- `pending`（create）→ `running`（run_batch 抢占成功）→ `completed`（全部 run completed）/ `partial`（部分 failed）/ `failed`（全部 failed）；
- 资产缺失跳过不计失败（缺失资产不建 run，汇总以 runs 为准，缺失在 get_batch 的 assets_json 差集展示）；
- ai_skipped_count 来源 runs 聚合（非内存计数——崩溃后 get_batch 重算仍准）；
- **降级原因分解（评审 R-4）**：get_batch 汇总在 Python 侧解析该批 runs 的 `config_json.ai.skip_reason`，拆分 `ai_skipped_by_budget` / `ai_skipped_by_wall_clock` 两计数（DB 列 `ai_skipped_by_batch_budget` 为两类合计，T0.8 语义保持"跳过 AI 的 run 数"；原因经 config_json 可辨、可审计）。

### 3.4 降级判定伪代码

```python
def _degrade_decision(self, batch: dict, started: float) -> tuple[bool, str | None]:
    """run 启动前判定（D2/D3）：返回 (degraded, skip_reason)。"""
    if int(batch["max_ai_calls"] or 0) > 0 and self._ai_used >= int(batch["max_ai_calls"]):
        return True, "batch_budget"
    limit = int(batch["max_wall_seconds"] or 0)
    if limit > 0 and (time.monotonic() - started) >= limit:
        return True, "batch_wall_clock"
    return False, None
```

### 3.5 测试方案（`test_batch.py`）

编排逻辑测试注入 FakeOrchestrator（`scan(run_id)` 可控行为：成功/抛异常/写含 requests_used 的 manifest summary）——不跑真实 decompile/AI：

1. **test_create_batch_persists_snapshot**：创建 → pending + 预算快照（settings.batch 值）+ assets_json 顺序一致；资产不存在 → NotFoundError；空列表 → ValidationError；
2. **test_run_batch_full_flow**：3 资产 fake scan 全成功 → batch completed；runs 关联列（asset_id/batch_id）正确；资产状态 ready；last_run_id 更新；get_batch 汇总（total/completed=3/failed=0/ai_skipped=0）；
3. **test_run_batch_partial_failure**：1/3 scan 抛异常 → batch partial + failed=1 + 该资产 error；**断言其余 2 资产正常完成（单点异常不取消其余，评审 R-6）**；
4. **test_run_batch_budget_degradation**：`max_ai_calls=2` 快照，fake scan 每次写 requests_used=1：run1/2 正常（_ai_used=2），run3 降级（ai_skipped=1 + config.ai.enabled=false + skip_reason='batch_budget'）→ batch ai_skipped_count=1；降级 run 无 ai_analysis 阶段不累加计数；
5. **test_run_batch_wall_clock_degradation**：`max_wall_seconds=1` + fake scan sleep(2)：run1 正常，run2+ 降级（skip_reason='batch_wall_clock'）；get_batch 分解计数正确（评审 R-4）；
6. **test_run_batch_rejects_non_pending**：run_batch 二次调用 → ConflictError；**并发抢占语义（评审 R-7）：条件 UPDATE rowcount 判定**；
7. **test_run_batch_missing_asset_skipped**：清单含已删资产 → 跳过不建 run，其余正常，batch completed；
8. **test_run_batch_concurrency_limit**：`max_concurrent_runs=1` + fake scan 记录并发峰值 → 峰值=1（信号量实证）；
9. **test_get_batch_summary_from_runs**（D7）：直接篡改 runs 行后 get_batch 重算正确（聚合非内存态）；
10. **test_run_config_golden**（评审 R-5）：**golden 期望 dict 直接断言**（非与 routes 对比）——期望值按现行内联构造固化（重构前先写，实施顺序保证）；含降级参数分支（ai_enabled=False + skip_reason 注入）；
11. **test_migrate_v5**（迁移，评审 R-8 构造法）：仿 `test_repository_v4_migration.py` 手法构造 v4 库——新库 initialize 后**回退 schema_migrations v5 记录 + DROP batches 表重建 v4 形状（无 assets_json 列）** → 再 initialize → 列存在 + 既有行 DEFAULT '[]'；幂等重跑；user_version=5；
12. **test_orchestrator_summary_requests_used**：AI 阶段 summary 含 requests_used（复用既有 orchestrator 测试模式，最小用例）。

### 3.6 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性 |
|---|---|---|
| 实施计划 T1.3（串行/并发 `batch.max_concurrent_runs`） | §3.2 Semaphore + gather | 一致 |
| 实施计划 T1.3（失败重试） | "失败单独重跑"=失败资产子集新建 batch（方案 L162 验收原文）；资产状态 error 可检索 | 一致（交互归 T1.4/T1.5） |
| 方案 §4.12/L152-154（预算帽/降级标记/汇总可审计） | D2 判定+计数、runs.ai_skipped_by_batch_budget、batches.ai_skipped_count 聚合 | 一致 |
| 方案 L162（并发上限/失败可单独重跑） | 测试 8 + 资产子集重建 batch | 一致 |
| T0.8（batches 表/runs 三列） | 全复用 + v5 补 assets_json（D1 缺口显式化） | 一致（增量） |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| run_batch 长时运行与进程重启（D6） | running batch 悬挂 | 已知限制文档化 + runs 聚合重算 + 子集重建 | 无自动恢复（Phase 1 明确） |
| ingest 与 DB 行不一致（拷贝成功后建行失败） | 孤儿 run 目录 | 逐资产 try/except：失败资产标 error 继续（partial 语义）+ 孤儿目录由 cleanup 兜底（既有机制） | 单资产失败不阻断批次 |
| 磁盘并发拷贝压力（to_thread 并发 ingest，评审 R-1） | 多资产同时拷贝 IO 争抢 | 单机批量场景可接受（线程池默认容量约束）；极端规模时 T1.6 评估串行化选项 | 信号量外再限 ingest 并发 |
| 预算为软帽（评审 R-9）：在途 run 请求不计入判定 | 实际消耗可超 max_ai_calls | 上界量化：max_concurrent_runs × max_requests_per_run（run 级熔断 L847-850 兜底）；scan 中途失败 summary 未写 → 已耗请求低估（容错路径按 0 计） | 文档化偏差；不因计数中断批次 |
| SQLite 并发写（batch 编排+scan+API） | 锁等待 | WAL + 短事务（既有）；batch 的 UPDATE 单行快速提交 | 无 |
| manifest 读失败（run 级损坏） | requests_used 计数缺失 | 容错：读不到按 0 累加 + 日志告警（不因计数中断批次） | 预算判定保守化风险已文档化 |
| v5 迁移破坏旧库 | batches 数据丢失 | 幂等加列（v4 风格）+ 升级测试（test_migrate_v5） | 旧库文件保留重建（T0.8 回滚策略） |

## 5. 依赖

- 前置：T1.2（AssetRegistry 注入）、T1.1（runs 三列）；运行时复用 storage.ingest/ScanOrchestrator/registry。
