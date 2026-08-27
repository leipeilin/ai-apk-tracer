# 核验报告：P6 E8 工程瑕疵批量清理（ruleset-quality-review）

> **任务编号**：P6（E8 六项：词表/provider gap/多命中/版本语义/severity 单源/死代码）
> **核验日期**：2026-08-27
> **核验对象**：`docs/analysis/rules-review/2026-08-27-p6-e8-cleanup.md` 及其变更
> **核验模型**：deepseek-v4-pro（独立子 agent，只读评审，49 次工具调用）
> **状态**：第 1 轮（已闭合）

---

## 1. 核验结论摘要（子 agent 原文）

E8-4（版本语义声明）、E8-5（severity 单源化，33/33 静态逐条一致）、E8-6（GUARD_RE 全仓零代码引用）三项达标；E8-1（词表扩展）实施正确但正则无词边界、误报面与"与 SENSITIVE_DATA_RE 对齐"的表述均有出入；E8-3（多命中全枚举）主体等价性成立——首个命中仍产出、行号经偏移保持的 sanitize 计算正确、排序确定、无重复/遗漏——但 11 个分支中有 5 个 sanitize 匹配分支的 `text` 提取仍用 `pattern.search(code)` 取原文首个匹配，多命中时第 2..N 个候选的证据文本全部错误；最严重的是 E8-2：`LEGACY_INDEX_SCOPE` gap 在 execute() 正确构造并传入 `_component_rule`，但 `_component_rule` 从不调用 `analyze_entry`、`guard_coverage` 不读取 `self.gaps`、`semantic_gaps` 恒为空，gap 在此路径是死端，永远不会出现在候选的 coverage_gaps/blocking_gaps——实施报告"gaps 经 scope_gaps 链路随候选上报"的断言与代码事实不符，且全仓无任何 LEGACY_INDEX_SCOPE 测试。

## 2. 问题清单（子 agent 提出）

**【R-1】【高】** E8-2 核心目标未达成：LEGACY_INDEX_SCOPE gap 在 `_component_rule` 路径是传递死端（gap 构造正确、传入 `DataFlowAnalyzer(scope_gaps=...)` 后仅存 `self.gaps`，但 `_component_rule` 只调 `guard_coverage`（不读 `self.gaps`），候选 `coverage_gaps` 来自 `special_metadata.pop` 恒空；对照组 COMPONENT_FLOW_ENTRIES 路径经 `analyze_entry`→`_finalize` 面市）。测试佐证缺失（全仓 LEGACY_INDEX_SCOPE 0 命中）。
**【R-2】【中】** E8-3 改造不彻底：5 个 sanitize 分支（FILE_ACCESS/UNIVERSAL/SSL/TRUST/VERIFIER）的 `text` 仍 `pattern.search(code).group(0)` 取原文首个匹配——多命中时第 2..N 个候选证据文本错误；且原文首个匹配位于注释时 text 取到注释文本；`setAllowFileAccess/*x*/(true)` 形态下 `search(code)` 返回 None 使 `.group(0)` 抛 AttributeError。
**【R-3】【低】** E8-1 词表无词边界：KeyEvent/Keyboard/Hotkey/Monkey、Concert、Author、Repay 命中 auxiliary 信号；"与 SENSITIVE_DATA_RE 对齐"表述不准确（Login/Pay/Cert/Key 不在该词表）。
**【R-4】【低】** EXTERNAL_CONTENT 的 loadUrl 匹配无注释排除（违反 docstring 自述的字符串参数类策略）；`break` 写法误导（应提为前置 guard）。
**【R-5】【低】** 两处代码注释 stale（execute:505 与 `_webview_bridge_artifact_records` docstring 的"候选单 match 行为不动"）。
**【R-6】【低】** 报告数字与静态事实不符（62/62 实为 59 个测试函数）；pytest 无法在该核验环境独立执行。
**【R-7】【低】** E8-5 一致性测试单向覆盖（yaml→META），反向（META 死注册）不校验。

## 3. 认可项（节选）

