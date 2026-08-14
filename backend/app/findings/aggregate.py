"""合并指向同一组件与敏感操作的规则候选。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from app.findings.review_state import aggregate_review_states
from app.findings.severity import determine_severity


def aggregate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按组件、授权 scope 与 chain 身份聚合，并生成稳定 finding ID。

    聚合是 chain 级而非“同组件/同 Sink”拼接：Source、Sink、传播路径、dataflow 与 impact
    全部来自同一个 primary，其他成员只能补充位置、规则来源和保守状态/gap。这样不同入口
    或不同链不能被合成为不存在的闭合 Source→Sink 证明。
    """

    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for candidate in candidates:
        if candidate.get("auxiliary"):
            continue
        scope_key = str(candidate.get("scope_key") or _aggregate_scope_key(candidate))
        chain_key = str(candidate.get("chain_key") or _aggregate_chain_key(candidate))
        key = (
            str(candidate.get("component", "unknown")),
            str(candidate.get("component_name", "unknown")),
            scope_key,
            chain_key,
        )
        grouped.setdefault(key, []).append(candidate)
    findings = []
    for key, members in sorted(grouped.items()):
        primary = max(
            members,
            key=lambda item: (
                {"L1": 1, "L2": 2, "L3": 3}.get(item.get("evidence_level"), 0),
                item.get("deterministic_chain_verified") is True,
                str(item.get("rule_id") or ""),
                _canonical_json(item.get("sources") or []),
                _canonical_json(item.get("sinks") or []),
                _canonical_json(item.get("propagation_paths") or []),
            ),
        )
        merged = dict(primary)
        rule_ids = sorted({item["rule_id"] for item in members})
        merged["rule_ids"] = rule_ids
        merged["locations"] = _unique(members, "locations")
        # Source、Sink 与传播路径必须来自同一个 primary，禁止跨成员拼出虚假闭合链。
        merged["sources"] = list(primary.get("sources", []))
        merged["sinks"] = list(primary.get("sinks", []))
        merged["propagation_paths"] = list(primary.get("propagation_paths", []))
        merged["limitations"] = sorted({text for item in members for text in item.get("limitations", [])})
        merged["blocking_gaps"] = _unique(members, "blocking_gaps")
        merged["ai_blocking_gaps"] = _unique(members, "ai_blocking_gaps")
        merged["coverage_gaps"] = _unique(members, "coverage_gaps")
        merged["analysis_incomplete"] = any(item.get("analysis_incomplete") is True for item in members)
        merged["analysis_status"] = _aggregate_analysis_status(members)
        merged["dataflow_status"] = primary.get("dataflow_status", "not_proven")
        merged["authorization_status"] = _conservative_authorization_status(members)
        merged["guard_status"] = _conservative_guard_status(members)
        merged["authorization_matrix"] = _unique(members, "authorization_matrix")
        merged["impact_status"] = primary.get("impact_status", "potential")
        merged["deterministic_chain_verified"] = primary.get("deterministic_chain_verified") is True
        merged["review_priority"] = max(int(item.get("review_priority", 0)) for item in members)
        review_state = aggregate_review_states(members)
        merged["review_state"] = review_state
        merged["evidence_decision"] = review_state["evidence_decision"]
        merged["false_positive_basis"] = review_state["false_positive_basis"]
        severity, reasons = determine_severity(merged)
        stable = json.dumps({"key": key, "rules": rule_ids}, ensure_ascii=False, sort_keys=True)
        merged.update({
            "id": "finding_" + hashlib.sha256(stable.encode()).hexdigest()[:20],
            "title": primary.get("title") or _title(primary),
            "severity": severity,
            "severity_reason": reasons,
            "confidence": primary.get("confidence_tier", "medium"),
            "evidence_level": primary.get("evidence_level", "L1"),
            "dynamic_validation_status": primary.get("dynamic_validation_status", "not_executed"),
            "review_status": review_state["status"],
        })
        findings.append(merged)
    return findings


def _aggregate_scope_key(candidate: dict[str, Any]) -> str:
    return _stable_hash({
        "component": candidate.get("component"),
        "component_name": candidate.get("component_name"),
        "entry_method_id": candidate.get("entry_method_id"),
        "entry_points": _canonical_set(candidate.get("entry_points") or []),
        "authorization_operation": candidate.get("authorization_operation") or "component_entry",
        "authorization_regions": sorted(
            [
                row.get("path_region")
                for row in candidate.get("authorization_matrix", [])
                if isinstance(row, dict)
            ],
            key=_canonical_json,
        ),
    })


