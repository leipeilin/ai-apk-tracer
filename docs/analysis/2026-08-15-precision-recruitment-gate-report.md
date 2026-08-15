# §5 召回守门历史回归报告（2026-08-15）

> **目的**：按方案 §5 对现有 run 产物离线重放 P0-2 分级逻辑，统计「被降级候选数」与「其中曾被人工判为真漏洞的条数」。**硬门槛：被降级集合中真漏洞数 = 0** 方可将 `demote_unproven_flow` 默认值翻为 true。

## 数据源与限制

| 数据源 | 数量 | 可用性 |
|---|---|---|
| 基线 run rule-results | 274 候选（21 规则） | ✅ 完整候选字段 |
| 基线 run findings | 260（funnel 聚合后） | ✅ 含 evidence_decision / ai_analysis |
| ai-cache entries | 822 | ⚠️ **仅 AI 输出无候选输入**（descriptor 只存哈希），无法重放 demotion；且全部为 8-09 前产物（prompt ≤ 3.0.4），**不含 8-14 后样本**——方案声称"含 8-14 之后产物"与实际不符 |
| 多 APK | 0 | ❌ 本地仅 1 个 APK，多 APK 验证无法执行（需用户提供 ≥2 个其他应用） |

## 重放结果（用当前 `unproven_flow_demotion_reason` 逻辑）

**rule-results 274 候选重放**：被降级 **1 条**（`legacy_fallback`，inferred_source_to_sink，ACTIVITY_INTENT_TO_SENSITIVE_SINK）。
**findings 260 重放**：被降级 **1 条**（同上，WbShareResultActivity）。

**降级集合真漏洞核对**：唯一被降级的 WbShareResultActivity 在 v04 §1.6 已人工动态验证为**误报**（fixed_local_target 闭环，目标固定本包 SDK 类）。→ **被降级集合中真漏洞数 = 0 ✅（硬门槛通过）**

## 关键发现：scope_unresolved 分支无法在历史产物上验证

基线 run 产物（8-09）是 P0-1 作用域化**之前**生成的：141 条 control_to_sink 的 gap codes 全是
`LINEAR_IR_PATH_SENSITIVITY_LIMITATION` / `SYMBOL_TARGET_AMBIGUOUS` / `GUARD_PATH_UNRESOLVED`
等旧 code，**没有 `CONTROL_SCOPE_UNRESOLVED`**（P0-1 新增）。因此重放时 `scope_unresolved`
分支 0 命中——**这不是逻辑问题，而是数据基础问题**：`scope_unresolved` 的降级效果必须
在 P0-1 修复后的新产物上验证（见 §8 口径 B 复算）。

## 结论

1. **硬门槛（被降级集合真漏洞=0）在现有产物上通过**——降级逻辑本身无真漏洞风险。
2. **P0-2 默认值维持 false 的正确性**：`scope_unresolved` 分支验证缺失（需 P0-1 后重跑）+ 多 APK 验证缺失，按方案 §5 守门流程**不得翻默认值**。
3. **数据基础缺口**：ai-cache 不含候选输入、多 APK 无样本——方案 §5 的补充样本源描述与实际不符，需在验收报告中标注。
