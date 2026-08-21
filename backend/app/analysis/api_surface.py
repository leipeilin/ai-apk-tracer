"""API 入口表生成（T2.2，方案 §2.1）。

读规则产物（T2.1 落盘 `rule-results/*.json`）+ manifest + code-index，
组装 `run_dir/api-surface/api_entry_table.json`——探索轨 Agent1 的确定性
"对外暴露 API 揭秘"输入。

设计：docs/analysis/2026-08-22-t2-2-implementation-plan.md
（含评审 R-1~R-9 修订：entry_method 实际格式 `name(params)->return` /
exported 四值域 / qualified_class 精确过滤 / lifecycle 补齐 / `__2` 去重 /
空数组产物容错 / 注册类语义标注）。

红线（方案 §2.0 L175）：只读 rule-results JSON 文件与 backend 自有
code-index——零 import 规则侧代码。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.config import ApiSurfaceSettings

LOGGER = logging.getLogger(__name__)

# 组件生命周期入口方法集合（外部可触发白名单；评审 R-4 补齐 provider.call/
# openAssetFile 与 service.onHandleIntent）
LIFECYCLE_METHODS: dict[str, set[str]] = {
    "activity": {"onCreate", "onStart", "onResume", "onNewIntent", "onActivityResult", "onRestart"},
    "service": {"onCreate", "onStartCommand", "onBind", "onHandleIntent"},
    "receiver": {"onReceive"},
    "provider": {"query", "insert", "update", "delete", "getType", "openFile", "openAssetFile", "call"},
}
_ENTRY_PREFIX = {"activity": "act", "service": "svc", "receiver": "rcv", "provider": "prv"}
_ILLEGAL_ID_CHARS = re.compile(r"[^A-Za-z0-9_]+")


def build_api_entry_table(
    run_dir: Path,
    manifest: dict[str, Any],
    settings: ApiSurfaceSettings,
    reader: Any | None,
) -> dict[str, Any]:
    """组装 API 入口表（全部确定性生成；产物缺失/空数组/解析不到时空值不伪造）。"""

    entries: list[dict[str, Any]] = []
    entries.extend(_manifest_entries(manifest, reader))
    if settings.include_binder:
        entries.extend(_binder_entries(run_dir, manifest))
    entries.extend(_dynrcv_entries(run_dir, manifest, reader))
    if settings.include_webview_jsbridge:
        entries.extend(_webview_entries(run_dir))
    _dedup_entry_ids(entries)
    return {
        "schema_version": "1.0.0",
        "package": manifest.get("package"),
        "api_entries": entries,
    }


# ---------------------------------------------------------------------------
# manifest 四类组件入口
# ---------------------------------------------------------------------------


def _manifest_entries(manifest: dict[str, Any], reader: Any | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for component in manifest.get("components", []):
        kind = str(component.get("kind") or "")
        if kind not in _ENTRY_PREFIX:
            continue
        name = str(component.get("name") or "")
        if not name:
            continue
        methods = _resolve_lifecycle_methods(reader, name, kind)
        base = _sanitize_id(name)
        exported = _map_exported(component)
        common = {
            "kind": kind,
            "component_name": name,
            "source": "manifest",
            "exported": exported,
            "exported_reason": component.get("exported_reason"),
            "permissions": [component["permission"]] if component.get("permission") else [],
            "intent_filters": component.get("intent_filters") or None,
            "authorities": component.get("authorities") if kind == "provider" else None,
            "reliability": "not_applicable",
        }
        if methods:
            for method in methods:
                entries.append({
                    "entry_id": f"{_ENTRY_PREFIX[kind]}_{base}_{_sanitize_id(method['name'])}",
                    "entry_method": f"{method['name']}{method['descriptor']}",
                    **common,
                })
        else:
            # 无方法解析（无 index/组件类不在索引）：单条组件级入口（不伪造方法）
            entries.append({
                "entry_id": f"{_ENTRY_PREFIX[kind]}_{base}",
                "entry_method": None,
                **common,
            })
    return entries


def _resolve_lifecycle_methods(reader: Any | None, component_fqcn: str, kind: str) -> list[dict[str, Any]]:
    """解析组件生命周期入口方法（评审 R-3：qualified_class 精确过滤防同简名异包误匹配）。"""

    if reader is None:
        return []
    wanted = LIFECYCLE_METHODS.get(kind, set())
    methods: list[dict[str, Any]] = []
    try:
        # component_files 返回的 file dict 已含 methods（_file_metadata 组装）
        for file in reader.component_files(component_fqcn):
            for method in file.get("methods", []):
                if (
                    method.get("name") in wanted
                    and str(method.get("qualified_class") or "") == component_fqcn
                ):
                    methods.append(method)
    except Exception:  # noqa: BLE001 - 容错边界：索引查询失败按无方法处理（不伪造）
        LOGGER.warning("组件入口方法解析失败", extra={"component": component_fqcn})
        return []
    # 稳定排序（name + descriptor）——同 entry_id 冲突时去重后缀确定
    return sorted(methods, key=lambda item: (item.get("name") or "", item.get("descriptor") or ""))


def _map_exported(component: dict[str, Any]) -> bool | None:
    """manifest exported 四值域映射（评审 R-2：conditional/unknown → None）。"""

    value = component.get("exported")
    if value == "true":
        return True
    if value == "false":
        return False
    return None


# ---------------------------------------------------------------------------
# 规则产物入口（T2.1 落盘 JSON → entry 转换）
# ---------------------------------------------------------------------------


def _load_artifact(run_dir: Path, name: str, entry_key: str) -> list[dict[str, Any]]:
    """读取规则产物（评审 R-5：缺失/空数组/损坏/信封不符统一容错空记录）。"""

    path = run_dir / "rule-results" / f"{name}.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text("utf-8"))
        records = payload.get(entry_key)
        return records if isinstance(records, list) else []
    except (json.JSONDecodeError, OSError):
        LOGGER.warning("规则产物读取失败（按空处理）", extra={"artifact": name})
        return []


def _binder_entries(run_dir: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Binder AIDL 入口：binder_bindings 产物 → reliability=resolve_status。"""

    component_exported: dict[str, bool | None] = {
        str(component.get("name") or ""): _map_exported(component)
        for component in manifest.get("components", [])
    }
    entries: list[dict[str, Any]] = []
    for binding in _load_artifact(run_dir, "binder_bindings", "bindings"):
        service_class = str(binding.get("service_class") or "")
        if not service_class:
            continue
        interface_method = binding.get("interface_method")
        method_token = _sanitize_id(str(interface_method)) if interface_method else f"code{binding.get('code')}"
        entries.append({
            "entry_id": f"binder_{_sanitize_id(service_class)}_{method_token}",
            "kind": "binder",
            "component_name": service_class,
            "source": "rule_artifact:binder_bindings",
            "exported": component_exported.get(service_class),  # 匹配不到 null（不伪造）
            "interface_method": interface_method,
            "transaction_code": binding.get("code"),
            "implementation_method_id": binding.get("implementation_method_id"),
            "reliability": binding.get("resolve_status") or "unresolved",
        })
    return entries


