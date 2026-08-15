# §5 召回守门历史回归报告（2026-08-15，v2 多 APK 扩充）

> **目的**：按方案 §5 对 run 产物离线重放 P0-2 分级逻辑，统计「被降级候选数」与「其中曾被人工判为真漏洞的条数」。**硬门槛：被降级集合中真漏洞数 = 0** 方可将 `demote_unproven_flow` 默认值翻为 true。

## 数据源与限制

| 数据源 | 数量 | 可用性 |
|---|---|---|
| 基线 run rule-results（8-09） | 274 候选（21 规则） | ✅ 完整候选字段 |
| 基线 run findings | 260（funnel 聚合后） | ✅ 含 evidence_decision / ai_analysis |
| **com.xiaomi.shop 复扫（8-15）** | 193 候选 | ✅ P0-1 修复后产物（control_to_sink 141→33） |
| **com.mi.health（8-15）** | 480 候选 | ✅ 风格差异大样本（receiver_exposure 282） |
| ai-cache entries | 822 | ⚠️ **仅 AI 输出无候选输入**（descriptor 只存哈希），无法重放 demotion；且全部为 8-09 前产物（prompt ≤ 3.0.4），**不含 8-14 后样本**——方案声称"含 8-14 之后产物"与实际不符 |

## 重放结果（用当前 `unproven_flow_demotion_reason` 逻辑）

**基线 274 候选**：被降级 **1 条**（`legacy_fallback`，WbShareResultActivity）——v04 §1.6 实证误报。

**com.xiaomi.shop 复扫 193 候选**：被降级 **5 条**——4 条 `bulk_statistics_consumer`（StatService2:195/247/267/285，**consumer_semantics 常量解析补强生效**：修订前这 4 条因常量引用不命中 statistics，现在正确降级）+ 1 条 `legacy_fallback`（WbShareResultActivity）。

**com.mi.health 480 候选**：被降级 **1 条**（`legacy_fallback`，RouterActivity）——**AI 判定 `ai_likely_supported`（flaw=True）**，疑似真漏洞。

## ⚠️ 硬门槛未通过：mi.health RouterActivity 疑似真漏洞被降级吞掉

com.mi.health 的被降级候选 `com.p038mi.health.router.framework.RouterActivity`（ACTIVITY_INTENT_TO_SENSITIVE_SINK，`inferred_source_to_sink`）：

- **AI 判定**：`evidence_decision=ai_likely_supported`、`flaw_holds=True`、exploitability 全绿（entry_reachable / propagation_proven / sink_effective / authorization_absent）
- **AI 依据**：onCreate 解析外部 Intent → `parseFitnessIntent` → `startActivity(fitnessIntent)` 启动任意组件，组件 exported 无权限
- **现状**：`inferred_source_to_sink` 的降级理由 `legacy_fallback`（轻量正则回退）**不应覆盖 AI 已判定的真实调用点链**——`legacy_fallback` 表示"语义链未闭合"，但该候选的 AI 分析已证明链闭合（sink 有真实调用点、传播同对象）

**这意味着**：若将 `demote_unproven_flow` 翻为 true，该疑似真漏洞会被降为 signal 不送 AI——**违反硬门槛"被降级集合中真漏洞数 = 0"**。

## 结论（v2 更新）

1. **硬门槛未通过**：mi.health RouterActivity 证明 `legacy_fallback` 降级存在**吞真漏洞风险**——基线样本恰好是误报（WbShare），换样本即暴露。
2. **P0-2 默认值维持 false 是强制结论**：不仅有 scope_unresolved 分支验证问题，现在有**实证的真漏洞吞没案例**。
3. **修复方向（`legacy_fallback` 降级收紧）**：
   - `inferred_source_to_sink` 的 `LEGACY_FLOW_FALLBACK` 不应无条件降级——AI 已判定 flaw_holds=True 且传播闭合的候选必须保留送 AI；
   - 建议：`legacy_fallback` 降级仅适用于**无 AI 判定依据**的候选（或直接取消该分支降级，样本量本就小——基线 1 条 / shop 复扫 1 条 / mi.health 1 条，降级收益 < 漏报风险）。
4. **数据基础缺口**：ai-cache 不含候选输入、prompt ≤3.0.4，无法作为口径 A 重放源——已标注。
