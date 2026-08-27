# 规则集质量评审（rules/，2026-08-27）

> **问题**：针对 APK 漏洞发现场景的内置规则集（`rules/`，30 条规则 + shared 运行时）是否存在错误、可补充什么——逐条代码级核对。
> **数据源**：rules/ 全部 30 个 rule.yaml + `sink_taxonomy/versions.yaml`（59 条 = 55 base + 4 manual）+ shared 5 个实质模块（detector.py 3678 行 / dataflow.py 3382 行 / authorization.py / index_reader.py / receiver_registration.py）+ backend/app/analysis/manifest.py 交叉核对 + run eada0e71 验收数据。
> **修订记录**：2026-08-27 按独立审计（`2026-08-27-ruleset-quality-review-audit.md`，R-1~R-10 全部采纳）修订——E8-2 控制流断言更正、sink taxonomy 现状更新至 59 条（4 条 manual 已入册）、leaves 机制描述更正、versions.yaml 行号按现行 392 行版本校准。

## 评审范围与方法

- 逐条核对 30 条规则的 YAML 元数据与 `shared/detector.py::execute` 分派路径（`RULE_META`/`GLOBAL_CODE_RULES`/`MANIFEST_FACT_RULES` 等）。
- 对 crypto/webview/manifest 全局规则逐正则推演（含嵌套块、字面量/常量、JADX 反编译形态）。
- 对 sink 分类双数据源（`dataflow.py::classify_operation_taxonomy` vs `sink_taxonomy/versions.yaml`）逐条 diff。
- 对 manifest 事实规则（§12）与 Android 官方语义（NSC 覆盖优先级、allowBackup 默认值、dataExtractionRules）比对，backend 解析层 grep 佐证。
- 引用 `docs/analysis/acceptance/2026-08-26-explorer-output-gap-analysis.md` 的实证数据佐证系统性结论。

## 总体结论

工程质量高于业界平均水平：**保守性设计（fail-closed、gap 显式化、arity 三态校验）是最大亮点**。但存在 **2 处确定性 bug**（E1 正则缺陷、E2 收录了不存在的 API 且漏掉真实 API）、**2 处与 Android 官方语义的偏差**（E3/E4）、**3 处覆盖缺口**（E5/E6 + sink taxonomy 封顶），以及 **1 处可移植性缺陷**（E7 厂商特定硬编码混入 shared 层）。

---

## 一、确认的错误（按严重度排序）

### E1【确定性 bug】`ORDERED_BROADCAST_UNRESTRICTED` 权限正则双重缺陷

`rules/shared/detector.py:2956`：

```python
permission = re.search(r"sendOrderedBroadcast\s*\([^,]+,\s*[^n][^u][^l][^l]", code)
```

- **语义错误**：`[^n][^u][^l][^l]` 试图表达"第二参数非 null"，但它是逐字符否定——权限名首 4 字符形如 `n?u?l?l` 即被误判为 null。
- **漏报（实际触发面）**：`[^,]+` 无法跨越第一参数的内嵌逗号。`sendOrderedBroadcast(new Intent("a","b"), null)` 中 `[^,]+` 在 `new Intent("a"` 处停止，`"b"),` 的 4 个字符（`"`、`b`、`"`、`,`）被当成"权限非空"——**无权限的有序广播被判为已限制**。
- **修复方向**：复用同文件 `_split_top_level_args`（`detector.py:172`）拆顶层参数后判 `null` 字面量。
- 缓解因素：该规则 `auxiliary: true`（`rules/receiver/ORDERED_BROADCAST_UNRESTRICTED/rule.yaml:7`），仅作加权信号，不单独成 finding。

### E2【确定性 bug】`LocationManager.getLastKnownLocation` 零覆盖，且收录了不存在的 API

- `rules/` 全目录 grep `getLastKnownLocation` **0 结果**。
- `rules/sink_taxonomy/versions.yaml:160-164` 与 `rules/shared/dataflow.py:2913-2916` 均将 `getLastLocation` 挂在 `LocationManager` family 下——**`LocationManager` 没有 `getLastLocation` 方法**（它是 `FusedLocationProviderClient` 的 API），真实 API 是 `getLastKnownLocation(String)`。
- 后果：纯 framework 位置读取链路（无 GMS 依赖时）整条漏检；`getLastKnownLocation` 是位置泄露最经典 sink。
- 修复：dataflow family 补 `getLastKnownLocation`（公开签名单参 `String provider`）+ versions.yaml 同步补录。

### E3【语义偏差】`CLEARTEXT_TRAFFIC_ALLOWED` 忽略 `networkSecurityConfig` 覆盖优先级

判定为 `uses_cleartext_traffic is True and target_sdk >= 28`（`rules/shared/detector.py:3484-3486`）。按 Android 官方语义：**声明了 `networkSecurityConfig` 的应用会忽略 `usesCleartextTraffic` 标志**。

