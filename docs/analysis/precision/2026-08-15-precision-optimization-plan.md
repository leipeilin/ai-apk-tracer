# 误报治理生产优化方案（基于根因报告 A–G）

> **日期**：2026-08-15
> **依据**：`docs/analysis/false-positive/2026-08-15-false-positive-root-cause-report.md`（根因 A–G）+ `2026-08-15-root-cause-report-verification.md`（核验结论）
> **基线 run**：`20260809T110600Z_1c55d3fb9f95_98fbe158`（com.xiaomi.shop 5.53.0）
> **性质**：可执行工程方案。所有削减量均由基线产物实测，不使用估算；所有改动点均给出代码锚点与验收口径。
> **修订 v2（2026-08-15）**：已按 `2026-08-15-precision-optimization-plan-review.md` 审查意见修订。采纳 5 项（P1-5 交叉验证、验收双口径、P0-1 作用域覆盖清单、回归数据源扩充、`demotion_reason` 结构化）；**不采纳其修订点①原文**（"非字面量 → 保守 L2"），理由与实测反证见 `2026-08-15-plan-review-response.md` §2。

---

## 0. 方案总览

| 阶段 | 措施 | 治理根因 | 实测削减（本规则 140 条基线） | 风险 |
|---|---|---|---|---|
| **P0-1** | `control_fact` 块级作用域化 | B（最核心） | 直接消除误链源头 | 中（改动数据流内核） |
| **P0-2** | 分级出链：值流未证明 → L1 signal，不占 AI 预算 | A+B | **140 → 2**（-98.6%） | **高（召回风险，需守门）** |
| **P0-3** | funnel 去重键语义化 | C | 140 → **27 组**（-80.7%，单独生效时） | 低 |
| **P1-4** | 切片必含 sink/source + 注入确定性事实 | D | AI 判定质量（非数量） | 低 |
| **P1-5** | 协议放开静态否定 + 决策层采信 | E+F | 人工复核量 | 中 |
| **P2-6** | 新增外部可控路由规则 | G | 补漏报 | 低 |

**组合效果（P0-1+2+3）**：本规则 140 → **2** 条送 AI；全 run 274 → 约 133（`control_to_sink` 141 条降级为 signal）。

> ⚠️ **单样本警告**：以上削减量仅来自 1 个 APK。P0-2 的阈值必须按 §5 的召回守门流程灰度，不得直接全量上线。

---

## 1. P0-1：`control_fact` 块级作用域化（根因 B 的根治）

### 问题
`rules/shared/dataflow.py:435-453`：`branch_hint` 遇到 untrusted 条件即 `control_fact = condition_fact`，**设置后永不重置**，线性执行至方法结束，并经 `:651` 跨方法继承。后果是入口方法中任意一个"攻击者可控 if"之后，**整个调用图内所有 effect 全部挂链**——本 run `control_to_sink` 138/140（98.6%）由此产生。

### 改法
线性 IR 缺少块结束标记，因此不做"精确支配分析"（成本高、收益边际），改为**保守的作用域近似**：

1. **IR 侧**：在 flow IR 生成阶段为 `branch_hint` 补 `block_end_line`（由 JADX 缩进/花括号配对推断；推断失败则不补）。
2. **执行侧**：`_execute_method` 用栈保存 `(control_fact, block_end_line)`；执行到 `instruction.line > block_end_line` 时**弹栈还原**，而非持续到方法末尾。
3. **降级路径**：`block_end_line` 缺失时维持现状（持续到方法末尾），但**必须追加 `CONTROL_SCOPE_UNRESOLVED` gap**，使该链无法被判为高可信。
4. **跨方法继承收紧**：仅当调用点**位于该分支块内**才传递 `control_fact`；块外调用不再继承。

**作用域结构覆盖清单**（均沿用「保守近似 + 缺失带 gap」原则）：

| 结构 | 处理 |
|---|---|
| `if` then 块 | `block_end_line` 基本情形 |
| **`else` 块** | untrusted 条件为 false 时 else 内同样受控——须独立记作用域，不能只覆盖 then |
| **循环**（`while`/`for` 条件受控） | 循环体为独立作用域 |
| **`switch-case`** | 按 case 区域边界切分 |
| **`try/catch`** | catch 块不继承 try 内的 `control_fact`（异常路径语义不同） |