def _dynrcv_entries(run_dir: Path, manifest: dict[str, Any], reader: Any | None) -> list[dict[str, Any]]:
    """动态 Receiver 入口：receiver_registrations 产物（恒含——动态注册面核心）。"""

    entries: list[dict[str, Any]] = []
    for registration in _load_artifact(run_dir, "receiver_registrations", "registrations"):
        receiver_class = registration.get("receiver_class")
        component_name = str(receiver_class) if receiver_class else (
            _fqcn_from_path(str(registration.get("path") or "")) or "unknown"
        )
        method_token = _sanitize_id(str(registration.get("method_name") or "register"))
        export_status = registration.get("export_status")
        if export_status == "legacy_unspecified":
            export_status = "unknown"  # 转换层职责：api_entry_table 无 legacy 枚举
        methods = (
            _resolve_lifecycle_methods(reader, str(receiver_class), "receiver")
            if receiver_class else []
        )
        if methods:
            for method in methods:
                entries.append({
                    "entry_id": f"dynrcv_{_sanitize_id(component_name)}_{_sanitize_id(method['name'])}",
                    "kind": "receiver",
                    "component_name": component_name,
                    "source": "rule_artifact:receiver_registrations",
                    "entry_method": f"{method['name']}{method['descriptor']}",
                    "actions": registration.get("actions") or [],
                    "export_status": export_status,
                    "externally_reachable": registration.get("externally_reachable"),
                })
        else:
            entries.append({
                "entry_id": f"dynrcv_{_sanitize_id(component_name)}_{method_token}",
                "kind": "receiver",
                "component_name": component_name,
                "source": "rule_artifact:receiver_registrations",
                "entry_method": None,
                "actions": registration.get("actions") or [],
                "export_status": export_status,
                "externally_reachable": registration.get("externally_reachable"),
            })
    return entries


def _webview_entries(run_dir: Path) -> list[dict[str, Any]]:
    """WebView JS bridge 入口。

    component_name 语义（评审 R-7）：产物 path 指向**注册调用类**（调用
    addJavascriptInterface 的类），非桥对象类——产物不含桥类型；T2.3/Agent1
    消费时须注意此语义。
    """

    entries: list[dict[str, Any]] = []
    for bridge in _load_artifact(run_dir, "webview_js_bridges", "bridges"):
        component_name = _fqcn_from_path(str(bridge.get("path") or "")) or "unknown"
        bridge_name = bridge.get("bridge_name")
        entries.append({
            "entry_id": (
                f"webview_{_sanitize_id(component_name)}_{_sanitize_id(str(bridge_name))}"
                if bridge_name else f"webview_{_sanitize_id(component_name)}"
            ),
            "kind": "webview_bridge",
            "component_name": component_name,
            "source": "rule_artifact:webview_js_bridges",
            "bridge_path": bridge.get("path"),
            "bridge_line": bridge.get("line"),
            "bridge_name": bridge_name,
            "reliability": "not_applicable",
        })
    return entries


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _sanitize_id(value: str) -> str:
    """entry_id 合法化：非 [A-Za-z0-9_] 字符（点/$ 等）→ 下划线（连续折叠）。"""

    return _ILLEGAL_ID_CHARS.sub("_", value).strip("_") or "x"


def _fqcn_from_path(path: str) -> str | None:
    """源码相对路径 → FQCN（条件式剥离 sources/ 前缀，评审 R-7；.java → .）。"""

    if not path:
        return None
    normalized = path.replace("\\", "/")
    normalized = normalized.removeprefix("sources/")
    if normalized.endswith(".java"):
        normalized = normalized.removesuffix(".java")
    return normalized.replace("/", ".") or None


def _dedup_entry_ids(entries: list[dict[str, Any]]) -> None:
    """entry_id 冲突去重：双下划线序号后缀（评审 R-6：`__2` 防与合法方法名 `_2` 撞车）。"""

    seen: dict[str, int] = {}
    for entry in entries:
        entry_id = entry["entry_id"]
        if entry_id in seen:
            seen[entry_id] += 1
            entry["entry_id"] = f"{entry_id}__{seen[entry_id]}"
        else:
            seen[entry_id] = 1
