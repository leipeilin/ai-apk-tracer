# SportXmsService 无鉴权 AIDL 动态验证 POC v2

## 用途

验证 `com.mi.health`（小米运动健康 3.57.0）的导出 Service `SportXmsService` 是否允许普通第三方应用绑定并调用 AIDL 接口。

**v2 扩展**：从 v1 的 4 个只读 transaction 扩展至全部 25 个 transaction，覆盖用户信息获取、使用数据监听和设备控制。

## 验证能力

### 1. 只读查询（自动，无副作用）

| transaction | 方法 | 返回 | 说明 |
|---|---|---|---|
| 11 | isDeviceConnected() | boolean | 设备连接状态 |
| 14 | getDeviceBattery() | String | 设备电量 |
| 15 | isSupportSomatosensoryGame() | boolean | 是否支持体感游戏 |
| 16 | hasOngoingSport() | boolean | 是否有进行中的运动 |
| 20 | isOpenPaidFeatures() | boolean | 是否开启付费功能 |
| 23 | getDeviceInfo() | DeviceInfo | 设备名称/型号/DID |

### 2. 用户信息获取（回调）

| transaction | 方法 | 回调数据 | 说明 |
|---|---|---|---|
| 10 | setXiaomiUserInfoListener | UserInfo(name, icon, sex, account) | 小米账号用户名/头像/性别/账号ID |
| 19 | getAidongMemberExpireTimestamp | long timestamp | 爱动会员过期时间 |
| 25 | setAidongCourseVipInfoListener | CourseVipInfo(active, expiredAt) | 课程VIP信息 |

### 3. 使用数据监听（回调注册，持续监听）

| transaction | 方法 | 回调数据 | 说明 |
|---|---|---|---|
| 7 | setSportStateChangedListener | onSportStarted/Paused/Restarted/Finished | 运动状态变化 |
| 8 | setSportXmsDataChangedListener | PhoneData(dur, hr, cal) | 运动数据（时长/心率/卡路里） |
| 9 | setSportXmsSensorDataChangedListener | WearSensorData(accel, gyro) | 传感器数据（加速度计/陀螺仪） |
| 12 | setDeviceConnectedListener | onConnectStart/Success/Failure/Disconnect | 设备连接状态变化 |

### 4. 设备控制（需用户确认，有副作用）

| transaction | 方法 | 参数 | 说明 |
|---|---|---|---|
| 1 | startSport(did, SportXmsRequestData) | did, timestamp, timezone, sportType, sportState, courseId | 开始运动 |
| 2 | pauseSport(did, sportType) | did, sportType | 暂停运动 |
| 3 | resumeSport(did, sportType) | did, sportType | 恢复运动 |
| 5 | restartSport(did, sportType) | did, sportType | 重新开始运动 |
| 6 | abnormalChangeSportStateToFinish() | 无 | 异常结束运动 |
| 13 | shake(vibrateLevel) | int | 触发设备振动 |
| 22 | switchCurDeviceMode(mode, callback) | mode, callback | 切换佩戴模式 |
| 24 | finishSportByType(did, sportType) | did, sportType | 按类型结束运动 |

### 5. 设备查询（回调）

| transaction | 方法 | 回调数据 | 说明 |
|---|---|---|---|
| 17 | isDeviceSporting(callback) | boolean | 设备是否在运动中 |
| 21 | getCurDeviceMode(callback) | int mode | 当前佩戴模式 |

## 工程结构

```
sportxms-poc/
  settings.gradle
  build.gradle
  gradle.properties
  gradle/wrapper/
  app/
    build.gradle
    src/main/
      AndroidManifest.xml     # 无权限声明，含 <queries>
      java/com/example/sportxms/poc/
        MainActivity.java     # 核心验证代码（含回调 Binder 桩）
      res/
        layout/activity_main.xml  # 分类按钮 + 日志区
        values/strings.xml
        values/themes.xml
```

## 使用方法

### 1. 构建安装

