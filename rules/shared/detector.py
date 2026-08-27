"""基于清单可达性与源码证据执行共享 Android 安全规则判定。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from typing import Any

from shared.authorization import evaluate_authorization, operation_for_rule
from shared.dataflow import DataFlowAnalyzer, classify_call_operation, classify_operation_taxonomy
from shared.index_reader import RuleIndexReader
from shared.receiver_registration import parse_receiver_registrations

RULE_META = {
    "ACTIVITY_EXPORTED_NO_PERMISSION": ("activity", "L1", "informational"),
    "ACTIVITY_INTENT_TO_SENSITIVE_SINK": ("activity", "L2", "medium"),
    "ACTIVITY_EXTERNAL_ROUTE_INJECTION": ("activity", "L2", "medium"),
    "SERVICE_EXPORTED_NO_PERMISSION": ("service", "L1", "informational"),
    "SERVICE_BINDER_CALLER_CHECK_MISSING": ("service", "L2", "high"),
    "SERVICE_IPC_INPUT_TO_SINK": ("service", "L2", "high"),
    "PROVIDER_READ_WRITE_PERMISSION_MISSING": ("provider", "L1", "informational"),
    "PROVIDER_CALLER_CHECK_MISSING": ("provider", "L2", "high"),
    "PROVIDER_URI_TO_FILE": ("provider", "L2", "high"),
    "PROVIDER_SQL_STRUCTURE_INJECTION": ("provider", "L2", "high"),
    "PROVIDER_UNAUTHORIZED_QUERY": ("provider", "L2", "medium"),
    "PROVIDER_UNAUTHORIZED_MUTATION": ("provider", "L2", "high"),
    "RECEIVER_EXPORTED_NO_PERMISSION": ("receiver", "L1", "informational"),
    "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION": ("receiver", "L1", "informational"),
    "RECEIVER_INPUT_TO_SINK": ("receiver", "L2", "medium"),
    "IMPLICIT_BROADCAST_SENSITIVE_DATA": ("receiver", "L2", "medium"),
    "ACTIVITY_SENSITIVE_NAME_HINT": ("activity", "L1", "informational"),
    "PROVIDER_LOOSE_URI_MATCH": ("provider", "L1", "informational"),
    "ORDERED_BROADCAST_UNRESTRICTED": ("receiver", "L1", "informational"),
    # 本地存储/配置族（§12，v2026-08-09）：纯 manifest 事实，不扫描代码。
    "DEBUGGABLE_IN_PRODUCTION": ("manifest", "L1", "high"),
    "ALLOW_BACKUP_ENABLED": ("manifest", "L1", "medium"),
    "CLEARTEXT_TRAFFIC_ALLOWED": ("manifest", "L1", "medium"),
    # WebView 家族（§12.2 ②，v2026-08-09）：全局代码模式，不绑定清单组件。
    "WEBVIEW_JS_BRIDGE_EXPOSED": ("webview", "L2", "critical"),
    "WEBVIEW_FILE_ACCESS_ENABLED": ("webview", "L2", "high"),
    "WEBVIEW_UNIVERSAL_ACCESS_FROM_FILE": ("webview", "L2", "high"),
    "WEBVIEW_SSL_ERROR_IGNORED": ("webview", "L2", "high"),
    "WEBVIEW_EXTERNAL_CONTENT": ("webview", "L2", "medium"),
    # 密码学/证书校验族（§12.2 ③，v2026-08-09）：全局代码模式。
    "TRUST_MANAGER_ALL_ACCEPT": ("crypto", "L2", "critical"),
    "HOSTNAME_VERIFIER_ALWAYS_TRUE": ("crypto", "L2", "high"),
    "WEAK_CIPHER_ECB": ("crypto", "L2", "medium"),
}
# WebView/密码学全局代码规则（§12.2 ②③）：不绑定清单组件，走 _global_code_rule 分支。
GLOBAL_CODE_RULES = {
    "WEBVIEW_JS_BRIDGE_EXPOSED",
    "WEBVIEW_FILE_ACCESS_ENABLED",
    "WEBVIEW_UNIVERSAL_ACCESS_FROM_FILE",
    "WEBVIEW_SSL_ERROR_IGNORED",
    "WEBVIEW_EXTERNAL_CONTENT",
    "TRUST_MANAGER_ALL_ACCEPT",
    "HOSTNAME_VERIFIER_ALWAYS_TRUE",
    "WEAK_CIPHER_ECB",
}
AUXILIARY = {
    "ACTIVITY_SENSITIVE_NAME_HINT",
    "PROVIDER_LOOSE_URI_MATCH",
    "ORDERED_BROADCAST_UNRESTRICTED",
}
MANIFEST_ONLY_RULES = {
    "ACTIVITY_EXPORTED_NO_PERMISSION",
    "SERVICE_EXPORTED_NO_PERMISSION",
    "PROVIDER_READ_WRITE_PERMISSION_MISSING",
    "RECEIVER_EXPORTED_NO_PERMISSION",
    "ACTIVITY_SENSITIVE_NAME_HINT",
}
# 本地存储/配置族（§12）：纯 manifest 事实规则，独立分支处理，不遍历组件。
MANIFEST_FACT_RULES = {
    "DEBUGGABLE_IN_PRODUCTION",
    "ALLOW_BACKUP_ENABLED",
    "CLEARTEXT_TRAFFIC_ALLOWED",
}
# Source 表示来自 Intent、IPC、Provider URI 或广播的外部可控输入。
SOURCE_PATTERNS = {
    "intent_extra": re.compile(r"(?:getIntent\s*\(\s*\)|\bintent\b).*?(?:get\w*Extra|getData|getAction|getExtras)", re.I | re.S),
    "ipc_input": re.compile(r"(?:onStartCommand|Parcel\.read\w+|Bundle\.get|Intent\.get\w*Extra|aidl)", re.I),
    "provider_uri": re.compile(r"\b(?:Uri|uri)\b.*?(?:getPath|getPathSegments|getLastPathSegment|getQueryParameter)", re.I | re.S),
    "receiver_input": re.compile(r"onReceive\s*\(.*?Intent.*?(?:get\w*Extra|getData|getAction|getExtras)", re.I | re.S),
}
# Sink 表示文件、组件启动、数据库等可能产生安全影响的敏感操作。
SINK_PATTERNS = {
    "file": re.compile(r"\b(?:new\s+File|FileInputStream|FileOutputStream|ParcelFileDescriptor\.open|openFileOutput)\b"),
    "webview": re.compile(r"\b(?:loadUrl|evaluateJavascript|addJavascriptInterface)\s*\("),
    "component_launch": re.compile(r"\b(?:startActivity|startService|bindService|sendBroadcast)\s*\("),
    "database": re.compile(r"\b(?:rawQuery|execSQL|insert|update|delete|applyBatch)\s*\("),
    "sensitive_state": re.compile(r"\b(?:setPassword|setPermission|setAdmin|setEnabled|AccountManager\s*\.|Settings\.(?:Secure|Global)|SharedPreferences\.Editor)\b"),
    "network": re.compile(r"\b(?:HttpURLConnection|OkHttpClient|newCall|enqueue|Call\s*\.\s*execute)\b"),
}
# 手动同步点（v2026-08-09）：与 rules/shared/dataflow.py GUARD_METHODS 同源。
# 当前无调用点（保留供工具/测试引用），但必须与 dataflow.GUARD_METHODS 保持
# 一致——新增调用者身份校验 API（getNameForUid/getPackageInfo）后此处同步。
GUARD_RE = re.compile(r"(?:checkCallingPermission|enforceCallingPermission|checkCallingOrSelfPermission|Binder\.getCallingUid|getNameForUid|getPackageInfo|PackageManager\.checkSignatures|enforceReadPermission|enforceWritePermission|SecurityException)")
SENSITIVE_DATA_RE = re.compile(r"(?:token|password|passwd|secret|credential|account|payment|location|contact|private|auth|device|battery|userId)", re.I)
SENSITIVE_BINDER_METHOD_RE = re.compile(
    r"\b(?:start|stop|finish|pause|resume|delete|set|register|get)(?:Sport|Workout|Sensor|Account|User|Device|Battery|Location|Wear|Data|State)\w*\s*\(",
    re.I,
)
COMPONENT_FLOW_ENTRIES = {
    "ACTIVITY_INTENT_TO_SENSITIVE_SINK": {"onCreate", "onNewIntent"},
    "ACTIVITY_EXTERNAL_ROUTE_INJECTION": {"onCreate", "onNewIntent"},
    "SERVICE_IPC_INPUT_TO_SINK": {"onStartCommand", "onBind"},
    "RECEIVER_INPUT_TO_SINK": {"onReceive"},
}
# P2-6（2026-08-15）：外部可控路由注入。
#
# 与 ACTIVITY_INTENT_TO_SENSITIVE_SINK 的本质差异是 **sink 语义**：
# 后者追"值流到达已知敏感 API"，因此应用自定义的路由 wrapper（如
# BasePluginFragment.Fasade.startNewPluginActivity）不在 effect 表内、永远不成 sink；
# 且 dataflow.classify_operation_taxonomy 对 resolved_target 非空的调用直接返回
# is_effect=False（要求进入真实 callee），插件 Activity 不在 manifest 组件索引中、
# resolve 失败即丢弃。
#
# v04 真机验证成立的漏洞（extra_splashinfo → startNewPluginActivity → ACTION_ROOT
# 隐式路由启动任意插件 + 全量 extras 注入）正是因此漏检。本规则把 sink 改为
# 「路由能力」：外部输入决定了 **启动目标** 或被 **全量透传** 进目标组件。
ROUTE_TARGET_METHODS = re.compile(
    r"\b(?:setClassName|setComponent|setClass|setAction|setPackage)\s*\(",
)
# 目标决策调用中"目标静态固定"的强证据（P1-5 打通，2026-08-15；2026-08-15 修订）：
#   - 类字面量：setClass(this, WbShareTransActivity.class)；
#   - 纯字符串字面量参数：setClassName("pkg","cls") / setAction("ACTION") / setPackage("pkg")；
#   - setComponent(new ComponentName("pkg","cls"))：组件名两个分量均为字符串字面量。
# 判定按方法语义 + 顶层参数整体匹配。修订前用 search 匹配参数区任意位置的字符串字面量，
# 会把 getStringExtra("key") 的 key、拼接表达式的前缀误判为目标固定——方向恰是
# "误判 fixed → 采信 fixed_local_target → ai_false_positive → 漏报"。因此：
#   - setAction/setPackage：唯一参数必须是纯字符串字面量；
#   - setClass：第二参数（目标类）必须是纯类字面量；
#   - setClassName：前两个参数（包名+类名）必须**都是**纯字符串字面量——任一外部可控即不固定；
#   - setComponent：唯一参数必须是 new ComponentName(纯字符串, 纯字符串)。
# 变量、方法调用、拼接表达式、常量引用一律 False。
_STRING_LITERAL_FULL = re.compile(r"^(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')$")
_CLASS_LITERAL_FULL = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\.class$")
_COMPONENT_NAME_LITERAL_RE = re.compile(
    r"^new\s+ComponentName\s*\(\s*\"(?:[^\"\\]|\\.)*\"\s*,\s*\"(?:[^\"\\]|\\.)*\"\s*\)$"
)


def _matching_paren_end(content: str, opening: int) -> int | None:
    """返回与 ``opening`` 处左括号配对的右括号下标（处理嵌套与引号转义）。"""
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(content)):
        char = content[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _split_top_level_args(args: str) -> list[str]:
    """按顶层逗号拆分参数列表（忽略括号内与引号内的逗号及引号内转义）。"""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    escaped = False
    current: list[str] = []
    for char in args:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
    return parts


def _ordered_broadcast_has_permission(code: str) -> bool:
    """任一 sendOrderedBroadcast 调用的 receiverPermission（第 2 个顶层参数）非 null 字面量即视为已限制。

    替代旧逐字符否定正则 ``[^,]+,\\s*[^n][^u][^l][^l]``：该写法无法处理第一
    参数的内嵌逗号——``sendOrderedBroadcast(new Intent("a","b"), null)`` 会被
    误判为已限制（无权限的有序广播漏报，评审 2026-08-27 E1）。此处按顶层
    参数拆分后判 ``null`` 字面量；receiverPermission 为变量/常量引用时视为
    已限制（运行时值未知，保守取"有限制"方向，规则本身为 auxiliary 加权信号）。
    """

    for match in re.finditer(r"sendOrderedBroadcast\s*\(", code):
        closing = _matching_paren_end(code, match.end() - 1)
        if closing is None:
            continue
        args = _split_top_level_args(code[match.end():closing])
        if len(args) >= 2:
            permission_arg = args[1].strip()
            if permission_arg and permission_arg != "null":
                return True
    return False


def _target_decision_is_fixed(match: re.Match[str], content: str) -> bool:
    """目标决策调用（setClassName/setComponent/setClass/setAction/setPackage）的目标
    是否静态固定。仅供 target_selection 类候选使用；bulk_extras_forwarding 无目标
    决策，不适用。

    按方法语义对顶层参数做整体判定（保守）：只有纯字面量才算固定，其余一律 False。
    """

    method = match.group(0).rstrip("(").strip()
    opening = match.end() - 1  # 正则已消费到 '('，其前一位即左括号
    closing = _matching_paren_end(content, opening)
    if closing is None:
        return False
    args = content[opening + 1: closing]
    params = _split_top_level_args(args)

    if method == "setAction" or method == "setPackage":
        # 唯一参数即目标值，必须整体为字符串字面量。
        return len(params) == 1 and bool(_STRING_LITERAL_FULL.match(params[0]))
    if method == "setClass":
        # 第二参数是目标类；第一参数是 context/this，不参与判定。
        return len(params) >= 2 and bool(_CLASS_LITERAL_FULL.match(params[1]))
    if method == "setClassName":
        # 包名与类名共同决定目标组件，任一外部可控即不固定。
        return (
            len(params) >= 2
            and bool(_STRING_LITERAL_FULL.match(params[0]))
            and bool(_STRING_LITERAL_FULL.match(params[1]))
        )
    if method == "setComponent":
        # 目标组件由 ComponentName 决定，两个分量都必须为字符串字面量。
        return len(params) == 1 and bool(_COMPONENT_NAME_LITERAL_RE.match(params[0]))
    return False


# 常量引用形态的消费方类名解析（P1③ 补强，2026-08-15）：setClassName/setComponent
# 的类名参数是**常量引用**（如 setClassName(ShopApp.instance, SERVICE_CLASS_NAME)）
# 时，字面量正则不命中——v04 实证 StatService2 4 条候选均此形态，consumer_semantics
# 漏判。补强：识别常量标识符后在同文件 content 查 `常量名 = "..."` 定义解析目标类名。
# 仅同文件解析；跨文件常量（查不到定义）保守返回空串（不降级）——避免把外部类常量
# 误判。与现有 setClassName/setComponent 字面量判定共用同一统计语义正则，零偏移。
_CONSUMER_CLASS_LITERAL_RE = re.compile(
    r"setClassName\s*\([^,]+,\s*\"([^\"]+)\"",
)
_CONSUMER_COMPONENT_LITERAL_RE = re.compile(
    r"setComponent\s*\(\s*new\s+ComponentName\s*\(\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*\)",
)
_CONSUMER_CLASS_CONSTANT_RE = re.compile(
    r"setClassName\s*\([^,]+,\s*([A-Z][A-Z0-9_]*)\b",
)
_CONSUMER_COMPONENT_CONSTANT_RE = re.compile(
    r"setComponent\s*\(\s*new\s+ComponentName\s*\(\s*\"[^\"]+\"\s*,\s*([A-Z][A-Z0-9_]*)\b",
)


def _resolve_consumer_name(content: str, file_content: str) -> str:
    """解析显式启动目标的消费方类名（简单名），供 bulk 消费方语义分级。

    优先级：setClassName 字面量 > setComponent 双字面量 > 常量引用（同文件解析）。
    常量引用的目标类名定义须在同文件 content 中可查（`常量名 = "..."`），
    查不到（跨文件常量/外部 SDK 常量）返回空串——保守不降级。
    """

    class_literal = _CONSUMER_CLASS_LITERAL_RE.search(content)
    if class_literal:
        return class_literal.group(1).rsplit(".", 1)[-1]
    component_literal = _CONSUMER_COMPONENT_LITERAL_RE.search(content)
    if component_literal:
        return component_literal.group(2).rsplit(".", 1)[-1]
    for constant_re in (_CONSUMER_CLASS_CONSTANT_RE, _CONSUMER_COMPONENT_CONSTANT_RE):
        const_match = constant_re.search(content)
        if const_match:
            const_name = const_match.group(1)
            definition = re.search(
                rf"\b{re.escape(const_name)}\s*=\s*\"([^\"]+)\"",
                file_content,
            )
            if definition:
                return definition.group(1).rsplit(".", 1)[-1]
    return ""


# 显式启动目标类提取（P0①，2026-08-15）：从 sink 方法内容中识别启动目标的
# 类字面量形态——new Intent(this, X.class) / setClass(this, X.class) /
# setComponent(new ComponentName("pkg","cls"))。仅匹配**类字面量**（X.class）
# 或 ComponentName 双字符串字面量，变量/表达式（new Intent(this, clsVar)）不提取
# ——目标类无法静态确定时不做注册检查（保守，避免把"目标不可知"误判为"未注册"）。
_EXPLICIT_LAUNCH_CLASS_RE = re.compile(
    r"(?:new\s+Intent\s*\(\s*[^,]+,\s*\(\s*Class\s*<\s*\?\s*>\s*\)\s*)?"
    r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\.class\b",
)
_EXPLICIT_COMPONENT_NAME_RE = re.compile(
    r"new\s+ComponentName\s*\(\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*\)",
)


def _explicit_launch_targets(content: str) -> list[str]:
    """提取方法中的显式启动目标 FQCN（类字面量 + ComponentName 双字面量）。

    返回去重后的目标类名列表；无显式目标（目标由变量/表达式决定）返回空列表。
    """

    targets: list[str] = []
    for match in _EXPLICIT_LAUNCH_CLASS_RE.finditer(content):
        # 排除 setClass 自身的匹配重复：类字面量正则已覆盖 Foo.class 形态
        class_name = match.group(1)
        if class_name not in targets:
            targets.append(class_name)
    for match in _EXPLICIT_COMPONENT_NAME_RE.finditer(content):
        combined = f"{match.group(1)}.{match.group(2)}"
        if combined not in targets:
            targets.append(combined)
    return targets


def _resolve_explicit_target_fqcn(target: str, file: dict[str, Any]) -> str:
    """把显式目标解析为 FQCN：imports 精确映射优先，其次同包（简单名）。"""

    if target.count(".") >= 1 and not target.endswith(".class"):
        # 已含点号视为 FQCN（ComponentName 组合或 import 完整类名）
        return target
    imports = file.get("imports") or []
    for imported in imports:
        if isinstance(imported, str) and imported.endswith(f".{target}"):
            return imported
    package = str(file.get("package") or "")
    return f"{package}.{target}" if package else target


def _manifest_component_names(manifest: dict[str, Any]) -> set[str]:
    """manifest 组件集合（FQCN）。"""
    return {
        str(comp.get("name") or "")
        for comp in (manifest.get("components") or [])
        if isinstance(comp, dict)
    }

# 全量 extras 透传：攻击者可注入任意 key，是 v04 危害的核心。
# 两种形态都要覆盖：
#   ① putExtras/replaceExtras 整体搬运一个 Bundle；
#   ② 以**非字面量键名**逐条写入（v04 真实形态是
#      `for (key : json.keys()) bundle.putString(key, ...)`——键名同样由攻击者 JSON 决定）。
# 形态 ② 只认首参非字符串字面量的 put*，在基线 APK 上命中 1157/244752 个方法（0.47%）。
ROUTE_BULK_EXTRAS_METHODS = re.compile(r"\b(?:putExtras|replaceExtras)\s*\(")
ROUTE_DYNAMIC_KEY_PUT = re.compile(
    r"\b(?:putString|putExtra|putInt|putBoolean|putLong|putFloat|putDouble"
    r"|putSerializable|putParcelable|putCharSequence|putStringArrayList)\s*\(\s*"
    r"(?![\"'])[A-Za-z_$][\w$.\[\]()]*\s*,",
)
ROUTE_LAUNCH_METHODS = re.compile(
    # 平台 API + 应用自定义路由 wrapper。后者是本规则存在的理由：v04 实证成立的漏洞
    # 走的是 BasePluginFragment.Fasade.startNewPluginActivity，不在平台 effect 表内，
    # 只匹配平台 API 会原样漏掉它（实测基线 APK 上确实漏检）。
    # 泛化 start*Activity*/start*Activities* 在基线 APK 上新增 71 个调用点、
    # 12 个 wrapper 方法名，量级可控。
    r"\b(?:startActivit(?:y|ies)\w*"
    r"|start\w+Activit(?:y|ies)\w*"
    r"|startService|startForegroundService|bindService)\s*\(",
)
PROVIDER_FLOW_RULES = {
    "PROVIDER_CALLER_CHECK_MISSING",
    "PROVIDER_URI_TO_FILE",
    "PROVIDER_SQL_STRUCTURE_INJECTION",
    "PROVIDER_UNAUTHORIZED_QUERY",
    "PROVIDER_UNAUTHORIZED_MUTATION",
}


def execute(rule_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """执行单条规则；Binder 分支批量加载组件并隔离组件级失败。

    Binder 内部 deadline 固定为启动后 119 秒，仅为父级 RuleRunner 墙钟限制预留清理时间；
    其他规则分支不由此 deadline 截断，最终硬上限仍以父进程配置为准。
    """
    manifest = payload.get("manifest", {})
    legacy_files = payload.get("code_index", {}).get("files", [])
    reader = RuleIndexReader(payload["index"]) if payload.get("index") else None
    candidates: list[dict[str, Any]] = []
    component_diagnostics: list[dict[str, Any]] = []
    # 规则产物收集（T2.1：stdout 协议可选字段，backend 汇总侧落盘为
    # rule-results/{binder_bindings|receiver_registrations|webview_js_bridges}.json）
    binder_bindings_records: list[dict[str, Any]] = []
    receiver_registration_records: list[dict[str, Any]] = []
    webview_bridge_records: list[dict[str, Any]] = []
    started = time.monotonic()
    deadline = started + 119.0
    try:
        if rule_id in {"DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", "IMPLICIT_BROADCAST_SENSITIVE_DATA", "ORDERED_BROADCAST_UNRESTRICTED"}:
            dynamic_scope = None
            if reader and rule_id == "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION":
                dynamic_scope = reader.dynamic_receiver_scope()
                files = dynamic_scope["files"]
            else:
                files = reader.search_for_rule(rule_id) if reader else legacy_files
            candidates.extend(_global_code_rule(rule_id, files, manifest, dynamic_receiver_scope=dynamic_scope))
            if rule_id == "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION":
                # 产物全量导出（不按 reportable 过滤——T2.2 api_surface 需要
                # 完整注册面，非 reportable 的 local/not_exported 判定正是可排除证据）
                for file in files:
                    receiver_registration_records.extend(parse_receiver_registrations(file, manifest))
        elif rule_id == "SERVICE_BINDER_CALLER_CHECK_MISSING":
            services = [
                component for component in manifest.get("components", [])
                if component.get("kind") == "service"
                and component.get("exported") == "true"
                and _authorization_status(component, rule_id, manifest) != "strongly_protected"
            ]
            binder_batch: dict[str, dict[str, Any]] = {}
            if reader and services:
                reader.db.set_progress_handler(
                    lambda: 1 if time.monotonic() >= deadline else 0,
                    10_000,
                )
                try:
                    binder_batch = reader.binder_components([str(item.get("name") or "") for item in services])
                except sqlite3.OperationalError as exc:
                    if "interrupted" not in str(exc).lower():
                        raise
                    for component in services:
                        component_diagnostics.append({
                            "component_name": component.get("name"), "duration_ms": 0,
                            "status": "timeout", "gaps": [{"code": "BINDER_BATCH_TIMEOUT", "critical": True}],
                        })
                    services = []
                finally:
                    reader.db.set_progress_handler(None, 0)
            for component in services:
                component_started = time.monotonic()
                name = str(component.get("name") or "")
                gaps: list[dict[str, Any]] = []
                status = "completed"
                try:
                    if component_started >= deadline:
                        status = "timeout"
                        gaps.append({"code": "BINDER_RULE_DEADLINE_EXCEEDED", "critical": True})
                        continue
                    facts = binder_batch.get(name, {})
                    matched_files = facts.get("files", []) if reader else _component_files(component, legacy_files)
                    gaps.extend(facts.get("gaps", []))
                    candidates.extend(_binder_rule_candidates(component, facts, manifest))
                except Exception as exc:  # 单个反编译异常组件不得拖垮整条 Binder 规则。
                    status = "error"
                    gaps.append({
                        "code": "BINDER_COMPONENT_ANALYSIS_FAILED", "critical": False,
                        "error_type": type(exc).__name__,
                    })
                finally:
                    component_diagnostics.append({
                        "component_name": name,
                        "duration_ms": round((time.monotonic() - component_started) * 1000, 3),
                        "status": status,
                        "gaps": gaps,
                    })
            # 产物导出（T2.1）：超时清空 services 时 binder_batch 保留已解析部分
            binder_bindings_records.extend(_binder_bindings_artifact(binder_batch))
        elif rule_id in PROVIDER_FLOW_RULES and reader:
            for component in manifest.get("components", []):
                if component.get("kind") != "provider" or component.get("exported") != "true":
                    continue
                scopes = reader.provider_entry_scopes(str(component.get("name") or ""))
                candidates.extend(_provider_rule_candidates(rule_id, component, scopes, manifest))
        elif rule_id in MANIFEST_FACT_RULES:
            # 本地存储/配置族（§12）：直接读 manifest 事实生成候选，不依赖代码索引。
            candidates.extend(_manifest_fact_candidates(rule_id, manifest))
        elif rule_id in GLOBAL_CODE_RULES:
            # WebView/密码学族（§12.2 ②③）：全局代码模式扫描，不绑定清单组件。
            files = reader.search_for_rule(rule_id) if reader else legacy_files
            candidates.extend(_global_code_rule(rule_id, files, manifest))
            if rule_id == "WEBVIEW_JS_BRIDGE_EXPOSED":
                # 产物独立收集（finditer 全枚举同文件多桥——候选单 match 行为不动）
                for file in files:
                    webview_bridge_records.extend(
                        _webview_bridge_artifact_records(str(file.get("content") or ""), file)
                    )
        else:
            kind = RULE_META[rule_id][0]
            for component in manifest.get("components", []):
                if component.get("kind") != kind:
                    continue
                flow_scope = None
                if rule_id in COMPONENT_FLOW_ENTRIES:
                    if reader:
                        flow_scope = reader.component_flow_scope(
                            str(component.get("name") or ""), COMPONENT_FLOW_ENTRIES[rule_id]
                        )
                        matched_files = flow_scope["files"]
                    else:
                        matched_files = _component_files(component, legacy_files)
                        flow_scope = {
                            "files": matched_files,
                            "entry_method_ids": [],
                            "gaps": [{"code": "LEGACY_INDEX_SCOPE", "critical": True}],
                        }
                else:
                    if rule_id in MANIFEST_ONLY_RULES:
                        matched_files = []
                    else:
                        matched_files = reader.component_files(component.get("name", "")) if reader else _component_files(component, legacy_files)
                if rule_id == "ACTIVITY_EXTERNAL_ROUTE_INJECTION":
                    candidates.extend(_route_injection_candidates(
                        rule_id, component, matched_files, manifest, flow_scope or {}
                    ))
                elif rule_id in COMPONENT_FLOW_ENTRIES:
                    candidates.extend(_component_flow_rule_candidates(
                        rule_id, component, matched_files, manifest, flow_scope or {}
                    ))
                else:
                    candidate = _component_rule(
                        rule_id, component, matched_files, manifest, component_flow_scope=flow_scope
                    )
                    if candidate:
                        candidates.append(candidate)
    finally:
        if reader:
            # P1-5 打通（2026-08-15）：在 reader 关闭前为候选补齐
            # call_site_exists / sink_argument_constant 两项规则事实。
            _attach_sink_argument_facts(candidates, reader, manifest)
            reader.close()
    result = {
        "protocol_version": "1.0.0",
        "rule_id": rule_id,
        "status": "completed",
        "candidates": sorted(candidates, key=_candidate_sort_key),
    }
    if rule_id == "SERVICE_BINDER_CALLER_CHECK_MISSING":
        result["component_diagnostics"] = component_diagnostics
        result["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
    # 规则产物（T2.1）：预算自控后挂可选协议字段（向后兼容——旧 backend 忽略）
    artifacts: dict[str, list[dict[str, Any]]] = {}
    artifact_gaps: list[dict[str, Any]] = []
    if rule_id == "SERVICE_BINDER_CALLER_CHECK_MISSING":
        bounded, gaps = _bound_artifact_records("binder_bindings", binder_bindings_records)
        artifacts["binder_bindings"] = bounded
        artifact_gaps.extend(gaps)
    elif rule_id == "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION":
        bounded, gaps = _bound_artifact_records("receiver_registrations", receiver_registration_records)
        artifacts["receiver_registrations"] = bounded
        artifact_gaps.extend(gaps)
    elif rule_id == "WEBVIEW_JS_BRIDGE_EXPOSED":
        bounded, gaps = _bound_artifact_records("webview_js_bridges", webview_bridge_records)
        artifacts["webview_js_bridges"] = bounded
        artifact_gaps.extend(gaps)
    if artifacts:
        result["artifacts"] = artifacts
    if artifact_gaps:
        result["artifact_gaps"] = artifact_gaps
    return result


_CHAIN_EVIDENCE_FIELDS = (
    "path", "line", "text", "kind", "method_name", "method_id", "ordinal",
    "resolved_target_id", "resolve_status", "evidence_id", "source_kind",
    "source_basis", "parameter_position", "parameter_type", "taxonomy",
    "receiver_type", "receiver_text", "effect_verified", "sensitive_result",
    "sensitive_data_evidence", "status",
)

# ---------------------------------------------------------------------------
# 规则产物导出（T2.1）：stdout 协议内嵌 artifacts，backend 汇总侧落盘。
# 依据 docs/analysis/explorer-track/2026-08-22-t2-1-implementation-plan.md（评审 R-1~R-7 修订）。
# ---------------------------------------------------------------------------

_ARTIFACT_MAX_BYTES = 2 * 1024 * 1024  # 单产物 2 MiB（stdout 预算 10 MiB 内自控）


def _binder_resolve_status(transaction: dict[str, Any]) -> str:
    """推导 transaction 的绑定状态（transaction 记录无该字段——T2.1 评审 R-2）。

    实现已唯一绑定 → bound（对齐 v2026-08-16 降级逻辑：dispatch 歧义在实现
    唯一时不再一票否决）；否则按 implementation gap 判 ambiguous/unresolved。
    """

    if transaction.get("implementation_method_id"):
        return "bound"
    codes = {gap.get("code") for gap in transaction.get("gaps", []) if isinstance(gap, dict)}
    if "BINDER_IMPLEMENTATION_AMBIGUOUS" in codes:
        return "ambiguous"
    return "unresolved"


def _binder_bindings_artifact(binder_batch: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """组装 Binder 绑定产物（评审 R-1：service_class 是 class 记录 dict，
    注入源取 qualified_name；manifest 组件名兜底）。

    service_class 与代码事实同源（索引 qualified_name），manifest 名仅在
    索引缺失时兜底——两者不一致时以索引为准。
    """

    records: list[dict[str, Any]] = []
    for service_name, facts in binder_batch.items():
        service_class = (
            (facts.get("service_class") or {}).get("qualified_name")
            if isinstance(facts.get("service_class"), dict)
            else facts.get("service_class")
        ) or service_name
        for transaction in facts.get("transactions", []):
            records.append({
                "service_class": service_class,
                **transaction,
                "resolve_status": _binder_resolve_status(transaction),
            })
    return records


def _webview_bridge_artifact_records(code: str, file: dict[str, Any]) -> list[dict[str, Any]]:
    """枚举单文件全部 addJavascriptInterface 桥调用点（评审 R-7：finditer
    全枚举——候选生成的单 match 行为不动，产物与候选收集解耦）。"""

    records: list[dict[str, Any]] = []
    pattern = re.compile(r"addJavascriptInterface\s*\(\s*[^,]+,\s*[\"']([^\"']+)[\"']")
    for match in pattern.finditer(code):
        # 注释行剔除：取 match 前缀的当前行行首（rfind 须在 prefix 上做，
        # 对整个 code rfind 会错位到文件末行）
        prefix = code[:match.start()]
        if re.match(r"\s*//", prefix[prefix.rfind("\n") + 1:]):
            continue
        records.append({
            "path": file.get("path"),
            "line": code.count("\n", 0, match.start()) + 1,
            "text": match.group(0)[:120],
            "description": "WebView.addJavascriptInterface 注入 JS 桥：任意加载到该 WebView 的"
                           "网页 JS 均可调用被注入对象的全部导出方法，若桥对象暴露敏感能力则构成"
                           "远程代码/数据访问面（JS 桥注入）。",
            "sink_kind": "js_bridge",
            "bridge_name": match.group(1),
        })
    return records


def _bound_artifact_records(
    name: str, records: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按字节预算截断产物记录（stdout 10 MiB 红线自控，防 RULE_OUTPUT_LIMIT）。

    字节估算与 detect.py 落盘同口径（ensure_ascii=False——CJK 不低估，评审 R-4）；
    截断是显式声明（gap 保真实总数供覆盖判断，对齐 _summarize_reaching_definitions
    的"摘要与输入解耦"先例）。
    """

    total = len(records)
    bounded: list[dict[str, Any]] = []
    size = 2  # json 数组括号
    for record in records:
        record_size = len(json.dumps(record, ensure_ascii=False)) + 1
        if size + record_size > _ARTIFACT_MAX_BYTES and bounded:
            return bounded, [{
                "code": "RULE_ARTIFACT_TRUNCATED",
                "critical": False,
                "artifact": name,
                "kept": len(bounded),
                "total": total,
            }]
        bounded.append(record)
        size += record_size
    return bounded, []