- 假阳性方向：带 NSC 且 NSC 禁明文的 app 仍被报"明文放开"。佐证：backend 全目录 grep `networkSecurityConfig` **0 结果**（`backend/app/analysis/manifest.py` 未解析该属性）。
- 漏报方向：`targetSdk < 28` 时明文默认放行（未显式声明 false），该存量风险面完全不覆盖（与 MobSF 等工具口径不一致）。
- 修复：manifest.py 补 NSC 属性解析 + 规则侧 NSC 存在时降级/标注；`targetSdk<28` 未显式 false 的场景纳入（至少 L1 informational）。

### E4【语义偏差】`ALLOW_BACKUP_ENABLED` 只认显式 true，忽略默认值与 API 31+ 豁免机制

- `backend/app/analysis/manifest.py:84` 仅解析 `allowBackup` 属性；`allowBackup` 默认值即 true（与 targetSdk 无关），未声明的 app 同样可被 adb backup 提取（`targetSdk>=23` 仅是 Auto Backup 全量备份特性的门槛），这批"沉默风险"不报。
- `fullBackupContent`（<API 31）与 `dataExtractionRules`（API 31+ autoBackup 豁免）均未解析——`allowBackup=true` 但已通过 dataExtractionRules 排除敏感数据的 app 被误报。
- 显式声明才报是自洽的保守策略，但与 `rule.yaml:9` 宣称的"数据可提取"语义有偏差，至少应在 limitations 声明当前未覆盖默认值与豁免机制。

### E5【覆盖缺口】弱加密规则族覆盖面过窄

`rules/shared/detector.py:3103` 只匹配 `Cipher.getInstance("AES/ECB/...` 字面量：

- `Cipher.getInstance("AES")` 在默认 provider 下**就是 ECB 模式**（最隐蔽写法）——漏检；
- transformation 经变量/常量传递——漏检；
- crypto 域仅 3 条规则（ECB/TrustManager/HostnameVerifier）：DES、3DES、RC4、MD5、SHA1、`PBEWithMD5AndDES` 均无规则。对照 OWASP MASVS-CRYPTO 全域，这是覆盖最薄的组件域。

### E6【覆盖缺口】SSL 放行/TrustManager 正则不能跨越嵌套块

- `rules/shared/detector.py:3040`（`WEBVIEW_SSL_ERROR_IGNORED`）：`[^}]{0,800}?` 无法跨过第一个 `}`。`if (cond) { handler.cancel(); } handler.proceed();`（proceed 在嵌套块之后）漏检。
- `rules/shared/detector.py:3069-3083`（`TRUST_MANAGER_ALL_ACCEPT`）：`([^{}]{0,400}?)` 只匹配扁平方法体，`return;` 形式的空实现不命中。
- `rules/shared/detector.py:3085-3099`（`HOSTNAME_VERIFIER_ALWAYS_TRUE`）同样只匹配扁平方法体，且方法名 `verify` 过泛（任意类的同名方法都会进入正则判定）——同款嵌套块限制。
- 典型"直接 proceed/空体"样本有效，变体形态系统性漏检。另注：上述规则均不验证 TrustManager/WebViewClient/HostnameVerifier 是否实际安装（`SSLContext.init` / `setDefaultHostnameVerifier` 关联），L2 语义下由 AI 复核兜底，可作为晋级条件补充。

### E7【可移植性缺陷】shared 层混入特定 APK 硬编码（过拟合）

- `rules/shared/dataflow.py:2934-2944`：`sport_leaves` 含 `com.xiaomi.fitness.sport_manager_export.` 前缀（注释自认"小米运动导出接口"）；`finishSport` 归入 `connection_session_control` 的 taxonomy 语义牵强。
- `rules/shared/index_reader.py:76-77`：`FLOW_INTRINSIC_METHODS` 混入 `isAllowedHttps/isValidUrl/validateUrl/isHttpsUrl` 等 shop APK 的 URL 校验 wrapper 名。
- `rules/shared/detector.py:102-105`：`SENSITIVE_BINDER_METHOD_RE` 词表含 `Sport|Workout|Wear`（运动健康域）。
- `rules/shared/dataflow.py:3007-3012`：`BluetoothOutputStream/UsbOutputStream/NfcOutputStream`（**SDK 中不存在的类**，特定 APK 自定义类名被当作平台 family）。leaves 机制仅匹配裸简单类名（`dataflow.py:2532-2533`：receiver_type 不含 `.` 与 `$` 才命中）——索引侧为简单名时任意包下同名类均命中（跨 APK 噪声），为 FQCN 形态时永不命中（死条目）。
- `rules/shared/dataflow.py:2927-2929`：`startGymSensor/startStepSensor` 等自研方法挂在 `SensorManager` family 下；`rules/shared/detector.py:3431` 评分逻辑含 `Sport|Workout|Account` 域特调。
- 这些特调部分有注释说明（sport_leaves 之 v04 真机验证驱动；`index_reader.py:76-77` 的 URL 校验 wrapper 名无逐条注释），但在"通用 APK 漏洞发现"定位下是噪声源/死代码。**建议迁出 shared 层，改为 per-APK custom sink proposal 数据（T2.7 管线的设计落点）**。

