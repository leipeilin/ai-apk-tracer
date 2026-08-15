from __future__ import annotations

from copy import deepcopy

import pytest

from app.findings.decision import DECISION_VERSION, DecisionEngine, decide_candidate
from app.findings.evidence import validate_ai_evidence_references, verify_candidate


VALID_REF = {"context_id": "ctx-1", "line": 12, "claim": "verified"}


def l1_candidate(**overrides: object) -> dict:
    candidate = {
        "evidence_level": "L1",
        "analysis_status": "ai_completed",
        "analysis_complete": True,
        "outcome": "exposure_only",
        "verified_evidence_refs": [VALID_REF],
        "invalid_evidence_refs": [],
        "blocking_gaps": [],
        "coverage_gaps": [],
        "severity_hint": "high",
    }
    candidate.update(overrides)
    return candidate


def l2_candidate(**overrides: object) -> dict:
    candidate = {
        "evidence_level": "L2",
        "analysis_status": "ai_completed",
        "analysis_complete": True,
        "verdict": "supports_candidate",
        "verified_evidence_refs": [VALID_REF],
        "invalid_evidence_refs": [],
        "blocking_gaps": [],
        "coverage_gaps": [],
        "deterministic_chain_verified": True,
        "dataflow_status": "interprocedural",
        "guard_status": "absent",
        "authorization_status": "unprotected",
        "impact_status": "confirmed",
        "severity_hint": "high",
    }
    candidate.update(overrides)
    return candidate


@pytest.mark.parametrize(
    ("candidate", "expected_status", "expected_severity", "reason_code"),
    [
        (
            l1_candidate(analysis_status="ai_failed"),
            "pending_ai",
            "informational",
            "ANALYSIS_FAILED",
        ),
        (
            l1_candidate(analysis_complete=False),
            "pending_ai",
            "informational",
            "ANALYSIS_NOT_COMPLETE",
        ),
        (
            l1_candidate(),
            "pending_manual",
            "informational",
            "L1_EXPOSURE_ONLY_COMPLETE",
        ),
        (
            l1_candidate(blocking_gaps=[{"code": "MISSING_ENTRY", "critical": True}]),
            "pending_ai",
            "informational",
            "CRITICAL_BLOCKING_OR_COVERAGE_GAP",
        ),
        (
            l1_candidate(outcome="potential_chain", promotion_recommended=True),
            "pending_ai",
            "informational",
            "L1_POTENTIAL_CHAIN_REQUIRES_VALIDATED_PROMOTION",
        ),
        (
            l2_candidate(outcome="positive", verdict=None),
            "pending_manual",
            "high",
            "L2_POSITIVE_GATES_PASSED",
        ),
        (
            l2_candidate(deterministic_chain_verified=False),
            "pending_ai",
            "high",
            "L2_POSITIVE_GATES_NOT_PROVEN",
        ),
        (
            l2_candidate(invalid_evidence_refs=[{"context_id": "missing"}]),
            "pending_ai",
            "high",
            "INVALID_EVIDENCE_REFS",
        ),
        (
            l2_candidate(coverage_gaps=[{"code": "CALLER_UNKNOWN", "critical": True}]),
            "pending_ai",
            "pending",
            "CRITICAL_BLOCKING_OR_COVERAGE_GAP",
        ),
        (
            l2_candidate(ai_analysis_status="skipped"),
            "pending_ai",
            "high",
            "ANALYSIS_SKIPPED",
        ),
        (
            l2_candidate(verdict="refutes_candidate", deterministic_negative_proof=True),
            "ai_false_positive",
            "high",
            "L2_REFUTED_WITH_DETERMINISTIC_NEGATIVE_PROOF",
        ),
        (
            l2_candidate(verdict="refutes_candidate", deterministic_chain_verified=False),
            "pending_manual",
            "high",
            "L2_REFUTED_WITHOUT_DETERMINISTIC_NEGATIVE_PROOF",
        ),
        (
            l2_candidate(
                verdict="refutes_candidate",
                authorization_status="strongly_protected",
            ),
            "ai_false_positive",
            "informational",
            "L2_REFUTED_WITH_DETERMINISTIC_NEGATIVE_PROOF",
        ),
        (
            l2_candidate(
                verdict="refutes_candidate",
                deterministic_chain_verified=False,
                disconnected_verified=True,
            ),
            "ai_false_positive",
            "high",
            "L2_REFUTED_WITH_DETERMINISTIC_NEGATIVE_PROOF",
        ),
        (
            l2_candidate(
                verdict="refutes_candidate",
                deterministic_chain_verified=False,
                ai_analysis={"deterministic_negative_proof": True},
            ),
            "pending_manual",
            "high",
            "L2_REFUTED_WITHOUT_DETERMINISTIC_NEGATIVE_PROOF",
        ),
        (
            l2_candidate(
                analysis_status=None,
                analysis_complete=None,
                verdict=None,
                ai_analysis={
                    "ai_analysis_status": "completed",
                    "analysis_complete": True,
                    "promotion_recommended": True,
                    "verified_evidence_refs": [VALID_REF],
                    "invalid_evidence_refs": [],
                },
                verified_evidence_refs=None,
                invalid_evidence_refs=None,
            ),
            "pending_manual",
            "high",
            "L2_POSITIVE_GATES_PASSED",
        ),
    ],
)
def test_decision_matrix(
    candidate: dict,
    expected_status: str,
    expected_severity: str,
    reason_code: str,
) -> None:
    original = deepcopy(candidate)

    decision = decide_candidate(candidate)

    assert decision == {
        "evidence_level": candidate["evidence_level"],
        "severity": expected_severity,
        "review_status": expected_status,
        "reason_codes": [reason_code],
        "decision_version": DECISION_VERSION,
    }
    assert candidate == original


