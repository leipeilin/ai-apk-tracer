# 《误报治理生产优化方案》审查意见

> **审查对象**：`docs/analysis/precision/2026-08-15-precision-optimization-plan.md`（P0-1 ~ P2-6 + 召回守门 + 验收指标）
> **审查日期**：2026-08-15
> **审查方法**：逐条复算方案声称的"实测"数字（基线 run `20260809T110600Z_1c55d3fb9f95_98fbe158` 产物：rule-results / slices / index/analysis.sqlite3 / decompile/sources）；对 P0-2 的关键判定（sink 参数常量性）做索引级实证抽查
> **总体判定**：**认可方案主体**（方向、顺序、工程护栏全部正确）。需在落地前修订 **4 处**：① P0-2 的"参数受控"判定语义与实现缺口；② P1-5 决策层采信必须与注入事实交叉验证；③ §8 验收指标口径矛盾；④ 两处次要补充（P0-1 作用域边界、历史回归数据基础）。

---

## 1. 结论总表

| 方案章节 | 断言 | 审查判定 | 说明 |
|---|---|---|---|
| §0 组合效果 | 全 run `control_to_sink` 141 条（ACTIVITY 138 + RECEIVER 3） | ✅ 实测命中 | 274 候选复算：control_to_sink 141 / receiver_exposure 76 / None 54 / inferred 1 / source_to_sink 1 / critical_gap 1 |
| §0 组合效果 | 274 → 约 133 | ✅ 实测命中 | 274 − 141 = 133 |
| §2 P0-2 判别位 | `control_to_sink` 的 `reaching_argument_indices` 为空（dataflow.py:614 写死 `[]`） | ✅ 实测命中 | 138 条 control_to_sink 的该字段 **全部为 `()`** |
| §2 P0-2 削减 | 140 → 2 送 AI（138 control + 1 inferred 降级，1 source 送 AI） | ✅ 逻辑成立 | 见 §2.1（有补充） |
| §2 P0-2 警告 | `arguments_json` 存变量名（zlib 压缩），非字面量 | ✅ 实测命中 | 见 §2.1 实证 |
| §3 P0-3 分组 | 语义键分组 140 → 27 组 | ✅ 实测命中 | 按「组件+sink 文件+sink 行+taxonomy」复算 = 27 组 |
| §3 P0-3 机制 | chain_key 内嵌逐候选唯一 chain_id（dataflow.py:259） | ✅ 与此前核验一致 | chain_id 120 种 = chain_key 120 种 |
| §6 P2-6 根因 | `startNewPluginActivity` 在索引中存在但从未成为 sink（resolved_target 非空 → is_effect=False） | ✅ 代码确认 | dataflow.py:2729-2730 |
| §8 验收 | 现状：本规则送 AI 136、unresolved 99.3%、refutes 1、pending_manual 59 | ✅ 实测命中 | 与核验报告一致 |

**方案所有"实测"数字均真实可复现，无虚报。** 以下修订意见不改变方案骨架，仅修正实现路径与口径。

---

## 2. 修订点 ①（关键）：P0-2 的「sink 参数受控 → L2」档在现实现下无法命中，需改为「常量性判定」

### 2.1 问题

方案 §2 的判定表：

| flow_kind | 条件 | 出链 |
|---|---|---|
| `control_to_sink` | **sink 参数受控** | L2 送 AI |
| `control_to_sink` | sink 参数不受控 | L1 signal |

但实测（复算 138 条）：**`control_to_sink` 分支的 `reaching_argument_indices` 恒为空**（dataflow.py:614 写死 `[]`）——`control_to_sink` 的定义就是"无 untrusted 参数到达 + control_fact untrusted"。因此"参数受控"**不可能**由现有 taint 机制给出；方案 §2 改走 reaching-definition 常量性判定，但存在语义缺口：

