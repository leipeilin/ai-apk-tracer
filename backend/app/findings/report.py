"""将已聚合发现项转换为受证据约束的 Markdown 报告。"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from app.findings.severity import SEVERITY_LABELS
from app.shared.errors import ConflictError

IMPACT_BY_SINK_KIND = {
    "persistent_state_write": {
        "capability": "将外部可控数据写入应用持久化状态（SharedPreferences、DataStore 或等价存储）",
        "consequence": "后续业务读取该状态时，可能改变环境、渠道、页面展示或业务流程；实际可控键和值域取决于输入校验",
    },
    "database_mutation": {
        "capability": "以外部输入驱动数据库新增、更新或删除操作",
        "consequence": "可能破坏应用数据完整性、修改业务状态或造成拒绝服务",
    },
    "content_mutation": {
        "capability": "通过 ContentResolver 或 Provider 写入口修改目标应用数据",
        "consequence": "可能造成未授权数据修改、删除或业务状态污染",
    },
    "file_mutation": {
        "capability": "以外部路径或参数驱动文件写入、覆盖、重命名或创建",
        "consequence": "可能破坏配置或缓存完整性；影响范围受 Provider 根目录和路径校验约束",
    },
    "file_write": {
        "capability": "通过外部输入触发文件写入或覆盖",
        "consequence": "可能修改目标应用可访问文件；具体范围取决于路径映射、模式和权限",
    },
    "file_delete": {
        "capability": "通过外部输入删除目标应用可访问文件",
        "consequence": "可能造成配置、缓存或业务文件丢失并触发拒绝服务",
    },
    "file_open": {
        "capability": "根据外部 URI 或路径打开文件描述符",
        "consequence": "可能形成未授权文件读取或写入；实际能力取决于打开模式和路径边界",
    },
    "file_read": {
        "capability": "读取并向调用方返回目标应用可访问文件",
        "consequence": "可能泄露配置、日志、缓存或业务文件内容",
    },
    "file": {
        "capability": "将外部 URI 或路径映射到本地文件",
        "consequence": "若路径边界或调用权限不足，可能导致越权文件访问",
    },
    "sensitive_query_result": {
        "capability": "通过 Provider 查询返回设备、账户或业务敏感字段",
        "consequence": "可能造成敏感数据跨应用泄露和用户/设备关联",
    },
    "data_disclosure": {
        "capability": "把敏感数据发送到调用方、广播、网络或可观察通道",
        "consequence": "可能泄露账户、设备、位置、连接状态或业务数据",
    },
    "implicit_broadcast": {
        "capability": "通过未限定目标或接收权限的广播发送数据",
        "consequence": "其他应用注册对应 Receiver 后可能接收广播内容或观察业务事件",
    },
    "component_launch": {
        "capability": "使用外部可控 Intent 启动应用内部组件",
        "consequence": "可能绕过正常导航、打开受限页面、传递恶意参数或触发内部副作用",
    },
    "ui_navigation": {
        "capability": "以外部参数控制页面、Fragment 或路由跳转",
        "consequence": "可能绕过正常入口、造成界面欺骗或触发内部页面逻辑",
    },
    "fragment_reflection": {
        "capability": "以外部类名实例化并挂载内部 Fragment",
        "consequence": "可能暴露内部页面或把攻击者参数传入 Fragment；具体影响取决于下游校验",
    },
    "location_sensor_collection": {
        "capability": "触发位置、运动或传感器采集流程",
        "consequence": "可能造成隐私风险、运动状态干扰、前台通知和额外耗电",
    },
    "connection_session_control": {
        "capability": "控制服务、连接或运动会话的启动、暂停、恢复或终止",
        "consequence": "可能干扰合法会话、破坏记录完整性或造成拒绝服务",
    },
    "device_protocol_output": {
        "capability": "向蓝牙、NFC、USB 或其他设备协议通道发送数据",
        "consequence": "可能触发设备操作、污染协议状态或干扰已连接设备",
    },
    "callback_event_injection": {
        "capability": "向回调、监听器或事件总线注入伪造状态",
        "consequence": "可能欺骗合法客户端、污染业务状态或触发后续处理",
    },
    "binder_sensitive_api": {
        "capability": "从普通第三方 UID 获取远程 Binder 并调用敏感 transaction",
        "consequence": "可能读取账户/设备数据、注册持续回调或执行服务端控制能力",
    },
    "unknown_effect": {
        "capability": "外部输入到达尚未准确分类的调用点",
        "consequence": "当前证据不足以确定实际安全影响，应先人工确认 Sink 语义",
    },
}

STATUS_LABELS = {
    "ai_candidate": "自动候选（旧状态）",
    "pending_ai": "待AI复核",
    "pending_manual": "待人工复核",
    "ai_false_positive": "AI确认误报",
    "manual_false_positive": "人工确认误报",
    "confirmed": "已确认",
    # 兼容旧数据
    "pending": "待人工复核",
    "false_positive": "人工确认误报",
}

GAP_DESCRIPTIONS = {
    "SYMBOL_TARGET_AMBIGUOUS": "存在同名方法无法唯一解析，相关调用链可能不准确",
    "JADX_PARTIAL_DECOMPILATION": "JADX 反编译部分失败，部分代码可能缺失",
    "JADX_COVERAGE_GAP": "JADX 反编译覆盖不完整",
    "INDEX_FILES_SKIPPED": "代码索引跳过部分文件",
    "RULE_PRESCAN_PARTIAL": "规则预筛选部分失败",
    "AI_ANALYSIS_FAILED": "AI 分析失败",
    "AI_ANALYSIS_SKIPPED": "AI 分析被跳过",
    "AI_ANALYSIS_INCOMPLETE": "AI 分析未完成",
    "AI_MAX_ROUNDS_REACHED": "AI 分析达到最大扩片轮数",
    "CONTEXT_EXPANSION_STALLED": "模型请求未解析到新的索引上下文，深度分析自然终止",
    "GUARD_SINK_METHOD_UNRESOLVED": "Guard 检查的 Sink 方法未解析",
    "GUARD_PATH_UNRESOLVED": "Guard 检查路径未解析",
    "GUARD_COVERAGE_UNPROVEN": "Guard 覆盖范围未证明",
    "EXPORTED_COMPONENT_WITHOUT_PROTECTION": "导出组件未受保护",
    "CALLER_IDENTITY_NOT_VERIFIED": "调用者身份未验证",
    "INTENT_FLAG_GUARD_BYPASSABLE": "Intent 标志位 Guard 可绕过",
    "COMPONENT_TARGET_MISMATCH": "组件目标不匹配",
    "EVIDENCE_LOCATION_NOT_FOUND": "证据位置未找到",
    "EVIDENCE_SOURCE_NOT_FOUND": "证据 Source 未找到",
    "EVIDENCE_SINK_NOT_FOUND": "证据 Sink 未找到",
    "AI_EVIDENCE_REF_INVALID": "AI 证据引用无效",
    "AI_BLOCKING_GAP_INVALID": "AI 阻断条件格式无效",
    "LEGACY_FLOW_FALLBACK": "使用旧版数据流回退分析，精度有限",
    "EFFECT_TAXONOMY_UNKNOWN": "敏感操作分类未知，无法确定实际影响",
    "RECEIVER_EXPORTED_UNPROTECTED": "动态 Receiver 已导出且未受保护",
    "DYNAMIC_RECEIVER_TARGET_UNRESOLVED": "动态 Receiver 目标未解析",
    "BINDER_TRANSACTION_UNRESOLVED": "Binder 事务未解析",
    "PROVIDER_AUTHORITY_CONFLICT": "Provider authority 冲突",
    "PROVIDER_PATH_TRAVERSAL": "Provider 路径穿越风险",
    "PROVIDER_URI_GRANT": "Provider URI 授权",
    "ANALYSIS_GAP": "分析覆盖缺口",
}


def _format_gaps(gaps: list[Any]) -> str:
    """按 code 聚合去重，显示计数和中文说明。"""

    aggregated: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for gap in gaps:
        if not isinstance(gap, dict):
            raw = str(gap)
            # Long descriptive strings become message, not code
            if len(raw) > 30 or (raw and ord(raw[0]) >= 0x4e00):
                code = "ANALYSIS_GAP"
                message = raw
            else:
                code = raw
                message = ""
            if code not in aggregated:
                aggregated[code] = {"count": 0, "message": message}
            aggregated[code]["count"] += 1
            if not aggregated[code]["message"] and message:
                aggregated[code]["message"] = message
            continue
        raw_code = gap.get("code", "ANALYSIS_GAP")
        raw_message = gap.get("message", "")
        # If code is a long sentence (not a standard code), use it as the message
        is_descriptive = (
            len(raw_code) > 40
            or (raw_code and ord(raw_code[0]) >= 0x4e00)
            or " " in raw_code
        )
        if is_descriptive:
            code = "ANALYSIS_GAP"
            message = raw_code
        else:
            code = raw_code
            message = raw_message
        if code not in aggregated:
            aggregated[code] = {
                "count": 0,
                "message": message,
                "critical": gap.get("critical", False),
            }
        aggregated[code]["count"] += 1
        if not aggregated[code]["message"] and message:
            aggregated[code]["message"] = message
        if gap.get("critical"):
            aggregated[code]["critical"] = True
    lines = []
    for code, info in aggregated.items():
        desc = info["message"] or GAP_DESCRIPTIONS.get(code, "")
        if not desc:
            desc = "未提供说明"
        count = info["count"]
        if count > 1:
            lines.append(f"- {code}（{count} 处）：{desc}")
        else:
            lines.append(f"- {code}：{desc}")
    return "\n".join(lines)



def _impact_details(finding: dict[str, Any]) -> dict[str, Any]:
    """根据已回查 Sink 生成具体但不夸大的危害说明。"""

    sink_kinds = []
    capabilities = []
    consequences = []
    evidence = []
    for sink in finding.get("sinks", []):
        if not isinstance(sink, dict):
            continue
        kind = str(sink.get("kind") or sink.get("taxonomy") or "unknown_effect")
        if kind not in sink_kinds:
            sink_kinds.append(kind)
        detail = IMPACT_BY_SINK_KIND.get(kind, IMPACT_BY_SINK_KIND["unknown_effect"])
        if detail["capability"] not in capabilities:
            capabilities.append(detail["capability"])
        if detail["consequence"] not in consequences:
            consequences.append(detail["consequence"])
        location = f"{sink.get('path', '未知文件')}:{sink.get('line', '未知行')}"
        evidence.append(f"{kind} @ {location}：{sink.get('text') or '已回查敏感调用'}")
    if not capabilities:
        capabilities.append("当前仅确认外部输入到达候选敏感操作")
        consequences.append("实际危害仍需人工确认 Sink 语义和运行时前置条件")
    dynamic_passed = finding.get("dynamic_validation_status") == "passed"
    review_confirmed = finding.get("review_status") == "confirmed"
    if dynamic_passed:
        confirmed = "已记录动态验证成功；具体结果以测试日志、截图和设备状态对比为准"
    elif review_confirmed:
        confirmed = "人工已确认静态漏洞链；尚未据此声称运行时影响已经发生"
    else:
        confirmed = "仅确认静态候选链，尚未执行或完成动态影响验证"
    limitations = list(finding.get("limitations") or [])
    limitations.extend(
        str(gap.get("message") or gap.get("code"))
        for gap in finding.get("blocking_gaps", [])
        if isinstance(gap, dict) and (gap.get("message") or gap.get("code"))
    )
    return {
        "confirmed": confirmed,
        "capabilities": capabilities,
        "consequences": consequences,
        "sink_evidence": evidence,
        "prerequisites": list(finding.get("attacker_prerequisites") or []),
        "scope": list(finding.get("impact_scope") or []),
        "limitations": list(dict.fromkeys(limitations)),
    }


def _adb_extra_template(finding: dict[str, Any]) -> str:
    source_text = " ".join(
        str(source.get("text") or source.get("kind") or "")
        for source in finding.get("sources", [])
        if isinstance(source, dict)
    )
    if "getIntExtra" in source_text or "readInt" in source_text:
        return " --ei '<EXTRA_KEY>' 1"
    if "getLongExtra" in source_text or "readLong" in source_text:
        return " --el '<EXTRA_KEY>' 1"
    if "getBooleanExtra" in source_text:
        return " --ez '<EXTRA_KEY>' true"
    if "getParcelableExtra" in source_text or "getSerializableExtra" in source_text:
        return ""
    if "getStringExtra" in source_text or "getExtra" in source_text:
        return " --es '<EXTRA_KEY>' '<TEST_VALUE>'"
    return ""


def _poc_guide(finding: dict[str, Any], package_name: str) -> dict[str, Any]:
    """生成安全的 ADB 可达性验证模板；不伪造参数、结果或动态影响。"""

    component_type = str(finding.get("component") or "unknown")
    component_name = str(finding.get("component_name") or "<COMPONENT_FQCN>")
    simple_name = component_name.rsplit(".", 1)[-1]
    target = f"{package_name}/{component_name}"
    extra = _adb_extra_template(finding)
    commands = [
        "adb devices",
        f"adb shell dumpsys package '{package_name}' | grep -A 15 '{simple_name}'",
        "adb logcat -c",
    ]
    notes = [
        "仅在已获授权的测试设备、测试账号和可回滚数据上执行。",
        "命令中的 <ACTION>、<EXTRA_KEY>、<TEST_VALUE>、<AUTHORITY> 和 <PATH> 必须依据 Manifest 与 Source 证据替换，不得把占位符当成真实参数。",
    ]
    expected = ["观察命令是否被系统接受，以及 logcat 中是否出现 SecurityException、Permission Denial、组件异常或目标业务日志。"]
    if component_type == "activity":
        commands.append(f"adb shell am start -W -n '{target}'{extra}")
        commands.append(f"adb shell am force-stop '{package_name}'")
        expected.append("若 Activity 可由 shell 启动，只能证明组件可达；还需结合 Sink 前后的状态变化确认实际影响。")
    elif component_type == "service":
        if "BINDER" in str(finding.get("rule_id") or "") or finding.get("binder_transactions"):
            commands.append(f"adb shell dumpsys activity services '{package_name}' | grep -A 25 '{simple_name}'")
            notes.append("ADB 不能直接完成普通第三方 UID 的任意 AIDL Parcel 调用；Binder 漏洞需使用无特殊权限、不同签名的最小测试 APK 执行 bindService + transact。")
            expected.append("dumpsys 只能辅助确认 Service/绑定记录；普通 UID 是否能取得 Binder 和调用 transaction 必须由测试 APK 证明。")
        else:
            commands.append(f"adb shell am startservice -n '{target}'{extra}")
            expected.append("startservice 成功仅证明 started-service 路径可达；需检查状态变化和日志确认 Sink 是否执行。")
    elif component_type == "receiver":
        commands.append(f"adb shell am broadcast -n '{target}' -a '<ACTION>'{extra}")
        expected.append("观察 Broadcast completed、权限拒绝和 onReceive 下游日志；动态 Receiver 还需先满足其注册生命周期。")
    elif component_type == "provider":
        commands.append(f"adb shell content query --uri 'content://<AUTHORITY>/<PATH>' --user 0")
        expected.append("只先执行只读 query；openFile/delete/update 等可能修改数据的验证应改用测试文件并单独确认。")
    else:
        commands.append(f"adb shell dumpsys package '{package_name}' | grep -A 20 '{simple_name}'")
        notes.append("组件类型不足，当前只能提供存在性检查，不能据此确认漏洞。")
    commands.append(
        f"adb logcat -d | grep -E '{simple_name}|SecurityException|Permission Denial|AndroidRuntime'"
    )
    if any(
        isinstance(source, dict) and any(token in str(source.get("text") or "") for token in ("getParcelableExtra", "getSerializableExtra"))
        for source in finding.get("sources", [])
    ):
        notes.append("该链路使用 Parcelable/Serializable，ADB 无法可靠构造目标应用自定义对象，应使用普通 UID 测试 APK。")
    return {"commands": commands, "notes": notes, "expected": expected}


def _render_poc_guide(guide: dict[str, Any]) -> str:
    commands = "\n".join(guide["commands"])
    notes = "\n".join(f"- {item}" for item in guide["notes"])
    expected = "\n".join(f"- {item}" for item in guide["expected"])
    return f"""以下命令用于复现入口可达性和收集证据，不代表命令执行后漏洞必然成立。

