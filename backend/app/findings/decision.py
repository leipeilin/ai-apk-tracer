"""Pure policy boundary for deriving automatic finding decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypedDict, cast

from app.analysis.candidate_funnel import (
    LOCAL_BROADCAST_RECEIVER_RE,
    deterministic_refutation_basis,
)
from app.analysis.coverage import coverage_allows
from app.findings.evidence import validate_ai_evidence_references
from app.findings.review_state import derive_review_state
from app.findings.severity import determine_severity

DECISION_VERSION = "1.0"

EvidenceLevel = Literal["L1", "L2", "L3"]
ReviewStatus = Literal[
    "pending_ai",
    "pending_manual",
    "ai_false_positive",
    "manual_false_positive",
    "confirmed",
]


class Decision(TypedDict):
    evidence_level: EvidenceLevel
    severity: str
    review_status: ReviewStatus
    reason_codes: list[str]
    decision_version: str


_MANUAL_TERMINAL_STATUSES = {"confirmed", "manual_false_positive"}
_INCOMPLETE_STATUSES = {
    "failed",
    "skipped",
    "incomplete",
    "ai_failed",
    "ai_skipped",
    "ai_incomplete",
}
_POSITIVE_OUTCOMES = {"support", "supported", "positive", "supports_candidate"}
_REFUTATION_OUTCOMES = {
    "negative",
    "refutation",
    "refuted",
    "refutes_candidate",
}
# SDK 语义反证：基于官方 API 的确定性语义（静态可证），不依赖 AI 语义判断，
# 可绕过 coverage 保守检查独立生效（v2026-08-09，docs/updates/2026-08-09-*-local-broadcast*.md）。
# receiver 匹配正则单一来源：app.analysis.candidate_funnel.LOCAL_BROADCAST_RECEIVER_RE
# （2026-08-09 复审：此前本地重复定义且无单词边界，EventBusUtils 类名会误匹配）。
_SDK_SEMANTIC_REFUTATIONS = frozenset({"local_broadcast_intra_process"})
_PROVEN_DATAFLOW = {"intraprocedural", "interprocedural", "verified"}
_POSITIVE_GUARDS = {"absent", "present_bypassable"}
_PROTECTED_AUTHORIZATION = {"protected", "strongly_protected"}
# 证据不足类 gap：属于"可利用/传播/符号解析"要素的静态限制，不构成对 AI 判定的
# 确定性冲突——存在这些 gap 时 AI 的四要素判定仍可被采信（降级 confidence 分档），
# 只有确定性冲突类（guard 阻断/闭链反判/红线否定）才触发 validation_failure
# （联合裁决 v1，2026-08-09，doc/joint-adjudication-v1.md）。
_EVIDENCE_INSUFFICIENCY_GAPS = frozenset({
    "EXFILTRATION_CHANNEL_UNVERIFIED",
    "DATAFLOW_NOT_PROVEN",
    "DATAFLOW_IR_STEP_BUDGET_EXCEEDED",
    "LINEAR_IR_PATH_SENSITIVITY_LIMITATION",
    "PATH_SENSITIVITY_LIMITATION",
    "GUARD_PATH_UNRESOLVED",
    "SYMBOL_TARGET_AMBIGUOUS",
    "RECURSIVE_FLOW_APPROXIMATION",
    "RECEIVER_ACTION_UNRESOLVED",
    "RECEIVER_TARGET_UNRESOLVED",
    "RECEIVER_TARGET_AMBIGUOUS",
    "RECEIVER_FLAG_UNKNOWN",
    "RECEIVER_CLASS_QUALIFICATION_UNRESOLVED",
    "RECEIVER_REGISTRATION_OVERLOAD_UNKNOWN",
    "RECEIVER_PERMISSION_PROTECTION_UNKNOWN",
    "INPUT_PROTOCOL_UNCONTROLLED",
    "COMPONENT_PARENT_AMBIGUOUS",
    "ENTRY_REACHABILITY_UNPROVEN",
    "CONTEXT_EXPANSION_STALLED",
    "LEGACY_FLOW_FALLBACK",
    "FLOW_IR_UNAVAILABLE",
    "METHOD_CONTENT_UNAVAILABLE",
    "INDEX_QUERY_FAILED",
    # 规则层/索引/授权/AI 生成的"证据不足"类（静态限制，非确定性冲突）——
    # v2026-08-09 核验 run 200257Z 时补齐（27 个有方向候选因此被拦）。
    "HARM_NOT_ESTABLISHED",
    "CALLER_NOT_PROVEN",
    "ATTACKER_INPUT_NOT_PROVEN",
    "PROTECTED_ACTION_UNVERIFIED",
    "BINDER_RETURN_TYPE_AMBIGUOUS",
    "BINDER_CALLER_CHECK_UNRESOLVED",
    "RULE_COMPONENT_PARTIAL",
    "AUTHORIZATION_PERMISSION_UNKNOWN",
    "COMPONENT_PARENT_UNRESOLVED",
    "ACTION_FILTER_AMBIGUOUS",
    "INTENT_COMPONENT_UNRESOLVED",
    "RECEIVER_REGISTRATION_UNVERIFIED",
    "DATA_SOURCE_UNVERIFIED",
    "SOURCE_SINK_LINK_UNPROVEN",
    "IMPACT_CHAIN_UNPROVEN",
    # v3.0.5（run 20260809T104055Z 核验）：3.0.5 提示词让 AI 输出更细分的
    # "证据不足类" gap（静态限制），白名单未同步导致 10 候选误拦、7 个有方向
    # 判定候选（flaw=True + entry=True）被 joint_failure 拦截。逐一核验语义均为
    # 静态不可证（authority/权限/敏感度/广播保护/危害未验证），非确定性冲突。
    "AUTHORITY_RESOLUTION_UNKNOWN",
    "AUTHORITY_RESOLUTION_UNVERIFIED",
    "SINK_EFFECT_UNVERIFIED",
    "ACTION_AUTHORIZATION_UNKNOWN",
    "PROVIDER_PERMISSION_UNKNOWN",
    "PROVIDER_DATA_SENSITIVITY_UNVERIFIED",
    "HARM_NOT_PROVEN",
    "PROTECTED_BROADCAST_UNRESOLVED",
    # 校验器生成的"AI 证据引用有效但语义覆盖不完整"类（v3.0.5 核验 run 200257Z
    # 时发现）：AI_EVIDENCE_SEMANTIC_INCOMPLETE 与宽松模式放行的
    # SEMANTIC_EVIDENCE_INCOMPLETE 是同一语义（引用可回查但缺 role/domain），
    # 必须同权放行，否则 _applicable_critical_gap 从 ai_blocking_gaps 路径把它
    # 拦回，联合裁决 v1 的双路径不一致导致 15 个本应采信候选被误拦。
    # 注意：AI_EVIDENCE_REF_INVALID（引用本身无效 = AI 自标证据无效）仍保持
    # 白名单外——防幻觉铁律不变，只有"引用有效但语义覆盖不全"才豁免。
    "AI_EVIDENCE_SEMANTIC_INCOMPLETE",
    # P2-6（2026-08-15）：路由注入规则的目标解析 gap——"路由目标由运行期值决定，
    # 静态无法枚举全部可达目标"是纯静态限制（证据不足），与 SYMBOL_TARGET_AMBIGUOUS
    # 同类，非确定性冲突。若不入白名单，AI 的 refutes（fixed_local_target 反证）会被
    # _applicable_critical_gap 拦回，交叉验证采信在生产上永不生效（safe 但无效）。
    "ROUTE_TARGET_RESOLUTION_UNVERIFIED",
})


def decide_candidate(candidate: Mapping[str, Any]) -> Decision:
    """Return a deterministic decision without mutating ``candidate``.

    Compatibility values emitted by the AI facade may be top-level or nested
    under ``ai_analysis``. The input is expected to have passed evidence
    verification; this boundary still fails closed when verified refs are
    absent or invalid refs remain.
    """

    snapshot = dict(candidate)
    analysis = _analysis(snapshot)
    evidence_level = _evidence_level(snapshot.get("evidence_level"))
    severity, _ = determine_severity(snapshot)

    existing_status = snapshot.get("review_status")
    if existing_status in _MANUAL_TERMINAL_STATUSES:
        return _decision(
            evidence_level,
            severity,
            cast(ReviewStatus, existing_status),
            "MANUAL_TERMINAL_STATUS_PRESERVED",
        )

    analysis_status = _first_value(
        snapshot, analysis, "ai_analysis_status", "analysis_status", "status"
    )
    if isinstance(analysis_status, str) and analysis_status.lower() in _INCOMPLETE_STATUSES:
        normalized = analysis_status.upper().removeprefix("AI_")
        return _decision(
            evidence_level, severity, "pending_ai", f"ANALYSIS_{normalized}"
        )

    if not _all_true_field(snapshot, analysis, "analysis_complete"):
        return _decision(
            evidence_level, severity, "pending_ai", "ANALYSIS_NOT_COMPLETE"
        )

    if _invalid_evidence_refs(snapshot, analysis):
        return _decision(
            evidence_level, severity, "pending_ai", "INVALID_EVIDENCE_REFS"
        )
    if not _valid_evidence_refs(snapshot, analysis):
        return _decision(
            evidence_level,
            severity,
            "pending_ai",
            "VALID_EVIDENCE_REFS_REQUIRED",
        )
    semantic_values = _field_values(snapshot, analysis, "semantic_evidence_complete")
    if semantic_values and not all(value is True for value in semantic_values):
        return _decision(
            evidence_level,
            severity,
            "pending_ai",
            "SEMANTIC_EVIDENCE_INCOMPLETE",
        )
    if _critical_gap(snapshot, analysis):
        return _decision(
            evidence_level,
            severity,
            "pending_ai",
            "CRITICAL_BLOCKING_OR_COVERAGE_GAP",
        )

    outcome = _outcome(snapshot, analysis)
    if evidence_level == "L1":
        if outcome == "exposure_only":
            return _decision(
                evidence_level,
                severity,
                "pending_manual",
                "L1_EXPOSURE_ONLY_COMPLETE",
            )
        if outcome == "potential_chain":
            return _decision(
                evidence_level,
                severity,
                "pending_ai",
                "L1_POTENTIAL_CHAIN_REQUIRES_VALIDATED_PROMOTION",
            )
        return _decision(
            evidence_level, severity, "pending_ai", "L1_ANALYSIS_UNRESOLVED"
        )

    if outcome in _POSITIVE_OUTCOMES:
        if _positive_gates_pass(snapshot):
            return _decision(
                evidence_level,
                severity,
                "pending_manual",
                "L2_POSITIVE_GATES_PASSED",
            )
        return _decision(
            evidence_level,
            severity,
            "pending_ai",
            "L2_POSITIVE_GATES_NOT_PROVEN",
        )

    if outcome in _REFUTATION_OUTCOMES:
        if _deterministic_negative_proof(snapshot):
            return _decision(
                evidence_level,
                severity,
                "ai_false_positive",
                "L2_REFUTED_WITH_DETERMINISTIC_NEGATIVE_PROOF",
            )
        if _cross_validated_refutation_basis(snapshot, analysis):
            return _decision(
                evidence_level,
                severity,
                "ai_false_positive",
                "L2_REFUTED_WITH_CROSS_VALIDATED_BASIS",
            )
        return _decision(
            evidence_level,
            severity,
            "pending_manual",
            "L2_REFUTED_WITHOUT_DETERMINISTIC_NEGATIVE_PROOF",
        )

    return _decision(
        evidence_level,
        severity,
        "pending_manual",
        "L2_COMPLETE_RESULT_UNRESOLVED",
    )


def _decision(
    evidence_level: EvidenceLevel,
    severity: str,
    review_status: ReviewStatus,
    reason_code: str,
) -> Decision:
    return {
        "evidence_level": evidence_level,
        "severity": severity,
        "review_status": review_status,
        "reason_codes": [reason_code],
        "decision_version": DECISION_VERSION,
    }


def _analysis(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    value = candidate.get("ai_analysis")
    return value if isinstance(value, Mapping) else {}


def _first_value(
    candidate: Mapping[str, Any], analysis: Mapping[str, Any], *fields: str
) -> Any:
    for source in (candidate, analysis):
        for field in fields:
            if field in source and source[field] is not None:
                return source[field]
    return None


def _field_values(
    candidate: Mapping[str, Any], analysis: Mapping[str, Any], field: str
) -> list[Any]:
    return [
        source[field] for source in (candidate, analysis)
        if field in source and source[field] is not None
    ]


def _all_true_field(
    candidate: Mapping[str, Any], analysis: Mapping[str, Any], field: str
) -> bool:
    values = _field_values(candidate, analysis, field)
    return bool(values) and all(value is True for value in values)


def _evidence_level(value: Any) -> EvidenceLevel:
    return cast(EvidenceLevel, value if value in {"L1", "L2", "L3"} else "L1")


def _outcome(candidate: Mapping[str, Any], analysis: Mapping[str, Any]) -> str:
    value = _first_value(
        candidate,
        analysis,
        "outcome",
        "verdict",
        "candidate_verdict",
        "triage_disposition",
    )
    if isinstance(value, str):
        return value.lower()
    promotion = _first_value(candidate, analysis, "promotion_recommended")
    if promotion is True:
        return "supports_candidate"
    if promotion is False:
        return "refutes_candidate"
    return "unresolved"


def _trusted_ai_outcome(analysis: Mapping[str, Any]) -> str:
    for field in ("outcome", "verdict", "candidate_verdict", "triage_disposition"):
        value = analysis.get(field)
        if isinstance(value, str):
            return value.lower()
    promotion = analysis.get("promotion_recommended")
    if promotion is True:
        return "supports_candidate"
    if promotion is False:
        return "refutes_candidate"
    return "unresolved"


def _invalid_evidence_refs(
    candidate: Mapping[str, Any], analysis: Mapping[str, Any]
) -> bool:
    return any(
        source.get("evidence_refs_valid") is False
        or _nonempty_sequence(source.get("invalid_evidence_refs"))
        for source in (candidate, analysis)
    )


def _valid_evidence_refs(
    candidate: Mapping[str, Any], analysis: Mapping[str, Any]
) -> bool:
    return any(
        _nonempty_sequence(source.get("verified_evidence_refs"))
        or (
            source.get("evidence_refs_valid") is True
            and _nonempty_sequence(source.get("evidence_refs"))
        )
        for source in (candidate, analysis)
    )


def _critical_gap(
    candidate: Mapping[str, Any], analysis: Mapping[str, Any]
) -> bool:
    for source in (candidate, analysis):
        for field in ("blocking_gaps", "coverage_gaps", "ai_blocking_gaps"):
            gaps = source.get(field, [])
            if not isinstance(gaps, Sequence) or isinstance(gaps, (str, bytes)):
                return True
            for gap in gaps:
                if not isinstance(gap, Mapping) or gap.get("critical", True) is True:
                    return True
    return False


def _positive_gates_pass(candidate: Mapping[str, Any]) -> bool:
    authorization = candidate.get("authorization_status")
    return (
        candidate.get("deterministic_chain_verified") is True
        and candidate.get("dataflow_status") in _PROVEN_DATAFLOW
        and candidate.get("guard_status") in _POSITIVE_GUARDS
        and authorization is not None
        and authorization != "unknown"
        and authorization not in _PROTECTED_AUTHORIZATION
    )


def _deterministic_negative_proof(candidate: Mapping[str, Any]) -> bool:
    # Negative conclusions require complete coverage for the candidate's domain;
    # a global partial run may still permit positive proof but never auto-refutation.
    if not coverage_allows(candidate, "negative_proof"):
        return False
    # Deliberately read only candidate-level deterministic facts. A nested AI
    # opinion cannot independently establish a false positive.
    if candidate.get("deterministic_negative_proof") is True:
        return True
    if _negative_reason_data(candidate.get("negative_proof")):
        return True
    if candidate.get("authorization_status") in _PROTECTED_AUTHORIZATION:
        return True
    if candidate.get("guard_status") == "present_effective":
        return True
    if candidate.get("guard_coverage_status") in {
        "effective",
        "fail_closed",
        "present_effective",
        "verified_effective",
    }:
        return True
    if candidate.get("disconnected_verified") is True:
        return True
    if any(
        candidate.get(field) in {"disconnected_verified", "verified_disconnected"}
        for field in (
            "connectivity_status",
            "dataflow_status",
            "deterministic_path_status",
            "source_sink_status",
        )
    ):
        return True
    if candidate.get("invalid_source_verified") is True:
        return True
    if candidate.get("invalid_sink_verified") is True:
        return True
    if any(
        candidate.get(field)
        in {"deterministically_invalid", "invalid_verified", "verified_invalid"}
        for field in ("source_status", "sink_status")
    ):
        return True
    return _nonempty_sequence(
        candidate.get("verified_invalid_sources")
    ) or _nonempty_sequence(candidate.get("verified_invalid_sinks"))


def _cross_validated_refutation_basis(
    candidate: Mapping[str, Any], analysis: Mapping[str, Any]
) -> bool:
    """P1-5：采信 AI 的 refutation_basis，但**每一项都必须被规则事实独立证实**。

    设计要点（安全边界）：AI 自报的 basis 可能是幻觉。若无条件采信，真漏洞会被判成
    ai_false_positive——把"高误报"翻转成"漏报"，方向更坏。项目已有先例：AI 自标
    evidence_refs 无效必须拒绝（AI_EVIDENCE_REF_INVALID 不在证据不足白名单内）。

    因此这里只做"AI 指认 + 机制复核"：AI 负责指出**哪一条**确定性反证成立，
    验证完全由 candidate.deterministic_facts（P1-4 注入，规则层静态计算）完成。
    任一项对不上、或所需事实缺失，整体不予采信 —— fail-closed。
    """

    # coverage 门禁与既有确定性负证保持同一标准：域内覆盖不完整时不允许自动否定。
    if not coverage_allows(candidate, "negative_proof"):
        return False

    basis = _refutation_basis_values(candidate, analysis)
    if not basis:
        return False

    facts = candidate.get("deterministic_facts")
    if not isinstance(facts, Mapping) or not facts:
        # 没有规则事实可交叉验证 → 无从复核 AI 断言，维持人工。
        return False

    return all(_refutation_basis_confirmed(item, candidate, facts) for item in basis)


def _refutation_basis_values(
    candidate: Mapping[str, Any], analysis: Mapping[str, Any]
) -> list[str]:
    for source in (candidate, analysis):
        raw = source.get("refutation_basis") if isinstance(source, Mapping) else None
        if isinstance(raw, (list, tuple)) and raw:
            return [str(item) for item in raw if isinstance(item, str) and item.strip()]
    return []


def _refutation_basis_confirmed(
    basis: str, candidate: Mapping[str, Any], facts: Mapping[str, Any]
) -> bool:
    """单项 basis 是否被规则层确定性事实证实。未知取值一律不采信。"""

    if basis == "in_process_terminus":
        # 值流未到达 Sink 实参 = 攻击者数据没有流出去，与"进程内终点"语义一致。
        return facts.get("value_flow_reaches_sink_argument") is False
    if basis == "sender_unreachable":
        # S2：发送方方法无 manifest 入口反向可达（SDK 死代码，V-04 BLE 实证）。
        return facts.get("sender_reachable") is False
    if basis == "guard_fail_closed":
        return (
            facts.get("guard_status") == "present_effective"
            or candidate.get("guard_status") == "present_effective"
        )
    if basis == "non_exported_provider":
        # 组件未导出/受强权限保护属确定性事实，规则层已在 authorization 中给出。
        return (
            facts.get("authorization_status") in _PROTECTED_AUTHORIZATION
            or candidate.get("exported") is False
            or candidate.get("provider_exported") is False
        )
    if basis == "fixed_local_target":
        # P0①（2026-08-15）：目标固定 ≠ 安全——未注册目标是"另一种危害（崩溃 DoS）"
        # 而非"安全"。采信前提是"目标固定**且可达**"：registered 为 False（未注册）或
        # 未知（规则未产出该字段）时不得采信（fail-closed），否则 AI 的 fixed_local_target
        # 会把 DoS 候选误判为 ai_false_positive 吞掉。
        if candidate.get("resolved_target_registered") is False:
            return False
        if candidate.get("resolved_target_registered") is None:
            return False
        return candidate.get("resolved_target_fixed") is True
    if basis == "constant_sink_argument":
        return candidate.get("sink_argument_constant") is True
    if basis == "no_real_call_site":
        return candidate.get("call_site_exists") is False
    return False


def _negative_reason_data(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(value.get(field) for field in ("code", "reason", "facts", "evidence"))
    return _nonempty_sequence(value)


def _nonempty_sequence(value: Any) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and bool(value)
    )


def _ai_validation_applicable(candidate: Mapping[str, Any]) -> bool:
    analysis = _analysis(candidate)
    status = str(candidate.get("analysis_status") or "")
    return (
        bool(analysis)
        or candidate.get("candidate_verdict") is not None
        or candidate.get("analysis_track") in {"l1_triage", "l2_review"}
        or (candidate.get("ai_required") is True and status.startswith("ai_"))
    )


def _has_sdk_semantic_refutation(candidate: Mapping[str, Any]) -> bool:
    """候选是否携带 SDK 语义反证（LocalBroadcastManager/EventBus 进程内分发的 receiver）。

    基于官方 API 确定性语义，静态可证；存在时豁免负向证明的 coverage 保守检查。
    """

    for sink in candidate.get("sinks") or []:
        if isinstance(sink, Mapping) and LOCAL_BROADCAST_RECEIVER_RE.search(
            str(sink.get("receiver_text") or "")
        ):
            return True
    return False


def _evidence_insufficiency_gap(gap: Mapping[str, Any]) -> bool:
    """gap 是否属于"证据不足类"（静态分析限制，非确定性冲突）。

    联合裁决 v1：这类 gap（EXFILTRATION_CHANNEL_UNVERIFIED、DATAFLOW_NOT_PROVEN、
    SYMBOL_TARGET_AMBIGUOUS 等）说明静态分析未能闭合证据，但不与 AI 的语义判定
    冲突——存在时 AI 判定仍可被采信（按置信度分档），只有确定性冲突类 gap 才
    触发 validation_failure。
    """

    code = str(gap.get("code") or "")
    if code in _EVIDENCE_INSUFFICIENCY_GAPS:
        return True
    # 命名约定兜底（v3.0.5）：白名单之外的未知 gap code，按语义模式推断是否
    # "证据不足类"（静态不可证，非确定性冲突）。这解决"白名单未更新"反复复发——
    # 提示词/规则升级后新输出的 gap code 不再需要逐个手补白名单。
    #
    # 证据不足类特征：code 表达"静态分析无法闭合证据"（未验证/未解析/未证明/
    # 未知/限制/近似/停滞/未建立），这类 gap 不与 AI 语义判定冲突。
    _INSUFFICIENCY_PATTERNS = (
        "UNVERIFIED", "UNRESOLVED", "NOT_PROVEN", "UNKNOWN", "LIMITATION",
        "AMBIGUOUS", "STALLED", "NOT_ESTABLISHED", "NOT_DEMONSTRATED",
        "APPROXIMATION", "BUDGET_EXCEEDED", "UNAVAILABLE", "UNCONFIRMED",
        "UNREACHABLE", "PENDING", "MISSING", "NOT_FOUND", "INCOMPLETE",
        "UNPROVEN", "EMPTY", "OVERLOAD", "UNCONTROLLED", "NOT_PROVIDED",
        "NOT_VERIFIED", "RECURSIVE", "TOO_DEEP", "DEEP_REVIEW",
        "NOT_CONFIRMED", "NOT_SPECIFIC",
    )
    if any(pattern in code for pattern in _INSUFFICIENCY_PATTERNS):
        # 显式排除：即使命名像"证据不足"，确定性冲突类 / AI 自标无效类 / 分析
        # 失败类仍必须保持拦截（防幻觉铁律 + 机制否决）。
        _DETERMINISTIC_EXCLUSIONS = {
            # AI 自标证据无效（引用本身无效）→ 否决，不采信
            "AI_EVIDENCE_REF_INVALID",
            "AI_EVIDENCE_REF_REQUIRED",
            # 分析未完成/失败/预算耗尽 → 应重跑 AI，不是降级采信
            "AI_ANALYSIS_FAILED",
            "AI_ANALYSIS_SKIPPED",
            "AI_ANALYSIS_INCOMPLETE",
            "ANALYSIS_NOT_COMPLETE",
            "ANALYSIS_INCOMPLETE",
            "ANALYSIS_FAILED",
            "CONTEXT_BUDGET_EXHAUSTED",
            "AI_MAX_ROUNDS_REACHED",
            # run 级基础设施缺口 → coverage 层语义，非候选级证据不足
            "JADX_PARTIAL_DECOMPILATION",
            "JADX_FAILED",
            "JADX_NO_PSEUDO_SOURCE",
            "INDEX_FILES_SKIPPED",
            "RULE_PRESCAN_PARTIAL",
            "RULE_FAILED",
        }
        return code not in _DETERMINISTIC_EXCLUSIONS
    return False


def _coverage_allows_joint(
    candidate: Mapping[str, Any], claim: str
) -> bool:
    """联合裁决版 coverage 检查：证据不足类 gap 不阻断证明方向。

    与 coverage_allows 的差异：gap 命中 _EVIDENCE_INSUFFICIENCY_GAPS（静态分析
    限制类）时不视为覆盖缺口——这类 gap 说明"证据未闭合"而非"证明被反驳"，
    AI 的语义判定仍可采信（按置信度分档）。确定性冲突类 gap（guard 阻断、闭链
    反判、红线否定等）仍保持 fail-closed。
    """

    gaps = candidate.get("coverage_gaps", []) or []
    explicit = candidate.get(f"{claim}_coverage_complete")
    if explicit is not None and explicit is False:
        # 显式标记未完成：若全部 gap 均为证据不足类（静态限制），联合裁决放行；
        # 否则 fail-closed。
        if gaps and all(
            isinstance(gap, Mapping) and _evidence_insufficiency_gap(gap)
            for gap in gaps
        ):
            return True
        return explicit is True
    if explicit is True:
        return True
    impact_field = "affects_positive_proof" if claim == "positive_proof" else "affects_negative_proof"
    for gap in gaps:
        if not isinstance(gap, Mapping):
            return False
        if gap.get(impact_field) is True:
            # 证据不足类 gap（静态分析限制）不视为确定性冲突 → 放行。
            if _evidence_insufficiency_gap(gap):
                continue
            return False
        impact = gap.get("claim_impact")
        if impact in {claim, "both"}:
            # 联合裁决 v1：显式声明影响该证明方向的 gap——若是证据不足类
            # （静态分析限制），不视为确定性冲突，放行；否则拦截。
            if _evidence_insufficiency_gap(gap):
                continue
            return False
        if impact is None and gap.get("critical", True):
            # 未标注 claim 方向的 critical gap：证据不足类放行，其余拦截
            if _evidence_insufficiency_gap(gap):
                continue
            return False
    return True


def _ai_strong_support(candidate: Mapping[str, Any], analysis: Mapping[str, Any]) -> bool:
    """AI 强成立：四要素全真 + 高置信 + 数据流已证 → 可直接采信 supported。"""

    ex = analysis.get("exploitability") or {}
    return (
        _trusted_ai_outcome(analysis) in {"supports_candidate", "positive", "supported"}
        and analysis.get("flaw_holds") is True
        and ex.get("entry_reachable") is True
        and ex.get("propagation_proven") is True
        and ex.get("sink_effective") is True
        and analysis.get("confidence_tier") == "high"
        and candidate.get("dataflow_status") in _PROVEN_DATAFLOW
    )


def _ai_likely_supported(candidate: Mapping[str, Any], analysis: Mapping[str, Any]) -> bool:
    """AI 中成立：缺陷成立 + 入口可达（传播可未证）→ 倾向成立待人工确认。

    联合裁决 v1：不依赖 verdict 方向（3.0.4 前 verdict 可能仍是 unresolved），
    以 AI 的中间判定（flaw_holds=True + entry_reachable=True）为方向信号。
    """

    ex = analysis.get("exploitability") or {}
    return (
        analysis.get("flaw_holds") is True
        and ex.get("entry_reachable") is True
    )


def _ai_likely_false_positive(candidate: Mapping[str, Any], analysis: Mapping[str, Any]) -> bool:
    """AI 否定：flaw=False 且证据充分（无 critical gap）→ 倾向误报待人工快速确认。

    联合裁决 v1：以 flaw_holds=False 为否定信号（与 _ai_likely_supported 以
    flaw=True 为成立信号对称），不依赖 verdict 方向——3.0.4 前 AI 常判
    flaw=False 但 verdict 保守 unresolved（不敢写 refutes），若要求 verdict
    在 refutes 会把 AI 的否定信号浪费掉（实测 29/30 个 flaw=False 候选因此
    漏采信）。guard 阻断（blocked）与确定性反证（deterministically_refuted /
    ai_false_positive）由更早分支处理，不进入这里。

    v2026-08-14（矛盾①修复）：AI 在证据严重不足（critical gap：DATAFLOW_NOT_PROVEN /
    SYMBOL_TARGET_AMBIGUOUS / EXFILTRATION_CHANNEL_UNVERIFIED）下判 flaw=False，
    本质是"没找到成立的证据"而非"找到不成立的证据"（违反"未找到证据≠不成立"铁律，
    实测 6×MainTabActivity 案例）。只有证据充分（无 critical gap）时的 flaw=False
    才作为否定信号采信，否则回落 unresolved。
    """

    if analysis.get("flaw_holds") is not False:
        return False
    for source in (candidate, analysis):
        for field in ("blocking_gaps", "ai_blocking_gaps"):
            gaps = source.get(field, [])
            if not isinstance(gaps, Sequence) or isinstance(gaps, (str, bytes)):
                return False
            if any(
                isinstance(gap, Mapping) and gap.get("critical", True) is True
                for gap in gaps
            ):
                # 证据不足下的否定不可信——AI 没找到成立证据，不等于证据表明不成立。
                return False
    return True


def _applicable_critical_gap(
    candidate: Mapping[str, Any], analysis: Mapping[str, Any]
) -> bool:
    for source in (candidate, analysis):
        for field in ("blocking_gaps", "ai_blocking_gaps"):
            gaps = source.get(field, [])
            if not isinstance(gaps, Sequence) or isinstance(gaps, (str, bytes)):
                return True
            if any(
                not isinstance(gap, Mapping)
                or (
                    gap.get("critical", True) is True
                    # 联合裁决 v1：证据不足类 gap（静态分析限制，非确定性冲突）不触发
                    # validation_failure——AI 判定仍可被采信（降级 confidence 分档）。
                    and not _evidence_insufficiency_gap(gap)
                )
                for gap in gaps
            ):
                return True

    outcome = _trusted_ai_outcome(analysis)
    # 联合裁决 v1：AI 有方向判定（flaw_holds 非空）时，只检查对应方向的 coverage——
    # 判成立只查 positive_proof，判否定只查 negative_proof；unresolved 才两向都查。
    ai_direction = analysis.get("flaw_holds")
    if ai_direction is True:
        claims = ("positive_proof",)
    elif ai_direction is False:
        claims = ("negative_proof",)
    elif outcome in _REFUTATION_OUTCOMES:
        claims = ("negative_proof",)
    elif outcome == "unresolved":
        claims = ("positive_proof", "negative_proof")
    else:
        claims = ("positive_proof",)
    sdk_refutation = _has_sdk_semantic_refutation(candidate)
    for claim in claims:
        if sdk_refutation and claim == "negative_proof":
            # SDK 语义反证（LocalBroadcast/EventBus 进程内分发）静态可证，豁免负向覆盖检查
            continue
        if not _coverage_allows_joint(candidate, claim):
            return True
    analysis_coverage = analysis.get("coverage_gaps", [])
    if not isinstance(analysis_coverage, Sequence) or isinstance(analysis_coverage, (str, bytes)):
        return True
    for gap in analysis_coverage:
        if not isinstance(gap, Mapping):
            return True
        impact = gap.get("claim_impact")
        if gap.get("critical", True) is True and (
            impact in {None, "both"}
            or "positive_proof" in claims and impact == "positive_proof"
            or "negative_proof" in claims and impact == "negative_proof"
        ):
            # 联合裁决 v1：证据不足类 coverage gap 不触发 validation_failure
            # （静态分析限制，非确定性冲突）。
            if _evidence_insufficiency_gap(gap):
                continue
            return True
    return False


def _ai_contract_failure(
    candidate: Mapping[str, Any], *, allow_partial_evidence: bool = False
) -> str | None:
    if not _ai_validation_applicable(candidate):
        return None
    analysis = _analysis(candidate)
    status = _first_value(candidate, analysis, "ai_analysis_status", "analysis_status", "status")
    if isinstance(status, str) and status.lower() in _INCOMPLETE_STATUSES:
        return f"ANALYSIS_{status.upper().removeprefix('AI_')}"
    if not isinstance(status, str) or status.lower() not in {"completed", "ai_completed"}:
        return "ANALYSIS_NOT_COMPLETE"
    if analysis.get("analysis_complete") is not True:
        return "ANALYSIS_NOT_COMPLETE"
    if _invalid_evidence_refs({}, analysis) and not allow_partial_evidence:
        return "INVALID_EVIDENCE_REFS"
    if not _valid_evidence_refs({}, analysis):
        # 无任何有效证据引用 → 判定无支撑，不得采信（无论宽松与否）
        return "VALID_EVIDENCE_REFS_REQUIRED"
    if analysis.get("semantic_evidence_complete") is not True and not allow_partial_evidence:
        return "SEMANTIC_EVIDENCE_INCOMPLETE"
    if _applicable_critical_gap(candidate, analysis):
        return "CRITICAL_BLOCKING_OR_COVERAGE_GAP"
    return None


def _validate_supplied_ai_contexts(candidate: dict[str, Any]) -> None:
    contexts: Sequence[Mapping[str, Any]] | None = None
    for field in ("ai_evidence_contexts", "slice_contexts", "contexts"):
        value = candidate.get(field)
        # 空列表视为未提供：不得用空上下文覆盖 verify_candidate 阶段由
        # slice_refs + 索引回查恢复的校验结果（v3.0.5 防御）。
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and value:
            contexts = [item for item in value if isinstance(item, Mapping)]
            break
    if contexts is None or not contexts or not isinstance(candidate.get("ai_analysis"), Mapping):
        return
    validation = validate_ai_evidence_references(candidate, contexts)
    public_validation = {
        key: value for key, value in validation.items()
        if key != "ai_evidence_blocking_gaps"
    }
    candidate.update(public_validation)
    candidate["ai_analysis"] = {**candidate["ai_analysis"], **public_validation}
    gaps = list(candidate.get("ai_blocking_gaps", []))
    # 幂等（v3.0.5）：与 evidence.verify_candidate 一致，先剔除校验器专属 code 旧值
    # 再追加本次结果，避免 checkpoint 恢复/重试携带的残留 AI_EVIDENCE_REF_INVALID 拦截。
    _VALIDATOR_GAP_CODES = {
        "AI_EVIDENCE_REF_INVALID",
        "AI_EVIDENCE_REF_REQUIRED",
        "AI_EVIDENCE_REQUIREMENTS_UNRESOLVED",
        "AI_EVIDENCE_SEMANTIC_INCOMPLETE",
    }
    gaps = [
        gap for gap in gaps
        if not isinstance(gap, Mapping) or gap.get("code") not in _VALIDATOR_GAP_CODES
    ]
    existing_codes = {
        gap.get("code") for gap in gaps if isinstance(gap, Mapping)
    }
    for gap in validation["ai_evidence_blocking_gaps"]:
        if gap.get("code") not in existing_codes:
            gaps.append(gap)
            existing_codes.add(gap.get("code"))
    candidate["ai_blocking_gaps"] = gaps


class DecisionEngine:
    """Pipeline v2 决策入口；写回 review_state 和可审计误报依据。"""

    def decide(
        self, candidate: dict[str, Any], *, enforce_ai_contract: bool = True
    ) -> dict[str, Any]:
        validation_failure = (
            _ai_contract_failure(candidate) if enforce_ai_contract else None
        )
        analysis = _analysis(candidate)
        verdict = _trusted_ai_outcome(analysis)
        # 联合裁决 v1：AI 有方向判定（flaw_holds 非空）且机制未排除时，允许部分证据
        # 缺失（INVALID_EVIDENCE_REFS / SEMANTIC_EVIDENCE_INCOMPLETE）降级采信——
        # 用宽松模式重新评估 contract，仅保留真正不可采信的类型（无有效 refs /
        # 分析未完成 / 确定性冲突 gap）。AI 判定分档据此决定采信强度。
        joint_failure = (
            _ai_contract_failure(candidate, allow_partial_evidence=True)
            if enforce_ai_contract
            else None
        )
        # 确定性 guard 验证（v2026-08-09）：debuggable guard 在 release 包（debuggable=false）
        # 阻断链路 → AI 的"成立"判定不得采信（消除 ADBDebugActivity 类高置信误报）；
        # AI 的否定仍保留（guard 佐证否定方向）。guard_blocked 候选已由 funnel 跳过 AI，
        # 此处仅需在 decision 给 blocked 语义（见下方 blocked 分支）。
        guard_blocks = candidate.get("guard_blocks") or []
        guard_blocked = any(
            isinstance(b, Mapping) and b.get("type") == "debuggable"
            for b in guard_blocks
        )
        deterministic_basis = deterministic_refutation_basis(candidate)
        if not coverage_allows(candidate, "negative_proof"):
            # coverage 保守只拦"非 SDK 语义"的负向证明（防假阴性铁律）。SDK 语义反证
            # （local_broadcast_intra_process：LocalBroadcastManager/EventBus 进程内分发）
            # 基于官方 API 的确定性语义、静态可证，不依赖 AI 语义判断，独立生效——
            # 否则 LocalBroadcast 误报永远无法被确定性否定（v2026-08-09，docs/updates/）。
            deterministic_basis = [
                basis for basis in deterministic_basis
                if basis in _SDK_SEMANTIC_REFUTATIONS
            ]
        # P1-5 接线（v2026-08-15 修订）：AI 的 refutation_basis 六值（fixed_local_target 等）
        # 经 _cross_validated_refutation_basis 与 candidate.deterministic_facts 逐项交叉验证
        # 通过后，同样构成确定性否定背书。修订前该机制只接在 decide_candidate（测试/兼容
        # 入口），生产路径 DecisionEngine.decide 从未调用——AI 输入切片也看不到
        # resolved_target_fixed 等字段，交叉验证在生产上永不触发（safe 但无效）。
        if not deterministic_basis and _cross_validated_refutation_basis(candidate, analysis):
            deterministic_basis = list(_refutation_basis_values(candidate, analysis)) or []
        if guard_blocked and verdict not in _REFUTATION_OUTCOMES:
            # 方案 X'（v2026-08-09）：guard 阻断 = "条件不可利用"（如 debuggable guard 在
            # release 包拦死链路），不是误报（区别于 deterministically_refuted）——调试
            # 功能真实存在，若未来分发 debuggable 构建则高危。保留候选可见 + guard 证据。
            # AI 否定（refutes）时仍走否定路径：guard 佐证否定方向。
            evidence_decision = "blocked"
            false_positive_basis = []
        elif not validation_failure and verdict in _REFUTATION_OUTCOMES and deterministic_basis:
            evidence_decision = "ai_false_positive"
            false_positive_basis = deterministic_basis
        elif deterministic_basis:
            evidence_decision = "deterministically_refuted"
            false_positive_basis = []
        elif verdict in _REFUTATION_OUTCOMES and analysis.get("flaw_holds") is True:
            # v2026-08-14（矛盾②修复）：AI 输出自相矛盾——verdict=refutes（想否决）
            # 但 flaw_holds=True（成立信号），且无确定性 basis 背书（上面分支未命中）。
            # 此时两套信号冲突，采信任一方向都可能错（3fe8a217 案例：AI 的 flaw=True
            # 是错的，removePref key 固定常量不可控）。不采信 → unresolved + 矛盾 gap，
            # 人工可见冲突并复核。
            evidence_decision = "unresolved"
            false_positive_basis = []
            _ai_blocking_gaps = candidate.setdefault("ai_blocking_gaps", [])
            if not isinstance(_ai_blocking_gaps, list):
                _ai_blocking_gaps = []
                candidate["ai_blocking_gaps"] = _ai_blocking_gaps
            _ai_blocking_gaps.append({
                "code": "AI_VERDICT_FLAW_CONFLICT",
                "critical": True,
                "message": "AI verdict=refutes 但 flaw_holds=True，输出自相矛盾，不采信任何方向",
            })
        elif _ai_strong_support(candidate, analysis) and not joint_failure:
            # 联合裁决 v1：AI 强成立（四要素全真 + 高置信 + 数据流已证）→ 采信 supported。
            evidence_decision = "supported"
            false_positive_basis = []
        elif _ai_likely_supported(candidate, analysis) and not joint_failure:
            # 联合裁决 v1：AI 中成立（缺陷成立 + 入口可达，传播/外溢未证）→
            # 倾向成立，进入人工快速确认队列（验证传播与外溢方向）。
            # 宽松模式（joint_failure）放行部分证据缺失（INVALID_EVIDENCE_REFS /
            # SEMANTIC_EVIDENCE_INCOMPLETE），只要 AI 有方向判定 + verified refs。
            evidence_decision = "ai_likely_supported"
            false_positive_basis = []
        elif _ai_likely_false_positive(candidate, analysis) and not joint_failure:
            # 联合裁决 v1：AI 否定（flaw=False）且无确定性反证背书 → 倾向误报，
            # 人工快速确认（防假阴性铁律：不直接删除，保留候选）。
            evidence_decision = "ai_likely_false_positive"
            false_positive_basis = []
        elif validation_failure:
            # 宽松模式下仍失败（无有效 refs / 分析未完成 / 确定性冲突 gap）→ 不采信。
            # 放在分档之后：AI 有方向判定时，部分证据缺失不再一票否决（联合裁决 v1）。
            evidence_decision = "unresolved"
            false_positive_basis = []
        elif candidate.get("evidence_level") == "L1":
            evidence_decision = "exposure_only"
            false_positive_basis = []
        elif (
            candidate.get("deterministic_chain_verified") is True
            and candidate.get("dataflow_status") in _PROVEN_DATAFLOW
            # v2026-08-14（矛盾③修复）：deterministic_chain_verified 是"方法内传播
            # 证明"（intraprocedural），不验证调用点存在性——死代码方法也能
            # chain_verified=True（89da4b67 案例：AccountChangedBroadcastHelper 全库
            # 无调用点、entry_method_id=None 却标 supported）。supported 必须绑定
            # 真实入口，否则降为 unresolved（配合 AI"无调用点"线索人工复核）。
            and candidate.get("entry_method_id")
        ):
            evidence_decision = "supported"
            false_positive_basis = []
        else:
            evidence_decision = "unresolved"
            false_positive_basis = []
        state = derive_review_state(
            candidate,
            evidence_decision=evidence_decision,
            false_positive_basis=false_positive_basis,
        )
        # 联合裁决 v1（v3.0.5 修正）：退回 pending_ai 必须以宽松模式（joint_failure）
        # 为准，与采信分档同权——严格模式失败但宽松模式通过（AI 有方向判定 + 有效
        # refs）时采信已成立，状态应保持 pending_manual（人工确认），不得再被
        # validation_failure 覆盖成 pending_ai（实测 14 个 ai_likely_supported 因此
        # 被误标，unresolved 队列虚增）。
        if joint_failure and candidate.get("review_status") not in _MANUAL_TERMINAL_STATUSES:
            state = {
                **state,
                "status": "pending_ai",
                "reason": "ai_evidence_contract_not_satisfied",
            }
        candidate["evidence_decision"] = evidence_decision
        candidate["false_positive_basis"] = false_positive_basis
        candidate["decision_reason_codes"] = (
            [validation_failure] if validation_failure else []
        )
        candidate["review_state"] = state
        candidate["review_status"] = state["status"]
        return candidate

    def apply(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for candidate in candidates:
            _validate_supplied_ai_contexts(candidate)
            self.decide(candidate, enforce_ai_contract=True)
        return candidates


def decide_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return DecisionEngine().apply(candidates)
