# 44 封顶 sink 评审清单（F3-1）

> **依据**：`explorer-track/2026-08-27-explorer-gap-fix-plan.md` F3-1
> **数据源**：run `eada0e71`（shop）50 候选中 44 个"sink 未命中 taxonomy"封顶候选——sink 描述 + 末跳结构化方法（`path#method:line`）+ source 语义 + 组件四维交叉判定
> **判定口径**：A=真敏感建议扩充（含 taxonomy 归类与 receiver 约束草案）；B=否决（常规操作，无论 taxonomy 怎么扩都不该收录）；存疑=需人工读码确认（混淆方法/语义不明）

## 汇总

| 判定 | 数量 | 处置 |
|---|---|---|
| **A（建议扩充）** | **4** | 经你确认后 `promote_custom_sink.py` 加入（taxonomy_version 1.0.0→1.1.0） |
| **存疑（待读码）** | **6** | 本轮不扩充——混淆方法需人工读码确认语义（建议后续抽查） |
| **B（否决）** | **34** | 不扩充——印证 F2（prompt 敏感度约束）的必要性 |

**解封预期**：A 类 4 条目 → 对应 **4 个候选**（#3/#4/#5/#6）解封 partial→validated。

---

## A 类明细（4 条——建议扩充）

| # | sink（末跳方法） | taxonomy 归类 | 理由 | 扩充条目草案（receiver_exact 精准零误报） |
|---|---|---|---|---|
| 3 | `LoginManager.getPrefEncryptedUserId:540`（com.xiaomi.shop2.account.lib） | **data_disclosure** | 读加密存储的用户 ID——账号隐私读取（外部 activity result → 登录态读取账号标识） | `{method: getPrefEncryptedUserId, receiver_exact: ["com.xiaomi.shop2.account.lib.LoginManager"], taxonomy: data_disclosure, source: manual}` |
| 4 | `LoginManager.getAccountId:540`（同上） | **data_disclosure** | 读账户 ID——同上（账号标识披露） | `{method: getAccountId, receiver_exact: ["com.xiaomi.shop2.account.lib.LoginManager"], taxonomy: data_disclosure, source: manual}` |
| 5 | `PreferenceUtil.setStringPref:93`（com.xiaomi.shop2.util） | **persistent_state_write** | 外部 intent 的 Bundle extra **直接写入持久偏好**（ShareImageReceiver——外部可控输入污染持久层，经典注入面） | `{method: setStringPref, receiver_exact: ["com.xiaomi.shop2.util.PreferenceUtil"], taxonomy: persistent_state_write, source: manual}` |
| 6 | `PreferenceUtil.setIntPref:53`（同上） | **persistent_state_write** | 同上（整型偏好写） | `{method: setIntPref, receiver_exact: ["com.xiaomi.shop2.util.PreferenceUtil"], taxonomy: persistent_state_write, source: manual}` |

**通用性说明**：条目用 `receiver_exact`（FQCN 精确匹配）——只认本 APK 的这两个类，零误报；若未来想要跨 App 通用（任意 LoginManager.getAccountId），可放宽为 `receiver_leaves`，但误报风险需另行评估（本轮保守）。

## 存疑明细（6 条——待人工读码，本轮不扩充）

| # | sink（末跳） | 存疑点 | 读码建议 |
|---|---|---|---|
| 10 | `StatService2.onError:351`（小米统计服务） | 错误上报——遥测外发有 data_disclosure 语义，但 onError 本身可能仅记录 | 看方法体是否触发网络上报（含哪些数据字段） |
| 13 | `AuthActivity.m16382a:43`（腾讯 OAuth） | **混淆方法**——"OAuth callback processing"是模型推断 | 读 m16382a 实际逻辑（是否处理 token/code 并转发） |
| 14 | `C6007f.m16147c:80`（QQ SDK） | **混淆方法**——"helper processing activity result" | 同上（QQ 互联 SDK 的回调链） |
| 24 | `C1135f.m2144a:31`（高德定位 APSService） | **混淆方法**——定位服务组件的 Binder 构造；若内部启动定位则属 location_sensor_collection | 读 onBind→m2144a 链是否发起定位请求 |
| 25 | `C1298la.m3585a:329`（高德定位） | 同上（混淆 Binder 管理） | 同上 |
| 26 | `MMKVContentProvider.mmkvFromAshmemID`（腾讯 MMKV） | **跨进程存储实例获取**——provider call() 的 ashmem ID 参数可控 → MMKV 实例；但实例获取本身非终点（真实敏感在其后 encode/decode——链未走到） | 读 call() 的完整链（参数是否流向 MMKV 写）——若通，链应扩展到 encode 而非停在实例获取 |

## B 类明细（34 条——否决，按模式归并）

| 模式 | 条目 | 否决理由 |
|---|---|---|
| **UI 生命周期/导航**（9） | finish()×2（#1/#43）、onBackPressed×3（#37/39/40）、showControlsIfCan（#36）、updateFinishButton（#38）、showLicense（#12）、attachYrnModule（#34） | UI 控制流——无数据语义 |
| **单例/工厂获取**（6） | PushClient/JrManager/MiotStoreApi/WBH5FaceVerifySDK/PluginGc.getInstance、LoginManager.login（#2——流程发起非终点） | 实例获取非敏感操作 |
| **初始化/入口分发**（10） | initData×3（#19/20/21）、initFragment（#28）、initRn×2（#29/30）、handleIntent×2（#27/33）、ImageSelector.onResult（#17）、SkyTreeUtils.upgradeAfterPerm（#44） | 入口处理逻辑——链的起点行为被误当终点 |
| **日志/结果回传**（2） | Log.e（#11）、setResultData（#15） | 非敏感输出通道 |
| **业务中间逻辑**（7） | syncPluginById×3（#7/8/16——插件同步业务）、genBarcodeBitmap（#41）、handleLocalImage（#42——图片解析业务）、takeFinish（#35）、PluginGc 触发（#23——GC 无敏感性） | 业务流程节点，非敏感终点 |

**B 类的结构性观察**（反哺 F2）：34 条 B 类中 16 条是"单例获取/初始化/入口分发"——即**模型把链尾恰好走到的普通方法当 sink**，与 sink 库缺条目无关。F2（prompt 注入九类敏感语义 + 禁令）预计可消除大部分此类无效链。

---

## 执行建议

1. **你确认 A 类 4 条**（或增删）→ 我执行 `promote_custom_sink.py`（manual 条目，taxonomy_version 1.1.0）；
2. **重校验**：`revalidate_run_candidates` 对 eada0e71 复评——预期 4 候选解封（validated 0→4）；
3. **存疑 6 条**：建议作为后续读码抽查任务（不阻塞本轮）；也可在 health run 后一并分析。
