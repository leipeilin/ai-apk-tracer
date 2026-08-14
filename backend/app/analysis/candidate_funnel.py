"""Pure deterministic candidate deduplication and routing decisions."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, TypeAlias

Candidate: TypeAlias = Mapping[str, Any]
CANDIDATE_KEY_SCHEMA = "candidate-funnel-exact-v1"


class CandidateRoute(StrEnum):
    """The only candidate destinations understood by the funnel boundary."""

    NONE = "none"
    L1_TRIAGE = "l1_triage"
    L2_REVIEW = "l2_review"


class CandidateReason(StrEnum):
    """Auditable reasons for a deterministic routing decision."""

    AUXILIARY = "AUXILIARY_CANDIDATE"
    L1_UNREACHABLE = "L1_UNREACHABLE"
    L1_PROTECTED = "L1_PROTECTED"
    L1_COMPONENT_NAME_MISSING = "L1_COMPONENT_NAME_MISSING"
    L1_ELIGIBLE = "L1_TRIAGE_ELIGIBLE"
    L2_ELIGIBLE = "L2_REVIEW_ELIGIBLE"
    EVIDENCE_LEVEL_UNKNOWN = "EVIDENCE_LEVEL_UNKNOWN"


@dataclass(frozen=True, slots=True)
class CandidatePrecheck:
    """A complete, deterministic eligibility decision for one candidate."""

    eligible: bool
    route: CandidateRoute
    reason_code: CandidateReason


@dataclass(frozen=True, slots=True)
class ExactCandidateGroup:
    """One first-seen candidate and the input indexes exactly equal to it."""

    candidate_key: str
    representative_index: int
    duplicate_indexes: tuple[int, ...]

    @property
    def original_indexes(self) -> tuple[int, ...]:
        return (self.representative_index, *self.duplicate_indexes)


@dataclass(frozen=True, slots=True)
class DuplicateProvenance:
    """Trace one discarded duplicate back to its first-seen representative."""

    candidate_index: int
    representative_index: int
    candidate_key: str


@dataclass(frozen=True, slots=True)
class CandidateRoutingEntry:
    """The route and source provenance for one exact-candidate group."""

    candidate_key: str
    representative_index: int
    duplicate_indexes: tuple[int, ...]
    original_indexes: tuple[int, ...]
    eligible: bool
    route: CandidateRoute
    reason_code: CandidateReason


@dataclass(frozen=True, slots=True)
class CandidateRoutingPlan:
    """Order-preserving unique candidates and their duplicate provenance."""

    entries: tuple[CandidateRoutingEntry, ...]
    duplicate_provenance: tuple[DuplicateProvenance, ...]

    def for_route(self, route: CandidateRoute | str) -> tuple[CandidateRoutingEntry, ...]:
        selected = CandidateRoute(route)
        return tuple(entry for entry in self.entries if entry.route is selected)

    @property
    def representative_indexes(self) -> tuple[int, ...]:
        return tuple(entry.representative_index for entry in self.entries)


# These fields are produced or changed after deterministic rule execution. They
# cannot participate in identity because retries, AI availability, or review
# activity would otherwise change a candidate's key.
_TOP_LEVEL_MUTABLE_FIELDS = frozenset({
    "analysis_incomplete",
    "analysis_status",
    "analysis_track",
    "candidate_id",
    "candidate_verdict",
    "chain_key",
    "deterministic_fact_hash",
    "evidence_decision",
    "evidence_variants",
    "false_positive_basis",
    "funnel_disposition",
    "is_ai_representative",
    "member_candidate_ids",
    "representative_candidate_id",
    "review_state",
    "scope_key",
    "confidence_tier",
    "context_requests",
    "duration_ms",
    "error",
    "errors",
    "final_analysis",
    "promotion_requested",
    "protocol_version",
    "review_status",
    "risk_score",
    "runtime",
    "severity",
    "severity_hint",
    "severity_reason",
    "severity_version",
    "slice_id",
    "slice_refs",
})
_RECURSIVE_RUNTIME_FIELDS = frozenset({
    "elapsed_ms",
    "last_error",
    "runtime_error",
    "timestamp",
    "transient_error",
    "transient_errors",
})
_SET_LIKE_LIST_FIELDS = frozenset({
    "actions",
    "authorization_matrix",
    "binder_return_types",
    "duplicate_authorities",
    "entry_points",
    "operation_modes",
    "path_regions",
    "permission_set",
    "permissions",
    "requested_permissions",
    "rule_ids",
    "unresolved_action_expressions",
})


def candidate_precheck(candidate: Candidate) -> CandidatePrecheck:
    """Return the deterministic route and auditable reason for ``candidate``."""

    if candidate.get("auxiliary"):
        return CandidatePrecheck(False, CandidateRoute.NONE, CandidateReason.AUXILIARY)

    evidence_level = candidate.get("evidence_level")
    if evidence_level == "L2":
        return CandidatePrecheck(True, CandidateRoute.L2_REVIEW, CandidateReason.L2_ELIGIBLE)
    if evidence_level != "L1":
        return CandidatePrecheck(
            False,
            CandidateRoute.NONE,
            CandidateReason.EVIDENCE_LEVEL_UNKNOWN,
        )

    if candidate.get("reachability_status") not in {None, "", "reachable", "conditional"}:
        return CandidatePrecheck(False, CandidateRoute.NONE, CandidateReason.L1_UNREACHABLE)
    if candidate.get("authorization_status") in {"protected", "strongly_protected"}:
        return CandidatePrecheck(False, CandidateRoute.NONE, CandidateReason.L1_PROTECTED)
    component_name = candidate.get("component_name")
    if not isinstance(component_name, str) or not component_name.strip():
        return CandidatePrecheck(
            False,
            CandidateRoute.NONE,
            CandidateReason.L1_COMPONENT_NAME_MISSING,
        )
    return CandidatePrecheck(True, CandidateRoute.L1_TRIAGE, CandidateReason.L1_ELIGIBLE)


def canonical_candidate_projection(candidate: Candidate) -> dict[str, Any]:
    """Build the JSON-compatible deterministic projection used for exact identity."""

    if not isinstance(candidate, Mapping):
        raise TypeError("candidate must be a mapping")
    return _project_mapping(candidate, top_level=True)


def exact_candidate_key(candidate: Candidate) -> str:
    """Return a stable SHA-256 key for the candidate's deterministic projection."""

    envelope = {
        "schema": CANDIDATE_KEY_SCHEMA,
        "candidate": canonical_candidate_projection(candidate),
    }
    canonical_json = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def deduplicate_exact_candidates(candidates: Sequence[Candidate]) -> tuple[ExactCandidateGroup, ...]:
    """Group only exact candidates, preserving their first-seen input order."""

    group_positions: dict[str, int] = {}
    group_values: list[tuple[str, int, list[int]]] = []
    for candidate_index, candidate in enumerate(candidates):
        key = exact_candidate_key(candidate)
        position = group_positions.get(key)
        if position is None:
            group_positions[key] = len(group_values)
            group_values.append((key, candidate_index, []))
        else:
            group_values[position][2].append(candidate_index)
    return tuple(
        ExactCandidateGroup(
            candidate_key=key,
            representative_index=representative_index,
            duplicate_indexes=tuple(duplicate_indexes),
        )
        for key, representative_index, duplicate_indexes in group_values
    )


