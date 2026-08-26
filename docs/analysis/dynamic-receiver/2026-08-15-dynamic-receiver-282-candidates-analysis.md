# 问题分析报告：mi.health 动态 Receiver 候选 282 条（DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION）

> **日期**：2026-08-15
> **样本**：com.mi.health（小米运动健康），run `20260815T125744Z_2a80fc5a8735_ef5915ff`
> **现象**：规则产出 receiver_exposure 候选 **282 条**（全 run 480 条的 59%），全部进入 AI 预算但几乎全部无法判定。
> **定性**：**不是误报爆发**，是规则保守性 + 预算选择未按可判定性优先 + 去重失效三因素叠加。
> **勘误（2026-08-15 核验，含预算机制复核）**：① 原"应用自身代码 0 条（全部为 SDK/第三方库）"断言**错误**——实测应用自身注册点 ≥190 条（67%）：`com/xiaomi/fitness/` 165 + 混淆包 `com/p038mi/` 25（fitness 是应用自身业务包，v2 实证 SportXmsService 等漏洞组件即在此包），SDK/第三方约 92 条。**据此修订建议 4（见 §4）**。② unknown 141→138。③ 复核确认 funnel/ai_required/去重以 findings 库为准：**coverage_insufficient 274 / ai_required=True 275 / representative 唯一 277**（候选侧 chain_id 唯一 282 是 rule-results 阶段计数，未过 funnel 聚合，不作为 findings 侧口径）。④ **预算机制复核（重要）**：`max_l1_candidates_per_run=20` 已生效，**实际送 AI 仅 20 条**（255 条 deferred），"275 条白烧 AI"表述失真；**真正问题转移为：20 条预算里 17 条是 gap 未解析形态、仅 3 条干净暴露面**——预算选择未优先"可判定的干净暴露面"。核心修复从"flag 分级降量"调整为"**flag 分级 + 预算按可判定性排序**"。

---

## 1. 现象（数据）

| 维度 | 数值 |
|---|---|
| 候选总数（该规则） | 282（flow_kind=receiver_exposure，evidence_level 全 L1） |
| 占全 run 候选 | 59%（480 条中 282 条） |
| 规则来源 | 100% `DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION` |
| 应用自身代码（勘误） | **≥190 条（67%）**：`com/xiaomi/fitness/` 165 + `com/p038mi/`（应用混淆包）25；SDK/第三方约 92 条（xiaomi/wearable 13、smarthome 9、onetrack 5、miot 3、android 3、learnium（RN）7、google 4、autonavi 2 等）。**原"0 条"断言错误**——fitness 是应用自身业务包（v2 报告实证：SportXmsService 等漏洞组件即在此包） |
| findings 决策 | `ai_required=True` **275** 条（复核确认，findings 库 277 条：275 True + 2 False）；`evidence_decision` **unresolved 269 / exposure_only 8** |
| funnel disposition | **274** 条 `coverage_insufficient` + exposure_only 2 + high_risk_uncertain 1（复核确认） |
| **实际送 AI（复核修正）** | **仅 20 条**——`max_l1_candidates_per_run=20` 预算已生效，255 条 `ai_budget_deferred`（`analysis_status=rule_only` 257）；**"275 条白烧 AI"表述失真**，275 是预算前的 ai_required 标记 |
| **送 AI 的 20 条形态（复核修正）** | 仅 **3 条**干净形态（无三大 gap），**17 条带 gap**（RECEIVER_FLAG_UNKNOWN/RECEIVER_TARGET_UNRESOLVED/RECEIVER_ACTION_UNRESOLVED 组合）——**预算选择未优先"最值得 AI 判的干净暴露面"**（38 条干净候选只进 3 条） |
| 去重后代表候选 | **277** 条（findings 侧 representative_candidate_id 唯一计数；候选侧 chain_id 282 唯一是 rule-results 阶段，未过 funnel 聚合） |
| 无 blocking_gaps 的"最干净"候选 | **38 条**（候选侧；此前按 findings 侧误记为 3 条，3 条是已进 AI 的干净形态） |

---

## 2. 根因链路

```
registerReceiver 注册点（全索引 49092 文件）
   │  parse_receiver_registrations 解析 285 个 reportable 注册点
   ▼
flag_status 分布：exported 43（15%）/ legacy_unspecified 101（36%）/ unknown 138（49%，勘误：原 141）
   │
   ▼ 判定逻辑：reportable = externally_reachable is not False
   │   - local_broadcast / not_exported / protected_only → False（不报告）
   │   - exported → True（报告）
   │   - **unknown / legacy_unspecified → None（≠False → 也报告）**   ← 保守扩大点
   ▼
282 条候选全部产出（85% 是"flag 无法确定"，仅 15% 确认暴露）
   │
   ▼ evidence_level 恒为 L1（规则声明 informational），但 funnel 未按 L1 降级
   ▼
275 条进 AI 预算 → AI 对"flag 未知"候选 269/277 判 unresolved（白烧预算）
   │
   ▼ 同时 P0-3 去重键含行号/注册点差异 → findings 侧 277 条各自为代表，未合并
```

