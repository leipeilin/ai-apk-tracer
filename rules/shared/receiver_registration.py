"""动态 BroadcastReceiver 注册调用的共享解析器。"""

from __future__ import annotations

import ast
import re
from typing import Any

from shared.authorization import permission_policy


_RECEIVER_FLAGS = {
    "RECEIVER_VISIBLE_TO_INSTANT_APPS": 0x1,
    "RECEIVER_EXPORTED": 0x2,
    "RECEIVER_NOT_EXPORTED": 0x4,
}


def parse_receiver_registrations(
    file: dict[str, Any], manifest: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """从索引调用点保守解析动态注册的 overload、授权与外部可达性。

    仅信任 framework/AndroidX owner、参数个数与可用 descriptor 一致的调用；应用 wrapper、
    未知 overload、无法求值的 flag/permission/action 都产生 critical gap。返回的 reportable
    只是路由信号，只有明确 local/not-exported/protected-only 才可判定不可外部到达。
    """

    manifest = manifest or {}
    content = str(file.get("content") or "")
    constants = _same_file_constants(content)
    registrations: list[dict[str, Any]] = []
    for method in file.get("methods", []):
        for call in method.get("call_sites", []):
            if call.get("method_name") != "registerReceiver":
                continue
            # 索引内 resolved target 是应用方法。wrapper 本身不是系统注册点；若它
            # 最终调用真实 Context/ContextCompat，那个底层 call-site 会单独被解析。
            if call.get("resolved_target_id"):
                continue
            if _receiver_api_family(call, file, method) == "application_method":
                continue
            # registerReceiver(null, filter) 是 sticky 广播查询（读取最后发送的 sticky
            # 值），不是接收器注册——不产生外部可达接收器候选（SOP 动态 Receiver 第 2 条；
            # v2026-08-09 修复 megvii CommonProtectorManager 类误报）。
            args = [str(value).strip() for value in call.get("arguments", [])]
            receiver_index = 1 if _receiver_api_family(call, file, method) == "context_compat" else 0
            if len(args) > receiver_index and args[receiver_index] == "null":
                continue
            registrations.append(_parse_call(file, method, call, manifest, constants))
    return registrations


def receiver_class_name(
    content: str,
    expression: str,
    package: str = "",
    imports: list[str] | None = None,
    scope_content: str | None = None,
) -> str | None:
    """将注册参数中的实例精确还原为当前调用点可见的 Receiver 类型。"""

    direct = re.search(r"\bnew\s+([A-Za-z_$][\w$.]*)", expression)
    if direct and direct.group(1).rsplit(".", 1)[-1] != "BroadcastReceiver":
        return _qualify(direct.group(1), package, imports)
    variable = expression.strip().removeprefix("this.")
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", variable):
        return None
    search_scopes = [scope_content] if scope_content is not None else []
    if content not in search_scopes:
        search_scopes.append(content)
    for scope in search_scopes:
        constructed = re.search(
            rf"\b(?:[A-Za-z_$][\w$<>.]*\s+)?(?:this\.)?{re.escape(variable)}\s*=\s*new\s+([A-Za-z_$][\w$.]*)",
            scope,
        )
        if constructed and constructed.group(1).rsplit(".", 1)[-1] != "BroadcastReceiver":
            return _qualify(constructed.group(1), package, imports)
        declaration = re.search(
            rf"\b([A-Za-z_$][\w$.]*)\s+(?:this\.)?{re.escape(variable)}\s*(?:[;=])", scope
        )
        if declaration and declaration.group(1).rsplit(".", 1)[-1] != "BroadcastReceiver":
            return _qualify(declaration.group(1), package, imports)
    return None


def is_exact_on_receive(method: dict[str, Any]) -> bool:
    """仅接受 Android 生命周期签名 onReceive(Context, Intent)，排除同名 overload。"""

    if method.get("name") != "onReceive":
        return False
    parameters = list(method.get("structured_parameters") or [])
    if len(parameters) != 2:
        return False
    types = [
        str(item.get("qualified_type") or item.get("normalized_type") or item.get("declared_type") or "")
        .replace("/", ".")
        .strip("L;?")
        for item in parameters
    ]
    return types[0].rsplit(".", 1)[-1] == "Context" and types[1].rsplit(".", 1)[-1] == "Intent"


def _receiver_api_family(
    call: dict[str, Any], file: dict[str, Any] | None = None, method: dict[str, Any] | None = None
) -> str:
    """仅凭可信 owner/type 识别注册 API；变量名和应用同名类型不能充当证明。"""

    if call.get("resolved_target_id"):
        return "application_method"
    file = file or {}
    method = method or {}
    receiver_text = str(call.get("receiver_text") or "").strip()
    receiver_type = str(call.get("receiver_type") or "").strip().replace("/", ".").strip("L;?")
    imports = {str(item) for item in file.get("imports", [])}

    def resolved_type(simple: str) -> str:
        if receiver_type != simple:
            return receiver_type
        imported = next((item for item in imports if item.rsplit(".", 1)[-1] == simple), "")
        return imported or receiver_type

    receiver_type = resolved_type(receiver_type.rsplit(".", 1)[-1])
    receiver_root = receiver_text.split(".", 1)[0]
    local_class_names = {str(item.get("name") or "") for item in file.get("classes", [])}
    if not receiver_type and receiver_root == "LocalBroadcastManager" and receiver_root not in local_class_names:
        receiver_type = "androidx.localbroadcastmanager.content.LocalBroadcastManager"
    if receiver_type == "androidx.localbroadcastmanager.content.LocalBroadcastManager":
        return "local_broadcast"
    if receiver_type == "androidx.core.content.ContextCompat":
        return "context_compat"

    platform_contexts = {
        "android.content.Context", "android.content.ContextWrapper", "android.app.Activity",
        "android.app.Service", "android.app.Application",
    }
    if receiver_type in platform_contexts:
        return "platform_context"

    if not receiver_text and receiver_type and not call.get("resolved_target_id"):
        # JADX 可省略继承所得 Context receiver；同名应用 wrapper 若存在会被索引
        # resolved_target_id 捕获并在上方拒绝。
        return "platform_context"

    if receiver_text.replace(" ", "") in {"", "this", "super"}:
        owner = str(method.get("qualified_class") or "")
        class_info = next(
            (item for item in file.get("classes", []) if item.get("qualified_name") == owner), None
        )
        parent = str((class_info or {}).get("extends") or "").replace("/", ".").strip("L;?")
        if parent and "." not in parent:
            parent = next((item for item in imports if item.rsplit(".", 1)[-1] == parent), parent)
        if parent in platform_contexts:
            return "platform_context"

    # 完整的应用/第三方 FQCN 足以证明它不是受信 framework owner。
    if "." in receiver_type and not receiver_type.startswith(("android.", "androidx.")):
        return "application_method"
    return "unknown"


def _descriptor_arguments(call: dict[str, Any]) -> list[str] | None:
    descriptor = str(call.get("method_descriptor") or "")
    match = re.match(r"^\((.*)\)->", descriptor)
    if not match:
        return None
    value = match.group(1).strip()
    return [] if not value else [item.strip() for item in _split_top_level(value, ",")]


def _known_descriptor_type(types: list[str] | None, index: int, expected: set[str]) -> bool:
    if types is None or index >= len(types):
        return True
    value = types[index].replace("/", ".").strip().strip("L;?")
    if not value or value == "?":
        return True
    return value.rstrip("[]").rsplit(".", 1)[-1] in expected


def _parse_call(
    file: dict[str, Any],
    method: dict[str, Any],
    call: dict[str, Any],
    manifest: dict[str, Any],
    constants: dict[str, str],
) -> dict[str, Any]:
    """按 API family 绑定参数位置，并在任何歧义处保留 unknown 与 gap。

    常量求值只展开同文件受支持表达式；action 只查看声明区及调用前方法前缀，避免借用注册
    之后或其他方法的赋值。Manifest protected-broadcast 目录缺版本时不能证明 protected-only。
    """

    content = str(file.get("content") or "")
    method_prefix = _method_prefix(content, method, call)
    declaration_scope = _declaration_scope(content, list(file.get("methods") or []))
    args = [str(value).strip() for value in call.get("arguments", [])]
    api_family = _receiver_api_family(call, file, method)
    local_broadcast = api_family == "local_broadcast"
    receiver_index = 1 if api_family == "context_compat" else 0
    filter_index = receiver_index + 1
    flag_index: int | None = None
    permission_index: int | None = None
    scheduler_index: int | None = None
    overload = "unknown"
    if api_family == "local_broadcast" and len(args) == 2:
        overload = "local"
    elif api_family == "context_compat" and len(args) == 4:
        overload, flag_index = "context_compat_flags", 3
    elif api_family == "context_compat" and len(args) == 6:
        overload, permission_index, scheduler_index, flag_index = "context_compat_permission_flags", 3, 4, 5
    elif api_family == "platform_context" and len(args) == 2:
        overload = "context_legacy"
    elif api_family == "platform_context" and len(args) == 3:
        overload, flag_index = "context_flags", 2
    elif api_family == "platform_context" and len(args) == 4:
        overload, permission_index, scheduler_index = "context_permission", 2, 3
    elif api_family == "platform_context" and len(args) == 5:
        overload, permission_index, scheduler_index, flag_index = "context_permission_flags", 2, 3, 4

    descriptor_types = _descriptor_arguments(call)
    descriptor_matches = (
        (descriptor_types is None or len(descriptor_types) == len(args))
        and _known_descriptor_type(descriptor_types, filter_index, {"IntentFilter"})
        and (flag_index is None or _known_descriptor_type(descriptor_types, flag_index, {"int", "Integer"}))
        and (permission_index is None or _known_descriptor_type(descriptor_types, permission_index, {"String"}))
        and (scheduler_index is None or _known_descriptor_type(descriptor_types, scheduler_index, {"Handler"}))
        and (
            api_family != "context_compat"
            or _known_descriptor_type(descriptor_types, 0, {"Context"})
        )
    )
    if overload != "unknown" and not descriptor_matches:
        overload = "unknown"
        flag_index = permission_index = scheduler_index = None

    receiver_expression = args[receiver_index] if receiver_index < len(args) else ""
    filter_expression = args[filter_index] if filter_index < len(args) else ""
    flag_expression = args[flag_index] if flag_index is not None and flag_index < len(args) else None
    permission_expression = (
        args[permission_index] if permission_index is not None and permission_index < len(args) else None
    )
    gaps: list[dict[str, Any]] = []
    if overload == "unknown":
        gaps.append({
            "code": "RECEIVER_REGISTRATION_OVERLOAD_UNKNOWN",
            "critical": True,
            "api_family": api_family,
            "argument_count": len(args),
            "method_descriptor": call.get("method_descriptor"),
        })

    flag_value, flag_unknown = _resolve_int(flag_expression, constants)
    if local_broadcast:
        flag_status = "local"
    elif flag_expression is None and overload != "unknown":
        flag_status = "legacy_unspecified"
    elif flag_unknown or flag_value is None:
        flag_status = "unknown"
        gaps.append({
            "code": "RECEIVER_FLAG_UNKNOWN",
            "critical": True,
            "expression": flag_expression,
            "unresolved_symbols": flag_unknown,
        })
    elif flag_value & 0x2 and flag_value & 0x4:
        flag_status = "unknown"
        gaps.append({
            "code": "RECEIVER_FLAG_CONFLICT",
            "critical": True,
            "expression": flag_expression,
            "value": flag_value,
        })
    elif flag_value & 0x4:
        flag_status = "not_exported"
    elif flag_value & 0x2:
        flag_status = "exported"
    else:
        flag_status = "unknown"
        gaps.append({
            "code": "RECEIVER_FLAG_EXPORT_STATE_UNKNOWN",
            "critical": True,
            "expression": flag_expression,
            "value": flag_value,
        })
    export_status = "not_applicable" if local_broadcast else flag_status

    permission, permission_known = _resolve_string(permission_expression, constants)
    if permission_expression is None or permission_expression.strip() == "null":
        permission, permission_known = None, True
    if overload == "unknown":
        permission_result = {
            "permission": None,
            "status": "unknown",
            "protection": None,
            "provenance": "registration_overload_unresolved",
        }
    elif not permission_known:
        permission_result = {
            "permission": None,
            "status": "unknown",
            "protection": None,
            "provenance": "registration_expression_unresolved",
        }
        gaps.append({
            "code": "DYNAMIC_RECEIVER_PERMISSION_UNRESOLVED",
            "critical": True,
            "expression": permission_expression,
        })
    else:
        permission_result = permission_policy(manifest, permission or None)
        if permission_result["status"] == "unknown":
            gaps.append({
                "code": "RECEIVER_PERMISSION_PROTECTION_UNKNOWN",
                "critical": True,
                "permission": permission,
                "provenance": permission_result["provenance"],
            })

    action_constants = {
        **_same_file_constants(declaration_scope),
        **_same_file_constants(method_prefix),
    }
    actions, unresolved_actions = _receiver_actions(
        content=method_prefix, expression=filter_expression, constants=action_constants,
    )
    for expression in unresolved_actions:
        gaps.append({
            "code": "RECEIVER_ACTION_UNRESOLVED",
            "critical": True,
            "expression": expression,
        })
    protected_actions = set(manifest.get("protected_broadcast_actions", []))
    catalog = manifest.get("protected_broadcast_catalog_version")
    action_authorization = [
        {
            "action": action,
            "status": "protected" if action in protected_actions else "unknown",
            "catalog": catalog,
        }
        for action in actions
    ]
    protected_only = bool(actions) and not unresolved_actions and bool(catalog) and all(
        item["status"] == "protected" for item in action_authorization
    )
    if local_broadcast or export_status == "not_exported" or protected_only:
        externally_reachable: bool | None = False
    elif export_status == "exported":
        externally_reachable = True
    else:
        externally_reachable = None
    reportable = externally_reachable is not False and permission_result["status"] != "strongly_protected"
    platform_branch = "SDK_INT" in method_prefix or "VERSION_CODES" in method_prefix
    return {
        "call": call,
        "method_id": method.get("id"),
        "method_name": method.get("name"),
        "path": file.get("path"),
        "line": int(call.get("start_line", method.get("start_line", 1))),
        "api_family": api_family,
        "overload": overload,
        "receiver_expression": receiver_expression,
        "receiver_class": receiver_class_name(
            declaration_scope,
            receiver_expression,
            str(file.get("package") or ""),
            list(file.get("imports") or []),
            scope_content=method_prefix,
        ),
        "filter_expression": filter_expression,
        "flag_expression": flag_expression,
        "flags_expression": flag_expression,
        "flag_value": flag_value,
        "flags_value": flag_value,
        "flag_status": flag_status,
        "export_status": export_status,
        "permission_expression": permission_expression,
        "permission": permission,
        "permission_status": permission_result["status"],
        "permission_policy": permission_result,
        "actions": actions,
        "unresolved_action_expressions": unresolved_actions,
        "action_authorization": action_authorization,
        "protected_actions_only": protected_only,
        "local_broadcast": local_broadcast,
        "platform_branch": platform_branch,
        "externally_reachable": externally_reachable,
        "reportable": reportable,
        "coverage_gaps": _unique_gaps(gaps),
    }


def _same_file_constants(content: str) -> dict[str, str]:
    constants: dict[str, str] = {}
    patterns = (
        re.compile(
            r"\b(?:(?:public|protected|private)\s+)*(?:static\s+final|final\s+static)\s+"
            r"(?:int|Integer|String|long|Long)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;\n]+)"
        ),
        re.compile(
            r"\bconst\s+val\s+([A-Za-z_$][\w$]*)(?:\s*:\s*[A-Za-z_$][\w.$<>?]*)?\s*=\s*([^;\n]+)"
        ),
    )
    for pattern in patterns:
        for name, expression in pattern.findall(content):
            constants[name] = expression.strip()
    return constants


def _resolve_int(expression: str | None, constants: dict[str, str]) -> tuple[int | None, list[str]]:
    if expression is None:
        return None, []
    unresolved: set[str] = set()

    def expand(value: str, active: set[str]) -> str:
        value = re.sub(r"\(\s*(?:byte|short|int|long|Integer|Long)\s*\)", "", value)
        value = re.sub(r"(?i)(0x[0-9a-f]+|\d+)[lL]\b", r"\1", value)

        def replace(match: re.Match[str]) -> str:
            token = match.group(0)
            leaf = token.rsplit(".", 1)[-1]
            if leaf in _RECEIVER_FLAGS:
                return str(_RECEIVER_FLAGS[leaf])
            if leaf in constants and leaf not in active:
                return f"({expand(constants[leaf], {*active, leaf})})"
            unresolved.add(token)
            return "0"

        return re.sub(
            r"(?<![0-9A-Za-z_$])[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*",
            replace,
            value,
        )

    expanded = expand(expression.strip(), set())
    try:
        tree = ast.parse(expanded, mode="eval")
        value = _eval_int_node(tree.body)
    except (SyntaxError, TypeError, ValueError, ZeroDivisionError):
        return None, sorted(unresolved or {expression})
    return (None, sorted(unresolved)) if unresolved else (value, [])


def _eval_int_node(node: ast.AST) -> int:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Invert, ast.UAdd, ast.USub)):
        value = _eval_int_node(node.operand)
        if isinstance(node.op, ast.Invert):
            return ~value
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.BitOr, ast.BitAnd, ast.BitXor, ast.LShift, ast.RShift, ast.Add, ast.Sub)
    ):
        left, right = _eval_int_node(node.left), _eval_int_node(node.right)
        operations = {
            ast.BitOr: int.__or__, ast.BitAnd: int.__and__, ast.BitXor: int.__xor__,
            ast.LShift: int.__lshift__, ast.RShift: int.__rshift__,
            ast.Add: int.__add__, ast.Sub: int.__sub__,
        }
        return operations[type(node.op)](left, right)
    raise ValueError("unsupported integer expression")