def build_candidate_routing_plan(candidates: Sequence[Candidate]) -> CandidateRoutingPlan:
    """Deduplicate and route candidates without modifying the input sequence or mappings."""

    entries: list[CandidateRoutingEntry] = []
    provenance: list[DuplicateProvenance] = []
    for group in deduplicate_exact_candidates(candidates):
        decision = candidate_precheck(candidates[group.representative_index])
        entries.append(CandidateRoutingEntry(
            candidate_key=group.candidate_key,
            representative_index=group.representative_index,
            duplicate_indexes=group.duplicate_indexes,
            original_indexes=group.original_indexes,
            eligible=decision.eligible,
            route=decision.route,
            reason_code=decision.reason_code,
        ))
        provenance.extend(
            DuplicateProvenance(
                candidate_index=index,
                representative_index=group.representative_index,
                candidate_key=group.candidate_key,
            )
            for index in group.duplicate_indexes
        )
    provenance.sort(key=lambda item: item.candidate_index)
    return CandidateRoutingPlan(tuple(entries), tuple(provenance))


def _project_mapping(value: Mapping[str, Any], *, top_level: bool) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("candidate mappings must use string keys")
        if _excluded_field(key, top_level=top_level):
            continue
        projected[key] = _project_value(item, field_name=key)
    return projected


