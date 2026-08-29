# T1-v3 攻击面发现 POC 动态验证记录（2026-08-29）

> **验证对象**：`docs/analysis/explorer-track/2026-08-29-t1v3-attack-surface-findings.md`（T1-v3 全量探针 39 候选的簇 1–3 攻击面发现）
> **方法**：5.53 反编译链复核 → 5.54 base.apk 拉取解码清单 diff → jadx 单类验证 5.54 关键类代码 → 5 组授权 POC（am start + logcat 取证）→ 按四要素判定
> **结论用途**：人工复核 verdict 依据 + golden 负回归样例（`evaluation/golden/v1/cases/activity-no-intent-filter-default-not-exported` / `provider-declared-exported-false` / `activity-alias-enabled-target-reachable`）的证据锚点

---

## 1. 验证环境与执行身份

| 项 | 值 |
|---|---|
| 设备 | serial `37b8b9b6` · Xiaomi 25102RKBEC（myron）· Android 16（API 36）· HyperOS |
| 手机版本 | `com.xiaomi.shop` **5.54.0.20260811.r1**（versionCode 20260811；探索 APK 为 5.53.0.20260527） |
| 执行身份 | adb shell（uid 2000，`caller=com.android.shell`） |
| 红线 22 声明 | shell 可达 ≠ 普通应用可达；本批"普通应用可达性"结论均以两版清单 exported/alias 结构判定，shell 动态结果仅用于观察组件行为与 Guard 生效性 |
| 5.54 清单 diff | 目标组件 effective exported 状态与 5.53 **完全一致**；差异仅两处：MiFiAppToolProxyActivity 运行时包名为 `com.xiaomi.jr.scaffold.developer.tool`（jadx 反编译产物中的 `p300jr` 是包重命名）；ShareImageReceiverActivity 新增 `enabled=false` + `ShareImageActivityAlias`（见 §2 B） |

## 2. POC 执行记录（均经 AskUserQuestion 授权后以计划内 argv 执行）

| # | 命令要点 | 预期 | 实际（logcat 时间戳） | 动态结论 |
|---|---|---|---|---|
| A | `am start -n com.xiaomi.shop/com.xiaomi.jr.scaffold.developer.tool.MiFiAppToolProxyActivity --es command send_log` | tool 未安装 → 静默 finish | 启动成功后 `16:33:32.346 removetask Task{#2047 A=10302:com.xiaomi.shop}`、回到桌面；无 MifiLog/SEND 日志；`pm` 查 `com.xiaomi.jr.tool` 不存在 | **safe**（签名 Guard `isOfficialToolInstalled` 生效） |
| C | `am start -n com.xiaomi.shop/com.xiaomi.shop.wxapi.WXPayEntryActivity --es wx_token_key com.tencent.mm.openapi.token --ei _wxapi_command_type 5 --es _mmessage_appPackage com.tencent.mm ...`（无 checksum） | checksum 校验拒绝 | `16:35:18.678` activity 创建并停留，无任何 onResp/EventBus 副作用日志（MM SDK 日志开关关闭，拒绝为静默） | **reachable_only**（Guard 拒绝朴素注入；完整伪造需测试 APK——checksum 为 byte[] extra，am 不支持） |
| D | `am start -n com.xiaomi.shop/com.alipay.sdk.app.AlipayResultActivity --es session POC-SESSION-0001 --es scene mqpSchemePay` | 随机会话未命中 → 秒退 | `16:35:02.007` resumed → `16:35:02.013` 回桌面（6ms finish），无回调注入 | **safe**（会话注册表 Guard 生效） |
| E1/E2 | 两次 `am start -n com.xiaomi.shop/com.xiaomi.shop2.plugin.PluginCartActivity`（第二次 `-a com.xiaomi.shop2.pluginCart -d ShopPlugin://java.lang.Object`） | onNewIntent → resetPluginFragment(host 类名) → loadClass | E1 `16:35:49.168 onActivityCreated`；E2 `16:36:09.376 result code=3`（START_DELIVERED_TO_TOP，onNewIntent 送达）；应用未崩溃，无 ClassCastException 日志（异常被 catch 走 Sentry 静默上报） | **inconclusive→sink 动态不可分辨**（链静态成立：`getFragmentClass()`=URI host → `loadClass(host).newInstance()`，5.54 单类反编译实证同 5.53） |
| B-直启 | `am start -a SEND -t image/jpeg -n com.xiaomi.shop/.activity.ShareImageReceiverActivity --eu EXTRA_STREAM <media uri>` | — | `16:38:36 / 16:39:47 result code=-92`、`Activity class does not exist`（5.54 对该组件新增 `enabled=false`） | 直启被禁 |
| B-alias | 同上但 `-n com.xiaomi.shop/com.xiaomi.shop.activity.ShareImageActivityAlias` | — | `16:39:47.943 result code=0`；18ms 后应用自身发出 `ShopPlugin://com.xiaomi.shop2.plugin.webview.RootFragment` 深链（= `ShareImageUtil.startGoWebView` 动作，`16:39:47.961`） | **alias 承接同一入口，enabled=false 未关闭攻击面**；实测走隐私已同意分支（`!hasInitCta()` 偏好写分支未触发） |

