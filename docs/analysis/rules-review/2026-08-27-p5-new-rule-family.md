# P5 任务实施报告：新增三条全局代码规则（PendingIntent / 日志泄露 / 硬编码密钥）

> **任务来源**：`docs/analysis/rules-review/2026-08-27-ruleset-quality-review.md` 第四节（契合度"高/中"的前三项）+ 第五节优先级 #5
> **实施日期**：2026-08-27
> **实施者**：主 agent（GLM-5.3）

## 1. 变更清单

### 1.1 PENDING_INTENT_MUTABLE（rules/intent/，L2，medium）

- **判定**：`PendingIntent.getActivity/getActivities/getBroadcast/getService/getForegroundService` 调用（`_matching_paren_end` 顶层括号界定）的调用文本不含 `FLAG_IMMUTABLE`——API 23+ 默认 mutable，底层 Intent 可被篡改（组件重定向/Intent 劫持面）；Android 12+ 强制显式声明（MASVS-PLATFORM-2）。
- **边界**：flags 为变量/常量引用同样报（无法静态判定，AI 复核兜底）；调用文本任意位置含 FLAG_IMMUTABLE 即视为已加固（含位或组合）。
- FTS 词项 `["PendingIntent"]`；rule.yaml `android_api: "23-36"`。

### 1.2 LOG_SENSITIVE_DATA（rules/log/，L2，medium）

- **判定**：`Log.[deviw]` 调用参数区含敏感标识符——复用 `SENSITIVE_DATA_RE` 词表（token/password/location/account 等 14 词，评审第四节"复用现有词表"建议）；sanitize 剔除字符串字面量后按标识符/方法名匹配（`Log.d(TAG, "userId=" + user.getUserId())` 经 `getUserId` 命中）。
- **边界**：TAG 变量名含敏感词会误报（L2 复核兜底，rule.yaml limitations 声明）；release 混淆/日志裁剪不在静态范围。
- FTS 词项 `["Log"]`。

### 1.3 HARDCODED_SECRET（rules/crypto/，L2，high）

- **判定**：敏感命名字符串常量——变量名含 `secret/api_key/access_key/private_key/token/password/passwd`（不区分大小写）且字面量值 ≥8 字符（MASVS-CRYPTO-2）。字符串值在字面量内 → 原 code 匹配 + 行首注释排除（JS_BRIDGE 同款策略）。
- **边界**：测试桩/占位值由 AI 复核排除（description 显式提示）；byte[]/char[] 与运行时拼接不覆盖（limitations 声明）。
- FTS 词项 `["secret", "secret_key", "password", "token", "api_key", "apikey", "access_key", "private_key", "passwd"]`（含下划线变体——unicode61 tokenizer 下 `SECRET_KEY` 为单 token，裸 `secret` 查询不命中）。

### 1.4 管线接入

- `RULE_META` 三条新条目（intent/log/crypto family）；`GLOBAL_CODE_RULES` 扩至 11 条；`GLOBAL_RULE_TERMS`（FTS）三条；detect.py 薄入口（`Path(__file__).parent.name` 取规则 ID 模板）；execute 的 `GLOBAL_CODE_RULES` 分支复用（FTS 初筛 → `_webview_crypto_match` 匹配 → `_global_base` L2 候选）。
- 元数据一致性测试的 family 集合扩展 `{webview, crypto}` → `{webview, crypto, intent, log}`；规则总数断言 30 → 33。

### 1.5 测试（test_webview_crypto_rules.py 新增 17 用例；核验后补 5 个边界用例共 22 个）

三规则各 4-5 个匹配边界（命中/显式加固不命中/短值不命中/良性名不命中/注释排除）+ 3 个 execute 集成（真实索引 + FTS 初筛 + 完整链路产出 L2 候选）；核验后补 `TestP5VerificationEdgeCases` 5 用例（Log.wtf、FLAG_IMMUTABLE 词边界子串巧合、flags 变量误报方向、ACCESS_TOKEN FTS 召回、词干误报形态）。

## 2. 验证结果

- `test_webview_crypto_rules.py` 49/49（含 P5 新增 19）；
- 全量测试：**1326 passed / 0 failed**（40.82s，2026-08-27）；
- 同步校验：PASS 82 / CONFLICT 0 / ORPHAN 0（不受影响）。