def _project_value(value: Any, *, field_name: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("candidate numbers must be finite")
        return value
    if isinstance(value, Mapping):
        return _project_mapping(value, top_level=False)
    if isinstance(value, (list, tuple)):
        projected = [_project_value(item, field_name=field_name) for item in value]
        if field_name in _SET_LIKE_LIST_FIELDS:
            unique = {_canonical_json_value(item): item for item in projected}
            projected = [unique[key] for key in sorted(unique)]
        return projected
    raise TypeError(f"candidate field {field_name!r} is not JSON-compatible")


def _excluded_field(key: str, *, top_level: bool) -> bool:
    if key in _RECURSIVE_RUNTIME_FIELDS:
        return True
    if key.endswith(("_at", "_timestamp", "_timestamp_ms")) or key.startswith("timestamp"):
        return True
    if not top_level:
        return False
    if key in _TOP_LEVEL_MUTABLE_FIELDS:
        return True
    if key.startswith(("ai_", "slice_", "transient_", "severity_")):
        return True
    return key.endswith(("_slice_id", "_slice_ids", "_analysis_status", "_review_status", "_severity"))


def _canonical_json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


# Short aliases keep call sites readable while retaining explicit primary names.
precheck_candidate = candidate_precheck
candidate_key = exact_candidate_key
deduplicate_candidates = deduplicate_exact_candidates
build_routing_plan = build_candidate_routing_plan


DISPOSITIONS = {
    "deterministically_refuted",
    "exposure_only",
    "deterministically_promoted_l2",
    "high_risk_uncertain",
    "coverage_insufficient",
}
_PIPELINE_HIGH_VALUE_TERMS = (
    "remote_binder", "remote binder", "binder_sensitive", "ontransact",
    "openfile", "file_mutation", "file_delete", "file_write",
    "data_disclosure", "sensitive_query_result", "fragment_reflection",
    "router_validation_bypass", "router bypass", "started_service",
    "onstartcommand", "receiver_effect", "receiver_binding",
    "location_sensor_collection", "device_protocol_output",
    "persistent_state_write", "database_mutation", "content_mutation",
)
_PIPELINE_IDENTITY_EXCLUDED_FIELDS = {
    "rule_id", "rule_ids", "rule_version", "title", "description",
    "severity_hint", "severity_reason", "confidence_tier", "review_priority",
    "risk_score", "locations", "limitations", "analysis_status",
    "analysis_incomplete", "review_status", "review_state",
    "evidence_decision", "false_positive_basis", "ai_analysis",
    "ai_analysis_trace", "ai_guard_assessment", "ai_preflight",
    "ai_status_reason", "ai_skip_reason", "ai_failure_reason",
    "ai_blocking_gaps", "ai_required", "ai_eligible", "ai_budget_deferred",
    "candidate_verdict", "analysis_track", "promotion_requested",
    "ai_promotion_proposal", "slice_id", "slice_refs", "context_requests",
    "candidate_id", "representative_candidate_id", "member_candidate_ids",
    "evidence_variants", "is_ai_representative", "funnel_disposition",
    "scope_key", "chain_key", "deterministic_fact_hash",
    "chain_id", "entry_method_id", "path_model", "flow_kind",
}
_PIPELINE_AI_RESULT_FIELDS = {
    "analysis_status", "ai_analysis", "ai_analysis_trace",
    "ai_guard_assessment", "ai_preflight", "ai_status_reason",
    "ai_skip_reason", "ai_failure_reason", "ai_blocking_gaps",
    "candidate_verdict", "analysis_track", "confidence_tier", "description",
    "slice_id", "slice_refs", "context_requests", "promotion_requested",
    "ai_promotion_proposal",
}


@dataclass(frozen=True)
class CandidateIdentity:
    """Three-part identity required before an AI result may be reused."""

    scope_key: str
    chain_key: str
    deterministic_fact_hash: str

    def as_dict(self) -> dict[str, str]:
        return {
            "scope_key": self.scope_key,
            "chain_key": self.chain_key,
            "deterministic_fact_hash": self.deterministic_fact_hash,
        }


@dataclass
class FunnelResult:
    """All candidates plus the representative indexes selected for AI."""

    candidates: list[dict[str, Any]]
    representative_indexes: list[int]
    groups: list[dict[str, Any]]
    summary: dict[str, int]


class CandidateFunnel:
    """Apply deterministic disposition, exact identity dedupe, and L1 budget."""

    def __init__(self, settings: Any = None):
        self.max_l1_candidates_per_run = int(
            _pipeline_setting(settings, "max_l1_candidates_per_run", 20)
        )
        self.min_l1_risk_score = int(
            _pipeline_setting(settings, "min_l1_risk_score", 80)
        )

    def process(self, candidates: list[dict[str, Any]]) -> FunnelResult:
        """原地标注候选、按三重身份分组，并只为代表项分配 AI 路由。

        不复制输入 list，其中的 candidate dict 会原地修改，返回值仍持有这些对象。只有
        scope、ordered chain 与 deterministic facts 三者全等才允许共享代表分析；L1 的 high-value 信号和
        risk score 仅决定是否路由到 AI，不提升 evidence level、severity 或确定性结论。
        """

        groups: dict[tuple[str, str, str], list[int]] = {}
        for index, candidate in enumerate(candidates):
            # Rule output must not impersonate an AI result. The orchestrator adds
            # these compatibility fields back only after a real AI analysis.
            candidate.pop("candidate_verdict", None)
            candidate.pop("analysis_track", None)
            identity = build_candidate_identity(candidate)
            candidate.update(identity.as_dict())
            candidate["candidate_id"] = _pipeline_candidate_id(candidate, identity)
            candidate["funnel_disposition"] = deterministic_precheck(
                candidate, self.min_l1_risk_score
            )
            candidate["risk_score"] = candidate_risk_score(candidate)
            candidate["ai_required"] = _pipeline_requires_ai(candidate)
            candidate["ai_eligible"] = False
            candidate["ai_budget_deferred"] = False
            groups.setdefault(
                (identity.scope_key, identity.chain_key, identity.deterministic_fact_hash), []
            ).append(index)

        group_records: list[dict[str, Any]] = []
        representative_indexes: list[int] = []
        l1_representatives: list[int] = []
        for member_indexes in groups.values():
            ordered_member_indexes = sorted(
                member_indexes, key=lambda index: candidates[index]["candidate_id"]
            )
            representative_index = _pipeline_select_representative(candidates, ordered_member_indexes)
            representative = candidates[representative_index]
            member_ids = [candidates[index]["candidate_id"] for index in ordered_member_indexes]
            # 方案 X'（v2026-08-09）：guard_blocked 成员是确定性事实，不参与"组内任一需
            # AI"的聚合——否则同组非 guard 成员会把 guard_blocked 候选"带飞"送 AI。
            group_ai_required = any(
                candidates[index]["ai_required"]
                and not candidates[index].get("auxiliary")
                and not candidates[index].get("guard_blocked")
                for index in ordered_member_indexes
            )
            rule_ids = sorted({
                str(candidates[index]["rule_id"])
                for index in ordered_member_indexes
                if candidates[index].get("rule_id")
            })
            for index in ordered_member_indexes:
                member = candidates[index]
                member["representative_candidate_id"] = representative["candidate_id"]
                member["is_ai_representative"] = index == representative_index
            representative["ai_required"] = group_ai_required
            representative["rule_ids"] = rule_ids
            representative["member_candidate_ids"] = member_ids
            representative["evidence_variants"] = [
                _pipeline_evidence_variant(candidates[index]) for index in ordered_member_indexes
            ]
            group_records.append({
                "representative_candidate_id": representative["candidate_id"],
                "member_candidate_ids": member_ids,
                "rule_ids": rule_ids,
                "ai_required": group_ai_required,
                **representative_identity(representative),
            })
            if not group_ai_required or representative.get("auxiliary"):
                continue
            if representative.get("guard_blocked"):
                # 方案 X'：guard_blocked 代表候选是确定性事实，即使组内有其他成员
                # 需要 AI，也不得把 guard_blocked 候选本身送进 AI 切片。
                continue
            if representative.get("evidence_level") == "L1":
                l1_representatives.append(representative_index)
            else:
                representative["ai_eligible"] = True
                representative_indexes.append(representative_index)

        l1_representatives.sort(
            key=lambda index: (
                candidate_risk_score(candidates[index]),
                int(candidates[index].get("review_priority") or 0),
                candidates[index]["candidate_id"],
            ),
            reverse=True,
        )
        selected_l1 = l1_representatives[: self.max_l1_candidates_per_run]
        deferred_l1 = l1_representatives[self.max_l1_candidates_per_run :]
        for index in selected_l1:
            candidates[index]["ai_eligible"] = True
        for index in deferred_l1:
            candidates[index]["ai_budget_deferred"] = True
            candidates[index]["analysis_status"] = "ai_budget_deferred"
        representative_indexes.extend(selected_l1)
        representative_indexes.sort()

        summary = {
            "candidate_count": len(candidates),
            "identity_group_count": len(groups),
            "deduplicated_count": len(candidates) - len(groups),
            "ai_representative_count": len(representative_indexes),
            "l1_ai_selected_count": len(selected_l1),
            "l1_ai_deferred_count": len(deferred_l1),
            **{
                disposition: sum(
                    item.get("funnel_disposition") == disposition for item in candidates
                )
                for disposition in sorted(DISPOSITIONS)
            },
        }
        return FunnelResult(candidates, representative_indexes, group_records, summary)


def funnel_candidates(candidates: list[dict[str, Any]], settings: Any = None) -> FunnelResult:
    return CandidateFunnel(settings).process(candidates)


def build_candidate_identity(candidate: Mapping[str, Any]) -> CandidateIdentity:
    """Build the three independent identities required for safe AI-result reuse.

    ``scope_key`` binds the exposed entry and authorization region, ``chain_key`` preserves ordered
    source/path/sink semantics, and ``deterministic_fact_hash`` covers remaining rule facts after
    excluding runtime/AI fields. Equality of only one or two keys is never sufficient.
    """

    scope = {
        "component": {
            "kind": candidate.get("component") or candidate.get("component_kind"),
            "name": candidate.get("component_name"),
        },
        "entry_points": candidate.get("entry_points") or [],
        "entry_method_id": candidate.get("entry_method_id"),
        "authorization_operation": candidate.get("authorization_operation") or "component_entry",
        "path_regions": _pipeline_path_regions(candidate.get("authorization_matrix") or []),
    }
    chain = {
        "chain_id": candidate.get("chain_id"),
        "entry_method_id": candidate.get("entry_method_id"),
        "path_model": candidate.get("path_model"),
        "flow_kind": candidate.get("flow_kind"),
        "sources": candidate.get("sources") or [],
        "sinks": candidate.get("sinks") or [],
        "taxonomy": _pipeline_taxonomy(candidate),
        "propagation_paths": candidate.get("propagation_paths") or [],
    }
    facts = {
        key: value
        for key, value in candidate.items()
        if key not in _PIPELINE_IDENTITY_EXCLUDED_FIELDS
        and key not in {"sources", "sinks", "propagation_paths"}
        and not key.startswith("ai_")
    }
    if not candidate.get("chain_id"):
        facts["legacy_rule_identity"] = {
            "rule_id": candidate.get("rule_id"),
            "rule_version": candidate.get("rule_version"),
        }
    return CandidateIdentity(
        scope_key=_pipeline_hash(_pipeline_canonical_semantic_sets(scope)),
        chain_key=_pipeline_hash(_pipeline_canonical_semantic_sets(chain)),
        deterministic_fact_hash=_pipeline_hash(_pipeline_canonical_semantic_sets(facts)),
    )


def representative_identity(candidate: Mapping[str, Any]) -> dict[str, str]:
    return {
        field: str(candidate.get(field) or "")
        for field in ("scope_key", "chain_key", "deterministic_fact_hash")
    }


def deterministic_precheck(candidate: Mapping[str, Any], min_l1_risk_score: int = 80) -> str:
    if deterministic_refutation_basis(candidate):
        return "deterministically_refuted"
    if _pipeline_has_critical_coverage_gap(candidate):
        return "coverage_insufficient"
    if candidate.get("evidence_level") == "L2":
        return (
            "deterministically_promoted_l2"
            if candidate.get("deterministic_chain_verified") is True
            else "coverage_insufficient"
        )
    if candidate.get("evidence_level") != "L1":
        return "coverage_insufficient"
    if (
        candidate.get("authorization_status") in {"unprotected", "conditional"}
        and _pipeline_has_high_value_signal(candidate)
        and candidate_risk_score(candidate) >= min_l1_risk_score
    ):
        return "high_risk_uncertain"
    return "exposure_only"


def deterministic_refutation_basis(candidate: Mapping[str, Any]) -> list[str]:
    basis: list[str] = []
    if candidate.get("authorization_status") in {"protected", "strongly_protected"}:
        basis.append("strong_permission")
    if candidate.get("guard_status") == "present_effective":
        basis.append("effective_guard")
    if candidate.get("reachability_status") in {
        "not_reachable", "unreachable", "internal_only", "not_exported",
    }:
        basis.append("not_reachable")
    if any(candidate.get(field) is False for field in (
        "rule_predicate_satisfied", "rule_premise_valid", "rule_precondition_verified",
    )):
        basis.append("rule_premise_refuted")
    if candidate.get("real_sink_verified") is False or (
        candidate.get("evidence_level") == "L2"
        and not candidate.get("sinks")
        and candidate.get("deterministic_chain_verified") is not True
    ):
        basis.append("no_real_sink")
    if _sink_is_local_broadcast(candidate):
        # 红线 9：LocalBroadcastManager/EventBus 进程内分发，不构成跨进程外溢通道。
        # sink 的 receiver_text 由规则层确定性记录（v2026-08-09），decision 层据此
        # 为 AI 的 refutes_candidate 提供确定性反证背书（docs/CHANGELOG.md 0.3.1）。
        basis.append("local_broadcast_intra_process")
    return basis


# 单词边界必须保留：EventBusUtils 这类类名不是 EventBus，误匹配会把真实跨进程
# 广播的包装类误判为进程内分发（假阴性落地为 ai_false_positive，直接隐藏漏洞）。
# 该常量是 decision.py 的 LOCAL_BROADCAST_RECEIVER_RE 的单一来源（v2026-08-09 复审）。
LOCAL_BROADCAST_RECEIVER_RE = re.compile(r"\bLocalBroadcastManager\b|\bEventBus\b")


def _sink_is_local_broadcast(candidate: Mapping[str, Any]) -> bool:
    """确定性检查：候选任一 sink 的 receiver 是否为 LocalBroadcastManager/EventBus。

    脏数据防护：sink 元素可能为非 Mapping（None/int/str），必须与
    decision._has_sdk_semantic_refutation 的 isinstance 保护保持一致——
    否则 decision 层依赖本函数不崩，脏 sink 会导致整个判定阶段崩溃。
    """

    for sink in candidate.get("sinks") or []:
        if not isinstance(sink, Mapping):
            continue
        receiver_text = str(sink.get("receiver_text") or "")
        if LOCAL_BROADCAST_RECEIVER_RE.search(receiver_text):
            return True
    return False


def candidate_risk_score(candidate: Mapping[str, Any]) -> int:
    values: list[int] = []
    for field in ("risk_score", "review_priority"):
        try:
            values.append(int(candidate.get(field) or 0))
        except (TypeError, ValueError):
            pass
    return max(values, default=0)


def propagate_representative_analysis(candidates: list[dict[str, Any]]) -> None:
    """Copy AI-only fields only when all three identity keys are equal."""

    by_id = {
        candidate.get("candidate_id"): candidate
        for candidate in candidates
        if candidate.get("candidate_id")
    }
    for representative in candidates:
        member_ids = representative.get("member_candidate_ids")
        if not isinstance(member_ids, list):
            continue
        for member_id in member_ids:
            member = by_id.get(member_id)
            if member is None or member is representative:
                continue
            if not _pipeline_identity_compatible(representative, member):
                continue
            for field in _PIPELINE_AI_RESULT_FIELDS:
                if field in representative:
                    member[field] = copy.deepcopy(representative[field])
            member["ai_result_source_candidate_id"] = representative["candidate_id"]


def _pipeline_requires_ai(candidate: Mapping[str, Any]) -> bool:
    # 方案 X'（v2026-08-09）：guard_blocked 候选（如 debuggable guard 在 release 包
    # 拦死链路）是确定性事实，无需 AI 分析——源头消除（同 LocalBroadcast 模式）。
    if candidate.get("guard_blocked"):
        return False
    disposition = candidate.get("funnel_disposition")
    if candidate.get("evidence_level") == "L1":
        # L1 高暴露组件确定性升级（v2026-08-09）：coverage_insufficient 表示存在真实
        # 代码上下文（如动态 receiver 注册点 + 外部可达），送 AI 深度分析——直接消掉
        # 最大漏报面（128 个 L1 中 88 个 coverage_insufficient 此前从不送 AI）；
        # exposure_only 是纯 manifest 事实（无代码上下文），AI 无内容可分析，不送。
        return disposition in {"high_risk_uncertain", "coverage_insufficient"}
    if candidate.get("evidence_level") != "L2" or disposition == "deterministically_refuted":
        return False
    if candidate.get("deterministic_chain_verified") is not True:
        return True
    return _pipeline_has_explicit_l2_uncertainty(candidate)


def _pipeline_has_explicit_l2_uncertainty(candidate: Mapping[str, Any]) -> bool:
    return (
        candidate.get("authorization_status") in {None, "", "unknown", "conditional"}
        or candidate.get("guard_status") in {None, "", "unknown", "present_partial"}
        or candidate.get("impact_status") in {None, "", "unknown", "potential"}
        or bool(candidate.get("uncertainties"))
        or any(
            isinstance(gap, Mapping) and gap.get("critical", True)
            for gap in candidate.get("blocking_gaps", [])
        )
    )


def _pipeline_has_high_value_signal(candidate: Mapping[str, Any]) -> bool:
    """Return a conservative routing hint, never a finding or severity assertion.

    The broad text fallback intentionally favors review recall; callers must combine it with exposure
    and risk thresholds, and must not treat a hit as proof of reachability, authorization, or impact.
    """

    if candidate.get("binder_remote_interface") is True:
        return True
    if candidate.get("fragment_reflection") or candidate.get("router_validation_bypass"):
        return True
    if candidate.get("started_service_state_machine") or candidate.get("receiver_binding"):
        return True
    searchable = json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str).lower()
    return any(term in searchable for term in _PIPELINE_HIGH_VALUE_TERMS)