@pytest.mark.parametrize("terminal_status", ["confirmed", "manual_false_positive"])
def test_manual_terminal_status_survives_automatic_rerun(terminal_status: str) -> None:
    candidate = l2_candidate(
        review_status=terminal_status,
        analysis_status="ai_failed",
        analysis_complete=False,
        invalid_evidence_refs=[{"context_id": "missing"}],
    )

    decision = decide_candidate(candidate)

    assert decision["review_status"] == terminal_status
    assert decision["reason_codes"] == ["MANUAL_TERMINAL_STATUS_PRESERVED"]


AI_CONTEXTS = [
    {
        "context_id": "manifest:activity:com.example.Entry",
        "kind": "manifest_component",
        "path": "AndroidManifest.xml",
        "start_line": 0,
        "end_line": 0,
    },
    {
        "context_id": "entry-method",
        "kind": "method",
        "path": "Entry.java",
        "start_line": 1,
        "end_line": 40,
        "method_name": "onCreate",
    },
]


def production_candidate(**overrides: object) -> dict:
    refs = [
        {"context_id": "manifest:activity:com.example.Entry", "claim": "exported without permission"},
        {"context_id": "entry-method", "path": "Entry.java", "line": 12, "claim": "external input"},
        {"context_id": "entry-method", "path": "Entry.java", "line": 30, "claim": "sensitive sink"},
    ]
    candidate = l2_candidate(
        ai_required=True,
        component="activity",
        component_name="com.example.Entry",
        entry_points=["onCreate"],
        locations=[{"path": "Entry.java", "line": 1}],
        sources=[{"path": "Entry.java", "line": 12}],
        sinks=[{"path": "Entry.java", "line": 30}],
        analysis_status="ai_completed",
        analysis_complete=None,
        verdict=None,
        verified_evidence_refs=None,
        invalid_evidence_refs=None,
        analysis_track="l2_review",
        ai_analysis={
            "analysis_track": "l2_review",
            "verdict": "supports_candidate",
            "analysis_complete": True,
            "evidence_refs": refs,
            "blocking_gaps": [],
            "verified_evidence_refs": [
                {**ref, "path": "AndroidManifest.xml" if ref["context_id"].startswith("manifest:") else "Entry.java"}
                for ref in refs
            ],
            "invalid_evidence_refs": [],
        },
        slice_refs=[context["context_id"] for context in AI_CONTEXTS],
    )
    # v2026-08-14（决策层矛盾③修复）：supported 分支要求 entry_method_id 非空
    # （死代码方法不得 supported）。production_candidate 代表"生产验证通过 +
    # 数据流已证"的真实 supported 场景，真实规则候选 entry_method_id=entry_points[0]。
    candidate.setdefault("entry_method_id", "Entry.java#com.example.Entry.onCreate:12")
    candidate.update(overrides)
    return candidate


@pytest.mark.parametrize(
    ("reference", "reason"),
    [
        ({"context_id": "missing", "line": 12, "claim": "missing"}, "CONTEXT_ID_NOT_FOUND"),
        (
            {"context_id": "entry-method", "path": "Other.java", "line": 12, "claim": "wrong path"},
            "PATH_MISMATCH",
        ),
        ({"context_id": "entry-method", "claim": "no line"}, "LINE_REQUIRED_FOR_CONCRETE_EVIDENCE"),
        ({"context_id": "entry-method", "line": True, "claim": "bool line"}, "LINE_INVALID"),
        (
            {"context_id": "entry-method", "line": 12, "end_line": 41, "claim": "outside"},
            "LINE_OUTSIDE_CONTEXT",
        ),
        (
            {"context_id": "entry-method", "line": 20, "end_line": 19, "claim": "reversed"},
            "END_LINE_BEFORE_LINE",
        ),
    ],
)
def test_ai_evidence_reference_validation_fails_closed(reference: dict, reason: str) -> None:
    candidate = production_candidate()
    candidate["ai_analysis"] = {**candidate["ai_analysis"], "evidence_refs": [reference]}

    validation = validate_ai_evidence_references(candidate, AI_CONTEXTS)

    assert validation["evidence_refs_valid"] is False
    assert validation["semantic_evidence_complete"] is False
    assert validation["invalid_evidence_refs"][0]["reason"] == reason
    assert any(gap["code"] == "AI_EVIDENCE_REF_INVALID" and gap["critical"] for gap in validation["ai_evidence_blocking_gaps"])


