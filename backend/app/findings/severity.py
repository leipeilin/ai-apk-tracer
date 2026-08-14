"""依据证据闭合程度、授权状态与实际影响确定发现项严重性。"""

from __future__ import annotations

SEVERITIES = {"critical", "high", "medium", "low", "informational", "pending"}
SEVERITY_LABELS = {
    "critical": "严重",
    "high": "高危",
    "medium": "中危",
    "low": "低危",
    "informational": "提示",
    "pending": "待定",
}


def determine_severity(candidate: dict) -> tuple[str, list[str]]:
    """根据证据、数据流、授权和影响状态返回最终严重性。

    规则 ``severity_hint`` 只是影响建议。没有确定性数据流、AI 失败、关键覆盖
    缺口或授权状态未知时，候选必须保持 ``pending``，不得直接成为 high。
    """

    evidence_decision = candidate.get("evidence_decision")
    if evidence_decision == "ai_false_positive":
        return "informational", ["AI 反驳与独立确定性反驳依据一致，标记为 AI 误报"]
    if evidence_decision == "deterministically_refuted":
        return "informational", ["确定性事实已反驳漏洞成立前提，仍保留人工可审计记录"]
    if evidence_decision == "blocked":
        # 方案 X'（v2026-08-09）：guard 阻断 = 当前构建不可利用（如 debuggable guard
        # 在 release 包拦死链路），不是误报——调试功能真实存在，若未来分发 debuggable
        # 构建则高危。severity 保持 pending（不可定级），reason 明确 guard 阻断。
        return "pending", ["确定性 guard 阻断链路（当前构建不可利用），定级待人工确认"]

    evidence_level = candidate.get("evidence_level", "L1")
    if evidence_level == "L1":
        return "informational", ["L1 仅确认攻击面或配置事实，不代表漏洞链成立"]

    blocking_gaps = [
        *candidate.get("blocking_gaps", []),
        *candidate.get("ai_blocking_gaps", []),
        *candidate.get("coverage_gaps", []),
    ]
    if _has_critical_gap(blocking_gaps):
        return "pending", ["关键证据或覆盖缺口阻止可靠定级"]

    dataflow_status = candidate.get("dataflow_status", "not_proven")
    if dataflow_status == "not_proven":
        return "pending", ["尚未证明不可信输入到敏感操作的数据传播"]

    analysis_status = candidate.get("analysis_status", "rule_only")
    deterministic = candidate.get("deterministic_chain_verified") is True
    if analysis_status in {"ai_failed", "ai_skipped"} and not deterministic:
        return "pending", ["AI 复核未完成，且确定性专项规则未闭合完整链路"]

    authorization_status = candidate.get("authorization_status", "unknown")
    guard_status = candidate.get("guard_status", "unknown")
    if guard_status == "present_effective":
        return "informational", ["实际入口到 Sink 前存在可证明 fail-closed 且局部支配的 Guard"]
    if authorization_status in {"protected", "strongly_protected"}:
        return "informational", ["当前 operation/path 存在可确认的有效调用者权限保护"]
    if authorization_status == "unknown":
        return "pending", ["当前 operation/path 的有效授权状态未知"]
    if guard_status in {"unknown", "present_partial"}:
        return "pending", ["实际入口到 Sink 的 GuardCoverage 尚未证明 fail-closed"]

    hint = candidate.get("severity_hint", "pending")
    if hint not in SEVERITIES or hint == "informational":
        hint = "pending"

    impact_status = candidate.get("impact_status", "potential")
    if impact_status == "potential" and hint in {"critical", "high"}:
        # 方法内候选尚未证明真实副作用时，最多保留中危建议。
        hint = "medium" if dataflow_status in {"intraprocedural", "interprocedural"} else "pending"

    reasons = candidate.get("severity_reason") or [
        "严重性由已证明的数据流、授权边界和静态副作用共同确定，仍需人工复核"
    ]
    return hint, reasons


def _has_critical_gap(gaps: list) -> bool:
    """判断任一结构化或非结构化缺口是否阻断可靠定级。"""

    for gap in gaps:
        if not isinstance(gap, dict):
            return True
        if gap.get("critical", True):
            return True
    return False
