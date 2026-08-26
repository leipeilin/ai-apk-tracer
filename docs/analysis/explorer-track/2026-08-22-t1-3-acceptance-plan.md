# 任务验收方案：T1.3（批量编排 batch.py）

> **任务编号**：T1.3
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t1-3-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测（FakeOrchestrator 注入）+ 全量回归 + 统一校验

---

## 1. 验收范围

- BatchOrchestrator（create_batch/get_batch/run_batch）+ 迁移 v5（assets_json）+ run_config 共享提取 + orchestrator summary 补字段 + 测试。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | batch 创建快照 | `test_create_batch_persists_snapshot` | pending + 预算快照 + assets_json 顺序 |
| A-2 | 编排全流程 | `test_run_batch_full_flow` | 3 资产 completed + runs 关联列 + 资产 ready + last_run_id |
| A-3 | 部分失败 | `test_run_batch_partial_failure` | batch partial + failed=1 + 资产 error + **其余资产正常完成（单点异常不取消其余，评审 R-6）** |
| A-4 | 预算降级 | `test_run_batch_budget_degradation` | max_ai_calls=2：run3 降级（标记+config+skip_reason='batch_budget'）+ ai_skipped_count=1 |
| A-5 | 墙钟降级 | `test_run_batch_wall_clock_degradation` | 超墙钟后未启动 run 降级（skip_reason='batch_wall_clock'）+ get_batch 分解计数正确（评审 R-4） |
| A-6 | 防重复执行 | `test_run_batch_rejects_non_pending` | 二次 run_batch → ConflictError |
| A-7 | 缺失资产跳过 | `test_run_batch_missing_asset_skipped` | 跳过不建 run，其余 completed |
| A-8 | 并发上限 | `test_run_batch_concurrency_limit` | max_concurrent_runs=1 → 峰值=1 |
| A-9 | 汇总走 runs 聚合 | `test_get_batch_summary_from_runs` | 篡改 runs 后 get_batch 重算正确 |
| A-10 | config 共享等价 | `test_run_config_golden`（评审 R-5：golden 期望 dict 直接断言，非与 routes 对比） | build_run_config 输出与固化期望一致 + 降级参数生效 |
| A-11 | v5 迁移 | `test_migrate_v5`（评审 R-8：v4 库构造法仿 v4 迁移测试手法） | v4→v5 加列 + DEFAULT '[]' + 幂等 + user_version=5 |
| A-12 | summary 补字段 | `test_orchestrator_summary_requests_used` | AI 阶段 manifest summary 含 requests_used |
| A-13 | 单测通过 | `.venv/bin/python -m pytest tests/test_batch.py -q` | 全部通过 |
| A-14 | 全量回归 | `.venv/bin/python -m pytest -q` | 926+ 全部通过（T1.2 基线 926） |
| A-15 | 统一校验 | `scripts/check-all.sh` + `ruff check` | 通过 |
| A-16 | routes 等价回归 | golden config 快照断言先行固化（改 routes 前写入 test_api.py 并先行通过，评审 R-5 实施顺序保证） | create_run 行为不变 |

## 3. 回归标准

- [ ] 单 APK run 行为与当前一致（方案 L161 验收原文：routes 改造等价性由既有 API 测试保障）。
- [ ] T1.2 资产注册表测试不受影响。
- [ ] 迁移链 v2→v3→v4→v5 叠加路径正确（test_migrate_v5 含 v4 库构造）。
- [ ] `ruff check` 通过。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 空/重复资产列表 | `create_batch([])` / 重复 id | 空列表 → ValidationError（至少 1 项）；重复 id **去重保序**（评审 R-10 定死），去重后空 → ValidationError |
| N-2 | batch 不存在 | `run_batch("missing")` / `get_batch("missing")` | NotFoundError |
| N-3 | 清单含已删资产 | run_batch 执行前删除资产 | 跳过该资产（无 run）+ 其余正常（A-7 同源细化） |
| N-4 | 资产副本文件丢失 | 副本路径不存在即 ingest 打开失败 | 该资产 error + 批次 partial（单点失败不阻断） |
| N-5 | manifest 损坏（计数读失败） | fake run 无 manifest/stages | 计数按 0 累加 + 不中断批次（日志告警） |
| N-6 | 迁移中断重跑 | 删 schema_migrations v5 记录后 initialize | 幂等重跑不重复加列 |
| N-7 | 降级 run 的 AI 计数 | 降级 run（无 ai_analysis 阶段） | 不累加 _ai_used（防降级 run 消耗预算假象） |

