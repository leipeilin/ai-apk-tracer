from __future__ import annotations

import copy

import pytest

from app.analysis.candidate_funnel import (
    CandidateReason,
    CandidateRoute,
    build_candidate_routing_plan,
    candidate_precheck,
    deduplicate_exact_candidates,
    exact_candidate_key,
)


def _candidate(**values) -> dict:
    candidate = {
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "rule_version": "1.0.0",
        "evidence_level": "L1",
        "component": "activity",
        "component_name": "com.example.ExportedActivity",
        "entry_points": ["com.example.ExportedActivity#onCreate"],
        "locations": [
            {"artifact": "manifest", "path": "AndroidManifest.xml", "line": None},
            {"artifact": "code", "path": "Example.java", "line": 10},
        ],
        "sources": [
            {"path": "Example.java", "line": 10, "kind": "intent"},
            {"path": "Example.java", "line": 11, "kind": "extra"},
        ],
        "sinks": [{"path": "Example.java", "line": 20, "kind": "webview"}],
        "propagation_paths": [{"from": "intent", "to": "webview", "status": "fact"}],
        "authorization_status": "unprotected",
        "authorization_matrix": [{"operation": "read", "allowed": True}],
        "guard_status": "absent",
        "dataflow_status": "intraprocedural",
        "reachability_status": "reachable",
        "deterministic_chain_verified": True,
        "operation_modes": ["r", "rw"],
    }
    candidate.update(values)
    return candidate


def test_exact_duplicates_group_under_first_seen_representative() -> None:
    first = _candidate()
    duplicate = copy.deepcopy(first)

    groups = deduplicate_exact_candidates([first, duplicate])

    assert len(groups) == 1
    assert groups[0].representative_index == 0
    assert groups[0].duplicate_indexes == (1,)
    assert groups[0].original_indexes == (0, 1)


def test_near_duplicates_do_not_collapse_and_ordered_lists_remain_ordered() -> None:
    original = _candidate()
    changed_location = _candidate(
        locations=[
            {"artifact": "manifest", "path": "AndroidManifest.xml", "line": None},
            {"artifact": "code", "path": "Example.java", "line": 12},
        ]
    )
    changed_rule = _candidate(rule_id="ACTIVITY_EXPORTED_NO_PERMISSION")
    reordered_sources = _candidate(sources=list(reversed(original["sources"])))

    groups = deduplicate_exact_candidates(
        [original, changed_location, changed_rule, reordered_sources]
    )

    assert [group.representative_index for group in groups] == [0, 1, 2, 3]
    assert len({group.candidate_key for group in groups}) == 4


def test_clearly_set_like_fields_are_canonicalized_without_reordering_inputs() -> None:
    first = _candidate(operation_modes=["rw", "r"])
    second = _candidate(operation_modes=["r", "rw"])
    before = copy.deepcopy(first)

    assert exact_candidate_key(first) == exact_candidate_key(second)
    assert first == before


def test_routing_plan_preserves_first_seen_order_and_duplicate_provenance() -> None:
    first = _candidate(component_name="First")
    second = _candidate(component_name="Second", evidence_level="L2")
    candidates = [
        first,
        second,
        copy.deepcopy(first),
        copy.deepcopy(second),
        copy.deepcopy(first),
    ]

    plan = build_candidate_routing_plan(candidates)

    assert plan.representative_indexes == (0, 1)
    assert plan.entries[0].duplicate_indexes == (2, 4)
    assert plan.entries[0].original_indexes == (0, 2, 4)
    assert plan.entries[1].duplicate_indexes == (3,)
    assert [
        (item.candidate_index, item.representative_index)
        for item in plan.duplicate_provenance
    ] == [(2, 0), (3, 1), (4, 0)]
    assert [entry.representative_index for entry in plan.for_route("l1_triage")] == [0]
    assert [entry.representative_index for entry in plan.for_route(CandidateRoute.L2_REVIEW)] == [1]


@pytest.mark.parametrize(
    ("candidate", "eligible", "route", "reason"),
    [
        (
            _candidate(auxiliary=True),
            False,
            CandidateRoute.NONE,
            CandidateReason.AUXILIARY,
        ),
        (
            _candidate(evidence_level="L2", auxiliary=True),
            False,
            CandidateRoute.NONE,
            CandidateReason.AUXILIARY,
        ),
        (
            _candidate(evidence_level="L2", component_name=None),
            True,
            CandidateRoute.L2_REVIEW,
            CandidateReason.L2_ELIGIBLE,
        ),
        (
            _candidate(reachability_status="unreachable"),
            False,
            CandidateRoute.NONE,
            CandidateReason.L1_UNREACHABLE,
        ),
        (
            _candidate(authorization_status="protected"),
            False,
            CandidateRoute.NONE,
            CandidateReason.L1_PROTECTED,
        ),
        (
            _candidate(authorization_status="strongly_protected"),
            False,
            CandidateRoute.NONE,
            CandidateReason.L1_PROTECTED,
        ),
        (
            _candidate(component_name=""),
            False,
            CandidateRoute.NONE,
            CandidateReason.L1_COMPONENT_NAME_MISSING,
        ),
        (
            _candidate(reachability_status="conditional"),
            True,
            CandidateRoute.L1_TRIAGE,
            CandidateReason.L1_ELIGIBLE,
        ),
        (
            _candidate(evidence_level="L3"),
            False,
            CandidateRoute.NONE,
            CandidateReason.EVIDENCE_LEVEL_UNKNOWN,
        ),
    ],
)
def test_precheck_routes_with_explicit_reason_codes(
    candidate: dict,
    eligible: bool,
    route: CandidateRoute,
    reason: CandidateReason,
) -> None:
    decision = candidate_precheck(candidate)

    assert decision.eligible is eligible
    assert decision.route == route
    assert decision.reason_code == reason