```bash
{commands}
```

验证注意事项：
{notes}

预期观察：
{expected}"""


def _nested_value(value: Any, *keys: str) -> Any:
    """从兼容的新旧 AI 元数据结构中提取首个非空字段。"""

    if isinstance(value, dict):
        for key in keys:
            if value.get(key) is not None:
                return value[key]
        for child in value.values():
            found = _nested_value(child, *keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _nested_value(child, *keys)
            if found is not None:
                return found
    return None


def build_report_payload(finding: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """构建报告数据；拒绝将仅具 L1 证据的提示项包装为正式漏洞。"""

    if finding.get("evidence_level") == "L1" or finding.get("severity") == "informational":
        raise ConflictError("L1 提示项不进入正式漏洞报告", "L1_REPORT_FORBIDDEN")
    app = finding.get("app", {})
    locations = finding.get("locations", [])
    evidence_refs = [
        f"{loc.get('path', 'AndroidManifest.xml')}:{loc.get('line', '未获取')} ({loc.get('verification', 'fact')})"
        for loc in locations
    ] or [f"finding/{finding['id']}：证据不足"]
    chain_steps = []
    for source in finding.get("sources", []):
        chain_steps.append(_step("source", source, finding["id"]))
    for path in finding.get("propagation_paths", []):
        chain_steps.append(_step("propagation", path, finding["id"]))
    for sink in finding.get("sinks", []):
        chain_steps.append(_step("sink", sink, finding["id"]))
    if not chain_steps:
        chain_steps.append({"type": "missing", "text": "证据不足：未形成 Source→Sink 链路", "evidence_id": finding["id"], "status": "missing"})
    component_name = finding.get("component_name") or finding.get("component", "未获取")
    analysis_summary = _analysis_summary(run, finding)
    critical_gap = any(
        not isinstance(gap, dict) or gap.get("critical", True)
        for gap in [*finding.get("coverage_gaps", []), *finding.get("blocking_gaps", [])]
    )
    title = finding.get("title") or f"{component_name} 静态安全候选"
    if critical_gap and not title.startswith("待确认："):
        title = f"待确认：{title}"
    dynamic_status = {
        "not_executed": "未执行",
        "passed": "成功",
        "failed": "失败",
        "inconclusive": "结果不确定",
    }.get(finding.get("dynamic_validation_status"), "未执行")
    package_name = str(app.get("package") or "<PACKAGE_NAME>")
    impact = {
        "severity": SEVERITY_LABELS.get(finding.get("severity", "pending"), "待定"),
        **_impact_details(finding),
    }
    poc_guide = _poc_guide(finding, package_name)
    manifest = run.get("manifest") or {}
    ai_metadata = [finding.get("ai_analysis", {}), finding.get("ai_analysis_trace", [])]
    stop_reason = finding.get("ai_stop_reason") or _nested_value(ai_metadata, "stop_reason")
    prompt_version = finding.get("prompt_version") or _nested_value(ai_metadata, "prompt_version")
    ai_schema_version = finding.get("ai_schema_version") or _nested_value(
        ai_metadata, "schema_version"
    )
    cache_hit = finding.get("cache_hit")
    if cache_hit is None:
        cache_hit = _nested_value(ai_metadata, "cache_hit")
    triage_disposition = finding.get("triage_disposition") or _nested_value(
        finding.get("ai_analysis", {}), "triage_disposition"
    )
    artifact_versions = manifest.get("artifact_schema_versions") or {}
    return {
        "app_version": {
            "version_code": app.get("version_code", "未获取"),
            "version_name": app.get("version_name", "未获取"),
            "compile_sdk_version": app.get("compile_sdk_version", "未获取"),
            "compile_sdk_codename": app.get("compile_sdk_codename", "未获取"),
            "package": app.get("package", "未获取"),
        },
        "pipeline_version": finding.get(
            "pipeline_version", manifest.get("pipeline_version", run.get("pipeline_version", "1.0.0"))
        ),
        "schema_versions": {
            "finding": finding.get("schema_version", "1.0.0"),
            "run_manifest": manifest.get("schema_version", run.get("schema_version", "1.0.0")),
            "report_payload": artifact_versions.get("report_payload", "2.0.0"),
            "ai": ai_schema_version,
        },
        "prompt_version": prompt_version,
        "status_layers": finding.get("status_layers") or {
            "funnel": finding.get("funnel_disposition"),
            "analysis": finding.get("analysis_status", "rule_only"),
            "evidence": finding.get("evidence_decision", "unresolved"),
            "review": finding.get("review_status", "pending_ai"),
        },
        "deterministic_facts": {
            "reachability_status": finding.get("reachability_status", "unknown"),
            "dataflow_status": finding.get("dataflow_status", "not_proven"),
            "authorization_status": finding.get("authorization_status", "unknown"),
            "guard_status": finding.get("guard_status", "unknown"),
            "impact_status": finding.get("impact_status", "potential"),
            "deterministic_chain_verified": finding.get("deterministic_chain_verified") is True,
        },
        "ai_observation": {
            "analysis_track": finding.get("analysis_track"),
            "triage_disposition": triage_disposition,
            "summary": (finding.get("ai_analysis") or {}).get("summary"),
            "stop_reason": stop_reason,
            "cache_hit": cache_hit,
        },
        "external_status": finding.get("external_status", "not_exported"),
        "title": title,
        "finding_status": STATUS_LABELS.get(finding.get("review_status", "pending_ai"), "待AI复核"),
        "analysis_status": finding.get("analysis_status", "rule_only"),
        "analysis_summary": analysis_summary,
        "coverage_gaps": finding.get("coverage_gaps", []),
        "blocking_gaps": finding.get("blocking_gaps", []),
        "dynamic_validation_status": dynamic_status,
        "environment": "未执行动态验证，仅完成 DEX 反编译伪源码静态分析",
        "description": finding.get("description") or f"静态分析发现 {component_name} 存在外部输入到敏感操作的候选链路，成立条件与 Guard 仍需人工复核。",
        "evidence_refs": evidence_refs,
        "chain_steps": chain_steps,
        "trust_boundary": {
            "original_boundary": finding.get("permission") or "未发现可确认的强权限边界；待人工复核",
            "proxy_component": component_name,
            "attacker_permission": ", ".join(finding.get("attacker_prerequisites", [])) or "普通第三方应用（静态推断，待验证）",
            "bypass_reason": "组件外部可达且候选链路未确认存在不可绕过 Guard；待验证",
        },
        "impact": impact,
        "poc": _render_poc_guide(poc_guide),
        "poc_guide": poc_guide,
        "poc_result": (
            "动态验证记录为成功；请以已归档日志、截图和状态对比确认具体影响"
            if finding.get("dynamic_validation_status") == "passed"
            else "尚未执行动态影响验证；上述命令只用于验证入口可达性和收集证据"
        ),
        "remediation": finding.get("remediation") or ["取消不必要导出或增加签名级权限", "在敏感操作前校验调用者身份并对外部参数实施严格白名单"],
        "run_id": run["id"],
        "finding_id": finding["id"],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    """将规范化报告数据渲染为固定章节结构的 Markdown。"""

    app = payload["app_version"]
    chain = "\n→ ".join(f"[{step['status']}] {step['text']}（证据：{step['evidence_id']}）" for step in payload["chain_steps"])
    trust = payload["trust_boundary"]
    impact = payload["impact"]
    refs = "\n".join(f"证据引用：{ref}" for ref in payload["evidence_refs"])
    remediation = "\n".join(payload["remediation"])
    analysis = payload["analysis_summary"]
    completeness = (
        f"扫描完整性：{'不完整' if analysis['analysis_incomplete'] else '完整'}；"
        f"JADX：{analysis['jadx_status']}（错误 {analysis['jadx_error_count']}）；"
        f"索引跳过文件：{analysis['skipped_file_count']}；"
        f"规则失败：{analysis['rule_failure_count']}/{analysis['rule_total_count']}；"
        f"规则组件缺口：{analysis['rule_component_gap_count']}；"
        f"AI：{analysis['ai_status']}（成功 {analysis['ai_success_count']}，失败 {analysis['ai_failure_count']}）"
        f"；AI 跳过原因：{analysis['ai_skip_reason'] or '无'}；"
        f"证据完整性：候选 {analysis['candidates_checked']}，闭合链 {analysis['deterministic_chains_closed']}，"
        f"可定级 Finding {analysis['gradeable_findings']}"
    )
    gaps = [*payload.get("coverage_gaps", []), *payload.get("blocking_gaps", [])]
    gap_text = _format_gaps(gaps) or "- 无"
    deterministic = payload.get("deterministic_facts", {})
    deterministic_text = "；".join(
        f"{key}={value}" for key, value in deterministic.items()
    ) or "旧报告未记录"
    observation = payload.get("ai_observation", {})
    observation_text = "；".join(
        f"{key}={value}" for key, value in observation.items() if value is not None
    ) or "无 AI observation"
    schema_text = "；".join(
        f"{key}={value}" for key, value in payload.get("schema_versions", {}).items()
        if value is not None
    ) or "未记录"
    status_layers = payload.get("status_layers", {})
    status_layers_text = "；".join(
        f"{key}={value}" for key, value in status_layers.items() if value is not None
    ) or "未记录"
    trust_statement = (
        f"原保护权限或信任边界：{trust['original_boundary']}；代理组件：{trust['proxy_component']}；"
        f"攻击者所需权限：{trust['attacker_permission']}；绕过说明：{trust['bypass_reason']}"
    )
    capabilities = "\n".join(f"- {item}" for item in impact.get("capabilities", [])) or "- 尚未识别具体能力"
    consequences = "\n".join(f"- {item}" for item in impact.get("consequences", [])) or "- 实际后果待验证"
    sink_evidence = "\n".join(f"- {item}" for item in impact.get("sink_evidence", [])) or "- 未提供 Sink 证据"
    limitations = "\n".join(f"- {item}" for item in impact.get("limitations", [])) or "- 无额外限制说明"
    prerequisites = "；".join(impact.get("prerequisites", [])) or "待验证"
    scope = "；".join(impact.get("scope", [])) or "由 Sink 和运行时前置条件决定"
    return f'''# {payload['title']}

## 版本
android:versionCode="{app['version_code']}"
android:versionName="{app['version_name']}"
android:compileSdkVersion="{app['compile_sdk_version']}"
android:compileSdkVersionCodename="{app['compile_sdk_codename']}"
package="{app['package']}"
发现状态：{payload['finding_status']}
自动分析状态：{payload['analysis_status']}
动态验证状态：{payload['dynamic_validation_status']}
Pipeline 版本：{payload.get('pipeline_version', '1.0.0')}
Prompt 版本：{payload.get('prompt_version') or '未记录'}
Schema 版本：{schema_text}
四层状态：{status_layers_text}
确定性事实：{deterministic_text}
AI observation：{observation_text}
外发状态：{payload.get('external_status', 'not_exported')}
{completeness}
覆盖/停止原因：{observation.get('stop_reason') or analysis.get('ai_skip_reason') or '正常完成或未记录'}
覆盖与阻断条件：
{gap_text}

## 测试环境：
{payload['environment']}

## 漏洞描述
{payload['description']}

{refs}

## 漏洞链路：
{chain}

{trust_statement}

漏洞危害：{impact['severity']}

当前证据边界：{impact['confirmed']}

攻击者可获得的能力：
{capabilities}

可能造成的具体后果：
{consequences}

对应 Sink 证据：
{sink_evidence}

利用前置条件：{prerequisites}

影响范围：{scope}

限制与未确认项：
{limitations}

## POC
{payload['poc']}

POC结果：{payload['poc_result']}

## 修复方案
{remediation}
'''


def _analysis_summary(run: dict[str, Any], finding: dict[str, Any]) -> dict[str, Any]:
    """从 run manifest 提取报告必须展示的完整性和阶段统计。"""

    manifest = run.get("manifest") or {}
    stages = {stage.get("name"): stage for stage in manifest.get("stages", [])}
    decompile = stages.get("decompiling", {})
    decompile_summary = decompile.get("summary", {})
    rules = stages.get("rule_prescan", {}).get("summary", {})
    ai_stage = stages.get("ai_analysis", {})
    ai_summary = ai_stage.get("summary", {})
    index_stats = stages.get("code_slicing", {}).get("summary", {}).get("index_stats", {})
    integrity = stages.get("evidence_integrity_validation", {}).get("summary", {})
    return {
        "analysis_incomplete": bool(manifest.get("analysis_incomplete") or finding.get("analysis_incomplete")),
        "jadx_status": decompile.get("status", "unknown"),
        "jadx_error_count": decompile_summary.get("error_count") or 0,
        "skipped_file_count": index_stats.get("skipped_file_count") or 0,
        "rule_failure_count": len(rules.get("rule_failures") or []),
        "rule_total_count": int(rules.get("rule_total_count") or 0),
        "rule_component_gap_count": len(rules.get("component_coverage_gaps") or []),
        "ai_status": ai_stage.get("status", finding.get("analysis_status", "unknown")),
        "ai_success_count": int(ai_summary.get("completed") or max(
            0,
            int(ai_summary.get("analyzed") or 0)
            - int(ai_summary.get("failed") or 0)
            - int(ai_summary.get("incomplete") or 0),
        )),
        "ai_failure_count": int(ai_summary.get("failed") or 0),
        "ai_skip_reason": ai_summary.get("reason") or finding.get("ai_skip_reason"),
        "candidates_checked": int(integrity.get("candidates_checked") or 0),
        "deterministic_chains_closed": int(integrity.get("deterministic_chains_closed") or 0),
        "gradeable_findings": int(integrity.get("gradeable_findings") or 0),
    }


def _step(kind: str, value: Any, evidence_id: str) -> dict[str, str]:
    if isinstance(value, dict):
        text = value.get("text") or value.get("kind") or str(value)
        status = value.get("status", "fact")
        evidence = value.get("evidence_id", evidence_id)
    else:
        text, status, evidence = str(value), "fact", evidence_id
    return {"type": kind, "text": text, "status": status, "evidence_id": evidence}
