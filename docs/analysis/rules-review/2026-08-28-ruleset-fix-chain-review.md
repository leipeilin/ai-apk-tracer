# 审查报告：规则集质量评审修复提交链（P1~P6）

> **审查对象**：`docs/analysis/rules-review/2026-08-27-ruleset-quality-review.md` 提出的修复优先级 #1~#6 对应的六个提交
> **提交链**：`37116ef`(P1) → `cec8588`(P2) → `7812141`(P3) → `c404cfd`(P4) → `8fdc112`(P5) → `71ed7f7`(P6)
> **审查方式**：逐提交 diff 核验 + 修复与评审建议的对应性 + 缺陷/偏差/覆盖缺口是否全部闭合 + 全量回归
> **审查时间**：2026-08-28

---

## 一、总体结论

**结论：✅ 六提交忠实落地评审建议，但评审清单本身存在 1 处覆盖缺口（E5/E6 未纳入修复）。**

六个提交（P1~P6）**忠实、高质量地落地**了评审文档第五节修复优先级 #1~#6，每个提交都附有 deepseek-v4-pro 独立审计报告（`*-audit.md`）且核验闭合（R-1~R-8 全采纳）。E1/E2/E3/E4/E7/E8 六项确定性 bug / 语义偏差 / 工程瑕疵**全部修复**，实现质量高于预期。

但审查发现一个**系统性缺口**：评审文档把 **E5（弱加密规则族覆盖窄）与 E6（SSL/TrustManager 正则不跨嵌套块）** 列为"覆盖缺口"（正文第 62/65 行），却**未将它们纳入第五节修复优先级清单**（6 项里无 E5/E6 对应项），导致 P1~P6 提交中 E5/E6 **完全未动**。

---

## 二、逐提交落地对照

| 提交 | 对应优先级 | 评审建议 | 实现 | 结论 |
|---|---|---|---|---|
| `37116ef` P1 | #1 sink taxonomy 扩充 | dataflow→versions.yaml base 层同步 + 同步校验脚本 | base 层 55→73 条 + `check_sink_taxonomy_sync.py`（CI 接入）+ write 双源冲突修复 | ✅ 忠实落地 |
| `cec8588` P2 | #2 E1/E2 | E1 用 `_split_top_level_args` 判 null；E2 补 `getLastKnownLocation` | `_ordered_broadcast_has_permission` 重写（含引号转义增强）+ `getLastKnownLocation` 补录 LocationManager、`getLastLocation` 改挂 FusedLocationProviderClient | ✅ 忠实落地，且超预期 |
| `7812141` P3 | #3 E3/E4 | manifest.py 补 NSC/dataExtractionRules/fullBackupContent 解析 | `manifest.py` 补解析 + detector 双向覆盖/豁免降级 + 2 条 rule.yaml 语义修正 | ✅ 忠实落地 |
| `c404cfd` P4 | #4 E7 | 厂商条目迁出 shared → per-APK custom sink | sport/伪平台类迁 versions.yaml 单一事实源 + dataflow manual 回退（verified_arity 三态） | ✅ 忠实落地 |
| `8fdc112` P5 | #5 三条新规则 | PendingIntent/日志泄露/硬编码密钥 | 三条新 rule.yaml + detect.py + detector 分支（30→33） | ✅ 忠实落地 |
| `71ed7f7` P6 | #6 E8 六项 | 词表统一/provider gap/finditer/版本声明/severity 单源化/GUARD_RE | E8-1~E8-6 全处理（E8-4 有反向论证，见下） | ✅ 忠实落地 |

---

## 三、实现质量确认（抽查关键正确性）

### 3.1 E1 修复（P2）——正确且超预期

评审建议"复用 `_split_top_level_args` 判 null"，实现新增 `_ordered_broadcast_has_permission` 函数：

- 用 `re.finditer` 遍历所有 `sendOrderedBroadcast(` 调用 + `_matching_paren_end` 找括号闭合 + `_split_top_level_args` 拆顶层参数 + 判 `args[1]` 非 `null` 字面量；
- **同时修复了 `_split_top_level_args` 自身的一个缺陷**（引号内转义 `\\"` 未处理），这是评审未要求的额外加固；
- 保守方向正确：`receiverPermission` 为变量/常量引用时视为"已限制"（运行时值未知，保守取有限制方向），且规则本身 `auxiliary: true`（加权信号，不单独成 finding）。

### 3.2 E2 修复（P2）——正确

`getLastKnownLocation` 补录到 `LocationManager` family（arity 1），`getLastLocation` 正确改挂 `FusedLocationProviderClient`——消除了"张冠李戴"（原 `getLastLocation` 挂在 LocationManager 下，而 LocationManager 无此方法）。

### 3.3 E8-4 反向论证（P6）——正确且有据