def _resolve_string(expression: str | None, constants: dict[str, str], active: set[str] | None = None) -> tuple[str | None, bool]:
    if expression is None:
        return None, True
    value = expression.strip()
    if value == "null":
        return None, True
    literal = re.fullmatch(r'"((?:\\.|[^"\\])*)"', value)
    if literal:
        try:
            return ast.literal_eval(value), True
        except (SyntaxError, ValueError):
            return None, False
    manifest_permission = re.fullmatch(r"(?:android\.)?Manifest\.permission\.([A-Za-z_$][\w$]*)", value)
    if manifest_permission:
        return f"android.permission.{manifest_permission.group(1)}", True
    active = active or set()
    if re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", value):
        leaf = value.rsplit(".", 1)[-1]
        if leaf in constants and leaf not in active:
            return _resolve_string(constants[leaf], constants, {*active, leaf})
    parts = _split_top_level(value, "+")
    if len(parts) > 1:
        resolved = [_resolve_string(part, constants, active) for part in parts]
        if all(known and item is not None for item, known in resolved):
            return "".join(str(item) for item, _ in resolved), True
    return None, False


def _method_prefix(content: str, method: dict[str, Any], call: dict[str, Any]) -> str:
    lines = content.splitlines()
    start_line = max(1, int(method.get("start_line") or 1))
    call_line = max(start_line, int(call.get("start_line") or start_line))
    prefix_lines = lines[start_line - 1:call_line]
    if not prefix_lines:
        return ""
    method_name = str(call.get("method_name") or "")
    call_index = prefix_lines[-1].find(method_name) if method_name else -1
    if call_index >= 0:
        prefix_lines[-1] = prefix_lines[-1][:call_index]
    else:
        prefix_lines = prefix_lines[:-1]
    return "\n".join(prefix_lines)


