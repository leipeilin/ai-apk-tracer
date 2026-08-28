# 探索轨攻击面发现报告（T1-v3 产物）

> **数据来源**：T1-v3 全量探针（2026-08-28/29，`probe-explorer/20260828T130310Z/`）——
> 智谱 glm-5.3-flash max 档 · 198 有效入口 · 70 个成功入口 · **39 候选**
> （validated 3 / partial 33 / unverified 3）
> **置信度声明**：本 run error 率 64.6%（128 入口 transient 失败——智谱 max 档 180s 墙钟
> 超时 + 后段服务过载），39 候选来自 70 个成功入口——**非完整覆盖**，簇结构仅代表已
> 探索部分。validated = 跳回查全通过（链代码实证）；partial = 跳回查通过但 sink 未命中
> taxonomy（custom sink 待人工确认）或部分跳失败。
> **用途**：攻击面候选清单（自动化判定）——**可利用性需人工/POC 确认**。

---

## 簇 1：Debug 工具暴露（5 组件——成体系的真实漏洞模式）

**模式语义**：`yrnsdk.debug` 与 `developer.tool` 构建变体的组件在生产 APK 中暴露——
debug 接口暴露生产是经典的配置失误类漏洞（单个是失误，成簇说明构建变体泄漏）。

### 1.1 MiFiAppToolProxyActivity（日志外发）——**簇内最高危**

| 项 | 值 |
|---|---|
| 组件 | `com.xiaomi.p300jr.scaffold.developer.tool.MiFiAppToolProxyActivity` |
| 链 | `MiFiAppToolProxyActivity → MifiLogExport.collectAndSendLog → collectAndProcessLog` |
| sink | 日志收集并以 `android.intent.action.SEND` 外发（⑨数据披露候选） |
| 置信 | partial ×2（1-2 跳回查通过） |
| 攻击语义 | 外部触发日志收集外发——若日志含账号/token/请求头则为敏感数据泄漏 |
| **验证建议** | `adb shell am start -n com.xiaomi.shop/.p300jr.scaffold.developer.tool.MiFiAppToolProxyActivity` → 观察日志外发 intent 的目标与内容（是否含 PII） |

### 1.2 WebDebugActivity（URL 启动）

| 项 | 值 |
|---|---|
| 组件 | `com.xiaomi.shop.yrnsdk.debug.WebDebugActivity` |
| 链 | `startActivity(new Intent(ACTION_VIEW, Uri.parse(...)))`；`MishopRouter.from(this).withBundle(bundle{'url'...})` |
| sink | 外部可控 URI → 任意页面打开 / 路由分发 |
| 置信 | partial ×2 |
| 攻击语义 | 深链劫持/钓鱼页面（debug 路由无白名单时） |
| 验证建议 | intent 携带 `url` extra 指向外部域 → 观察是否加载 |

### 1.3 OpenAppActivity（任意应用启动 ×3 候选）

| 项 | 值 |
|---|---|
| 组件 | `com.xiaomi.shop.yrnsdk.debug.OpenAppActivity` |
| 链 | `initRn → CommonBridgeModule.rh_openApp:2191 → PackageManager.getLaunchIntentForPackage(packageName) → startActivity` |
| sink | **按包名启动任意应用** |
| 置信 | partial ×3 |
| 攻击语义 | 跳板启动其他应用（绕过其启动防护）/ 唤起恶意 app |
| 验证建议 | intent 携带目标包名 extra → 确认无包名白名单 |

### 1.4 MiShopDebugActivity / MiniProgramActivity

- debug 写偏好（`setBooleanPref`）；`ShareManager.wakeUpMiniProgram(0.0d, username...)`
  小程序唤醒参数注入——均 partial，语义为 debug 面暴露（同簇佐证）。

**NavigationActivity → MiShopDebugActivity**：正常导航 Activity 指向 debug 组件——
攻击者可经导航入口触达 debug 面的旁证。

## 簇 2：支付回调劫持（2 组件 6 候选）

**模式语义**：exported 支付回调 Activity 接收外部伪造的支付结果——经典支付劫持面。

### 2.1 WXPayEntryActivity（微信支付结果注入）