def chain_to_candidate(
    candidate: dict[str, Any],
    chain: dict[str, Any],
    *,
    source_kind: str | None = None,
    sink_kind: str | None = None,
) -> dict[str, Any]:
    """Project exactly one deterministic chain into exactly one candidate."""

    source = chain.get("source") if isinstance(chain.get("source"), dict) else None
    sink = chain.get("sink") if isinstance(chain.get("sink"), dict) else None
    path = [dict(node) for node in chain.get("path", []) if isinstance(node, dict)]
    result = dict(candidate)
    result.pop("chains", None)
    result.update({
        "chain_id": _stable_candidate_chain_id(chain),
        "entry_method_id": chain.get("entry_method_id"),
        "path_model": str(chain.get("path_model") or "linear_ir_v1"),
        "flow_kind": str(chain.get("flow_kind") or "source_to_sink"),
        "sources": [_evidence(source, source_kind or source.get("kind", "external_input"))]
        if source else [],
        "sinks": [_evidence(sink, sink_kind or sink.get("kind", "sensitive_sink"))]
        if sink else [],
        "propagation_paths": path,
    })
    if chain.get("entry_method_name"):
        result["entry_method_name"] = chain["entry_method_name"]
    entry_identity = chain.get("entry_method_id") or chain.get("entry_method_name")
    if entry_identity:
        result["entry_points"] = [entry_identity]
    return result


