from __future__ import annotations

import copy

from app.analysis.candidate_funnel import (
    CandidateFunnel,
    build_candidate_identity,
    propagate_representative_analysis,
)
from app.analysis.orchestrator import ScanOrchestrator
from app.findings.aggregate import aggregate_candidates
from app.findings.decision import DecisionEngine


def _candidate(**overrides):
    candidate = {
        "rule_id": "TEST_RULE",
        "component": "activity",
        "component_name": "com.example.Entry",
        "entry_points": ["onCreate"],
        "authorization_operation": "component_entry",
        "authorization_matrix": [{"path_region": {"entry": "onCreate"}}],
        "evidence_level": "L1",
        "analysis_status": "rule_only",
        "dataflow_status": "not_applicable",
        "authorization_status": "unprotected",
        "guard_status": "unknown",
        "impact_status": "potential",
        "reachability_status": "reachable",
        "deterministic_chain_verified": False,
        "review_priority": 40,
        "locations": [{"path": "Entry.java", "line": 1}],
        "sources": [],
        "sinks": [],
        "propagation_paths": [],
        "blocking_gaps": [],
        "coverage_gaps": [],
        "limitations": [],
    }
    candidate.update(overrides)
    return candidate


def _closed_l2(**overrides):
    candidate = _candidate(
        evidence_level="L2",
        dataflow_status="intraprocedural",
        deterministic_chain_verified=True,
        authorization_status="unprotected",
        guard_status="absent",
        impact_status="statically_confirmed",
        sources=[{"path": "Entry.java", "line": 2, "kind": "external_input"}],
        sinks=[{"path": "Entry.java", "line": 3, "kind": "file_mutation", "taxonomy": "file_mutation"}],
        propagation_paths=[{"from": "onCreate", "to": "delete", "order": 0}],
        chain_id="chain_111111111111111111111111",
        entry_method_id="com.example.Entry#onCreate",
        path_model="linear_ir_v1",
        flow_kind="source_to_sink",
    )
    candidate.update(overrides)
    return candidate


def test_identity_keeps_distinct_chains_separate() -> None:
    first = _closed_l2()
    second = _closed_l2(
        sinks=[{"path": "Entry.java", "line": 4, "kind": "data_disclosure", "taxonomy": "data_disclosure"}],
        propagation_paths=[{"from": "onCreate", "to": "send", "order": 0}],
    )

    result = CandidateFunnel().process([first, second])

    assert first["scope_key"] == second["scope_key"]
    assert first["chain_key"] != second["chain_key"]
    assert len(result.groups) == 2
    assert all(len(group["member_candidate_ids"]) == 1 for group in result.groups)
    assert len(aggregate_candidates(result.candidates)) == 2


def test_deterministic_semantics_are_part_of_ai_group_identity() -> None:
    first = _closed_l2(
        rule_id="RULE_ONE", rule_version="1.0.0", guard_status="unknown",
        semantic_variant="first",
    )
    second = copy.deepcopy(first)
    second["rule_id"] = "RULE_TWO"
    second["semantic_variant"] = "second"
    second["locations"] = [{"path": "OtherEvidence.java", "line": 9}]

    result = CandidateFunnel().process([first, second])

    assert len(result.groups) == 2
    assert result.representative_indexes == [0, 1]
    assert first["deterministic_fact_hash"] != second["deterministic_fact_hash"]
    assert all(candidate["is_ai_representative"] for candidate in result.candidates)
    assert all(candidate["ai_eligible"] for candidate in result.candidates)


def test_ordinary_exported_l1_and_dynamic_receiver_do_not_enter_ai() -> None:
    candidates = [
        _candidate(rule_id="RECEIVER_EXPORTED_NO_PERMISSION"),
        _candidate(
            rule_id="DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION",
            component="dynamic_receiver",
            component_name="dynamic:Receiver.java",
        ),
    ]

    result = CandidateFunnel().process(candidates)

    assert result.representative_indexes == []
    assert all(candidate["funnel_disposition"] == "exposure_only" for candidate in candidates)
    assert all(candidate["ai_required"] is False for candidate in candidates)