### E8【工程瑕疵】（6 项，均为小改）

| # | 问题 | 证据 |
|---|---|---|
| 1 | `ACTIVITY_SENSITIVE_NAME_HINT` 词表仅 5 词（Reset/Password/Admin/Payment/Debug），与 `SENSITIVE_DATA_RE` 14 词不一致，Login/Token/Account/Pay 缺失 | `detector.py:1912` vs `detector.py:101` |
| 2 | Provider flow 规则在无索引时经 else 分支回退 `_component_rule` 旧式全文件逻辑（`detector.py:500` legacy 回退 → `detector.py:510`，功能可用但语义降级），且不打 `LEGACY_INDEX_SCOPE` gap 标记（对照 `detector.py:494` 其他分支的做法） | `detector.py:458/500/510` |
| 3 | webview/crypto 全局规则每文件只取第一个 match（`search` 非 `finditer`），同文件多处命中只报一处 | `detector.py:2964`（仅 JS_BRIDGE artifact 用 finditer） |
| 4 | 所有 `rule.yaml` 统一声明 `android_api: "1-36"`，但 `DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION` 依赖的 `RECEIVER_EXPORTED`/`RECEIVER_NOT_EXPORTED` 标志（`receiver_registration.py:14-15`）语义只适用 API 33+/targetSdk 34+，版本声明失去信息量 | 全部 30 个 rule.yaml |
| 5 | severity 只存在于 `RULE_META`（`detector.py:17-51`）而 rule.yaml 无此字段——元数据双源，存在漂移风险（当前抽查一致） | `detector.py:17-51` |
| 6 | `GUARD_RE` 自认"当前无调用点"的死代码（与 `dataflow.GUARD_METHODS` 的同步注释同为摆设） | `detector.py:97-100` |

---

## 二、系统性缺口（实证）：sink taxonomy 封顶

引自 `docs/analysis/acceptance/2026-08-26-explorer-output-gap-analysis.md`（run eada0e71）：

- **46 个 partial 中 44 个是"跳回查 100% 通过但 sink 未命中 taxonomy 被封顶"**——瓶颈已从跳回查转移到 taxonomy 覆盖面（验收时点 55 条；截至本报告修订已扩至 59 条），validated=0 的直接根因。
- 44 个未命中样本中的真扩展点：`LoginManager.getPrefEncryptedUserId`（隐私读取）、`PreferenceUtil.setStringPref`（偏好写入）——versions.yaml 的 **base 条目**只覆盖平台形态（SharedPreferences.Editor/Settings/DataStore），不含应用自封装存储层。人工管线已部分生效：2026-08-26/27 经评审入册 4 条 manual 条目（`versions.yaml:356-391`，提交 `76ac2c4`，恰含上述两个方法及其同类），但 44 个未命中 sink 中多数仍未入册。
- 本次 diff 新增的 dataflow→versions.yaml base 层遗漏条目（11 项）：`ParcelFileDescriptor.open`、`FileInputStream`/`RandomAccessFile` 构造器（file_read）、`MatrixCursor`、okhttp `send`、`compileStatement`、`truncate`、`open`、`Editor.clear`、`Settings.Secure putInt/putLong/putBoolean`、`Constructor.newInstance`、`getCurrentLocation`——两份数据同步完全靠人工（`versions.yaml:3-5` description 自认）。
- **双源 taxonomy 冲突已兑现**：`write` 条目（receiver_leaves 含 `BluetoothOutputStream/UsbOutputStream/NfcOutputStream/ProtocolWriter`，`versions.yaml:342-355`）归 `file_mutation`，而 `dataflow.py:3007-3012` 将同 receiver 的 `write` 归 `device_protocol_output`——同一调用在规则轨与 explorer 轨得到不同 taxonomy，是"人工同步风险已兑现"的实证，应作为同步校验脚本的验收用例。

---

## 三、设计亮点（评审中确认，后续迭代不应破坏）