def test_ai_evidence_semantic_completeness_reports_missing_role_and_domain() -> None:
    candidate = production_candidate()
    candidate["ai_analysis"] = {
        **candidate["ai_analysis"],
        "evidence_refs": candidate["ai_analysis"]["evidence_refs"][:2],
    }

    validation = validate_ai_evidence_references(candidate, AI_CONTEXTS)

    assert validation["evidence_refs_valid"] is True
    assert validation["semantic_evidence_complete"] is False
    assert validation["missing_evidence_roles"] == ["sink"]
    assert validation["missing_evidence_domains"] == ["impact"]
    assert validation["required_evidence_domains"] == ["authorization", "dataflow", "impact"]


def test_production_verification_and_apply_accept_complete_semantic_evidence() -> None:
    candidate = production_candidate()
    code_index = {
        "files": [{
            "path": "Entry.java",
            "line_count": 40,
            "content": "\n".join(f"line {line}" for line in range(1, 41)),
            "classes": [],
            "methods": [{
                "id": "entry-method",
                "name": "onCreate",
                "symbol_key": "com.example.Entry#onCreate",
                "start_line": 1,
                "end_line": 40,
            }],
        }]
    }

    verified = verify_candidate(candidate, code_index)
    DecisionEngine().apply([verified])

    assert verified["evidence_refs_valid"] is True
    assert verified["semantic_evidence_complete"] is True
    assert verified["required_evidence_roles"] == ["sink", "source"]
    assert verified["evidence_decision"] == "supported"
    assert verified["review_status"] == "pending_manual"
    assert verified["decision_reason_codes"] == []


def test_l2_finalization_with_valid_refs_is_supported_but_recommendation_cannot_decide() -> None:
    candidate = production_candidate(ai_evidence_contexts=AI_CONTEXTS, analysis_track="finalization")
    candidate["ai_analysis"] = {
        **candidate["ai_analysis"],
        "analysis_track": "finalization",
        "source_analysis_track": "l2_review",
        "review_recommendation": "ai_false_positive",
    }

    verified = verify_candidate(candidate, {
        "files": [{
            "path": "Entry.java",
            "line_count": 40,
            "content": "\n".join(f"line {line}" for line in range(1, 41)),
            "classes": [],
            "methods": [],
        }]
    })
    DecisionEngine().apply([verified])

    assert verified["semantic_evidence_complete"] is True
    assert verified["evidence_decision"] == "supported"
    assert verified["review_status"] == "pending_manual"
    assert verified["decision_reason_codes"] == []


@pytest.mark.parametrize(
    "refs",
    [
        [],
        [{"context_id": "missing", "line": 12, "claim": "not in final slice"}],
    ],
)
def test_l2_finalization_with_empty_or_invalid_refs_stays_pending_ai(refs: list[dict]) -> None:
    candidate = production_candidate(ai_evidence_contexts=AI_CONTEXTS, analysis_track="finalization")
    candidate["ai_analysis"] = {
        **candidate["ai_analysis"],
        "analysis_track": "finalization",
        "source_analysis_track": "l2_review",
        "review_recommendation": "confirmed",
        "evidence_refs": refs,
    }

    DecisionEngine().apply([candidate])

    assert candidate["evidence_decision"] == "unresolved"
    assert candidate["review_status"] == "pending_ai"
    assert candidate["decision_reason_codes"] in (
        ["INVALID_EVIDENCE_REFS"],
        ["VALID_EVIDENCE_REFS_REQUIRED"],
    )


def test_apply_rejects_rule_injected_candidate_verdict_without_ai_evidence() -> None:
    candidate = l2_candidate(
        analysis_status="rule_only",
        candidate_verdict="refutes_candidate",
        analysis_track="l2_review",
        ai_analysis=None,
        authorization_status="strongly_protected",
    )

    DecisionEngine().apply([candidate])

    assert candidate["evidence_decision"] == "deterministically_refuted"
    assert candidate["review_status"] == "pending_ai"
    assert candidate["decision_reason_codes"] == ["ANALYSIS_NOT_COMPLETE"]
    assert candidate["false_positive_basis"] == []


