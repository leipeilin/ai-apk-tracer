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

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

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


async def generate_report_document(
    finding: dict[str, Any],
    *,
    settings: ReportSettings | None = None,
    provider: ReportDraftProvider | None = None,
) -> ReportDocument:
    """门禁校验 → provider 草稿 → 组装 ReportDocument（不落盘）。"""

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

    draft_provider = provider or project_draft_from_l2_review
    draft = await draft_provider(finding)
    evidence_source = (
        "explorer_candidate" if finding.get("candidate_source") == "explorer"
        else "rule_candidate"
    )
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
            "provenance": "projected_from_l2_review",
            "prompt_version": None,
            "model": None,
            "analysis_complete": draft.analysis_complete,
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
