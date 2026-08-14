# AI-APK-Tracer 漏洞发现正确率低：全链路根因分析报告

> **日期**：2026-08-15
> **触发事件**：v04 人工动态验证（`manual-verification-report/v04/v04-p1-activity-intent-dynamic-verification.md`）：`ACTIVITY_INTENT_TO_SENSITIVE_SINK` 140 候选 → 33 条唯一链路（人工归并）→ **136 误报/不成立（含 4 条 debuggable 确定性阻断，约 97%）**，仅确认 1 条真实攻击面（插件路由注入，且是验证过程中新发现，规则未产出）
> **数据来源**：run `20260809T110600Z_1c55d3fb9f95_98fbe158` 规则产物与切片（rule-results/*.json、slices/*.json）、`tracer.sqlite3` findings 表、`ai-cache/entries` 822 条真实 AI 响应、`rules/shared/*.py` 与 `backend/app/*.py` 源码、`docs/analysis/2026-08-14-*` 已有问题确认文档
> **性质**：实证分析报告，所有结论均可由文中数据与代码位置复核
> **勘误**：已按 `2026-08-15-root-cause-report-verification.md` 核验意见修正——第一轮 5 处（funnel 键机制、deferred 归属、AI 数据源边界、sink 归并口径、v04 表述）；第二轮 2 处（根因 A 的 116/93 计数口径、`setLongPref` 方法起始行 `:76`→`:73`，另补全 `getPluginInfoFromProvider:260`、`updateSinglePluginFromCloud:207`）。修正点均以「勘误」标注

---

## 1. 摘要（TL;DR）

**一句话归因**：规则层让"控制流共现"（98.6% 的候选，source 值从未到达 sink 参数）以 L2 证据等级出链 → funnel 精确去重 0 生效，140 条重复链全部送 AI → AI 拿到无边、缺 sink、被跨链污染的残缺切片，只能复述规则断言（本 run 95.6% 判"缺陷成立"）→ AI 协议红线与决策层确定性负证门禁双重 fail-closed，禁止静态否定（本 run 99.3% unresolved）→ 全部积压人工复核 → 人工 97% 判误报。**真正的漏洞（外部可控组件路由）反而因规则只追"声明的敏感 sink"而漏检。**

---

## 2. 实证数据总览

### 2.1 规则层产出（rule-results/ACTIVITY_INTENT_TO_SENSITIVE_SINK.json，140 候选）

| 维度 | 分布 | 关键结论 |
|---|---|---|
| taxonomy | `persistent_state_write` 97 / `data_disclosure` 24 / `ui_navigation` 12 / `database_mutation` 6 / `unknown_effect` 1 | SP 写占 69% |
| **flow_kind** | **`control_to_sink` 138（98.6%）** / `source_to_sink` 1 / `inferred_source_to_sink` 1 | **值流真正到达 sink 参数的仅 1 条** |
| dataflow_status | `not_proven` × 140 | 规则自知全部未证明 |
| deterministic_chain_verified | `False` × 140 | 无一条链被确定性验证 |
| sink 分布 | 按方法归并：`PreferenceUtil.removePref:210` 45（行级 217/219/221 各 15）、`setStringPref:93` 27（100/102/104 各 9）、`setBooleanPref:117` 18（124/126/128 各 6）、`PluginInfoManager.getPluginInfoFromProvider:260` 24（271/273 各 12）、`SplashCommonUtils.jump:104` 12（:128）、`updateSinglePluginFromCloud:207` 6（:216）、`ADBDebugActivity.handleEnvSwitch:76` 4（:85）、`WbShareResultActivity` 1（:25）、**`PreferenceUtil.setLongPref:73` 3（80/82/84 各 1）** | 实际 18 个行级 sink 站点、合计 140（每候选恰 1 个 sink），与 v04 报告各链路分组一一对应；`setLongPref` 3 条为核验后补入、方法起始行 `:73`（勘误） |
| funnel identity | 规则层产物（rule-results）中 `scope_key/chain_key` 未填充（全 None）；**funnel 层已正常生成三键**（见 §2.2） | 去重键由 funnel 层 `build_candidate_identity` 计算，不依赖规则层填键（勘误） |

**⚠️ 计数口径说明（勘误）**：同一批 140 条候选在本报告与 v04 报告中存在**三种计数口径**，引用时须显式标注，否则数字看似矛盾：

| 口径 | 划分方式 | 分布 |
|---|---|---|
| **A. 规则产物 sink 文件归属** | 按候选的 sink 落在哪个文件 | `PreferenceUtil.java` **93**（MainTabActivity 66 + MainActivity 27）/ `PluginInfoManager.java` 30 / `SplashCommonUtils.java` 12 / `ADBDebugActivity.java` 4 / `WbShareResultActivity.java` 1 = **140** |
| **A'. taxonomy 归属** | 按 `operation_taxonomy` | `persistent_state_write` **97** = PreferenceUtil 93 + ADBDebugActivity 4（后者亦为 SP 写，但 sink 在 Activity 内联） |
| **B. v04 人工链路归并** | 按"组件 + 来源 + sink 类别"归并为 33 条唯一链路 | g_utm 组 89 / extra_splashinfo 组 27 / SplashCommonUtils 12 / PluginInfoManager 30 / ADBDebug 4 / WbShare 1（v04 §3 汇总表；**各组相加为 163 > 140，说明分组间存在重叠计数**） |
| **C. 行级 sink 站点** | 按 sink 精确行号 | **18 个站点**，合计 **140** |

口径 A 与 B 不等价，有两重原因：① v04 按**来源**（g_utm / extra_splashinfo）而非 sink 归组，同一来源组内含多种 sink；② v04 §3 汇总表各组相加为 163、超出总数 140，即部分候选被重复计入多组。因此 v04 §1.1+§1.2 的"116 个 PreferenceUtil 写候选"（89+27）**不能直接与规则产物的 93 条对照**——后者是按 sink 文件精确统计、且与 140 总数自洽的口径。涉及数量论证时应以口径 A/C 为准。

**真实候选样例**（PreferenceUtil 写链）：
- source：`MiHostLaunchCommand.java:79 uri.getQueryParameter(...)`（g_utm 读点）
- sink：`PreferenceUtil.java:219 editorEdit.apply(...)`（removePref 内部）
- path：`isExternalLaunch → handleLaunchCommand → onSchemeApp → getJumpInfo` —— **只是方法调用栈，source 与 sink 之间无任何值流边**
- blocking_gaps：全部为 `LINEAR_IR_PATH_SENSITIVITY_LIMITATION`

### 2.2 Funnel 后（slices/candidates.json，该规则 140 条）

- `funnel_disposition`：`coverage_insufficient` × 140（L2 非 deterministic_chain_verified 的全部去向）
- **identity 组数：140（精确去重 0 生效）**；报告中的"33 条唯一链路"是人工按"组件+来源+sink 类别"归并的结果，系统本身无此能力
- **去重机制（勘误）**：funnel 层三键已正常生成——`scope_key` 9 种、`chain_key` 120 种、`deterministic_fact_hash` 81 种，但**三键合取后仍为 140 组**；退化原因是 `chain_key` 内嵌逐候选唯一的 `chain_id`（`dfc_` + entry/source/sink/path 哈希，dataflow.py:259；实测 chain_id 120 种 = chain_key 120 种）。不是"键未填充"，而是**合取粒度过细**
- 本规则 `ai_required=True` 136 条 / `False` 4 条（后者即 ADBDebugActivity 4 条 debuggable 确定性阻断），**无 `ai_budget_deferred`**（勘误：全 run 52 条 deferred 全部属于 `DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION`，与本规则无关）；run 内 AI 结果见 §2.3

### 2.3 AI 环节（本 run `runs/<run_id>/ai-cache/results.json` 该规则 136 条；全局缓存见括注）

| 指标 | 本 run（136 条） | 全局跨 run 缓存（822 条，括注） | 结论 |
|---|---|---|---|
| verdict | **unresolved 135（99.3%）** / refutes_candidate 1 / supports_candidate **0** | unresolved 781（95%）/ refutes 11 / supports 0 | AI 几乎从未确认，也几乎从未否定 |
| flaw_holds | **True 130（95.6%）** / False 6 | True 454（55%）/ False 193 / None 175 | AI 倾向于"缺陷成立"——复述规则断言而非独立证明（本 run 更强） |
| exfiltration_channel | 本 run 缺省 | unverified 639（78%）/ absent 8 | 外溢通道几乎全部未验证 |
| top blocking_gaps | — | `GUARD_PATH_UNRESOLVED` 700、`EXFILTRATION_CHANNEL_UNVERIFIED` 642、`DATAFLOW_NOT_PROVEN` 567、`SYMBOL_TARGET_AMBIGUOUS` 429、`LINEAR_IR_PATH_SENSITIVITY_LIMITATION` 263 | AI 面对大量"静态不可证"缺口 |

勘误：原报告 §2.3 只引用 `ai-cache/entries`（**全局跨 run 缓存**，822 条）未标注边界；本 run 该规则真实分布为 136 条（run 内 status：completed 152 / incomplete 6 / failed 4 为全部规则合计；本规则 stop_reason：analysis_complete 135 / context_expansion_stalled 1）。**结论方向不变甚至更强**（本 run unresolved 99.3%、flaw_holds True 95.6% 均高于全局值）。

### 2.4 决策与人工（tracer.sqlite3 findings，260 条）

`manual_false_positive` 140 / `pending_ai` 61 / `pending_manual` 59。140 条人工误报与 v04 动态验证结论吻合。

### 2.5 AI 切片输入（slices/slice_bb21709c48f77eccd217/round-000.json）

- **edges = 0 条**（切片中无任何调用/值流边）
- **sink 所在文件 PreferenceUtil.java 不在上下文中**（8 个 context 无一是 sink 文件），反而包含无关的 `PagerAdapter.java:1-13` code_window
- 8 个 context：manifest_component + 6 个 method 窗口 + 1 个无关 window

---

## 3. 根因分析（自上游至下游）

### 根因 A（规则层）：sink 定义为"敏感 API 调用"，无危害模型

- `rules/shared/dataflow.py` `classify_operation_taxonomy`（:2695）将 **`SharedPreferences.Editor.put*/apply/commit`、`startActivity`、`insert/update/delete`、`Settings.Secure/Global.put*`** 全部归为 effect；`classify_call_operation`（:3117）`is_sink = is_effect`，无敏感度分级
- 后果：写本地 SP、启动本包固定 Activity 与写系统设置同级（L2 medium）。**写本地 SP 在 Android 威胁模型中绝大多数场景不构成漏洞**（无跨进程外溢、无提权）——**规则产物中 sink 落在 `PreferenceUtil.java` 的 93 条候选**（MainTabActivity 66 + MainActivity 27，占全部 140 条的 66%）即"注入面成立但危害不存在"的典型，对应 v04 §1.1/§1.2 的动态验证结论（勘误：原文引用 v04 人工归并口径的 116 条，该口径与 sink 文件口径不等价，详见 §2.1 计数口径说明）
- sink 判定只检查"调用存在 + receiver 家族匹配"，**从不检查 sink 参数是否受控**（removePref 的 key 是固定常量）

### 根因 B（链生成层，最核心）：`control_fact` 无作用域，"分支条件可控"被解释为"整段代码可控"

- `_execute_method`（dataflow.py:349）对 `branch_hint`（:435-453）：条件求值为 untrusted 即 `control_fact = condition_fact`，**设置后永不重置**，线性执行到方法结束；并通过 `inherited_control` **跨方法继承**（:402、:651）
- `_execute_call`（:601-625）：sink 无 untrusted 参数到达时，只要 `control_fact` untrusted → 生成 `control_to_sink` 链，**参数与 sink 无关也成链**
- `effect_chains`（:1937）注释自述："返回……每个 typed effect，**不要求效果参数携带 taint**"
- 后果：入口 onCreate/onNewIntent 中任意"攻击者可控 if 条件"之后，**整个调用图内所有 SP 写/startActivity/insert 全部挂链**（写常量也挂）。138/140（98.6%）候选由此产生
- 关键：这些链的 `dataflow_status=not_proven`、critical gaps 全部如实标注，**但证据等级未降，仍按 L2 输出**（detector.py `evidence_output: L2`），"未证明的链"与"证明的链"同等待遇进入下游

### 根因 C（Funnel 层）：精确去重 0 生效

- 机制（勘误）：`build_candidate_identity`（`backend/app/analysis/candidate_funnel.py:519`）本身工作正常，funnel 后三键已生成（scope_key 9 / chain_key 120 / deterministic_fact_hash 81），但 **`chain_key` 内嵌逐候选唯一的 `chain_id`**（`dfc_` + entry/source/sink/path 哈希，dataflow.py:259）→ **三键合取后 140 组**，语义完全相同的链（同入口、同 source 位置、同 sink）也永远无法合并，140→140
- 后果：重复链全部进入 AI 工作清单（本规则 136 条实际分析，无预算延后——勘误：此前所述"52 条 deferred"属另一规则）、重复付费、人工复核重复劳动

### 根因 D（切片层）：AI 输入残缺 + 跨链污染

- **sink/source 上下文缺失**：sink anchor（PreferenceUtil.java:219）在 `build_initial`（context_builder.py:104）中因 `path not in self.files`（组件 flow scope 文件子集不含 sink 文件）静默丢失，AI 只能看到候选描述里的 sink 断言
- **edges 恒空**：`_refresh_edges_and_guards`（:739）只在已加载方法间建**调用**边，初始 6-8 个 context 间无调用关系 → 0 条边；切片协议中没有"值流边"概念，AI 无从判断 source→sink 是否连通
- **跨链污染**（docs/analysis/2026-08-14-slice-mismatch-problem.md 已确认）：`_slice_id()` 只哈希 `rule_id+component+locations`（修复前），MainTabActivity 78 候选、sinks 13 种、**共用同一 slice**——"AI 分析全看 putBoolean:124 上下文，却为 commit:221 等候选下结论"
- 后果：AI 面对"候选断言 + 残缺代码窗口 + 0 边"，只能复述规则断言 → 本 run 95.6% flaw_holds=True；**AI 的"成立"判断不是独立语义证明**

### 根因 E（AI 协议层）：双重 fail-closed 禁止静态否定

- l2-review 3.0.5 红线 23（system.md:49）：**"外溢通道未验证时必须生成 EXFILTRATION_CHANNEL_UNVERIFIED……不得输出 refutes_candidate"**——哪怕静态已确定"非导出 provider、数据无法跨进程取回"，只要不满足极窄的"组件不返回数据且无持久化副作用"，AI 只能 unresolved
- 红线 4（"一般偏好写，key 仅统计/UI/CPS"）归类为"证据缺失类"→ 强制 unresolved + blocking_gap，不是确定性否定
- 后果：AI 是"被请来降误报"的，但协议**不允许它独立把候选判成误报**——本 run 99.3% unresolved 是协议设计的结果，不是模型能力问题

### 根因 F（决策层）：确定性负证门禁 + 证据不足白名单"自首不降级"

- `decide_candidate`（`backend/app/findings/decision.py:228`）：AI refutes 必须 `_deterministic_negative_proof`（:382）有确定性背书（coverage 允许 negative_proof + 强权限/有效 Guard/确定性反证字段），否则 → `L2_REFUTED_WITHOUT_DETERMINISTIC_NEGATIVE_PROOF` → **pending_manual**
- `_EVIDENCE_INSUFFICIENCY_GAPS`（:65）把 `DATAFLOW_NOT_PROVEN`、`HARM_NOT_ESTABLISHED`、`LEGACY_FLOW_FALLBACK` 等定义为"静态限制，非确定性冲突"——**规则明说"链未证明"，系统仍让其以 L2 身份进入 AI 分析且 AI 判定被采信**（AI 判成立 → `L2_POSITIVE_GATES_PASSED` 可到 pending_manual；判否定无背书 → 也 pending_manual）
- 已确认的三处决策矛盾（docs/analysis/2026-08-14-decision-contradictions-fix-plan.md）：
  1. 6 候选：AI 在 3 个 critical gap 下 `flaw_holds=False` → 被标 `ai_likely_false_positive`（违反"未找到证据≠证据表明不成立"）
  2. `3fe8a217`：AI `verdict=refutes` 但 `flaw_holds=True` → 决策层采信错误的成立信号 → `ai_likely_supported`（实际误报）
  3. `89da4b67`：`deterministic_chain_verified=True` → `supported`，**但全库无调用点（红线 13 死代码）**——确定性链验证未查调用点存在性

### 根因 G（反向问题）：真实攻击面漏检

- v04 §2 实证：`extra_splashinfo → Fasade.startNewPluginActivity`（任意插件 Activity + 全量 extras 注入，ACTION_ROOT 隐式路由）动态验证**成立**，但规则未产出该候选——规则只追"声明的敏感 sink"（PreferenceUtil 写），插件 Activity 不在 manifest 组件索引、resolve 失败成 gap 即丢弃
- 规则重心是"值流到已知敏感 API"，不是"外部可控的路由/导航能力"这个更真实的攻击面

---

## 4. 因果链整合

```
[规则层] sink=敏感API调用、无危害模型（A）
   └→ [链生成] control_fact 无作用域 → 控制流共现成链 138/140（B）
        └→ 全部 dataflow_status=not_proven，但仍按 L2 输出
             └→ [Funnel] identity 含逐候选唯一 chain_id → 去重 0 生效（C）
                  └→ 140 条重复链全部送 AI
                       └→ [切片] 无边、缺 sink、跨链污染（D）
                            └→ AI 只能复述断言：95.6% flaw_holds=True、99.3% unresolved（E 协议禁否定）
                                 └→ [决策] 无确定性负证 → pending_manual（F）
                                      └→ 人工复核：97% 误报