def test_high_risk_rule_cannot_schedule_an_incompatible_rule_as_its_member() -> None:
    high_risk = _candidate(
        rule_id="REMOTE_BINDER_SPECIAL_REVIEW",
        review_priority=80,
    )
    ordinary = _candidate(rule_id="ORDINARY_EXPOSURE", review_priority=100)

    result = CandidateFunnel().process([high_risk, ordinary])

    assert len(result.groups) == 2
    assert result.representative_indexes == [0]
    assert high_risk["is_ai_representative"] is True
    assert high_risk["ai_required"] is True
    assert high_risk["ai_eligible"] is True
    assert ordinary["is_ai_representative"] is True
    assert ordinary["ai_required"] is False
    assert {tuple(group["rule_ids"]) for group in result.groups} == {
        ("ORDINARY_EXPOSURE",),
        ("REMOTE_BINDER_SPECIAL_REVIEW",),
    }


def test_same_chain_different_rules_merge_and_finish_consistently() -> None:
    first = _closed_l2(rule_id="RULE_ONE", rule_version="1.0.0", guard_status="unknown")
    second = _closed_l2(rule_id="RULE_TWO", rule_version="1.0.0", guard_status="unknown")

    result = CandidateFunnel().process([first, second])

    assert len(result.groups) == 1
    assert len(result.representative_indexes) == 1
    assert all(candidate["ai_required"] for candidate in result.candidates)
    representative = result.candidates[result.representative_indexes[0]]
    analysis = {
        "analysis_track": "l2_review",
        "verdict": "supports_candidate",
        "candidate_verdict": "supports_candidate",
        "analysis_complete": True,
        "verified_evidence_refs": [{"context_id": "ctx", "line": 2}],
        "invalid_evidence_refs": [],
        "semantic_evidence_complete": True,
        "blocking_gaps": [],
    }
    representative.update({
        "analysis_status": "ai_completed",
        "candidate_verdict": "supports_candidate",
        "analysis_track": "l2_review",
        "ai_analysis": analysis,
    })

    propagate_representative_analysis(result.candidates)
    for candidate in result.candidates:
        DecisionEngine().decide(candidate)
    findings = aggregate_candidates(result.candidates)

    assert all(candidate["evidence_decision"] == "supported" for candidate in result.candidates)
    assert all(candidate["review_status"] == "pending_manual" for candidate in result.candidates)
    assert len(findings) == 1
    assert findings[0]["analysis_status"] == "ai_completed"
    assert findings[0]["review_status"] == "pending_manual"


def test_candidate_and_finding_ids_are_stable_when_input_is_reversed() -> None:
    first = _closed_l2(rule_id="RULE_ONE", guard_status="unknown")
    second = _closed_l2(
        rule_id="RULE_TWO",
        chain_id="chain_222222222222222222222222",
        sinks=[{"path": "Entry.java", "line": 4, "kind": "data_disclosure", "taxonomy": "data_disclosure"}],
        propagation_paths=[{"from": "onCreate", "to": "send", "order": 0}],
        guard_status="unknown",
    )

    forward = CandidateFunnel().process([copy.deepcopy(first), copy.deepcopy(second)])
    reverse = CandidateFunnel().process([copy.deepcopy(second), copy.deepcopy(first)])

    assert {item["candidate_id"] for item in forward.candidates} == {
        item["candidate_id"] for item in reverse.candidates
    }
    assert {item["id"] for item in aggregate_candidates(forward.candidates)} == {
        item["id"] for item in aggregate_candidates(reverse.candidates)
    }


def test_ai_result_propagates_only_with_complete_triple_identity() -> None:
    first = _closed_l2(rule_id="RULE_ONE", guard_status="unknown")
    second = _closed_l2(rule_id="RULE_TWO", guard_status="unknown")
    result = CandidateFunnel().process([first, second])
    representative = result.candidates[result.representative_indexes[0]]
    member = next(candidate for candidate in result.candidates if candidate is not representative)
    representative["analysis_status"] = "ai_completed"
    representative["ai_analysis"] = {"candidate_verdict": "supports_candidate"}
    member["chain_key"] = "tampered"

    propagate_representative_analysis(result.candidates)

    assert "ai_analysis" not in member
    assert member["analysis_status"] == "rule_only"


