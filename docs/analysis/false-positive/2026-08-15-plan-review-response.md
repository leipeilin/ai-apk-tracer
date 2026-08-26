# 对《误报治理生产优化方案审查意见》的答复

> **答复对象**：`docs/analysis/precision/2026-08-15-precision-optimization-plan-review.md`
> **答复日期**：2026-08-15
> **方法**：复现审查报告的每一项实证（索引级 SQL + 候选产物复算），并将其核心建议在 138 条 `control_to_sink` 上做全量落地推演
> **总体表态**：**部分认可**。审查的 §3（P1-5 交叉验证）、§4（验收双口径）、§5（作用域边界、回归数据源）**完全正确，应直接采纳**。但 **§2（修订点①，被其自评为"关键"）建立在一次取错层级的抽查上，其修订建议会使 P0-2 收益从 140→2 退化为 140→57，且这 57 条按 v04 实证全部是误报**。该建议不应采纳原文，需按本文 §2.4 改写。

---

## 1. 审查报告的正确部分（认可，直接采纳）

| 审查条目 | 我的复核 | 处置 |
|---|---|---|
| §1 结论总表全部 9 项"实测命中" | ✅ 逐项复现一致（141 control_to_sink、274−141=133、27 组、chain_id 120、dataflow.py:2729 等） | 无异议 |
| §2.2 `removePref` 20 个调用点抽查 | ✅ SQL 可复现，20 条精确命中 | 数据真实（但用法有误，见 §2） |
| **§3 P1-5 决策层须交叉验证** | ✅ **完全正确且重要** | **采纳** |
| **§4 §8 验收指标双口径矛盾** | ✅ 确实矛盾 | **采纳** |
| **§5.1 P0-1 需覆盖 else/循环/switch** | ✅ 原方案确有遗漏 | **采纳** |
| **§5.2 历史回归仅 1 个 run** | ✅ 属实（`.ai-apk-tracer/runs/` 下只有 110600Z） | **采纳** |

§3 尤其关键：让决策层无条件采信 AI 自报的 `refutation_basis`，等于把"97% 误报"翻转成漏报。审查提出的"只采信与 P1-4 注入事实一致的 basis，矛盾或缺失即维持 `pending_manual`"是正确的护栏，与项目既有的联合裁决边界一致。

---

## 2. 不认可：审查 §2（修订点①）取错了统计层级

### 2.1 审查的抽查对象错了

审查用这条 SQL 支撑其全部结论：

```sql
select ... from call_sites
where method_name='removePref' and resolved_target_id like '%PreferenceUtil%'
```

这查的是**「谁调用了 `PreferenceUtil.removePref`」**（wrapper 的调用点，20 条）。

但规则产物里，候选的 **sink 不是这些调用点**，而是 **wrapper 函数体内部的那一行**：

```json
{
  "path": "com/xiaomi/shop2/util/PreferenceUtil.java", "line": 219,
  "text": "editorEdit.apply(...)", "method_name": "apply",
  "method_id": ".../PreferenceUtil.java#PreferenceUtil.removePref:210"
}
```

138 条 `control_to_sink` 的 sink `method_name` 分布是：`apply` 33、`commit` 31、`remove` 15、`putBoolean` 8、`putString` 9、`putLong` 1、`startActivity` 11、`query` 24、`update` 6 —— **全是 `SharedPreferences.Editor` / `ContentResolver` 的终端 API，不是 `removePref` 这类 wrapper**。

### 2.2 直接后果：审查的"85% 为常量"推不出方案收益

我按候选**真实 sink 行**去索引取参数：

```
control_to_sink 138 条的 sink 调用点参数形态：
  含字面量        : 0
  非字面量 / 无参 : 138   ← apply()/commit() 本来就是零参
  索引未命中      : 0
```

`apply()`、`commit()` 是**零参调用**，`putString(str, str2)` 的参数是 wrapper 的形参名。**在 sink 层做"字面量常量判定"恒为假**，审查建议的"常量 → L1 signal"档**一条都命中不了**，反而全部落进"非字面量 → 保守 L2 送 AI"。

顺带一提，审查自己的分类计数也有小偏差：它称"字面量 3 / 常量引用 14 / 非字面量 3"，实际复算为 **字面量 5 / 常量引用 10 / 非字面量 5**（`"mine_page_version"` 出现 2 次被计为常量引用，`PlayUtils` 的 `str` 有 4 条被少计）。这不影响主结论，但说明该抽查未经复核。

### 2.3 若按审查原文实施：收益从 140→2 退化为 140→57

我把审查建议的判定表在 138 条上做了全量推演。要让"常量性判定"有意义，必须**穿透 wrapper**——用 `propagation_paths` 中 `resolved_target_id == sink.method_id` 的那一跳，定位真实业务调用点（实测 **138/138 全部可定位**，路径数据完整）。在这个正确层级上：

| sink 关键参数形态 | 条数 | 审查建议的处置 |
|---|---|---|
| 字面量常量（`"back_url"`、`"mine_page_version"`） | **36** | L1 signal |
| 常量引用（`AccountConstants.PREF_UID` 等） | **45** | 需 reaching-definition 求值，失败则保守 L2 |
| 非字面量（`str`、`str2`、`arrayList`、`startInfoNew.server_time`） | **57** | **保守 L2 送 AI** |
| 无法判定 | 0 | — |