- **非常量 ≠ 受控**：参数可能来自内部逻辑、静态字段、文件读取——`control_to_sink` 的参数从未带 taint，常量性判定只能区分"字面量常量"与"非字面量"，**无法证明非常量参数受攻击者控制**；
- 若把"非常量 → 受控 → L2 送 AI"作为默认，则 P0-2 收益取决于 138 条中非常量占比（未实测）；
- 若把"非常量 → 不受控 → 降级"，则引入新的漏报面（如 RN bridge 等真实可控点）。

### 2.2 实证（索引级抽查，支撑修订）

对基线 run `index/analysis.sqlite3` 中 `removePref` 全部 20 个调用点解压 `arguments_json`：

| 参数形态 | 调用点数 | 示例 |
|---|---|---|
| 字面量字符串 | 3 | `"back_url"` / `"back_name"` / `"backurl"`（XmAdUtil.removeAdBackInfo） |
| 编译期常量引用（static final） | 14 | `AccountConstants.PREF_C_UID`、`Constants.HomePageVersion.VersionType`、`PREF_MODE_LASTTIME`、`"mine_page_version"` |
| **非字面量（变量/表达式）** | **3** | `str`（PlayUtils.savePlayPosition 入参）、`safeReadableMap.getString("key")`（PreferenceHandler.handleRemovePreference，RN bridge——**真实外部可控点**） |

结论：**常量性判定在数据上完全可行**（解压 arguments_json 即可判字面量；常量引用需 reaching-definition 或符号表二次判定），且**本规则 20 个 removePref 调用点中约 85% 为常量**——P0-2 按"常量 → L1 signal"的收益依然接近 140→2 的上限；同时非字面量样例恰好包含真实面（RN bridge），证明"非常量 → 保守 L2"是正确的漏报防护方向。

### 2.3 修订建议

将 P0-2 判定表第三档改述为（语义精确化 + 实现可判）：

| flow_kind | 判定（按序） | 出链 |
|---|---|---|
| `source_to_sink` 且 `reaching_argument_indices` 非空 | — | L2 送 AI |
| `control_to_sink` 且 **sink 关键参数被证明为字面量常量**（arguments_json 解压 + reaching-definition 确认） | 不受控 | **L1 signal** |
| `control_to_sink` 且参数**非字面量**（含常量引用待定、变量、表达式） | 未证明受控 | **保守 L2 送 AI** |
| `inferred_source_to_sink`（LEGACY_FLOW_FALLBACK） | — | L1 signal |

并补充：
1. 落地前先对 138 条做**全量常量性实测**（本审查仅抽查 removePref 一个方法族），把"非常量占比"写进方案，作为收益与召回风险的量化依据；
2. 明确「常量引用」（`AccountConstants.PREF_UID` 类）的判定路径：走 reaching-definition 求值到字面量，求值失败按"非字面量"保守处理；
3. 降级原因用结构化字段记录（`demotion_reason=const_sink_argument` / `non_literal`），供 §5 回归审计。

---

## 3. 修订点 ②（关键）：P1-5 决策层采信必须与注入事实交叉验证，否则产生假阴性面

### 3.1 问题

方案 §4-3 把 `refutation_basis`"纳入确定性背书来源"。若决策层**直接采信 AI 的 refutation_basis**（如 AI 声称"provider 非导出""目标固定本包"），而该断言恰好是幻觉，则真漏洞会被判成 `ai_false_positive`——把当前"97% 误报"问题翻转为**漏报**，方向更坏。l2-review 3.0.5 已有先例教训：AI 自标 `evidence_refs` 无效必须拒绝（`AI_EVIDENCE_REF_INVALID` 在 `_EVIDENCE_INSUFFICIENCY_GAPS` 白名单外）。

### 3.2 修订建议

§4-3 改为显式交叉验证规则：

- 决策层**只采信与 P1-4 注入的确定性事实一致的 `refutation_basis`**：
  - `provider_exported=false`（注入事实）↔ AI basis=`non_exported_provider` → 采信；
  - 注入事实缺失或与 AI basis 矛盾 → **不采信**，维持 `pending_manual`；
- `refutation_basis` 枚举值必须与注入事实字段一一对应（单一来源，遵循项目"同一语义单一来源"约定）；
- 保留既有联合裁决边界：确定性冲突、AI 自标证据无效仍然否决，不放宽。