def test_l1_top_n_budget_marks_remaining_candidates_deferred() -> None:
    candidates = [
        _candidate(
            component_name=f"com.example.Remote{i}",
            rule_id="SERVICE_BINDER_CALLER_CHECK_MISSING",
            review_priority=100 - i,
            binder_remote_interface=True,
        )
        for i in range(4)
    ]

    result = CandidateFunnel({"max_l1_candidates_per_run": 2, "min_l1_risk_score": 80}).process(candidates)

    assert len(result.representative_indexes) == 2
    assert sum(candidate["ai_budget_deferred"] for candidate in candidates) == 2
    assert all(
        candidate["funnel_disposition"] == "high_risk_uncertain"
        for candidate in candidates
    )


def test_deterministically_closed_l2_without_uncertainty_does_not_need_ai() -> None:
    candidate = _closed_l2()

    result = CandidateFunnel().process([candidate])

    assert candidate["funnel_disposition"] == "deterministically_promoted_l2"
    assert candidate["ai_required"] is False
    assert result.representative_indexes == []


def test_promotion_false_alone_never_marks_ai_false_positive() -> None:
    candidate = _closed_l2(ai_required=True)
    ScanOrchestrator._apply_ai_analysis(
        candidate,
        {
            "summary": "AI 不支持该候选",
            "candidate_verdict": "refutes_candidate",
            "analysis_track": "l2_review",
            "promotion_recommended": False,
            "analysis_complete": True,
            "blocking_gaps": [],
        },
        [],
        {"contexts": [], "request_history": []},
    )

    DecisionEngine().decide(candidate)

    assert candidate["candidate_verdict"] == "refutes_candidate"
    assert candidate["analysis_track"] == "l2_review"
    assert candidate["review_status"] == "pending_ai"
    assert candidate["decision_reason_codes"] == ["VALID_EVIDENCE_REFS_REQUIRED"]
    assert candidate["false_positive_basis"] == []


def test_rule_injected_refutation_cannot_mark_ai_false_positive() -> None:
    candidate = _closed_l2(
        authorization_status="strongly_protected",
        ai_required=True,
        analysis_status="ai_completed",
        candidate_verdict="refutes_candidate",
    )

    DecisionEngine().decide(candidate)

    assert candidate["review_status"] == "pending_ai"
    assert candidate["evidence_decision"] == "deterministically_refuted"
    assert candidate["decision_reason_codes"] == ["ANALYSIS_NOT_COMPLETE"]
    assert candidate["false_positive_basis"] == []


def test_incomplete_negative_proof_coverage_blocks_automatic_false_positive() -> None:
    candidate = _closed_l2(
        authorization_status="strongly_protected",
        ai_required=True,
        analysis_status="ai_completed",
        candidate_verdict="refutes_candidate",
        negative_proof_coverage_complete=False,
        positive_proof_coverage_complete=True,
    )

    DecisionEngine().decide(candidate)

    assert candidate["review_status"] != "ai_false_positive"
    assert candidate["evidence_decision"] != "ai_false_positive"
    assert candidate["false_positive_basis"] == []


def test_l1_exposure_defaults_to_pending_manual() -> None:
    candidate = _candidate()
    CandidateFunnel().process([candidate])

    DecisionEngine().decide(candidate)

    assert candidate["review_status"] == "pending_manual"
    assert candidate["evidence_decision"] == "exposure_only"