## 3. 行为影响评估

- 新增候选面：三条 L2 规则对所有后续 run 生效（AI 复核漏斗）；规则总数 30→33（rule-runner 并行开销线性 +3）；`LOG_SENSITIVE_DATA` 的 FTS 单词项 "Log" 是 11 条全局规则中初筛面最宽的一条（所有含独立词 log 的文件进入正则判定——大 APK 上候选文件集随 log 使用面线性放大，核验 R-7 声明）；
- 既有规则零行为变化（仅集合扩展与函数新增分支）；
- severity 初值：PENDING_INTENT medium（默认 mutable 是平台现状，非显式选择）、LOG medium（泄露面依赖数据上下文）、HARDCODED_SECRET high（密钥泄露直接危害）——最终定级由 AI 复核。

## 4. 待核验点

1. ~~PENDING_INTENT 的"flags 经变量引用 IMMUTABLE 常量时字符串不在调用文本内"~~（核验 R-3 更正：该形态是**误报**方向——变量 flags 不含 FLAG_IMMUTABLE 子串即报出；真正的漏报边界是调用文本内无关标识符含 FLAG_IMMUTABLE 子串，已改词边界判定消除）；
2. LOG_SENSITIVE_DATA 的 TAG 误报面（sanitize 后 TAG 变量名含 private 等词的误报率）；
3. HARDCODED_SECRET 的正则对 Kotlin `const val` 形态与多行声明的覆盖（JADX 反编译为 Java 形态，风险低但需确认）；
4. FTS 词项的召回边界（如 `mSecretKey` 驼峰字段名——tokenizer 下 `msecretkey` 单 token，词项 `secret` 不命中；核验 R-2 后已扩 12 个 SCREAMING_SNAKE 复合词项，驼峰与未列入的中缀复合名仍在边界外，rule.yaml 已声明）；
5. 三规则的 severity 初值与 rule.yaml android_api 声明的合理性。

## 5. 核验处置修订（2026-08-27，deepseek-v4-pro 核验 R-1~R-8 后）

- **R-1（高，采纳（已实施））**：`FINDING_COMPONENT_KINDS`（backend/app/reporting/poc.py）补 `intent`/`log` 两域——否则新规则 finding 经报告质量检查会误 FAIL（复现 2026-08-26 审查 R-3 修复过的故障类）；`test_evaluation_report_quality.py` 通过性用例同步扩展；§1.4 管线接入清单补记该下游契约点；
- **R-2（中，采纳（已实施））**：HARDCODED_SECRET 的 FTS 词项扩 12 个 SCREAMING_SNAKE 复合词（access_token/client_secret/db_password 等——unicode61 tokenchars 下复合名整体成单 token，原词项整文件漏检）；补 ACCESS_TOKEN 集成召回用例；rule.yaml limitations 补 FTS 词法边界声明；
- **R-3（中，采纳（已实施））**：FLAG_IMMUTABLE 判定改 `\bFLAG_IMMUTABLE\b` 词边界（EXTRA_FLAG_IMMUTABLE_STATE 类子串巧合不再误判安全）；报告 §4.1 方向标签更正（变量 flags 是误报非漏报）；rule.yaml 补 getActivities 与词边界语义；
- **R-4（低，采纳）**：用例计数勘误 19→17（核验后共 22）；
- **R-5（低，采纳（已实施））**：正则补 `Log.wtf` + 用例；
- **R-6（低，采纳（已实施））**：测试注释/docstring 更新（8→11 条）；docs/04-规则体系.md 更新（总数 33、补 ACTIVITY_EXTERNAL_ROUTE_INJECTION 存量遗漏与 P5 两节）；
- **R-7（低，采纳）**：§3 补 Log 初筛面成本声明；
- **R-8（低，采纳（已实施））**：HARDCODED_SECRET limitations 补词干误报面（tokenCount/passwordEncoder 类）；LOG 描述 MASVS-STORAGE-1→STORAGE-2（日志专项）；词干误报形态测试锚定。

核验后测试：test_webview_crypto_rules.py 54/54（含 TestP5VerificationEdgeCases 5 用例）+ test_evaluation_report_quality 通过；全量与提交前最终数字见 §2 更新。核验报告：`2026-08-27-p5-new-rule-family-audit.md`。