带 `CONTROL_SCOPE_UNRESOLVED` gap 的链**仍参与 P0-2 降级**，但 `demotion_reason` 记为 `scope_unresolved`（与 `scope_out_of_block` 区分），避免两类降级在回归审计中混淆。

### 验收
- 基线 run 重跑后 `control_to_sink` 显著下降，且**下降的链必须都是"sink 在分支块外"**（逐条抽样核对，不接受整体数字达标）。
- 新增单测：分支内 sink 仍成链、分支后 sink 不成链、`block_end_line` 缺失时带 gap。

---

## 2. P0-2：分级出链——值流未证明的链不占 AI 预算（杠杆最大）

### 问题
`rules/shared/detector.py:456` 无条件 `_base(rule_id, component, "L2", ...)`：无论 `flow_kind` 与 `dataflow_status`，一律 L2 出链。而 `deterministic_chain_verified`（:432）要求 `effect_verified and 收敛 and 无 critical gap`——本 run **141 条 `control_to_sink` 无一为 True**，`dataflow_status` 全为 `not_proven`，却与已证明链同等进入 AI。

### 改法：按证据强度三分
在 detector 组装候选处引入 `evidence_tier` 判定：

| 序 | 判据 | 出链等级 | 是否送 AI |
|---|---|---|---|
| 1 | `flow_kind=source_to_sink` 且 `reaching_argument_indices` 非空 | **L2** | 是 |
| 2 | `flow_kind=control_to_sink` 且 **P0-1 修复后 `control_fact` 仍在 sink 支配域内** | **L2** | 是 |
| 3 | `flow_kind=control_to_sink` 且 sink 在分支块外（P0-1 判定） | **L1 signal** | **否**（仅列示） |
| 4 | `flow_kind=inferred_source_to_sink`（`LEGACY_FLOW_FALLBACK`） | **L1 signal** | 否 |

**判据说明（v2 修订，见 `2026-08-15-plan-review-response.md` §2）**：

降级与否由 **P0-1 的作用域分析**裁决，**不使用"sink 参数字面量性"作为主判据**。原因：

- `control_to_sink` 的定义（`dataflow.py:601-625`）已确定"**无任何 untrusted 值到达 sink 参数**"（`:614` 写死 `reaching_argument_indices: []`）。这是 taint 引擎的确定性事实，强于字面量形态推断。
- **参数非常量 ≠ 参数受控**。实测反证：若按"非字面量 → 保守 L2"实施，138 条中 **57 条**会被送 AI（字面量 36 / 常量引用 45 / 非字面量 57，按 wrapper 真实调用点统计），而这 57 条按 v04 动态验证**全部是误报**（PluginInfoManager 30 + SplashCommonUtils 11 + PreferenceUtil 12 + ADBDebug 4）——成本放大 28 倍、零召回收益。
- ⚠️ **层级陷阱**：候选的 sink 是 **wrapper 函数体内部行**（`PreferenceUtil.java:219 editorEdit.apply()`，`method_id=PreferenceUtil.removePref:210`），不是业务侧的 `removePref(...)` 调用点。直接在 sink 行取参数，得到的是 `apply()`/`commit()` 的**零参**或 wrapper 形参名（`str`、`str2`）——实测 138 条在 sink 层"含字面量"为 **0**。做任何参数级判定都必须先经 `propagation_paths` 中 `resolved_target_id == sink.method_id` 的那一跳穿透到真实调用点（实测 **138/138 可定位**）。

**参数常量性的正确用途**：作为 **P1-4 注入给 AI 的确定性事实**（如"sink 参数为常量 `back_url`，攻击者不可控"），帮助 AI 输出 `refutes`；**不作为出链等级的裁决者**。

**降级原因结构化**：`demotion_reason` ∈ {`scope_out_of_block`, `legacy_fallback`, `guard_blocked`}，供 §5 回归审计。

