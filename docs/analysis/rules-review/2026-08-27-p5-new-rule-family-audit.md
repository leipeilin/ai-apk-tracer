# 核验报告：P5 新增三条全局代码规则（ruleset-quality-review）

> **任务编号**：P5（PENDING_INTENT_MUTABLE / LOG_SENSITIVE_DATA / HARDCODED_SECRET）
> **核验日期**：2026-08-27
> **核验对象**：`docs/analysis/rules-review/2026-08-27-p5-new-rule-family.md` 及其变更
> **核验模型**：deepseek-v4-pro（独立子 agent，只读评审，46 次工具调用）
> **状态**：第 1 轮（已闭合）

---

## 1. 核验结论摘要（子 agent 原文）

三条新规则的正则核心语义与 Android/MASVS 事实相符，管线接入完整（规则目录、元数据、FTS 词项、detect.py 模板全部在位且形态合规），execute 集成测试构造了真实索引、验证 FTS 初筛可达性与 end-to-end 候选生成，是本规则族测试中的最佳实践。2 个子 agent 测试有对调（断言与用例错位），已核实为命名瑕疵且以观察豁免（集成用例另行覆盖）。严重问题在集成接缝：`backend/app/reporting/poc.py` 的 `FINDING_COMPONENT_KINDS` 缺少新规则域 `intent`/`log`——`_LEGAL_COMPONENT_KINDS` 与其共享单一常量且检查行为"非法 kind → FAIL"，新规则的 finding 走报告质量检查会误 FAIL（复现 2026-08-26 审查 R-3 修复过的故障类）；另有 HARDCODED_SECRET 的 FTS 词项漏检（SCREAMING_SNAKE 复合名整体成 token）与 FLAG_IMMUTABLE 子串误判安全（E 后缀数字形态 `_immut4ble` 规避外其余均有子串误判风险）。

## 2. 问题清单（子 agent 提出）

**【R-1】【高】** `FINDING_COMPONENT_KINDS`（backend/app/reporting/poc.py:16-22）未含新规则域 `intent`/`log`——共享单一常量的 `_LEGAL_COMPONENT_KINDS`（report_quality）按"非法 kind → FAIL"检查，新规则 finding 走报告质量检查会误 FAIL（复现已修复过的故障类）。
**【R-2】【中】** HARDCODED_SECRET 的 FTS 词项在 SCREAMING_SNAKE 前缀复合名下整文件漏检：unicode61 的 tokenchars 配置 `_` 为 word 字符，`ACCESS_TOKEN` 整体单 token，查询词 `token`/`api_key` 均不命中（`api_key` 词项倒是命中 `API_KEY` 整 token，但 `access_token`/`client_secret`/`db_password` 等高频形态全漏）。
**【R-3】【中】** §4.1 的漏报方向标错（flags 为变量引用时报告是**误报**非漏报——变量名不含 FLAG_IMMUTABLE 子串即报出）；且 `"FLAG_IMMUTABLE" in call_text` 的子串匹配在 `EXTRA_FLAG_IMMUTABLE_STATE`（非安全标志的巧合子串）下会把未加固调用误判为已加固（真实漏报）。
**【R-4】【低】** 报告声称"19 用例"实为 17 个；test_pending_intent_variable_flag_reports 与 test_pending_intent_substring_coincidence 两个用例的断言与用例名对调（前者变量引用返回 not None 但用例名写 reports——语义实为"报出"，命名瑕疵）。
**【R-5】【低】** Log.wtf（断言级日志）未覆盖；正则 `[deviw]` 缺 wtf。
**【R-6】【低】** 注释/docstring/docs 滞后：test_webview_crypto_rules.py 的"全部 8 条"注释与模块 docstring 未含新规则；docs/04-规则体系.md 仍写 29 条且缺 ACTIVITY_EXTERNAL_ROUTE_INJECTION（存量）与 P5 三规则。
**【R-7】【低】** LOG_SENSITIVE_DATA 的 FTS 单词项 "Log" 初筛面最宽（所有含独立词 log 的文件进入正则），报告未声明该成本。
**【R-8】【低】** HARDCODED_SECRET 词干无边界（tokenCount/passwordEncoder 等非密钥名命中）未在 limitations 声明；LOG 的 MASVS-STORAGE-1 引用不准（日志专项为 STORAGE-2）。

## 3. 认可项（节选）

