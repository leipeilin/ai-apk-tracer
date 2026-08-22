# 任务评审：M2-ACCEPTANCE-CLOSURE 实施方案/验收方案

> **评审对象**：`docs/analysis/2026-08-22-m2-acceptance-closure-implementation-plan.md` 与 `docs/analysis/2026-08-22-m2-acceptance-closure-acceptance-plan.md`
> **评审日期**：2026-08-22
> **评审方式**：独立只读子 agent（deepseek-v4-flash 视角）
> **结论**：方案方向正确，但首版存在 1 个关键 + 若干中/低问题；主 agent 对 13 条意见**全部采纳**并已修订两份方案，本轮闭合无需第二轮。

---

## 1. 评审结论摘要

- 范围聚焦、复用现有工具、失败不伪造数据等方向正确。
- 关键问题：`test_no_rules_import.py` 若按“源码文本子串”断言会命中 `backend/app/analysis/sink_taxonomy.py` docstring 导致必然失败；已改为 AST import 节点扫描。
- 其余问题集中在验收可判定性与数据源准确性（unverified 预算、三本账字段、正/负样本映射、custom sink、diff 判定等）。

## 2. 问题清单与处置记录

| 编号 | 严重度 | 问题摘要 | 处置 | 修订动作 |
|---|---|---|---|---|
| R-1 | 关键 | `import rules` 文本子串测试与 sink_taxonomy docstring 冲突 | 采纳 | 2.1 改为 `ast.parse` 仅检测 import 节点，注释/字符串/docstring 豁免 |
| R-2 | 高 | unverified 不占 AI 预算验收不可判定 | 采纳 | 2.4 增加三项候选 id 集合断言；总计数仅作旁证 |
| R-3 | 中 | 三本账数据源写错 | 采纳 | 2.4 修正 explorer/ai_analysis summary 字段与分组公式 |
| R-4 | 中 | 正/负样本 ground truth 未固化 | 采纳 | 2.4/2.5 要求内嵌映射表并引用 2026-08-16 方案与 golden 判别键 |
| R-5 | 中 | 审查 4.5 custom sink 未落到验收 | 采纳 | 2.4 增加 custom sink 命中/误标采集项 |
| R-6 | 中 | 审查 4.4 提交顺序/最终基线未落文档 | 采纳 | 2.5 明确 acceptance-runs 必含 HEAD/提交顺序/测试数快照 |
| R-7 | 中 | 默认配置 diff 缺少 AI 环境前提 | 采纳 | 2.2 增加同模型/同 key 前提 |
| R-8 | 低 | run 创建未写明 authorized=true | 采纳 | 2.2/2.3 明确 `authorized=true` |
| R-9 | 低 | call_tree 性能判定标准不一致 | 采纳 | 2.4 明确 p50 >2s 视为未达标 |
| R-10 | 低 | verify trace 无法证明盲验剥离 | 采纳 | 2.4 限定 trace 抽查为元数据，盲验以单测为准 |
| R-11 | 低 | read_requests 计算口径未定义 | 采纳 | 2.4 写明单入口 read_requests = 各 round `len(requests_executed)` 之和 |
| R-12 | 低 | 默认配置 diff 非空判定未闭环 | 采纳 | 2.2 明确 dump 复核一致通过、实质差异不通过 |
| R-13 | 中 | 预算跑满“部分链+缺口清单”未验收 | 采纳 | 2.4 增加 `terminated_by=budget` entry rounds 非空且 run completed 验收 |

## 3. 认可项

- 不修改探索轨/核验轨核心实现、不默认开启开关、不做 M3/M4。
- 复用 `scripts/baseline-manifest.py` 与 M1 基线。
- 既有单测覆盖伪造 method_id、deep_dive 不改链、A-6、verify 降级。
- 双 APK 串行、失败不伪造、文档闭环方向正确。

## 4. 边界检查表

| 检查项 | 状态 |
|---|---|
| 预算耗尽 | 已补充 R-13 |
| 失败降级 | ✅ |
| 异常输入 | 已通过 R-1/R-2 修正 |
| 回归 | 已通过 R-7/R-12 修正 |
| 回滚 | ✅ |
| 并发 | 串行执行，不涉及 |
| 验收可判定性 | 已通过 R-2/R-3/R-4/R-9/R-10/R-11 修正 |
| 审查 4.4/4.5 闭环 | 已通过 R-5/R-6 修正 |
