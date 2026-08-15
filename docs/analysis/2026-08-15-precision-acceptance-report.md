# §8 验收指标复算报告（2026-08-15，v2 多 APK 扩充）

> **目的**：按方案 §8 双口径复算。口径 A（P0-2 关闭）验证 P1-4/P1-5 AI 质量效果；口径 B（P0-2 生效）验证数量收敛。基线 run：`20260809T110600Z_1c55d3fb9f95_98fbe158`（com.xiaomi.shop 5.53.0）；多 APK 样本：`20260815T124147Z`（com.xiaomi.shop 复扫）、`20260815T125744Z`（com.mi.health）。

## 口径 B —— P0-2 生效后数量收敛（已实测复算）

**复算方法**：复用基线 run 现有索引（schema 2.9.0 = 当前版本）+ 当前规则代码重跑 `ACTIVITY_INTENT_TO_SENSITIVE_SINK`，再以当前 `unproven_flow_demotion_reason` 逻辑离线分级。

| 指标 | 现状（基线产物） | 复算结果 | 目标 | 达标 |
|---|---|---|---|---|
| 本规则候选数 | 140 | 140（重跑一致） | — | ✅ |
| 送 AI 候选数 | 136 | **1**（source_to_sink MainTabActivity） | ≤5 | ✅ |
| control_to_sink 降级 | 0 | **138/138 全部 scope_unresolved** | — | ✅ |
| inferred_source_to_sink 降级 | 0 | 1（legacy_fallback） | — | ✅ |

**关键机制确认**：138 条 control_to_sink **全部**带 `CONTROL_SCOPE_UNRESOLVED` gap——基线索引的 flow IR 由旧版本生成（无 `block_end_line`），P0-1 降级路径在规则侧检测到 IR 缺失即产 gap（`dataflow.py:451-455`），链不会判高可信 → `scope_unresolved` 降级全部命中。**P0-1 的"缺失带 gap"兜底在旧索引上直接生效**。

### P0-1 真实重跑实证（com.xiaomi.shop 复扫，8-15 新代码全量）

| 指标 | 基线（8-09 产物） | 复扫（8-15 新代码） | 结论 |
|---|---|---|---|
| control_to_sink | 141 | **33**（-77%） | ✅ P0-1 作用域化消除误链源头 |
| 候选总数 | 274 | 193 | 随规则演进 |
| bulk_statistics_consumer 降级 | 0 | 4（StatService2） | ✅ consumer_semantics 常量解析生效 |
| legacy_fallback 降级 | 1 | 1（WbShare） | 误报形态 |

**com.mi.health（风格差异样本）**：480 候选，control_to_sink 仅 **15**（不同编码风格），receiver_exposure 282——**证明 98.6% 误报率是电商样本特性，非普适**；该样本暴露 `legacy_fallback` 吞真漏洞风险（见 §5 守门报告 v2）。

## 口径 A —— AI 质量指标（P0-2 关闭）

**基线（修复前，8-09 产物）**：AI 分析的 142 条中 unresolved 99.3%（141）、refutes 0.4%（1）、`refutation_basis` 输出 0 条。

**多 APK findings 决策分布（8-15 新代码，含 P1-4/P1-5 修复）**：

| 样本 | findings | unresolved | ai_likely_supported | exposure_only | 其他 |
|---|---|---|---|---|---|
| com.xiaomi.shop 复扫 | 160 | 90 | 17 | 45 | mixed 6 / deterministically_refuted 1 / blocked 1 |
| com.mi.health | 461 | 308 | 63 | 90 | — |

| 指标 | 现状（修复前基线） | 目标 | 复算状态 |
|---|---|---|---|
| AI unresolved 占比 | 99.3%（基线） | ≤60% | ⏳ 8-15 新代码样本仍以 unresolved 为主（shop 90/160、mi.health 308/461），需按口径 A 严格口径重跑统计 |
| AI refutes 占比 | 0.4% | ≥30% | ⏳ 需真实 AI 重跑统计 |
| 切片含 sink 上下文比例 | 待实测（见注） | 100% | ✅ 机制已加固（SINK_CONTEXT_UNAVAILABLE gap + 按需加载，本轮落地） |
| refutation_basis 交叉验证通过率 | 0%（无 basis） | ≥80% | ⏳ 需真实 AI 重跑 |

> **注（2026-08-15 勘误）**：方案 §8 引用的"切片 8 context 无一是 sink 文件 PreferenceUtil.java（slice_bb21709c）"经复核为**误读**——该切片对应候选的 sink 实际是 `SplashCommonUtils.java:128`（startActivity），本就不该含 PreferenceUtil。真实流水线 `build_code_index` 全量索引，sink 文件几乎总在 `self.files` 中（走正常分支）。P1-4 的实际价值是**防御性**：覆盖"文件在索引中但未进 files"的边界（未来 scope 子集索引），且无法加载时产精确的 `SINK_CONTEXT_UNAVAILABLE` gap 而非通用 `PATH_NOT_INDEXED`。

> ⚠️ **口径 A 的复算依赖真实 AI 调用**（136 条 × 3.0.7 prompt + P1-4 事实注入切片），耗时与成本高。机制侧已全部就绪（P1-4 事实注入、P1-5 交叉验证、3.0.7 协议放开），但**模型行为未实测**——这是方案遗留的最大未验证项，见"遗留"。

## 与 §5 守门的衔接

- **被降级集合真漏洞 = 0**：⚠️ **v2 已推翻**——com.mi.health 的 RouterActivity（legacy_fallback 降级）为 `ai_likely_supported` 疑似真漏洞，硬门槛**未通过**（详见 §5 守门报告 v2）。P0-2 **不得翻默认值**，且 `legacy_fallback` 分支需收紧。
- **P0-2 默认值**：维持 `false`——口径 A 未实测 + **legacy_fallback 吞真漏洞实证**，双重理由。

## 遗留

1. **口径 A 真实 AI 重跑**：需在 P0-2 关闭下用 3.0.7 重跑（或抽样），实测 unresolved/refutes/basis 通过率——**最高优先未验证项**。
2. **`legacy_fallback` 降级收紧（v2 新增，P0 级）**：mi.health RouterActivity 实证 `inferred_source_to_sink` 降级会吞掉 AI 已判定成立（flaw_holds=True）的候选。建议取消该分支无条件降级，或仅降级无 AI 判定依据的候选。
3. **多 APK 验证**：已补齐 2 个新样本（shop 复扫 + mi.health），control_to_sink 占比证实非普适（15~33 vs 141）；仍需更多样本（RN bridge 等）确认 receiver_exposure 282 条（mi.health）是否为规则噪声。
4. ai-cache 822 条不含候选输入（仅哈希），且全部为 8-09 前产物（prompt ≤ 3.0.4），无法作为口径 A 重放源。