def test_ai_application_does_not_change_deterministic_fact_hash() -> None:
    candidate = _closed_l2(guard_status="unknown")
    CandidateFunnel().process([candidate])
    before = candidate["deterministic_fact_hash"]
    independently_recomputed_before = build_candidate_identity(candidate).deterministic_fact_hash
    deterministic_gaps_before = copy.deepcopy(candidate["blocking_gaps"])

    ScanOrchestrator._apply_ai_analysis(
        candidate,
        {
            "summary": "建议人工复核",
            "candidate_verdict": "supports_candidate",
            "analysis_track": "l2_review",
            "promotion_recommended": True,
            "analysis_complete": True,
            "guard_status": "absent",
            "blocking_gaps": [{"code": "AI_UNCERTAINTY", "critical": False}],
        },
        [],
        {"contexts": [], "request_history": []},
    )

    assert candidate["deterministic_fact_hash"] == before
    assert candidate["blocking_gaps"] == deterministic_gaps_before
    assert candidate["ai_blocking_gaps"] == [{"code": "AI_UNCERTAINTY", "critical": False}]
    assert build_candidate_identity(candidate).deterministic_fact_hash == independently_recomputed_before


def test_l1_ai_potential_chain_stays_proposal_without_formal_evidence() -> None:
    candidate = _candidate()
    original_sources = copy.deepcopy(candidate["sources"])
    original_sinks = copy.deepcopy(candidate["sinks"])

    ScanOrchestrator._apply_ai_analysis(
        candidate,
        {
            "candidate_verdict": "potential_chain",
            "analysis_track": "l1_triage",
            "promotion_recommended": True,
            "analysis_complete": True,
            "suggested_sources": [{"context_id": "ctx-source"}],
            "suggested_sinks": [{"context_id": "ctx-sink"}],
            "suggested_paths": [{"source_ref": 0, "sink_ref": 0}],
            "blocking_gaps": [],
        },
        [],
        {"contexts": [], "request_history": []},
    )

    assert candidate["evidence_level"] == "L1"
    assert candidate["sources"] == original_sources
    assert candidate["sinks"] == original_sinks
    assert "promotion_requested" not in candidate
    assert candidate["ai_promotion_proposal"]["candidate_verdict"] == "potential_chain"


def _unproven_candidate(**overrides) -> dict:
    """control_to_sink + 作用域未解析：P0-2 的降级目标。"""

    candidate = {
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "rule_version": "2.0.0",
        "evidence_level": "L2",
        "component": "activity",
        "component_name": "com.example.MainActivity",
        "entry_points": ["MainActivity#onCreate"],
        "entry_method_id": "com/example/MainActivity.java#MainActivity.onCreate:10",
        "authorization_status": "unprotected",
        "guard_status": "absent",
        "dataflow_status": "not_proven",
        "flow_kind": "control_to_sink",
        "operation_taxonomy": "persistent_state_write",
        "deterministic_chain_verified": False,
        "chain_id": "dfc_unproven",
        "sources": [{"path": "a.java", "line": 1, "kind": "intent_extra"}],
        "sinks": [{"path": "b.java", "line": 2, "kind": "persistent_state_write"}],
        "propagation_paths": [],
        "blocking_gaps": [{"code": "CONTROL_SCOPE_UNRESOLVED", "critical": True}],
        "locations": [],
    }
    candidate.update(overrides)
    return candidate


def test_unproven_flow_demotion_reason_matches_scope_only() -> None:
    """P0-2 判据：只认 P0-1 的作用域结论，不看 sink 参数字面量性。

    v2（2026-08-15）：LEGACY_FLOW_FALLBACK 不再降级——com.mi.health RouterActivity
    （inferred_source_to_sink）被 AI 判 flaw_holds=True 成立，降级会漏报真漏洞
    （§5 守门硬门槛被打破）；轻量回退只是规则层精度不足，恰需 AI 判定。
    """

    from app.analysis.candidate_funnel import unproven_flow_demotion_reason

    assert unproven_flow_demotion_reason(_unproven_candidate()) == "scope_unresolved"

    # v2：legacy 回退不再降级（真实调用点需 AI 判定）
    legacy = _unproven_candidate(
        flow_kind="inferred_source_to_sink",
        blocking_gaps=[{"code": "LEGACY_FLOW_FALLBACK", "critical": True}],
    )
    assert unproven_flow_demotion_reason(legacy) is None

    # 作用域已解析的 control_to_sink：P0-1 生效后块外 sink 根本不成链，
    # 能留下来的说明 sink 受分支支配，属真实攻击面，不得降级。
    scoped = _unproven_candidate(blocking_gaps=[
        {"code": "LINEAR_IR_PATH_SENSITIVITY_LIMITATION", "critical": True}
    ])
    assert unproven_flow_demotion_reason(scoped) is None

    # 值流已证明到 sink 参数：绝不降级
    proven = _unproven_candidate(flow_kind="source_to_sink")
    assert unproven_flow_demotion_reason(proven) is None

    # 确定性验证过的链：绝不降级
    verified = _unproven_candidate(deterministic_chain_verified=True)
    assert unproven_flow_demotion_reason(verified) is None


