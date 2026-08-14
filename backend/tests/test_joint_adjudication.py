"""联合裁决模型（v1）测试：机制否决 + AI 分档采信。

背景（run 194354Z）：139 个 ai_completed 全 unresolved（AI 判定 0% 采信）。
联合裁决 v1（doc/joint-adjudication-v1.md）：机制负责排除不可能（guard 阻断、
确定性反证、闭链冲突、AI 自标证据无效），AI 在机制未排除空间内按强度分档
采信——不冲突即信任 AI 的方向判定。
"""

from __future__ import annotations

from app.findings.decision import DecisionEngine
from app.findings.review_state import derive_review_state


def _candidate(**overrides: object) -> dict:
    """构造联合裁决测试候选（默认：flaw=True + entry=True 的中成立形态）。"""
    base = {
        "evidence_level": "L2",
        "analysis_status": "ai_completed",
        "dataflow_status": "not_proven",
        "authorization_status": "unprotected",
        "guard_status": "absent",
        "reachability_status": "reachable",
        "impact_status": "potential",
        "ai_analysis": {
            "summary": "测试候选",
            "verdict": "unresolved",
            "confidence_tier": "medium",
            "analysis_complete": True,
            "semantic_evidence_complete": True,
            "verified_evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "flaw_holds": True,
            "exploitability": {
                "entry_reachable": True,
                "propagation_proven": False,
                "sink_effective": True,
                "guard_bypassed": False,
                "authorization_absent": True,
                "exfiltration_channel": "unverified",
            },
            "blocking_gaps": [
                {"code": "EXFILTRATION_CHANNEL_UNVERIFIED", "critical": True},
                {"code": "DATAFLOW_NOT_PROVEN", "critical": True},
            ],
        },
        "coverage_gaps": [
            {"code": "SYMBOL_TARGET_AMBIGUOUS", "critical": True, "claim_impact": "both"},
        ],
        "positive_proof_coverage_complete": False,
        "negative_proof_coverage_complete": False,
    }
    base.update(overrides)
    return base


def test_ai_likely_supported_when_flaw_and_entry_true() -> None:
    """联合裁决：flaw=True + entry=True（传播未证）→ ai_likely_supported。"""

    candidate = _candidate()
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "ai_likely_supported"
    assert candidate["review_status"] == "pending_manual"
    assert "ai_likely_supported" in candidate["review_state"]["reason"]


