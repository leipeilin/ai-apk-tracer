"""集中派生自动分析后的复核状态，避免各阶段自行猜测误报。"""

from __future__ import annotations

from typing import Any


FINAL_MANUAL_STATUSES = {"confirmed", "manual_false_positive"}
AI_INCOMPLETE_STATUSES = {
    "rule_only",
    "ai_failed",
    "ai_skipped",
    "ai_incomplete",
    "ai_budget_deferred",
}


def derive_review_state(
    candidate: dict[str, Any],
    *,
    evidence_decision: str,
    false_positive_basis: list[str],
) -> dict[str, Any]:
    """依据 AI 必要性与双重反驳条件返回唯一 review 状态。"""

    existing = candidate.get("review_status")
    if existing in FINAL_MANUAL_STATUSES:
        status = str(existing)
        reason = "preserved_manual_decision"
    elif evidence_decision == "ai_false_positive" and false_positive_basis:
        status = "ai_false_positive"
        reason = "ai_refutation_with_deterministic_basis"
    elif _ai_still_required(candidate):
        status = "pending_ai"
        reason = "required_ai_analysis_not_completed"
    elif evidence_decision == "ai_likely_supported":
        # 联合裁决 v1：AI 倾向成立（缺陷成立+入口可达）→ 人工快速确认传播/外溢。
        status = "pending_manual"
        reason = "ai_likely_supported_needs_confirmation"
    elif evidence_decision == "ai_likely_false_positive":
        # 联合裁决 v1：AI 倾向误报（flaw=False 无确定性反证）→ 人工快速确认否定。
        status = "pending_manual"
        reason = "ai_likely_false_positive_needs_confirmation"
    else:
        status = "pending_manual"
        reason = "automatic_analysis_requires_manual_review"
    return {
        "status": status,
        "reason": reason,
        "evidence_decision": evidence_decision,
        "false_positive_basis": list(false_positive_basis),
    }


def aggregate_review_states(members: list[dict[str, Any]]) -> dict[str, Any]:
    """保守合并成员状态；聚合层只消费决策结果，不读取 promotion 建议。"""

    states = [_state_from_candidate(member) for member in members]
    statuses = {state["status"] for state in states}
    if "confirmed" in statuses:
        status = "confirmed"
    elif "pending_ai" in statuses:
        status = "pending_ai"
    elif "pending_manual" in statuses:
        status = "pending_manual"
    elif statuses == {"ai_false_positive"}:
        status = "ai_false_positive"
    elif statuses == {"manual_false_positive"}:
        status = "manual_false_positive"
    else:
        status = "pending_manual"
    evidence_decisions = sorted({state["evidence_decision"] for state in states})
    false_positive_basis = sorted({
        basis
        for state in states
        for basis in state.get("false_positive_basis", [])
    })
    return {
        "status": status,
        "reason": "aggregated_member_review_states",
        "evidence_decision": evidence_decisions[0] if len(evidence_decisions) == 1 else "mixed",
        "false_positive_basis": false_positive_basis,
    }


def _state_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    state = candidate.get("review_state")
    if isinstance(state, dict) and state.get("status"):
        return state
    return {
        "status": candidate.get("review_status") or (
            "pending_ai" if _ai_still_required(candidate) else "pending_manual"
        ),
        "reason": "legacy_candidate_state",
        "evidence_decision": candidate.get("evidence_decision", "unresolved"),
        "false_positive_basis": list(candidate.get("false_positive_basis") or []),
    }


def _ai_still_required(candidate: dict[str, Any]) -> bool:
    analysis_status = str(candidate.get("analysis_status") or "rule_only")
    if "ai_required" in candidate and candidate.get("ai_required") is not True:
        return False
    if "ai_required" not in candidate and analysis_status not in {
        "ai_failed",
        "ai_skipped",
        "ai_incomplete",
        "ai_budget_deferred",
    }:
        return False
    return analysis_status in AI_INCOMPLETE_STATUSES or analysis_status not in {
        "ai_completed",
        "completed",
        "human_confirmed",
    }
