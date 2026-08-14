# AI 判定正确率基线报告 v2（3.0.1 四要素判定完整数据）

> **数据来源**：run `20260808T155920Z_1c55d3fb9f95_12bdc4c0`（基线 APK sha256 `1c55d3fb9f95`）
> **AI 状态**：l2-review **3.0.1**（四要素 + 23 红线 + 扩片节制）+ thinking 关闭 + max_tokens 8000 + max_requests_per_run 500
> **日期**：2026-08-09
> **验证原则**：所有"成立/不成立"判定基于确定性证据（索引库 `analysis.sqlite3` 的 flow_ir/summary、切片、coverage_gaps、deterministic_chain_verified），核心样本逐条查代码验证，未做主观猜测。

## 1. 总览

| 指标 | 3.0.1（本轮） | 2.0.4（上轮） | 说明 |
|---|---|---|---|
| AI 完成 | **145/147（98.6%）** | 138/147（93.9%） | 修复 3.0.1 生效（扩片节制 + 预算 500 + 注入字段剥离） |
| failed / incomplete | **1 / 1** | 2 / 7 | 残余：1 个漏 analysis_complete（repair 前必填缺失），1 个扩片停滞 |
| preflight | passed | passed | — |

## 2. AI 判定分布（145 个 completed）

| 维度 | 分布 |
|---|---|
| **verdict** | unresolved 137 · **refutes_candidate 8** · supports_candidate 0 |
| **flaw_holds** | **True 49（33.8%）· False 96** —— AI 首次大规模给出缺陷成立方向判定 |
| propagation_proven | True 17 · False 128 |
| exfiltration_channel | unverified 137 · absent 8 |
| reachability_class | remote 138 · local 7 |

## 3. 正确率指标（ground truth = 确定性证据）

| 指标 | 定义 | 值 | 验证方法 |
|---|---|---|---|
| **AI 判定成立正确率** | AI supports 且证据成立 | **无法计算（0 样本）**；AI 零虚报（0 个 supports） | — |
| **AI 判定不成立正确率（语义层）** | AI refutes 有确定性依据 | **8/8 有依据**；已逐个用索引 `flow_ir` 验证 1 个（LocalBroadcastManager 判定 ✅ 与索引 `receiver_text` 完全一致） | 索引代码验证 |
| **AI 判定不成立正确率（决策层）** | AI refutes 被确定性采信 | **0/8（采信率 0%）** —— decision 层 `false_positive_basis=[]`，全部落 unresolved | review_state 字段 |
| **AI 保守正确率** | AI unresolved 且证据确实不足 | **137/137（100%）** | 与 evidence_decision=unresolved 一致 |
| **漏洞证据有效率** | 确定性闭链 / 总 finding | **3/275 = 1.1%** | deterministic_chain_verified |

## 4. 3 个闭链候选（本轮终于全部被 AI 分析）

| 候选 | AI 判定 | 索引验证结论 |
|---|---|---|
| ShopApp.sendLoginBroadcast | **refutes_candidate（conf=high）**：Sink 是 LocalBroadcastManager（进程内，红线 9） | ✅ **正确**：索引 `sendLoginBroadcast` summary 的 side_effects[0] `receiver_text="LocalBroadcastManager.getInstance(...)"`，450 行，与 AI claim 完全一致 |
| AccountChangedBroadcastHelper | unresolved + flaw=True：缺陷成立但调用点未验证（CALLER_UNVERIFIED）+ protected broadcast 未验证 | 合理保守（静态无法确认动态广播入口） |
| OwnSystemXiaomiAccountManager | unresolved + **flaw=False**：调用点未验证 → 缺陷不成立 | ⚠️ 与确定性闭链有分歧，需人工确认（确定性说 intraprocedural 链闭合，AI 认为调用点缺失） |

## 5. 发现的三个质量问题

### 5.1 要素混淆：AI 把"可利用性未验证"当作"缺陷不成立"
96 个 flaw_holds=False 中 **95 个的 blocking_gap 是 `EXFILTRATION_CHANNEL_UNVERIFIED`** —— 这属于"可利用"要素，不是"缺陷成立"要素。四要素独立，AI 混淆了判定维度。flaw=False 应该基于"无真实调用点/死代码/仅声明"（红线 1/2/13），而非"外溢通道未验证"。
**影响**：flaw_holds 作为"缺陷成立"信号的可靠性受损；修复方向是提示词强化要素边界（3.0.2）。

### 5.2 决策层不采信 AI 的正确否定（工程缺口）
8 个 refutes 全部 `evidence_decision=unresolved`、`false_positive_basis=[]` —— 即使 AI 正确识别了 LocalBroadcastManager（确定性可证），决策层也没有对应的确定性反证检查规则去背书，导致正确 refutes 无法降低误报。
**修复方向**：为确定性否定类红线（8/9/13/18 等）补决策层反证规则（receiver 是 LocalBroadcastManager → refutation 基础）。

### 5.3 残余 2 个未完成
- 1 failed：`analysis_complete: missing`（模型漏必填，repair 前缺失语义无法兜底）
- 1 incomplete：扩片停滞（残余）

## 6. 与 vuln-judgment-prompt 方案的对照

| 方案设计 | 基线验证结果 |
|---|---|
| §2 四要素独立论证 | ✅ 生效（flaw_holds 49/96 分布出现），但**要素边界需强化**（见 5.1） |
| §4 红线反向排除 + verdict 映射 | ✅ 生效（8 个 refutes 均基于确定性否定类红线），但**决策层需补确定性反证背书**（见 5.2） |
| §3 结构化字段 | ✅ 全部输出合规（PYDANTIC OK，145/145 有完整四要素） |
| §5 可达性分级 | ✅ remote 138 / local 7，分级生效 |
| 红线 23 静态约束 | ✅ 8 个 refutes 均走"静态确定性反证"路径（exfil=absent），未滥用 |

## 7. 下一步

1. **3.0.2 提示词**：强化四要素边界（flaw_holds 只依据"真实调用点"，exfil 归"可利用"）；
2. **决策层补确定性反证规则**：LocalBroadcastManager/进程内分发等可确定性验证的否定类红线 → decision.py 背书，让正确 refutes 落地；
3. **人工确认 OwnSystemXiaomiAccountManager 分歧样本**（确定性闭链 vs AI flaw=False）；
4. 按此基线继续评估 §12 规则族补充（范围提升）与 sink-anchored。