def _pipeline_has_critical_coverage_gap(candidate: Mapping[str, Any]) -> bool:
    return any(
        not isinstance(gap, Mapping) or gap.get("critical", True)
        for gap in candidate.get("coverage_gaps", [])
    )


def _pipeline_identity_compatible(
    representative: Mapping[str, Any], member: Mapping[str, Any]
) -> bool:
    recorded = representative_identity(representative)
    if recorded != representative_identity(member):
        return False
    recomputed_representative = build_candidate_identity(representative).as_dict()
    recomputed_member = build_candidate_identity(member).as_dict()
    return recorded == recomputed_representative == recomputed_member


def _pipeline_select_representative(
    candidates: list[dict[str, Any]], indexes: Iterable[int]
) -> int:
    return max(
        indexes,
        key=lambda index: (
            bool(candidates[index].get("ai_required")),
            candidate_risk_score(candidates[index]),
            len(candidates[index].get("locations") or []),
            candidates[index].get("candidate_id") or "",
        ),
    )


def _pipeline_evidence_variant(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "rule_id": candidate.get("rule_id"),
        "locations": copy.deepcopy(candidate.get("locations") or []),
        "sources": copy.deepcopy(candidate.get("sources") or []),
        "sinks": copy.deepcopy(candidate.get("sinks") or []),
        "propagation_paths": copy.deepcopy(candidate.get("propagation_paths") or []),
        "limitations": copy.deepcopy(candidate.get("limitations") or []),
    }


