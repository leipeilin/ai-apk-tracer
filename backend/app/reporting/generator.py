"""报告草稿生成器（M3-1 门禁 + provider 投影 + 组装）。

门禁（拒绝路径——最保守语义）：
1. ``allow_executable_poc=True`` → ValidationError(EXECUTABLE_POC_FORBIDDEN)
   ——当前实现不存在可执行生成路径，开关置真视为配置违例直接拒绝；
2. ``require_confirmed_finding=True`` 且 review_status != "confirmed" →
   ConflictError(REPORT_DRAFT_REQUIRES_CONFIRMED)；
3. L1 informational → ConflictError(L1_REPORT_FORBIDDEN)（沿确定性报告先例）。

provider 抽象（方案取舍 1）：M3-1 默认投影 provider 从 finding.ai_analysis
（L2 已验证输出）投影 ReportDraft（provenance="projected_from_l2_review"
——不冒充新 AI 生成）；M3-2 接入真实 prompt 协议时仅替换 provider，
ReportDocument 结构零改动。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from app.analysis.ai_models import ReportDraftOutput, ReportEvidenceRef, ReportInput
from app.config import ReportSettings
from app.reporting.models import (
    EXPLORER_CAVEAT,
    EvidencePointer,
    ReportDocument,
    ReportDraft,
)
from app.reporting.poc import build_poc_skeleton
from app.reporting.repair import build_repair_draft
from app.shared.errors import ConflictError, ValidationError

LOGGER = logging.getLogger(__name__)

# finding_id 落盘文件名字符白名单（评审 R-5：防路径注入）
_FINDING_ID_PATTERN = re.compile(r"[A-Za-z0-9_.\-]+")

# finding → ReportDraft（M3-2 接入真实 prompt 协议时替换此实现）
ReportDraftProvider = Callable[[dict[str, Any]], Awaitable[ReportDraft]]

# 确定性投影字段（原样复制不改写——方案 §2 deterministic 定义）
_DETERMINISTIC_FIELDS = (
    "rule_id", "rule_ids", "component", "component_name", "severity",
    "severity_reason", "review_status", "review_reason", "review_state",
    "evidence_level", "sources", "sinks", "locations", "propagation_paths",
    "entry_method_id", "entry_points", "guard_status", "authorization_status",
    "dynamic_validation_status", "binder_transactions", "flow_kind",
    "attacker_prerequisites", "sanitizers_or_guards", "manifest_facts",
    "title", "description", "app",
)


def _evidence_pointers(finding: dict[str, Any]) -> list[EvidencePointer]:
    """从 sources/sinks/locations 投影可回查引用（去重）。"""

    pointers: list[EvidencePointer] = []
    seen: set[tuple[str, int | None]] = set()
    for bucket, default_note in (("sources", "source"), ("sinks", "sink"), ("locations", "location")):
        for item in finding.get(bucket) or []:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if not path:
                continue
            line = item.get("line")
            if not isinstance(line, int) or line < 1:
                line = None
            key = (str(path), line)
            if key in seen:
                continue
            seen.add(key)
            pointers.append(EvidencePointer(
                path=str(path), line=line,
                note=str(item.get("text") or default_note)[:200] or default_note,
            ))
    return pointers


async def project_draft_from_l2_review(finding: dict[str, Any]) -> ReportDraft:
    """默认投影 provider：从 finding.ai_analysis（L2 已验证输出）投影草稿。

    provenance="projected_from_l2_review"——诚实标注（非新 AI 生成）。
    """

    ai = finding.get("ai_analysis") or {}
    title = str(finding.get("title") or finding.get("rule_id") or "未命名发现")
    component = str(finding.get("component_name") or finding.get("component") or "未知组件")
    verdict = ai.get("candidate_verdict") or "unresolved"
    confidence = ai.get("confidence_tier") or "low"
    harm = ai.get("harm") or "危害未评估"
    description = str(finding.get("description") or "")

    summary = (
        f"{component} 存在 {title}（L2 复核裁决：{verdict}，置信 {confidence}）。"
        f"{description[:300]}{'…' if len(description) > 300 else ''}"
    ).strip()
    narrative = (
        f"规则 {finding.get('rule_id')} 在 {component} 上命中；"
        f"L2 独立复核结论：{verdict}（flaw_holds={ai.get('flaw_holds')}）。{harm}"
    )
    exploit = str(
        ai.get("exploitability") and "可利用性评估见 deterministic 投影与 L2 复核记录"
        or "可利用性评估缺失（待补充）")
    return ReportDraft(
        summary=summary,
        vulnerability_narrative=narrative,
        exploit_scenario=exploit,
        evidence_refs=_evidence_pointers(finding),
        confidence_tier=confidence if confidence in {"low", "medium", "high"} else "low",
        analysis_complete=bool(ai.get("analysis_complete")),
    )


def _deterministic_projection(finding: dict[str, Any]) -> dict[str, Any]:
    return {field: finding.get(field) for field in _DETERMINISTIC_FIELDS}


_L2_VERDICT_VALUES = {"supports_candidate", "refutes_candidate", "unresolved"}
_TIER_VALUES = {"low", "medium", "high"}


def _build_report_input(finding: dict[str, Any]) -> ReportInput:
    """finding → ReportInput 投影（评审 R-6/R-7）。

    deterministic_summary 拼接确定性字段（排除 finding.description——
    L2 AI 文本，混入即违背 AI/确定性分离）；l2_* 用 candidate_verdict +
    harm 结构化子字段（真实 ai_analysis 键名）；探索种子三字段按存在性
    透传（归一化层当前不产出——接口就绪数据缓期，评审 R-1）。
    """

    ai = finding.get("ai_analysis") or {}
    harm = ai.get("harm") if isinstance(ai.get("harm"), dict) else {}
    parts = [
        f"规则: {finding.get('rule_id')}",
        f"组件: {finding.get('component_name')}（{finding.get('component')}）",
        f"source 事实: {json.dumps(finding.get('sources') or [], ensure_ascii=False)[:3000]}",
        f"sink 事实: {json.dumps(finding.get('sinks') or [], ensure_ascii=False)[:2000]}",
        f"Guard: {finding.get('guard_status')}",
        f"授权: {finding.get('authorization_status')}",
        f"数据流: {finding.get('flow_kind')}",
        f"严重性提示: {finding.get('severity_hint')}",
    ]
    deterministic_summary = "；".join(str(p) for p in parts if p and str(p) != "None") or "确定性事实缺失"
    if len(deterministic_summary) > 9500:
        deterministic_summary = deterministic_summary[:9500] + "…(truncated)"
    l2_verdict = ai.get("candidate_verdict")
    l2_tier = ai.get("confidence_tier")
    pointers = _evidence_pointers(finding)
    return ReportInput(
        finding_id=str(finding.get("id") or finding.get("finding_id") or "unknown"),
        rule_id=str(finding.get("rule_id") or "unknown"),
        component_name=str(finding.get("component_name") or finding.get("component") or "unknown"),
        deterministic_summary=deterministic_summary,
        explorer_hypothesis=finding.get("explorer_hypothesis"),
        explorer_impact_proposal=finding.get("explorer_impact_proposal"),
        explorer_component_summary=finding.get("explorer_component_summary"),
        evidence_refs=[
            ReportEvidenceRef(
                path=p.path, line=p.line, end_line=p.end_line,
                note=(p.note[:200] if p.note else None))
            for p in pointers
        ],
        l2_verdict=l2_verdict if l2_verdict in _L2_VERDICT_VALUES else None,
        l2_confidence_tier=l2_tier if l2_tier in _TIER_VALUES else None,
        l2_flaw_holds=ai.get("flaw_holds") if isinstance(ai.get("flaw_holds"), bool) else None,
        l2_harm_impact_type=harm.get("impact_type"),
        l2_harm_impact_target=harm.get("impact_target"),
    )


async def _ai_report_draft(
    finding: dict[str, Any], analyzer: Any
) -> tuple[ReportDraft | None, dict[str, Any]]:
    """真协议调用（M3-2 评审 R-5）：成功 (draft, meta)；失败 (None, failure)。

    evidence_refs 由确定性投影补齐（防 AI 虚构引用——DraftOutput 无此字段）。
    """

    result = await analyzer.report_entry(_build_report_input(finding))
    if not isinstance(result, dict) or result.get("status") != "completed":
        return None, {
            "fallback": True,
            "classification": result.get("classification") if isinstance(result, dict) else None,
            "message": str(result.get("message") or "")[:200] if isinstance(result, dict) else "协议返回异常",
        }
    try:
        output = ReportDraftOutput.model_validate(result.get("analysis") or {})
    except PydanticValidationError:  # repair 后仍不符合严格契约则降级（协议层不抛出）
        return None, {
            "fallback": True, "classification": "response_invalid",
            "message": "分析结果不符合 ReportDraftOutput 严格契约",
        }
    metadata = result.get("metadata") or {}
    return ReportDraft(
        summary=output.summary,
        vulnerability_narrative=output.vulnerability_narrative,
        exploit_scenario=output.exploit_scenario,
        evidence_refs=_evidence_pointers(finding),
        confidence_tier=output.confidence_tier,
        analysis_complete=output.analysis_complete,
    ), {
        "fallback": False,
        "prompt_version": metadata.get("prompt_version"),
        "model": metadata.get("model"),
    }


async def generate_report_document(
    finding: dict[str, Any],
    *,
    settings: ReportSettings | None = None,
    provider: ReportDraftProvider | None = None,
    analyzer: Any = None,
) -> ReportDocument:
    """门禁校验 → 草稿（provider 显式注入优先，其次 analyzer 真协议，
    缺省投影——评审 R-10a 优先级）→ 组装 ReportDocument（不落盘）。

    analyzer 真协议失败时降级回投影（报告永不因 AI 阻塞——provenance 诚实
    标注 + fallback 可观测，评审 R-9）。
    """

    config = settings or ReportSettings()
    if config.allow_executable_poc:
        raise ValidationError(
            "可执行 PoC 生成路径不存在：allow_executable_poc 必须保持 false（仅骨架）",
            "EXECUTABLE_POC_FORBIDDEN",
        )
    if config.require_confirmed_finding and finding.get("review_status") != "confirmed":
        raise ConflictError(
            f"报告草稿仅对 confirmed finding 生成（当前 {finding.get('review_status')}）",
            "REPORT_DRAFT_REQUIRES_CONFIRMED",
        )
    # L1 拒绝双条件（评审 R-2：对齐 report.py:360 先例——informational
    # severity 同样不进正式报告，仅查 evidence_level 会放行绕过）
    if finding.get("evidence_level") == "L1" or finding.get("severity") == "informational":
        raise ConflictError("L1 提示项不进入正式漏洞报告", "L1_REPORT_FORBIDDEN")

    finding_id = str(finding.get("id") or finding.get("finding_id") or "")
    if not finding_id:
        raise ValidationError(
            "finding 缺少稳定 ID，无法生成报告草稿（防 unknown 兜底多 finding 覆盖）",
            "FINDING_ID_MISSING",
        )
    if not _FINDING_ID_PATTERN.fullmatch(finding_id):
        raise ValidationError(
            f"finding ID 含非法字符: {finding_id!r}", "FINDING_ID_INVALID")

    draft: ReportDraft | None = None
    ai_meta: dict[str, Any] = {}
    if provider is not None:
        draft = await provider(finding)
    elif analyzer is not None:
        draft, ai_meta = await _ai_report_draft(finding, analyzer)
        if draft is None:
            draft = await project_draft_from_l2_review(finding)
    else:
        draft = await project_draft_from_l2_review(finding)
    assert draft is not None

    evidence_source = (
        "explorer_candidate" if finding.get("candidate_source") == "explorer"
        else "rule_candidate"
    )
    used_ai_protocol = bool(ai_meta) and not ai_meta.get("fallback")
    return ReportDocument(
        finding_id=finding_id,
        run_id=str(finding.get("run_id") or ""),
        evidence_source=evidence_source,
        explorer_caveat=EXPLORER_CAVEAT if evidence_source == "explorer_candidate" else None,
        deterministic=_deterministic_projection(finding),
        ai_draft={
            "summary": draft.summary,
            "narrative": draft.vulnerability_narrative,
            "exploit_scenario": draft.exploit_scenario,
            "confidence_tier": draft.confidence_tier,
            "provenance": (
                "ai_report_protocol" if used_ai_protocol else "projected_from_l2_review"
            ),
            "prompt_version": ai_meta.get("prompt_version"),
            "model": ai_meta.get("model"),
            "analysis_complete": draft.analysis_complete,
            **({"fallback": ai_meta} if ai_meta.get("fallback") else {}),
        },
        poc_skeleton=build_poc_skeleton(finding),
        repair=build_repair_draft(finding),
    )


def save_report_document(document: ReportDocument, run_dir: Path) -> Path:
    """落盘 run_dir/reports/drafts/{finding_id}.json（0o700——沿报告先例）。

    symlink 防护（评审 R-5：预置 symlink 可写穿——沿 _existing_or_scoped_path
    精神，写前拒绝非常规路径）。
    """

    drafts_dir = run_dir / "reports" / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = drafts_dir / f"{document.finding_id}.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValidationError(
            f"报告草稿落盘路径异常（symlink/非常规文件）: {path}", "REPORT_DRAFT_PATH_UNSAFE")
    path.write_text(document.model_dump_json(indent=2), "utf-8")
    return path
