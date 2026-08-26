# 《全链路根因分析报告》核验结论

> **核验对象**：`docs/analysis/false-positive/2026-08-15-false-positive-root-cause-report.md`
> **核验日期**：2026-08-15
> **核验方法**：逐条比对 run `20260809T110600Z_1c55d3fb9f95_98fbe158` 产物（rule-results / slices / ai-cache / ai-trace / manifest）、`.ai-apk-tracer/tracer.sqlite3`、`rules/` 与 `backend/app/` 源码、`prompts/l2-review/3.0.5/system.md`、v04 人工验证报告
> **总体判定**：**根因分析（A–G）与全部代码引用准确成立；§2 实证数据有 5 处需要修正**，其中 2 处影响结论表述、3 处属精度/口径问题。报告的因果链与修复建议方向不受影响。

> ### 📌 第二轮复核（2026-08-15，针对修订版）
>
> 报告已按本文档 §4 的 6 条建议完成修正。**5 处修正全部落实，数字逐项复算准确**：
>
> | 修正项 | 复核结果 |
> |---|---|
> | sink 分布改「按方法归并」+ 补 `setLongPref` 3 条 | ✅ 合计校正为 140；⚠️ 方法起始行写作 `:76`，**实际 `:73`**（行级 80/82/84 正确） |
> | funnel 三键机制改写 | ✅ scope 9 / chain 120 / fact_hash 81 / chain_id 120 全部复算一致；建议 3 已同步改为「放宽合取粒度」 |
> | deferred 归属澄清 | ✅ 本规则 `ai_required` True 136 / False 4（4 条均 ADBDebugActivity），`ai_budget_deferred` = 0 |
> | AI 数据源边界区分 | ✅ 本 run 136 条：unresolved 135（99.3%）、flaw_holds True 130（95.6%）、stop_reason `analysis_complete` 135 + `context_expansion_stalled` 1，全部精确 |
> | v04 表述补「4 条 debuggable 阻断」 | ✅ |
>
> **新发现 1 处遗留错误（根因 A）**：报告称「v04 §1.1/§1.2 的 **116 个** PreferenceUtil 候选」。实测规则产物中 sink 落在 `PreferenceUtil.java` 的候选为 **93 条**（MainTabActivity 66 + MainActivity 27）。116 是 v04 的**人工链路归并口径**（89 + 27），其中 g_utm 组 89 条含 SplashCommonUtils 等非 PreferenceUtil sink。
>
> **进一步发现（v04 报告自身问题）**：v04 §3 汇总表各组相加为 **163 > 140**（89+27+12+30+4+1），即分组间存在**重叠计数**。116 这一数字不仅口径不同，其来源分组本身就不自洽，不能用于数量论证。
>
> **口径提示**：同一批候选存在四种计数方式——规则产物 sink 文件归属（PreferenceUtil 93 / PluginInfoManager 30 / SplashCommonUtils 12 / ADBDebug 4 / WbShare 1 = **140** ✅ 自洽）、taxonomy 归属（`persistent_state_write` **97** = PreferenceUtil 93 + ADBDebugActivity 4 内联 SP 写）、v04 人工链路归并（合计 **163** ❌ 有重叠）、行级 sink 站点（**18 站点 = 140** ✅）。**数量论证应以 sink 文件口径或行级站点口径为准。**
>
> ### ✅ 修正落地状态（2026-08-15）
>
> 上述 2 项已直接写入原报告：
> - **§2.1 新增「计数口径说明」表**：列出 A（sink 文件 140）/ A'（taxonomy 97）/ B（v04 归并 163，标注重叠）/ C（行级 18 站点 140）四种口径及换算关系
> - **根因 A 改为以 93 条为论证基数**（占 140 条的 **66.4%**，已复算），116 降为勘误括注
> - **补全方法起始行**：`setLongPref:76`→`:73`、`getPluginInfoFromProvider:260`、`updateSinglePluginFromCloud:207`（`removePref:210`、`setStringPref:93`、`setBooleanPref:117`、`handleEnvSwitch:76`、`jump:104` 经核验原本即正确）
> - 报告头部「勘误」行已更新为两轮修正记录

