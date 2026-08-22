# 任务验收方案：M2 审查意见闭合（双 APK 探索轨验收 + 基线/测试/文档补齐）

> **任务编号**：M2-ACCEPTANCE-CLOSURE
> **日期**：2026-08-22
> **依据**：`docs/analysis/2026-08-22-m2-acceptance-closure-implementation-plan.md`。

---

## 1. 验收点清单

### 1.1 源码扫描测试

| 验收点 | 方式 | 预期结果 |
|---|---|---|
| `test_no_rules_import.py` 存在并通过 | `backend/.venv/bin/python -m pytest backend/tests/test_no_rules_import.py -q` | passed |
| 测试使用 AST 检测 import 节点 | 代码审查 | 不将注释/字符串/docstring 中的“import rules”字样判违规 |
| 全量测试不回归 | `backend/.venv/bin/python -m pytest -q` | 1147 + 新增 ≥1 全部通过，0 failed |

### 1.2 默认配置基线 diff

| 验收点 | 方式 | 预期结果 |
|---|---|---|
| AI 环境前提 | 检查 run 配置与日志 | 与 M1 基线同模型、同 API key 可用环境；若不可用则单独标注 |
| health 默认 run 完成 | 检查 run status | `completed` |
| shop 默认 run 完成 | 检查 run status | `completed` |
| health 基线 diff | `baseline-manifest.py` 生成后与 m1-health-baseline.json 对比 | 文件集合一致、逐文件 sha256 一致、findings_count 一致、聚合哈希一致 |
| shop 基线 diff | 同上 | 一致 |
| diff 非空判定 | 按实施计划 2.2 第 7 条 | sqlite 字节噪声经 dump 复核一致 → 通过；实质差异 → 不通过并记录归因 |

### 1.3 探索轨 + 核验轨双 APK

| 验收点 | 方式 | 预期结果 |
|---|---|---|
| 所有 run 创建带 `authorized=true` | 检查创建请求/记录 | 是 |
| health 探索 run 完成 | run status | `completed`（或记录失败原因） |
| shop 探索 run 完成 | run status | `completed`（或记录失败原因） |
| 覆盖 ≥5 validated/partially | 解析 `explorer/candidates.json` | health/shop 各 ≥5 |
| 已知 8 项覆盖 ≥6 且 ≥4 validated | 按 `m2-acceptance-runs.md` 内嵌映射表比对 | ≥6 覆盖，≥4 validated |
| 负样本不进候选池 | 按映射表检查候选池 | V-04/V-05/V-06、`sp-control-flow-cooccurrence-refuted`、`ownsystem-unselected-implementation` 不出现 |
| 未通过校验 0 条进 finding | unverified 与 findings 交集 | 空 |
| unverified 不占 AI 预算 | 按实施计划 2.4 三项断言 | 全部通过 |
| 三本账可导出 | `explorer` stage summary + `ai_analysis` summary | explorer/deep_dive/verify/ai_stage 分列，公式可复算 |
| call_tree 单入口性能 | 计时脚本 | health p50 ≤2s；p50 >2s 记为未达标并记录优化方向 |
| 默认配置 diff 为空 | 1.2 结果 | 通过 |
| 检索循环预算 | observations.json 按“rounds 的 requests_executed 之和”统计 | rounds ≤4、read_requests ≤20 |
| 预算跑满不报错 | observations.json `terminated_by=budget` entries | rounds 非空、run completed、记录缺口原因 |
| 伪造 method_id 判 unverified | 既有单测 | `test_explorer_validation*` passed |
| deep_dive 不改写链 | 既有单测 | passed |
| backend 无 import rules | 新增测试 | passed |
| verify 盲验/命题/循环语义 | 既有单测（`test_verify_agent.py` 用例）+ run trace 元数据抽查 | 单测 passed；trace 抽查仅记录轮数/terminated_by/input_hash/undecided |
| verify 降级回退 | 既有单测 + run 记录 | 单测 passed；真实 run 未触发则记录“未触发” |
| 证据引用适配 | 既有 DecisionEngine 端到端测试 | A-6 passed |
| custom sink 命中/误标 | 解析 `explorer/candidates.json` | 记录 custom_sink_proposal 数、深挖跳过、人工确认、误标数量 |
| `ai_likely_supported` 占比对比 | 默认 run vs 探索+verify run | 记录数值，预期下降但不归零 |

### 1.4 文档更新

| 验收点 | 方式 | 预期结果 |
|---|---|---|
| `m2-acceptance-runs.md` 创建 | 文件存在 | 包含执行时 HEAD、M2 父链提交顺序、最终测试数快照、run_id、指标、diff 结论、正/负样本映射表、未达标项说明 |
| §4.3 checkbox 更新 | 检查 implementation plan | 已执行项勾选，未达标项明确未勾选并说明 |
| 审查报告处置记录 | 检查 m2-implementation-review.md | 4.1–4.5 逐条处置 |
| T2.12 交接行更新 | 检查 t2-12 implementation plan | 明确由 M2-ACCEPTANCE-CLOSURE 执行 |

---

## 2. 回归标准

- 默认配置下探索轨/核验轨不改变确定性产物与 findings_count。
- 全部既有测试通过。
- 新功能开关保持默认关闭；验收 run 通过临时环境变量开启，不写入 `config/default.yaml`。

## 3. 边界与负例

- 若真实 AI 无 key 或 provider 不可用：run 应降级/失败并记录，不得伪造数据；默认配置 diff 不可与 M1 基线直接判一致。
- 若探索候选不足：如实记录，不勾选对应验收点。
- 若 call_tree p50 >2s：记录实测值，视为未达标并给出优化方向。
- 若 `analysis.sqlite3` 字节 diff 非空：按 dump 复核后给出结论；实质差异 → 验收不通过。
- 若 `observations.json` 中预算跑满 entry 无 rounds 或 run 异常：视为未达标。

## 4. 回退方案

- 所有新能力仍默认关闭；验收数据仅记录，不改变默认行为。
- 若验收发现实现缺陷，先记录缺陷，不强行宣称 M2 验收通过。
