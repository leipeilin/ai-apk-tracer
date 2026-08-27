# P3 任务实施报告：E3/E4 manifest 规则语义修正（NSC 覆盖 + 豁免机制 + 存量风险面）

> **任务来源**：`docs/analysis/rules-review/2026-08-27-ruleset-quality-review.md` 第五节优先级 #3
> **实施日期**：2026-08-27
> **实施者**：主 agent（GLM-5.3）

## 1. 变更清单

### 1.1 backend/app/analysis/manifest.py：解析 3 个 application 属性

新增 `networkSecurityConfig` / `dataExtractionRules` / `fullBackupContent` 入口属性解析（`_attr` 透传 resource 引用，缺失为 None），输出字段 `network_security_config` / `data_extraction_rules` / `full_backup_content`。只解析入口不解析 XML 内容（NSC 的 cleartextTrafficPermitted、dataExtractionRules 的 domain 覆盖面留待下游/人工复核——在规则描述中显式声明）。

### 1.2 rules/shared/detector.py `_manifest_fact_candidates`：两规则条件树扩展

**CLEARTEXT_TRAFFIC_ALLOWED（评审 E3）**：
- 既有分支保留：`usesCleartextTraffic=true` 且 `targetSdk>=28` 且无 NSC → medium（显式放开）；
- **NSC 覆盖降级**：`usesCleartextTraffic=true` 且 `targetSdk>=28` 且 NSC 存在 → low，描述声明"按 Android 官方语义 manifest 标志被 NSC 覆盖忽略，明文策略以 NSC 内容为准（NSC XML 未解析，需下游/人工复核）"——消除假阳性方向的口径错误；
- **存量分支**：`0 < targetSdk < 28` 且未显式 `false` → low，描述"平台默认允许明文流量（存量风险面）"——补齐 MobSF 口径的存量覆盖；显式 `false` 不报；targetSdk 未知（0/None/非法）不报。

**ALLOW_BACKUP_ENABLED（评审 E4）**：
- 既有分支保留：`allowBackup=true` 且 `targetSdk>=23` 且无豁免 → medium；
- **豁免降级**：`allowBackup=true` 且 `targetSdk>=23` 且（`dataExtractionRules` 或 `fullBackupContent` 存在）→ low，描述"备份范围以规则内容为准（XML 内容未解析需复核）"；
- **存量分支**：未声明 `allowBackup`（None）且 `targetSdk>=23` → low，描述"属性默认 true，Auto Backup 生效，adb backup 可提取（存量沉默风险）"——修正原评审指出的"默认值与 targetSdk 解耦"口径（`allowBackup` 默认 true 与 targetSdk 无关，`targetSdk>=23` 仅是 Auto Backup 特性门槛）；
- 显式 `false` 不报；`targetSdk<23` 显式 true 不报（既有行为保留）。

### 1.3 rule.yaml limitations 同步更新（2 个文件）

两规则 limitations 如实描述三分支行为与 severity 层级。

### 1.4 测试（backend/tests/test_manifest_fact_rules.py）

- 更新 2 个既有测试的预期（`targetSdk<28` 显式 true 从"不报"变为"报 low 存量"——评审 E3 修复方向的预期行为变化）；
- 新增 8 个用例：NSC 降级、未声明+targetSdk<28 存量、显式 false targetSdk<27 不报、未声明 allowBackup+>=23 存量、未声明+<23 不报、双豁免机制降级（参数化 2 实例）、manifest.py 解析 3 属性（存在/缺失）。

## 2. 验证结果

- `backend/tests/test_manifest_fact_rules.py` 21/21 通过；
- 全量测试：**1289 passed / 0 failed**（`backend/.venv/bin/python -m pytest backend/tests/ --tb=no`，38.67s，2026-08-27）；
- 真实 run 数据 smoke：对 shop run（eada0e71）的 manifest.json 调用新逻辑不 crash（该 run 的 manifest.json 为 run 元数据结构、解析字段不在其中，字段验证待新 run 重新解析后确认——行为安全性已验证）。

## 3. 行为影响评估

- 新增候选面：所有 `targetSdk<28` 的 APK（明文存量 low）、所有未声明 allowBackup 且 `targetSdk>=23` 的 APK（备份存量 low）、带 NSC/豁免机制的显式声明 APK（从 medium 降级 low）——存量分支是低危 L1 候选，进 AI 复核漏斗；
- 既有 medium 候选（无 NSC 显式放开 / 无豁免显式 allowBackup）行为不变；
- manifest 解析层为纯增量字段（默认 None），不消费该字段的下游零影响。

## 4. 待核验点

1. NSC 降级与存量分支的条件树是否有遗漏形态（如 `usesCleartextTraffic="unknown"`（resource 引用）在存量分支的 `is not False` 判定下会报——保守方向是否可接受）；
2. `allow_backup is None` 分支与"manifest 无 application 节点"的解析边界（此时报存量是否恰当）；
3. targetSdk 未知（0/None）时不进存量分支的保守选择；
4. 测试对行为变化的覆盖完整性（尤其既有测试预期更新的合理性）。

## 5. 核验处置修订（2026-08-27，deepseek-v4-pro 核验 R-1~R-6 后）

- **R-1（中，采纳）**：CLEARTEXT 存量分支非报条件改为"显式 false **且无 NSC**"（`not (uses_cleartext is False and not has_nsc)`）——NSC 覆盖语义是双向的，显式 false + NSC（<28 默认允许明文）实际为放行状态，原实现漏报；
- **R-2（中，采纳）**：ALLOW_BACKUP 存量分支扩展为 `allow_backup in (None, "unknown")`（资源引用与未声明同报——allowBackup 默认 true，运行时行为一致）；CLEARTEXT unknown 形态行为锚定（<28 报 low、>=28 不报）；两规则 limitations 声明资源引用口径；
- **R-4（低，采纳）**：描述拆分 Auto Backup（云端，targetSdk>=23）与 adb backup（本地提取，Android 12+ 平台对非 debuggable 应用默认排除）两条向量；
- **R-5（低，采纳）**：存量分支描述追加豁免提示；`_platform_assumptions` 补 network_security_config/data_extraction_rules/full_backup_content 三事实（AI 复核降级候选的上下文完整）；
- **R-6（低，采纳）**：manifest.py 注释更正为"适用性取决于设备版本而非 targetSdk"（防止后续误加 targetSdk 门控）；
- **R-3（低，采纳-声明）**：limitations 声明 targetSdk<23 旧备份机制风险面不在覆盖范围（对称扩分支留后续）。

核验后测试：`test_manifest_fact_rules.py` 28/28（新增 `TestP3VerificationEdgeCases` 7 用例——NSC 双向覆盖、unknown 四形态、platform_assumptions 三事实）；全量 **1296 passed / 0 failed**（39.66s）。核验报告：`2026-08-27-p3-manifest-semantics-audit.md`。