---

## 4. 修订点 ③：§8 验收指标口径矛盾

### 4.1 问题

P0-2 生效后本规则送 AI 仅 2 条，此时"AI unresolved ≤60%"（=1 条）与"refutes ≥30%"（=0.6 条）**无统计意义**；而验收表现状列是 136 条（P0-2 关闭口径）。两个口径混在同一张表。

### 4.2 修订建议

- 验收表注明**双口径**：
  - **口径 A（P0-2 灰度期，默认关闭）**：以基线 run 136 条复算 AI 指标（unresolved ≤60%、refutes ≥30%），验证 P1-4/5 本身的效果；
  - **口径 B（P0-2 生效后）**：只验收数量指标（送 AI ≤5、pending_manual ≤20、被降级集合真漏洞 = 0）；
- 或在表头注明"除数量指标外，AI 质量指标均在 P0-2 默认关闭状态复算"。

---

## 5. 修订点 ④：两处次要补充

### 5.1 P0-1 的作用域边界需展开

方案只提 `block_end_line`（if 块近似）。建议明确以下结构的处理策略（均可沿用"保守近似 + 缺失带 gap"原则）：
- **else 块**：untrusted 条件为 false 时 else 内代码同样受控——`block_end_line` 方案只覆盖 then 块；
- **循环**（while/for 条件受控）：循环体应为独立作用域；
- **switch-case**：case 区域边界；
- 明确带 `CONTROL_SCOPE_UNRESOLVED` gap 的链**仍参与 P0-2 降级**（降级原因记 `scope_unresolved`），避免两类降级混淆回归审计。

### 5.2 历史回归数据基础薄弱

现存 run 仅 1 个（110600Z），§5.2"对现有全部 run 产物离线重放"实际只有基线自身（其 140 条人工标签可用，但覆盖面有限）。建议：
- 将**全局 `ai-cache/entries` 822 条**（跨 run 真实 AI 输入/输出，含 8-14 后 run 的产物）纳入离线重放样本；
- §5.3 的"至少 3 个不同应用"是硬要求，建议优先选取与小米商城编码风格差异大的样本（混淆程度、插件体系、RN bridge 使用），以验证 `control_to_sink` 占比 98.6% 的普适性。

---

## 6. 修订建议汇总（供方案作者直接执行）

| # | 修订 | 涉及方案章节 | 动作 |
|---|---|---|---|
| 1 | P0-2 判定表改「字面量常量 → L1 signal；非字面量 → 保守 L2」；补充 138 条全量常量性实测 | §2 | 改判定表 + 前置实测任务 |
| 2 | P1-5 决策层只采信与注入事实一致的 `refutation_basis` | §4-3 | 加交叉验证规则 |
| 3 | §8 验收指标双口径（灰度期 / 生效后） | §8 | 拆分指标 |
| 4 | P0-1 补充 else/循环/switch 作用域策略 + 降级链仍参与 P0-2 | §1 | 补充实现边界 |
| 5 | 历史回归纳入全局 ai-cache 822 条；多 APK 样本强调风格差异 | §5 | 扩充数据源 |
| 6 | 常量性判定降级原因结构化（`demotion_reason`） | §2/§5 | 字段设计 |

---

## 7. 结论

方案是**当前最完整的误报治理路径**：根因对应完整（A–G 全覆盖）、数字全部实测可复现、工程护栏（配置开关默认关闭、硬门槛、多 APK 验证）严谨。本审查**不推翻任何既定方向**，修订点集中在两处实现缺口（P0-2 判定语义、P1-5 采信规则）与两处口径问题（验收指标、回归数据源）。按 §6 修订后即可进入 P0-1 实施。

> 附：本审查的 removePref 常量性抽查数据可复现路径——`index/analysis.sqlite3` → `call_sites` 表（`arguments_json` 为 zlib 压缩 JSON 数组），查询条件 `method_name='removePref' AND resolved_target_id LIKE '%PreferenceUtil%'`，共 20 条。