def test_apply_rejects_raw_positive_verdict_with_invalid_reference() -> None:
    candidate = production_candidate(ai_evidence_contexts=AI_CONTEXTS)
    refs = list(candidate["ai_analysis"]["evidence_refs"])
    refs[1] = {**refs[1], "path": "Other.java"}
    candidate["ai_analysis"] = {**candidate["ai_analysis"], "evidence_refs": refs}

    DecisionEngine().apply([candidate])

    assert candidate["evidence_decision"] == "unresolved"
    assert candidate["review_status"] == "pending_ai"
    assert candidate["decision_reason_codes"] == ["INVALID_EVIDENCE_REFS"]
    assert candidate["invalid_evidence_refs"][0]["reason"] == "PATH_MISMATCH"


def test_apply_requires_analysis_complete_and_preserves_deterministic_refutation() -> None:
    candidate = production_candidate(
        ai_evidence_contexts=AI_CONTEXTS,
        authorization_status="strongly_protected",
    )
    candidate["ai_analysis"] = {
        **candidate["ai_analysis"],
        "verdict": "refutes_candidate",
        "analysis_complete": False,
        "evidence_refs": [candidate["ai_analysis"]["evidence_refs"][0]],
    }

    DecisionEngine().apply([candidate])

    assert candidate["evidence_decision"] == "deterministically_refuted"
    assert candidate["false_positive_basis"] == []
    assert candidate["review_status"] == "pending_ai"
    assert candidate["decision_reason_codes"] == ["ANALYSIS_NOT_COMPLETE"]


def test_apply_allows_refutation_only_with_complete_applicable_evidence() -> None:
    candidate = production_candidate(
        ai_evidence_contexts=AI_CONTEXTS,
        authorization_status="strongly_protected",
    )
    candidate["ai_analysis"] = {
        **candidate["ai_analysis"],
        "verdict": "refutes_candidate",
        "evidence_refs": [candidate["ai_analysis"]["evidence_refs"][0]],
    }

    DecisionEngine().apply([candidate])

    assert candidate["semantic_evidence_complete"] is True
    assert candidate["evidence_decision"] == "ai_false_positive"
    assert candidate["review_status"] == "ai_false_positive"
    assert candidate["false_positive_basis"]


def test_apply_blocks_complete_evidence_when_critical_gap_remains() -> None:
    candidate = production_candidate(
        ai_evidence_contexts=AI_CONTEXTS,
        coverage_gaps=[{"code": "GUARD_BLOCKED", "critical": True}],
    )

    DecisionEngine().apply([candidate])

    assert candidate["semantic_evidence_complete"] is True
    assert candidate["evidence_decision"] == "unresolved"
    assert candidate["review_status"] == "pending_ai"
    assert candidate["decision_reason_codes"] == ["CRITICAL_BLOCKING_OR_COVERAGE_GAP"]


def test_apply_uses_only_coverage_gaps_applicable_to_refutation() -> None:
    candidate = production_candidate(
        ai_evidence_contexts=AI_CONTEXTS,
        authorization_status="strongly_protected",
        coverage_gaps=[{
            "code": "POSITIVE_PATH_PARTIAL",
            "critical": True,
            "claim_impact": "positive_proof",
        }],
    )
    candidate["ai_analysis"] = {
        **candidate["ai_analysis"],
        "verdict": "refutes_candidate",
        "evidence_refs": [candidate["ai_analysis"]["evidence_refs"][0]],
    }

    DecisionEngine().apply([candidate])

    assert candidate["semantic_evidence_complete"] is True
    assert candidate["evidence_decision"] == "ai_false_positive"
    assert candidate["review_status"] == "ai_false_positive"
    assert candidate["decision_reason_codes"] == []


def _local_broadcast_candidate(**overrides: object) -> dict:
    """SDK 语义反证测试候选：LocalBroadcastManager 进程内分发。"""
    candidate = l2_candidate(
        deterministic_chain_verified=False,
        impact_status="potential",
        verdict="refutes_candidate",
        candidate_verdict="refutes_candidate",
        sinks=[{
            "kind": "implicit_broadcast",
            "receiver_text": "LocalBroadcastManager.getInstance(getAppContext())",
            "effect_verified": False,
        }],
        ai_analysis={
            "verdict": "refutes_candidate",
            "analysis_complete": True,
            "semantic_evidence_complete": True,
            "verified_evidence_refs": [VALID_REF],
            "exploitability": {"exfiltration_channel": "absent"},
        },
        blocking_gaps=[],
        coverage_gaps=[{"code": "EXFILTRATION_CHANNEL_ABSENT", "critical": True}],
    )
    candidate.update(overrides)
    return candidate