```bash
cd tools/sportxms-poc
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

### 2. 启动 POC

```bash
adb shell am start -n com.example.sportxms.poc/.MainActivity
```

启动后自动绑定 SportXmsService，绑定成功后可点击按钮执行各验证项。

### 3. 验证流程

1. **自动只读**：点击"自动只读"按钮，自动执行 6 个只读查询（设备信息/电量/连接状态等）
2. **获取用户信息**：点击"获取用户信息"按钮，注册回调等待 UserInfo 返回
3. **注册数据监听**：点击"注册监听"按钮，注册全部 4 个回调（运动状态/数据/传感器/连接）
4. **设备控制**：点击控制按钮（需在弹窗中确认），向手环发送控制指令
5. **设备查询**：点击"设备查询"按钮，查询当前运动状态和佩戴模式

### 4. 证据采集

```bash
# 采集完整日志
adb logcat -s SportXmsPOC -d > poc_v2_result.txt

# 采集系统级服务连接记录
adb shell dumpsys activity services com.mi.health | grep -A 20 SportXmsService

# 采集测试应用 UID
adb shell dumpsys package com.example.sportxms.poc | grep userId

# 采集目标应用 UID
adb shell dumpsys package com.mi.health | grep userId

# 采集签名差异
keytool -printcert -jarfile app/build/outputs/apk/debug/app-debug.apk
keytool -printcert -jarfile <目标APK路径>
```

## 常量来源

所有 DESCRIPTOR、transaction 编号、回调接口描述符和 Parcelable 字段顺序均从反编译源码逐行核对：

- 主接口 DESCRIPTOR: `v5e.java:155`
- transaction 编号: `v5e.java:18-42`
- onTransact 实现: `v5e.java:172-288`（无 UID/签名/权限校验）
- Manifest: `AndroidManifest.xml:2271`（`exported=true`，无 `android:permission`）
- 回调接口描述符: `a4e/y3e/z3e/c4e/k3e/n3e/h3e/l3e/i3e.java` 中 `attachInterface()` / `writeInterfaceToken()`
- Parcelable 字段: `SportXmsRequestData/SportXmsFinishData/UserInfo/PhoneData/CourseVipInfo/WearSensorData.java`

## 回调接口描述符对照

| 回调接口 | 描述符 | 使用 transaction |
|---|---|---|
| IRemoteSportXmsStateChangedListener | `com.xiaomi.fitness.sport_xms.listener.IRemoteSportXmsStateChangedListener` | 7 |
| IRemoteSportXmsDataChangedListener | `com.xiaomi.fitness.sport_xms.listener.IRemoteSportXmsDataChangedListener` | 8 |
| IRemoteSportXmsSensorDataChangedListener | `com.xiaomi.fitness.sport_xms.listener.IRemoteSportXmsSensorDataChangedListener` | 9 |
| IRemoteUserInfoListener | `com.xiaomi.fitness.sport_xms.listener.IRemoteUserInfoListener` | 10 |
| IRemoteDeviceConnectedListener | `com.xiaomi.fitness.sport_xms.listener.IRemoteDeviceConnectedListener` | 12 |
| IRemoteIsDeviceSportingListener | `com.xiaomi.fitness.sport_xms.listener.IRemoteIsDeviceSportingListener` | 17 |
| IRemoteAidongMemberListener | `com.xiaomi.fitness.sport_xms.listener.IRemoteAidongMemberListener` | 19 |
| IRemoteDeviceModeListener | `com.xiaomi.fitness.sport_xms.listener.IRemoteDeviceModeListener` | 21, 22 |
| IRemoteCourseVipInfoListener | `com.xiaomi.fitness.sport_xms.listener.IRemoteCourseVipInfoListener` | 25 |

## 注意事项

- 测试 APK 不申请任何权限，使用默认 debug 签名
- `AndroidManifest.xml` 包含 `<queries>` 声明以兼容 Android 11+
- 控制类操作（开始/暂停/结束运动、振动、切换模式）需在弹窗中二次确认
- 回调注册后将持续接收数据，直到服务断开或应用退出
- 传感器数据监听仅在运动期间有效（由 startSport 触发注册）
- 建议在隔离测试环境中进行控制类验证，避免影响正常使用