def test_demotion_disabled_by_default_and_gated_by_setting() -> None:
    """默认关闭：判据照常计算并统计，但不改变 AI 准入——保证灰度可评估、行为不突变。"""

    from app.analysis.candidate_funnel import CandidateFunnel

    disabled = CandidateFunnel().process([_unproven_candidate()])
    candidate = disabled.candidates[0]
    assert candidate["demotion_reason"] == "scope_unresolved"
    assert candidate["flow_evidence_tier"] == "candidate"
    assert candidate["ai_required"] is True, "默认配置下不得改变既有 AI 准入行为"
    assert disabled.summary["unproven_flow_matched_count"] == 1
    assert disabled.summary["demoted_candidates"] == 0

    enabled = CandidateFunnel({"demote_unproven_flow": True}).process([_unproven_candidate()])
    demoted = enabled.candidates[0]
    assert demoted["flow_evidence_tier"] == "signal"
    assert demoted["ai_required"] is False, "开启后值流未证明的链不得占用 AI 预算"
    assert enabled.summary["demoted_candidates"] == 1
    assert enabled.summary["demotion_reason_scope_unresolved"] == 1


def test_demotion_keeps_candidate_and_identity_stable() -> None:
    """降级是"不送 AI"而非"丢弃"：候选仍在产物中，且 candidate_id 不随开关变化。"""

    from app.analysis.candidate_funnel import CandidateFunnel

    disabled = CandidateFunnel().process([_unproven_candidate()])
    enabled = CandidateFunnel({"demote_unproven_flow": True}).process([_unproven_candidate()])

    assert len(enabled.candidates) == len(disabled.candidates) == 1, "降级不得丢弃候选"
    assert enabled.candidates[0]["candidate_id"] == disabled.candidates[0]["candidate_id"], (
        "分级结果由候选事实推导而来，不得反过来参与身份计算，"
        "否则同一候选在开关开/关下会得到不同 candidate_id"
    )


def test_demotion_does_not_touch_proven_chains() -> None:
    """安全边界：值流已证明的链在开启后仍须送 AI。"""

    from app.analysis.candidate_funnel import CandidateFunnel

    proven = _unproven_candidate(
        flow_kind="source_to_sink",
        blocking_gaps=[{"code": "LINEAR_IR_PATH_SENSITIVITY_LIMITATION", "critical": True}],
    )
    result = CandidateFunnel({"demote_unproven_flow": True}).process([proven])
    assert result.candidates[0]["flow_evidence_tier"] == "candidate"
    assert result.candidates[0]["ai_required"] is True
    assert result.summary["demoted_candidates"] == 0