def test_sdk_semantic_refutation_bypasses_coverage_when_ai_refutes() -> None:
    """AI refutes + LocalBroadcast（SDK 语义反证）→ ai_false_positive，即使 coverage 保守。"""

    candidate = _local_broadcast_candidate()
    DecisionEngine().apply([candidate])

    assert candidate["evidence_decision"] == "ai_false_positive"
    assert "local_broadcast_intra_process" in candidate["false_positive_basis"]


def test_sdk_semantic_refutation_deterministically_refutes_without_ai() -> None:
    """LocalBroadcast 确定性反证不依赖 AI——rule_only（无 AI 判定）也应 deterministically_refuted。"""

    candidate = _local_broadcast_candidate(
        analysis_status="rule_only",
        ai_analysis={},
        verdict=None,
        candidate_verdict=None,
    )
    DecisionEngine().apply([candidate])

    assert candidate["evidence_decision"] == "deterministically_refuted"


def test_non_sdk_refutation_still_respects_coverage() -> None:
    """非 SDK 语义反证（如 strong_permission）仍受 coverage 保守保护。"""

    candidate = _local_broadcast_candidate(
        verdict="refutes_candidate",
        candidate_verdict="refutes_candidate",
        sinks=[],
        authorization_status="strongly_protected",
        coverage_gaps=[{"code": "PERMISSION_UNVERIFIED", "critical": True}],
    )
    DecisionEngine().apply([candidate])

    # coverage 挡（critical gap 无 claim_impact）→ 非 SDK 反证被清除 → unresolved
    assert candidate["evidence_decision"] == "unresolved"


def test_sdk_semantic_refutation_matches_eventbus_and_respects_word_boundary() -> None:
    """EventBus 命中 SDK 反证；EventBusUtils 类名不得误匹配（单词边界）。"""

    from app.findings.decision import _has_sdk_semantic_refutation

    assert _has_sdk_semantic_refutation({
        "sinks": [{"receiver_text": "EventBus.getDefault()"}],
    }) is True
    assert _has_sdk_semantic_refutation({
        "sinks": [{"receiver_text": "EventBusUtils.dispatch(event)"}],
    }) is False, "EventBusUtils 不是 EventBus，不得误判为进程内分发"
    assert _has_sdk_semantic_refutation({
        "sinks": [{"receiver_text": "context.sendBroadcast(intent)"}],
    }) is False
    assert _has_sdk_semantic_refutation({"sinks": None}) is False


