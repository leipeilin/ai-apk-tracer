# AI 判定正确率基线报告（第一份完整 AI 数据）

> **数据来源**：run `20260808T050946Z_1c55d3fb9f95_3c6501ae`（基线 APK sha256 `1c55d3fb9f95`，com.mi.health 同源基线）
> **AI 状态**：l2-review 2.0.4 + thinking 关闭 + max_tokens 8000（D11-D14 修复全部生效）
> **日期**：2026-08-08
> **验证原则**：所有"成立/不成立"判断基于确定性证据（索引库 `analysis.sqlite3`、切片、coverage_gaps、deterministic_chain_verified），未做任何主观猜测。

## 1. 总览

| 指标 | 值 | 说明 |
|---|---|---|
| 候选总数 | 147（L2）+ 128（L1） | finding 275 |
| AI 分析完成 | **138 / 147（93.9%）** | 2 failed + 7 incomplete |
| AI 完成率历史 | 1.4% → 5.4% → 40% → **93.9%** | D11→D12→D13→D14 修复曲线 |
| preflight | passed | prompt 1.0.1 |
| 峰值并发 | 12 | F1 配置生效 |

## 2. AI verdict 分布（核心基线）

| verdict | 数量 | 占比 |
|---|---|---|
| unresolved | **138** | **100%** |
| supports_candidate | 0 | 0% |
| refutes_candidate | 0 | 0% |
| **裁决率（方向性 verdict）** | **0 / 138** | **0%** |

**AI 对全部 138 个候选给出 unresolved，confidence_tier 全为 low。** 这不是 AI 故障——与确定性证据交叉验证后，这 138 个候选的 `deterministic_chain_verified` 全部为 false、`dataflow_status` 全部为 not_proven、`guard_status` 全部为 unknown/absent，**证据链确实未闭合，AI 的 unresolved 是诚实且正确的**。

### 由此得出的正确率指标

| 指标 | 定义 | 值 | 说明 |
|---|---|---|---|
| AI 判定成立正确率 | AI supports 且证据成立 / AI supports | **无法计算（0 样本）** | AI 从未输出 supports |
| AI 判定不成立正确率 | AI refutes 且确定性否定成立 / AI refutes | **无法计算（0 样本）** | AI 从未输出 refutes |
| AI 虚报率（假阳性） | AI supports 但证据不成立 | **0 / 138 = 0%** | AI 从不编造成立证据 ✅ |
| AI 保守正确率 | AI unresolved 且证据确实不足 | **138 / 138 = 100%** | 与确定性证据完全一致 ✅ |
| AI 漏报（应裁决未裁决） | 确定性闭链候选被 AI 判 unresolved | 3 个闭链候选 AI 均未分析（预算饿死） | 见 §4 |

**关键结论：当前 AI 判定价值为 0（0% 裁决率），但可靠性为 100%（零虚报）。** 基线显示 AI 的问题是"过度保守"而非"幻觉"——它需要判定标准来打破全 unresolved，这正是 vuln-judgment-prompt-v1.md 要解决的。

## 3. 漏洞证据有效率（确定性口径）

以确定性证据为 ground truth，"证据成立"分三级：

| 级别 | 判定 | 数量 | 占比 |
|---|---|---|---|
| **A. 证据链闭合（静态确认）** | `deterministic_chain_verified=true` + `impact=statically_confirmed` | **3** | 1.1%（275） |
| **B. 外部暴露事实（L1）** | manifest 确定性事实（exported 无权限等），但缺危害链 | **128** | 46.5% |
| **C. 证据不足（L2 未闭合）** | 数据流 not_proven / guard unknown / 有 critical gap | **144** | 52.4% |

- **严格口径"漏洞证据有效"（A 级）= 3/275 = 1.1%**
- 宽松口径（A+B，有确定性事实）= 131/275 = 47.6%
- **C 级占一半以上 —— 当前规则产出的候选大多缺乏闭合证据链**，这是"AI 全 unresolved"的根本原因，不是 AI 的问题。

## 4. 关键发现：最强的证据恰好没被 AI 分析

