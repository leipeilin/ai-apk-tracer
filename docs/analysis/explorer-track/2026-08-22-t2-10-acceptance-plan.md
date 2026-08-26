# 任务验收方案：T2.10 探索产物注册与人工队列审计视图

> **任务编号**：T2.10
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t2-10-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest（队列构建纯函数 + API 端点）+ 前端 tsc/build/eslint 门禁 + 全量回归

---

## 1. 验收点清单

| 编号 | 验收项 | 验收方式与步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 队列投影形状 | `build_explorer_queue`（三档混合候选） | 条目含 candidate_id/component/chain/validation/deep_dive/confidence/sort_keys；**不含** hops 全文与 rounds 审计（脱全量） |
| A-2 | 排序规则 | 构造：A（deep_dive 证据 3）/B（证据 1）/C（无深挖 confidence=high）/D（无深挖 low） | 序：A > B > C > D（deep_dive 证据优先 → 置信度 → 跳完整度） |
| A-3 | 跳完整度次级排序 | 同证据数同置信度，verified 2/2 vs 1/2 | 2/2 在前 |
| A-4 | deep_dive 投影 | 含 deep_dive 的候选 | evidence_count/confirmed_fact_count/remaining_gap_count/consistency_downgraded/requests_used；无 deep_dive → None |
| A-5 | 计数汇总 | 三档混合 | counts 含 validated/partially_validated/unverified/pending/total/deep_dive_completed |
| A-6 | 空输入容错 | `build_explorer_queue([])` | {queue: [], counts: 全零}；不抛 |
| A-7 | API 端点形状 | API 级 run（无探索产物）→ GET | 200 + {candidates: [], counts 全零} |
| A-8 | API 端点数据 | 预置 candidates.json（含三档+deep_dive）→ GET | 200 + 排序后队列 + counts；validated 条目在 counts（列表主体 partial/unverified） |
| A-9 | API 404 | 不存在的 run_id | 404 |
| A-10 | observations artifact 注册 | explorer.enabled run 后 manifest artifacts | 含 explorer_candidates（既有）+ explorer_observations（path=explorer/observations.json） |
| A-11 | 前端类型与构建 | `npm run build`（tsc -b && vite build）+ eslint | 零错误 |
| A-12 | 面板挂载 | RunDetailPage 渲染（构建级验证——无单测基建） | ExplorerQueuePanel import 与挂载（tsc 门禁保证契约） |

## 2. 回归标准

- [ ] `cd backend && .venv/bin/python -m pytest` 全量通过（基线 1138 passed / 0 failed）；
- [ ] `scripts/check-all.sh` 通过（backend + frontend tsc/eslint/build + 规则契约）；
- [ ] 改动文件 ruff 零错误。

## 3. 边界与负例

| 编号 | 场景 | 预期 |
|---|---|---|
| N-1 | candidates.json 损坏 | API 空态 200（不抛） |
| N-2 | validation/deep_dive 缺失的历史产物 | 条目照常（deep_dive=None；validation 缺字段用 None 兜底） |
| N-3 | 置信度未知值 | confidence_rank=0（排最后） |
| N-4 | run 目录存在但 explorer/ 目录缺失 | 空态 200 |

## 5. 回退方案

- 端点/面板独立新增——前端面板移除挂载即回退；backend 端点独立可弃用（无既有消费者）。

## 6. 验收记录（实施后填写）

> **验收日期**：2026-08-22。**结果：全部通过**。全量回归 **1147 passed / 0 failed**（基线 1138 + 新增 9）；`scripts/check-all.sh` 通过（backend pytest + 规则契约 30 + 前端 `tsc -b && vite build`——项目无 eslint 基建，门禁如实化——评审 R-3）；改动文件 ruff 零错误（含顺带修复 routes.py 既有 2 项：B008 File 单例/BLE001 异常具体化）。

| 编号 | 结果 | 实测说明（测试函数） |
|---|---|---|
| A-1 | 通过 | `test_queue_entry_shape`：七字段投影 + 脱 hops/rounds 全文 + hop_count 派生（R-7） |
| A-2 | 通过 | `test_queue_sorting_confidence_primary`（评审 R-1 重写）：置信度主键 → deep_dive 证据次键（high 无深挖 > mid3 证据 > mid1 证据 > low——unverified 不沉底） |
| A-3 | 通过 | `test_queue_sorting_hop_ratio`：同置信同证据 → 跳完整度 |
| A-4 | 通过 | `test_queue_entry_shape`：deep_dive 投影（evidence_count/confirmed_fact_count；无 consistency_downgraded——R-2 修正） |
| A-5 | 通过 | `test_queue_counts_and_validated_excluded`：八键计数 + validated 不进列表 + queue_length（R-9） |
| A-6 | 通过 | `test_queue_empty_and_malformed`：空/非 mapping/未知置信度容错 |
| A-7 | 通过 | `test_endpoint_empty_state`：无产物 → 200 空态 |
| A-8 | 通过 | `test_endpoint_returns_sorted_queue`：预置三档 → 排序 entries + counts（键名 entries 统一——R-6） |
| A-9 | 通过 | `test_endpoint_404` |
| A-10 | 通过 | `test_explorer.py` 集成测试断言：artifacts 含 explorer_candidates + explorer_observations |
| A-11 | 通过 | `npm run build`（tsc -b && vite build）零错误 |
| A-12 | 通过 | ExplorerQueuePanel 挂载 RunDetailPage（tsc 契约门禁）+ usePolling 同 findings 活跃判定（R-4） |
| N-1 | 通过 | `test_endpoint_corrupted_file_empty_state` |
| N-2 | 通过 | `test_queue_empty_and_malformed`（缺 validation → pending 兜底） |
| N-3 | 通过 | 同上（confidence_rank=0） |
| N-4 | 通过 | `test_endpoint_empty_state`（explorer/ 目录缺失） |