**三个独立成因**：

1. **规则保守性（正确但放大）**：`externally_reachable is not False` 把 `unknown`/`legacy_unspecified` 也报告——防漏报方向正确，但 85% 候选不是"确认暴露"而是"无法排除暴露"。
2. **信息等级与 AI 预算错配（主要效率问题）**：规则声明 L1/informational（提示级），却与 L2 候选同权送 AI；AI 对 flag 未知的候选没有可判定的输入，产出 97% unresolved。
3. **P0-3 去重失效**：该规则候选的 chain_key 含注册点行号/调用差异，语义相同形态未合并，findings 侧 277 条各自占位。

---

## 3. 影响

| 影响 | 程度 |
|---|---|
| AI 预算浪费（复核修正） | 预算实际拦住 255 条（机制在工作）；**浪费点在于 20 条预算里 17 条是 gap 未解析形态**（AI 产出 unresolved 概率高），仅 3 条是干净暴露面——预算选择未按"可判定性"优先 |
| 人工复核负担 | 13 条 pending_manual + 277 条 pending_ai 队列噪声 |
| 真实暴露面淹没 | 43 条确认 exported 的候选混在 282 条里被稀释；**且 67% 注册点来自应用自身业务包（fitness 165 + p038mi 25）**——被淹没的主要是应用自身模块的动态 receiver 暴露面（fitness 是业务核心包，优先级应更高），SDK 面是次要 |
| 全 run 指标失真 | receiver_exposure 占 59%，压低整体 precision 统计 |

**不属于缺陷**：规则语义正确（exported 43 条是真实攻击面），不是误判放大，是**分级/预算机制缺失**。

---

## 4. 修复建议（按收益排序）

| # | 方案 | 改动点 | 预期收益 | 风险 |
|---|---|---|---|---|
| 1 | **flag 分级**（推荐先行） | 规则侧：`exported` → L2 送 AI；`legacy_unspecified`/`unknown` → L1 signal + 保留 `RECEIVER_FLAG_UNKNOWN`/gap | 282 → 约 43 条送 AI（-85%），AI 预算与真实暴露面对齐 | 低（unknown 形态仍保留候选+gap，人工可查） |
| 2 | **L1 informational 规则默认不进 AI** | funnel：rule 声明 informational 且 evidence_level=L1 时默认不送 AI（对齐 P0-2 signal 语义） | 全规则族受益（基线 run 该规则 76 条同形态） | 中（需确认 informational 语义是否可覆盖真实暴露） |
| 3 | **修 P0-3 去重** | 该规则 chain_key 剔除注册点行号差异，按 flag/action/注册点 owner 聚合 | findings 277 → 数十组，复核量下降 | 低 |
| 4 | **注册点业务模块分组（勘误修订）** | 原"SDK 噪声过滤"前提不成立（应用自身占 67%）——改为按注册点 owner/业务模块分组展示（fitness/p038mi/onetrack/RN/高德等），并**优先复核应用自身模块的 exported 注册点** | 降低浏览噪声，且不丢失应用自身暴露面 | 低 |

**建议组合**：先做 1（flag 分级），顺带 3（去重）；2 需评估 informational 语义后单独定；4 按修订后的"业务模块分组"实施（原"SDK 过滤"前提已证伪）。

---

## 5. 与既有工作的关系

- 同类问题前例：control_to_sink 141 条误链（P0-1/P0-2 治理）——本次是 **receiver 规则族的"低信息密度"形态**，不是误链，治理路径不同（分级而非去链）。
- legacy_fallback 教训适用：**降级必须基于可判定事实**（flag 状态），不能基于"未知"——flag 分级正是把"未知"显式化为 gap 而非送 AI 猜测。
- 与 §5 守门衔接：该规则候选不参与 P0-2 降级（flow_kind=receiver_exposure 不在 demotion 分支），需独立治理。

---

## 6. 结论

282 条是**规则保守性 + 预算选择未按可判定性优先 + 去重失效**的组合结果，不是误报缺陷。预算机制（20 条上限）已拦住 255 条，但 **20 条预算被 17 条 gap 未解析形态占据、仅 3 条干净暴露面**——核心修复是 **flag 分级 + 预算按可判定性排序**（exported/干净形态优先送 AI，unknown 形态让位），在预算内最大化真实暴露面覆盖，不损失 38 条干净候选中的任何一条。基线 run 同规则 76 条同形态，修复全局受益。
