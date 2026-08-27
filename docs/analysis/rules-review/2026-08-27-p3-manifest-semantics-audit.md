# 核验报告：P3 manifest 规则语义修正（ruleset-quality-review）

> **任务编号**：P3（E3/E4：NSC 覆盖 + 豁免机制 + 存量风险面）
> **核验日期**：2026-08-27
> **核验对象**：`docs/analysis/rules-review/2026-08-27-p3-manifest-semantics.md` 及其变更
> **核验模型**：deepseek-v4-pro（独立子 agent，只读评审，31 次工具调用）
> **状态**：第 1 轮（已闭合）

---

## 1. 核验结论摘要（子 agent 原文）

本次修复主体成立：NSC 覆盖降级方向、targetSdk 28/23 门槛与边界值、allowBackup 默认 true 与 targetSdk 解耦口径、dataExtractionRules/fullBackupContent 豁免入口的解析与"按属性存在判定"（正确地未按 targetSdk 门控——两属性生效取决于设备版本而非 targetSdk）均与 Android 官方语义一致；条件树无死分支、无顺序矛盾；解析层 `_attr`（android 命名空间）+ 非 `_bool` 的选型正确；新字段为纯增量，backend 内零消费点，`severity_hint "low"` 在 SEVERITIES 与 candidate schema 枚举中均受支持，且数据流确认新字段会实际进入规则运行时。残留问题集中在边缘组合语义（NSC 覆盖未对称应用于 <28 分支的显式 false）、"unknown"（资源引用）形态两规则处理不对称、targetSdk<23 备份存量面未覆盖、描述口径（adb backup vs Auto Backup 未按平台版本区分），均为中低严重度。

## 2. 问题清单（子 agent 提出）

**【R-1】【中】** NSC 覆盖语义只应用到 targetSdk>=28 分支，<28 存量分支的"显式 false 豁免"未按同一官方语义修正：官方忽略是双向的，"targetSdk<28 + 显式 false + NSC（未禁明文）"实际是明文放行但规则完全不报（detector.py:3558 未引用 has_nsc）。
**【R-2】【中】** "unknown"（资源引用形态）在两规则间处理不对称且无测试锚定：allow_backup=="unknown"+>=23 三分支全不命中（漏报方向）；uses_cleartext=="unknown"+<28 报 low、+>=28 不报——三个取舍各自可辩但互相不一致，报告 §4.1 自曝的边界无任何用例固化。
**【R-3】【低】** targetSdk<23 的备份存量风险面不覆盖且 limitations 未声明该残余缺口（adb backup 提取向量本身不受 23 门槛限制）。
**【R-4】【低】** 描述把 Auto Backup（云端，需用户账号恢复落地）与 adb backup（本地提取，Android 12+ 平台对非 debuggable 应用默认排除）混为一条路径。
**【R-5】【低】** ALLOW_BACKUP 存量分支未提示豁免属性存在；`_platform_assumptions` 未携带三个新事实，降级候选的 AI 复核上下文不完整。
**【R-6】【低】** manifest.py 注释"fullBackupContent（<API 31）"版本口径过简（实际适用性由设备版本决定，与 targetSdk 无关），易诱导后续错误加 targetSdk 门控。

## 3. 认可项（节选）

1. NSC 覆盖降级方向正确（降级 low + 描述声明需复核而非静默——同时消除假阳性方向并保留漏报侧信号）；
2. targetSdk 28/23 门槛与边界值正确（28 精确归入 >=28 分支、27/23 锚定测试在位）；
3. allowBackup"默认值与 targetSdk 解耦"口径与 Android 备份机制事实相符；
4. 豁免属性按"存在性"判定且不按 targetSdk 门控恰好正确（避开设备版本/targetSdk 混淆陷阱）；
5. 解析层 `_attr` 选型正确（resource 引用属性用 _bool 会丢引用）；
6. 条件树无死分支（True/False/None/unknown × targetSdk 全形态推演）；
7. 既有 medium 行为不变；既有测试预期更新符合评审 E3 原文方向（"low" 高于 informational 满足"至少 L1 informational"）；
8. 下游零影响核实成立（新字段无消费点、schema 枚举支持、数据流确认可达、旧 run 优雅回退）；
9. rule.yaml limitations 与代码行为一致；报告自我披露诚实（§4 与实际边界相符）。

## 4. 边界检查表（子 agent 原文）

| 检查项 | 结论 |
|---|---|
| 语义正确性 | 通过（R-1/R-4 为边缘组合与措辞残留） |
| 条件树 | 通过（unknown 不对称性归入 R-2） |
| 解析层 | 通过 |
| 测试覆盖 | 有条件通过（两个自曝边界无测试固化、数字无法独立复跑） |
| 下游影响 | 通过 |

---

## 5. 处置记录（主 agent 回填，2026-08-27）

| 编号 | 严重度 | 处置 | 落点 |
|---|---|---|---|
| R-1 | 中 | **采纳（已实施）**：CLEARTEXT 存量分支非报条件改为 `not (uses_cleartext is False and not has_nsc)`——显式 false 且无 NSC 才不报；描述与 limitations 同步声明 NSC 双向覆盖语义；新增测试 `test_cleartext_explicit_false_with_nsc_target_27_reports_stock` / `..._without_nsc_..._no_candidate` | detector.py、rule.yaml、测试 |
| R-2 | 中 | **采纳（已实施）**：ALLOW_BACKUP 存量分支扩展 `in (None, "unknown")`；CLEARTEXT unknown 行为锚定（2 用例）；ALLOW_BACKUP unknown 2 用例；limitations 声明资源引用口径 | detector.py、rule.yaml、测试 |
| R-3 | 低 | **采纳（声明）**：limitations 末尾声明"targetSdk<23 的旧备份机制风险面不在本规则覆盖范围"；对称扩 <23 分支留后续迭代 | rule.yaml |
| R-4 | 低 | **采纳（已实施）**：medium 与存量分支描述拆分 Auto Backup（云端）与 adb backup（本地提取，Android 12+ 平台对非 debuggable 默认排除）两条向量 | detector.py |
| R-5 | 低 | **采纳（已实施）**：存量分支描述追加豁免提示；`_platform_assumptions` 补三事实（`_fact` 字符串形态，缺失标 unknown）；测试断言覆盖 | detector.py、测试 |
| R-6 | 低 | **采纳（已实施）**：注释更正为"适用性取决于设备版本而非 targetSdk，故下游只按属性存在判定、不做 targetSdk 门控" | manifest.py |

**闭合结论**：R-1~R-6 全部采纳并落实。核验后测试：`test_manifest_fact_rules.py` 28/28（新增 `TestP3VerificationEdgeCases` 7 用例）；全量 **1296 passed / 0 failed**（39.66s，2026-08-27）。遗留：targetSdk<23 备份存量对称分支（R-3 后半）→ 后续迭代。