def _aggregate_chain_key(candidate: dict[str, Any]) -> str:
    return _stable_hash({
        "chain_id": candidate.get("chain_id"),
        "entry_method_id": candidate.get("entry_method_id"),
        "path_model": candidate.get("path_model"),
        "flow_kind": candidate.get("flow_kind"),
        "sources": candidate.get("sources") or [],
        "sinks": candidate.get("sinks") or [],
        "propagation_paths": candidate.get("propagation_paths") or [],
        "operation_taxonomy": candidate.get("operation_taxonomy") or candidate.get("taxonomy"),
    })


_SEMANTIC_SET_FIELDS = frozenset({
    "actions", "authorization_matrix", "duplicate_authorities", "entry_points", "operation_modes", "path_regions",
    "permission_set", "permissions", "requested_permissions", "rule_ids",
    "unresolved_action_expressions",
})


def _canonical_set(values: list[Any]) -> list[Any]:
    unique = {_canonical_json(value): value for value in values}
    return [unique[key] for key in sorted(unique)]


def _canonical_semantic_sets(value: Any, field_name: str = "") -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_semantic_sets(item, str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        projected = [_canonical_semantic_sets(item, field_name) for item in value]
        return _canonical_set(projected) if field_name in _SEMANTIC_SET_FIELDS else projected
    return value


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(_canonical_semantic_sets(value)).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _unique(members: list[dict], field: str) -> list:
    seen = set()
    result = []
    for member in members:
        for value in member.get(field, []):
            marker = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, dict) else str(value)
            if marker not in seen:
                seen.add(marker)
                result.append(value)
    return result


def _aggregate_analysis_status(members: list[dict[str, Any]]) -> str:
    """保守暴露 AI 失败、跳过和未完成，避免成功成员掩盖异常成员。"""

    statuses = {str(member.get("analysis_status", "rule_only")) for member in members}
    unsuccessful = statuses & {"ai_failed", "ai_skipped", "ai_incomplete"}
    successful = statuses & {"ai_completed", "human_confirmed"}
    if unsuccessful and successful:
        return "ai_partial"
    if "ai_failed" in unsuccessful:
        return "ai_failed"
    if "ai_incomplete" in unsuccessful:
        return "ai_incomplete"
    if "ai_skipped" in unsuccessful:
        return "ai_skipped"
    if "human_confirmed" in successful:
        return "human_confirmed"
    if "ai_completed" in successful:
        return "ai_completed"
    return "rule_only"


def _strongest_status(members: list[dict[str, Any]], field: str, order: list[str]) -> str:
    """按显式顺序选出聚合成员中证据最强的状态。"""

    ranking = {value: index for index, value in enumerate(order)}
    values = [member.get(field) for member in members if member.get(field) in ranking]
    return max(values, key=lambda value: ranking[value]) if values else order[0]


def _conservative_authorization_status(members: list[dict[str, Any]]) -> str:
    """最弱授权成员优先，禁止 primary 的强授权掩盖弱或未知成员。"""

    statuses = {str(member.get("authorization_status", "unknown")) for member in members}
    for status in ("unprotected", "conditional", "unknown", "protected", "strongly_protected"):
        if status in statuses:
            return status
    return "unknown"


def _conservative_guard_status(members: list[dict[str, Any]]) -> str:
    """合并 Guard 时将任一无 Guard 入口视为对有效 Guard 的旁路。"""

    statuses = {str(member.get("guard_status", "unknown")) for member in members}
    if "unknown" in statuses:
        return "unknown"
    if "present_partial" in statuses:
        return "present_partial"
    if "present_bypassable" in statuses:
        return "present_bypassable"
    if "absent" in statuses and "present_effective" in statuses:
        return "present_bypassable"
    if statuses == {"present_effective"}:
        return "present_effective"
    if "absent" in statuses:
        return "absent"
    return "unknown"


def _title(candidate: dict) -> str:
    component = candidate.get("component", "Android 组件")
    return f"{component} 静态安全候选：{candidate.get('rule_id', '未知规则')}"