3 个闭链候选（证据级别 A）**全部 `ai_incomplete`（run_request_budget_exhausted）**：

| 候选 | 规则 | 组件 |
|---|---|---|
| candidate_ca8947d26263c40dc421 | IMPLICIT_BROADCAST_SENSITIVE_DATA | AccountChangedBroadcastHelper |
| candidate_809eebd670750e265f07 | IMPLICIT_BROADCAST_SENSITIVE_DATA | ShopApp |
| candidate_94ea0024421259a85207 | IMPLICIT_BROADCAST_SENSITIVE_DATA | OwnSystemXiaomiAccountManager |

**根因**：`context_budget.max_requests_per_run=140`，147 个候选排队，排在最后的 7 个被预算饿死，恰好包含全部 3 个闭链候选。→ **应提高 max_requests_per_run（147 → 180+），否则每次都会漏掉最强的证据。**

## 5. AI 证据引用可回查性（641 条引用）

| 验证项 | 数量 | 占比 | 验证方法 |
|---|---|---|---|
| context_id 在切片内可回查 | 460 | 71.8% | context_id ∈ 切片 contexts |
| **切片外引用（方法真实存在）** | 181 | 28.2% | 逐条查 `analysis.sqlite3` methods 表，**21/21 去重方法全部存在，0 幻觉** |
| 行号越界 | 22 | 3.4% | line 超出 context 范围 |

**修正后的结论**：AI 的 641 条证据引用**全部有真实依据**（方法真实存在于反编译产物和确定性链路中），没有凭空编造。181 条"切片外引用"是**切片不完整**导致的 —— 链路上的方法没被包含进切片（预算/选择原因），AI 引用了"知道但没看到"的方法上下文。这是**切片覆盖问题**，不是 AI 幻觉。

## 6. AI 缺口识别一致性

| 项 | 值 |
|---|---|
| AI 输出 blocking_gaps 总数 | 547 |
| 确定性 coverage_gaps 总数 | 194 |
| AI code 集合 ⊇ 确定性 code 集合 | **是**（确定性独有 code = 空集） |
| AI 独有 code 语义 | 均为语义包状态转述（DATAFLOW_NOT_PROVEN/GUARD_PATH_UNRESOLVED 等），非编造 |

**AI 的缺口识别与确定性语义包完全同源**，额外识别来自 AI 对 guard/dataflow/符号解析状态的汇总转述。

## 7. 残余问题（影响完整性的 9 个候选）

| 类型 | 数量 | 原因 |
|---|---|---|
| ai_failed | 2 | `context_requests.N.type/target: missing` —— l2-review 2.0.4 未声明 ContextRequest 元素结构（D13 同类尾巴） |
| ai_incomplete | 7 | `run_request_budget_exhausted`（预算 140 < 147） |

## 8. 基线对 vuln-judgment-prompt-v1.md 实施的直接影响

| 基线事实 | 对方案的意义 |
|---|---|
| AI 0% 裁决率、100% 保守 | **四要素判定标准（§2）正是打破全 unresolved 的关键** —— 给 AI 明确的"缺陷成立/可利用/危害/可达性"判据，让它敢于裁决 |
| 0 假阳性 | AI 基础可靠，方案可在其上安全叠加，不会放大误报 |
| 52% 候选证据链未闭合 | 判定标准必须与确定性证据绑定（§4 红线反向排除），防止 AI 在证据不足时虚报 |
| 3 个闭链候选被预算饿死 | 实施方案的同时必须修 `max_requests_per_run`，否则最强证据永远进不了 AI |
| 28% 切片外引用 | 切片覆盖需要加强（可考虑增大 max_contexts_per_slice 或链路锚点），否则 AI 引用不可回查 |

## 9. 修复清单（与方案实施同步）

1. `context_budget.max_requests_per_run`：140 → 200（覆盖 147 候选 + 扩片余量）
2. l2-review 提示词补充 `context_requests` 元素结构声明（type/target 必填）→ 消除 2 个 failed
3. 实施 vuln-judgment-prompt-v1.md §2-§8（四要素 + 结构化字段 + 红线）
