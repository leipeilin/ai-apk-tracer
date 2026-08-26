# 任务验收方案：T2.5b（探索 Agent 驱动循环）

> **任务编号**：T2.5b
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t2-5b-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest（FakeAnalyzer + 真实 index）+ 集成 + 全量回归

---

## 1. 验收范围

- ExplorerOrchestrator（循环/转换/落盘）+ analyzer.explore_entry + 输出模型注册 + orchestrator 阶段 + 测试。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式 | 预期结果 |
|---|---|---|---|
| A-1 | loop.done 终止 | `test_explore_entry_loop_done` | 两轮循环 + 候选 + 落盘 |
| A-2 | 预算终止 | `test_explore_entry_budget_termination` | max_rounds 耗尽 + 部分链保留 |
| A-3 | 读码请求执行 | `test_read_requests_execution` | code_context 含真实方法体 |
| A-4 | 请求预算截断 | `test_requests_budget_truncation` | 8 请求限 3 执行 |
| A-5 | 候选转换 | `test_candidate_conversion` | ExplorerCandidate schema 校验通过 |
| A-6 | 候选上限 | `test_explore_all_candidate_cap` | max_candidates_per_run 生效 |
| A-7 | AI 失败短路 | `test_analyzer_failure_short_circuit` | 零候选 + 剩余入口跳过 + 不挂 |
| A-8 | 无方法入口 | `test_explore_entry_no_method` | no_method 记录零轮 |
| A-9 | 输出模型注册 | `test_ai_output_model_registered` | get_ai_output_model 解析成功 |
| A-10 | 集成 | `test_orchestrator_explorer_stage` | run completed + stage skipped（AI 不可用） |
| A-11 | 单测通过 | `pytest tests/test_explorer.py -q` | 全部通过 |
| A-12 | 全量回归 | `pytest -q` | 1010+ 全部通过 |
| A-13 | 统一校验 | check-all + ruff | 通过 |

## 3. 边界与负例

| 编号 | 场景 | 预期 |
|---|---|---|
| N-1 | 空 Observation 解析失败（analysis 不符 schema） | 该入口终止（error）+ 缺口记录 |
| N-2 | read_requests 操作非法（协议层已拦——防御） | 跳过该请求 + 记录 |
| N-3 | observations.json 预存在（重跑） | 追加（entries 数组扩展） |
| N-4 | code_context 超限 | 8KB 截断 + truncated 标注 |

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
| A-11 | | | |
| A-12 | | | |
| A-13 | | | |
| N-1 | | | |
| N-2 | | | |
| N-3 | | | |
| N-4 | | | |