1. E8-5 全量静态核对通过（33/33 一致、测试逻辑正确、rule_runner 无 schema 冲突）；
2. E8-6 通过（GUARD_RE 零代码残留）；
3. E8-4 通过（版本语义与平台事实一致、不改窄 android_api 的决策正确）；
4. E8-3 匹配语义等价性成立（finditer 插入序即旧行为、注释排除策略一致、`_match` helper 保持 None 语义、调用点唯一无重复、`search_for_rule` 文件去重 + Python 稳定排序保证输出确定）；
5. EXTERNAL_CONTENT 的 js_enabled 前置条件语义保持（sanitized 判定 + 文件级前置 + 多 loadUrl 全枚举）；
6. 回归面静态核查未见破坏（4 处单候选断言均单调用点源码；`test_detector_webview_artifact_multi_bridge` 只断言 artifacts——候选 1→2 不破坏断言且口径就此对齐）；
7. E8-1 分支位置正确（词表判定先于 exported 过滤，与 MANIFEST_ONLY 空文件路径兼容）；
8. E8-2 构造侧正确（与 COMPONENT_FLOW_ENTRIES 无索引分支同口径）——缺陷仅在面市断链。

## 4. 边界检查表（子 agent 原文）

| 核验项 | 结论 |
|---|---|
| E8-1 词表扩展 | 有条件通过（R-3） |
| E8-2 provider 无索引 gap | 不通过（R-1 死端） |
| E8-3 多命中全枚举 | 有条件通过（R-2/R-4/R-5） |
| E8-4 版本语义 | 通过 |
| E8-5 severity 单源化 | 通过（R-7 留改进） |
| E8-6 GUARD_RE 删除 | 通过 |
| 回归核对 | 有条件通过（R-6 数字待澄清） |

---

## 5. 处置记录（主 agent 回填，2026-08-27）

| 编号 | 严重度 | 处置 | 落点 |
|---|---|---|---|
| R-1 | 高 | **采纳（已实施）**：通用尾部 `result["coverage_gaps"]` 合并 scope_gaps；新增 `_attach_scope_gaps` helper 供早 return 分支（NAME_HINT 已接入）；新增测试 `test_scope_gaps_merged_into_candidate_coverage_gaps`（经 `_component_rule` 直调断言 LEGACY_INDEX_SCOPE 面市到 coverage_gaps） | detector.py、测试 |
| R-2 | 中 | **采纳（已实施）**：5 处 sanitize 分支 `text` 统一改 `code[match.start():match.end()]`（按 sanitized 匹配跨度切原文——sanitize 保字符数）；新增 `test_multi_match_texts_are_per_call_site` 与 `test_sanitize_match_survives_comment_only_source`（None 崩溃路径消除） | detector.py ×5、测试 |
| R-3 | 低 | **采纳（已实施）**：词表改驼峰/下划线/点分 token **整词匹配**（`re.split` 拆 token 后集合交集）；`key` 从词表移除（KeyEvent/Keyboard/Hotkey 拆词后 Key 为独立 token 仍整词命中，UI 组件高频词误报面大于收益）；Concert/Author/Repay 类由整词匹配自然排除；报告"与 SENSITIVE_DATA_RE 对齐"表述更正为"部分对齐"；新增良性驼峰名负断言测试 | detector.py、测试 |
| R-4 | 低 | **采纳（已实施）**：EXTERNAL_CONTENT 循环内补 `_comment_line` 排除；`if not js_enabled: break` 提为循环前 guard（`return matches`）；新增注释掉的 loadUrl 负断言测试 | detector.py、测试 |
| R-5 | 低 | **采纳（已实施）**：两处 stale 注释更新为"候选与产物均 finditer 全枚举（口径一致）" | detector.py ×2 |
| R-6 | 低 | **采纳**：报告数字更正（新增 6 用例后共 65 个测试函数；口算 62 为笔误）；pytest 由主 agent 环境执行留档（`1295 passed / 0 failed`，排除 explorer 并行 WIP） | P6 报告 §2 |
| R-7 | 低 | **采纳（已实施）**：`test_severity_assertion_is_bidirectional` 反向断言（33 个 yaml id 集合 == RULE_META 键集合，防死注册） | 测试 |

**闭合结论**：R-1~R-7 全部采纳并实施。核验后测试：`test_webview_crypto_rules.py` 65/65（新增 TestP6VerificationFixes 6 用例）；全量 **1295 passed / 0 failed**（40.07s，排除 test_explorer.py 并行 WIP——见 P5 核验报告声明）；同步校验 PASS 82 / CONFLICT 0 / ORPHAN 0。E8-2 的 gap 面市链路经测试锚定（早 return 分支 + 通用尾部双路径）。