---

## 1. 核验结论总表

| 章节 | 断言 | 判定 | 说明 |
|---|---|---|---|
| §2.1 | 140 候选、flow_kind 138/1/1、dataflow_status 全 not_proven、deterministic_chain_verified 全 False、evidence_level 全 L2、taxonomy 97/24/12/6/1、scope_key+chain_key 全 None | ✅ **全部属实** | 逐字段复算一致 |
| §2.1 | sink 分布（removePref:210 45、setStringPref:93 27 …） | ⚠️ **口径需注明 + 漏项** | 见 §2.1 |
| §2.2 | identity 组数 140、去重 0 生效 | ✅ 结论对 / ❌ **机制描述错** | 见 §2.2 |
| §2.2 | 「88 条分析、52 条 ai_budget_deferred」 | ❌ **归错规则** | 见 §2.3 |
| §2.3 | 822 条 AI 响应及其全部分布数字 | ⚠️ **数字对、数据边界错** | 见 §2.4 |
| §2.4 | findings 260 = 140/61/59 | ✅ **全部属实** | DB 复算一致 |
| §2.5 | 切片 edges=0、8 contexts、缺 sink 文件、含无关 PagerAdapter | ✅ **全部属实** | 见 §2.5 |
| §3 A–G | 全部根因与代码行号 | ✅ **全部属实** | 见 §3 |
| §6 | 已修复/遗留项 | ✅ 与 docs/updates 8-14 文档一致 | — |

---

## 2. 需修正的 5 处

### 2.1 sink 分布是「方法级归并」，且漏 3 条

报告列出 8 类共 137 条，实际产物为 **18 个行级 sink 站点、合计 140 条**（每候选恰好 1 个 sink）：

| 文件:行 | 条数 | 报告归并为 |
|---|---|---|
| PreferenceUtil.java 217 / 219 / 221 | 15 / 15 / 15 = 45 | `removePref:210` 45 ✅ |
| PreferenceUtil.java 100 / 102 / 104 | 9 / 9 / 9 = 27 | `setStringPref:93` 27 ✅ |
| PreferenceUtil.java 124 / 126 / 128 | 6 / 6 / 6 = 18 | `setBooleanPref:117` 18 ✅ |
| PluginInfoManager.java 271 / 273 | 12 / 12 = 24 | `getPluginInfoFromProvider` 24 ✅ |
| SplashCommonUtils.java 128 | 12 | `jump:104` 12 ✅ |
| PluginInfoManager.java 216 | 6 | `updateSinglePluginFrom` 6 ✅ |
| ADBDebugActivity.java 85 | 4 | `handleEnvSwitch:76` 4 ✅ |
| WbShareResultActivity.java 25 | 1 | ✅ |
| **PreferenceUtil.java 80 / 82 / 84** | **1 / 1 / 1 = 3** | ❌ **报告未列**（`setLongPref`，方法起始 :76） |

方法级归并本身无误（`removePref` 确在 :210 起始），但需注明是归并口径；`setLongPref` 3 条应补上，否则 8 类合计 137 ≠ 140。

### 2.2 去重失效的机制描述错误（影响修复方向）

报告称「规则层未填充 `scope_key/chain_key`（140 个候选 chain_key 相同、scope_key 全 None）」，并据此建议「规则层填充 scope_key/chain_key」。

实测：**规则层输出确实全 None，但 funnel 阶段已正常生成三键**（`slices/candidates.json`）：

| 键 | 不同取值数 |
|---|---|
| `scope_key` | **9** |
| `chain_key` | **120** |
| `deterministic_fact_hash` | **81** |
| **三键合取** | **140** |

即 `build_candidate_identity` 工作正常，退化原因是**三键合取粒度过细**（`chain_key` 含逐候选唯一的 `chain_id`，`dfc_` + entry/source/sink/path 哈希，dataflow.py:259），而非「键未填充」。

