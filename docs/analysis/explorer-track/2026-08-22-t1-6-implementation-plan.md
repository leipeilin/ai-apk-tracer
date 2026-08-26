# 任务实施方案：T1.6（batch 预算降级测试与迁移测试——收尾核查）

> **任务编号**：T1.6
> **日期**：2026-08-22
> **依据大纲**：
> - 实施计划：`docs/analysis/explorer-track/2026-08-21-explorer-track-implementation-plan.md` T1.6（`backend/tests/` 新增，依据 §4.12/§4.13）
> - 方案 Phase 1 验收清单：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan.md` L158-164
> **状态**：已闭合（独立复核发现 R-1 高危缺口 → 修复 + 补测 2 项 → 全量 955 passed）
> **前置依赖**：T1.1-T1.5 全部交付
> **任务性质**：**核查型**——预算降级测试与迁移测试已随 T1.3 前置交付（test_batch.py 15 项 + 迁移 v5 测试），本任务对上级验收要求逐条对账。**独立复核推翻"无缺口"初判**：发现降级未在真实 pipeline 生效的高危缺陷，已修复并补真实链路测试。

---

## 1. 核查方法论

1. 以方案 Phase 1 验收清单（L158-164，5 条）为主轴逐条对账；
2. 以 §4.12（batch 预算帽）/ §4.13（迁移机制）的要点为辅核对；
3. 已覆盖项给出测试证据（文件 + 用例名）；缺口项补实现；
4. 结论交子 agent 独立复核（防自查自证）。

## 2. 逐条对账

### 2.1 方案 L160："用 3 个本地 APK 导入并批量扫描成功，每个 APK 独立 run，结果可按批次汇总"

| 验收要素 | 证据 | 状态 |
|---|---|---|
| 3 资产导入 + 批量 | `test_run_batch_full_flow`（3 资产注册 → run_batch → completed） | 覆盖 |
| 每个 APK 独立 run | 同上：runs 关联列（asset_id/batch_id）逐一断言，3 run 各自独立 | 覆盖 |
| 按批次汇总 | `get_batch`：total/completed/failed/ai_skipped 聚合 + `test_get_batch_summary_from_runs`（篡改重算） | 覆盖 |
| 真实 pipeline 端到端（**评审 R-1/R-2 处置后补**） | `test_batch_real_pipeline_degradation`：3 资产真实 ScanOrchestrator（真实 jadx decompile）→ run1 正常、run2/3 墙钟降级、全 completed、资产 ready + last_run_id 联动；T1.5 手工端到端（1 资产）为辅证 | 覆盖（自动化真实链路） |

自动化测试用 FakeOrchestrator（编排协议层）；真实 pipeline 由既有 `test_upload_creates_run`（单 run 全链路）+ 本任务新增 `test_batch_real_pipeline_degradation`（3 资产真实批次）承载。

### 2.2 方案 L161："单 APK run 行为与当前一致（回归测试通过）"

全量 **953 passed / 0 failed**（含全部既有 run/findings/review/cleanup/API 测试）；routes 的 config 构造改造（T1.3 D4）有 golden 断言（`test_run_config_golden`）+ 既有端到端测试保障。

### 2.3 方案 L162："批量扫描有并发上限、失败任务可单独重跑"

| 验收要素 | 证据 | 状态 |
|---|---|---|
| 并发上限 | `test_run_batch_concurrency_limit`（max_concurrent_runs=1 → scan 并发峰值=1 实证） | 覆盖 |
| 失败可单独重跑 | 语义承载：失败资产子集新建 batch（`create_batch` 任意 asset_ids 子集 + 资产状态 error 可检索——T1.3 设计 §3.3，前端多选发起承载交互） | 覆盖（语义） |

"重跑"无专门自动化测试的理由：重跑 = 对资产子集再次 `create_batch`——该 API 已被 `test_create_batch_persists_snapshot`/`test_create_batch_dedupes_preserving_order` 覆盖，专门"重跑"测试不产生新断言面。

### 2.4 方案 L163："构造 batch.max_ai_calls=1 的批量任务：后续 run 正确降级为仅确定性主链，ai_skipped_by_batch_budget 标记可见、batch 汇总可审计"

| 验收要素 | 证据 | 状态 |
|---|---|---|
| max_ai_calls 快照生效 | `test_create_batch_persists_snapshot`（快照 7/99）+ `test_run_batch_budget_degradation`（=2 边界跨越）+ `test_run_batch_budget_degradation_cap_one`（**=1 字面**：run2/3 连续降级、批次继续，评审 R-4 处置后补） | 覆盖 |
| 降级为仅确定性主链 | **评审 R-1 发现真实缺口并修复**：orchestrator 原不消费 run config 的 ai 段（降级只落审计元数据，预算帽仍会被超耗）——已修（`_run` 读 config.ai.enabled → `_run_ai_stage` 跳过路径，classification=`disabled_by_run_config`）；`test_batch_real_pipeline_degradation` 真实链路断言降级 run 的 ai_analysis=skipped + reason 含"batch 预算/墙钟降级" + requests_used=0 | 覆盖（修复后） |
| ai_skipped_by_batch_budget 标记可见 | runs 列断言（`[0,0,1]` / `[0,1,1]`）；前端 BatchPanel 徽标展示（T1.5） | 覆盖 |
| batch 汇总可审计 | `ai_skipped_count` + `by_budget/by_wall_clock` 分解（`test_run_batch_wall_clock_degradation` 断言分解；T1.4 `test_get_batch_summary` API 层） | 覆盖 |
| manifest requests_used 事实源 | `test_api.py::test_upload_manifest_ai_summary_has_requests_used`（真实 pipeline 键存在，**评审 R-3 更正引用**——原报告误写为 `test_orchestrator_summary_requests_used`）+ FakeOrchestrator 同构消费（正数路径）+ 真实降级用例（=0 路径） | 覆盖 |

### 2.5 方案 L164："旧版本 tracer.sqlite3 经迁移脚本升级后结构与既有数据完好（迁移测试通过）"

| 迁移路径 | 证据 | 状态 |
|---|---|---|
| v1 legacy → v5（全链叠加） | `test_repository_v4_migration.py::test_v4_upgrade_from_v1_legacy_preserves_data`（v1 构造 → [1,2,3,4,5] + 数据完好） | 覆盖 |
| v3（含迁移记录）→ v5 | `test_v4_upgrade_from_v3_with_migration_records`（叠加路径构造） | 覆盖 |
| v4 → v5（batches 加列） | `test_batch.py::test_migrate_v5_upgrade_and_idempotent`（回退记录 + DROP/重建 v4 形状 → 升级 + DEFAULT '[]' + 既有行） | 覆盖 |
| 中断恢复（幂等重跑） | `test_v4_interrupted_migration_reruns_idempotently` + `test_migrate_v5_upgrade_and_idempotent` 幂等段 | 覆盖 |
| 大表迁移 | `test_v4_large_runs_table_migrates_correctly`（1000 行数据完好） | 覆盖 |
| 新库直建 v5 | `test_migrate_v5_fresh_database` + `test_v4_fresh_database_creates_assets_batches` | 覆盖 |
| schema_migrations 机制（§4.13：版本化注册、禁止内联改表） | 上述全部经 `initialize()` 迁移链（无内联 ALTER 路径） | 覆盖 |

### 2.6 §4.12 其余要点

- `batch.max_wall_seconds` 墙钟降级：`test_run_batch_wall_clock_degradation` ✓
- 配置样例（L509-512）：`BatchSettings` 字段与方案样例一致（T0.7 交付，test_config 覆盖）✓

## 3. 结论与范围声明（评审后修订）

- **原结论被独立复核推翻**（自查自证风险的现实印证）：复核发现 **R-1 高危缺口**——降级在真实 pipeline 未生效（orchestrator 不消费 run config 的 ai 段，batch 预算帽会被超耗）——"无新增代码"不成立。
- **修复与补充**（本任务交付）：
  1. `orchestrator.py`：`_run` 读 run config `ai.enabled`（默认 True 兼容历史 run）→ `_run_ai_stage` 新增 `ai_enabled` 参数与跳过路径（classification=`disabled_by_run_config`，候选标记同构"AI 不可用"）；
  2. `test_batch.py` 新增 2 项：`test_batch_real_pipeline_degradation`（3 资产真实 jadx 链路 + 墙钟降级真实跳过 AI 断言——同时闭环 R-1 验证 / R-2 的 3-APK 真实证据 / R-4 真实侧）+ `test_run_batch_budget_degradation_cap_one`（=1 字面连续降级）；
  3. 本报告 §2.1/§2.4 更正（R-2 手工证据表述如实化、R-3 测试名引用更正）。
- **维持不引入**：重跑专门测试（无新断言面，评审认可）；golden_evaluation 扩展（任务行未要求，评审认可）。
- T1.5 手工清单补"失败资产子集重跑"项（R-5，验收记录补录）。

## 4. 风险

| 风险 | 对策 |
|---|---|
| 自查自证（核查结论偏乐观） | 子 agent 独立复核已执行——并实际发现 R-1 高危缺口（流程有效性验证） |
| ~~语义等价性争议（max_ai_calls=2 vs =1）~~ | 已闭合：补 =1 字面用例（评审 R-4） |
| 修复回归（orchestrator 改动影响既有 AI 流程） | 全量 955 passed（+2）；跳过路径仅在 ai_enabled=False 时进入，既有 run（无该标记）行为不变 |
