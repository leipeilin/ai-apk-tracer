# 任务验收记录：M4-T4.2（批量评估——探索轨指标 + 三本账 + wall-time）

> **任务编号**：M4-T4.2
> **依据**：`2026-08-23-m4-t4-2-implementation-plan.md`（含评审 R-1~R-5 修订）
> **流程**：六阶段完整执行

## 验收结果

| 编号 | 结果 | 实测 |
|---|---|---|
| A-1/A-2 | 通过 | hit/conditional 分离（conditional 经 matches 直调——R-1）；hit_rate 分母仅 hit case |
| A-3/N-1 | 通过 | candidates.json 缺失 → proposals_total=0；无 hit case → rate None |
| A-4/A-5 | 通过 | 三本账五值提取（字段名对齐真实 manifest：explorer/ai_analysis/aggregation 阶段——R-5）+ wall_seconds；缺阶段/坏时间容错 |
| A-6 | 通过 | run-case 加权聚合（总命中/总 run×hit-case——每 run 独立评估同一 golden 的平均语义）+ 总三本账 + unaggregated_runs 剔除（R-4） |
| A-7/N-4 | 通过 | CLI `--runs` 模式（真实 golden v3 manifest 加载）+ mutually exclusive group（--results/--runs 二选一，同给/均不给退出码 2——R-2） |
| A-8 | 通过 | 全量 **1224 passed / 0 failed**（+8）；离线模式不变 |
| **A-9** | **通过（如实输出）** | **真实 shop run（dc24a077）冒烟**：`explorer_hit_rate=0.2`（1/5 hit case 命中——真实数据，非预设 0——R-3 修正口径）、conditional 0/3、proposals=50、三本账 {explorer 424 / deep_dive 0 / verify 29 / ai_stage 62 / total 486}、wall 1954.2s——**与 M2 验收记录 §2.2 的 manifest 数字逐项一致**（评估口径正确性的交叉验证） |

## 实施勘误

- fake manifest 被 `load_golden_dataset` 完整校验（ai_responses 的 schema_sha256 须真实）——CLI/聚合测试改用真实 DEFAULT_MANIFEST；
- 聚合的 hit_cases_total 为 run-case 累加语义（2 run × 5 case = 10）——文档化于测试注释。

## 回归

全量 1224 passed / 0 failed；ruff 零错误。