def _stable_candidate_chain_id(chain: dict[str, Any]) -> str:
    identity = {
        "entry_method_id": chain.get("entry_method_id"),
        "source": _stable_chain_evidence(chain.get("source")),
        "sink": _stable_chain_evidence(chain.get("sink")),
        "ordered_path": [
            _stable_chain_evidence(node)
            for node in chain.get("path", [])
            if isinstance(node, dict)
        ],
        "flow_kind": str(chain.get("flow_kind") or "source_to_sink"),
        "scope": chain.get("chain_scope"),
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "chain_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _stable_chain_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {field: value[field] for field in _CHAIN_EVIDENCE_FIELDS if value.get(field) is not None}


def _is_component_method(method: dict[str, Any], component: dict[str, Any]) -> bool:
    """方法是否属于该组件类本身（兼容索引中 FQCN 与短类名两种形态）。"""

    name = str(component.get("name") or "")
    if not name:
        return False
    if str(method.get("qualified_class") or "") == name:
        return True
    class_name = str(method.get("class_name") or "")
    return bool(class_name) and class_name == name.rsplit(".", 1)[-1]


def _route_injection_candidates(
    rule_id: str,
    component: dict[str, Any],
    files: list[dict[str, Any]],
    manifest: dict[str, Any],
    scope: dict[str, Any],
) -> list[dict[str, Any]]:
    """P2-6：识别「外部输入决定启动目标 / 被全量透传」的路由注入面。

    与值流规则的关键区别：这里**不要求 sink 是已知敏感 API**。只要满足
    「外部可控输入 → 目标决定或 extras 透传 → 组件启动」，路由能力本身就是攻击面。

    保守约束（避免制造新噪声）：
    - 路由方法必须从**读取外部输入的入口方法**经已解析调用边可达。v04 的真实形态正是
      跨方法的（onCreate 读 extra_splashinfo → handleSplashInfo 组装并启动），
      只在单方法内匹配会漏掉它；但可达性只沿 `resolve_status=resolved` 的确定边走，
      不做跨类猜测；
    - 组件必须外部可达（exported 且非强权限保护）；
    - 目标 resolve 失败（插件/动态注册组件不在 manifest 索引中）时**保留候选并产 gap**，
      不静默丢弃——这正是 v04 漏洞此前被丢掉的原因。
    """

    if component.get("exported") != "true" or not files:
        return []
    authorization = _authorization(component, rule_id, manifest)
    if authorization["status"] == "strongly_protected" and not authorization["has_uri_grant_alternative"]:
        return []

    scope_files = scope.get("files") or files
    entry_ids = {str(item) for item in (scope.get("entry_method_ids") or []) if item}
    methods_by_id: dict[str, tuple[dict[str, Any], str, dict[str, Any]]] = {}
    for file in scope_files:
        for method in file.get("methods") or []:
            method_id = str(method.get("id") or "")
            if method_id:
                methods_by_id[method_id] = (method, str(file.get("path") or ""), file)

    # 从"读取了外部输入"的入口出发，沿已解析调用边求可达闭包。
    tainted_roots = {
        method_id
        for method_id, (method, _, _) in methods_by_id.items()
        if SOURCE_PATTERNS["intent_extra"].search(str(method.get("content") or ""))
        and (not entry_ids or method_id in entry_ids or _is_component_method(method, component))
    }
    reachable: dict[str, str] = {method_id: method_id for method_id in tainted_roots}
    frontier = list(tainted_roots)
    while frontier:
        current = frontier.pop()
        entry = methods_by_id.get(current)
        if not entry:
            continue
        for call in entry[0].get("call_sites") or []:
            if call.get("resolve_status") != "resolved":
                continue
            target = str(call.get("resolved_target_id") or "")
            if target and target in methods_by_id and target not in reachable:
                reachable[target] = reachable[current]
                frontier.append(target)

    results: list[dict[str, Any]] = []
    for method_id, root_id in sorted(reachable.items()):
        method, path, file = methods_by_id[method_id]
        content = str(method.get("content") or "")
        launch_match = ROUTE_LAUNCH_METHODS.search(content)
        if not launch_match:
            continue
        target_match = ROUTE_TARGET_METHODS.search(content)
        bulk_match = (
            ROUTE_BULK_EXTRAS_METHODS.search(content)
            or ROUTE_DYNAMIC_KEY_PUT.search(content)
        )
        if not target_match and not bulk_match:
            continue

        root_method, root_path, root_file = methods_by_id[root_id]
        root_content = str(root_method.get("content") or "")
        source_match = SOURCE_PATTERNS["intent_extra"].search(root_content)
        if not source_match:
            continue
        start_line = int(method.get("start_line") or 1)
        root_start = int(root_method.get("start_line") or 1)
        injection = "bulk_extras_forwarding" if bulk_match else "target_selection"
        # P1-5 打通（2026-08-15）：target_selection 类候选输出目标固定性事实
        # `resolved_target_fixed`，供决策层交叉验证 fixed_local_target 反证。
        # 仅 target_selection 输出：bulk_extras_forwarding 即使同方法命中目标决策调用
        # （setAction/putExtras 并存），其注入面核心仍是 extras 透传，输出该字段
        # 会把"无目标决策/目标与注入面无关"误读为"目标固定或不固定"。
        target_fixed: bool | None = None
        if injection == "target_selection" and target_match:
            target_fixed = _target_decision_is_fixed(target_match, content)
        # P0①（2026-08-15）：显式启动目标注册检查——独立于 resolved_target_fixed。
        # v04 动态验证发现 extra_close_url → go2CloseSet → WebActivity（com.xiaomi.shop2.
        # closeset.WebActivity）**未在 manifest 注册** → startActivity 抛
        # ActivityNotFoundException → 崩溃 DoS（可重复触发）。**目标固定 ≠ 安全**：
        # 未注册目标是"另一种危害（DoS）"而非"安全"，fixed_local_target 采信前提
        # 是"目标固定且可达"。因此从 sink 方法提取显式目标类字面量，查 manifest
        # 组件集合；未注册 → resolved_target_registered=False + DoS gap + 降级 L1。
        registered: bool | None = None
        explicit_targets = _explicit_launch_targets(content)
        if explicit_targets:
            component_names = _manifest_component_names(manifest)
            registered = all(
                _resolve_explicit_target_fqcn(target, file) in component_names
                for target in explicit_targets
            )
        gaps: list[dict[str, Any]] = [{
            "code": "ROUTE_TARGET_RESOLUTION_UNVERIFIED",
            "critical": True,
            "method": method_id,
            "message": (
                "路由目标由运行期值决定（插件/动态组件可能不在 manifest 索引中），"
                "静态无法枚举全部可达目标；需人工或动态确认目标集合"
            ),
        }]
        if registered is False:
            # 目标类未在 manifest 注册：显式启动必崩（ActivityNotFoundException）。
            # 这是确定性静态可判的 DoS——外部可触发进程崩溃（v04 实证
            # extra_close_url → WebActivity 崩溃链）。降级 L1/低危但仍保留候选：
            # DoS 是真实攻击面，不应被 fixed_local_target 反证吞掉。
            gaps.append({
                "code": "UNREGISTERED_LAUNCH_TARGET",
                "critical": True,
                "method": method_id,
                "message": (
                    "显式启动目标未在 manifest 注册，startActivity 将抛 "
                    "ActivityNotFoundException 导致进程崩溃（外部可重复触发 = 本地 DoS）"
                ),
            })
        # P0②（2026-08-15）：receiver 入口本地广播隔离（红线 9，对齐
        # RECEIVER_INPUT_TO_SINK 的检测）。候选 11 实证：MainActivity 通过
        # LocalBroadcastManager.registerReceiver 注册的 onReceive 回调（进程内分发）
        # 触发 startService——外部应用无法跨进程触发本地广播（红线 9），
        # 注入面不成立。判定：root 方法是 onReceive 且所在文件引入
        # LocalBroadcastManager/EventBus（进程内分发框架）→ 产 gap + input_control 标记。
        local_broadcast_isolated = False
        if str(root_method.get("name") or "") == "onReceive":
            root_imports = " ".join(
                str(item) for item in (root_file.get("imports") or [])
                if isinstance(item, str)
            )
            root_content_all = str(root_file.get("content") or "")
            if re.search(
                r"LocalBroadcastManager|EventBus", root_imports + root_content_all
            ):
                local_broadcast_isolated = True
                gaps.append({
                    "code": "LOCAL_BROADCAST_ISOLATED",
                    "critical": True,
                    "method": root_id,
                    "message": (
                        "receiver 入口经 LocalBroadcastManager/EventBus 注册（进程内分发，"
                        "红线 9），外部应用无法跨进程触发——注入面不成立"
                    ),
                })
        # P1③（2026-08-15）：bulk 消费方语义分级（保守）——v04 动态验证显示
        # bulk_extras_forwarding 中"目标固定 + 统计/SDK 语义消费方"5/5 误报
        # （AuthActivity/StatService2/SplashCommonUtils 等）。仅在**显式启动目标**
        # （setClassName/setComponent 的类名）含**明确统计语义**（Stat/Report/
        # Trace/Log/Umeng 等 SDK 统计惯例）时输出 consumer_semantics="statistics"，
        # 供 funnel 降级为 signal（受开关控制）；其余语义（含未知）不输出该字段——
        # 保留 L2，不做强行推断（消费方消费外部 URL/敏感参数属语义判断，规则层不猜，
        # 避免新误判方向）。
        consumer_semantics: str | None = None
        if injection == "bulk_extras_forwarding" and registered is not False:
            consumer_name = _resolve_consumer_name(
                content, str(file.get("content") or ""),
            )
            if re.search(
                r"(?:^|\.)(?:Stat|Report|Trace|Track|Log|Umeng|Flurry|Firebase|Bugly)"
                r"(?:Service|Helper|Trigger|Manager|Util)?(?:\d*)$",
                consumer_name,
            ) or re.search(r"Stat(?:istic|istics|us)?|Report|Umeng|Flurry|Bugly", consumer_name):
                consumer_semantics = "statistics"
        if bulk_match:
            gaps.append({
                "code": "BULK_EXTRAS_FORWARDING",
                "critical": True,
                "method": method_id,
                "message": "putExtras/replaceExtras 整体透传外部 Bundle，攻击者可注入任意 key",
            })

        source_line = root_start + root_content[: source_match.start()].count("\n")
        launch_line = start_line + content[: launch_match.start()].count("\n")
        decision = target_match or bulk_match
        decision_line = start_line + content[: decision.start()].count("\n")
        propagation = [{
            "path": path, "line": decision_line, "text": decision.group(0),
            "kind": "route_decision", "status": "fact", "method_id": method_id,
            "evidence_id": f"{path}:{decision_line}",
        }]
        if method_id != root_id:
            propagation.insert(0, {
                "path": root_path, "line": source_line,
                "text": f"{root_method.get('name')} 读取外部输入后经已解析调用边到达路由方法",
                "kind": "call", "status": "fact", "method_id": root_id,
                "evidence_id": f"{root_path}:{source_line}",
            })
        chain = {
            "entry_method_id": root_id,
            "entry_method_name": root_method.get("name"),
            "path_model": "route_injection_v1",
            "flow_kind": "external_route_control",
            "source": {
                "path": root_path, "line": source_line,
                "text": source_match.group(0)[:120],
                "kind": "intent_extra", "status": "fact",
                "method_id": root_id, "method_name": root_method.get("name"),
                "evidence_id": f"{root_path}:{source_line}",
            },
            "sink": {
                "path": path, "line": launch_line,
                "text": launch_match.group(0),
                "kind": "component_launch", "taxonomy": "ui_navigation",
                "status": "fact", "effect_verified": True,
                "method_id": method_id, "method_name": method.get("name"),
                "evidence_id": f"{path}:{launch_line}",
            },
            "path": propagation,
            "blocking_gaps": gaps,
            "dataflow_status": "not_proven",
        }
        base = _base(
            rule_id, component, "L2", files, manifest,
            "外部可控输入参与决定组件启动目标或被全量透传，构成路由注入面",
        )
        result = chain_to_candidate(base, chain, source_kind="intent_extra")
        is_dos = registered is False
        result.update({
            "operation_taxonomy": "ui_navigation",
            "dataflow_status": "not_proven",
            "deterministic_chain_verified": False,
            # P0①（2026-08-15）：未注册目标 = 崩溃 DoS（确定性静态可判），降级 L1/低危
            # 但仍保留候选——DoS 是真实攻击面，外部可重复触发进程崩溃。
            "impact_status": "denial_of_service" if is_dos else "potential",
            "route_injection_kind": injection,
            # P0①（2026-08-15）：目标未注册时**不得输出 resolved_target_fixed**——
            # "目标固定"会被决策层采信为 fixed_local_target 反证（ai_false_positive），
            # 把 DoS 误判为误报吞掉。未注册目标不是"安全"而是"另一种危害"。
            **({"resolved_target_fixed": target_fixed}
               if target_fixed is not None and not is_dos else {}),
            **({"resolved_target_registered": registered} if registered is not None else {}),
            "coverage_gaps": list(scope.get("gaps") or []),
            "guard_status": "unknown",
            "authorization_status": authorization["status"],
            "authorization_matrix": authorization["rows"],
            "authorization_operation": operation_for_rule(rule_id, chain["sink"])[0],
            "blocking_gaps": _unique_records(gaps),
            "reachability_status": "reachable",
            # P0②（2026-08-15）：本地广播隔离标记——AI 判定时可据此排除
            # （红线 9 进程内分发，外部不可达）。
            **({"input_control": "local_broadcast_isolated"} if local_broadcast_isolated else {}),
            # P1③（2026-08-15）：bulk 消费方语义分类——statistics 供 funnel 降级 signal
            # （受 demote_unproven_flow 开关控制）；未输出 = 语义未知，保留 L2。
            **({"consumer_semantics": consumer_semantics} if consumer_semantics else {}),
        })
        if is_dos:
            result["evidence_level"] = "L1"
            result["severity_hint"] = "low"
            result["confidence_tier"] = "high"
            result["title"] = "外部可控输入触发未注册目标显式启动（崩溃 DoS）"
            result["description"] = (
                "外部可控输入决定组件启动目标，且目标类未在 manifest 注册——"
                "显式 startActivity 抛 ActivityNotFoundException 导致进程崩溃，"
                "外部应用可重复触发（本地 DoS，低危）"
            )
        results.append(result)
    return sorted(results, key=_candidate_sort_key)


def _component_flow_rule_candidates(
    rule_id: str,
    component: dict[str, Any],
    files: list[dict[str, Any]],
    manifest: dict[str, Any],
    scope: dict[str, Any],
) -> list[dict[str, Any]]:
    """Emit every Activity/Service/Receiver DataFlow chain independently."""

    if component.get("exported") != "true" or not files:
        return []
    preliminary = _authorization(component, rule_id, manifest)
    if preliminary["status"] == "strongly_protected" and not preliminary["has_uri_grant_alternative"]:
        return []
    analyzer = DataFlowAnalyzer(
        scope.get("files", files),
        entry_method_ids=scope.get("entry_method_ids"),
        scope_gaps=scope.get("gaps"),
    )
    flow = analyzer.analyze_entry(COMPONENT_FLOW_ENTRIES[rule_id])
    chains = list(flow.get("chains") or [])
    if not chains:
        source_pattern = {
            "activity": SOURCE_PATTERNS["intent_extra"],
            "service": SOURCE_PATTERNS["ipc_input"],
            "receiver": SOURCE_PATTERNS["receiver_input"],
        }[component["kind"]]
        fallback = _intraprocedural_flow(files, source_pattern)
        if fallback:
            source, sink = fallback
            entry_method = next((
                method for file in files for method in file.get("methods", [])
                if method.get("name") == source.get("method_name")
            ), None)
            chains = [{
                "entry_method_id": (entry_method or {}).get("id"),
                "entry_method_name": source.get("method_name"),
                "source": {**source, "status": "inferred"},
                "sink": {**sink, "status": "inferred"},
                "path": [{
                    "text": f"{source['path']} 中轻量回退匹配到同方法 Source/Sink，语义链未闭合",
                    "status": "candidate",
                    "evidence_id": f"{source['path']}:{source['line']}",
                }],
                "blocking_gaps": [
                    *flow.get("coverage_gaps", []),
                    {"code": "LEGACY_FLOW_FALLBACK", "critical": True},
                ],
                "dataflow_status": "not_proven",
                "flow_kind": "inferred_source_to_sink",
                "path_model": "legacy_pattern_v1",
            }]
    # 组件级数据流 trace 按候选复制会导致输出爆炸（实测单组件 78 条链 x 921KB
    # reaching_definitions = 90MB，触发 RULE_OUTPUT_LIMIT 使整族候选丢失）。
    # 这里只随候选下发判定所需的摘要，逐条明细留在规则进程内不外发。
    common_metadata: dict[str, Any] = {
        "summary_fixpoint": flow.get("summary_fixpoint", {}),
        "method_summaries": _summarize_method_summaries(flow.get("method_summaries", {})),
        "reaching_definitions": _summarize_reaching_definitions(
            flow.get("reaching_definitions", [])
        ),
        "validation_transitions": _cap_records(flow.get("validation_transitions", [])),
        "slot_overwrites": _cap_records(flow.get("slot_overwrites", [])),
        "router_validation_bypass": _cap_records(flow.get("router_validation_bypasses", [])),
        "final_reaching_state": flow.get("final_reaching_state"),
    }
    if rule_id == "ACTIVITY_INTENT_TO_SENSITIVE_SINK":
        common_metadata["fragment_reflection"] = analyzer.fragment_reflection_analysis(flow)
    elif rule_id == "SERVICE_IPC_INPUT_TO_SINK":
        common_metadata["started_service_state_machine"] = analyzer.started_service_state_machine(flow)
    elif rule_id == "RECEIVER_INPUT_TO_SINK":
        common_metadata["receiver_binding"] = analyzer.receiver_input_analysis()

    results: list[dict[str, Any]] = []
    for chain in chains:
        source = chain.get("source") or {}
        sink = chain.get("sink") or {}
        if not source or not sink:
            continue
        guard = analyzer.guard_coverage(
            chain,
            entry_method_id=str(chain.get("entry_method_id") or "") or None,
            sink=sink,
        )
        if guard["status"] == "present_effective":
            continue
        operation, mode = operation_for_rule(rule_id, sink)
        authorization = _authorization(
            component, rule_id, manifest, sink=sink, operation=operation, mode=mode
        )
        if authorization["status"] == "strongly_protected" and not authorization["has_uri_grant_alternative"]:
            continue
        coverage_gaps = _chain_coverage_gaps(flow.get("coverage_gaps", []), chain)
        protocol_gated, protocol_gates = _receiver_protocol_gate(analyzer, chain)
        gaps = _unique_records([
            *chain.get("blocking_gaps", []),
            *coverage_gaps,
            *guard.get("blocking_gaps", []),
            *authorization.get("blocking_gaps", []),
            *(
                [{
                    "code": "INPUT_PROTOCOL_UNCONTROLLED",
                    "critical": True,
                    "gates": protocol_gates,
                    "message": "外部输入需特定二进制/序列化协议（"
                               + ",".join(protocol_gates)
                               + "）才能触发业务分支，普通应用无法构造合法消息",
                }]
                if protocol_gated else []
            ),
        ])
        deterministic = bool(
            sink.get("effect_verified") is True
            and flow.get("summary_fixpoint", {}).get("status") == "converged"
            and not any(gap.get("critical") is True for gap in gaps)
        )
        base = _base(
            rule_id, component, "L2", files, manifest,
            "已识别组件精确入口到敏感操作的独立静态候选链路",
        )
        result = chain_to_candidate(base, chain, source_kind="taint_source")
        result.update(common_metadata)
        result.update({
            "operation_taxonomy": sink.get("taxonomy", "unknown_effect"),
            "dataflow_status": chain.get("dataflow_status", "not_proven"),
            "final_reaching_state": chain.get("final_reaching_state"),
            "deterministic_chain_verified": deterministic,
            "impact_status": "statically_confirmed" if deterministic else "potential",
            "coverage_gaps": coverage_gaps,
            "guard_status": guard["status"],
            "guard_coverage": guard,
            "guard_summary": guard,
            "authorization_status": authorization["status"],
            "authorization_matrix": authorization["rows"],
            "authorization_operation": operation,
            "blocking_gaps": gaps,
            "input_control": "protocol_gated" if protocol_gated else "direct",
        })
        results.append(result)
    return sorted(results, key=_candidate_sort_key)


def _receiver_protocol_gate(
    analyzer: Any, chain: dict[str, Any]
) -> tuple[bool, list[str]]:
    """检测 receiver 输入到 Sink 之间是否存在二进制/序列化协议门。

    v2026-08-14（动态验证提炼，真机 Android 16 验证 ShopPushMessageReceiver 误报）：
    推送类接收器（mipush/极光等）的业务回调需要 `getByteArrayExtra("mipush_payload")`
    + protobuf 解析、或 `getSerializableExtra`+强转、或 `getParcelableExtra`+特定类
    才能触发——adb/普通应用无法构造合法消息，外部输入"到达 Sink"在业务语义上不成立
    （静态可达 ≠ 业务分支可达）。命中协议门 → 候选标记 input_control=protocol_gated
    并追加 INPUT_PROTOCOL_UNCONTROLLED（证据不足类 gap，决策层降级不采信）。

    判定只做保守命中：宁可漏识别（落 unresolved 人工复核）不可误识别。
    """

    method_ids: set[str] = set()
    source = chain.get("source") or {}
    sink = chain.get("sink") or {}
    if source.get("method_id"):
        method_ids.add(str(source["method_id"]))
    if sink.get("method_id"):
        method_ids.add(str(sink["method_id"]))
    for node in chain.get("path", []):
        if isinstance(node, dict) and node.get("method_id"):
            method_ids.add(str(node["method_id"]))
    if not method_ids:
        return False, []
    content = "\n".join(
        str(analyzer.methods_by_id.get(mid, {}).get("content") or "")
        for mid in method_ids
    )
    if not content:
        return False, []
    gates: list[str] = []
    # 二进制 payload 门：getByteArrayExtra 取值后交给解析器（protobuf 风格 parseFrom/
    # readFrom/decode 或混淆解析方法）——消息语义由服务端协议定义，外部无法构造合法内容。
    # 补充 payload 命名门：extra key 含 "payload"（如 mipush_payload）本身就是协议载荷信号，
    # 即使解析器是 JADX 混淆名（如 C7971ji.m21500a）也能命中。
    if re.search(r"getByteArrayExtra\s*\([^)]*\)", content) and (
        re.search(
            r"(?:parseFrom|readFrom|decode|decodeValue)\s*\(",
            content,
        )
        or re.search(r"getByteArrayExtra\s*\(\s*\"[^\"]*payload[^\"]*\"\s*\)", content)
    ):
        gates.append("binary_payload")
    # 序列化命令门：getSerializableExtra 需要具体 Serializable 类（如 MiPushCommandMessage），
    # 外部无法注入任意对象图。
    if re.search(r"getSerializableExtra\s*\([^)]*\)", content) and re.search(
        r"\binstanceof\b", content,
    ):
        gates.append("serialized_command")
    # Parcelable 门：getParcelableExtra 需要特定 Parcelable 类，且通常伴随类名强转。
    if re.search(r"getParcelableExtra\s*\([^)]*\)", content):
        gates.append("parcelable_gated")
    return bool(gates), gates


def _chain_coverage_gaps(gaps: list[dict[str, Any]], chain: dict[str, Any]) -> list[dict[str, Any]]:
    method_ids = {
        str(value) for value in [
            chain.get("entry_method_id"),
            (chain.get("source") or {}).get("method_id"),
            (chain.get("sink") or {}).get("method_id"),
            *[
                node.get("method_id") for node in chain.get("path", [])
                if isinstance(node, dict)
            ],
        ] if value
    }
    selected = []
    for gap in gaps:
        owner = gap.get("entry_method_id") or gap.get("caller") or gap.get("method")
        if owner and method_ids and str(owner) not in method_ids:
            continue
        selected.append(gap)
    return _unique_records(selected)


def _unique_records(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for value in values:
        if isinstance(value, dict):
            marker = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            unique.setdefault(marker, value)
    return list(unique.values())


def _binder_caller_check_lines(files: list[dict[str, Any]]) -> dict[str, list[int]]:
    """扫描 Binder 闭包文件中的调用者身份校验证据。

    v2026-08-09（Cluster E 误报根因修复）：transaction 解析失败/实现歧义时，
    校验往往写在闭包文件自身（如 UploadLogSDKService.java:42 `Binder.getCallingUid()`
    → :81 `C8102c.m22324a(this, uid)` 精确包名校验）。返回 {path: [行号]}；
    空 dict 表示无校验证据。命中即抑制 "caller check missing" 候选——宁可
    漏识别（落 unresolved/人工复核）不可误识别 ai_false_positive 隐藏真漏洞。
    """
    seen: dict[str, list[int]] = {}
    source = re.compile(
        r"(?:Binder\s*\.\s*getCallingUid|getCallingPid|getNameForUid|checkCallingPermission|"
        r"enforceCallingPermission|checkCallingOrSelfPermission|enforceCallingOrSelfPermission|"
        r"checkSignatures|checkUidSignatures)"
    )
    for file in files:
        content = str(file.get("content") or "")
        for match in source.finditer(content):
            seen.setdefault(str(file.get("path") or ""), []).append(
                content[: match.start()].count("\n") + 1
            )
    return seen


def _binder_rule_candidates(
    component: dict[str, Any], binder_facts: dict[str, Any], manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """为每个 concrete Binder transaction 的每个真实 effect/return 生成独立候选。"""

    files = list(binder_facts.get("files") or [])
    flow_files = list(binder_facts.get("flow_files") or [])
    if not files or not binder_facts.get("on_bind"):
        return []
    merged: dict[str, dict[str, Any]] = {}
    for file in [*files, *flow_files]:
        key = str(file.get("path") or "")
        if key not in merged:
            merged[key] = {**file, "methods": list(file.get("methods", []))}
        else:
            methods = {str(item.get("id")): item for item in merged[key].get("methods", [])}
            methods.update({str(item.get("id")): item for item in file.get("methods", [])})
            merged[key]["methods"] = list(methods.values())
    analysis_files = list(merged.values())
    analyzer = DataFlowAnalyzer(analysis_files, scope_gaps=[])
    candidates: list[dict[str, Any]] = []
    global_gaps = [gap for gap in binder_facts.get("gaps", []) if gap.get("critical") is True]
    for transaction in binder_facts.get("transactions", []):
        transaction_gaps = [
            *transaction.get("gaps", []),
            *(gap for gap in global_gaps if gap.get("transaction_code") in {None, transaction.get("code")}),
        ]
        implementation_id = str(transaction.get("implementation_method_id") or "")
        implementation = analyzer.methods_by_id.get(implementation_id)
        chains = analyzer.effect_chains(implementation_id) if implementation else []
        descriptor = str(transaction.get("implementation_descriptor") or "")
        return_type = descriptor.split(")->", 1)[1] if ")->" in descriptor else "void"
        dispatch_result = str(transaction.get("dispatch_assigned_to") or "")
        dispatch_ordinal = int(transaction.get("dispatch_ordinal") or 0)
        matching_reply_writes = [
            site for site in transaction.get("reply_write_call_sites", [])
            if site.get("method_name") != "writeNoException"
            and int(site.get("ordinal") or 0) > dispatch_ordinal
            and any(
                re.search(rf"(?<![\w$]){re.escape(dispatch_result)}(?![\w$])", str(argument))
                for argument in site.get("arguments", [])
            )
        ] if dispatch_result else []
        disclosure = bool(
            implementation
            and return_type not in {"void", "Unit", "kotlin.Unit", "?"}
            and matching_reply_writes
        )
        if disclosure:
            reply_write = matching_reply_writes[0]
            return_line = next(
                (int(item.get("line", implementation.get("end_line", 1))) for item in reversed(implementation.get("flow_ir", [])) if item.get("op") == "return"),
                int(implementation.get("end_line", implementation.get("start_line", 1))),
            )
            chains.append({
                "chain_id": f"binder-return-{implementation_id}-{transaction.get('code')}",
                "entry_method_id": implementation_id,
                "entry_method_name": implementation.get("name"),
                "source": {
                    "path": transaction.get("implementation_path") or implementation.get("path"),
                    "line": return_line,
                    "text": "implementation return value",
                    "kind": "binder_return",
                    "method_id": implementation_id,
                    "method_name": implementation.get("name"),
                    "status": "fact",
                },
                "sink": {
                    "path": transaction.get("path"),
                    "line": reply_write.get("start_line") or transaction.get("case_line"),
                    "text": f"reply.{reply_write.get('method_name')}({dispatch_result})",
                    "kind": "binder_reply_disclosure",
                    "taxonomy": "data_disclosure",
                    "effect_verified": True,
                    "arguments": reply_write.get("arguments", []),
                    "method_id": transaction.get("on_transact_method_id"),
                    "method_name": "onTransact",
                    "ordinal": reply_write.get("ordinal"),
                },
                "path": [],
                "blocking_gaps": [],
                "dataflow_status": "interprocedural",
                "flow_kind": "return_disclosure",
            })
        if not chains and transaction_gaps:
            has_critical = any(gap.get("critical") is True for gap in transaction_gaps)
            if implementation:
                # v2026-08-16（S1）：实现已唯一绑定但没有可识别 effect 链时，
                # 以实现方法为 sink 出链（而非"dispatch 目标未解析"），候选保留
                # 实现上下文供 AI/Guard 判定；无 critical gap 时不再走 critical_gap
                # 形态，Guard 分析可作用到实现方法。
                sink = {
                    "path": transaction.get("implementation_path") or implementation.get("path"),
                    "line": int(implementation.get("start_line", transaction.get("case_line", 1))),
                    "text": f"implementation {implementation.get('name')} effect",
                    "kind": "binder_impl_effect",
                    "taxonomy": "unknown_effect",
                    "effect_verified": False,
                    "method_id": implementation_id,
                    "method_name": implementation.get("name"),
                }
                chain_entry = implementation_id
                chain_kind = "binder_dispatch" if not has_critical else "critical_gap"
            else:
                sink = {
                    "path": transaction.get("path"), "line": transaction.get("case_line"),
                    "text": "Binder dispatch target unresolved", "kind": "binder_unknown_effect",
                    "taxonomy": "unknown_effect", "effect_verified": False,
                }
                chain_entry = transaction.get("on_transact_method_id")
                chain_kind = "critical_gap"
            chains = [{
                "chain_id": f"binder-gap-{transaction.get('path')}:{transaction.get('line')}",
                "entry_method_id": chain_entry,
                "source": {
                    "path": transaction.get("path"), "line": transaction.get("case_line"),
                    "text": f"transaction {transaction.get('case_token')}", "kind": "binder_transaction",
                    "status": "fact",
                },
                "sink": sink,
                "path": [], "blocking_gaps": transaction_gaps,
                "dataflow_status": "not_proven", "flow_kind": chain_kind,
            }]
        for chain in chains:
            sink = chain.get("sink") or {}
            dispatch_ordinal = int(transaction.get("dispatch_ordinal") or 0)
            on_transact_id = str(transaction.get("on_transact_method_id") or "")
            common_guard = analyzer.guard_segment(
                on_transact_id,
                boundary_ordinal=max(1, dispatch_ordinal),
                start_line=None,
                end_line=max(int(transaction.get("switch_line") or 1) - 1, 1),
                end_ordinal=(int(transaction.get("case_ordinal_start")) - 1) if transaction.get("case_ordinal_start") else None,
            )
            case_guard = analyzer.guard_segment(
                on_transact_id,
                boundary_ordinal=max(1, dispatch_ordinal),
                start_line=int(transaction.get("case_line") or transaction.get("line") or 1),
                end_line=int((transaction.get("dispatch_call_site") or {}).get("start_line") or transaction.get("case_end_line") or transaction.get("line") or 1),
                start_ordinal=int(transaction.get("case_ordinal_start")) if transaction.get("case_ordinal_start") else None,
                end_ordinal=dispatch_ordinal - 1 if dispatch_ordinal else None,
            )
            implementation_guard = (
                analyzer.guard_coverage(chain, entry_method_id=implementation_id, sink=sink)
                if implementation_id and chain.get("flow_kind") != "critical_gap"
                else {"status": "unknown" if transaction_gaps else "absent", "guards": [], "blocking_gaps": []}
            )
            layer_statuses = [common_guard["status"], case_guard["status"], implementation_guard["status"]]
            if "present_effective" in layer_statuses:
                continue
            if "present_bypassable" in layer_statuses:
                guard_status = "present_bypassable"
            elif "present_partial" in layer_statuses:
                guard_status = "present_partial"
            elif "unknown" in layer_statuses:
                guard_status = "unknown"
            else:
                guard_status = "absent"
            authorization = evaluate_authorization(manifest, component, "component_entry", entry=str(transaction.get("interface_method") or "onTransact"))
            if authorization["status"] == "strongly_protected" and not authorization["has_uri_grant_alternative"]:
                continue
            critical_gaps = [
                *transaction_gaps,
                *chain.get("blocking_gaps", []),
                *common_guard.get("blocking_gaps", []),
                *case_guard.get("blocking_gaps", []),
                *implementation_guard.get("blocking_gaps", []),
                *authorization.get("blocking_gaps", []),
            ]
            candidate_chain = {
                **chain,
                "chain_scope": {
                    "binder_descriptor": transaction.get("descriptor"),
                    "transaction_code": transaction.get("code"),
                    "dispatch_method_id": transaction.get("on_transact_method_id"),
                },
                "path": [{
                    "path": transaction.get("path"),
                    "line": transaction.get("case_line") or transaction.get("line"),
                    "text": f"transaction {transaction.get('code')} → {transaction.get('interface_method')}",
                    "kind": "binder_dispatch",
                    "method_id": transaction.get("on_transact_method_id"),
                    "ordinal": transaction.get("dispatch_ordinal"),
                    "status": "fact",
                    "evidence_id": f"{transaction.get('path')}:{transaction.get('case_line') or transaction.get('line')}",
                }, *(chain.get("path") or [{
                    "text": f"{transaction.get('interface_method')} → {sink.get('kind')}",
                    "status": "fact" if sink.get("effect_verified") else "candidate",
                    "evidence_id": f"{transaction.get('path')}:{transaction.get('line')}",
                }])],
            }
            result = chain_to_candidate(
                _base(
                    "SERVICE_BINDER_CALLER_CHECK_MISSING", component, "L2", files, manifest,
                    "concrete Binder transaction 在真实 effect/return 前缺少已证明的调用方 Guard",
                ),
                candidate_chain,
            )
            result.update({
                "binder_remote_interface": True,
                "binder_transactions": [transaction],
                "binder_transaction": transaction,
                "binder_return_types": binder_facts.get("return_types", []),
                "binder_inheritance_chain": binder_facts.get("inheritance_chain", []),
                "operation_taxonomy": sink.get("taxonomy", "unknown_effect"),
                "dataflow_status": (
                    "interprocedural"
                    if implementation_id and on_transact_id and implementation_id != on_transact_id
                    else chain.get("dataflow_status", "not_proven")
                ),
                "deterministic_chain_verified": bool(
                    sink.get("effect_verified") is True
                    and not any(gap.get("critical") is True for gap in critical_gaps)
                ),
                "impact_status": "statically_confirmed" if sink.get("effect_verified") is True else "potential",
                "guard_status": guard_status,
                "guard_coverage": {
                    "status": guard_status,
                    "layers": {"common": common_guard, "case": case_guard, "implementation": implementation_guard},
                    "guards": [
                        *common_guard.get("guards", []), *case_guard.get("guards", []),
                        *implementation_guard.get("guards", []),
                    ],
                },
                "authorization_status": authorization["status"],
                "authorization_matrix": authorization["rows"],
                "blocking_gaps": critical_gaps,
            })
            # v2026-08-09（Cluster E 修复延伸）：主循环 critical_gap 链
            # （flow_kind=critical_gap → implementation_guard=unknown）同样存在
            # "不检查调用者校验"的盲区。但调用者校验必须位于**当前 transaction
            # 的 case 作用域内**才有效（文件其它 case 的 enforceCallingPermission
            # 不保护本链）——因此只扫描 transaction.path 文件中
            # [case_line, case_end_line] 行号范围内的校验证据。
            if chain.get("flow_kind") == "critical_gap" or (implementation_id and transaction_gaps):
                _tx_path = str(transaction.get("path") or "")
                _case_start = int(transaction.get("case_line") or transaction.get("line") or 0)
                _case_end = int(transaction.get("case_end_line") or _case_start)
                _tx_file = next(
                    (f for f in files if str(f.get("path") or "") == _tx_path), None
                )
                if _tx_file is not None:
                    _tx_content = str(_tx_file.get("content") or "")
                    _tx_lines = _tx_content.splitlines()
                    _tx_segment = "\n".join(_tx_lines[_case_start - 1:_case_end])
                    if re.search(
                        r"(?:Binder\s*\.\s*getCallingUid|getCallingPid|getNameForUid|"
                        r"checkCallingPermission|enforceCallingPermission|"
                        r"checkCallingOrSelfPermission|enforceCallingOrSelfPermission|"
                        r"checkSignatures|checkUidSignatures)",
                        _tx_segment,
                    ):
                        continue
            candidates.append(result)
    if not binder_facts.get("transactions") and global_gaps:
        on_bind = binder_facts.get("on_bind") or {}
        # v2026-08-09（Cluster E 误报根因修复）：transaction 解析失败（如
        # BINDER_RETURN_TYPE_AMBIGUOUS）时，规则此前直接按 guard_status=unknown
        # 生成候选，完全不检查调用者身份校验——但校验往往写在闭包文件自身。
        # 命中 caller check 证据则抑制候选，而非继续误报 "caller check missing"。
        if _binder_caller_check_lines(files):
            return sorted(candidates, key=_candidate_sort_key)
        source = {
            "path": files[0]["path"],
            "line": int(on_bind.get("start_line", 1)),
            "text": "onBind concrete Binder target",
            "kind": "binder_transaction",
            "status": "fact",
        }
        result = chain_to_candidate(
            _base(
                "SERVICE_BINDER_CALLER_CHECK_MISSING", component, "L2", files, manifest,
                "Binder concrete transaction 绑定存在关键歧义，无法确定性关闭攻击面",
            ),
            {
                "entry_method_id": on_bind.get("id"),
                "entry_method_name": on_bind.get("name") or "onBind",
                "source": source,
                "sink": {**source, "kind": "binder_unknown_effect", "status": "inferred"},
                "path": [],
                "flow_kind": "critical_gap",
                "path_model": "binder_dispatch_v1",
                "chain_scope": {"component": component.get("name")},
            },
        )
        result.update({
            "binder_remote_interface": True,
            "binder_transactions": [],
            "dataflow_status": "not_proven",
            "deterministic_chain_verified": False,
            "impact_status": "potential",
            "guard_status": "unknown",
            "blocking_gaps": global_gaps,
        })
        candidates.append(result)
    elif not binder_facts.get("transactions"):
        # v2026-08-16（S3）：事务完全未解析但组件文件显示敏感 AIDL 能力 +
        # 无 caller check 的导出 Service，确定性升级 L2（安全网）。
        upgrade = _service_sensitive_binder_upgrade(files)
        if upgrade:
            source = {
                "path": files[0]["path"],
                "line": 1,
                "text": "onBind sensitive AIDL Stub capability",
                "kind": "binder_transaction",
                "status": "fact",
            }
            result = chain_to_candidate(
                _base(
                    "SERVICE_BINDER_CALLER_CHECK_MISSING", component, "L2", files, manifest,
                    "导出 Service 暴露敏感 AIDL 能力且未发现调用者身份校验（Binder 事务未解析）",
                ),
                {
                    "entry_method_id": (binder_facts.get("on_bind") or {}).get("id"),
                    "entry_method_name": "onBind",
                    "source": source,
                    "sink": {**source, "kind": "binder_unknown_effect", "status": "inferred"},
                    "path": [],
                    "flow_kind": "binder_dispatch",
                    "path_model": "binder_dispatch_v1",
                    "chain_scope": {"component": component.get("name")},
                },
            )
            result.update({
                "binder_remote_interface": True,
                "binder_transactions": [],
                "dataflow_status": "not_proven",
                "deterministic_chain_verified": False,
                "impact_status": "potential",
                "guard_status": "unknown",
                "blocking_gaps": [{
                    "code": "SERVICE_SENSITIVE_BINDER_UPGRADE",
                    "critical": False,
                    "sensitive_terms": upgrade["sensitive_terms"],
                }],
            })
            candidates.append(result)
    return sorted(candidates, key=_candidate_sort_key)


def _provider_rule_candidates(
    rule_id: str,
    component: dict[str, Any],
    scopes: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """消费逐 overload DataFlow chains，并按 entry/chain 生成 Provider 候选。"""

    candidates: list[dict[str, Any]] = []
    for scope in scopes:
        entry_name = str(scope.get("entry_name") or "")
        entry_id = str(scope.get("entry_method_id") or "")
        analyzer = DataFlowAnalyzer(scope.get("files", []), [entry_id], scope.get("gaps", []))
        flow = analyzer.analyze_entry({entry_name})
        value_chains = list(flow.get("chains", []))
        return_chains = [chain for chain in value_chains if chain.get("flow_kind") == "return_disclosure"]
        if rule_id == "PROVIDER_CALLER_CHECK_MISSING":
            chains = [*analyzer.effect_chains(entry_id), *return_chains]
        elif rule_id == "PROVIDER_URI_TO_FILE":
            chains = [
                chain for chain in value_chains
                if (chain.get("source") or {}).get("source_kind") == "provider_uri"
                and (chain.get("sink") or {}).get("effect_verified") is True
                and (
                    str((chain.get("sink") or {}).get("kind") or "").startswith("file_")
                    or (chain.get("sink") or {}).get("taxonomy") == "file_mutation"
                )
            ]
        elif rule_id == "PROVIDER_SQL_STRUCTURE_INJECTION":
            chains = [
                chain for chain in value_chains
                if (chain.get("source") or {}).get("source_kind") in {
                    "provider_projection", "provider_selection", "provider_sort_order"
                }
                and (chain.get("sink") or {}).get("method_name") in {"query", "rawQuery", "execSQL", "compileStatement"}
                and bool((chain.get("sink") or {}).get("reaching_argument_indices"))
            ]
        elif rule_id == "PROVIDER_UNAUTHORIZED_QUERY":
            chains = []
            if entry_name == "query":
                for chain in return_chains:
                    sensitivity, evidence = _provider_chain_result_sensitivity(chain, analyzer)
                    sink = dict(chain.get("sink") or {})
                    if sensitivity == "non_sensitive":
                        continue
                    if sensitivity == "sensitive":
                        sink["sensitive_result"] = True
                        sink["sensitive_data_evidence"] = evidence
                    else:
                        sink["sensitive_result"] = False
                        sink["effect_verified"] = False
                        chain = {
                            **chain,
                            "blocking_gaps": [
                                *chain.get("blocking_gaps", []),
                                {
                                    "code": "PROVIDER_QUERY_SENSITIVITY_UNPROVEN",
                                    "critical": True,
                                    "entry_method_id": entry_id,
                                },
                            ],
                        }
                    chains.append({**chain, "sink": sink})
                # v2026-08-16（S3）：query→helper 委托 / 常量投影列名场景
                # （DeviceProvider V-03 型）DataFlow 追踪不到 return_disclosure 链，
                # 回退到确定性列名/语句判定，直接以 query effect 出链。
                if not chains:
                    query_effect = _provider_query_effect(scope.get("files", []))
                    if query_effect:
                        chains.append({
                            "entry_method_id": entry_id,
                            "entry_method_name": entry_name,
                            "source": _provider_entry_source(
                                analyzer, entry_id,
                                _synthetic(scope.get("files", []), "外部 query/selection/projection") or {},
                            ),
                            "sink": {
                                **query_effect,
                                "sensitive_result": True,
                                "sensitive_data_evidence": query_effect["sensitive_data_evidence"],
                                # S5（2026-08-16）：method_id 必须与 line 所在方法一致
                                # （query→helper 场景 line 在 helper 内），否则证据回查
                                # METHOD_LOCATION_MISMATCH 会剥离 sink（DeviceProvider 实证）。
                                "method_id": query_effect.get("method_id") or entry_id,
                                "method_name": query_effect.get("method_name") or entry_name,
                            },
                            "path": [],
                            "blocking_gaps": [],
                            "dataflow_status": "intraprocedural",
                            "flow_kind": "return_disclosure",
                            # S8（2026-08-16）：fallback 的 sink 由列名词表确定性判定，
                            # 不依赖 getInstance 等工厂调用解析；flow 的符号歧义 gap
                            # 不阻塞闭合（DeviceProvider 实证）。
                            "symbol_gap_exempt": True,
                        })
        else:
            chains = [
                chain for chain in value_chains
                if entry_name in {"insert", "update", "delete", "openFile", "call", "applyBatch"}
                and chain.get("flow_kind") in {"source_to_sink", "control_to_sink"}
                and (chain.get("sink") or {}).get("effect_verified") is True
                and (chain.get("sink") or {}).get("taxonomy") in {
                    "persistent_state_write", "file_mutation", "database_mutation",
                    "device_protocol_output", "callback_event_injection", "connection_session_control",
                }
            ]
        for chain in chains:
            chain = {
                **chain,
                "entry_method_id": chain.get("entry_method_id") or entry_id,
                "entry_method_name": chain.get("entry_method_name") or entry_name,
                "source": _provider_entry_source(analyzer, entry_id, chain.get("source") or {}),
            }
            sink = chain.get("sink") or {}
            mode = _provider_chain_mode(entry_name, sink)
            operation = entry_name if entry_name in {"query", "insert", "update", "delete", "openFile", "call", "applyBatch"} else "provider_access"
            authorization = evaluate_authorization(
                manifest, component, operation,
                path=(chain.get("source") or {}).get("path_region"), mode=mode, entry=entry_name,
            )
            if authorization["status"] == "strongly_protected" and not authorization["has_uri_grant_alternative"]:
                continue
            guard = analyzer.guard_coverage(chain, entry_method_id=entry_id, sink=sink)
            if guard["status"] == "present_effective":
                continue
            coverage_gap_items = flow.get("coverage_gaps", [])
            if chain.get("symbol_gap_exempt"):
                coverage_gap_items = [
                    item for item in coverage_gap_items
                    if not (
                        isinstance(item, dict)
                        and item.get("code") in {"SYMBOL_TARGET_AMBIGUOUS", "CALL_TARGET_UNRESOLVED"}
                    )
                ]
            gaps = [
                *chain.get("blocking_gaps", []),
                *coverage_gap_items,
                *guard.get("blocking_gaps", []),
                *authorization.get("blocking_gaps", []),
            ]
            files = scope.get("files", [])
            result = chain_to_candidate(
                _base(
                    rule_id, component, "L2", files, manifest,
                    "Provider 精确入口存在未充分授权的真实数据流/能力链",
                ),
                chain,
            )
            result.update({
                "entry_descriptor": scope.get("entry_descriptor"),
                "operation_taxonomy": sink.get("taxonomy", "unknown_effect"),
                "dataflow_status": chain.get("dataflow_status", "not_proven"),
                "deterministic_chain_verified": bool(
                    sink.get("effect_verified") is True
                    and not any(gap.get("critical") is True for gap in gaps)
                ),
                "impact_status": "statically_confirmed" if sink.get("effect_verified") is True else "potential",
                "guard_status": guard["status"],
                "guard_coverage": guard,
                "guard_summary": guard,
                "authorization_status": authorization["status"],
                "authorization_matrix": authorization["rows"],
                "authorization_operation": operation,
                "operation_mode": mode,
                "blocking_gaps": gaps,
                "coverage_gaps": coverage_gap_items,
            })
            if rule_id == "PROVIDER_URI_TO_FILE":
                boundary_status = _canonical_boundary_status(files)
                duplicate_authorities = _duplicate_authorities(component, manifest)
                result.update({
                    "path_boundary_status": boundary_status,
                    "operation_modes": [mode] if mode else _provider_operation_modes(files),
                    "duplicate_authorities": duplicate_authorities,
                    "authority_resolution_status": "ambiguous" if duplicate_authorities else "unique",
                    "deterministic_chain_verified": bool(
                        boundary_status == "unsafe_prefix"
                        and sink.get("effect_verified") is True
                        and not any(gap.get("critical") is True for gap in gaps)
                    ),
                })
            candidates.append(result)
    return sorted(candidates, key=_candidate_sort_key)


def _provider_entry_source(
    analyzer: DataFlowAnalyzer, entry_method_id: str, source: dict[str, Any]
) -> dict[str, Any]:
    """Prefer a real Provider entry parameter; mark any fallback as inferred."""

    if source.get("kind") != "capability_entry":
        return source
    method = analyzer.methods_by_id.get(str(entry_method_id))
    if method:
        for parameter in method.get("structured_parameters", []) or []:
            if not isinstance(parameter, dict) or not parameter.get("name"):
                continue
            return {
                "path": method.get("path"),
                "line": int(method.get("start_line", 1)),
                "text": str(parameter["name"]),
                "kind": "entry_parameter",
                "method_id": method.get("id"),
                "method_name": method.get("name"),
                "source_kind": parameter.get("source_kind") or "provider_parameter",
                "source_basis": parameter.get("source_basis") or "provider-entry-signature",
                "parameter_position": parameter.get("position"),
                "parameter_type": parameter.get("qualified_type") or parameter.get("normalized_type"),
                "status": "fact",
            }
    return {**source, "status": "inferred"}


def _provider_chain_result_sensitivity(
    chain: dict[str, Any], analyzer: DataFlowAnalyzer
) -> tuple[str, str | None]:
    """只以实际返回 Cursor 的列、SQL SELECT 或 RowBuilder 值判断敏感性。"""

    source = chain.get("source") or {}
    detail = next(
        (
            node for node in chain.get("path", [])
            if isinstance(node, dict) and node.get("kind") == "sensitive_result"
        ),
        source,
    )
    method_name = str(detail.get("operation_name") or detail.get("method_name") or "")
    arguments = [str(value) for value in detail.get("arguments", [])]

    def classify(expression: str) -> tuple[str, str | None]:
        match = SENSITIVE_DATA_RE.search(expression)
        return ("sensitive", match.group(0)) if match else ("non_sensitive", None)

    if method_name == "rawQuery" and arguments:
        sql = arguments[0]
        selected = re.search(r"\bselect\s+(.*?)\s+from\b", sql, re.I | re.S)
        return classify(selected.group(1)) if selected else ("unknown", None)
    if method_name == "query" and len(arguments) >= 2:
        projection = arguments[1].strip()
        if re.search(r"(?:new\s+String\s*\[|arrayOf\s*\(|\{)[\s\S]*[\"']", projection):
            return classify(projection)
        return "unknown", None
    if method_name == "MatrixCursor" and arguments:
        columns = arguments[0].strip()
        if re.search(r"(?:new\s+String\s*\[|arrayOf\s*\(|\{)[\s\S]*[\"']", columns):
            return classify(columns)

    method = analyzer.methods_by_id.get(str(source.get("method_id") or ""))
    if method and (method_name == "MatrixCursor" or (source.get("kind") == "cursor_result")):
        row_adds = [
            call for call in method.get("call_sites", [])
            if call.get("method_name") == "add"
            and (
                str(call.get("receiver_type") or "").rsplit(".", 1)[-1] == "RowBuilder"
                or "newRow" in str(call.get("receiver_text") or "")
            )
        ]
        if row_adds:
            values = " ".join(
                str(argument) for call in row_adds for argument in call.get("arguments", [])
            )
            return classify(values)
    return "unknown", None


def _provider_chain_mode(entry_name: str, sink: dict[str, Any]) -> str | None:
    if entry_name != "openFile":
        return None
    arguments = " ".join(str(value) for value in sink.get("arguments", []))
    for token, mode in (
        ("MODE_READ_WRITE", "rw"), ("MODE_WRITE_ONLY", "w"),
        ("MODE_APPEND", "wa"), ("MODE_TRUNCATE", "rwt"), ("MODE_READ_ONLY", "r"),
    ):
        if token in arguments:
            return mode
    literal = re.search(r"[\"'](r|w|wa|rw|rwt)[\"']", arguments)
    return literal.group(1) if literal else None


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    transaction = candidate.get("binder_transaction") or {}
    return (
        str(candidate.get("component_name") or ""),
        str(candidate.get("entry_method_name") or transaction.get("interface_method") or ""),
        str(candidate.get("entry_descriptor") or transaction.get("dispatch_descriptor") or ""),
        int(transaction.get("code") if transaction.get("code") is not None else -1),
        str(candidate.get("chain_id") or ""),
    )


def _component_rule(
    rule_id: str,
    component: dict,
    files: list[dict],
    manifest: dict,
    binder_facts: dict[str, Any] | None = None,
    component_flow_scope: dict[str, Any] | None = None,
) -> dict | None:
    """结合组件暴露面与源码信号，构造非 DataFlow 候选证据。"""
    if rule_id in COMPONENT_FLOW_ENTRIES:
        raise ValueError("DataFlow rules must use _component_flow_rule_candidates")
    exported = component.get("exported") == "true"
    preliminary_authorization = _authorization(component, rule_id, manifest)
    no_permission = preliminary_authorization["status"] == "unprotected"
    code = "\n".join(item.get("content", "") for item in files)
    # L1 仅依据清单可达性、权限声明或低成本启发式信号，不声称存在完整数据流。
    if rule_id.endswith("EXPORTED_NO_PERMISSION"):
        if not (exported and no_permission):
            return None
        if component["kind"] == "receiver" and _only_protected_system_actions(component):
            return None
        return _base(rule_id, component, "L1", files, manifest, "确认组件外部可达且未声明组件权限")
    if rule_id == "PROVIDER_READ_WRITE_PERMISSION_MISSING":
        if not exported or preliminary_authorization["status"] == "strongly_protected":
            return None
        result = _base(rule_id, component, "L1", files, manifest, "导出 Provider 的有效读取或写入策略存在弱保护或未知区域")
        result["authorization_matrix"] = preliminary_authorization["rows"]
        result["authorization_status"] = preliminary_authorization["status"]
        result["blocking_gaps"].extend(preliminary_authorization["blocking_gaps"])
        return result
    if rule_id == "ACTIVITY_SENSITIVE_NAME_HINT":
        if not re.search(r"(?:Reset|Password|Admin|Payment|Debug)", component.get("name", ""), re.I):
            return None
        result = _base(rule_id, component, "L1", files, manifest, "组件命名包含敏感业务启发式词")
        result["auxiliary"] = True
        return result
    if rule_id == "PROVIDER_LOOSE_URI_MATCH":
        loose_manifest = any("*" in str(value) for row in component.get("path_permissions", []) for value in row.values())
        loose_code = bool(re.search(r"addURI\s*\([^)]*(?:\*|#)", code))
        if not (loose_manifest or loose_code):
            return None
        result = _base(rule_id, component, "L1", files, manifest, "Provider URI 匹配包含宽松通配符")
        result["auxiliary"] = True
        return result
    if not exported or not files:
        return None
    authorization = preliminary_authorization["status"]
    if authorization == "strongly_protected" and not preliminary_authorization["has_uri_grant_alternative"]:
        return None
    # L2 从组件类型选择对应 Source，再寻找同一组件范围内的 Sink 与调用方校验信号。
    source_pattern = {
        "activity": SOURCE_PATTERNS["intent_extra"],
        "service": SOURCE_PATTERNS["ipc_input"],
        "provider": SOURCE_PATTERNS["provider_uri"],
        "receiver": SOURCE_PATTERNS["receiver_input"],
    }[component["kind"]]
    source = _first_match(files, source_pattern)
    sink = _first_sink(files)
    semantic = DataFlowAnalyzer(
        (component_flow_scope or {}).get("files", files),
        entry_method_ids=(component_flow_scope or {}).get("entry_method_ids"),
        scope_gaps=(component_flow_scope or {}).get("gaps"),
    )
    special_metadata: dict[str, Any] = {}
    if rule_id == "SERVICE_BINDER_CALLER_CHECK_MISSING":
        binder_facts = binder_facts or {}
        indexed_on_bind = binder_facts.get("on_bind")
        on_bind = next((
            (file, method) for file in files for method in file.get("methods", [])
            if indexed_on_bind and method.get("id") == indexed_on_bind.get("id")
        ), None) or _method_by_name(files, "onBind")
        raw_transactions = binder_facts.get("transactions") or []
        transactions = []
        for item in raw_transactions:
            interface_method = str(item.get("interface_method") or "")
            operation = classify_operation_taxonomy({
                "method_name": interface_method,
                "method_descriptor": item.get("implementation_method_descriptor"),
                "receiver_type": item.get("implementation_receiver_type") or "",
                "receiver_text": item.get("implementation_receiver") or "",
                "arguments": [],
            })
            sensitive = bool(interface_method and SENSITIVE_BINDER_METHOD_RE.search(f"{interface_method}(") )
            returns_sensitive_data = bool(
                sensitive and interface_method.lower().startswith(("get", "read", "query"))
                and item.get("parcel_writes")
            )
            transactions.append({
                **item,
                "sensitive": sensitive,
                "operation_taxonomy": operation.get("taxonomy"),
                "impact_verified": bool(operation.get("verified") and operation.get("is_effect")) or returns_sensitive_data,
            })
        class_facts = [item for file in files for item in file.get("classes", [])]
        inheritance_chain = binder_facts.get("inheritance_chain", [])
        aidl_stub = any(
            str(item.get("extends") or "").endswith("Stub")
            or any(str(interface).endswith("IInterface") for interface in item.get("implements", []))
            for item in [*class_facts, *inheritance_chain]
        )
        has_remote_dispatch = bool(transactions) or bool(re.search(r"\bDESCRIPTOR\b.*\bonTransact\s*\(", code, re.S))
        remote_stub = aidl_stub or has_remote_dispatch
        sensitive_transaction = next((
            item for item in transactions
            if item.get("sensitive") and item.get("impact_verified")
        ), None)
        if not (on_bind and remote_stub and sensitive_transaction):
            return None
        source = _method_evidence(files, on_bind, "Binder/AIDL 外部方法")
        sink = {
            "path": sensitive_transaction["path"],
            "line": sensitive_transaction["line"],
            "text": f"transaction {sensitive_transaction['code']} → {sensitive_transaction['interface_method']}",
            "kind": "binder_sensitive_api",
            "method_name": sensitive_transaction["interface_method"],
            "effect_verified": True,
        }
        special_metadata.update({
            "binder_remote_interface": True,
            "binder_transactions": transactions,
            "binder_return_types": binder_facts.get("return_types", []),
            "binder_inheritance_chain": inheritance_chain,
            "dataflow_status": "interprocedural" if sensitive_transaction else "not_proven",
            "deterministic_chain_verified": bool(sensitive_transaction),
            "impact_status": "statically_confirmed" if sensitive_transaction else "potential",
        })
    elif rule_id == "PROVIDER_CALLER_CHECK_MISSING":
        crud = re.search(r"\b(?:query|insert|update|delete|openFile|call|applyBatch)\s*\(", code)
        if not (crud and sink):
            return None
        source = source or _synthetic(files, "Provider CRUD 外部参数")
        if not source:
            return None
    elif rule_id == "PROVIDER_URI_TO_FILE":
        file_effect = _provider_file_effect(files)
        if not (source and file_effect):
            return None
        sink = file_effect
        boundary_status = _canonical_boundary_status(files)
        duplicate_authorities = _duplicate_authorities(component, manifest)
        special_metadata.update({
            "path_boundary_status": boundary_status,
            "duplicate_authorities": duplicate_authorities,
            "authority_resolution_status": "ambiguous" if duplicate_authorities else "unique",
            "operation_modes": _provider_operation_modes(files),
            "provider_paths": component.get("provider_paths", []),
            "grant_uri_permissions": component.get("grant_uri_permissions"),
            "grant_uri_patterns": component.get("grant_uri_patterns", []),
            "dataflow_status": "intraprocedural",
            "deterministic_chain_verified": boundary_status == "unsafe_prefix",
            "impact_status": "statically_confirmed" if boundary_status == "unsafe_prefix" else "potential",
        })
    elif rule_id == "PROVIDER_SQL_STRUCTURE_INJECTION":
        structure_source = _first_match(files, re.compile(r"\b(?:selection|projection|sortOrder|groupBy|having)\b"))
        sql_concat = _first_match(files, re.compile(r"(?:rawQuery|execSQL|query)\s*\([^;\n]*(?:\+|String\.format|append\s*\()"))
        if not (structure_source and sql_concat):
            return None
        source, sink = structure_source, sql_concat
    elif rule_id == "PROVIDER_UNAUTHORIZED_QUERY":
        query_effect = _provider_query_effect(files)
        if not query_effect:
            return None
        source = source or _synthetic(files, "外部 query/selection/projection")
        if not source:
            return None
        sink = query_effect
        special_metadata.update({
            "dataflow_status": "intraprocedural",
            "deterministic_chain_verified": True,
            "impact_status": "statically_confirmed",
        })
    elif rule_id == "PROVIDER_UNAUTHORIZED_MUTATION":
        mutation = _provider_mutation_effect(files)
        if not mutation:
            return None
        source = source or _synthetic(files, "外部 ContentValues/selection")
        if not source:
            return None
        sink = mutation
        special_metadata.update({
            "dataflow_status": "intraprocedural",
            "deterministic_chain_verified": mutation.get("effect_verified") is True,
            "impact_status": "statically_confirmed" if mutation.get("effect_verified") is True else "potential",
            "semantic_blocking_gaps": [mutation["classification_gap"]] if mutation.get("classification_gap") else [],
        })
    elif not (source and sink):
        return None
    guard_coverage = special_metadata.get("guard_coverage") or semantic.guard_coverage(sink=sink)
    special_metadata["guard_coverage"] = guard_coverage
    special_metadata["guard_summary"] = guard_coverage
    if guard_coverage.get("status") == "present_effective":
        return None
    operation, mode = operation_for_rule(rule_id, sink)
    if rule_id == "PROVIDER_URI_TO_FILE":
        operation_modes = special_metadata.get("operation_modes", [])
        mode = operation_modes[0] if len(operation_modes) == 1 else None
    effective_authorization = _authorization(
        component, rule_id, manifest, sink=sink, operation=operation, mode=mode
    )
    if (
        effective_authorization["status"] == "strongly_protected"
        and not effective_authorization["has_uri_grant_alternative"]
    ):
        return None
    # 仅在特定规则分支确认所需 Source/Sink 条件后升级为 L2，并记录可追溯证据链。
    result = _base(rule_id, component, "L2", files, manifest, "已识别组件入口到敏感操作的静态候选链路")
    result["sources"] = [_evidence(source, "taint_source")]
    result["sinks"] = [_evidence(sink, sink.get("kind", "sensitive_sink"))]
    same_method = source.get("method_name") and source.get("method_name") == sink.get("method_name")
    semantic_path = special_metadata.pop("semantic_path", [])
    semantic_gaps = special_metadata.pop("coverage_gaps", [])
    semantic_blocking_gaps = special_metadata.pop("semantic_blocking_gaps", [])
    result["propagation_paths"] = semantic_path or [{
        "text": (
            f"{source['path']}#{source.get('method_name')} 中外部输入变量传播到 {sink.get('kind', 'sensitive_sink')}"
            if same_method else
            f"{source['path']} 中入口与敏感操作需要跨方法继续复核"
        ),
        "status": "fact" if same_method else "candidate",
        "evidence_id": f"{source['path']}:{source['line']}",
    }]
    result["dataflow_status"] = "intraprocedural" if same_method else "not_proven"
    result["deterministic_chain_verified"] = bool(same_method or sink.get("effect_verified"))
    result["impact_status"] = "statically_confirmed" if sink.get("effect_verified") else "potential"
    result["coverage_gaps"] = semantic_gaps
    result["authorization_status"] = effective_authorization["status"]
    result["authorization_matrix"] = effective_authorization["rows"]
    result["authorization_operation"] = operation
    result["guard_status"] = guard_coverage["status"]
    result.update(special_metadata)
    result["blocking_gaps"] = [
        *semantic_blocking_gaps,
        *effective_authorization["blocking_gaps"],
        *guard_coverage.get("blocking_gaps", []),
    ]
    if result["authorization_status"] == "unknown":
        result["blocking_gaps"].append({
            "code": "AUTHORIZATION_STATUS_UNKNOWN", "critical": True,
        })
    if result["guard_status"] in {"unknown", "present_partial"}:
        result["blocking_gaps"].append({
            "code": "GUARD_COVERAGE_UNPROVEN", "critical": True,
            "guard_status": result["guard_status"],
        })
    if special_metadata.get("authority_resolution_status") == "ambiguous":
        result["blocking_gaps"].append({
            "code": "DUPLICATE_PROVIDER_AUTHORITY",
            "critical": True,
            "providers": special_metadata["duplicate_authorities"],
        })
    if special_metadata.get("path_boundary_status") == "unsafe_prefix":
        result["severity_reason"] = ["canonical 路径仅使用字符串前缀检查，缺少目录分隔符边界"]
        result["impact_status"] = "statically_confirmed"
    return result


def _dynamic_receiver_exposures(file: dict, manifest: dict | None = None) -> list[dict]:
    """兼容返回共享 parser 判定出的当前调用点暴露面。"""

    exposures = []
    for registration in parse_receiver_registrations(file, manifest):
        if not registration.get("reportable"):
            continue
        status = registration.get("flag_status")
        exposures.append({
            "line": registration["line"],
            "status": status,
            "platform": (
                "显式导出 Receiver 注册路径"
                if status == "exported" else
                "存在 SDK 分支：旧 Android 分支未指定 receiver flag"
                if registration.get("platform_branch") else
                "Receiver 导出状态未知，需复核 flag/permission"
                if status == "unknown" else
                "旧式无 flag registerReceiver；外部可达性依赖 action/permission/API"
            ),
            "registration": registration,
        })
    return exposures


def _method_by_name(files: list[dict], name: str) -> tuple[dict, dict] | None:
    """返回首个指定方法及所属文件。"""

    for file in files:
        for method in file.get("methods", []):
            if method.get("name") == name:
                return file, method
    return None


def _method_evidence(files: list[dict], match: tuple[dict, dict], text: str) -> dict:
    """把结构化方法转换为规则证据。"""

    file, method = match
    return {
        "path": file["path"],
        "line": int(method.get("start_line", 1)),
        "text": text,
        "kind": "external_input",
        "method_name": method.get("name"),
    }


def _first_executable_match(files: list[dict], pattern: re.Pattern, kind: str) -> dict | None:
    """在清洗后的方法体中查找指定语法事实。"""

    match = _first_match(files, pattern)
    if match:
        match["kind"] = kind
    return match


def _provider_operation_modes(files: list[dict]) -> list[str]:
    """提取 openFile 支持或映射的读写模式，用于区分只读与 mutation 能力。"""

    modes = set()
    for file in files:
        for method in file.get("methods", []):
            if method.get("name") not in {"openFile", "modeToMode", "parseMode"}:
                continue
            content = method.get("content", "")
            modes.update(re.findall(r"[\"'](r|w|wa|rw|rwt)[\"']", content))
            if "MODE_READ_ONLY" in content:
                modes.add("r")
            if "MODE_WRITE_ONLY" in content:
                modes.add("w")
            if "MODE_READ_WRITE" in content:
                modes.add("rw")
            if "MODE_APPEND" in content:
                modes.add("wa")
            if "MODE_TRUNCATE" in content:
                modes.add("rwt")
    return sorted(modes)


def _provider_file_effect(files: list[dict]) -> dict | None:
    """确认 Provider 的 openFile/delete 方法存在真实文件打开或删除副作用。"""

    patterns = [
        ("file_delete", re.compile(r"\.\s*delete\s*\(")),
        ("file_open", re.compile(r"ParcelFileDescriptor\.open\s*\(")),
        ("file_write", re.compile(r"(?:FileOutputStream|Files\.write|openFileOutput)")),
    ]
    for file in files:
        for method in file.get("methods", []):
            if method.get("name") not in {"openFile", "delete"}:
                continue
            sanitized = _sanitize_executable(method.get("content", ""))
            for effect_kind, pattern in patterns:
                match = pattern.search(sanitized)
                if match:
                    modes = _provider_operation_modes(files)
                    if effect_kind == "file_open" and modes and set(modes) <= {"r"}:
                        effect_kind = "file_read"
                    return {
                        "path": file["path"],
                        "line": int(method.get("start_line", 1)) + sanitized.count("\n", 0, match.start()),
                        "text": match.group(0),
                        "kind": effect_kind,
                        "method_name": method.get("name"),
                        "operation_modes": modes,
                        "effect_verified": True,
                    }
    return None


def _canonical_boundary_status(files: list[dict]) -> str:
    """以窄正则启发式区分 canonical 路径边界形状，不作完整路径证明。

    只有同时看到 canonicalization 与 equals(root/file) 或 ``root + separator`` 前缀边界才返回
    safe_boundary；裸 startsWith 标为 unsafe_prefix。别名、封装 helper 或复杂控制流无法识别时
    返回 unknown/absent，调用方不得据此证明无目录穿越。
    """

    code = "\n".join(method.get("content", "") for file in files for method in file.get("methods", []))
    if not re.search(r"getCanonical(?:File|Path)", code):
        return "absent"
    safe_boundary = re.search(
        r"(?:equals\s*\([^)]*(?:root|file)|startsWith\s*\([^)]*\+\s*(?:File\.)?separator)",
        code,
        re.I,
    )
    if safe_boundary:
        return "safe_boundary"
    if re.search(r"startsWith\s*\(", code):
        return "unsafe_prefix"
    return "unknown"


def _duplicate_authorities(component: dict, manifest: dict) -> list[str]:
    """返回与当前 Provider 共享 authority 的其他组件。"""

    authorities = {
        value.strip() for value in str(component.get("authorities") or "").split(";") if value.strip()
    }
    if not authorities:
        return []
    conflicts = manifest.get("authority_conflicts", {})
    owners = {
        owner for authority in authorities for owner in conflicts.get(authority, [])
        if owner != component.get("name")
    }
    if not owners:
        owners = {
            item.get("name") for item in manifest.get("components", [])
            if item.get("kind") == "provider"
            and item.get("name") != component.get("name")
            and authorities.intersection({
                value.strip() for value in str(item.get("authorities") or "").split(";") if value.strip()
            })
            and item.get("name")
        }
    return sorted(owners)


def _intraprocedural_flow(files: list[dict], source_pattern: re.Pattern) -> tuple[dict, dict] | None:
    """证明 Source 返回值或参数在同一方法内进入敏感调用参数。"""

    for file in files:
        for method in file.get("methods", []):
            original = _method_body_text(method)
            sanitized = _sanitize_executable(original)
            source_match = source_pattern.search(sanitized)
            if not source_match:
                continue
            source_line = int(method.get("start_line", 1)) + sanitized.count("\n", 0, source_match.start())
            source = {
                "path": file["path"], "line": source_line,
                "text": original[source_match.start():source_match.end()][:200],
                "kind": "taint_source", "method_name": method.get("name"),
            }
            statement_start = sanitized.rfind("\n", 0, source_match.start()) + 1
            statement_end = sanitized.find(";", source_match.end())
            statement_end = statement_end if statement_end >= 0 else len(sanitized)
            statement = sanitized[statement_start:statement_end]
            assigned = re.search(r"\b([A-Za-z_$][\w$]*)\s*=", statement)
            tainted_name = assigned.group(1) if assigned else None
            for sink_kind, sink_pattern in SINK_PATTERNS.items():
                for sink_match in sink_pattern.finditer(sanitized):
                    call_end = sanitized.find(")", sink_match.end())
                    call_end = call_end + 1 if call_end >= 0 else min(len(sanitized), sink_match.end() + 300)
                    call_text = sanitized[sink_match.start():call_end]
                    direct_nesting = sink_match.start() <= source_match.start() and source_match.end() <= call_end
                    if tainted_name:
                        if sink_match.start() <= source_match.end() or not re.search(rf"\b{re.escape(tainted_name)}\b", call_text):
                            continue
                    elif not direct_nesting:
                        continue
                    sink_line = int(method.get("start_line", 1)) + sanitized.count("\n", 0, sink_match.start())
                    sink = {
                        "path": file["path"], "line": sink_line,
                        "text": original[sink_match.start():sink_match.end()][:200],
                        "kind": sink_kind, "method_name": method.get("name"),
                        "tainted_variable": tainted_name,
                    }
                    return source, sink
    return None


def _authorization(
    component: dict,
    rule_id: str | None = None,
    manifest: dict[str, Any] | None = None,
    *,
    sink: dict[str, Any] | None = None,
    operation: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """通过版本化 Effective Authorization Matrix 计算当前操作。"""

    manifest = manifest or {"components": [component], "analysis_platform_api": 36}
    mapped_operation, mapped_mode = operation_for_rule(rule_id, sink)
    return evaluate_authorization(
        manifest,
        component,
        operation or mapped_operation,
        path=(sink or {}).get("uri") or (sink or {}).get("path_region"),
        mode=mode if mode is not None else mapped_mode,
        entry=(sink or {}).get("method_name") or mapped_operation,
    )


def _authorization_status(
    component: dict, rule_id: str | None = None, manifest: dict[str, Any] | None = None
) -> str:
    """兼容返回 operation-specific 授权状态。"""

    return _authorization(component, rule_id, manifest)["status"]


# v2026-08-16（S3）：Provider query 返回列敏感性词表（按 V-03 实际返回列校准；
# MAC 仅 log 未进 Cursor，不作为返回列）。
_PROVIDER_SENSITIVE_COLUMN_RE = re.compile(
    r"(?i)(?:mac|battery|device_name|devicename|device_model|is_connected|isconnect|"
    r"connect_state|connected|account|token|phone|iccid|imei|serial|user_id|userId)"
)


def _provider_cursor_columns(scope_text: str, files: list[dict]) -> list[str]:
    """收集 Cursor 构造/填充涉及的列名（覆盖 V-03 型三种形态）。"""

    columns: list[str] = []
    for match in re.finditer(r"\bnew\s+MatrixCursor\s*\(([^)]*)\)", scope_text):
        argument = match.group(1).strip()
        if not argument or argument == "null":
            continue
        if "\"" in argument:
            columns.extend(re.findall(r"\"([^\"]+)\"", argument))
        else:
            constant_name = argument.split(".")[-1].strip()
            for file in files:
                content = str(file.get("content") or "")
                array_match = re.search(
                    r"\b(?:String\[\]|String\s*\[\])\s*" + re.escape(constant_name) + r"\s*=\s*\{(.*?)\}",
                    content, re.S,
                )
                if array_match:
                    columns.extend(re.findall(r"\"([^\"]+)\"", array_match.group(1)))
    for match in re.finditer(r"\.add\s*\(\s*([\"']?[A-Za-z_$][\w$.]*[\"']?)\s*,", scope_text):
        identifier = match.group(1).strip("\"'")
        if "\"" in match.group(1) or "'" in match.group(1):
            columns.append(identifier)
            continue
        leaf = identifier.split(".")[-1]
        for file in files:
            content = str(file.get("content") or "")
            constant_match = re.search(
                r"\b" + re.escape(leaf) + r"\s*=\s*[\"']([^\"']+)[\"']", content
            )
            if constant_match:
                columns.append(constant_match.group(1))
    return columns


def _provider_query_effect(files: list[dict]) -> dict | None:
    """确认 query 方法实际构造或返回 Cursor，且数据包含潜在敏感字段。

    v2026-08-16（S3）：支持 query→私有 helper 委托（DeviceProvider.query →
    queryDeviceStatus 型），敏感性判定从"构造语句局部共现"扩为跨语句列名词表
    （DEFAULT_*_PROJECTION 常量 + RowBuilder.add 列名）。
    """

    for file in files:
        for method in file.get("methods", []):
            if method.get("name") != "query":
                continue
            original = _method_body_text(method)
            sanitized = _sanitize_executable(original)
            if re.search(r"throw\s+new\s+UnsupportedOperationException", sanitized):
                continue
            query_match = re.search(r"\b(?:MatrixCursor|rawQuery|\.\s*query\s*\()", sanitized)
            if query_match:
                # 保留原"构造语句局部关联"路径（rawQuery/直接构造）。
                statement_start = max(original.rfind("\n", 0, query_match.start()), original.rfind(";", 0, query_match.start())) + 1
                statement_end = original.find(";", query_match.end())
                statement_end = statement_end if statement_end >= 0 else min(len(original), query_match.end() + 400)
                statement = original[statement_start:statement_end]
                sensitive = SENSITIVE_DATA_RE.search(statement)
                if sensitive:
                    return {
                        "path": file["path"],
                        "line": int(method.get("start_line", 1)) + sanitized.count("\n", 0, query_match.start()),
                        "text": statement.strip()[:200],
                        "kind": "sensitive_query_result",
                        "method_name": "query",
                        "method_id": method.get("id"),
                        "taxonomy": "data_disclosure",
                        "sensitive_data_evidence": sensitive.group(0),
                        "effect_verified": True,
                    }
                column = next(
                    (value for value in _provider_cursor_columns(statement, files)
                     if _PROVIDER_SENSITIVE_COLUMN_RE.search(value)),
                    None,
                )
                if column:
                    return {
                        "path": file["path"],
                        "line": int(method.get("start_line", 1)) + sanitized.count("\n", 0, query_match.start()),
                        "text": statement.strip()[:200],
                        "kind": "sensitive_query_result",
                        "method_name": "query",
                        "method_id": method.get("id"),
                        "taxonomy": "data_disclosure",
                        "sensitive_data_evidence": column,
                        "effect_verified": True,
                    }
                continue
            # query→helper 委托（DeviceProvider.query → queryDeviceStatus 型）。
            helper_names = set(re.findall(r"\breturn\s+([A-Za-z_$][\w$]*)\s*\(", original))
            helper_names.update(
                str(call.get("method_name") or "")
                for call in method.get("call_sites", [])
                if str(call.get("method_name") or "") not in {"query", "match", "equals"}
            )
            for helper_file in files:
                for helper in helper_file.get("methods", []):
                    if helper.get("name") not in helper_names or helper.get("id") == method.get("id"):
                        continue
                    helper_text = _method_body_text(helper)
                    helper_sanitized = _sanitize_executable(helper_text)
                    helper_match = re.search(r"\b(?:MatrixCursor|rawQuery|\.\s*query\s*\()", helper_sanitized)
                    if not helper_match:
                        continue
                    columns = _provider_cursor_columns(helper_text, files)
                    column = next(
                        (value for value in columns if _PROVIDER_SENSITIVE_COLUMN_RE.search(value)),
                        None,
                    )
                    if not column:
                        continue
                    return {
                        "path": helper_file["path"],
                        "line": int(helper.get("start_line", 1)) + helper_sanitized.count("\n", 0, helper_match.start()),
                        "text": helper_text.strip()[:200],
                        "kind": "sensitive_query_result",
                        "method_name": "query",
                        "method_id": helper.get("id"),
                        "taxonomy": "data_disclosure",
                        "sensitive_data_evidence": column,
                        "effect_verified": True,
                    }
    return None


_SERVICE_SENSITIVE_BINDER_PATTERNS = (
    re.compile(r"\b(?:startSport|pauseSport|resumeSport|finishSport|restartSport|abnormalChangeSportStateTo(?:Finish|Pause))\b"),
    re.compile(r"\b(?:getDeviceInfo|getDeviceBattery|getCurrentDeviceModel|getDeviceName|getMac|getDid|getBattery)\b"),
    re.compile(r"\b(?:set\w*Listener|add\w*Listener|register\w*(?:Callback|Listener))\b"),
    re.compile(r"\b(?:issueStart|issueEnd|updateFindDeviceStatus|transceive|writeCharacteristic|writeDescriptor)\b"),
)


def _service_sensitive_binder_upgrade(files: list[dict]) -> dict | None:
    """S3：导出 Service 敏感 AIDL 能力升级信号（L1→L2 安全网）。

    条件：组件文件内存在 ``extends ...Stub`` 的类、敏感接口词表命中、无 caller
    check，且没有可解析的 ``onTransact`` 事务——事务可解析时由
    ``SERVICE_BINDER_CALLER_CHECK_MISSING`` 出 L2 候选，此处只兜底避免重复。
    """

    code = "\n".join(str(item.get("content") or "") for item in files)
    if not code:
        return None
    extends_stub = (
        any(
            "Stub" in str(cls.get("extends_name") or "")
            for file in files
            for cls in file.get("classes", [])
        )
        or bool(re.search(r"\bextends\s+[\w.$]*\bStub\b", code))
    )
    if not extends_stub:
        return None
    matched = [pattern.pattern for pattern in _SERVICE_SENSITIVE_BINDER_PATTERNS if pattern.search(code)]
    if not matched:
        return None
    has_transactions = any(
        str(method.get("name") or "") == "onTransact"
        and "case" in str(method.get("content") or "")
        for file in files
        for method in file.get("methods", [])
    )
    if has_transactions:
        return None
    has_caller_check = bool(re.search(
        r"(?:getCallingUid|getCallingPid|getNameForUid|checkCallingPermission|"
        r"enforceCallingPermission|checkCallingOrSelfPermission|enforceCallingOrSelfPermission|"
        r"checkSignatures|checkUidSignatures)",
        code,
    ))
    if has_caller_check:
        return None
    return {"sensitive_terms": matched}


def _provider_mutation_effect(files: list[dict]) -> dict | None:
    """按 receiver 类型确认 Provider 写入口中的真实副作用。"""

    mutation_entries = {"insert", "update", "delete", "applyBatch", "call", "openFile"}
    for file in files:
        for method in file.get("methods", []):
            entry_name = str(method.get("name") or "")
            if entry_name not in mutation_entries:
                continue
            original = _method_body_text(method)
            sanitized = _sanitize_executable(original)
            if re.fullmatch(
                r"\s*\{?\s*(?:return\s+(?:0|null|false)\s*;|throw\s+new\s+UnsupportedOperationException\s*\([^;]*;?)?\s*\}?\s*",
                sanitized,
            ):
                continue
            for call in method.get("call_sites", []):
                call_name = str(call.get("method_name") or "")
                receiver_type = str(call.get("receiver_type") or "")
                receiver_leaf = receiver_type.rsplit(".", 1)[-1]
                classification: dict[str, Any] | None = None
                if call_name == "delete":
                    classification = classify_call_operation(call, entry_name)
                elif call_name in {"insert", "update", "applyBatch", "execSQL", "compileStatement"}:
                    if receiver_leaf == "ContentResolver":
                        classification = {"is_sink": True, "kind": "content_mutation", "verified": True}
                    elif receiver_leaf in {"SQLiteDatabase", "SupportSQLiteDatabase"} or receiver_leaf.endswith(("Dao", "DAO")):
                        classification = {"is_sink": True, "kind": "database_mutation", "verified": True}
                    else:
                        classification = {
                            "is_sink": True,
                            "kind": "unknown_mutation",
                            "verified": False,
                            "gap": {
                                "code": "SINK_RECEIVER_TYPE_UNKNOWN",
                                "critical": True,
                                "method_name": call_name,
                                "receiver_type": receiver_type or None,
                            },
                        }
                if classification and classification.get("is_sink"):
                    return {
                        "path": file["path"],
                        "line": int(call.get("start_line", method.get("start_line", 1))),
                        "text": f"{call.get('receiver_text') or '?'}.{call_name}(...)"[:200],
                        "kind": classification["kind"],
                        "method_name": entry_name,
                        "receiver_type": receiver_type or None,
                        "effect_verified": classification.get("verified") is True,
                        "classification_gap": classification.get("gap"),
                    }
            # 无结构化调用点时保留文件和状态写的窄匹配，不把 Provider 入口声明当 Sink。
            for effect_kind, pattern in (
                ("file_write", re.compile(r"(?:FileOutputStream|Files\.write|ParcelFileDescriptor\.open|openFileOutput)")),
                ("state_write", re.compile(r"(?:SharedPreferences\.Editor|Settings\.(?:Secure|Global)\s*\.\s*put)")),
            ):
                match = pattern.search(sanitized)
                if match:
                    return {
                        "path": file["path"],
                        "line": int(method.get("start_line", 1)) + sanitized.count("\n", 0, match.start()),
                        "text": match.group(0)[:200],
                        "kind": effect_kind,
                        "method_name": entry_name,
                        "effect_verified": True,
                    }
    return None


def _implicit_broadcast_flow(file: dict) -> dict[str, Any] | None:
    """证明同一方法、同一 Intent 变量从敏感 putExtra 进入 sendBroadcast。"""

    for method in file.get("methods", []):
        calls = method.get("call_sites", [])
        if not calls:
            continue
        content = method.get("content", "")
        for put in calls:
            if put.get("method_name") != "putExtra" or not put.get("receiver_text"):
                continue
            relative_line = max(0, int(put.get("start_line", method.get("start_line", 1))) - int(method.get("start_line", 1)))
            lines = content.splitlines()
            statement = lines[relative_line] if relative_line < len(lines) else ""
            if not SENSITIVE_DATA_RE.search(statement):
                continue
            intent_name = str(put["receiver_text"]).rsplit(".", 1)[-1]
            restricted = any(
                call.get("method_name") in {"setPackage", "setComponent"}
                and call.get("receiver_text") == put.get("receiver_text")
                for call in calls
            )
            if restricted:
                continue
            for send in calls:
                if send.get("method_name") != "sendBroadcast":
                    continue
                args = send.get("arguments", [])
                if not args or not re.search(rf"\b{re.escape(intent_name)}\b", args[0]):
                    continue
                source = {
                    "path": file["path"], "line": int(put["start_line"]),
                    "text": f"{put.get('receiver_text')}.putExtra(...)体系敏感字段",
                    "kind": "sensitive_data", "method_name": method.get("name"),
                }
                receiver_text = send.get("receiver_text")
                # 单词边界必须保留：EventBusUtils 等包装类不是 EventBus，误匹配会把
                # 真实跨进程广播降级为 effect_verified=False（漏报真漏洞）。
                # 与 backend/app/analysis/candidate_funnel.py 的 LOCAL_BROADCAST_RECEIVER_RE
                # 语义一致（v2026-08-09 复审，规则层与 backend 层无法共享常量，须手动同步）。
                local_broadcast = bool(receiver_text) and bool(
                    re.search(r"\bLocalBroadcastManager\b|\bEventBus\b", str(receiver_text))
                )
                # 红线 9：LocalBroadcastManager/EventBus 是进程内分发，不构成跨进程外溢通道。
                # 规则层直接降级 effect_verified（确定性事实），避免产生"假闭链"候选——
                # 否则 funnel 会跳过 AI 且 decision 判 supported（负向证明覆盖保守）。
                sink = {
                    "path": file["path"], "line": int(send["start_line"]),
                    "text": "sendBroadcast(...)未限定目标", "kind": "implicit_broadcast",
                    "method_name": method.get("name"),
                    "effect_verified": not local_broadcast,
                    # 确定性 receiver 文本（如 LocalBroadcastManager.getInstance(...)）——
                    # decision 层据此识别"进程内分发"红线 9，为 AI refutes 提供确定性反证背书
                    # （v2026-08-09：详见 docs/updates/2026-08-09-l2-review-302-*.md）。
                    "receiver_text": receiver_text,
                }
                return {
                    "source": source,
                    "sink": sink,
                    "path": [{
                        "text": f"{intent_name}.putExtra → sendBroadcast({intent_name})",
                        "status": "fact",
                        "evidence_id": f"{file['path']}:{put['start_line']}",
                    }],
                }
    return None


def _expand_dynamic_receiver_effects(
    analyzer: DataFlowAnalyzer, binding: dict[str, Any]
) -> dict[str, Any]:
    """Restore every direct onReceive effect instead of the compatibility first chain."""

    if binding.get("transitions") or not binding.get("binding_complete"):
        return binding
    entry_method_id = str(binding.get("on_receive") or "")
    chains = analyzer.effect_chains(entry_method_id) if entry_method_id else []
    effects = [
        {
            "source": chain.get("source"),
            "effect": chain.get("sink"),
            "effect_taxonomy": (chain.get("sink") or {}).get("taxonomy"),
            "effect_kind": (chain.get("sink") or {}).get("kind"),
            "method_path": [
                node.get("method_id") for node in chain.get("path", [])
                if isinstance(node, dict) and node.get("method_id")
            ],
            "path": chain.get("path", []),
            "verified": (chain.get("sink") or {}).get("effect_verified") is True,
        }
        for chain in chains
        if (chain.get("sink") or {}).get("effect_verified") is True
    ]
    if not effects:
        return binding
    return {
        **binding,
        "effects": effects,
        "confirmed_effects": effects,
        "effect_path": [effect.get("path") for effect in effects],
    }


# R-1（2026-08-15，v3）：动态 receiver flag 分级三值——exported 需叠加"无关键 gap"
# 才是真实可判定暴露面（实测 exported 43 条中仅 6 条干净，37 条带 target/action/
# flag gap 仍无法判定）；供 funnel L1 预算按可判定性排序（R-2）。
_BIG_RECEIVER_GAPS = frozenset({
    "RECEIVER_FLAG_UNKNOWN", "RECEIVER_TARGET_UNRESOLVED", "RECEIVER_ACTION_UNRESOLVED",
})


def _receiver_flag_tier(flag_status: str | None, candidate_gaps: set[str]) -> str:
    """按 flag 状态 + 关键 gap 判定 receiver 候选的可判定性分级。"""

    if flag_status == "exported" and not (candidate_gaps & _BIG_RECEIVER_GAPS):
        return "confirmed_exported_clean"
    if flag_status == "exported":
        return "confirmed_exported_gap"
    return "unresolved_flag"


def _dynamic_receiver_binding_candidates(
    rule_id: str,
    file: dict[str, Any],
    binding: dict[str, Any],
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    registration = binding.get("registration") or {}
    confirmed = [
        effect for effect in binding.get("confirmed_effects", [])
        if isinstance(effect, dict) and effect.get("verified") is True
    ]
    complete = bool(binding.get("binding_complete") and confirmed)
    selected_effects: list[dict[str, Any] | None] = confirmed if complete else [None]
    results = []
    for effect in selected_effects:
        level = "L2" if effect else "L1"
        result = _global_base(
            rule_id,
            file,
            level,
            manifest,
            "已确认外部 Receiver 注册到 onReceive 敏感副作用的完整静态链路"
            if effect else
            "动态 Receiver 注册点可能对其他应用可达，尚未闭合到确定副作用",
        )
        critical_gaps = [
            gap for gap in binding.get("coverage_gaps", [])
            if isinstance(gap, dict) and gap.get("critical") is True
        ]
        binding_view = {
            **binding,
            "effects": [effect] if effect and not effect.get("event") else [],
            "transitions": [effect] if effect and effect.get("event") else [],
            "confirmed_effects": [effect] if effect else [],
            "effect_path": [effect.get("path") or effect.get("method_path")] if effect else [],
        }
        result.update({
            "locations": [{
                "artifact": "code", "path": file["path"],
                "line": int(registration.get("line", 1)),
            }],
            "dynamic_receiver_status": binding.get("export_status") or binding.get("flag_status"),
            "receiver_binding": binding_view,
            "coverage_gaps": binding.get("coverage_gaps", []),
            "blocking_gaps": critical_gaps,
            "permission": binding.get("permission"),
            "authorization_status": binding.get("permission_status") or "unknown",
            "authorization_matrix": [{
                "entry": "registerReceiver",
                "operation": "dynamic_receiver_registration",
                "reachability": "reachable" if binding.get("externally_reachable") is True else "conditional",
                "effective_permission": binding.get("permission"),
                "authorization": {"status": binding.get("permission_status") or "unknown"},
                "protection": (binding.get("permission_policy") or {}).get("protection"),
                "provenance": (binding.get("permission_policy") or {}).get("provenance"),
                "blocking_gaps": critical_gaps,
            }],
            "reachability_status": "reachable" if binding.get("externally_reachable") is True else "conditional",
            "guard_status": "absent",
        })
        # R-1（2026-08-15，v3）：flag 分级三值——exported 需叠加"无关键 gap"
        # 才是真实可判定暴露面；供 funnel L1 预算按可判定性排序（R-2）。
        _flag_status = binding.get("export_status") or binding.get("flag_status")
        _candidate_gaps = {
            str(gap.get("code")) for gap in critical_gaps
            if isinstance(gap, dict)
        }
        result["receiver_flag_tier"] = _receiver_flag_tier(_flag_status, _candidate_gaps)
        result["platform_assumptions"].append(
            "显式导出 Receiver 注册路径"
            if binding.get("flag_status") == "exported" else
            "Receiver flag/permission 未完全解析"
            if binding.get("flag_status") == "unknown" else
            "旧式无 flag registerReceiver；外部可达性依赖 action/permission/API"
        )
        sink = dict((effect or {}).get("effect") or {}) if effect else None
        if sink is not None:
            sink["kind"] = effect.get("effect_kind") or sink.get("kind") or "sensitive_effect"
            sink["taxonomy"] = effect.get("effect_taxonomy") or sink.get("taxonomy")
        path = list((effect or {}).get("path") or [])
        if effect and not path:
            path = [{
                "text": f"registerReceiver → {binding.get('on_receive')} → {sink['kind']}",
                "status": "fact",
                "evidence_id": f"{registration.get('path')}:{registration.get('line')}",
                "method_path": effect.get("method_path") or [],
            }]
        chain = {
            "entry_method_id": binding.get("on_receive") or registration.get("method_id"),
            "entry_method_name": "onReceive" if binding.get("on_receive") else "registerReceiver",
            "source": {**registration, "status": registration.get("status", "fact")},
            "sink": sink,
            "path": path,
            "flow_kind": "receiver_binding" if effect else "receiver_exposure",
            "path_model": "receiver_binding_v1",
            "chain_scope": {
                "registration_method_id": registration.get("method_id"),
                "registration_ordinal": registration.get("ordinal"),
                "registration_path": registration.get("path"),
                "registration_line": registration.get("line"),
            },
        }
        result = chain_to_candidate(result, chain, source_kind="external_registration")
        result["dataflow_status"] = binding.get("dataflow_status", "not_proven") if effect else "not_proven"
        result["deterministic_chain_verified"] = bool(effect and not critical_gaps)
        result["impact_status"] = "statically_confirmed" if effect else "potential"
        if effect:
            result["operation_taxonomy"] = sink.get("taxonomy")
            result["review_priority"] = max(result.get("review_priority", 0), 85)
        results.append(result)
    return results


def _global_code_rule(
    rule_id: str,
    files: list[dict],
    manifest: dict,
    dynamic_receiver_scope: dict[str, Any] | None = None,
) -> list[dict]:
    """扫描不绑定清单组件的代码模式，并生成全局规则候选。"""

    results = []
    if rule_id == "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION":
        if dynamic_receiver_scope is None:
            for file in files:
                legacy_analyzer = DataFlowAnalyzer([file])
                bindings = [
                    _expand_dynamic_receiver_effects(legacy_analyzer, binding)
                    for binding in legacy_analyzer.dynamic_receiver_bindings(manifest)
                ]
                for exposure in _dynamic_receiver_exposures(file, manifest):
                    binding = next((
                        item for item in bindings
                        if item.get("registration", {}).get("line") == exposure["line"]
                    ), None)
                    if binding:
                        results.extend(_dynamic_receiver_binding_candidates(
                            rule_id, file, binding, manifest
                        ))
                    else:
                        results.extend(_dynamic_receiver_binding_candidates(
                            rule_id,
                            file,
                            {
                                "registration": {
                                    "path": file["path"], "line": exposure["line"],
                                    "text": "registerReceiver", "kind": "receiver_registration",
                                },
                                "reportable": True,
                                "externally_reachable": exposure["status"] == "exported",
                                "flag_status": exposure["status"],
                                "export_status": exposure["status"],
                                "permission_status": "unknown",
                                "coverage_gaps": [],
                                "confirmed_effects": [],
                                "binding_complete": False,
                            },
                            manifest,
                        ))
            return results
        analyzer = DataFlowAnalyzer(
            files,
            entry_method_ids=(dynamic_receiver_scope or {}).get("entry_method_ids"),
            scope_gaps=(dynamic_receiver_scope or {}).get("gaps"),
        )
        bindings = [
            _expand_dynamic_receiver_effects(analyzer, binding)
            for binding in analyzer.dynamic_receiver_bindings(manifest)
        ]
        files_by_path = {str(file.get("path") or ""): file for file in files}
        for binding in bindings:
            registration = binding.get("registration", {})
            if not binding.get("reportable"):
                continue
            file = files_by_path.get(str(registration.get("path") or ""))
            if file is None:
                continue
            results.extend(_dynamic_receiver_binding_candidates(
                rule_id, file, binding, manifest
            ))
        return results

    for file in files:
        code = file.get("content", "")
        if rule_id == "IMPLICIT_BROADCAST_SENSITIVE_DATA":
            flow = _implicit_broadcast_flow(file)
            if flow:
                result = _global_base(rule_id, file, "L2", manifest, "敏感数据进入未限定目标或接收权限的同一广播 Intent")
                result["sources"] = [_evidence(flow["source"], "sensitive_data")]
                result["sinks"] = [_evidence(flow["sink"], "implicit_broadcast")]
                result["propagation_paths"] = flow["path"]
                result["dataflow_status"] = "intraprocedural"
                sink_effective = bool((flow.get("sink") or {}).get("effect_verified"))
                if sink_effective:
                    result["deterministic_chain_verified"] = True
                    result["impact_status"] = "statically_confirmed"
                else:
                    # LocalBroadcastManager/EventBus 进程内分发（红线 9）：无跨进程效果，
                    # 不构成确定性闭链；保留候选让 AI 复核（funnel 判定 ai_required=True）。
                    result["impact_status"] = "potential"
                    result.setdefault("blocking_gaps", []).append({
                        "code": "EXFILTRATION_CHANNEL_ABSENT",
                        "critical": True,
                        "message": "Sink 为 LocalBroadcastManager/EventBus 进程内分发，无跨进程外溢通道",
                    })
                results.append(result)
        elif rule_id == "ORDERED_BROADCAST_UNRESTRICTED":
            ordered = re.search(r"sendOrderedBroadcast\s*\(", code)
            permission = _ordered_broadcast_has_permission(code)
            if ordered and not permission:
                result = _global_base(rule_id, file, "L1", manifest, "有序广播未识别接收权限限制")
                result["auxiliary"] = True
                results.append(result)
        elif rule_id in GLOBAL_CODE_RULES:
            # WebView/密码学族（§12.2 ②③）：按规则专属模式在可执行代码区域匹配，
            # 生成 L2 候选（需 AI 复核可利用性与危害，不走确定性闭链）。
            matched = _webview_crypto_match(rule_id, code, file)
            if matched:
                result = _global_base(rule_id, file, "L2", manifest, matched["description"])
                result["locations"] = [{
                    "artifact": "code", "path": file["path"], "line": matched["line"],
                }]
                result["sinks"] = [_evidence({
                    "path": file["path"], "line": matched["line"],
                    "text": matched["text"], "status": "fact",
                }, matched["sink_kind"])]
                results.append(result)
    return results


def _webview_crypto_match(rule_id: str, code: str, file: dict) -> dict | None:
    """WebView/密码学规则的单一匹配入口。

    两类匹配策略：
    - 方法调用类（FILE_ACCESS/UNIVERSAL/SSL/TRUST/VERIFIER）：先用 _sanitize_executable
      剔除注释与字符串字面量再匹配——注释里的假调用点、字符串拼装的方法名不构成真实调用点；
    - 字符串参数类（JS_BRIDGE 的桥名、WEAK_CIPHER 的算法名）：检测目标本身在字符串字面量
      内（addJavascriptInterface(obj, "Android") / Cipher.getInstance("AES/ECB/...")），
      用原始 code 匹配，但需先排除注释（sanitize 后做"注释定位"，原文匹配）。

    命中仅证明"调用点存在"，可利用性/危害由 AI 阶段判定（evidence_level=L2）。
    """

    def _line_at(offset: int) -> int:
        return code.count("\n", 0, offset) + 1

    sanitized = _sanitize_executable(code)

    if rule_id == "WEBVIEW_JS_BRIDGE_EXPOSED":
        # 桥名在字符串内：原文匹配 + 剔除注释（sanitize 后的空白位置对应注释）。
        pattern = re.compile(r"addJavascriptInterface\s*\(\s*[^,]+,\s*[\"']([^\"']+)[\"']")
        match = pattern.search(code)
        if match and not re.match(r"\s*//", code[:match.start()][code.rfind("\n") + 1:]):
            return {
                "line": _line_at(match.start()),
                "text": match.group(0)[:120],
                "description": "WebView.addJavascriptInterface 注入 JS 桥：任意加载到该 WebView 的"
                               "网页 JS 均可调用被注入对象的全部导出方法，若桥对象暴露敏感能力则构成"
                               "远程代码/数据访问面（JS 桥注入）。",
                "sink_kind": "js_bridge",
            }
        return None

    if rule_id == "WEBVIEW_FILE_ACCESS_ENABLED":
        pattern = re.compile(r"setAllowFileAccess\s*\(\s*true\s*\)|setAllowFileAccessFromFileURLs\s*\(\s*true\s*\)")
        match = pattern.search(sanitized)
        if match:
            return {
                "line": _line_at(match.start()),
                "text": pattern.search(code).group(0)[:120],
                "description": "WebView 显式启用 file:// 文件访问：加载的网页可读取应用私有文件"
                               "（setAllowFileAccess(true) 或 setAllowFileAccessFromFileURLs(true)），"
                               "本地文件数据泄露面。",
                "sink_kind": "file_access",
            }
        return None

    if rule_id == "WEBVIEW_UNIVERSAL_ACCESS_FROM_FILE":
        pattern = re.compile(r"setAllowUniversalAccessFromFileURLs\s*\(\s*true\s*\)")
        match = pattern.search(sanitized)
        if match:
            return {
                "line": _line_at(match.start()),
                "text": pattern.search(code).group(0)[:120],
                "description": "WebView.setAllowUniversalAccessFromFileURLs(true)：任意来源的网页均可"
                               "跨域访问 file:// 资源，file 域不再隔离，本地文件读取面扩大。",
                "sink_kind": "file_access",
            }
        return None

    if rule_id == "WEBVIEW_SSL_ERROR_IGNORED":
        pattern = re.compile(
            r"onReceivedSslError\s*\([^)]*\)\s*\{[^}]{0,800}?\w+\s*\.\s*proceed\s*\(",
            re.I | re.S,
        )
        match = pattern.search(sanitized)
        if match:
            return {
                "line": _line_at(match.start()),
                "text": pattern.search(code).group(0)[:160],
                "description": "onReceivedSslError 内调用 handler.proceed()：证书校验错误被放行，"
                               "中间人攻击者可注入任意内容（SSL 错误放行）。",
                "sink_kind": "ssl_bypass",
            }
        return None

    if rule_id == "WEBVIEW_EXTERNAL_CONTENT":
        js_enabled = re.compile(r"setJavaScriptEnabled\s*\(\s*true\s*\)").search(sanitized)
        external_load = re.compile(r"loadUrl\s*\(\s*[\"']https?://").search(code)
        if js_enabled and external_load:
            return {
                "line": _line_at(external_load.start()),
                "text": external_load.group(0)[:120],
                "description": "WebView 启用 JavaScript 且加载外部 http(s) URL：若页面内容可被攻击者"
                               "控制则构成反射型 XSS 攻击面（JS 可访问桥/本地资源）。",
                "sink_kind": "xss_surface",
            }
        return None

    if rule_id == "TRUST_MANAGER_ALL_ACCEPT":
        pattern = re.compile(
            r"checkServerTrusted\s*\([^)]*\)\s*\{([^{}]{0,400}?)\}",
            re.I | re.S,
        )
        match = pattern.search(sanitized)
        if match:
            body = match.group(1).strip()
            if not body or re.match(r"^\s*(?:/\*.*?\*/\s*)*$", body):
                return {
                    "line": _line_at(match.start()),
                    "text": pattern.search(code).group(0)[:160],
                    "description": "X509TrustManager.checkServerTrusted 实现为空/不抛异常：接受任意"
                                   "服务器证书，TLS 中间人攻击可完全绕过（TrustManager 空实现）。",
                    "sink_kind": "cert_bypass",
                }
        return None

    if rule_id == "HOSTNAME_VERIFIER_ALWAYS_TRUE":
        pattern = re.compile(
            r"verify\s*\([^)]*\)\s*\{\s*(?:return\s*\(?\s*true\s*\)?\s*;?)\s*\}",
            re.I | re.S,
        )
        match = pattern.search(sanitized)
        if match:
            return {
                "line": _line_at(match.start()),
                "text": pattern.search(code).group(0)[:160],
                "description": "HostnameVerifier.verify 恒真返回 true：主机名校验被绕过，"
                               "任何证书对任意主机名均通过（域名校验绕过）。",
                "sink_kind": "hostname_bypass",
            }
        return None

    if rule_id == "WEAK_CIPHER_ECB":
        # 算法名在字符串内：原文匹配，但需排除注释中的 Cipher.getInstance。
        pattern = re.compile(r"Cipher\s*\.\s*getInstance\s*\(\s*[\"']AES/ECB/", re.I)
        match = pattern.search(code)
        if match:
            return {
                "line": _line_at(match.start()),
                "text": match.group(0)[:120],
                "description": "Cipher.getInstance 使用 AES/ECB 模式：ECB 下相同明文块产生相同"
                               "密文块，泄露明文模式信息，可被模式分析攻击（弱加密模式）。",
                "sink_kind": "weak_cipher",
            }
        return None

    return None


# 组件生命周期入口由系统调用，索引中不存在 resolved 调用者——不视为死代码。
_COMPONENT_LIFECYCLE_ENTRIES = frozenset({
    "onCreate", "onNewIntent", "onStart", "onResume", "onPause", "onStop",
    "onDestroy", "onReceive", "onStartCommand", "onBind", "onRebind",
    "onUnbind", "query", "insert", "update", "delete", "openFile", "call",
    "onTransact", "handleMessage",
})
# S2（2026-08-16）：发送方可达性判定的 manifest 组件入口集合（窄于
# _COMPONENT_LIFECYCLE_ENTRIES——query/insert 等 Provider 入口不适用于广播发送方）。
_SENDER_REACHABILITY_ENTRY_METHODS = frozenset({
    "onCreate", "onNewIntent", "onReceive", "onStartCommand", "onBind", "onRebind", "main",
})
_STRING_LITERAL_RE = re.compile(r"[\"'][^\"']*[\"']")
# 编译期常量引用（JADX 伪代码实参形态）：以全大写段结尾的点分路径
# （AccountConstants.PREF_C_UID / PREF_MODE_LASTTIME）或单段全大写标识符。
# Android 惯例 static final 常量全大写命名；JADX 局部变量几乎总是小写（str/str2）。
# 末段非全大写（Constants.HomePageVersion.VersionType）无法静态确认，保守不认。
_CONSTANT_REF_RE = re.compile(r"(?:[A-Z][A-Za-z0-9_]*\.)*[A-Z][A-Z0-9_]+")


def _derive_app_package(manifest: dict[str, Any]) -> str:
    """从 manifest 组件名推导应用主包（出现最多的前两段前缀，如 com.mi.health）。"""

    counts: dict[str, int] = {}
    for component in manifest.get("components") or []:
        name = str(component.get("name") or "")
        if name.startswith("dynamic:"):
            name = name.split(":", 1)[1]
        segments = [segment for segment in name.split(".") if segment]
        if len(segments) >= 2:
            prefix = ".".join(segments[:2])
            counts[prefix] = counts.get(prefix, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def _sender_reachability_entry_ids(
    reader: "RuleIndexReader", manifest: dict[str, Any]
) -> set[str]:
    """收集应用主包内 manifest 组件入口方法 id（S2 反向可达目标）。"""

    app_package = _derive_app_package(manifest)
    if not app_package:
        return set()
    placeholders = ",".join("?" for _ in _SENDER_REACHABILITY_ENTRY_METHODS)
    rows = reader.db.execute(
        f"SELECT id FROM methods "
        f"WHERE name IN ({placeholders}) AND qualified_class LIKE ?",
        [*sorted(_SENDER_REACHABILITY_ENTRY_METHODS), f"{app_package}.%"],
    ).fetchall()
    return {str(row["id"]) for row in rows}


def _find_method_id(reader: "RuleIndexReader", path: str, method_name: str) -> str | None:
    """按文件路径 + 方法名定位方法 id（发送方方法定位，S2）。"""

    if not path or not method_name:
        return None
    row = reader.db.execute(
        "SELECT m.id FROM methods m JOIN files f ON f.id=m.file_id "
        "WHERE f.path = ? AND m.name = ? ORDER BY m.id LIMIT 1",
        (path, method_name),
    ).fetchone()
    return str(row["id"]) if row else None


def _attach_sink_argument_facts(
    candidates: list[dict[str, Any]], reader: "RuleIndexReader | None",
    manifest: dict[str, Any] | None = None,
) -> None:
    """为候选补齐 `call_site_exists` / `sink_argument_constant` 两项规则事实。

    P1-5 打通（2026-08-15）：`no_real_call_site` 反证依赖 `call_site_exists is False`
    （红线 13 死代码）；`constant_sink_argument` 反证依赖
    `sink_argument_constant is True`（sink 参数为编译期常量，攻击者不可控）。

    判定规则（保守，宁可漏判不可误判——误判会被决策层采信为 ai_false_positive）：
    - sink 方法是组件生命周期/框架回调入口 → `call_site_exists=True`（系统调用，
      索引中不存在 resolved 调用者是正常形态，不是死代码）；
    - 其余方法：全索引无任何 resolved 调用者且**无解析失败的同名调用点**
      → `call_site_exists=False`（真死代码）；存在解析失败的同名调用点
      （pending/ambiguous，receiver 类型匹配）→ `call_site_exists=True`
      （resolve 失败 ≠ 无调用者，可能是重载/泛型/Receiver 推断不足）；
    - 有 resolved 调用者且**无任何解析失败调用点**时：所有调用点的数据实参
      （排除首个"上下文"实参）**全部为字符串字面量** → `sink_argument_constant=True`；
      存在解析失败调用点、或任一数据实参是变量/常量引用/表达式
      → False（不采信——pending 调用点里的变量实参可能被漏统计）。
    """

    if reader is None:
        return
    entry_ids = _sender_reachability_entry_ids(reader, manifest or {})
    app_package = _derive_app_package(manifest or {})
    for candidate in candidates:
        # S2（2026-08-16）：发送方可达性——隐式广播候选的发送方方法若无任何
        # manifest 入口反向可达，则区分 SDK 死代码与真实路径（V-04 BLE 实证）。
        if candidate.get("rule_id") == "IMPLICIT_BROADCAST_SENSITIVE_DATA":
            broadcast_sink = next((
                item for item in candidate.get("sinks") or []
                if isinstance(item, dict) and item.get("kind") == "implicit_broadcast"
            ), None)
            if broadcast_sink:
                sender_id = _find_method_id(
                    reader,
                    str(broadcast_sink.get("path") or ""),
                    str(broadcast_sink.get("method_name") or ""),
                )
                if sender_id:
                    reachable = reader.reaches_entry_method(sender_id, entry_ids)
                    candidate["sender_reachable"] = reachable
                    if not reachable and app_package:
                        path = str(broadcast_sink.get("path") or "")
                        if not path.startswith(app_package.replace(".", "/")):
                            candidate["sdk_dead_code"] = True
        sink = next((
            item for item in candidate.get("sinks") or []
            if isinstance(item, dict) and item.get("method_id")
        ), None)
        method_id = str(sink.get("method_id") or "") if sink else ""
        if not method_id:
            continue
        short = method_id.rsplit("#", 1)[-1]
        method_name = short.split(":", 1)[0].rsplit(".", 1)[-1]
        # 目标类名：method_id 形如 `com/x/Y.java#Y.foo:12`，取 # 前路径的
        # 类文件主干（去掉 .java）用于 receiver 类型匹配（保守子串）。
        class_name = method_id.rsplit("/", 1)[-1]
        class_name = class_name.split(".java", 1)[0].split("#", 1)[0]
        if method_name in _COMPONENT_LIFECYCLE_ENTRIES:
            candidate["call_site_exists"] = True
            continue
        callers, has_unresolved = reader.sink_callers(
            method_id, class_name=class_name, method_name=method_name
        )
        if not callers:
            # 无 resolved 调用者：仅当也不存在解析失败的同名调用点才判死代码
            # （resolve 失败 ≠ 无调用者——重载/泛型/Receiver 推断不足时
            # 调用点存在但解析不成功，判死代码会被采信为 no_real_call_site）。
            candidate["call_site_exists"] = has_unresolved
            continue
        candidate["call_site_exists"] = True
        if has_unresolved:
            # 存在解析失败的调用点：其实参可能含变量（pending 调用点里的
            # new Gson().toJson(...)/cityName/str 等），不得据此判常量。
            candidate["sink_argument_constant"] = False
            continue
        candidate["sink_argument_constant"] = all(
            _call_args_literal_except_context(args) for args in callers
        )


# 首个实参的"上下文形态"：this/context/ctx/App 实例/getContext() 等。识别后从
# 数据参数中排除（PreferenceUtil(Context, key) 形态）；单数据参数方法
# （doRemove(key)）无 context 首参时全部实参参与常量性判定。
_CONTEXT_ARG_RE = re.compile(
    r"(?:this|super|mContext|context|ctx|getContext\(\)"
    r"|[\w$]+App\.instance|[\w$.]*(?:Activity|Context|Service|Application))"
)


def _call_args_literal_except_context(args: list[str]) -> bool:
    """单个调用点的实参常量性：排除上下文首参后，数据实参全部为
    字符串字面量或编译期常量引用。"""

    if not args:
        return False
    start = 1 if _CONTEXT_ARG_RE.fullmatch(str(args[0]).strip()) else 0
    data_args = args[start:]
    if not data_args:
        return False
    for arg in data_args:
        stripped = str(arg).strip()
        if _STRING_LITERAL_RE.fullmatch(stripped) or _CONSTANT_REF_RE.fullmatch(stripped):
            continue
        return False
    return True


def _base(rule_id: str, component: dict, level: str, files: list[dict], manifest: dict, description: str) -> dict:
    """创建字段完整、尚未附加具体 Source/Sink 的候选结果。"""
    kind, _, severity = RULE_META[rule_id]
    locations = [{"artifact": "manifest", "path": "AndroidManifest.xml", "line": None}]
    if files:
        locations.append({"artifact": "code", "path": files[0]["path"], "line": 1})
    authorization = _authorization(component, rule_id, manifest)
    return {
        "rule_id": rule_id,
        "rule_version": "1.0.0",
        "component": kind,
        "component_name": component.get("name"),
        "title": rule_id.replace("_", " ").title(),
        "description": description,
        "severity_hint": severity,
        "severity_reason": [],
        "impact_scope": [],
        "attacker_prerequisites": ["普通第三方应用"] if component.get("exported") == "true" else [],
        "user_interaction": "unknown",
        "severity_version": "1.0.0",
        "confidence_tier": "high" if level == "L1" else "medium",
        "evidence_level": level,
        "analysis_status": "rule_only",
        "dataflow_status": "not_applicable" if level == "L1" else "not_proven",
        "authorization_status": authorization["status"],
        "authorization_matrix": authorization["rows"],
        "impact_status": "potential",
        "deterministic_chain_verified": False,
        "review_priority": _review_priority(rule_id, component, files),
        "reachability_status": "reachable" if component.get("exported") == "true" else "conditional",
        "guard_status": "unknown",
        "platform_assumptions": _platform_assumptions(manifest),
        "locations": locations,
        "entry_points": [component.get("name")],
        "sources": [],
        "sinks": [],
        "sanitizers_or_guards": [],
        "propagation_paths": [],
        "context_requests": [],
        "blocking_gaps": list(authorization["blocking_gaps"]),
        "limitations": ["DEX 反编译伪源码可能失真；静态结果未动态验证"],
        "permission": component.get("permission"),
    }


def _platform_assumptions(manifest: dict) -> list[str]:
    """输出候选可直接引用的平台事实，避免 AI 反复扩片索要 Manifest 配置。

    AI 深度分析常需要 targetSdk、minSdk、debuggable 等信息判定动态 Receiver 导出默认值、
    Provider 默认导出和 debug 分支可达性。这些事实只存在于 Manifest，不在代码索引中；
    若不随候选下发，模型会持续请求 method/class 扩片却永远解析不到，最终以
    CONTEXT_EXPANSION_STALLED 终止。缺失值显式标记为 unknown，不得省略键。
    """

    def _fact(key: str, value: object) -> str:
        return f"{key}={value if value not in (None, '') else 'unknown'}"

    return [
        _fact("analysis_platform_api", manifest.get("analysis_platform_api", 36)),
        _fact("target_sdk", manifest.get("target_sdk")),
        _fact("min_sdk", manifest.get("min_sdk")),
        _fact("compile_sdk_version", manifest.get("compile_sdk_version")),
        _fact("debuggable", manifest.get("debuggable", False)),
        _fact("allow_backup", manifest.get("allow_backup")),
        _fact("uses_cleartext_traffic", manifest.get("uses_cleartext_traffic")),
    ]



_TRACE_RECORD_CAP = 200


def _cap_records(records: Any) -> list[dict[str, Any]]:
    """截断组件级 trace 明细并显式标注截断，避免候选输出按链路数放大。"""

    if not isinstance(records, list):
        return []
    if len(records) <= _TRACE_RECORD_CAP:
        return list(records)
    kept = list(records[:_TRACE_RECORD_CAP])
    kept.append({
        "trace_truncated": True,
        "total_records": len(records),
        "retained_records": _TRACE_RECORD_CAP,
    })
    return kept


def _summarize_reaching_definitions(records: Any) -> dict[str, Any]:
    """把逐条 reaching definition 压缩为可判定摘要，保留 kill 与 state 分布。"""

    if not isinstance(records, list):
        return {"total": 0, "values": 0, "killed": 0, "states": {}, "samples": []}
    states: dict[str, int] = {}
    values: set[str] = set()
    killed = 0
    for item in records:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1
        value = item.get("value")
        if isinstance(value, str):
            values.add(value)
        if item.get("killed_version") is not None:
            killed += 1
    return {
        "total": len(records),
        "values": len(values),
        "killed": killed,
        "states": states,
        "samples": list(records[:20]),
    }


def _summarize_method_summaries(summaries: Any) -> dict[str, Any]:
    """只保留方法摘要的规模与键名，完整摘要体不随候选外发。"""

    if not isinstance(summaries, dict):
        return {"total": 0, "methods": []}
    keys = [str(key) for key in summaries]
    return {
        "total": len(keys),
        "methods": sorted(keys)[:_TRACE_RECORD_CAP],
        "methods_truncated": len(keys) > _TRACE_RECORD_CAP,
    }

def _review_priority(rule_id: str, component: dict, files: list[dict]) -> int:
    """按高价值跨应用能力为攻击面候选分配复核优先级，不改变风险等级。"""

    code = "\n".join(file.get("content", "") for file in files)
    if re.search(r"(?:extends\s+[\w.$]*(?:Stub|Binder)|\bonTransact\s*\(|\bIInterface\b)", code):
        return 100
    if component.get("kind") == "provider" and re.search(r"\b(?:openFile|delete|call)\s*\(", code):
        return 95
    if component.get("kind") == "service" and re.search(r"\bonBind\s*\(", code):
        return 90
    if component.get("kind") == "service" and re.search(r"(?:Location|Sensor|startForeground|Sport|Workout|Account)", code, re.I):
        return 85
    if component.get("kind") == "provider" and re.search(r"\b(?:MatrixCursor|rawQuery|query)\b", code):
        return 75
    if component.get("kind") == "activity" and re.search(r"\b(?:WebView|loadUrl)\b", code):
        return 70
    return 40 if rule_id.endswith("EXPORTED_NO_PERMISSION") else 50


def _global_base(rule_id: str, file: dict, level: str, manifest: dict, description: str) -> dict:
    component = {"name": f"dynamic:{file['path']}", "exported": "true", "permission": None}
    result = _base(rule_id, component, level, [file], manifest, description)
    result["locations"] = [{"artifact": "code", "path": file["path"], "line": 1}]
    return result


def _manifest_fact_candidates(rule_id: str, manifest: dict) -> list[dict]:
    """本地存储/配置族（§12）：纯 manifest 事实，确定性生成 L1 候选。

    - DEBUGGABLE_IN_PRODUCTION: debuggable=true 的生产配置（任意调试器可附加）。
    - ALLOW_BACKUP_ENABLED: allowBackup=true 且 targetSdk>=23（adb backup 可提取数据）。
    - CLEARTEXT_TRAFFIC_ALLOWED: usesCleartextTraffic=true 且 targetSdk>=28（明文流量显式放开）。

    条件不满足时返回空列表；这些事实是 F2 一期已解析的确定性字段，不依赖代码索引。
    """

    component = {"name": "application", "exported": "false", "permission": None}
    try:
        target_sdk = int(manifest.get("target_sdk") or 0)
    except (TypeError, ValueError):
        target_sdk = 0

    if rule_id == "DEBUGGABLE_IN_PRODUCTION":
        if manifest.get("debuggable") is not True:
            return []
        result = _base(
            rule_id, component, "L1", [], manifest,
            "应用以 debuggable=true 发布：任意调试器可附加进程、读取内存与文件、绕过校验，"
            "属生产环境高危配置（Android ApplicationInfo.FLAG_DEBUGGABLE）。",
        )
        result["severity_hint"] = "high"
        return [result]

    if rule_id == "ALLOW_BACKUP_ENABLED":
        if manifest.get("allow_backup") is not True or target_sdk < 23:
            return []
        result = _base(
            rule_id, component, "L1", [], manifest,
            "android:allowBackup=true 且 targetSdk>=23：adb backup 可提取应用私有数据"
            "（SharedPreferences/数据库/文件），本地数据泄露面。",
        )
        return [result]

    if rule_id == "CLEARTEXT_TRAFFIC_ALLOWED":
        if manifest.get("uses_cleartext_traffic") is not True or target_sdk < 28:
            return []
        result = _base(
            rule_id, component, "L1", [], manifest,
            "android:usesCleartextTraffic=true 且 targetSdk>=28：targetSdk>=28 默认禁止明文流量，"
            "此处显式放开，HTTPS 降级/中间人攻击面扩大。",
        )
        return [result]

    return []


def _component_files(component: dict, files: list[dict]) -> list[dict]:
    simple = component.get("name", "").split(".")[-1]
    return [file for file in files if simple and (simple in file.get("path", "") or re.search(rf"\b{re.escape(simple)}\b", file.get("content", "")))]


def _first_match(files: list[dict], pattern: re.Pattern) -> dict | None:
    """仅在方法可执行区域查找首个匹配，排除 import、注释、字符串和声明。"""

    for file in files:
        for region in _executable_regions(file):
            match = pattern.search(region["sanitized"])
            if match:
                line = region["start_line"] + region["sanitized"].count("\n", 0, match.start())
                original = region["original"][match.start():match.end()]
                return {
                    "path": file["path"],
                    "line": line,
                    "text": original[:200],
                    "kind": "pattern",
                    "method_name": region.get("method_name"),
                }
    return None


def _first_sink(files: list[dict]) -> dict | None:
    """返回首个真实方法体中的敏感调用候选。"""

    for kind, pattern in SINK_PATTERNS.items():
        match = _first_match(files, pattern)
        if match:
            match["kind"] = kind
            return match
    return None


def _method_body_text(method: dict) -> str:
    """屏蔽方法声明前缀并保留同一行内的真实方法体及行号布局。"""

    body = method.get("content", "").splitlines(keepends=True)
    if body:
        opening_brace = body[0].find("{")
        if opening_brace >= 0:
            body[0] = " " * (opening_brace + 1) + body[0][opening_brace + 1:]
        else:
            body[0] = "\n" if body[0].endswith("\n") else ""
    return "".join(body)


def _executable_regions(file: dict) -> list[dict]:
    """生成保持换行位置的可执行方法区域，便于证据行号回查。"""

    methods = file.get("methods", [])
    if methods:
        regions = []
        for method in methods:
            executable = _method_body_text(method)
            regions.append({
                "original": executable,
                "sanitized": _sanitize_executable(executable),
                "start_line": int(method.get("start_line", 1)),
                "method_name": method.get("name"),
            })
        return regions
    content = file.get("content", "")
    filtered = "\n".join(
        "" if line.lstrip().startswith(("package ", "import ")) else line
        for line in content.splitlines()
    )
    return [{"original": filtered, "sanitized": _sanitize_executable(filtered), "start_line": 1}]


def _sanitize_executable(content: str) -> str:
    """以空格替换注释和字符串，同时保留字符数及换行位置。"""

    result = list(content)
    index = 0
    quote = None
    block_comment = False
    while index < len(content):
        if block_comment:
            if content.startswith("*/", index):
                result[index:index + 2] = "  "
                block_comment = False
                index += 2
            else:
                if content[index] != "\n":
                    result[index] = " "
                index += 1
            continue
        if quote:
            if content[index] == "\\":
                if content[index] != "\n":
                    result[index] = " "
                if index + 1 < len(content) and content[index + 1] != "\n":
                    result[index + 1] = " "
                index += 2
                continue
            if content[index] == quote:
                result[index] = " "
                quote = None
            elif content[index] != "\n":
                result[index] = " "
            index += 1
            continue
        if content.startswith("//", index):
            while index < len(content) and content[index] != "\n":
                result[index] = " "
                index += 1
            continue
        if content.startswith("/*", index):
            result[index:index + 2] = "  "
            block_comment = True
            index += 2
            continue
        if content[index] in {'"', "'"}:
            quote = content[index]
            result[index] = " "
        index += 1
    return "".join(result)


def _match(file: dict, match: re.Match, kind: str) -> dict:
    content = file.get("content", "")
    return {"path": file["path"], "line": content.count("\n", 0, match.start()) + 1, "text": match.group(0)[:200], "kind": kind}


def _synthetic(files: list[dict], text: str) -> dict | None:
    """把推断 Source 锚定到真实入口参数/方法，而不是伪造文件第 1 行。"""

    for file in files:
        for method in file.get("methods", []):
            for parameter in method.get("structured_parameters", []) or []:
                if parameter.get("source_kind"):
                    return {
                        "path": file["path"],
                        "line": int(method.get("start_line", 1)),
                        "text": str(parameter.get("name") or text),
                        "kind": "external_input",
                        "method_name": method.get("name"),
                        "source_kind": parameter.get("source_kind"),
                        "status": "fact",
                    }
    for file in files:
        if file.get("methods"):
            method = file["methods"][0]
            return {
                "path": file["path"], "line": int(method.get("start_line", 1)),
                "text": text, "kind": "external_input", "method_name": method.get("name"),
                "status": "inferred",
            }
    for file in files:
        content = str(file.get("content") or "")
        match = re.search(r"\b(?:query|insert|update|delete|openFile|call|applyBatch)\s*\(", content)
        if match:
            return {
                "path": file["path"], "line": content.count("\n", 0, match.start()) + 1,
                "text": text, "kind": "external_input", "status": "inferred",
            }
    return None


def _evidence(match: dict, kind: str) -> dict:
    evidence = {
        field: match[field]
        for field in _CHAIN_EVIDENCE_FIELDS
        if match.get(field) is not None
    }
    evidence.update({
        "kind": kind,
        "text": match.get("text", kind),
        "path": match["path"],
        "line": match["line"],
        "status": match.get("status", "fact"),
        "evidence_id": match.get("evidence_id") or f"{match['path']}:{match['line']}",
    })
    return evidence


def _only_protected_system_actions(component: dict) -> bool:
    facts = component.get("broadcast_action_authorization", [])
    return bool(facts) and all(item.get("status") == "protected" for item in facts)