- 报告对 `chain_id` 的诊断正确，对「规则层未填键」的诊断错误。
- **修复建议 3 需改写**：应放宽合取粒度（如 scope + sink 位置 + taxonomy），而非补填 funnel 层本就已生成的键。

另：字段实际名为 `funnel_disposition`（不是 `disposition`），取值 `coverage_insufficient` × 140 ✅。

### 2.3 「52 条 ai_budget_deferred」归错规则

- 全 run 共 52 条 deferred，**全部属于 `DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION`**；
- `ACTIVITY_INTENT_TO_SENSITIVE_SINK` 的 `ai_budget_deferred` **全为 False**，`ai_required=True` 136 条、False 4 条；
- run 内 AI 结果中该规则 **实际分析 136 条**，非报告所写的 88 条。

因此「AI 预算被重复链耗尽（52 条 deferred）」对本规则不成立——预算耗尽发生在另一规则上。根因 C 的其余论述（重复付费、人工重复劳动）仍成立。

### 2.4 §2.3 的 822 条取自全局跨 run 缓存，不代表本 run

报告标注数据源为 `ai-cache/entries`（822 条）。实测存在两套存储：

| 路径 | 内容 | 条数 |
|---|---|---|
| `.ai-apk-tracer/ai-cache/entries/` | **全局跨 run** 缓存 | **822** |
| `runs/<run_id>/ai-cache/results.json` | 本 run 结果（单文件） | **162** |
| `runs/<run_id>/ai-trace/*/*.jsonl` | 本 run 轨迹 | 342 行 |

报告所有数字均能在**全局缓存**中精确复现（unresolved 781、refutes 11、supports 0、flaw True 454/False 193/None 175、exfiltration unverified 639/absent 8、gaps 700/642/567/429/263 —— 全部一致），但它们是**跨 run 聚合**，不能用于「本 run 95% unresolved」这一结论。

本 run 该规则的真实分布（136 条）：

| 指标 | 本 run 值 | 报告值（全局） |
|---|---|---|
| unresolved | 135（99.3%） | 781（95%） |
| refutes_candidate | 1 | 11 |
| supports_candidate | 0 | 0 |
| flaw_holds=True | 130（95.6%） | 454（55%） |

**结论方向不变甚至更强**（本 run unresolved 99.3%、flaw_holds True 95.6%，均高于报告引用值），但需替换数据源标注与百分比。另：run 内 `results.json` 记录 status `completed` 152 / `incomplete` 6 / `failed` 4，stop_reason `analysis_complete` 152 / `context_expansion_stalled` 6 / `ai_failed` 4。

### 2.5 v04 结论表述略去「4 待确认」

v04 报告汇总行原文为「**136 误报/不成立 + 4 待确认**」，其中 ADBDebugActivity 4 条为 `debuggable=false` 确定性阻断（判定「不成立」）。报告摘要写「136 误报/不成立（约 97%）」尚可，但正文若表述为「136 误报」则与人工报告的分类不完全一致。

---

## 3. 已逐条命中的部分（无需修改）

### 3.1 代码引用全部准确

