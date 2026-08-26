"""四组件攻击面导出（T2.3，方案 §2.3）。

产出 `run_dir/attack_surface/{activity,service,provider,receiver}.json`
——探索轨 Agent1 的"从攻击面出发"确定性输入。receiver 合并静态（manifest）
与动态（T2.1 receiver_registrations 产物）注册。

设计：docs/analysis/explorer-track/2026-08-22-t2-3-implementation-plan.md
（含评审 R-1~R-7 修订：exported 保守统一高估 / T0.5 样例勘误 /
entry_methods 含 dynrcv / reason 组合标注 / provider 读写权限透传 /
binder 无挂靠声明 / auxiliary 含入聚合）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

_KIND_FILES = ("activity", "service", "provider", "receiver")


def build_attack_surfaces(
    run_dir: Path,
    manifest: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """组装四组件攻击面 payload（全部确定性生成）。

    数据流：manifest 组件 → 四类条目（receiver 合并动态注册）；
    api_entry_table（T2.2 产物，只读容错）→ api_entry_refs + entry_methods；
    candidates → sensitive_capabilities（component_name 精确匹配聚合）。
    返回 {文件名: payload}；四文件恒生成（无该类组件时 components=[]，
    Agent1 输入面稳定）。
    """

    entries = _load_entry_table(run_dir)
    registrations = _load_registrations(run_dir)
    capabilities = _aggregate_capabilities(candidates)

    components: dict[str, list[dict[str, Any]]] = {kind: [] for kind in _KIND_FILES}
    for component in manifest.get("components", []):
        kind = str(component.get("kind") or "")
        if kind not in _KIND_FILES:
            continue
        name = str(component.get("name") or "")
        if not name:
            continue
        entry = _manifest_component_entry(component, name, entries, capabilities.get(name, []))
        if kind == "receiver":
            # 评审 D4：类名键合并静态与动态注册
            dynamics = [r for r in registrations if str(r.get("receiver_class") or "") == name]
            if dynamics:
                _merge_dynamic_into_entry(entry, dynamics, entries)
        components[kind].append(entry)

    # 纯动态 receiver（类名不在 manifest 静态清单）
    static_names = {
        str(component.get("name") or "")
        for component in manifest.get("components", [])
        if component.get("kind") == "receiver"
    }
    for registration in registrations:
        receiver_class = str(registration.get("receiver_class") or "")
        if not receiver_class or receiver_class in static_names:
            continue
        components["receiver"].append(
            _dynamic_only_entry(registration, entries)
        )

    package = manifest.get("package")
    return {
        kind: {"schema_version": "1.0.0", "package": package, "components": items}
        for kind, items in components.items()
    }


# ---------------------------------------------------------------------------
# manifest 组件条目
# ---------------------------------------------------------------------------


def _manifest_component_entry(
    component: dict[str, Any],
    name: str,
    entries: list[dict[str, Any]],
    capabilities: list[str],
) -> dict[str, Any]:
    kind = str(component.get("kind"))
    related = [entry for entry in entries if entry.get("component_name") == name]
    entry_methods = sorted({
        entry["entry_method"]
        for entry in related
        if entry.get("entry_method") and entry.get("source") in {"manifest", "rule_artifact:receiver_registrations"}
    })
    refs = [entry["entry_id"] for entry in related]
    entry: dict[str, Any] = {
        "kind": kind,
        "name": name,
        "exported": _map_manifest_exported(component),
        "exported_reason": component.get("exported_reason"),
        "permission": component.get("permission"),
        "permission_protection": component.get("permission_protection"),
        "entry_methods": entry_methods,
        "intent_filters": component.get("intent_filters") or None,
        "sensitive_capabilities": capabilities,
        "api_entry_refs": refs,
        "source": "manifest",
    }
    if kind == "provider":
        entry["authorities"] = component.get("authorities")
        # 评审 R-6：读写权限粒度透传（主 permission 之外的可审计补充）
        entry["read_permission"] = component.get("read_permission")
        entry["write_permission"] = component.get("write_permission")
    if kind == "receiver":
        actions = sorted({
            action
            for intent_filter in (component.get("intent_filters") or [])
            for action in (intent_filter.get("actions") or [])
        })
        entry["actions"] = actions or None
    return entry


def _map_manifest_exported(component: dict[str, Any]) -> bool:
    """攻击面保守高估（D2）：conditional/unknown → True（覆盖旧设备/未知保守）。"""

    value = component.get("exported")
    return value == "true" or value in {"conditional", "unknown"}


# ---------------------------------------------------------------------------
# 动态 receiver（T2.1 产物）
# ---------------------------------------------------------------------------


def _dynamic_exported(registration: dict[str, Any]) -> bool:
    """评审 R-1：None（未知）→ True——与 manifest 侧保守方向统一。"""

    return registration.get("externally_reachable") is not False


def _merge_dynamic_into_entry(
    entry: dict[str, Any],
    dynamics: list[dict[str, Any]],
    entries: list[dict[str, Any]],
) -> None:
    """静态条目合并动态注册（D4）：source/actions/refs 并集 + exported OR。"""

    entry["source"] = "manifest+dynamic"
    static_exported = entry["exported"]
    dynamic_exported = any(_dynamic_exported(r) for r in dynamics)
    # 任一可达即可达；reason 组合标注（评审 R-5：防 True 来源为动态时静态 reason 误导）
    entry["exported"] = static_exported or dynamic_exported
    dynamic_reason = ";".join(
        f"{r.get('export_status') or 'unknown'}/reachable={r.get('externally_reachable')}"
        for r in dynamics
    )
    entry["exported_reason"] = f"static:{entry.get('exported_reason')};dynamic:{dynamic_reason}"
    dynamic_actions = sorted({
        action for registration in dynamics for action in (registration.get("actions") or [])
    })
    existing_actions = set(entry.get("actions") or [])
    entry["actions"] = sorted(existing_actions | set(dynamic_actions)) or None
    dynamic_refs = {
        e["entry_id"] for e in entries
        if e.get("source") == "rule_artifact:receiver_registrations"
        and e.get("component_name") == entry["name"]
    }
    entry["api_entry_refs"] = list(dict.fromkeys([*entry.get("api_entry_refs", []), *sorted(dynamic_refs)]))
    entry["dynamic_registrations"] = [
        {
            "export_status": r.get("export_status"),
            "externally_reachable": r.get("externally_reachable"),
        }
        for r in dynamics
    ]


def _dynamic_only_entry(
    registration: dict[str, Any],
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    receiver_class = str(registration.get("receiver_class") or "")
    refs = [
        e["entry_id"] for e in entries
        if e.get("source") == "rule_artifact:receiver_registrations"
        and e.get("component_name") == receiver_class
    ]
    entry_methods = sorted({
        e["entry_method"] for e in entries
        if e.get("source") == "rule_artifact:receiver_registrations"
        and e.get("component_name") == receiver_class
        and e.get("entry_method")
    })
    return {
        "kind": "receiver",
        "name": receiver_class,
        "exported": _dynamic_exported(registration),
        "exported_reason": (
            f"dynamic:{registration.get('export_status') or 'unknown'}"
            f"/reachable={registration.get('externally_reachable')}"
        ),
        "permission": None,
        "permission_protection": None,
        "entry_methods": entry_methods,
        "intent_filters": None,
        "actions": sorted(registration.get("actions") or []) or None,
        "sensitive_capabilities": [],
        "api_entry_refs": refs,
        "source": "dynamic",
        "dynamic_registrations": [{
            "export_status": registration.get("export_status"),
            "externally_reachable": registration.get("externally_reachable"),
        }],
    }


# ---------------------------------------------------------------------------
# 数据源加载与聚合
# ---------------------------------------------------------------------------


def _load_entry_table(run_dir: Path) -> list[dict[str, Any]]:
    """读 T2.2 api_entry_table 产物（容错：缺失/损坏/结构不符 → 空列表）。"""

    path = run_dir / "api-surface" / "api_entry_table.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text("utf-8"))
        entries = payload.get("api_entries")
        return entries if isinstance(entries, list) else []
    except (json.JSONDecodeError, OSError):
        LOGGER.warning("api_entry_table 读取失败（按空处理）")
        return []


def _load_registrations(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "rule-results" / "receiver_registrations.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text("utf-8"))
        registrations = payload.get("registrations")
        return registrations if isinstance(registrations, list) else []
    except (json.JSONDecodeError, OSError):
        LOGGER.warning("receiver_registrations 读取失败（按空处理）")
        return []


def _aggregate_capabilities(candidates: list[dict[str, Any]]) -> dict[str, list[str]]:
    """敏感能力聚合（D3）：component_name 精确匹配组件名；全局规则（dynamic:
    path）不入组件——不伪造归属；auxiliary 候选含入（rule_id 自带语义可辨）。"""

    capabilities: dict[str, set[str]] = {}
    for candidate in candidates:
        component_name = str(candidate.get("component_name") or "")
        rule_id = str(candidate.get("rule_id") or "")
        if not component_name or not rule_id or component_name.startswith("dynamic:"):
            continue
        capabilities.setdefault(component_name, set()).add(rule_id)
    return {name: sorted(rules) for name, rules in capabilities.items()}