同时：真实路由注入面因"只追声明 sink"漏检（G）
```

**数据闭环验证**：140 候选（98.6% 无值流）→ 140 全 coverage_insufficient → AI（136 条）99.3% unresolved / 95.6% 判成立 → 260 finding 中 140 manual_false_positive。每一环均有产物/缓存/库表实证。

---

## 5. 修复建议（按杠杆排序）

| # | 建议 | 预期收益 | 涉及 |
|---|---|---|---|
| 1 | **`control_to_sink` / `dataflow_status=not_proven` 链不得按 L2 输出**：降为 L1 线索或 signals 级，不进 AI 预算；值流未证明的链仅在确定性价值链（guard/授权）完整时才可升级 | 砍掉 ~98% 的 AI 花费与人工复核量（本规则 140→~2） | detector.py `_component_flow_rule_candidates`、`_component_rule`、`backend/app/analysis/candidate_funnel.py` |
| 2 | **sink 危害分级**：`persistent_state_write` 仅 `Settings.Secure/Global` 或"敏感值 + 跨进程读取面"时算敏感；本地 SP 写默认信号级；`ui_navigation` 仅在目标外部可控/任意组件时算敏感（检查 resolved_target 是否本包固定） | 消除 97/140 的 SP 写候选 + 12 条固定目标链 | dataflow.py `classify_operation_taxonomy` |
| 3 | **funnel identity 放宽合取粒度（勘误修正）**：去重键 = 入口（scope）+ sink 位置 + taxonomy + effect 类别（复刻人工 33 链归并逻辑），**剔除 `chain_key` 内嵌的逐候选唯一 `chain_id`**；三键合取粒度由"全量链身份"放宽为"语义类别" | 140→33 组，AI 调用 136→~33，预算利用率 ×4 | `backend/app/analysis/candidate_funnel.py` `build_candidate_identity` |
| 4 | **切片完整性 + 确定性事实注入**：sink/source 方法必须入上下文（修复 `path not in self.files` 静默丢失）；AI 输入补充"目标固定性、provider 导出、值流是否证明、sink 参数是否受控"等规则已算出的确定性事实 | AI 有依据输出 refutes，95.6% 的"复述式成立"下降 | context_builder.py `build_initial`、`_candidate_anchors`、prompts/l2-review |
| 5 | **AI 协议允许静态否定**：有确定性事实（固定目标/非导出/进程内终点/常量 key）时允许 `refutes_candidate` + basis，决策层采信为 `ai_false_positive`；红线 23 增加"静态可证无通道"例外 | 决策层可闭环，人工复核量显著下降 | prompts/l2-review/3.0.5、`backend/app/findings/decision.py` `_deterministic_negative_proof` |
| 6 | **决策层对齐"未证明≠成立"**：`deterministic_chain_verified=True` 增加调用点存在性校验（红线 13）；flaw_holds=False 在 critical gap 下不得标 ai_likely_false_positive | 消除三处已知矛盾 | `backend/app/findings/decision.py`、`backend/app/analysis/candidate_funnel.py` |
| 7 | **新增外部可控路由规则**：ACTION_ROOT 类隐式路由 → 插件/非 manifest 组件（v04 §2 真实面）独立规则 | 补上最大漏报面 | rules/activity 新规则 |

**预期综合效果**：以本规则为例，候选 140 → 预计 ≤5 条真值流链；AI 调用 136 → ≤5；人工复核 140 → ≤10；同时补回插件路由注入类真实漏洞。

---

## 6. 已修复项与遗留项（截至 2026-08-14）

| 项 | 状态 | 说明 |
|---|---|---|
| slice_id 缺链身份（跨链污染） | ✅ 已修（8-14） | `_slice_id()` 增加 sources/sinks/propagation_paths 哈希 + `_anchor_projection` |
| finding↔slice 一致性自检 | ✅ 已修（8-14） | `finding_slice_sink_mismatch` 扫描期自检 + 存量回溯 CLI |
| 决策三处矛盾 | ✅ 已修（8-14） | docs/updates/2026-08-14-decision-contradictions-fix.md |
| receiver 协议门（mipush/极光类） | ✅ 已修（8-14） | `INPUT_PROTOCOL_UNCONTROLLED` gap |
| **根因 A：sink 危害分级** | ⏸ 未动 | 最上游，未列入任何修复计划 |
| **根因 B：control_to_sink 出链** | ⏸ 未动 | 最核心放大器，未列入任何修复计划 |
| **根因 C：funnel identity 去重失效** | ⏸ 未动 | chain_key 内嵌逐候选唯一 chain_id → 三键合取 140 组（勘误：非"规则层未填键"） |
| **根因 D：切片 sink 上下文缺失/无边** | ⏸ 部分 | slice_id 已修，但上下文选择与 edges 仍残缺 |
| **根因 E：协议禁静态否定** | ⏸ 未动 | 红线 23 保守设计 |

---

## 7. 结论

当前正确率低的本质不是"AI 判得不准"，而是**管线前端的候选语义（控制流共现 ≈ 漏洞）与后端的判定协议（禁止静态否定）共同把误报锁存到人工环节**。修复应优先处理上游（根因 A/B/C），它们同时降低 AI 成本与人工复核量；下游修复（D/E/F）解决"AI 与决策层无法闭环否定"的结构性问题。真实漏洞的补检（G）需要把"外部可控路由能力"纳入规则模型。