### 实测效果（基线 run）
```
140 条候选
 ├─ control_to_sink        138  → L1 signal（不送 AI）
 ├─ inferred_source_to_sink  1  → L1 signal（不送 AI）
 └─ source_to_sink           1  → L2 送 AI
送 AI：140 → 2
```
全 run：`control_to_sink` 141 条（ACTIVITY 138 + RECEIVER_INPUT_TO_SINK 3）降级，274 → 约 133 条候选。

### ⚠️ 召回风险（必须正视）
`control_to_sink` **不是天然无效的模式**——"攻击者控制分支条件 → 走到特权操作"是真实攻击面。本 run 它 100% 误报，是因为 P0-1 描述的作用域缺陷把它污染成了噪声。因此：

- **P0-1 必须先于 P0-2 上线**。作用域修好后，剩余的 `control_to_sink` 才是有意义的信号。
- 降级为 **signal 而非丢弃**：候选仍写入产物与前端列表，只是不占 AI 预算、不进人工队列。
- 必须通过 §5 召回守门后才能设为默认。

---

## 3. P0-3：funnel 去重键语义化（根因 C）

### 问题（已勘误订正）
`build_candidate_identity`（`backend/app/analysis/candidate_funnel.py:519`）本身工作正常，funnel 后三键均已生成：`scope_key` 9 种 / `chain_key` 120 种 / `deterministic_fact_hash` 81 种。退化原因是**三键合取**——`chain_key` 内嵌逐候选唯一的 `chain_id`（`dfc_` + entry/source/sink/path 哈希，`dataflow.py:259`），实测 `chain_id` 120 种 = `chain_key` 120 种，导致合取后仍是 140 组。

### 改法
1. `chain_key` 计算**剔除 `chain_id`**（保留 entry/sources/sinks/taxonomy/flow_kind 语义要素）；`chain_id` 仍保留在候选体内供追溯，只是不参与身份哈希。
2. `deterministic_fact_hash` 的 `facts` 集合排除随链路波动的组件级 trace 字段（`method_summaries`、`reaching_definitions`、`validation_transitions`、`slot_overwrites` 等），只保留判定相关事实。
3. 分组代表送 AI，同组成员复用结论（`_PIPELINE_AI_RESULT_FIELDS` 复制机制已存在，无需新建）。

### 实测效果
按「组件 + sink 文件 + sink 行 + taxonomy」语义键分组：**140 → 27 组**（-80.7%），与人工归并的 33 条链路量级一致（人工另按 source 细分）。

> 注：P0-2 与 P0-3 效果**不叠加**——P0-2 生效后只剩 2 条送 AI，去重收益归零。P0-3 的价值在于 **P0-2 未覆盖的规则族**（如 `DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION` 76 条、本 run 52 条 `ai_budget_deferred` 全部来自该规则），以及 P0-2 灰度期间的兜底。

---

## 4. P1：AI 侧闭环（根因 D/E/F）

### 4-1 切片完整性（根因 D）
- **修 sink 静默丢失**：`context_builder.py:104 build_initial` → `_methods_at`（:452）在 `path not in self.files` 时静默返回 `[]`。sink anchor 所在文件不在组件 flow scope 时应**按需加载**该文件；确实无法加载时必须产出 `SINK_CONTEXT_UNAVAILABLE` gap，而不是无声丢弃。
  - 实证：`slice_bb21709c48f77eccd217` 的 8 个 context 中**无一是 sink 文件 PreferenceUtil.java**，却含无关的 `PagerAdapter.java:1-13`。
- **注入确定性事实**：把规则已算出但未下发的事实写入切片头部——`flow_kind`、`dataflow_status`、`reaching_argument_indices` 是否为空、sink 参数常量性、目标是否本包固定、provider 是否导出。**这是 AI 能输出 `refutes` 的前提**（目前它只能看到候选断言 + 残缺代码）。

### 4-2 协议允许静态否定（根因 E）
`prompts/l2-review/3.0.5/system.md:49` 红线 23 规定外溢通道未验证时**一律不得 `refutes_candidate`**。本 run 结果：unresolved 135/136（99.3%）。