| 亮点 | 证据 |
|---|---|
| 授权矩阵 fail-closed：未收录平台权限一律 `unknown` + critical gap，绝不猜强度 | `authorization.py:16`（注释）/`145-156` |
| sink 判定 arity 三态校验（verified/candidate/reject），防同名方法误配 | `dataflow.py:2629-2656` |
| "resolve 失败 ≠ 死代码"保守修正，注释完整记录修订前假阴性方向 | `index_reader.py:120-160` |
| SIMPLE_GLOB 语义正确（`.` 任意字符 / `*` 重复前一原子，非 shell glob）——常见错误点，此处实现正确 | `authorization.py:380-403` |
| 索引只读安全边界（symlink 拒绝、目录白名单、query_only） | `index_reader.py:96-103` |
| Provider CRUD override descriptor 形状校验，防误绑无关同名方法 | `index_reader.py:1255-1296` |
| 动态 receiver 解析仅信任 framework/AndroidX owner，应用 wrapper/未知 overload 一律 gap | `receiver_registration.py:22-27` |

---

## 四、可补充的规则方向（对照 OWASP MASVS / 业界基线）

按与现有架构契合度排序：

| 方向 | 佐证缺口 | 契合度 |
|---|---|---|
| `PendingIntent` 缺 `FLAG_IMMUTABLE`（API 23+ 默认 mutable，Android 12 强制） | 无任何 pending intent 规则 | 高（纯 manifest/调用点事实，同 §12 模式） |
| `LocationManager.getLastKnownLocation` sink | E2 的 0 结果 | 高（一行 taxonomy 补录） |
| 日志敏感数据泄露（`Log.d` 等 + 复用 `SENSITIVE_DATA_RE` 词表） | 词表目前仅用于广播规则 | 高 |
| 硬编码密钥/凭证（字符串常量 + crypto 上下文） | crypto 域仅 3 条 | 中（L2 模式匹配） |
| `DexClassLoader`/动态代码加载 | 0 覆盖 | 中 |
| Java 反序列化（`ObjectInputStream.readObject` 入口数据） | 0 覆盖 | 中 |
| 不安全随机（安全场景 `Random` 而非 `SecureRandom`） | 0 覆盖 | 中 |
| `Intent.parseUri`/隐式 Intent 重定向 | route injection 只覆盖 `setClassName/setComponent/setClass/setAction/setPackage` 族（`detector.py:124-126`） | 中 |
| WebView `setSavePassword`；targetSdk 分层默认值（`setAllowFileAccess` 在 targetSdk<30 默认 true，规则只报显式调用） | `detector.py:3012` 只匹配显式 true | 中（需 targetSdk 矩阵） |
| `addJavascriptInterface` 的 API 17 分界（`@JavascriptInterface` 语义差异） | 描述未提版本语义 | 低（AI 复核兜底） |
| Task hijacking（`allowTaskReparenting`/launchMode 组合） | 0 覆盖 | 低 |

---

## 五、修复优先级建议

| # | 动作 | 类型 | 预期效果 |
|---|---|---|---|
| 1 | sink taxonomy 扩充（dataflow→versions.yaml base 层同步 + 44 个未命中 sink 人工评审入册，含 E2 补录；配套同步校验脚本，以 `write` 双源冲突为验收用例） | 数据 + 小改 | 44 个封顶 partial 候选解封（validated=0 的直接解法，已有报告背书） |
| 2 | E1 改用 `_split_top_level_args` 判 null；E2 dataflow family 补 `getLastKnownLocation` | 代码小改 | 消除 2 处确定性 bug |
| 3 | E3/E4：manifest.py 补 `networkSecurityConfig`/`dataExtractionRules`/`fullBackupContent` 解析，规则侧联动 | 解析层小改 | 消除假阳性方向 + 补默认值存量覆盖 |
| 4 | E7：厂商特定条目迁出 shared 层 → per-APK custom sink 数据（T2.7 落点） | 重构（中） | 通用可移植性 |
| 5 | 按第四节表逐条补规则族（PendingIntent / 日志泄露 / 硬编码密钥优先） | 规则新增 | 覆盖面对齐 MASVS 基线 |
| 6 | E8 六项工程瑕疵批量清理（词表统一、provider 回退补 `LEGACY_INDEX_SCOPE` gap 标记、finditer、版本声明、severity 单源化、GUARD_RE 死代码清理） | 代码小改 | 一致性 |

---

## 附：评审方法局限

- 未执行动态验证（正则缺陷 E1/E6 为静态推演结论，建议补单测固化：`backend/tests/test_manifest_fact_rules.py` 已有同风格测试可挂载）。
- sink taxonomy 遗漏条目清单为人工 diff（dataflow 侧 76+ family 分支 vs versions.yaml base 层 55 条；全文 59 条 = 55 base + 4 manual），未写脚本全量比对——如需精确全集可补一个同步校验脚本（审计发现的 `write` 双源冲突应作为其验收用例）。
- versions.yaml 行号锚点基于 2026-08-27 现行 392 行版本（提交 `76ac2c4` 之后）；本报告初稿基于 81 行旧版，行号已按审计处置校准。
