"""版本化 Android 有效授权矩阵。"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

MATRIX_VERSION = "1.0.0-api36"
PLATFORM_CATALOG_VERSION = "android-api-36-minimal-1"
AUTHORIZATION_STATUSES = {
    "unprotected", "conditional", "protected", "strongly_protected", "unknown",
}

# 仅收录规则常见权限。平台权限不在此表时必须保持 unknown。
PLATFORM_PERMISSION_CATALOG: dict[str, str] = {
    "android.permission.INTERNET": "normal",
    "android.permission.ACCESS_NETWORK_STATE": "normal",
    "android.permission.ACCESS_WIFI_STATE": "normal",
    "android.permission.CHANGE_WIFI_STATE": "normal",
    "android.permission.VIBRATE": "normal",
    "android.permission.WAKE_LOCK": "normal",
    "android.permission.FOREGROUND_SERVICE": "normal",
    "android.permission.POST_NOTIFICATIONS": "dangerous",
    "android.permission.CAMERA": "dangerous",
    "android.permission.RECORD_AUDIO": "dangerous",
    "android.permission.READ_CONTACTS": "dangerous",
    "android.permission.WRITE_CONTACTS": "dangerous",
    "android.permission.READ_CALENDAR": "dangerous",
    "android.permission.WRITE_CALENDAR": "dangerous",
    "android.permission.READ_PHONE_STATE": "dangerous",
    "android.permission.CALL_PHONE": "dangerous",
    "android.permission.READ_SMS": "dangerous",
    "android.permission.SEND_SMS": "dangerous",
    "android.permission.RECEIVE_SMS": "dangerous",
    "android.permission.ACCESS_COARSE_LOCATION": "dangerous",
    "android.permission.ACCESS_FINE_LOCATION": "dangerous",
    "android.permission.ACCESS_BACKGROUND_LOCATION": "dangerous",
    "android.permission.READ_EXTERNAL_STORAGE": "dangerous",
    "android.permission.WRITE_EXTERNAL_STORAGE": "dangerous",
    "android.permission.BLUETOOTH_CONNECT": "dangerous",
    "android.permission.BLUETOOTH_SCAN": "dangerous",
    "android.permission.BODY_SENSORS": "dangerous",
    "android.permission.BODY_SENSORS_BACKGROUND": "dangerous",
    "android.permission.MANAGE_EXTERNAL_STORAGE": "signature|appop|preinstalled",
    "android.permission.WRITE_SETTINGS": "signature|appop|preinstalled",
    "android.permission.SYSTEM_ALERT_WINDOW": "signature|setup|appop|installer|pre23|development",
    "android.permission.BIND_ACCESSIBILITY_SERVICE": "signature",
    "android.permission.BIND_AUTOFILL_SERVICE": "signature",
    "android.permission.BIND_DEVICE_ADMIN": "signature",
    "android.permission.BIND_NOTIFICATION_LISTENER_SERVICE": "signature",
    "android.permission.BIND_VPN_SERVICE": "signature",
    "android.permission.DUMP": "signature|development",
    "android.permission.INTERACT_ACROSS_USERS": "signature|development",
    "android.permission.INTERACT_ACROSS_USERS_FULL": "signature",
    "android.permission.MANAGE_USERS": "signature",
    "android.permission.PACKAGE_USAGE_STATS": "signature|appop|development",
    "android.permission.READ_LOGS": "signature|development",
    "android.permission.WRITE_SECURE_SETTINGS": "signature",
}

_BASE_BY_NUMBER = {
    0: "normal", 1: "dangerous", 2: "signature", 3: "signatureOrSystem",
    4: "knownSigner", 5: "internal",
}
_FLAG_BITS = {
    0x10: "privileged", 0x20: "development", 0x40: "appop", 0x80: "pre23",
    0x100: "installer", 0x200: "verifier", 0x400: "preinstalled", 0x800: "setup",
    0x1000: "instant", 0x2000: "runtime", 0x4000: "oem", 0x8000: "vendorPrivileged",
    0x10000: "textClassifier", 0x20000: "recents", 0x40000: "role",
    0x80000: "configurator", 0x100000: "incidentReportApprover",
    0x200000: "appPredictor", 0x400000: "companion", 0x800000: "retailDemo",
    0x1000000: "module",
}
_BASE_NAMES = {value.lower(): value for value in _BASE_BY_NUMBER.values()}
_FLAG_NAMES = {value.lower(): value for value in _FLAG_BITS.values()}
_STATUS_RANK = {"unprotected": 0, "conditional": 1, "unknown": 2, "protected": 3, "strongly_protected": 4}


def parse_protection_level(value: Any) -> dict[str, Any]:
    """解析 protectionLevel base+flags；任何未知值均 fail-closed 为 unknown。"""

    result = {"raw": value, "base": None, "flags": [], "status": "unknown", "unknown_tokens": []}
    if isinstance(value, bool) or value is None:
        result["unknown_tokens"] = ["missing"]
        return result
    if isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"\s*(?:0[xX][0-9a-fA-F]+|\d+)\s*", value)):
        number = int(value, 0) if isinstance(value, str) else value
        base_value = number & 0xF
        result["base"] = _BASE_BY_NUMBER.get(base_value)
        remaining = number & ~0xF
        flags = []
        for bit, name in _FLAG_BITS.items():
            if remaining & bit:
                flags.append(name)
                remaining &= ~bit
        result["flags"] = flags
        if result["base"] is None:
            result["unknown_tokens"].append(f"base:{base_value}")
        if remaining:
            result["unknown_tokens"].append(f"flags:0x{remaining:x}")
    elif isinstance(value, str):
        tokens = [token.strip() for token in value.split("|") if token.strip()]
        bases = []
        flags = []
        for token in tokens:
            lower = token.lower()
            if lower in _BASE_NAMES:
                bases.append(_BASE_NAMES[lower])
            elif lower in _FLAG_NAMES:
                flags.append(_FLAG_NAMES[lower])
            else:
                result["unknown_tokens"].append(token)
        if len(bases) == 1:
            result["base"] = bases[0]
        else:
            result["unknown_tokens"].append("base_missing" if not bases else "multiple_bases")
        result["flags"] = flags
    else:
        result["unknown_tokens"] = ["invalid_type"]
    if result["unknown_tokens"]:
        return result
    base = result["base"]
    if base in {"signature", "signatureOrSystem", "knownSigner", "internal"}:
        result["status"] = "strongly_protected"
    elif base in {"normal", "dangerous"}:
        result["status"] = "conditional"
    return result


def permission_policy(manifest: dict[str, Any], permission: str | None, protection_hint: Any = None) -> dict[str, Any]:
    """解析权限来源与强度；未收录平台权限和未知目录项保持 unknown。"""

    if not permission:
        return {"permission": None, "status": "unprotected", "protection": None, "provenance": "none"}
    custom = manifest.get("custom_permissions", {})
    if permission in custom:
        raw = custom[permission]
        if isinstance(raw, dict):
            protection = raw.get("protection_level")
            provenance = raw.get("provenance", "manifest_permission")
        else:
            protection = raw
            provenance = "manifest_permission"
    elif permission in PLATFORM_PERMISSION_CATALOG:
        protection = PLATFORM_PERMISSION_CATALOG[permission]
        provenance = PLATFORM_CATALOG_VERSION
    elif permission.startswith("android.permission."):
        protection = None
        provenance = "platform_catalog_missing"
    elif protection_hint not in {None, "platform_or_unknown", "unknown"}:
        protection = protection_hint
        provenance = "component_protection_hint"
    else:
        protection = None
        provenance = "dependency_or_undeclared_permission"
    parsed = parse_protection_level(protection)
    return {
        "permission": permission,
        "status": parsed["status"],
        "protection": parsed,
        "provenance": provenance,
    }


def build_authorization_matrix(
    manifest: dict[str, Any],
    component: dict[str, Any],
    operation: str,
    path: str | None = None,
    mode: str | None = None,
    entry: str | None = None,
) -> list[dict[str, Any]]:
    """为具体入口/操作生成逐 path region 的有效授权行。"""

    directions = _operation_directions(operation, mode)
    regions = _path_regions(component, path)
    rows = []
    for direction in directions:
        for region in regions:
            permission, provenance, hint = _effective_permission(
                manifest, component, direction, region.get("policy")
            )
            policy = permission_policy(manifest, permission, hint)
            gaps = []
            if policy["status"] == "unknown":
                gaps.append({
                    "code": "AUTHORIZATION_PERMISSION_UNKNOWN", "critical": True,
                    "permission": permission, "provenance": policy["provenance"],
                })
            authority = _authority_resolution(manifest, component, path)
            gaps.extend(authority["blocking_gaps"])
            grant = _uri_grant_alternative(component, region, direction)
            alternatives = [{
                "kind": "manifest_permission", "permission": permission,
                "status": policy["status"], "direction": direction,
            }]
            status = policy["status"]
            prerequisites = _prerequisites(status, permission)
            if grant:
                alternatives.append(grant)
                status = "conditional"
                prerequisites.append(f"持有该 URI 的 {direction} 临时或持久授权")
            reachability = _reachability(component)
            if reachability == "not_reachable":
                status = "protected" if status == "unprotected" else status
            rows.append({
                "matrix_version": MATRIX_VERSION,
                "component": component.get("name"),
                "entry": entry or operation,
                "operation": operation,
                "access": direction,
                "path_region": region["region"],
                "reachability": reachability,
                "authorization": {"status": status},
                "alternatives": alternatives,
                "effective_permission": permission,
                "provenance": provenance,
                "attacker_prerequisites": prerequisites,
                "authority_resolution": authority["status"],
                "blocking_gaps": gaps,
            })
    return rows


def evaluate_authorization(
    manifest: dict[str, Any],
    component: dict[str, Any],
    operation: str,
    path: str | None = None,
    mode: str | None = None,
    entry: str | None = None,
) -> dict[str, Any]:
    """返回矩阵及保守合并状态；最弱区域/方向不能被强成员掩盖。"""

    rows = build_authorization_matrix(manifest, component, operation, path, mode, entry)
    statuses = [row["authorization"]["status"] for row in rows]
    if not statuses:
        status = "unknown"
    elif "unprotected" in statuses:
        status = "unprotected"
    elif "conditional" in statuses:
        status = "conditional"
    elif "unknown" in statuses:
        status = "unknown"
    elif "protected" in statuses:
        status = "protected"
    else:
        status = "strongly_protected"
    return {
        "matrix_version": MATRIX_VERSION,
        "status": status,
        "rows": rows,
        "blocking_gaps": _unique_gaps([gap for row in rows for gap in row["blocking_gaps"]]),
        "has_uri_grant_alternative": any(
            alternative.get("kind") == "uri_grant"
            for row in rows for alternative in row["alternatives"]
        ),
    }


def operation_for_rule(rule_id: str | None, sink: dict[str, Any] | None = None) -> tuple[str, str | None]:
    """将规则与实际 Sink 映射到授权操作。"""

    if rule_id == "PROVIDER_UNAUTHORIZED_QUERY":
        return "query", None
    if rule_id == "PROVIDER_UNAUTHORIZED_MUTATION":
        method = str((sink or {}).get("method_name") or "update")
        return method if method in {"insert", "update", "delete", "call", "applyBatch", "openFile"} else "update", None
    if rule_id == "PROVIDER_URI_TO_FILE":
        return "openFile", (sink or {}).get("mode")
    if rule_id == "PROVIDER_READ_WRITE_PERMISSION_MISSING":
        return "provider_access", None
    return "component_entry", None


def _operation_directions(operation: str, mode: str | None) -> list[str]:
    if operation in {"query", "getType", "canonicalize", "uncanonicalize", "refresh"}:
        return ["read"]
    if operation == "openFile":
        if mode:
            normalized = mode.lower()
            directions = []
            if "r" in normalized:
                directions.append("read")
            if any(token in normalized for token in ("w", "a", "+", "c", "t")):
                directions.append("write")
            return directions or ["read", "write"]
        return ["read", "write"]
    if operation in {"insert", "update", "delete", "call", "applyBatch", "bulkInsert"}:
        return ["write"]
    if operation == "provider_access":
        return ["read", "write"]
    return ["entry"]


def _effective_permission(
    manifest: dict[str, Any], component: dict[str, Any], direction: str, path_policy: dict[str, Any] | None
) -> tuple[str | None, list[dict[str, Any]], Any]:
    provenance: list[dict[str, Any]] = []
    field = "permission"
    if direction == "read":
        field = "read_permission"
    elif direction == "write":
        field = "write_permission"
    if path_policy:
        candidates = (
            [f"{direction}Permission", "permission"] if direction in {"read", "write"} else ["permission"]
        )
        for key in candidates:
            if path_policy.get(key) is not None:
                permission = path_policy[key]
                provenance.append({"source": "path-permission", "field": key, "value": permission})
                return permission, provenance, path_policy.get(f"{direction}_permission_protection")
    if component.get("manifest_tag") == "activity-alias":
        if component.get("permission_declared") is not None:
            permission = component.get("permission_declared")
            provenance.append({"source": "activity-alias", "field": "permission", "value": permission})
            return permission, provenance, component.get("permission_declared_protection")
        target = next((item for item in manifest.get("components", []) if item.get("name") == component.get("target_activity")), None)
        if target:
            target_permission = target.get("permission_declared")
            if target_permission is None:
                target_permission = target.get("permission")
            if target_permission:
                provenance.append({"source": "target_activity", "component": target.get("name"), "field": "permission", "value": target_permission})
                return target_permission, provenance, target.get("permission_protection")
        elif component.get("target_activity"):
            provenance.append({"source": "target_activity", "component": component.get("target_activity"), "status": "missing"})
    permission = component.get(field)
    hint = component.get(f"{field}_protection")
    if permission:
        provenance.append({"source": "component", "field": field, "value": permission})
        return permission, provenance, hint
    if direction in {"read", "write"}:
        permission = component.get("permission")
        hint = component.get("permission_protection")
        if permission:
            provenance.append({"source": "component", "field": "permission", "value": permission})
            return permission, provenance, hint
    app_permission = manifest.get("application_permission")
    if app_permission:
        provenance.append({"source": "application", "field": "permission", "value": app_permission})
        return app_permission, provenance, manifest.get("application_permission_protection")
    provenance.append({"source": "default", "value": None})
    return None, provenance, None


def _path_regions(component: dict[str, Any], path: str | None) -> list[dict[str, Any]]:
    policies = component.get("path_permissions", []) or []
    if path is not None:
        clean = _uri_path(path)
        matching = [policy for policy in policies if _path_matches(policy, clean)]
        return [
            {"region": _region(policy), "policy": policy}
            for policy in matching
        ] or [{"region": {"kind": "default", "value": clean}, "policy": None}]
    regions = [{"region": {"kind": "default", "value": "*"}, "policy": None}]
    regions.extend({"region": _region(policy), "policy": policy} for policy in policies)
    return regions


def _region(policy: dict[str, Any]) -> dict[str, Any]:
    for key, kind in (("path", "exact"), ("pathPrefix", "prefix"), ("pathPattern", "pattern")):
        if policy.get(key) is not None:
            return {"kind": kind, "value": policy[key]}
    return {"kind": "unknown", "value": None}


def _path_matches(policy: dict[str, Any], path: str) -> bool:
    if policy.get("path") is not None:
        return path == policy["path"]
    if policy.get("pathPrefix") is not None:
        return path.startswith(str(policy["pathPrefix"]))
    if policy.get("pathPattern") is not None:
        return bool(re.fullmatch(_android_simple_glob_regex(str(policy["pathPattern"])), path))
    return False


def _android_simple_glob_regex(pattern: str) -> str:
    """将 Android PatternMatcher SIMPLE_GLOB 转为等价的保守正则。

    SIMPLE_GLOB 中 ``.`` 匹配任意字符，``*`` 重复前一个原子；它不是 shell glob。
    """

    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            atom = re.escape(pattern[index + 1])
            index += 2
        elif char == ".":
            atom = "."
            index += 1
        else:
            atom = re.escape(char)
            index += 1
        if index < len(pattern) and pattern[index] == "*":
            atom = f"(?:{atom})*"
            index += 1
        parts.append(atom)
    return "".join(parts)


def _uri_path(value: str) -> str:
    parsed = urlparse(value)
    return parsed.path if parsed.scheme else value.split("?", 1)[0]


def _uri_grant_alternative(component: dict[str, Any], region: dict[str, Any], direction: str) -> dict[str, Any] | None:
    enabled = component.get("grant_uri_permissions")
    patterns = component.get("grant_uri_patterns", []) or []
    matched_pattern = any(
        region["region"].get("kind") == _region(pattern).get("kind")
        and region["region"].get("value") == _region(pattern).get("value")
        for pattern in patterns
    )
    if enabled is True or enabled == "true" or matched_pattern:
        return {
            "kind": "uri_grant", "status": "conditional", "direction": direction,
            "prerequisite": f"调用方持有 URI {direction} grant",
            "provenance": "grantUriPermissions" if enabled is True or enabled == "true" else "grant-uri-permission",
        }
    if enabled == "unknown":
        return {
            "kind": "uri_grant", "status": "unknown", "direction": direction,
            "prerequisite": f"URI {direction} grant 状态未知", "provenance": "grantUriPermissions_unknown",
        }
    return None


def _authority_resolution(manifest: dict[str, Any], component: dict[str, Any], path: str | None) -> dict[str, Any]:
    tokens = component.get("authority_tokens") or [
        token.strip() for token in str(component.get("authorities") or "").split(";") if token.strip()
    ]
    gaps = []
    if not tokens:
        return {"status": "not_applicable" if component.get("kind") != "provider" else "unknown", "blocking_gaps": []}
    if any("${" in token or token.startswith("@") for token in tokens):
        gaps.append({"code": "AUTHORITY_PLACEHOLDER_UNRESOLVED", "critical": True, "authorities": tokens})
    conflicts = manifest.get("authority_conflicts", {}) or {}
    conflicted = sorted(token for token in tokens if token in conflicts)
    if conflicted:
        gaps.append({"code": "DUPLICATE_PROVIDER_AUTHORITY", "critical": True, "authorities": conflicted})
    if path and urlparse(path).netloc and urlparse(path).netloc not in tokens:
        gaps.append({"code": "AUTHORITY_NOT_OWNED_BY_COMPONENT", "critical": True, "authority": urlparse(path).netloc})
    return {"status": "ambiguous" if gaps else "unique", "blocking_gaps": gaps}


def _reachability(component: dict[str, Any]) -> str:
    exported = component.get("exported")
    if exported in {True, "true"}:
        return "reachable"
    if exported in {False, "false"}:
        return "not_reachable"
    return "conditional"


def _prerequisites(status: str, permission: str | None) -> list[str]:
    if status == "unprotected":
        return ["普通第三方应用"]
    if status == "conditional":
        return [f"声明或获授权限 {permission}" if permission else "满足条件授权"]
    if status == "strongly_protected":
        return [f"与权限 {permission} 的授权主体满足强保护条件"]
    if status == "protected":
        return [f"持有权限 {permission}"]
    return ["权限保护级或授权目录项待确认"]


def _unique_gaps(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for gap in gaps:
        marker = (gap.get("code"), str(gap.get("permission")), str(gap.get("authorities")))
        if marker not in seen:
            seen.add(marker)
            result.append(gap)
    return result