def _declaration_scope(content: str, methods: list[dict[str, Any]]) -> str:
    lines = content.splitlines()
    hidden: set[int] = set()
    for method in methods:
        start = max(1, int(method.get("start_line") or 1))
        end = max(start, int(method.get("end_line") or start))
        hidden.update(range(start, end + 1))
    return "\n".join("" if line_number in hidden else line for line_number, line in enumerate(lines, 1))


def _receiver_actions(
    content: str, expression: str, constants: dict[str, str]
) -> tuple[list[str], list[str]]:
    action_expressions: list[str] = []
    direct = re.search(r"\bnew\s+IntentFilter\s*\((.*)\)\s*$", expression, re.S)
    if direct:
        args = _split_top_level(direct.group(1), ",")
        if args:
            action_expressions.append(args[0])
    variable = expression.strip()
    if re.fullmatch(r"[A-Za-z_$][\w$]*", variable):
        escaped = re.escape(variable)
        action_expressions.extend(re.findall(
            rf"\b{escaped}\s*=\s*new\s+IntentFilter\s*\(\s*([^,)]+)", content
        ))
        action_expressions.extend(re.findall(
            rf"\b{escaped}\s*\.\s*addAction\s*\(\s*([^\n;)]+)", content
        ))
    actions: list[str] = []
    unresolved: list[str] = []
    for action_expression in action_expressions:
        normalized = action_expression.strip()
        action, known = _resolve_string(normalized, constants)
        if known and action:
            actions.append(action)
        else:
            unresolved.append(normalized)
    if not action_expressions:
        unresolved.append(expression.strip() or "<missing-filter>")
    return sorted(set(actions)), sorted(set(unresolved))


def _split_top_level(value: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
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
        elif char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth = max(0, depth - 1)
        elif char == delimiter and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return parts


def _qualify(type_name: str, package: str, imports: list[str] | None = None) -> str:
    first = type_name.split(".", 1)[0]
    if "." in type_name and first[:1].islower():
        return type_name
    imported = next(
        (item for item in (imports or []) if str(item).rsplit(".", 1)[-1] == first), None
    )
    if imported:
        return str(imported) + type_name[len(first):]
    return f"{package}.{type_name}" if package else type_name


def _unique_gaps(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result = []
    for gap in gaps:
        marker = (gap.get("code"), gap.get("expression"), gap.get("permission"))
        if marker not in seen:
            seen.add(marker)
            result.append(gap)
    return result
