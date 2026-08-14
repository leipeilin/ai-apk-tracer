# CommonBaseActivity Fragment 注入 POC

V-03 漏洞动态验证工具。验证两个导出的 CommonBaseActivity 是否允许外部指定任意内部 BaseFragment。

## 原理

两个 CommonBaseActivity 均以 `exported=true` 导出且无权限：

- `com.xiaomi.fitness.baseui.common.CommonBaseActivity`
- `com.xiaomi.fitness.devicesettings.base.CommonBaseActivity`

Activity 从 Intent extra `fragment_param` 读取 `FragmentParams` Parcelable，提取 `className` 后通过 `Class.forName(className).newInstance()` 反射实例化任意 `BaseFragment` 子类，并通过 `setArguments(bundle)` 注入外部 Bundle。无类名白名单。

## 技术方案

`FragmentParams` 是应用内自定义的 Parcelable，ADB 无法直接传递。本 POC 通过 `DexClassLoader` 加载目标 APK，获取 `FragmentParams.CREATOR`，用 `Parcel` 手动写入字段后调用 `createFromParcel` 构造对象，不依赖混淆方法名。

## 构建

```bash
cd tools/commonbase-activity-poc
./gradlew assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk
```

## 使用

### 1. 启动 POC 应用

```bash
adb shell am start -n com.example.commonbase.poc/.MainActivity
```

应用启动后会自动加载目标 APK（`com.mi.health`），状态栏显示 "APK 已加载" 后即可操作。

### 2. 选择目标 Activity

点击顶部按钮选择要测试的 CommonBaseActivity：
- `baseui.common.CommonBaseActivity`（新版）
- `devicesettings.base.CommonBaseActivity`（旧版，已 @Deprecated）

### 3. 发送 Fragment 注入

点击对应按钮发送不同的 Fragment 类名：

| 按钮 | Fragment | 说明 |
|---|---|---|
| GuideWearInstructionFragment | 穿戴引导页 | 无副作用，验证实例化是否成功 |
| GuideWebViewFragment | WebView 页 | 含 URL host 校验，验证 guard |
| DeviceInstallAPPDebugFragment | 调试页 | 调试功能页面 |
| EmergencyContactListFragment | 紧急联系人 | 可能泄露联系人数据 |
| 自定义 Fragment 类名 | 任意类名 | 输入自定义类名测试 |

### 4. 可达性验证

- **发送空 fragment_param**：验证 Activity 是否可被外部启动（预期显示"页面不存在"或 finish）
- **发送无效类名**：验证 `Class.forName` 失败时的处理（预期 toast 或崩溃）

### 5. 查看结果

发送后观察手机屏幕是否打开了目标 Fragment，同时查看 logcat：

```bash
adb logcat -d | grep -iE "CommonBaseActivity|fragmentParam|target fragment"
```

成功时 logcat 会显示：
```
CommonBaseActivity: fragmentParam:com.xiaomi.fitness.devicesettings.guide.GuideWearInstructionFragment
target fragment com.xiaomi.fitness.devicesettings.guide.GuideWearInstructionFragment
```

## FragmentParams 构造格式

从反编译源码确认的 `writeToParcel` 顺序：

```
parcel.writeByte(backAble ? 1 : 0)   // boolean: 是否可返回
parcel.writeBundle(bundle)            // Bundle: Fragment 参数
parcel.writeString(className)         // String: Fragment 类名
parcel.writeByte(isResizeMode ? 1 : 0) // boolean: 是否调整模式
```

`CREATOR.createFromParcel` 按相同顺序读取，本 POC 直接用 Parcel 写入后调用 `createFromPal` 构造对象。

## 验证清单

| 测试项 | 方法 | 预期结果 |
|---|---|---|
| 组件可达 | 发送空 fragment_param | Toast "页面不存在" 或 finish |
| Fragment 实例化 | 发送 GuideWearInstructionFragment | 页面正常显示 |
| 无效类名 | 发送 `com.fake.NotExist` | Toast 或崩溃 |
| WebView URL 校验 | 发送 GuideWebViewFragment + 外部 URL | URL 被清空，加载空白页 |
| WebView 合法 URL | 发送 GuideWebViewFragment + watch.iot.mi.com | 正常加载 |
| Bundle 注入 | 向 Fragment 注入自定义参数 | 观察参数是否被使用 |