def test_stable_key_ignores_runtime_ai_review_severity_slice_error_and_timestamp_fields() -> None:
    deterministic = _candidate()
    runtime_enriched = {
        **copy.deepcopy(deterministic),
        "analysis_status": "ai_completed",
        "review_status": "pending_manual",
        "manual_review_status": "approved",
        "severity_hint": "critical",
        "severity_reason": ["AI changed this"],
        "risk_severity": "critical",
        "slice_id": "slice-random",
        "context_slice_id": "slice-random-2",
        "slice_refs": ["context-random"],
        "ai_analysis": {"verdict": "confirmed"},
        "ai_analysis_trace": [{"round": 2, "result": {"random": True}}],
        "runtime": {"python": "3.12.9"},
        "duration_ms": 987,
        "created_at": "2026-08-04T00:00:00Z",
        "finished_at": "2026-08-04T00:00:01Z",
        "transient_error": "connection reset",
        "error": {"request_id": "random-request"},
    }

    assert exact_candidate_key(deterministic) == exact_candidate_key(runtime_enriched)


def test_deterministic_rule_facts_remain_part_of_the_key() -> None:
    original = _candidate()
    variants = [
        _candidate(authorization_status="unknown"),
        _candidate(guard_status="present_partial"),
        _candidate(dataflow_status="not_proven"),
        _candidate(reachability_status="conditional"),
        _candidate(review_priority=90),
        _candidate(propagation_paths=[{"from": "intent", "to": "file", "status": "fact"}]),
        _candidate(custom_rule_fact={"verified": False}),
    ]

    assert all(exact_candidate_key(original) != exact_candidate_key(item) for item in variants)


def test_all_public_operations_leave_inputs_unchanged() -> None:
    candidates = [
        _candidate(),
        _candidate(analysis_status="rule_only", slice_id="slice-runtime"),
        _candidate(component_name="Other"),
    ]
    before = copy.deepcopy(candidates)

    candidate_precheck(candidates[0])
    exact_candidate_key(candidates[0])
    deduplicate_exact_candidates(candidates)
    build_candidate_routing_plan(candidates)

    assert candidates == before


def test_deterministic_refutation_basis_local_broadcast_sink() -> None:
    """回归：LocalBroadcastManager/EventBus 进程内分发须提供确定性反证背书。

    3.0.1 run（20260808T155920Z）：AI 正确识别 ShopApp 的 LocalBroadcast 判定为
    refutes，但 decision 层 false_positive_basis=[] 不采信（采信率 0/8）。
    0.3.1：规则层在 sink 记录 receiver_text，decision 层据此给 local_broadcast_intra_process。
    """

    from app.analysis.candidate_funnel import deterministic_refutation_basis

    candidate = {
        "sinks": [{
            "path": "com/xiaomi/shop2/ShopApp.java",
            "line": 450,
            "kind": "implicit_broadcast",
            "receiver_text": "LocalBroadcastManager.getInstance(getAppContext())",
        }],
        "evidence_level": "L2",
        "authorization_status": "unprotected",
        "guard_status": "absent",
        "reachability_status": "reachable",
    }
    basis = deterministic_refutation_basis(candidate)
    assert "local_broadcast_intra_process" in basis

    # 非 LocalBroadcast 的普通 sendBroadcast 不产生该背书
    candidate["sinks"] = [{"receiver_text": "context.sendBroadcast(intent)"}]
    assert "local_broadcast_intra_process" not in deterministic_refutation_basis(candidate)

    # EventBusUtils 等包装类不得误匹配（单词边界，2026-08-09 复审）
    candidate["sinks"] = [{"receiver_text": "EventBusUtils.dispatch(event)"}]
    assert "local_broadcast_intra_process" not in deterministic_refutation_basis(candidate), \
        "EventBusUtils 不是 EventBus，不得误判为进程内分发（否则真漏洞被 ai_false_positive 隐藏）"

    # 无 receiver 信息时不产生（旧候选兼容）
    candidate["sinks"] = [{"kind": "implicit_broadcast"}]
    assert "local_broadcast_intra_process" not in deterministic_refutation_basis(candidate)
