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
    # funnel 自身写回的分级结果：由候选事实推导而来，不得反过来参与身份计算，
    # 否则同一候选在开关开/关两种配置下会得到不同 candidate_id。
    "demotion_reason", "flow_evidence_tier",
}
# 组件级数据流 trace：由 detector 的 common_metadata 统一下发给同组件的每条链
# （rules/shared/detector.py `common_metadata`），随链路数量波动且非判定依据。
# 参与身份哈希只会制造伪差异，使语义相同的链无法合并。
_PIPELINE_IDENTITY_TRACE_FIELDS = {
    "method_summaries",
    "reaching_definitions",
    "validation_transitions",
    "slot_overwrites",
    "router_validation_bypass",
    "summary_fixpoint",
    "fragment_reflection",
    "started_service_state_machine",
    "receiver_binding",
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
        self.demote_unproven_flow = bool(
            _pipeline_setting(settings, "demote_unproven_flow", False)
        )
        # R-2（2026-08-15）：L1 预算按可判定性排序——receiver_flag_tier 高的
        # （confirmed_exported_clean）优先进预算。默认关闭走守门（口径 A/B 对比
        # 后翻默认），避免改变既有预算选择行为。
        self.l1_priority_clean = bool(
            _pipeline_setting(settings, "l1_priority_clean", False)
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
            demotion_reason = unproven_flow_demotion_reason(candidate)
            candidate["demotion_reason"] = demotion_reason
            if demotion_reason and self.demote_unproven_flow:
                # 降级为 signal：不占 AI 预算、不进人工队列，但候选仍完整写入产物，
                # 前端可列示、回归可审计——是降级而非丢弃。
                candidate["flow_evidence_tier"] = "signal"
                candidate["ai_required"] = False
            else:
                candidate["flow_evidence_tier"] = "candidate"
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

        # R-2（2026-08-15）：L1 预算排序——开启 l1_priority_clean 时，可判定性
        # 分级（receiver_flag_tier）优先于 risk_score：confirmed_exported_clean
        # （6 条真实暴露面）> confirmed_exported_gap > unresolved_flag > 默认。
        # 关闭时维持原排序（risk_score 优先），行为不变。
        def _l1_sort_key(index: int) -> tuple:
            candidate = candidates[index]
            if self.l1_priority_clean:
                tier_priority = {
                    "confirmed_exported_clean": 4,
                    "confirmed_exported_gap": 3,
                    "unresolved_flag": 2,
                }.get(candidate.get("receiver_flag_tier"), 1)
                return (
                    tier_priority,
                    candidate_risk_score(candidate),
                    int(candidate.get("review_priority") or 0),
                    candidate["candidate_id"],
                )
            return (
                candidate_risk_score(candidate),
                int(candidate.get("review_priority") or 0),
                candidate["candidate_id"],
            )

        l1_representatives.sort(key=_l1_sort_key, reverse=True)
        selected_l1 = l1_representatives[: self.max_l1_candidates_per_run]
        deferred_l1 = l1_representatives[self.max_l1_candidates_per_run :]
        for index in selected_l1:
            candidates[index]["ai_eligible"] = True
        for index in deferred_l1:
            candidates[index]["ai_budget_deferred"] = True
            candidates[index]["analysis_status"] = "ai_budget_deferred"
        representative_indexes.extend(selected_l1)
        representative_indexes.sort()

        # P0-2 可观测：降级行为必须可审计、可回溯（按 reason 分组计数）。
        # demote_unproven_flow 关闭时仍统计"若开启会降级多少"，供灰度评估。
        demotion_counts: dict[str, int] = {}
        for item in candidates:
            reason = item.get("demotion_reason")
            if reason:
                demotion_counts[str(reason)] = demotion_counts.get(str(reason), 0) + 1
        summary = {
            "candidate_count": len(candidates),
            "identity_group_count": len(groups),
            "deduplicated_count": len(candidates) - len(groups),
            "ai_representative_count": len(representative_indexes),
            "l1_ai_selected_count": len(selected_l1),
            "l1_ai_deferred_count": len(deferred_l1),
            "unproven_flow_demotion_enabled": int(self.demote_unproven_flow),
            "unproven_flow_matched_count": sum(demotion_counts.values()),
            "demoted_candidates": (
                sum(demotion_counts.values()) if self.demote_unproven_flow else 0
            ),
            **{
                f"demotion_reason_{reason}": count
                for reason, count in sorted(demotion_counts.items())
            },
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

    P0-3（2026-08-15）：三键此前恒等于"逐候选唯一"，精确去重 0 生效——`chain_key` 内嵌
    `chain_id`（`dfc_` + entry/source/sink/path 哈希）且携带完整 `propagation_paths`（含行号、
    调用文本、ordinal），`deterministic_fact_hash` 携带 gap/guard 的行号级明细。语义完全相同
    的链（同入口、同 source、同 sink、同调用序列）因此永远无法合并。修复思路是**只做语义投影，
    不放宽判定要素**：链身份改用有序 `method_id` 序列，gap/guard 只取 code/critical/status，
    组件级 trace 字段（随链路波动、非判定依据）不参与身份。
    """

    # R-3（2026-08-15）：DYNAMIC_RECEIVER 的暴露入口是注册点文件（逐候选不同），
    # 若按注册点文件做 scope 身份则永不合组——投影为 owner（包前缀），
    # 与 chain 的 receiver_semantics（tier+owner+action）配合聚合。
    _is_receiver_exposure = candidate.get("flow_kind") == "receiver_exposure"
    scope = {
        "component": {
            "kind": candidate.get("component") or candidate.get("component_kind"),
            "name": (
                _pipeline_registration_owner(candidate)
                if _is_receiver_exposure else candidate.get("component_name")
            ),
        },
        "entry_points": candidate.get("entry_points") or [],
        "entry_method_id": (
            str(candidate.get("entry_method_id") or "").split(":", 1)[0]
            if _is_receiver_exposure else candidate.get("entry_method_id")
        ),
        "authorization_operation": candidate.get("authorization_operation") or "component_entry",
        "path_regions": _pipeline_path_regions(candidate.get("authorization_matrix") or []),
    }
    # R-3：receiver_exposure 候选的 entry_method_id 是注册点方法（含行号：
    # "path#Method:line"），同方法内多行注册行号不同——投影去行号。
    _receiver_entry = candidate.get("entry_method_id")
    if _is_receiver_exposure and isinstance(_receiver_entry, str):
        _receiver_entry = _receiver_entry.split(":", 1)[0]
    chain = {
        # chain_id 逐候选唯一（dataflow.py:259 对 entry/source/sink/path 取哈希），保留在候选体内
        # 供追溯，但不参与身份——否则任何两条链都不可能同组。
        "entry_method_id": _receiver_entry,
        "path_model": candidate.get("path_model"),
        "flow_kind": candidate.get("flow_kind"),
        "sources": _pipeline_endpoint_projection(candidate.get("sources") or []),
        "sinks": _pipeline_endpoint_projection(candidate.get("sinks") or []),
        "taxonomy": _pipeline_taxonomy(candidate),
        # 有序方法序列保留"经过哪些方法、顺序如何"的语义，剔除行号/调用文本/ordinal 等
        # 同一语义链在不同候选间必然波动的表层差异。
        "propagation_path_shape": _pipeline_path_shape(candidate.get("propagation_paths") or []),
    }
    # R-3（2026-08-15）：DYNAMIC_RECEIVER 候选按 flag 分级 + 注册点 owner + action
    # 聚合，剔除注册点行号/调用点差异——同形态（同 owner 同 tier 同 action 组合）
    # 的 receiver 暴露面合并为一组复核，避免 277 条各自占位。sources 投影为
    # owner+path（去行号），因为 receiver 候选的 source 就是注册点（逐行号不同）。
    if _is_receiver_exposure:
        chain["receiver_semantics"] = {
            "flag_tier": candidate.get("receiver_flag_tier") or "tier_unknown",
            "owner": _pipeline_registration_owner(candidate),
            "actions": sorted({
                str(action) for action in
                (candidate.get("receiver_binding") or {}).get("actions") or []
            }),
        }
        chain["sources"] = _pipeline_receiver_source_projection(
            candidate.get("sources") or []
        )
    facts = {
        _pipeline_fact_key(key): _pipeline_fact_projection(key, value)
        for key, value in candidate.items()
        if key not in _PIPELINE_IDENTITY_EXCLUDED_FIELDS
        and key not in _PIPELINE_IDENTITY_TRACE_FIELDS
        and key not in {"sources", "sinks", "propagation_paths"}
        and not key.startswith("ai_")
    }
    # R-3：receiver_exposure 的 component_name 是注册点文件（dynamic:<path>），
    # 逐注册点唯一——投影为 owner；entry_points 含行号（注册点方法行）也投影
    # 为 owner+path，避免 facts 身份拆散同形态组。
    if _is_receiver_exposure:
        if "component_name" in facts:
            facts["component_name"] = _pipeline_registration_owner(candidate)
        if "entry_points" in facts and isinstance(facts["entry_points"], (list, tuple)):
            facts["entry_points"] = [
                _pipeline_entry_projection(item) for item in facts["entry_points"]
            ]
        if "entry_method_name" in facts:
            facts["entry_method_name"] = "registerReceiver"
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


def _pipeline_endpoint_projection(endpoints: Sequence[Any]) -> list[Any]:
    """Project source/sink endpoints down to their semantic identity.

    保留 path+line（这是"哪个 sink"的本体，不能丢）与 taxonomy/kind，剔除 ordinal、
    resolve_status、调用文本等同一 sink 在不同链路中会波动的字段。
    """

    projected: list[Any] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, Mapping):
            projected.append(endpoint)
            continue
        projected.append({
            "path": endpoint.get("path"),
            "line": endpoint.get("line"),
            "method_id": endpoint.get("method_id"),
            "taxonomy": endpoint.get("taxonomy") or endpoint.get("operation_taxonomy"),
            "kind": endpoint.get("kind"),
        })
    return projected


def _pipeline_registration_owner(candidate: Mapping[str, Any]) -> str:
    """动态 receiver 注册点 owner：注册点文件路径的包前缀（前 3 段）。

    R-3（2026-08-15）：同 owner 同 flag 同 action 的 receiver 暴露面合并为一组
    复核（如 com/xiaomi/fitness 的多个注册点）；未知路径回退 'owner_unknown'。
    """

    registration = (candidate.get("receiver_binding") or {}).get("registration") or {}
    path = str(registration.get("path") or candidate.get("component_name") or "")
    if path.startswith("dynamic:"):
        path = path[len("dynamic:"):]
    parts = [part for part in path.split("/") if part]
    return "/".join(parts[:3]) if len(parts) >= 3 else (path or "owner_unknown")


def _pipeline_entry_projection(entry: Any) -> Any:
    """receiver 入口投影：entry 字符串/对象去行号（注册点方法行逐候选不同）。"""

    if isinstance(entry, str):
        # "path#Method:line" → "path#Method"
        return entry.split(":", 1)[0]
    if isinstance(entry, Mapping):
        projected = dict(entry)
        if "method_id" in projected:
            projected["method_id"] = str(projected["method_id"]).split(":", 1)[0]
        return projected
    return entry


def _pipeline_receiver_source_projection(sources: Sequence[Any]) -> list[Any]:
    """receiver 候选的 source 投影：注册点 source 按 owner+path 聚合（去行号）。

    R-3（2026-08-15）：DYNAMIC_RECEIVER 的 source 是 registerReceiver 调用点，
    逐注册点行号不同——按 path（去 line）保留"哪个文件注册"的语义即可。
    """

    projected: list[Any] = []
    for source in sources:
        if not isinstance(source, Mapping):
            projected.append(source)
            continue
        projected.append({
            "path": source.get("path"),
            "kind": source.get("kind"),
        })
    return projected


def _pipeline_path_shape(paths: Sequence[Any]) -> list[Any]:
    """Ordered method sequence of a propagation path (call shape without表层细节).
    顺序敏感：列表不参与 set-like 归并，因此调换顺序会产生不同的 chain_key。
    节点缺少 method_id/path 时回退到节点自身的规范化内容——否则多个无标识节点会被
    投影成同一个 None，顺序差异随之消失（实测基线 run 1660 个节点中有 4 个属此情形）。
    """

    shape: list[Any] = []
    for node in paths:
        if not isinstance(node, Mapping):
            shape.append(node)
            continue
        identity = node.get("method_id") or node.get("path")
        shape.append(identity if identity else _pipeline_canonical_semantic_sets(dict(node)))
    return shape


def _pipeline_fact_key(key: str) -> str:
    return "blocking_gap_codes" if key == "blocking_gaps" else key


def _pipeline_fact_projection(key: str, value: Any) -> Any:
    """Reduce rule facts to判定语义, dropping line-level noise.

    gap/coverage 只取 code + critical，guard 只取 status——判定依据是"有没有这类 gap /
    guard 是否有效"，而不是"gap 落在第几行"。
    """

    if key in {"blocking_gaps", "coverage_gaps"} and isinstance(value, (list, tuple)):
        codes = {
            (str(item.get("code")), bool(item.get("critical")))
            for item in value
            if isinstance(item, Mapping)
        }
        return [{"code": code, "critical": critical} for code, critical in sorted(codes)]
    if key in {"guard_coverage", "guard_summary"} and isinstance(value, Mapping):
        return {"status": value.get("status")}
    return value


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
    # P0②（2026-08-15）：入口侧本地广播隔离（对齐红线 9 语义）。路由注入规则
    # 对 LocalBroadcastManager/EventBus 注册的 onReceive 入口产出
    # input_control=local_broadcast_isolated + LOCAL_BROADCAST_ISOLATED gap——
    # 进程内分发，外部应用无法跨进程触发（候选 11 动态验证实证）。与 sink 侧
    # local_broadcast_intra_process 同根因，合并为同一确定性反证 basis。
    if candidate.get("input_control") == "local_broadcast_isolated":
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


def unproven_flow_demotion_reason(candidate: Mapping[str, Any]) -> str | None:
    """判断候选是否属于"值流未证明"，返回结构化降级原因（不降级返回 None）。

    P0-2（2026-08-15）判据说明：**不使用 sink 参数字面量性**。

    `control_to_sink` 的定义（rules/shared/dataflow.py `_execute_call`）已确定"无任何
    untrusted 值到达 sink 参数"——这是 taint 引擎给出的确定性事实。而"参数非常量"只说明
    它是个变量，**不能证明它受攻击者控制**：基线 run 实测按字面量判据会把 57 条候选判为
    "可能受控"送 AI，而 v04 真机验证这 57 条全部是误报。

    因此降级判据交给 P0-1 的作用域分析：
    - `CONTROL_SCOPE_UNRESOLVED`：分支作用域无法推断，control_to_sink 的支配关系存疑。
      P0-1 生效后，作用域可解析的块外 sink 根本不会成链；能走到这里的只有边界未知的链。

    **`LEGACY_FLOW_FALLBACK` / `inferred_source_to_sink` 不降级（2026-08-15 v2 收紧）**：
    轻量正则回退仅在主分析 0 链时触发（同方法 Source/Sink），语义链"未闭合"只表示
    规则层精度不足，**不等于无漏洞**——恰是需要 AI 判定的候选。com.mi.health 实证：
    RouterActivity（inferred_source_to_sink）被 AI 判 flaw_holds=True、exploitability
    全绿，若降级即漏报真漏洞（§5 守门硬门槛被打破）。样本量极小（基线/shop 复扫各 1
    条误报、mi.health 1 条真漏洞），降级收益 < 漏报风险。

    已被确定性验证的链（`deterministic_chain_verified`）永不降级。
    """

    if candidate.get("deterministic_chain_verified") is True:
        return None
    flow_kind = candidate.get("flow_kind")
    gap_codes = {
        str(gap.get("code"))
        for gap in candidate.get("blocking_gaps") or []
        if isinstance(gap, Mapping)
    }
    if flow_kind == "control_to_sink" and "CONTROL_SCOPE_UNRESOLVED" in gap_codes:
        return "scope_unresolved"
    # P1③（2026-08-15）：bulk_extras_forwarding 且消费方为统计语义（sink 方法名含
    # Stat/Report 等 SDK 统计惯例）→ 降级 signal。v04 动态验证显示该形态 5/5 误报
    # （AuthActivity/StatService2/SplashCommonUtils 固定消费方）；判定仅认规则层
    # 显式输出的 consumer_semantics=statistics，语义未知的保留 L2。
    if candidate.get("consumer_semantics") == "statistics":
        return "bulk_statistics_consumer"
    return None


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