## 5. 回退方案

- 任一验收点失败：修复后复验；设计缺陷（D1-D7 层面）上升评审第 2 轮讨论。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审 10 项意见第 1 轮全部采纳（含高危 R-1 同步 ingest 阻塞事件循环 → to_thread；R-2 伪码漏 create_run → INSERT 三列一次落库）。实施中测试暴露并修复 3 处真实缺陷：① 降级判定时机在 ingest 前，并发 ingest 使判定与 scan 完成顺序失序 → **信号量改为包整个资产处理**（判定移至 scan 启动前，语义与计数顺序一致）；② AI 阶段另有两个 summary 构造点（跳过/断路早退路径）缺 `requests_used` → 三处全部补齐；③ scan 外异常时 run 停留 queued → 协程内收敛为 failed（`BATCH_ASSET_PROCESSING_ERROR`）。全量 942 passed / 0 failed（+16）。

| 编号 | 结果（通过/失败/部分通过） | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | pending + 预算快照(7/99) + assets 对象数组快照 + NotFoundError/空列表 ValidationError | - |
| A-2 | 通过 | 3 资产 completed + runs 三关联列 + 资产 ready + last_run_id 更新 | - |
| A-3 | 通过 | batch partial + (completed=1, failed=2)：收敛 failed + 抛异常（run 置 BATCH_ASSET_PROCESSING_ERROR）两路径 + 其余正常 | - |
| A-4 | 通过 | 串行下 run3 降级（标记=1 + ai.enabled=False + skip_reason='batch_budget'）+ ai_skipped/by_budget=1 + 其余 AI 元数据保留 | - |
| A-5 | 通过 | 墙钟 1s + run1 耗时 1.2s → run2 降级（skip_reason='batch_wall_clock'）+ by_wall_clock=1 | - |
| A-6 | 通过 | 二次 run_batch → ConflictError(BATCH_NOT_PENDING)；missing → NotFoundError；条件 UPDATE 抢占 | - |
| A-7 | 通过 | 已删资产跳过不建 run（total_runs=1）+ 其余 completed | - |
| A-8 | 通过 | max_concurrent_runs=1 → scan 并发峰值=1（tracker 实证） | - |
| A-9 | 通过 | 篡改 runs status 后 get_batch 重算 (0, 2) | - |
| A-10 | 通过 | golden dict 断言（默认/降级/无 skip_reason 键三分支） | - |
| A-11 | 通过 | v4 库（回退记录+DROP/重建）升级 → 列在 + 既有行 '[]' + 幂等 + user_version=5；新库直建 | - |
| A-12 | 通过 | 真实 pipeline manifest ai_analysis summary 含 requests_used（test_api 集成断言）+ 三构造点补齐 | - |
| A-13 | 通过 | test_batch.py 15 项全过 | - |
| A-14 | 通过 | 全量 pytest：**942 passed / 0 failed** | - |
| A-15 | 通过 | check-all（含前端构建）+ ruff（新增/改动文件）全过 | - |
| A-16 | 通过 | test_api 既有端到端测试（upload→completed→findings）不受 routes 改造影响 | - |
| N-1 | 通过 | 空列表 ValidationError；["a","a"] 去重后 ["a"] → NotFoundError（资产不存在） | - |
| N-2 | 通过 | run_batch/get_batch missing → NotFoundError | - |
| N-3 | 通过 | 执行前删资产 → 跳过（A-7 同源） | - |
| N-4 | 通过 | converge_fail/raise 两失败路径资产 error + 批次 partial（A-3 承载） | - |
| N-5 | 通过 | `_requests_used_of` 容错（读不到按 0 + 告警，noqa 理由注释） | - |
| N-6 | 通过 | 删 v5 记录重跑幂等（A-11 断言） | - |
| N-7 | 通过 | 降级 run 不累加 _ai_used（A-4 断言 run1/2 计数、run3 降级不消耗） | - |