**送 AI 量：57 条**（原方案 2 条）。而这 57 条的构成是：

```
PluginInfoManager.java  30   ← v04 §1.4 判定：误报（provider exported=false，无外溢）
SplashCommonUtils.java  11   ← v04 §1.3 判定：误报倾向（目标固定 NavigationActivity）
PreferenceUtil.java     12   ← v04 §1.1 判定：误报（g_utm 只到 ShopApp.instance.utm）
ADBDebugActivity.java    4   ← v04 §1.5 判定：不成立（debuggable=false 确定性阻断）
```

**按 v04 动态验证，这 57 条无一成立。** 审查的建议在本 run 上不仅没有换来任何召回，还把 AI 成本与人工复核量放大了 28 倍。

其中 ADBDebugActivity 4 条尤其说明问题：它们已被 `guard_blocked` 确定性阻断（`_pipeline_requires_ai` 直接返回 False），却因为参数 `0` 不是字面量字符串而被判为"未证明受控 → 保守 L2"。这暴露了"非字面量 = 可能受控"这个推断本身的粗糙。

### 2.4 我的反建议：判据用 taint 事实，不用参数字面量

审查§2.1 有一句判断是对的：**「非常量 ≠ 受控」**。但它由此推出"非常量就保守送 AI"，方向反了——正确的结论是**参数字面量性根本不适合作为主判据**。

理由：`control_to_sink` 的定义（`dataflow.py:601-625`）已经明确"**没有任何 untrusted 值到达 sink 参数**"（`reaching_argument_indices: []` 是写死的）。这是 taint 引擎给出的**确定性事实**，比"参数长得像不像常量"强得多。参数是 `str` 只说明它是个变量，不说明它被攻击者控制。

修订后的判定表：

| 序 | 判据 | 出链 |
|---|---|---|
| 1 | `flow_kind=source_to_sink` 且 `reaching_argument_indices` 非空 | **L2 送 AI** |
| 2 | `flow_kind=control_to_sink` 且 **P0-1 修复后 `control_fact` 仍在 sink 支配域内** | **L2 送 AI** |
| 3 | `flow_kind=control_to_sink` 且 sink 在分支块外（P0-1 判定） | **L1 signal** |
| 4 | `inferred_source_to_sink`（`LEGACY_FLOW_FALLBACK`） | **L1 signal** |

即：**把"是否降级"交给 P0-1 的作用域分析，而不是参数字面量**。这也回到方案原本的设计意图——P0-1 必须先行，P0-2 只是消费它的结论。

参数常量性判定**保留但降级用途**：作为 P1-4 注入给 AI 的确定性事实之一（"sink 参数为常量 `back_url`"是很有价值的排除依据），而非出链等级的裁决者。

### 2.5 采纳审查的两个附带建议

- ✅ **落地前做 138 条全量实测**——审查这个要求是对的，本文 §2.3 已完成（36/45/57 分布）。
- ✅ **`demotion_reason` 结构化字段**——采纳，取值改为 `scope_out_of_block` / `legacy_fallback` / `guard_blocked`，与 §2.4 判据对齐。

---

## 3. 修订汇总（相对审查 §6）

| # | 审查建议 | 我的处置 |
|---|---|---|
| 1 | P0-2 改「字面量→signal；非字面量→保守 L2」 | ❌ **不采纳原文**，改为 §2.4 判定表（判据用 P0-1 作用域事实）；✅ 采纳"全量实测"要求（已完成） |
| 2 | P1-5 只采信与注入事实一致的 `refutation_basis` | ✅ **采纳** |
| 3 | §8 验收指标双口径 | ✅ **采纳** |
| 4 | P0-1 补 else/循环/switch + 降级链仍参与 P0-2 | ✅ **采纳** |
| 5 | 回归纳入全局 ai-cache 822 条；多 APK 强调风格差异 | ✅ **采纳**（822 条为跨 run 缓存，可作离线重放样本） |
| 6 | `demotion_reason` 结构化 | ✅ **采纳**，取值按 §2.4 调整 |

---

## 4. 结论

审查报告的**工程判断力是可靠的**——它独立发现了 P1-5 的漏报风险（这是原方案真实的设计缺口）、验收口径矛盾、作用域覆盖不全、回归样本不足，这四条都应当直接进方案。

但它的"关键修订点①"栽在一个具体而典型的错误上：**抽查取的是 wrapper 调用点，而候选的 sink 在 wrapper 函数体内部**。两者相差一层调用，导致"85% 常量"这个看似有力的数据无法支撑其结论，按原文实施反而会把送 AI 量从 2 条抬到 57 条、且这 57 条全部是 v04 已证的误报。

这件事本身也印证了方案 §5 的设计前提：**任何基于单点抽查的阈值决策都必须做全量推演验证**。审查要求方案作者"落地前对 138 条做全量常量性实测"是完全正确的——只是这个要求同样适用于审查自己的抽查。
