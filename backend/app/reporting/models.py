"""M3-1 报告数据结构（方案 §2 字段设计）。

设计取舍：
- ``EvidencePointer`` 为轻量引用（path/line/end_line/note）——不复用
  ``ai_models.EvidenceReference``（其 context_id/claim 必填属 AI 协议
  切片语义，finding 投影场景无此数据；实施偏差已记录于验收文档）；
- ``ReportDocument.deterministic`` 保持原样投影（dict——"不改写"语义，
  强 schema 反而引入漂移风险）；
- ``PoCSkeleton.executable_files_created`` 恒空列表——供机器断言
  （allow_executable_poc=False 的结构化证明）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

EvidenceSource = Literal["rule_candidate", "explorer_candidate"]
DraftProvenance = Literal["projected_from_l2_review", "ai_report_protocol"]
PocKind = Literal["intent", "uri", "binder_transaction", "broadcast", "provider_query"]

# explorer 候选证据置信度告警（指引 §6.3——M2 质量项未闭环期间的固定口径）
EXPLORER_CAVEAT = (
    "explorer_validated=0 期间，探索质量未达标，探索候选证据置信度低于规则候选"
)


class EvidencePointer(BaseModel):
    """可回查证据引用（轻量——从 finding 的 sources/sinks/locations 投影）。"""

    path: str = Field(description="工作区相对路径")
    line: int | None = Field(default=None, ge=1, description="起始行")
    end_line: int | None = Field(default=None, ge=1, description="结束行")
    note: str | None = Field(default=None, description="该引用支撑的事实摘要")


class ReportDraft(BaseModel):
    """AI 草稿层（M3-1 默认由 finding.ai_analysis 投影——provenance 标注）。"""

    summary: str = Field(description="报告摘要")
    vulnerability_narrative: str = Field(description="漏洞叙述")
    exploit_scenario: str = Field(description="利用场景描述")
    evidence_refs: list[EvidencePointer] = Field(default_factory=list, description="可回查证据引用")
    confidence_tier: Literal["low", "medium", "high"] = Field(description="置信等级")
    analysis_complete: bool = Field(description="分析是否完整")


class PoCSkeleton(BaseModel):
    """PoC 骨架（零可执行产物——仅步骤与命令骨架文本）。"""

    component_kind: str = Field(description="入口组件类型（activity/service/...）")
    kind: PocKind = Field(description="骨架类型")
    steps: list[str] = Field(default_factory=list, description="操作步骤说明")
    command_skeleton: list[str] = Field(
        default_factory=list,
        description="命令骨架文本（全占位符 <PACKAGE>/<ACTION>/<EXTRA_KEY>——非可执行）",
    )
    notes: list[str] = Field(default_factory=list, description="使用注意（授权设备/占位符替换/非可执行声明）")
    executable_files_created: list[str] = Field(
        default_factory=list,
        description="生成的可执行文件清单（恒空——allow_executable_poc=False 的机器断言锚点）",
    )

    @field_validator("executable_files_created")
    @classmethod
    def _must_stay_empty(cls, value: list[str]) -> list[str]:
        """schema 级强制（评审 R-6）：恒空不能只靠生成器约定——M3-2 换
        provider 后 AI 输出直接构造 PoCSkeleton 也无法绕过。"""
        if value:
            raise ValueError(
                "零可执行产物承诺：executable_files_created 必须为空列表")
        return value


class RepairDraft(BaseModel):
    """修复建议（确定性映射与 AI 建议分列）。"""

    deterministic_recommendations: list[str] = Field(
        default_factory=list, description="按规则/组件类型的确定性建议映射")
    ai_recommendations: list[str] = Field(
        default_factory=list, description="AI 补充建议（M3-1 投影阶段为空或来自 L2 复核）")
    ai_rationale: str | None = Field(default=None, description="AI 建议依据")


class ReportDocument(BaseModel):
    """服务层合并产物（AI 草稿与确定性证据分开展示的结构载体）。

    落盘 ``run_dir/reports/drafts/{finding_id}.json``；M3-2 接入真实
    prompt 协议时仅替换 provider 实现，本结构零改动。
    """

    finding_id: str = Field(description="finding 稳定 ID")
    run_id: str = Field(description="run ID")
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    evidence_source: EvidenceSource = Field(
        description="证据来源（rule_candidate/explorer_candidate——报告须标注）")
    explorer_caveat: str | None = Field(
        default=None, description="探索候选置信度告警（仅 explorer 来源注入）")
    deterministic: dict[str, Any] = Field(
        default_factory=dict, description="确定性投影（sources/sinks/severity 等——原样不改写）")
    ai_draft: dict[str, Any] = Field(
        default_factory=dict,
        description="AI 草稿（summary/narrative/exploit_scenario/confidence_tier/provenance/prompt_version/model）",
    )
    poc_skeleton: PoCSkeleton = Field(description="PoC 骨架")
    repair: RepairDraft = Field(description="修复建议")