| 引用 | 核验结果 |
|---|---|
| `dataflow.py:2695 classify_operation_taxonomy` | ✅ 函数定义在 :2696 |
| `dataflow.py:3117 classify_call_operation` | ✅ 定义 :3118；`is_sink = is_effect` 在 **:3123** |
| `dataflow.py:349 _execute_method` | ✅ |
| `dataflow.py:402 control_fact = inherited_control` | ✅ 原句命中 |
| `dataflow.py:435-453 branch_hint` | ✅ `if condition_fact.state in {"untrusted","maybe_untrusted"}: control_fact = condition_fact`，**确无重置** |
| `dataflow.py:601-625 _execute_call` | ✅ `if not direct_reaching and control_fact and ...` → `flow_kind: "control_to_sink"`、`reaching_argument_indices: []` |
| `dataflow.py:651 跨方法继承 control_fact` | ✅ `self._execute_method(..., control_fact)` |
| `dataflow.py:1937 effect_chains` | ✅ 注释原文「不要求效果参数携带 taint」 |
| `candidate_funnel.py:519 build_candidate_identity` | ✅ 行号精确 |
| `decision.py:228 / :382 / :65` | ✅ 三处均命中（`_REFUTATION_OUTCOMES` 分支、`_deterministic_negative_proof`、`_EVIDENCE_INSUFFICIENCY_GAPS`） |
| `context_builder.py:104 build_initial` / `:739 _refresh_edges_and_guards` | ✅ |
| `detector.py evidence_output L2` | ✅ `"ACTIVITY_INTENT_TO_SENSITIVE_SINK": ("activity","L2","medium")`（:19） |
| `_component_flow_rule_candidates` | ✅ detector.py:321 |

注：`backend/app/candidate_funnel.py`、`backend/app/decision.py` 实际路径为 `backend/app/analysis/candidate_funnel.py`、`backend/app/findings/decision.py`，报告写的是简称，建议补全。

### 3.2 根因 E 红线原文属实

`prompts/l2-review/3.0.5/system.md:49` 原文与报告引用一致：「外溢通道未验证时……**不得输出 refutes_candidate**……仅当存在静态确定性反证（组件不返回数据、Sink 无任何输出通道且无持久化副作用）时才允许」。红线 4「一般偏好写，key 仅统计/UI/CPS」在证据缺失类映射表中（→ unresolved + blocking_gap）✅。

### 3.3 根因 F 属实

`_EVIDENCE_INSUFFICIENCY_GAPS` 确含 `DATAFLOW_NOT_PROVEN`(:?)、`LEGACY_FLOW_FALLBACK`(:85)、`HARM_NOT_ESTABLISHED`(:91)，注释明确「不构成对 AI 判定的确定性冲突……AI 的四要素判定仍可被采信」✅。

### 3.4 §2.5 切片实证完全属实

`slices/slice_bb21709c48f77eccd217/round-000.json`：`edges` **0 条**；`contexts` **8 个** = 1 manifest_component + 1 无关 `androidx/viewpager/widget/PagerAdapter.java:1-13` code_window + 6 个 method 窗口（MiHostLaunchCommand.handleLaunchCommand:46、SplashCommonUtils.jump:104、MainTabActivity 的 onCreate/onActivityResult/onResume/onNewIntent）；**无任何 PreferenceUtil.java 上下文** ✅。

### 3.5 §2.4 DB 实证完全属实

`findings` 表 260 条：`manual_false_positive` 140 / `pending_ai` 61 / `pending_manual` 59 ✅。

---

## 4. 修改建议（最小改动）

1. **§2.1**：sink 分布加注「按方法归并」，补 `setLongPref:76` 3 条，合计校正为 140。
2. **§2.2 / 根因 C / 建议 3**：改为「funnel 三键已生成（scope 9 / chain 120 / fact_hash 81），但三键**合取**后仍为 140 组；根因是 `chain_key` 内嵌逐候选唯一的 `chain_id`」；修复方向由「规则层填键」改为「放宽合取粒度」。
3. **§2.2**：删除「52 条 ai_budget_deferred」归属本规则的表述，改为「本规则 136 条全部进入 AI（无 deferred）；全 run 52 条 deferred 属 DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION」。
4. **§2.3**：数据源标注改为 `.ai-apk-tracer/ai-cache/entries`（**全局跨 run**，822 条），并**增列本 run 数字**（136 条：unresolved 135 / refutes 1 / flaw True 130）；「95% unresolved」若指本 run 应改为 99.3%。
5. **§1 摘要**：「136 误报」改为「136 误报/不成立（含 4 条 debuggable 确定性阻断）」。
6. **全文**：`candidate_funnel.py` → `backend/app/analysis/candidate_funnel.py`，`decision.py` → `backend/app/findings/decision.py`。
