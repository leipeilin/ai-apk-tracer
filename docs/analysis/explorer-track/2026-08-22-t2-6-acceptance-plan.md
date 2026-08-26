# 任务验收方案：T2.6（探索候选三档校验）

> **任务编号**：T2.6
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t2-6-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest（真实 index）+ 集成 + 全量回归

---

## 1. 验收范围

- `validate_explorer_candidates`（跳回查/guard/三档）+ explorer 阶段集成 + 测试。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式 | 预期结果 |
|---|---|---|---|
| A-1 | validated（全跳通过） | `test_validated_full_hops` | status + verified_hop_count + 空 failed |
| A-2 | partially_validated | `test_partially_validated` | 失败跳索引记录 |
| A-3 | unverified | `test_unverified` | 零跳通过 |
| A-4 | guard 阻断 | `test_blocked_by_guard` | release→True / debug→False |
| A-5 | 三档计数 | `test_validation_counts` | 计数字典正确 |
| A-6 | 填充后 schema 合法 | `test_schema_validation_populated` | explorer_candidate schema 通过 |
| A-7 | 阶段集成 | `test_orchestrator_stage_summary` | summary 含三档计数 + candidates.json 含 validation |
| A-8 | 单测通过 | `pytest tests/test_explorer_validation.py -q` | 全部通过 |
| A-9 | 全量回归 | `pytest -q` | 1020+ 全部通过 |
| A-10 | 统一校验 | check-all + ruff | 通过 |

## 3. 边界与负例

| 编号 | 场景 | 预期 |
|---|---|---|
| N-1 | hops 结构异常（缺字段） | 该候选 unverified + notes 异常摘要（不抛） |
| N-2 | 空 candidates | 返回零计数（不抛） |
| N-3 | call_site_line 与表行不匹配 | 跳失败（保守） |
| N-4 | manifest_facts 缺 debuggable | guard 判定按 release 保守（verify_candidate_guards 既有语义） |

## 4. 验收记录（实施后填写）

| 编号 | 结果 | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | | | |
| A-2 | | | |
| A-3 | | | |
| A-4 | | | |
| A-5 | | | |
| A-6 | | | |
| A-7 | | | |
| A-8 | | | |
| A-9 | | | |
| A-10 | | | |
| N-1 | | | |
| N-2 | | | |
| N-3 | | | |
| N-4 | | | |