def test_guard_blocked_yields_blocked_state() -> None:
    """方案 X'：guard_blocked 候选 → evidence_decision=blocked（区别于误报）。"""

    candidate = _local_broadcast_candidate(
        verdict="unresolved",
        candidate_verdict="unresolved",
        analysis_status="rule_only",
        ai_analysis={},
        guard_blocks=[{"type": "debuggable", "path": "ADBDebugActivity.java", "line": 43}],
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "blocked"
    # blocked 不是误报：false_positive_basis 为空
    rs = candidate.get("review_state") or {}
    assert rs.get("false_positive_basis") == []


def test_guard_blocked_does_not_override_ai_refutes() -> None:
    """guard_blocked + AI refutes 仍走否定路径（guard 佐证否定）。"""

    candidate = _local_broadcast_candidate(
        guard_blocks=[{"type": "debuggable", "path": "p", "line": 1}],
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] in {"ai_false_positive", "deterministically_refuted"}


def test_severity_blocked_gives_pending_with_reason() -> None:
    """方案 X'：blocked 候选 severity=pending（不可定级），reason 说明 guard 阻断。"""

    from app.findings.severity import determine_severity

    candidate = {
        "evidence_decision": "blocked",
        "evidence_level": "L2",
        "guard_blocks": [{"type": "debuggable", "path": "p", "line": 1}],
    }
    severity, reasons = determine_severity(candidate)
    assert severity == "pending"
    assert any("guard" in r for r in reasons)


def _entry_code_index() -> dict:
    """构造包含 Entry.java 与 Extra.java 的 code_index，Extra 方法不在 slice_refs 白名单。"""
    return {
        "files": [
            {
                "path": "Entry.java",
                "line_count": 40,
                "content": "\n".join(f"line {line}" for line in range(1, 41)),
                "classes": [],
                "methods": [{
                    "id": "entry-method",
                    "name": "onCreate",
                    "symbol_key": "com.example.Entry#onCreate",
                    "start_line": 1,
                    "end_line": 40,
                }],
            },
            {
                "path": "Extra.java",
                "line_count": 20,
                "content": "\n".join(f"line {line}" for line in range(1, 21)),
                "classes": [],
                "methods": [{
                    "id": "Extra.java#Extra.doSink:10",
                    "name": "doSink",
                    "symbol_key": "com.example.Extra#doSink",
                    "start_line": 10,
                    "end_line": 16,
                }],
            },
        ]
    }


def test_index_resolve_accepts_ref_to_indexed_method_outside_slice_refs() -> None:
    """v3.0.5 索引回查：AI 引用 slice_refs 未登记、但索引中真实存在的方法应通过校验。"""
    candidate = production_candidate()
    extra_ref = {
        "context_id": "Extra.java#Extra.doSink:10",
        "path": "Extra.java",
        "line": 12,
        "end_line": 12,
        "claim": "onCreate 沿调用链进入 Extra.doSink 敏感写入。",
    }
    candidate["ai_analysis"] = {
        **candidate["ai_analysis"],
        "evidence_refs": candidate["ai_analysis"]["evidence_refs"] + [extra_ref],
    }

    verified = verify_candidate(candidate, _entry_code_index())

    assert verified["evidence_refs_valid"] is True
    assert verified["invalid_evidence_refs"] == []
    assert extra_ref["context_id"] in {
        ref.get("context_id") for ref in verified["verified_evidence_refs"]
    }


def test_index_resolve_keeps_rejecting_unknown_context_id() -> None:
    """防幻觉底线：索引中不存在的方法引用仍必须判 CONTEXT_ID_NOT_FOUND。"""
    candidate = production_candidate()
    ghost_ref = {
        "context_id": "Ghost.java#Ghost.doSink:1",
        "path": "Ghost.java",
        "line": 3,
        "claim": "幻觉方法引用。",
    }
    candidate["ai_analysis"] = {
        **candidate["ai_analysis"],
        "evidence_refs": candidate["ai_analysis"]["evidence_refs"] + [ghost_ref],
    }

    verified = verify_candidate(candidate, _entry_code_index())

    assert verified["evidence_refs_valid"] is False
    assert any(
        ref.get("reason") == "CONTEXT_ID_NOT_FOUND"
        for ref in verified["invalid_evidence_refs"]
    )


def test_index_resolve_keeps_rejecting_line_outside_method() -> None:
    """索引回查命中后行号仍须落在方法范围内，越界引用保持无效。"""
    candidate = production_candidate()
    out_of_range_ref = {
        "context_id": "Extra.java#Extra.doSink:10",
        "path": "Extra.java",
        "line": 18,
        "end_line": 18,
        "claim": "行号超出 Extra.doSink 范围（10-16）。",
    }
    candidate["ai_analysis"] = {
        **candidate["ai_analysis"],
        "evidence_refs": candidate["ai_analysis"]["evidence_refs"] + [out_of_range_ref],
    }

    verified = verify_candidate(candidate, _entry_code_index())

    assert verified["evidence_refs_valid"] is False
    assert any(
        ref.get("reason") == "LINE_OUTSIDE_CONTEXT"
        for ref in verified["invalid_evidence_refs"]
    )


def test_empty_ai_evidence_contexts_does_not_skip_index_resolve() -> None:
    """防御：候选带空列表 ai_evidence_contexts=[] 时，不得吞掉索引回查（v3.0.5）。"""
    candidate = production_candidate(ai_evidence_contexts=[])
    extra_ref = {
        "context_id": "Extra.java#Extra.doSink:10",
        "path": "Extra.java",
        "line": 12,
        "end_line": 12,
        "claim": "索引回查应恢复 Extra.doSink 上下文。",
    }
    candidate["ai_analysis"] = {
        **candidate["ai_analysis"],
        "evidence_refs": candidate["ai_analysis"]["evidence_refs"] + [extra_ref],
    }

    verified = verify_candidate(candidate, _entry_code_index())

    assert verified["evidence_refs_valid"] is True
    assert verified["invalid_evidence_refs"] == []
    assert extra_ref["context_id"] in {
        ref.get("context_id") for ref in verified["verified_evidence_refs"]
    }


def test_empty_supplied_ai_contexts_does_not_overwrite_verified_evidence() -> None:
    """防御：DecisionEngine.apply 时空列表 ai_evidence_contexts 不得覆盖已救回的校验结果。"""
    candidate = production_candidate(ai_evidence_contexts=[])
    extra_ref = {
        "context_id": "Extra.java#Extra.doSink:10",
        "path": "Extra.java",
        "line": 12,
        "end_line": 12,
        "claim": "apply 后索引回查结果应保留。",
    }
    candidate["ai_analysis"] = {
        **candidate["ai_analysis"],
        "evidence_refs": candidate["ai_analysis"]["evidence_refs"] + [extra_ref],
    }
    verified = verify_candidate(candidate, _entry_code_index())
    assert verified["evidence_refs_valid"] is True

    DecisionEngine().apply([verified])

    assert verified["evidence_refs_valid"] is True
    assert verified["invalid_evidence_refs"] == []
    assert extra_ref["context_id"] in {
        ref.get("context_id") for ref in verified["verified_evidence_refs"]
    }


def test_stale_validator_gaps_cleared_on_reverify() -> None:
    """幂等（v3.0.5）：候选携带旧校验残留 AI_EVIDENCE_REF_INVALID 时，重新 verify
    应清除该残留（本次校验通过），不被已失效的 gap 拦截。"""

    candidate = production_candidate(
        ai_blocking_gaps=[
            {"code": "AI_EVIDENCE_REF_INVALID", "critical": True, "count": 2},
            {"code": "DATAFLOW_NOT_PROVEN", "critical": True},
        ],
    )
    candidate["ai_analysis"] = {
        **candidate["ai_analysis"],
        "blocking_gaps": [
            {"code": "AI_EVIDENCE_REF_INVALID", "critical": True, "count": 2},
            {"code": "DATAFLOW_NOT_PROVEN", "critical": True},
        ],
    }

    verified = verify_candidate(candidate, _entry_code_index())
    DecisionEngine().apply([verified])

    assert verified["evidence_refs_valid"] is True
    assert all(
        gap.get("code") != "AI_EVIDENCE_REF_INVALID"
        for gap in verified.get("ai_blocking_gaps", [])
    )
    # production_candidate 数据流已证 → 可能 supported；否则降级采信 ai_likely_supported。
    # 两种都是采信，关键是残留 gap 已被清除、不落 unresolved。
    assert verified["evidence_decision"] in {"supported", "ai_likely_supported"}
    assert verified["review_status"] == "pending_manual"


def test_likely_false_positive_rejected_when_critical_gap_present() -> None:
    """v2026-08-14（矛盾①修复）：AI 在证据严重不足（critical gap）下判 flaw=False
    是"没找到成立的证据"而非"找到不成立的证据"——不得采信为 ai_likely_false_positive，
    应落 unresolved（实测 6×MainTabActivity：flaw=False + 3 critical gap）。"""

    candidate = l2_candidate(
        analysis_status="ai_completed",
        ai_analysis={
            "summary": "数据流未证明，判定方向不确定",
            "verdict": "unresolved",
            "confidence_tier": "low",
            "analysis_complete": True,
            "semantic_evidence_complete": True,
            "verified_evidence_refs": [VALID_REF],
            "evidence_refs": [VALID_REF],
            "flaw_holds": False,
            "exploitability": {
                "entry_reachable": False, "propagation_proven": False,
                "sink_effective": False, "guard_bypassed": False,
                "authorization_absent": True, "exfiltration_channel": "unverified",
            },
            "blocking_gaps": [
                {"code": "DATAFLOW_NOT_PROVEN", "critical": True},
                {"code": "SYMBOL_TARGET_AMBIGUOUS", "critical": True},
                {"code": "EXFILTRATION_CHANNEL_UNVERIFIED", "critical": True},
            ],
        },
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] != "ai_likely_false_positive"
    assert candidate["evidence_decision"] == "unresolved"
    # unresolved 进入人工复核队列（pending_manual），而非误报快速确认队列
    assert candidate["review_status"] == "pending_manual"


def test_likely_false_positive_kept_when_no_critical_gap() -> None:
    """v2026-08-14（矛盾①修复，保守侧）：证据充分（无 critical gap）时的
    flaw=False 仍作为否定信号采信——正常否定不受影响。"""

    candidate = l2_candidate(
        analysis_status="ai_completed",
        ai_analysis={
            "summary": "确定性否定",
            "verdict": "refutes_candidate",
            "confidence_tier": "high",
            "analysis_complete": True,
            "semantic_evidence_complete": True,
            "verified_evidence_refs": [VALID_REF],
            "evidence_refs": [VALID_REF],
            "flaw_holds": False,
            "exploitability": {
                "entry_reachable": False, "propagation_proven": False,
                "sink_effective": False, "guard_bypassed": False,
                "authorization_absent": True, "exfiltration_channel": "absent",
            },
            "blocking_gaps": [],
        },
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "ai_likely_false_positive"
    assert candidate["review_status"] == "pending_manual"


def test_ai_verdict_flaw_conflict_goes_unresolved() -> None:
    """v2026-08-14（矛盾②修复）：AI 输出自相矛盾——verdict=refutes（想否决）但
    flaw_holds=True（成立信号）且无确定性 basis——不采信任一方向，落 unresolved
    + AI_VERDICT_FLAW_CONFLICT gap（实测 3fe8a217：AI 的 flaw=True 是错的，
    removePref key 固定常量不可控）。"""

    candidate = l2_candidate(
        analysis_status="ai_completed",
        ai_analysis={
            "summary": "verdict 与 flaw 冲突",
            "verdict": "refutes_candidate",
            "confidence_tier": "medium",
            "analysis_complete": True,
            "semantic_evidence_complete": True,
            "verified_evidence_refs": [VALID_REF],
            "evidence_refs": [VALID_REF],
            "flaw_holds": True,
            "exploitability": {
                "entry_reachable": True, "propagation_proven": True,
                "sink_effective": True, "guard_bypassed": False,
                "authorization_absent": True, "exfiltration_channel": "absent",
            },
            "blocking_gaps": [],
        },
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "unresolved"
    # 矛盾输出不采信 → 人工复核队列
    assert candidate["review_status"] == "pending_manual"
    assert any(
        gap.get("code") == "AI_VERDICT_FLAW_CONFLICT"
        for gap in candidate.get("ai_blocking_gaps", [])
    )


def test_deterministic_supported_requires_entry_method_id() -> None:
    """v2026-08-14（矛盾③修复）：deterministic_chain_verified 是方法内传播证明，
    不验证调用点存在性——死代码方法（entry_method_id=None）不得 supported
    （实测 89da4b67：AccountChangedBroadcastHelper 全库无调用点却标 supported）。"""

    candidate = l2_candidate(
        entry_method_id=None,  # 死代码：无真实入口
        evidence_level="L2",
        analysis_status="rule_only",
        deterministic_chain_verified=True,
        dataflow_status="intraprocedural",
        review_status="pending_manual",
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] != "supported"


def test_deterministic_supported_kept_with_entry_method_id() -> None:
    """v2026-08-14（矛盾③修复，保守侧）：chain_verified=True 且 entry_method_id
    存在（真实入口）→ 仍 supported——正常链不受影响。"""

    candidate = l2_candidate(
        entry_method_id="Entry.java#com.example.Entry.onCreate:12",
        evidence_level="L2",
        analysis_status="rule_only",
        deterministic_chain_verified=True,
        dataflow_status="intraprocedural",
        review_status="pending_manual",
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "supported"


def test_refutation_basis_accepted_only_when_confirmed_by_rule_facts() -> None:
    """P1-5：AI 的 refutation_basis 每一项都必须被规则事实证实才采信。"""

    from app.findings.decision import decide_candidate

    confirmed = l2_candidate(
        verdict="refutes_candidate",
        deterministic_chain_verified=False,
        refutation_basis=["in_process_terminus"],
        deterministic_facts={"value_flow_reaches_sink_argument": False},
    )
    decision = decide_candidate(confirmed)
    assert decision["review_status"] == "ai_false_positive"
    assert "L2_REFUTED_WITH_CROSS_VALIDATED_BASIS" in decision["reason_codes"]


def test_refutation_basis_rejected_when_facts_contradict() -> None:
    """AI 声称"进程内终点"但规则事实显示值流已到达 Sink 实参 → 不采信。

    这是本机制最重要的安全边界：无条件采信 AI 自报 basis 会把"高误报"翻转成"漏报"。
    """

    from app.findings.decision import decide_candidate

    contradicted = l2_candidate(
        verdict="refutes_candidate",
        deterministic_chain_verified=False,
        refutation_basis=["in_process_terminus"],
        deterministic_facts={"value_flow_reaches_sink_argument": True},
    )
    decision = decide_candidate(contradicted)
    assert decision["review_status"] == "pending_manual"
    assert "L2_REFUTED_WITHOUT_DETERMINISTIC_NEGATIVE_PROOF" in decision["reason_codes"]


def test_refutation_basis_rejected_when_facts_missing() -> None:
    """没有 deterministic_facts 可交叉验证时 fail-closed，退回人工。"""

    from app.findings.decision import decide_candidate

    no_facts = l2_candidate(
        verdict="refutes_candidate",
        deterministic_chain_verified=False,
        refutation_basis=["in_process_terminus"],
    )
    assert decide_candidate(no_facts)["review_status"] == "pending_manual"


def test_refutation_basis_requires_every_item_confirmed() -> None:
    """多项 basis 只要有一项对不上，整体不予采信——不做"部分采信"。"""

    from app.findings.decision import decide_candidate

    partial = l2_candidate(
        verdict="refutes_candidate",
        deterministic_chain_verified=False,
        refutation_basis=["in_process_terminus", "guard_fail_closed"],
        deterministic_facts={
            "value_flow_reaches_sink_argument": False,
            "guard_status": "absent",  # 与 guard_fail_closed 矛盾
        },
    )
    assert decide_candidate(partial)["review_status"] == "pending_manual"


def test_unknown_refutation_basis_never_accepted() -> None:
    """未知取值一律不采信，防止 AI 编造 basis 绕过机制。"""

    from app.findings.decision import decide_candidate

    fabricated = l2_candidate(
        verdict="refutes_candidate",
        deterministic_chain_verified=False,
        refutation_basis=["totally_made_up_reason"],
        deterministic_facts={"value_flow_reaches_sink_argument": False},
    )
    assert decide_candidate(fabricated)["review_status"] == "pending_manual"