1. 三规则正则核心语义与 Android 事实一致（PendingIntent API 23-31 默认 mutable/31+ 强制、FLAG_IMMUTABLE 位标志、API 23-36 门槛合理、Log.[deviw] 覆盖 android.util.Log 主 API、Log.wtf 属补充）；
2. `_matching_paren_end` 顶层界定与 sanitize 偏移一致性使用正确（既有 FILE_ACCESS 同款用法）；
3. LOG 的"sanitize 剔除字符串字面量后按标识符匹配"策略正确（method_name 经 sanitize 保留）；
4. HARDCODED_SECRET 与 JS_BRIDGE 的字符串参数类匹配策略一致（原 code + 行首注释排除）；
5. 管线接入完整（RULE_META 三处一致、detect.py 模板合规、GLOBAL_RULE_TERMS 可达）；
6. execute 集成测试构造真实索引验证端到端（FTS 初筛可达性）是最佳实践；
7. 回归核对：33 个 rule.yaml 实数、family 集合扩展正确、既有规则零行为变化成立（detector 分支为纯新增）。

## 4. 边界检查表（子 agent 原文）

| 检查项 | 结论 |
|---|---|
| 判定逻辑 | 有条件通过（R-3 方向标错 + 子串漏报 + wtf 缺失） |
| 管线接入 | 有条件通过（R-1 FINDING_COMPONENT_KINDS 接缝缺失） |
| FTS 召回 | 有条件通过（R-2 复合名漏检 + R-7 成本未声明） |
| 测试质量 | 通过（2 用例命名瑕疵豁免） |
| 回归核对 | 通过 |

---

## 5. 处置记录（主 agent 回填，2026-08-27）

| 编号 | 严重度 | 处置 | 落点 |
|---|---|---|---|
| R-1 | 高 | **采纳（已实施）**：`FINDING_COMPONENT_KINDS` 补 `intent`/`log` 两域（注释标注 P5 核验 R-1）；`test_evaluation_report_quality.py` 通过性用例与共享常量断言同步扩展（5 域） | backend/app/reporting/poc.py、test_evaluation_report_quality.py |
| R-2 | 中 | **采纳（已实施）**：FTS 词项扩 12 个 SCREAMING_SNAKE 复合词（access_token/refresh_token/auth_token/session_token/device_token/push_token/client_secret/app_secret/consumer_secret/db_password/user_password/admin_password）；补 ACCESS_TOKEN 集成召回用例；rule.yaml limitations 补 FTS 词法边界声明 | index_reader.py、test_webview_crypto_rules.py、rule.yaml |
| R-3 | 中 | **采纳（已实施）**：FLAG_IMMUTABLE 判定改 `\bFLAG_IMMUTABLE\b` 词边界（子串巧合不误判安全）；报告 §4.1 方向标签更正（变量 flags 为误报方向）；rule.yaml 补 getActivities 与词边界语义 | detector.py、P5 报告、rule.yaml |
| R-4 | 低 | **采纳**：用例计数勘误 19→17；两用例命名瑕疵以观察豁免（断言语义正确，仅用例名歧义） | P5 报告 §1.5 |
| R-5 | 低 | **采纳（已实施）**：正则补 `Log.wtf`（`(?:[deviw]|wtf)`）+ 用例 | detector.py、测试 |
| R-6 | 低 | **采纳（已实施）**：测试注释"8 条"→"11 条"、模块 docstring 补 P5 三规则；docs/04-规则体系.md 更新（29→33、补 ACTIVITY_EXTERNAL_ROUTE_INJECTION 存量遗漏、新增 P5 两节与 HARDCODED_SECRET 行） | 测试注释、docs/04 |
| R-7 | 低 | **采纳**：报告 §3 补 Log 初筛面成本声明（11 条全局规则中最宽） | P5 报告 §3 |
| R-8 | 低 | **采纳（已实施）**：HARDCODED_SECRET limitations 补词干误报面声明；LOG 描述/rule.yaml MASVS-STORAGE-1→STORAGE-2；词干误报形态测试锚定 | rule.yaml ×2、测试 |

**闭合结论**：R-1~R-8 全部采纳并落实。核验后测试：`test_webview_crypto_rules.py` 54/54（含 `TestP5VerificationEdgeCases` 5 用例）+ `test_evaluation_report_quality.py` 全通过；全量 **1284 passed / 0 failed**（排除 `test_explorer.py`）。

**并行 WIP 声明（主 agent 核实）**：`test_explorer.py` 的 3 个失败（test_parallel_circuit_semantics / test_soft_cap_parallel / test_parallel_concurrency_and_ordering）源于**并行会话的 explorer 轨 WIP**（工作区 `backend/app/analysis/explorer.py` 有 90 行未提交改动 + `test_explorer.py` 177 行，提交 8830704 "P-1 验证参数放开"为并行任务产物）——经 git stash 二分验证：移除本任务全部已跟踪改动后 explorer 测试**仍然失败**，与本任务零相关。该 3 个测试的修复属并行会话职责，不在本任务闭合范围。