## 3. 逐项判定（人工六值 verdict）

| 报告项 | 关键源码事实（5.53 反编译） | 5.54 实测 | verdict |
|---|---|---|---|
| 1.1 MiFiAppToolProxyActivity | `MiFiAppToolManager.java:49` 静态标志默认 false，仅签名校验通过的 `com.xiaomi.jr.tool` 安装后置位（SHA1=`RpNktUEoNBDOWZXaquWclt+m6Gs=`）；`MifiLogExport` SEND 走系统分享面板需用户点击 | Guard 生效（POC A） | **blocked** |
| 1.2–1.4 yrnsdk.debug 四组件 | 5.53 清单无 intent-filter 且未声明 exported → 默认 false；5.54 同（`no_intent_filter_default`） | 清单一致性确认 | **refutes_candidate**（入口不可达；红线 10 类） |
| 2.1 WXPayEntryActivity | `handleIntent` 双校验：token=公开常量 `com.tencent.mm.openapi.token`；checksum salt 硬编码 `"mMcShCsTr"`（可伪造）；消费方 `PayResultReceiver.java:64-73` 仅支付流程中注册，服务端 authoritative（红线 20） | 朴素注入被拒（POC C）；完整伪造需测试 APK | **unresolved → 条件成立**（reachability=local；gap: DYNAMIC_CONFIRM_REQUIRES_TEST_APK） |
| 2.2 AlipayResultActivity | `f415a.remove(session)` 必须命中进行中的 OpenAuthTask（`AlipayResultActivity.java:31-42`） | 随机会话 6ms finish（POC D） | **unresolved → 条件成立**（gap: OPENAUTH_SESSION_PREDICTABILITY） |
| 3.1 ShareImageReceiverActivity | `ShareImageReceiverActivity.java:30-32` 偏好写仅在 `!hasInitCta()`（隐私未同意）分支；写入值=URLEncoder(md5(攻击者 URI))（值域受限） | 直启被禁但 alias 承接（POC B）；已同意设备走 startGoWebView 分支 | **blocked**（sink 对已初始化设备条件不满足；入口经 alias 仍可达——5.54 变化非修复） |
| 3.2 MainActivity 账号广播 | 涉及登录状态操作，安全门禁止真实账户动态 | 未执行 | **unresolved**（gap: LOGIN_STATE_REQUIRED） |
| 3.3 PluginCartActivity | `BasePluginActivity.getFragmentClass()`=intent data host → `loadClass(host).newInstance()`（5.54 `:193-198/264-268/437` 同 5.53） | onNewIntent 送达，sink 动态不可分辨（POC E） | **unresolved**（影响上限低：仅能加载应用内已有类，无法注入字节码） |
| 3.4 MMKVContentProvider | 两版清单均 declared `exported=false`（attack_surface 产物 `reason=manifest_explicit` 正确）；报告"exported provider"前提错误 | — | **refutes_candidate**（basis: non_exported_provider） |
| 3.5 UpdateProvider | 同上（`com.xiaomi.shop.hms.update.provider`，exported=false） | — | **refutes_candidate**（basis: non_exported_provider） |

## 4. 负回归样例抽取（本次 golden 补充的依据）

同一类入口误判模式——**忽略清单 exported 判定规则**（组件未声明 `android:exported` 时，有 intent-filter 才默认 true，否则默认 false；显式声明以清单为准）：

1. **activity-no-intent-filter-default-not-exported**（negative，explorer miss）：簇 1 的 WebDebugActivity / OpenAppActivity / MiShopDebugActivity / MiniProgramActivity——无 filter 未声明 → 非导出，"debug 接口暴露生产"结论不成立；
2. **provider-declared-exported-false**（negative，explorer miss）：MMKVContentProvider / UpdateProvider——显式 `exported=false`，"exported provider 的 ashmem 句柄披露 / 任意文件删除"前提错误；
3. **activity-alias-enabled-target-reachable**（conditional，explorer conditional）：反向防漏样例——5.54 ShareImageReceiverActivity `enabled=false` 但 `ShareImageActivityAlias`（enabled、同 filter）承接后组件 onCreate 实际执行，"target disabled = 不可达"的过度抑制同样不成立。

注：explorer 确定性产物（attack_surface）与 AI 记录的 `component.exported` 均为正确值（False）；误判发生在**候选攻击语义与报告层**——对 `exported=false` 入口仍产出"外部可控/经 binder 传入"语义的候选并升级为"生产暴露"叙事。explorer prompt（1.0.0）已同步补充 exported 默认值规则与 `exported=false` 入口的候选禁令。