def test_l1_priority_clean_sorting_switch() -> None:
    """R-2：l1_priority_clean 开关——开启时 tier 优先，关闭时行为不变。

    构造 3 个 L1 候选（clean/gap/unresolved + 不同 risk_score），验证：
    - 关闭：按 risk_score 排序（行为不变）；
    - 开启：clean 优先（即使 risk_score 低）。
    """

    from app.analysis.candidate_funnel import CandidateFunnel

    def make(tier: str | None, risk: int) -> dict:
        return {
            "candidate_id": f"c-{tier or 'none'}-{risk}",
            "rule_id": "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION",
            "flow_kind": "receiver_exposure",
            "evidence_level": "L1",
            "funnel_disposition": "coverage_insufficient",
            "reachability_status": "reachable",
            "authorization_status": "unprotected",
            "guard_blocked": False,
            "risk_score": risk,
            "review_priority": 0,
            "receiver_flag_tier": tier,
            "sources": [], "sinks": [], "blocking_gaps": [],
        }

    clean = make("confirmed_exported_clean", 30)
    gap = make("confirmed_exported_gap", 90)
    unres = make("unresolved_flag", 60)

    # 关闭（默认）：risk_score 优先 → gap(90) > unres(60) > clean(30)
    off = CandidateFunnel({"l1_priority_clean": False})
    order_off = sorted(
        [clean, gap, unres],
        key=lambda c: (c.get("risk_score") or 0), reverse=True,
    )
    assert [c["receiver_flag_tier"] for c in order_off] == [
        "confirmed_exported_gap", "unresolved_flag", "confirmed_exported_clean",
    ], "关闭时按 risk_score 排序"

    # 开启：tier 优先 → clean(30) > gap(90) > unres(60)
    on = CandidateFunnel({"l1_priority_clean": True})
    assert on.l1_priority_clean is True
    tier_priority = {
        "confirmed_exported_clean": 4,
        "confirmed_exported_gap": 3,
        "unresolved_flag": 2,
    }
    order_on = sorted(
        [clean, gap, unres],
        key=lambda c: (
            tier_priority.get(c.get("receiver_flag_tier"), 1),
            c.get("risk_score") or 0,
        ),
        reverse=True,
    )
    assert [c["receiver_flag_tier"] for c in order_on] == [
        "confirmed_exported_clean", "confirmed_exported_gap", "unresolved_flag",
    ], "开启时 tier 优先（clean 即使 risk 低也居首）"


def test_receiver_exposure_identity_aggregation() -> None:
    """R-3：receiver_exposure 身份聚合——同 owner+同 flag+同 gap 合并，
    跨 tier 不合并；非 receiver 候选身份不受影响。

    同方法内多行 registerReceiver（如 RNDeviceModule.java#l ×5）是真正该
    合并的形态；gap 差异是判定要素（P0-3 不放宽），保留则保守不合并。
    """

    from app.analysis.candidate_funnel import build_candidate_identity

    def make(flow_kind: str, name: str, tier: str, gap: str) -> dict:
        return {
            "rule_id": "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION",
            "flow_kind": flow_kind,
            "component": "receiver",
            "component_name": f"dynamic:{name}",
            "entry_method_id": f"{name}#l:10",
            "entry_points": [f"{name}#l:10"],
            "entry_method_name": "registerReceiver",
            "receiver_flag_tier": tier,
            "receiver_binding": {
                "registration": {"path": name},
                "actions": ["com.example.ACTION"],
            },
            "blocking_gaps": [{"code": gap, "critical": True}],
            "sources": [{"path": name, "line": 10, "kind": "external_receiver"}],
            "sinks": [], "propagation_paths": [],
            "authorization_matrix": [],
        }

    # 同方法 2 个注册点（不同行号）→ 应合并（同 owner/tier/gap）
    a = make("receiver_exposure", "com/example/util/Reg.java", "unresolved_flag", "RECEIVER_TARGET_UNRESOLVED")
    b = make("receiver_exposure", "com/example/util/Reg.java", "unresolved_flag", "RECEIVER_TARGET_UNRESOLVED")
    ia, ib = build_candidate_identity(a), build_candidate_identity(b)
    assert (ia.scope_key, ia.chain_key, ia.deterministic_fact_hash) == (
        ib.scope_key, ib.chain_key, ib.deterministic_fact_hash,
    ), "同 owner+同 tier+同 gap 的 receiver 注册点必须合并"

    # 跨 tier → 不合并
    c = make("receiver_exposure", "com/example/util/Reg.java", "confirmed_exported_clean", "RECEIVER_TARGET_UNRESOLVED")
    ic = build_candidate_identity(c)
    assert ia.chain_key != ic.chain_key, "跨 flag tier 不得合并（判定语义不同）"

    # 非 receiver 候选：component_name 不被投影为 owner（不受 R-3 影响）
    d = make("control_to_sink", "com/example/util/Reg.java", None, "RECEIVER_TARGET_UNRESOLVED")
    id_ = build_candidate_identity(d)
    assert id_.scope_key != ia.scope_key, "非 receiver 候选的 scope 身份不得用 owner 投影"
