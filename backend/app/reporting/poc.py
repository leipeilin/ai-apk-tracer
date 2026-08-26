"""PoC 骨架构造（零可执行产物——方案 §2 PoCSkeleton）。

按 finding 的规则/组件类型选择骨架形态；命令全部为占位符文本
（对齐 findings/report.py 的 _poc_guide/_adb_extra_template 先例但
明确降级为"骨架文本"——不产出任何可执行文件）。
"""

from __future__ import annotations

from typing import Any

from app.reporting.models import PocKind, PoCSkeleton

# finding 组件域全集（2026-08-26 审查 R-3：真实产物实测 activity/crypto/
# manifest/provider/receiver/service/webview + 入口域兜底——report_quality
# 与本模块共享单一常量防口径漂移）
FINDING_COMPONENT_KINDS: frozenset[str] = frozenset({
    "activity", "service", "receiver", "provider", "webview",
    "crypto", "manifest", "other", "binder", "webview_bridge",
})

# rule_id 关键词 → 骨架类型（确定性映射；未命中按组件类型兜底）
_RULE_KIND_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("BINDER",), "binder_transaction"),
    (("PROVIDER",), "provider_query"),
    (("RECEIVER", "BROADCAST"), "broadcast"),
    (("ACTIVITY", "INTENT", "ROUTE"), "intent"),
    (("URI", "DEEP_LINK"), "uri"),
)

_NON_EXECUTABLE_NOTES = [
    "本骨架仅为验证步骤说明，不包含任何可执行文件；命令中的占位符（<PACKAGE>/<ACTION>/<EXTRA_KEY> 等）需按目标环境替换",
    "仅在获得授权的测试设备上执行验证",
]


def _skeleton_kind(finding: dict[str, Any]) -> PocKind:
    rule_id = str(finding.get("rule_id") or "")
    for keywords, kind in _RULE_KIND_HINTS:
        if any(keyword in rule_id.upper() for keyword in keywords):
            return kind
    component = str(finding.get("component") or "").lower()
    return {
        "activity": "intent", "service": "binder_transaction",
        "receiver": "broadcast", "provider": "provider_query",
    }.get(component, "intent")


def build_poc_skeleton(finding: dict[str, Any]) -> PoCSkeleton:
    """finding → PoC 骨架（确定性——无 AI 参与，executable_files_created 恒空）。"""

    component = str(finding.get("component") or "other")
    kind = _skeleton_kind(finding)
    package = "<PACKAGE>"
    entry_points = finding.get("entry_points") or []
    entry_hint = str(entry_points[0]) if entry_points else str(
        finding.get("entry_method_id") or "<ENTRY>")

    if kind == "binder_transaction":
        transactions = finding.get("binder_transactions") or []
        codes = [str(t.get("code")) for t in transactions if isinstance(t, dict) and t.get("code") is not None]
        code_hint = "/".join(codes) if codes else "<CODE>"
        steps = [
            "确认目标组件 exported 且调用无需签名级权限（攻击面事实见报告 deterministic 部分）",
            f"构造测试 APK：bindService 绑定 {entry_hint} 所在组件",
            f"通过 Binder.transact 发起事务（code={code_hint}），观察返回数据",
        ]
        commands = [
            "# Binder 事务无法用 ADB 直接构造——需测试 APK（骨架说明，非可执行命令）",
            f"# bind {package} 中的目标 service → transact(code={code_hint}, data=<EXTRA_KEY>)",
        ]
        notes = ["Binder 调用需要客户端代码承载——ADB 仅能用于组件触发类验证（沿确定性报告先例）", *_NON_EXECUTABLE_NOTES]
    elif kind == "provider_query":
        # 评审 R-10：authorities 占位符化——骨架命令不得混入真实可执行值
        steps = [
            "确认 provider exported 且未受签名级权限保护",
            "以外部应用身份 query 目标 provider（构造 content URI——authority 见报告 deterministic 投影）",
        ]
        commands = ["adb shell content query --uri content://<AUTHORITY>/<PATH>"]
        notes = list(_NON_EXECUTABLE_NOTES)
    elif kind == "broadcast":
        steps = [
            "确认 receiver exported（manifest 声明或动态注册）",
            f"构造指向 {entry_hint} 的显式/隐式 Intent 广播",
        ]
        commands = [
            f"adb shell am broadcast -a <ACTION> -n {package}/<RECEIVER_CLASS> --es <EXTRA_KEY> <VALUE>",
        ]
        notes = list(_NON_EXECUTABLE_NOTES)
    elif kind == "uri":
        steps = [
            "构造携带目标 URI 的 Intent（深链）",
            f"触发 {entry_hint} 解析该 URI",
        ]
        commands = [
            f"adb shell am start -a android.intent.action.VIEW -d \"<URI_SCHEME>://<PATH>\" {package}",
        ]
        notes = list(_NON_EXECUTABLE_NOTES)
    else:  # intent
        steps = [
            "确认 activity exported 且 intent-filter 可从外部触发",
            f"构造携带攻击载荷 extras 的 Intent 指向 {entry_hint}",
            "观察敏感操作执行（日志/返回数据/副作用）",
        ]
        commands = [
            f"adb shell am start -n {package}/<ACTIVITY_CLASS> --es <EXTRA_KEY> <VALUE>",
        ]
        notes = list(_NON_EXECUTABLE_NOTES)

    return PoCSkeleton(
        component_kind=component,
        kind=kind,
        steps=steps,
        command_skeleton=commands,
        notes=notes,
        executable_files_created=[],
    )