改法（新版本 `3.0.6`，**旧版本保留共存**，registry 按精确 id/version 解析）：
- 在红线 23 增列**静态可证例外**：目标为本包固定组件、provider `exported=false`、sink 参数为常量、值流终点在进程内 —— 满足任一且有 `evidence_refs` 背书时，允许 `refutes_candidate`。
- 新增输出字段 `refutation_basis`（枚举），供决策层机器校验，避免自由文本。
- 按项目既有约定：新增枚举/必填字段必须同步进 `system.md` 显式声明，并由现有参数化测试（`test_prompt_declares_every_required_output_field`、`test_prompt_declares_every_schema_enum_value`）覆盖。

### 4-3 决策层采信（根因 F）—— **必须交叉验证，不得无条件采信**

`backend/app/findings/decision.py:382 _deterministic_negative_proof`：把 4-2 的 `refutation_basis` 纳入确定性背书来源（当前仅认强权限/有效 Guard/确定性反证字段）。

> ⚠️ **风险与护栏（审查意见采纳，见 `2026-08-15-plan-review-response.md` §1）**：若决策层**直接采信 AI 自报的 `refutation_basis`**，而该断言是幻觉，真漏洞会被判成 `ai_false_positive`——把"97% 误报"翻转成**漏报**，方向更坏。项目已有先例：AI 自标 `evidence_refs` 无效必须拒绝（`AI_EVIDENCE_REF_INVALID` 不在 `_EVIDENCE_INSUFFICIENCY_GAPS` 白名单内）。

**强制交叉验证规则**：

| AI `refutation_basis` | 必须匹配的 P1-4 注入事实 | 不一致 / 事实缺失 |
|---|---|---|
| `non_exported_provider` | `provider_exported == false` | **不采信** → `pending_manual` |
| `fixed_local_target` | `resolved_target` 为本包固定组件 | **不采信** → `pending_manual` |
| `constant_sink_argument` | sink 参数经 reaching-definition 求值为字面量 | **不采信** → `pending_manual` |
| `in_process_terminus` | 值流终点在进程内 | **不采信** → `pending_manual` |

- `refutation_basis` 枚举值与注入事实字段**一一对应、单一来源**（遵循项目「同一语义单一来源」约定）。
- 保留既有联合裁决边界：确定性冲突、AI 自标证据无效仍然否决，**不放宽**；只有"证据不足类 gap"允许放行降级。

---

## 5. 召回守门（P0-2 上线前置条件，不可跳过）

单个 APK 无法支撑"降级 98.6% 候选"的决策。上线前必须：

1. **配置开关先行**：新增 `funnel.demote_unproven_flow`（默认 **false**），以及 `funnel.unproven_flow_tier`（`signal` / `l2`）。先发布代码、不改默认行为。
2. **历史回归**：对现有 run 产物离线重放分级逻辑，统计「被降级候选数」与「其中曾被人工判为真漏洞的条数」。
   - **硬门槛：被降级集合中真漏洞数 = 0**，方可将默认值翻为 true。
   - ⚠️ **数据基础薄弱（审查意见采纳）**：`.ai-apk-tracer/runs/` 下**当前只有 1 个 run**（110600Z），其 140 条人工标签虽可用但覆盖面有限。补充样本源：全局跨 run 缓存 `.ai-apk-tracer/ai-cache/entries`（**822 条**真实 AI 输入/输出，含 8-14 之后的产物）纳入离线重放。
3. **多 APK 验证**：至少 3 个不同应用重跑，确认 `control_to_sink` 占比与误报率结论可复现（本 run 98.6% 可能是该应用编码风格所致）。
   - 样本应**刻意选取风格差异大**的应用：混淆程度、是否有插件体系、是否使用 RN bridge（RN bridge 的 `safeReadableMap.getString(...)` 是真实外部可控点，本 run 已实证存在）。
4. **可观测**：run manifest 增记 `demoted_candidates`（按 rule / flow_kind / `demotion_reason` 分组计数），使降级行为可审计、可回溯。

---

## 6. P2-6：新增外部可控路由规则（根因 G，补漏报）

