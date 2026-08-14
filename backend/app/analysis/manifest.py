"""解析解码后的 Android Manifest，并推导组件的有效导出状态。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from app.shared.errors import ValidationError

ANDROID_NS = "http://schemas.android.com/apk/res/android"
A = f"{{{ANDROID_NS}}}"
COMPONENT_TAGS = {
    "activity": "activity",
    "activity-alias": "activity",
    "service": "service",
    "provider": "provider",
    "receiver": "receiver",
}
PROTECTED_BROADCAST_CATALOG_VERSION = "android-api-36-minimal-1"
PROTECTED_BROADCAST_ACTIONS = {
    "android.intent.action.BOOT_COMPLETED",
    "android.intent.action.LOCKED_BOOT_COMPLETED",
    "android.intent.action.PACKAGE_ADDED",
    "android.intent.action.PACKAGE_REMOVED",
    "android.intent.action.PACKAGE_REPLACED",
    "android.intent.action.PACKAGE_CHANGED",
    "android.intent.action.UID_REMOVED",
    "android.intent.action.USER_ADDED",
    "android.intent.action.USER_REMOVED",
    "android.intent.action.USER_SWITCHED",
    "android.intent.action.SIM_STATE_CHANGED",
    "android.provider.Telephony.SMS_RECEIVED",
}


def _attr(element: ET.Element, name: str) -> str | None:
    return element.get(A + name)


def _bool(value: str | None) -> bool | str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return "unknown"


def parse_manifest(path: Path, analysis_platform_api: int = 36) -> dict[str, Any]:
    """解析 Manifest 元数据，并按目标 SDK 与分析平台推导组件属性。

    无法读取或解析 XML 时抛出 ``ValidationError``。
    """

    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ValidationError("无法解析解码后的 AndroidManifest.xml", "MANIFEST_PARSE_FAILED") from exc
    package = root.get("package") or "未获取"
    uses_sdk = root.find("uses-sdk")
    target_sdk = _int_attr(uses_sdk, "targetSdkVersion") if uses_sdk is not None else None
    min_sdk = _int_attr(uses_sdk, "minSdkVersion") if uses_sdk is not None else None
    permissions: dict[str, str] = {}
    permission_declarations: dict[str, dict[str, Any]] = {}
    for permission in root.findall("permission"):
        name = _attr(permission, "name")
        if name:
            protection_level = _attr(permission, "protectionLevel") or "normal"
            permissions[name] = protection_level
            permission_declarations[name] = {
                "protection_level": protection_level,
                "provenance": "AndroidManifest.xml/permission",
            }
    application = root.find("application")
    app_permission = _attr(application, "permission") if application is not None else None
    app_permission_protection = _permission_protection(app_permission, permissions)
    # application 级安全属性。AI 判定 debug 分支可达性、备份数据泄露与明文流量时需要这些事实，
    # 它们只存在于 Manifest 而不在代码索引中；缺失时保持 None，由下游显式标记 unknown。
    # 注意 debuggable 未声明时平台默认 false，不可当作 unknown 处理。
    app_debuggable = _bool(_attr(application, "debuggable")) if application is not None else None
    app_allow_backup = _bool(_attr(application, "allowBackup")) if application is not None else None
    app_cleartext = _bool(_attr(application, "usesCleartextTraffic")) if application is not None else None
    components: list[dict[str, Any]] = []
    if application is not None:
        for child in application:
            if child.tag not in COMPONENT_TAGS:
                continue
            filters = []
            for intent_filter in child.findall("intent-filter"):
                filters.append({
                    "actions": [_attr(node, "name") for node in intent_filter.findall("action") if _attr(node, "name")],
                    "categories": [_attr(node, "name") for node in intent_filter.findall("category") if _attr(node, "name")],
                    "data": [{_local(k): v for k, v in node.attrib.items()} for node in intent_filter.findall("data")],
                })
            explicit_exported = _bool(_attr(child, "exported"))
            component_type = COMPONENT_TAGS[child.tag]
            exported, reason = effective_exported(
                component_type, explicit_exported, bool(filters), target_sdk, analysis_platform_api
            )
            permission_declared = _attr(child, "permission")
            permission = permission_declared or app_permission
            read_permission = _attr(child, "readPermission")
            write_permission = _attr(child, "writePermission")
            path_permissions = []
            for node in child.findall("path-permission"):
                values = {_local(k): v for k, v in node.attrib.items()}
                values["read_permission_protection"] = _permission_protection(
                    values.get("readPermission") or permission, permissions
                )
                values["write_permission_protection"] = _permission_protection(
                    values.get("writePermission") or permission, permissions
                )
                path_permissions.append(values)
            metadata = [
                {
                    "name": _attr(node, "name"),
                    "resource": _attr(node, "resource"),
                    "value": _attr(node, "value"),
                }
                for node in child.findall("meta-data")
            ]
            grant_uri_patterns = [
                {_local(k): v for k, v in node.attrib.items()}
                for node in child.findall("grant-uri-permission")
            ]
            provider_paths = _provider_paths(path.parent, metadata) if component_type == "provider" else []
            component = {
                "kind": component_type,
                "manifest_tag": child.tag,
                "name": _qualify(package, _attr(child, "name") or ""),
                "target_activity": _qualify(package, _attr(child, "targetActivity") or "") or None,
                "explicit_exported": explicit_exported,
                "exported": exported,
                "exported_reason": reason,
                "permission": permission,
                "permission_declared": permission_declared,
                "permission_provenance": "component" if permission_declared else ("application" if app_permission else "none"),
                "permission_protection": _permission_protection(permission, permissions),
                "permission_declared_protection": _permission_protection(permission_declared, permissions),
                "read_permission": read_permission,
                "read_permission_protection": _permission_protection(read_permission or permission, permissions),
                "write_permission": write_permission,
                "write_permission_protection": _permission_protection(write_permission or permission, permissions),
                "grant_uri_permissions": _bool(_attr(child, "grantUriPermissions")),
                "authorities": _attr(child, "authorities"),
                "authority_tokens": _authority_tokens(_attr(child, "authorities")),
                "intent_filters": filters,
                "broadcast_action_authorization": _broadcast_action_authorization(filters),
                "path_permissions": path_permissions,
                "grant_uri_patterns": grant_uri_patterns,
                "metadata": metadata,
                "provider_paths": provider_paths,
            }
            components.append(component)
    components_by_name = {component["name"]: component for component in components}
    for component in components:
        if component.get("manifest_tag") != "activity-alias":
            continue
        target = components_by_name.get(component.get("target_activity"))
        target_permission = target.get("permission") if target else None
        component["target_permission"] = target_permission
        component["target_permission_protection"] = target.get("permission_protection") if target else None
        if component.get("permission_declared") is None and target_permission:
            component["permission"] = target_permission
            component["permission_protection"] = target.get("permission_protection")
            component["permission_provenance"] = "target_activity"
        elif component.get("permission_declared") is None and app_permission:
            component["permission_provenance"] = "application"
    authority_owners: dict[str, list[str]] = {}
    for component in components:
        if component.get("kind") != "provider":
            continue
        for authority in component.get("authority_tokens", []):
            authority_owners.setdefault(authority, []).append(component["name"])
    authority_conflicts = {
        authority: sorted(owners) for authority, owners in authority_owners.items() if len(owners) > 1
    }
    return {
        "schema_version": "1.0.0",
        "package": package,
        "version_code": root.get(A + "versionCode") or "未获取",
        "version_name": root.get(A + "versionName") or "未获取",
        "compile_sdk_version": root.get(A + "compileSdkVersion") or "未获取",
        "compile_sdk_codename": root.get(A + "compileSdkVersionCodename") or "未获取",
        "min_sdk": min_sdk,
        "target_sdk": target_sdk,
        "analysis_platform_api": analysis_platform_api,
        "debuggable": False if app_debuggable is None else app_debuggable,
        "allow_backup": app_allow_backup,
        "uses_cleartext_traffic": app_cleartext,
        "application_permission": app_permission,
        "application_permission_protection": app_permission_protection,
        "custom_permissions": permissions,
        "permission_declarations": permission_declarations,
        "protected_broadcast_catalog_version": PROTECTED_BROADCAST_CATALOG_VERSION,
        "protected_broadcast_actions": sorted(PROTECTED_BROADCAST_ACTIONS),
        "uses_permissions": [_attr(node, "name") for node in root.findall("uses-permission") if _attr(node, "name")],
        "authority_conflicts": authority_conflicts,
        "components": components,
    }


def _provider_paths(manifest_dir: Path, metadata: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """解析 Provider meta-data 引用的 paths XML，并保留可访问根类型。"""

    roots: list[dict[str, Any]] = []
    for item in metadata:
        resource = item.get("resource")
        if not isinstance(resource, str) or not resource.startswith("@xml/"):
            continue
        resource_name = resource.split("/", 1)[1]
        candidates = [
            manifest_dir / "res" / "xml" / f"{resource_name}.xml",
            manifest_dir.parent / "resources" / "res" / "xml" / f"{resource_name}.xml",
        ]
        xml_path = next((candidate for candidate in candidates if candidate.is_file() and not candidate.is_symlink()), None)
        if xml_path is None:
            roots.append({"resource": resource, "status": "missing"})
            continue
        try:
            xml_root = ET.parse(xml_path).getroot()
        except (ET.ParseError, OSError):
            roots.append({"resource": resource, "status": "invalid"})
            continue
        for node in xml_root:
            roots.append({
                "resource": resource,
                "status": "parsed",
                "type": _local(node.tag),
                "name": _attr(node, "name"),
                "path": _attr(node, "path") or ".",
                "source": xml_path.as_posix(),
            })
    return roots


def effective_exported(kind: str, explicit: bool | str | None, has_filter: bool, target_sdk: int | None, platform_api: int) -> tuple[str, str]:
    """按 Android 组件类型和 SDK 规则返回有效导出状态及推导依据。"""

    if explicit == "unknown":
        return "unknown", "manifest_explicit_unresolved"
    if explicit is not None:
        return ("true" if explicit else "false", "manifest_explicit")
    if kind == "provider":
        if target_sdk is None:
            return "unknown", "target_sdk_missing"
        return ("true", "provider_default_target_sdk_lt_17") if target_sdk < 17 else ("false", "provider_default_target_sdk_gte_17")
    if has_filter:
        if (target_sdk or 0) >= 31 and platform_api >= 31:
            return "conditional", "android_12_requires_explicit_exported"
        return "true", "intent_filter_default"
    return "false", "no_intent_filter_default"


def _permission_protection(name: str | None, custom_permissions: dict[str, str]) -> str | None:
    """返回自定义权限 protectionLevel；平台或依赖权限保守标记为未知强度。"""

    if not name:
        return None
    return custom_permissions.get(name, "platform_or_unknown")


def _authority_tokens(value: str | None) -> list[str]:
    """规范化多 authority 声明，保留无法展开的 placeholder。"""

    if not value:
        return []
    return list(dict.fromkeys(token.strip().lower() for token in value.split(";") if token.strip()))


def _broadcast_action_authorization(filters: list[dict[str, Any]]) -> list[dict[str, str]]:
    """仅按版本化最小目录确认 protected broadcast，未知 action 不作前缀推断。"""

    actions = [action for row in filters for action in row.get("actions", [])]
    return [
        {
            "action": action,
            "status": "protected" if action in PROTECTED_BROADCAST_ACTIONS else "unknown",
            "catalog": PROTECTED_BROADCAST_CATALOG_VERSION,
        }
        for action in actions
    ]


def _qualify(package: str, name: str) -> str:
    if not name:
        return ""
    if name.startswith("."):
        return package + name
    if "." not in name:
        return f"{package}.{name}"
    return name


def _local(name: str) -> str:
    return name.split("}", 1)[-1]


def _int_attr(element: ET.Element, name: str) -> int | None:
    value = _attr(element, name)
    try:
        return int(value) if value else None
    except ValueError:
        return None