def test_ai_strong_support_when_full_factors_and_proven() -> None:
    """联合裁决：四要素全真 + confidence=high + 数据流已证 → supported。"""

    candidate = _candidate(
        dataflow_status="intraprocedural",
        ai_analysis={
            "summary": "强成立",
            "verdict": "supports_candidate",
            "confidence_tier": "high",
            "analysis_complete": True,
            "semantic_evidence_complete": True,
            "verified_evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "flaw_holds": True,
            "exploitability": {
                "entry_reachable": True,
                "propagation_proven": True,
                "sink_effective": True,
                "guard_bypassed": False,
                "authorization_absent": True,
                "exfiltration_channel": "unverified",
            },
        },
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "supported"


def test_ai_likely_false_positive_when_flaw_false() -> None:
    """联合裁决：flaw=False（无确定性反证）→ ai_likely_false_positive。"""

    candidate = _candidate(
        ai_analysis={
            "summary": "否定",
            "verdict": "refutes_candidate",
            "confidence_tier": "medium",
            "analysis_complete": True,
            "semantic_evidence_complete": True,
            "verified_evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "flaw_holds": False,
            "exploitability": {
                "entry_reachable": False,
                "propagation_proven": False,
                "sink_effective": False,
                "guard_bypassed": False,
                "authorization_absent": True,
                "exfiltration_channel": "absent",
            },
        },
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "ai_likely_false_positive"
    assert candidate["review_status"] == "pending_manual"
    assert "ai_likely_false_positive" in candidate["review_state"]["reason"]


def test_guard_blocked_still_mechanism_veto() -> None:
    """机制否决：guard_blocked 时 AI 判定不得采信 → blocked。"""

    candidate = _candidate(
        guard_blocked=True,
        guard_blocks=[{"type": "debuggable", "path": "p", "line": 1}],
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "blocked"


def test_ai_self_marked_invalid_refs_not_trusted() -> None:
    """机制否决：AI 自标证据无效（AI_EVIDENCE_REF_INVALID）→ 不采信，保持 unresolved。"""

    candidate = _candidate(
        ai_analysis={
            "summary": "有判定但证据无效",
            "verdict": "unresolved",
            "confidence_tier": "low",
            "analysis_complete": True,
            "semantic_evidence_complete": False,
            "verified_evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "evidence_refs": [],
            "flaw_holds": True,
            "exploitability": {
                "entry_reachable": True,
                "propagation_proven": False,
                "sink_effective": True,
                "guard_bypassed": False,
                "authorization_absent": True,
                "exfiltration_channel": "unverified",
            },
            "blocking_gaps": [
                {"code": "AI_EVIDENCE_REF_INVALID", "critical": True},
            ],
        },
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "unresolved"


def test_no_ai_direction_stays_unresolved() -> None:
    """无方向判定（flaw=None 且 verdict=unresolved）→ 保持 unresolved。"""

    candidate = _candidate(
        ai_analysis={
            "summary": "无判定",
            "verdict": "unresolved",
            "confidence_tier": "low",
            "analysis_complete": True,
            "semantic_evidence_complete": True,
            "verified_evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "flaw_holds": None,
            "exploitability": {},
        },
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "unresolved"


def test_review_state_likely_reasons_distinct() -> None:
    """review_state 对两种倾向结论的 reason 必须区分（人工快速确认分流）。"""

    s1 = derive_review_state(
        _candidate(), evidence_decision="ai_likely_supported", false_positive_basis=[]
    )
    s2 = derive_review_state(
        _candidate(), evidence_decision="ai_likely_false_positive", false_positive_basis=[]
    )
    assert s1["status"] == "pending_manual"
    assert s2["status"] == "pending_manual"
    assert s1["reason"] != s2["reason"]
    assert "ai_likely_supported" in s1["reason"]
    assert "ai_likely_false_positive" in s2["reason"]


# ===== 辅助函数直接单测（端到端测试的补充，覆盖决策矩阵的每个分支） =====

def test_evidence_insufficiency_gap_whitelist() -> None:
    """白名单：证据不足类 code 命中，确定性冲突类未命中。"""

    from app.findings.decision import _evidence_insufficiency_gap

    assert _evidence_insufficiency_gap({"code": "EXFILTRATION_CHANNEL_UNVERIFIED"}) is True
    assert _evidence_insufficiency_gap({"code": "DATAFLOW_NOT_PROVEN"}) is True
    assert _evidence_insufficiency_gap({"code": "SYMBOL_TARGET_AMBIGUOUS"}) is True
    assert _evidence_insufficiency_gap({"code": "LEGACY_FLOW_FALLBACK"}) is True
    # v3.0.5：AI_EVIDENCE_SEMANTIC_INCOMPLETE 与 SEMANTIC_EVIDENCE_INCOMPLETE 同权豁免
    # （引用可回查但缺 role/domain，属证据不足类；否则 _applicable_critical_gap 双路径误拦）。
    assert _evidence_insufficiency_gap({"code": "AI_EVIDENCE_SEMANTIC_INCOMPLETE"}) is True
    # v3.0.5（run 20260809T104055Z）：3.0.5 提示词输出的更细分"证据不足类"gap 也应豁免
    # （静态不可证：authority/权限/敏感度/广播保护/危害未验证，非确定性冲突）。
    for code in (
        "AUTHORITY_RESOLUTION_UNKNOWN",
        "AUTHORITY_RESOLUTION_UNVERIFIED",
        "SINK_EFFECT_UNVERIFIED",
        "ACTION_AUTHORIZATION_UNKNOWN",
        "PROVIDER_PERMISSION_UNKNOWN",
        "PROVIDER_DATA_SENSITIVITY_UNVERIFIED",
        "HARM_NOT_PROVEN",
        "PROTECTED_BROADCAST_UNRESOLVED",
    ):
        assert _evidence_insufficiency_gap({"code": code}) is True, code
    # 确定性冲突类：不在白名单 → False（会被拦截）
    assert _evidence_insufficiency_gap({"code": "GUARD_BLOCKED"}) is False
    assert _evidence_insufficiency_gap({"code": "AI_EVIDENCE_REF_INVALID"}) is False
    assert _evidence_insufficiency_gap({}) is False


def test_coverage_allows_joint_explicit_flag_all_insuff() -> None:
    """explicit flag=False + 全部 gap 为证据不足类 → 放行（联合裁决核心）。"""

    from app.findings.decision import _coverage_allows_joint

    candidate = {
        "positive_proof_coverage_complete": False,
        "coverage_gaps": [
            {"code": "SYMBOL_TARGET_AMBIGUOUS", "critical": True},
            {"code": "DATAFLOW_NOT_PROVEN", "critical": True},
        ],
    }
    assert _coverage_allows_joint(candidate, "positive_proof") is True


def test_coverage_allows_joint_explicit_flag_with_conflict_gap() -> None:
    """explicit flag=False + 存在确定性冲突 gap → 拦截（fail-closed 保留）。"""

    from app.findings.decision import _coverage_allows_joint

    candidate = {
        "positive_proof_coverage_complete": False,
        "coverage_gaps": [
            {"code": "SYMBOL_TARGET_AMBIGUOUS", "critical": True},
            {"code": "GUARD_BLOCKED", "critical": True},  # 确定性冲突，非白名单
        ],
    }
    assert _coverage_allows_joint(candidate, "positive_proof") is False


def test_coverage_allows_joint_affects_field_insuff_passthrough() -> None:
    """affects_positive_proof=True 但 gap 为证据不足类 → 放行。"""

    from app.findings.decision import _coverage_allows_joint

    candidate = {
        "coverage_gaps": [
            {"code": "SYMBOL_TARGET_AMBIGUOUS", "critical": True,
             "affects_positive_proof": True},
        ],
    }
    assert _coverage_allows_joint(candidate, "positive_proof") is True


def test_coverage_allows_joint_affects_field_conflict_blocked() -> None:
    """affects_positive_proof=True + 确定性冲突 gap → 拦截。"""

    from app.findings.decision import _coverage_allows_joint

    candidate = {
        "coverage_gaps": [
            {"code": "GUARD_BLOCKED", "critical": True, "affects_positive_proof": True},
        ],
    }
    assert _coverage_allows_joint(candidate, "positive_proof") is False


def test_coverage_allows_joint_claim_impact_conflict_blocked() -> None:
    """claim_impact=both + 确定性冲突 gap → 拦截（非白名单且无证据不足命名）。"""

    from app.findings.decision import _coverage_allows_joint

    candidate = {
        "coverage_gaps": [
            {"code": "GUARD_BLOCKED", "critical": True, "claim_impact": "both"},
        ],
    }
    assert _coverage_allows_joint(candidate, "positive_proof") is False


def test_ai_strong_support_requires_high_confidence() -> None:
    """边界：四要素全真但 confidence=medium → 不采信 supported（降级 likely）。"""

    from app.findings.decision import _ai_strong_support

    analysis = {
        "verdict": "supports_candidate",
        "confidence_tier": "medium",  # 非 high → 不满足 strong
        "flaw_holds": True,
        "exploitability": {
            "entry_reachable": True,
            "propagation_proven": True,
            "sink_effective": True,
        },
    }
    candidate = {"dataflow_status": "intraprocedural"}
    assert _ai_strong_support(candidate, analysis) is False
    # 但 likely_supported 仍命中（flaw+entry）→ 降级采信而非丢弃
    from app.findings.decision import _ai_likely_supported
    assert _ai_likely_supported(candidate, analysis) is True


def test_ai_strong_support_requires_proven_dataflow() -> None:
    """边界：四要素全真 + high 但数据流未证 → 不采信 supported。"""

    from app.findings.decision import _ai_strong_support

    analysis = {
        "verdict": "supports_candidate",
        "confidence_tier": "high",
        "flaw_holds": True,
        "exploitability": {
            "entry_reachable": True,
            "propagation_proven": True,
            "sink_effective": True,
        },
    }
    candidate = {"dataflow_status": "not_proven"}
    assert _ai_strong_support(candidate, analysis) is False


def test_ai_likely_false_positive_requires_flaw_false() -> None:
    """边界：verdict=refutes 但 flaw 非 False（如 None）→ 不采信否定。"""

    from app.findings.decision import _ai_likely_false_positive

    # flaw=None（无方向判定）→ 不采信
    assert _ai_likely_false_positive({}, {"verdict": "refutes_candidate", "flaw_holds": None}) is False
    # flaw=True 与 refutes 矛盾 → 不采信
    assert _ai_likely_false_positive({}, {"verdict": "refutes_candidate", "flaw_holds": True}) is False
    # 正常否定 → 采信
    assert _ai_likely_false_positive({}, {"verdict": "refutes_candidate", "flaw_holds": False}) is True


def test_deterministic_refutation_takes_priority_over_likely_false_positive() -> None:
    """分支优先级：确定性反证（deterministic_basis）优先于 ai_likely_false_positive。"""

    candidate = _candidate(
        ai_analysis={
            "summary": "否定+确定性反证",
            "verdict": "refutes_candidate",
            "confidence_tier": "medium",
            "analysis_complete": True,
            "semantic_evidence_complete": True,
            "verified_evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "flaw_holds": False,
            "exploitability": {"exfiltration_channel": "absent"},
        },
        sinks=[{"kind": "implicit_broadcast",
                "receiver_text": "LocalBroadcastManager.getInstance(getAppContext())"}],
    )
    DecisionEngine().apply([candidate])
    # LocalBroadcast SDK 语义反证 → 优先 ai_false_positive / deterministically_refuted
    assert candidate["evidence_decision"] in {"ai_false_positive", "deterministically_refuted"}


def test_likely_false_positive_preserves_candidate() -> None:
    """防假阴性铁律：ai_likely_false_positive 不删除候选、severity 保持 pending。"""

    from app.findings.severity import determine_severity

    candidate = _candidate(
        ai_analysis={
            "summary": "否定",
            "verdict": "refutes_candidate",
            "confidence_tier": "medium",
            "analysis_complete": True,
            "semantic_evidence_complete": True,
            "verified_evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "flaw_holds": False,
            "exploitability": {"exfiltration_channel": "absent"},
        },
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "ai_likely_false_positive"
    # 候选仍在（未删除）+ review 状态是待人工确认（非终局误报）
    assert candidate["review_status"] == "pending_manual"
    severity, _ = determine_severity(candidate)
    assert severity == "pending", "倾向误报不得自动定级（防假阴性）"


def test_partial_evidence_invalid_refs_still_likely_supported() -> None:
    """宽松模式：AI 有方向判定 + verified refs 非空，即使 invalid refs 存在 → 采信。

    联合裁决 v1：部分证据无效（INVALID_EVIDENCE_REFS）不一票否决 AI 判定，
    有 verified refs 支撑时降级采信为 ai_likely_supported（人工确认）。
    """

    candidate = _candidate(
        ai_analysis={
            "summary": "有判定但部分 refs 无效",
            "verdict": "unresolved",
            "confidence_tier": "medium",
            "analysis_complete": True,
            "semantic_evidence_complete": False,  # AI 未声明语义完整
            "verified_evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "invalid_evidence_refs": [{"context_id": "com/x/F.java#X.other:999", "reason": "LINE_OUT_OF_RANGE"}],
            "flaw_holds": True,
            "exploitability": {
                "entry_reachable": True,
                "propagation_proven": False,
                "sink_effective": True,
                "guard_bypassed": False,
                "authorization_absent": True,
                "exfiltration_channel": "unverified",
            },
        },
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "ai_likely_supported"


def test_analysis_coverage_gaps_insuff_passthrough() -> None:
    """analysis 的 coverage_gaps 证据不足类 → 放行（_applicable_critical_gap 后半段）。"""

    candidate = _candidate(
        ai_analysis={
            **_candidate()["ai_analysis"],
            "coverage_gaps": [
                {"code": "EXFILTRATION_CHANNEL_UNVERIFIED", "critical": True,
                 "claim_impact": "both"},
            ],
        },
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "ai_likely_supported"


def test_analysis_coverage_gaps_conflict_blocked() -> None:
    """analysis 的 coverage_gaps 确定性冲突类 → 拦截（不采信）。"""

    candidate = _candidate(
        ai_analysis={
            **_candidate()["ai_analysis"],
            "coverage_gaps": [
                {"code": "GUARD_PATH_CONFIRMED_BLOCK", "critical": True,
                 "claim_impact": "both"},
            ],
        },
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "unresolved"


def test_joint_adjudication_end_to_end_prompt_304() -> None:
    """集成：l2-review 3.0.4 提示词声明的机制内裁决与 decision 分档一致。"""

    from app.analysis.prompt_registry import PromptRegistry

    system = PromptRegistry().load("l2-review", "3.0.4").system_template
    assert "机制内裁决" in system
    assert "不禁止 high" in system
    # decision 层的采信语义（ai_likely_supported = flaw+entry）在提示词有对应约束
    assert "缺陷成立 + 入口可达" in system or "缺陷成立、可利用" in system


def test_likely_false_positive_signal_is_flaw_false_not_verdict() -> None:
    """优化：flaw=False 即否定信号（verdict 可 unresolved）。

    实测（run 200257Z）：29/30 个 flaw=False 候选 verdict=unresolved（3.0.4 前 AI
    判了缺陷不成立但不敢写 refutes），原逻辑要求 verdict in refutes 导致漏采信
    25 个。改为以 flaw_holds=False 为否定信号（与 likely_supported 对称）。
    """

    candidate = _candidate(
        ai_analysis={
            "summary": "否定但 verdict 保守",
            "verdict": "unresolved",  # 关键：不是 refutes
            "confidence_tier": "low",
            "analysis_complete": True,
            "semantic_evidence_complete": True,
            "verified_evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "flaw_holds": False,  # AI 已判缺陷不成立
            "exploitability": {
                "entry_reachable": True,
                "propagation_proven": False,
                "sink_effective": False,
                "guard_bypassed": False,
                "authorization_absent": True,
                "exfiltration_channel": "absent",
            },
        },
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "ai_likely_false_positive"


def test_ai_evidence_semantic_incomplete_no_longer_blocks_joint() -> None:
    """v3.0.5：AI_EVIDENCE_SEMANTIC_INCOMPLETE（引用可回查但缺 role/domain）属证据
    不足类，不再被 _applicable_critical_gap 从 ai_blocking_gaps 路径误拦——与宽松
    模式放行 SEMANTIC_EVIDENCE_INCOMPLETE 同权（修复联合裁决 v1 双路径不一致）。"""

    candidate = _candidate(
        ai_analysis={
            "summary": "引用可回查但语义覆盖不完整",
            "verdict": "unresolved",
            "confidence_tier": "medium",
            "analysis_complete": True,
            "semantic_evidence_complete": False,
            "verified_evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "evidence_refs": [{"context_id": "com/x/F.java#X.m:1", "claim": "c"}],
            "flaw_holds": True,
            "exploitability": {
                "entry_reachable": True,
                "propagation_proven": False,
                "sink_effective": True,
                "guard_bypassed": False,
                "authorization_absent": True,
                "exfiltration_channel": "unverified",
            },
            "blocking_gaps": [
                {"code": "EXFILTRATION_CHANNEL_UNVERIFIED", "critical": True},
            ],
        },
        ai_blocking_gaps=[
            {"code": "AI_EVIDENCE_SEMANTIC_INCOMPLETE", "critical": True,
             "missing_roles": ["sink"], "missing_domains": ["impact"]},
        ],
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "ai_likely_supported"
    assert candidate["review_status"] == "pending_manual"


def test_ai_evidence_ref_invalid_still_blocks_joint() -> None:
    """v3.0.5：AI_EVIDENCE_REF_INVALID（引用本身无效 = AI 自标证据无效）保持白名单
    外，防幻觉铁律不变——仍然否决，不采信。"""

    candidate = _candidate(
        ai_blocking_gaps=[
            {"code": "AI_EVIDENCE_REF_INVALID", "critical": True, "count": 2},
        ],
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] == "unresolved"
    assert candidate["review_status"] == "pending_ai"


def test_evidence_insufficiency_gap_naming_fallback() -> None:
    """v3.0.5 命名兜底：白名单外但命名含"证据不足"语义模式的 code 自动豁免，
    确定性冲突/分析失败/AI 自标无效类保持拦截（解决"白名单未更新"反复复发）。"""

    from app.findings.decision import _evidence_insufficiency_gap

    # 正向：证据不足命名模式 → 豁免（静态不可证）
    for code in (
        "EFFECT_CALL_TARGET_AMBIGUOUS",      # AMBIGUOUS
        "DYNAMIC_RECEIVER_PERMISSION_UNRESOLVED",  # UNRESOLVED
        "GUARD_COVERAGE_UNPROVEN",           # UNPROVEN
        "SINK_SEMANTICS_UNRESOLVED",         # UNRESOLVED
        "AUTHORIZATION_STATUS_UNKNOWN",      # UNKNOWN
        "PROPAGATION_PATH_UNVERIFIED",       # UNVERIFIED
        "EFFECT_PATH_RECURSIVE_OR_TOO_DEEP", # RECURSIVE/TOO_DEEP
    ):
        assert _evidence_insufficiency_gap({"code": code}) is True, code

    # 反向：确定性冲突/分析失败/AI 自标无效 → 保持拦截
    for code in (
        "GUARD_BLOCKED",
        "AI_EVIDENCE_REF_INVALID",
        "AI_ANALYSIS_FAILED",
        "AI_ANALYSIS_SKIPPED",
        "AI_ANALYSIS_INCOMPLETE",
        "ANALYSIS_NOT_COMPLETE",
        "CONTEXT_BUDGET_EXHAUSTED",
        "JADX_PARTIAL_DECOMPILATION",
        "RULE_PRESCAN_PARTIAL",
    ):
        assert _evidence_insufficiency_gap({"code": code}) is False, code
