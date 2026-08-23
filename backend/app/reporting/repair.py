"""修复建议构造（确定性与 AI 分列——方案 §2 RepairDraft）。

确定性映射对齐 findings/report.py remediation 先例（rule_id/组件类型
→ 固定建议）；AI 部分在 M3-1 投影阶段从 L2 复核结论投影（无则空）。
"""

from __future__ import annotations

from typing import Any

from app.reporting.models import RepairDraft

# 组件/规则关键词 → 确定性建议（沿 report.py:477 语义按组件类型扩展）
_DETERMINISTIC_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("BINDER", "CALLER_CHECK", "SERVICE"), (
        "对 exported service 的 Binder 事务增加调用者身份校验（签名级 permission 或 enforceCallingPermission",
        "按事务 code 白名单化可执行操作，拒绝未声明的事务",
    )),
    (("ACTIVITY", "INTENT", "ROUTE", "EXTERNAL"), (
        "取消不必要导出或增加签名级权限",
        "在敏感操作前校验调用者身份并对外部参数实施严格白名单",
    )),
    (("PROVIDER",), (
        "为 exported provider 增加读/写权限保护（signature 级）",
        "收紧 URI 匹配范围并对 path 参数做白名单校验",
    )),
    (("RECEIVER", "BROADCAST"), (
        "为敏感广播接收器增加 permission 保护或改为显式/本地广播",
        "校验广播来源并对 extras 做严格输入校验",
    )),
    (("URI", "DEEP_LINK"), (
        "校验深链 URI 的 scheme/host 白名单",
        "敏感操作不直接由深链参数驱动",
    )),
)

_DEFAULT_RECOMMENDATIONS = (
    "取消不必要导出或增加签名级权限",
    "在敏感操作前校验调用者身份并对外部参数实施严格白名单",
)


def _deterministic_recommendations(finding: dict[str, Any]) -> list[str]:
    rule_id = str(finding.get("rule_id") or "")
    component = str(finding.get("component") or "")
    for keywords, recommendations in _DETERMINISTIC_HINTS:
        if any(keyword in rule_id.upper() or keyword in component.upper() for keyword in keywords):
            return list(recommendations)
    return list(_DEFAULT_RECOMMENDATIONS)


def build_repair_draft(finding: dict[str, Any]) -> RepairDraft:
    """finding → 修复建议草稿（确定性映射 + L2 复核投影的 AI 部分）。"""

    ai_analysis = finding.get("ai_analysis") or {}
    ai_recommendations: list[str] = []
    ai_rationale: str | None = None
    # M3-1：AI 建议从 L2 复核的候选裁决投影（无则空——诚实留白）
    verdict = str(ai_analysis.get("candidate_verdict") or "")
    if verdict:
        ai_recommendations.append(
            f"依据 L2 复核裁决（{verdict}）复核以下确定性建议的适用范围")
        ai_rationale = (
            f"L2 复核 confidence_tier={ai_analysis.get('confidence_tier')}，"
            f"flaw_holds={ai_analysis.get('flaw_holds')}"
            if ai_analysis.get("confidence_tier") is not None else None
        )
    return RepairDraft(
        deterministic_recommendations=_deterministic_recommendations(finding),
        ai_recommendations=ai_recommendations,
        ai_rationale=ai_rationale,
    )