def _pipeline_candidate_id(
    candidate: Mapping[str, Any], identity: CandidateIdentity
) -> str:
    existing = candidate.get("candidate_id")
    if isinstance(existing, str) and existing:
        return existing
    locations = sorted(candidate.get("locations") or [], key=_pipeline_json)
    return "candidate_" + _pipeline_hash({
        **identity.as_dict(),
        "chain_id": candidate.get("chain_id"),
        "rule_id": candidate.get("rule_id"),
        "locations": locations,
    })[:20]


def _pipeline_path_regions(rows: Sequence[Any]) -> list[Any]:
    regions = [row.get("path_region") for row in rows if isinstance(row, Mapping)]
    unique = {_pipeline_json(region): region for region in regions}
    return [unique[key] for key in sorted(unique)]


def _pipeline_taxonomy(candidate: Mapping[str, Any]) -> list[Any]:
    values: list[Any] = []
    for field in ("operation_taxonomy", "taxonomy"):
        if candidate.get(field) is not None:
            values.append(candidate[field])
    for sink in candidate.get("sinks") or []:
        if isinstance(sink, Mapping):
            values.append({
                "taxonomy": sink.get("taxonomy") or sink.get("operation_taxonomy"),
                "kind": sink.get("kind"),
            })
    return values


def _pipeline_setting(settings: Any, name: str, default: int) -> Any:
    if settings is None:
        return default
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _pipeline_canonical_semantic_sets(value: Any, field_name: str = "") -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _pipeline_canonical_semantic_sets(item, str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        projected = [_pipeline_canonical_semantic_sets(item, field_name) for item in value]
        if field_name in _SET_LIKE_LIST_FIELDS:
            unique = {_pipeline_json(item): item for item in projected}
            return [unique[key] for key in sorted(unique)]
        return projected
    return value


def _pipeline_hash(value: Any) -> str:
    return hashlib.sha256(_pipeline_json(value).encode("utf-8")).hexdigest()


def _pipeline_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