评审建议"改窄 `android_api: "1-36"` 数字（RECEIVER_EXPORTED_FLAG 只适用 API 33+）"。实施者**没有盲从**，而是论证：`registerReceiver` 本身 API 1 即有，`RECEIVER_EXPORTED/RECEIVER_NOT_EXPORTED` 标志常量虽 API 33+ 引入、targetSdk 34+ 强制，但改窄 `android_api` 数字反而会引入错误声明。最终改为在 rule.yaml `limitations` 补版本语义。**此反向论证成立**，与项目"保守不误判"的哲学一致。

### 3.4 E8-3 多命中全枚举（P6）——附带修复了一个崩溃路径

`_webview_crypto_match` 返回类型 `dict | None` → `list[dict]`，11 个规则分支改 `finditer` 全枚举。核验 R-2 发现旧行为 `pattern.search(code)` 在多命中时取原文首个匹配，**第 2..N 个候选的证据文本错误 + 注释内首个匹配会取到注释文本**，已一并修复为按 sanitized 匹配跨度切原文。

---

## 四、发现的问题

### C-1【系统性缺口·P1】E5/E6 未纳入修复清单，P1~P6 均未处理

**事实**：
- 评审文档正文将 E5（弱加密规则族覆盖窄：`Cipher.getInstance("AES")` 默认 ECB、transformation 变量传递、DES/3DES/RC4/MD5/SHA1 无规则）和 E6（`WEBVIEW_SSL_ERROR_IGNORED` 的 `[^}]{0,800}?` 与 `TRUST_MANAGER_ALL_ACCEPT` 的 `([^{}]{0,400}?)` 不能跨嵌套块）列为"**覆盖缺口**"；
- 但第五节修复优先级清单的 6 项里，**没有 E5/E6 对应项**（#5 是"补规则族 PendingIntent/日志/硬编码密钥"，属第四节"可补充方向"，不是 E5/E6）；
- 结果：当前 `detector.py:3126` 仍是 `[^}]{0,800}?`（E6 未修），`detector.py:3160` 仍是 `([^{}]{0,400}?)`（E6 未修），`detector.py:3192` 仍是 `Cipher.getInstance("AES/ECB/` 字面量（E5 未修）。

**定位**：这是**评审文档自身的清单遗漏**，而非 P1~P6 实施错误。P1~P6 忠实执行了清单，但清单本身没有覆盖 E5/E6。

**影响评估**：
- E5：弱加密 DES/3DES/RC4/MD5/SHA1 仍零覆盖——crypto 域仍是覆盖最薄的组件域；
- E6：SSL 放行/TrustManager 变体（`proceed` 在嵌套块之后、空实现 `return;`）系统性漏检——这些是真实 APK 上常见写法。

**建议**：将 E5/E6 补入后续修复计划（可作为 P7/P8，或并入下一轮规则集评审）。

### C-2【说明性】P5 提交的 "explorer 3 失败属并行 WIP 已声明"

P5 提交信息注明"explorer 3 失败属并行 WIP 已声明"——这是诚实记录，说明当时 `test_explorer.py` 有 3 个失败（属并行开发的 explorer 轨 P-2 工作未完成）。经后续 `8d46b29`（P-2）完成后，当前全量测试已 **1348 passed / 0 failed**，此历史失败已消除。**非缺陷**，仅记录。

---

## 五、全量回归

实测当前 HEAD：**1348 passed / 0 failed**（41.55s），无失败。P6 提交内注明的"1295 passed（排除 test_explorer.py 3 个失败）"是当时并行 WIP 状态，现已随 P-2 完成消除。

---

## 六、总结

| 维度 | 评价 |
|---|---|
| 清单忠实度 | ✅ P1~P6 六提交忠实落地评审 #1~#6 |
| 修复正确性 | ✅ E1/E2/E3/E4/E7/E8 全部正确，且 E1/E8-3 有超预期的额外加固 |
| 反向论证质量 | ✅ E8-4 对评审建议做了有理有据的修正 |
| 核验闭合 | ✅ 每提交附 deepseek-v4-pro 独立审计报告，R-1~R-8 全采纳 |
| 回归 | ✅ 1348 passed 无失败 |
| **覆盖完整性** | ⚠️ E5/E6 被评审清单遗漏，P1~P6 均未处理 |

**结论：✅ 六提交正确，但评审清单存在 E5/E6 覆盖缺口，建议补入后续计划。**

---

## 附：审查数据源

- `git show` 六个提交完整 diff
- `rules/shared/detector.py` / `dataflow.py` / `manifest.py` 实读核对
- 全量 pytest 实测（1348 passed）
- `ruleset-quality-review.md` 第五节修复优先级清单 vs 正文 E1~E8 逐项比对