| 项 | 值 |
|---|---|
| 链 | `initData → BaseWXApiImplV10.handleResp → WXPayEntryActivity.onResp → EventBus.post(WXpayEvent)` |
| sink | 注入携带**攻击者可控 errCode** 的支付结果事件（onResp:69） |
| 置信 | partial（2/4 跳回查通过） |
| 攻击语义 | 伪造"支付成功"事件 → 宿主业务侧误发货/解锁 |
| 验证建议 | 构造 `WXPayResp` 形态 intent 发往该组件 → 观察 EventBus 订阅方行为 |

### 2.2 AlipayResultActivity（支付宝回调注入 ×5 候选）

- `OpenAuthTask.m367a → Callback.onResult(9000, "OK", attackerBundle)`——**9000=支付
  成功码**，attackerBundle 攻击者可控（③事件注入语义）；
- 5 条链变体（onResult/m351a/mo352a 分发路径）——同 sink 的多路径覆盖。

## 簇 3：单点强发现

### 3.1 ShareImageReceiverActivity → SharedPreferences 注入（**validated ×2**）

| 项 | 值 |
|---|---|
| 链 | `ShareImageReceiverActivity → PreferenceUtil.setStringPref:93 → Editor.putString/apply` |
| sink | 持久状态写（键 `PREF_KEY_SHARE_IMAGES_INFO_MD5`） |
| 置信 | **validated**（2/2 与 4/4 跳回查全通过） |
| 攻击语义 | exported 分享接收器 → 外部可控值写共享偏好（配置/状态注入——若该键参与
  分享校验逻辑则为校验绕过） |
| **特殊意义** | **F5 复发检测的直接实证**——该组件是规则轨 ACTIVITY_INTENT_TO_SENSITIVE_SINK
  的 finding 组件，探索轨在其相邻面产出了规则未覆盖的新 sink |
| 验证建议 | intent 携带可控 extra → 检查写入值是否外部可控 + 该 MD5 键的消费逻辑 |

### 3.2 MainActivity → 账号读取（validated）

- `LoginManager.getAccountId:272`（读 com.xiaomi 账户名，⑨隐私封装）→ line 540 流入
  `SubProcessLoginManager.broadcastLogin` 外发——账号信息广播链。

### 3.3 PluginCartActivity → 反射类加载

- `BasePluginActivity.resetPluginFragment:269 → ClassLoader.loadClass(fragmentClass).newInstance()`
  （①反射实例化）——**若 fragmentClass 来自 intent extra 则为远程类加载**（插件化经典
  漏洞）。partial（1/2 跳），**建议优先人工确认 fragmentClass 的数据来源**。

### 3.4 MMKVContentProvider → ashmem 句柄披露

- `mmvFromAshmemID → MMKV.mmkvWithAshmemID:268 → ParcelableMMKV（携带 ashmem FD）经
  Bundle 返回调用方`——exported provider 的共享内存句柄披露（已知 MMKV 攻击面：
  跨进程读写应用配置）。partial（2/2 跳回查通过）。

### 3.5 UpdateProvider → 任意文件删除（partial ×2）

- `UpdateProvider.delete:140 → C4029a.m8589a(uri) 解析 File → File.delete()`——
  exported provider 对 URI 解析路径的文件删除（DoS/篡改）。**华为 HMS SDK 的已知
  攻击面模式**。

---

## 对照与局限

| 维度 | 结论 |
|---|---|
| golden extra-close | **未命中**（MainActivity 仅产出 login 方向——方向惯性依旧） |
| 规则轨正交性 | 全部 39 候选与规则轨零同链——debug 暴露/支付劫持/MMKV/反射加载均为规则模式外 |
| 覆盖局限 | 128 入口 error 未探索（含 13 个 F5 引导域入口的部分）——簇完整性待 v4 |

## 后续

1. **T1-v4 复核**（跑中）：error 率达标后在完整数据上重做本分析——预期簇覆盖更全
  （debug 簇/支付簇的更多入口 + 引导域 13 入口产出）；
2. **人工验证排序建议**：3.1（validated+F5 实证）→ 1.1（日志外发 PII）→ 2.1/2.2
  （支付劫持 POC）→ 3.3（反射加载数据源确认）；
3. **taxonomy 供给**：本批 partial 的 custom sink（日志外发/支付事件注入/ashmem
  披露/文件删除）是 F3 taxonomy 扩充的候选样本。