v04 动态验证成立的真实漏洞（`extra_splashinfo` → `Fasade.startNewPluginActivity` → ACTION_ROOT 隐式路由启动任意插件 + 全量 extras 注入）**规则完全未产出**。

核查确认：`startNewPluginActivity` 在索引中**存在**（7 个方法匹配），且在候选 JSON 中出现过——但仅出现在 `method_summaries` 的噪声列表里，**从未成为 sink**。原因是 `classify_operation_taxonomy` 只识别 `Context.startActivity` 家族（`dataflow.py:2816`），应用自定义的路由 wrapper 不在 effect 表内；而 `dataflow.py:2729` 对 `resolved_target` 非空的调用直接返回 `is_effect=False`（要求进入真实 callee），插件 Activity 不在 manifest 组件索引中，resolve 失败即丢弃。

### 改法
新增独立规则族 `ACTIVITY_EXTERNAL_ROUTE_INJECTION`：
- **Sink 语义改为「路由能力」**而非「已知敏感 API」：识别 `Intent.setAction`/`setClassName`/`putExtras` 后交由任意 `start*` 的路径，其中 **action 或目标组件来自外部输入**。
- 覆盖隐式路由：`setAction(常量) + 目标由 pluginId 等外部值决定` 的模式。
- 覆盖 `putExtras(bundle)` 的**全量透传**（攻击者可注入任意 key），这是 v04 危害的核心。
- 非 manifest 组件（插件/动态注册）resolve 失败时**产 gap 并保留候选**，不静默丢弃。

---

## 7. 落地顺序与工程约定

```
P0-1 control_fact 作用域化      ← 先修根因，否则 P0-2 会掩盖问题
  └─ P0-2 分级出链（默认关闭）   ← 配置开关 + 召回守门
       └─ §5 历史回归 + 多 APK 验证 → 翻默认值
P0-3 funnel 去重语义化          ← 可与 P0-1 并行，风险低
P1-4 切片完整性 + 事实注入       ← 独立，风险低，先行亦可
  └─ P1-5 协议 3.0.6 + 决策采信  ← 依赖 P1-4 的事实注入
P2-6 路由注入规则               ← 独立
```

**必须遵守的项目约定**：
- 每次改动新建 `docs/updates/YYYY-MM-DD-<英文描述>.md` **单独一个文件**（不在旧文档追加）。
- prompt 改动一律**新增版本号**，旧版本保留共存；`system.md` 必须显式声明全部 required 字段与枚举值。
- 本地统一校验入口 `scripts/check-all.sh`。
- 涉及"同一语义的正则/常量"必须单一来源；规则层与 backend 无法共享模块时显式注释「手动同步点」并 grep 验证真实字节。

---

## 8. 验收指标（以基线 run 复算）

> ⚠️ **双口径（审查意见采纳）**：P0-2 生效后本规则仅剩 2 条送 AI，此时"unresolved ≤60%"（=1 条）、"refutes ≥30%"（=0.6 条）已无统计意义。因此 **AI 质量类指标一律在 P0-2 默认关闭（口径 A）下复算**，数量类指标在 P0-2 生效后（口径 B）验收。

**口径 A —— P0-2 关闭（灰度期），验证 P1-4/P1-5 自身效果**

| 指标 | 现状 | 目标 |
|---|---|---|
| AI `unresolved` 占比 | 99.3%（135/136） | ≤ 60% |
| AI 给出 `refutes` 的占比 | 0.7%（1/136） | ≥ 30%（需 P1-4 事实注入支撑） |
| 切片含 sink 上下文比例 | 0%（抽样切片 8 context 无 sink 文件） | 100%（否则必带 gap） |
| `refutation_basis` 通过交叉验证的比例 | — | ≥ 80%（低于此值说明 AI 在编造 basis） |

**口径 B —— P0-2 生效后，验证数量收敛**

| 指标 | 现状 | 目标 |
|---|---|---|
| 本规则送 AI 候选数 | 136 | ≤ 5 |
| 人工 `pending_manual` | 59 | ≤ 20 |
| 被降级候选中的真漏洞 | — | **0（硬门槛）** |
| v04 插件路由注入 | 未产出 | 由 P2-6 产出候选 |
