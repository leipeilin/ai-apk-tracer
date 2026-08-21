"""严格定义 AI 各阶段的输入、输出与可审计追踪模型。"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Annotated, ClassVar, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)]
Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/#@+\-]*$"),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def _require_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("path 必须是无父目录跳转的 POSIX 相对路径")
    return value


RelativePath = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1_024),
    AfterValidator(_require_relative_path),
]

# 探索轨（Agent1）的 method_id 是"低信任建议"：格式正确性（path#Class.method:line 可回查）
# 由探索轨三档校验层（explorer_validation，T2.6）的 call_sites 回查判定，不在 schema 层做
# 严格 pattern 前置，避免 LLM 输出带签名/构造器/泛型/内部类写法时频繁校验失败。
MethodId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]


class StrictAIModel(BaseModel):
    """所有 AI 边界模型共享的严格校验策略。"""

    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


class EvidenceReference(StrictAIModel):
    """引用输入切片中可回查的确定性证据。"""

    context_id: Identifier = Field(description="被引用输入上下文的稳定 ID")
    path: RelativePath | None = Field(default=None, description="证据所在的工作区相对路径；缺省时仅按 context_id 回查")
    line: int | None = Field(default=None, ge=1, le=10_000_000, description="证据起始行；必须落在引用上下文范围内")
    end_line: int | None = Field(default=None, ge=1, le=10_000_000, description="证据结束行；缺省时表示单行或整个上下文")
    claim: LongText = Field(description="该引用直接支持的、可回查的具体主张")


class ContextRequest(StrictAIModel):
    """请求补充一个有界且可解析的上下文目标。"""

    type: Literal["method", "class", "component", "callers", "callees", "file_symbols"] = Field(description="编排器可解析的扩片目标类型")
    target: ShortText = Field(description="要补充的符号、组件或调用关系目标")
    path: RelativePath | None = Field(default=None, description="用于消歧目标的工作区相对路径")
    line: int | None = Field(default=None, ge=1, le=10_000_000, description="用于消歧目标的源码行号")
    reason: LongText = Field(description="当前结论为何需要这份额外上下文")


class ExploitabilityAssessment(StrictAIModel):
    """vuln-judgment-prompt §3：可利用性逐项评估。"""

    entry_reachable: bool = Field(description="攻击者入口是否可达（组件 exported/隐式 intent 可触发）")
    propagation_proven: bool = Field(description="攻击者输入是否沿同值/同对象/key-slot 传播到 Sink")
    sink_effective: bool = Field(description="Sink 是否真实执行了敏感操作而非空操作")
    guard_bypassed: bool = Field(description="是否存在且被绕过的 Guard；无 Guard 时为 False")
    authorization_absent: bool = Field(description="是否存在权限/签名级授权保护；无保护时为 True")
    exfiltration_channel: Literal["confirmed", "unverified", "absent"] = Field(
        description="执行结果回到攻击者的跨进程通道状态；静态无法证明时用 unverified"
    )


class HarmAssessment(StrictAIModel):
    """vuln-judgment-prompt §3：危害影响评估。"""

    impact_type: Literal["data_disclosure", "data_tamper", "dos", "privilege_escalation", "device_control", "financial", "other"] = Field(
        description="危害类型；无法具体描述时用 other 并说明"
    )
    impact_target: ShortText = Field(description="受影响的资产/数据/能力范围描述")
    server_confirmation_required: bool = Field(
        default=False, description="影响是否依赖服务端/硬件/动态确认，静态阶段无法定论"
    )


class ImpactVector(StrictAIModel):
    """vuln-judgment-prompt §3：CVSS 因子级描述（AI 不输出分数，分数由确定性映射器计算）。"""

    confidentiality: Literal["none", "partial", "total"] = Field(description="机密性影响")
    integrity: Literal["none", "partial", "total"] = Field(description="完整性影响")
    availability: Literal["none", "partial", "total"] = Field(description="可用性影响")
    privileges_required: Literal["none", "low", "high"] = Field(description="利用所需前置权限")
    attack_complexity: Literal["low", "high"] = Field(description="利用复杂度")
    user_interaction: Literal["none", "required"] = Field(description="是否需要用户交互")


class BlockingGap(StrictAIModel):
    """阻止模型形成可靠结论的证据缺口。"""

    code: Identifier = Field(description="可稳定聚合的证据缺口代码")
    message: LongText = Field(description="缺少何种事实以及它如何阻止结论")
    critical: bool = Field(description="该缺口是否足以阻止形成支持或反驳裁决")
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=32, description="用于界定缺口的已知证据引用")


class Uncertainty(StrictAIModel):
    """模型必须显式披露的不确定性。"""

    topic: ShortText = Field(description="不确定性所涉及的语义主题")
    reason: LongText = Field(description="无法由当前输入消除不确定性的原因")
    impact: Literal["low", "medium", "high"] = Field(description="不确定性对裁决可靠性的影响等级")
    resolvable: bool = Field(description="追加受控上下文是否可能消除该不确定性")


class DeterministicFact(StrictAIModel):
    """由确定性分析产生、允许模型引用但不得改写的事实。"""

    fact_type: Identifier = Field(description="确定性分析事实的稳定类别")
    statement: LongText = Field(description="模型只可引用、不得改写的事实陈述")
    evidence_refs: list[EvidenceReference] = Field(min_length=1, max_length=32, description="生成该事实时已回查的证据引用")


class DeterministicSemanticBundle(StrictAIModel):
    """传给模型的规范化确定性语义包。"""

    candidate: dict[str, JsonValue] = Field(description="待分析候选的规范化确定性字段")
    contexts: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=256, description="预算内、带稳定 context_id 的方法或文件上下文切片")
    manifest_facts: list[DeterministicFact] = Field(default_factory=list, max_length=128, description="由 Manifest 解析器确认的不可改写事实")
    semantic_facts: list[DeterministicFact] = Field(default_factory=list, max_length=256, description="由规则和索引确认的不可改写语义事实")
    blocking_gaps: list[BlockingGap] = Field(default_factory=list, max_length=64, description="进入本轮分析前已知的阻断性覆盖缺口")
    uncertainties: list[Uncertainty] = Field(default_factory=list, max_length=64, description="确定性阶段已识别但尚未消除的不确定性")


class PreflightInput(StrictAIModel):
    """验证服务是否能遵守严格 JSON 输出协议的最小输入。"""

    provider_kind: Literal["openai-compatible"] = Field(description="待预检的模型服务协议类型")
    model: ShortText = Field(description="服务端模型 ID")
    response_format: Literal["json_object", "json_schema"] = Field(description="本次预检要求服务遵守的结构化输出模式")
    required_capabilities: list[ShortText] = Field(min_length=1, max_length=16, description="服务必须逐项确认的严格协议能力")


class PreflightOutput(StrictAIModel):
    """AI 服务能力预检结果。"""

    ok: bool = Field(description="服务是否确认能够遵守全部要求")
    message: LongText = Field(description="对预检结果的简明说明")
    acknowledged_capabilities: list[ShortText] = Field(default_factory=list, max_length=16, description="服务明确确认的协议能力")
    analysis_complete: bool = Field(description="响应是否完整完成本阶段；false 不得视为已接受输出")


class ProposedEvidence(StrictAIModel):
    """L1 提议的 Source、Sink 或 Guard 位置，后续必须确定性回查。"""

    context_id: Identifier = Field(description="提议位置所属输入上下文的稳定 ID")
    path: RelativePath = Field(description="提议位置的工作区相对路径")
    line: int = Field(ge=1, le=10_000_000, description="待确定性回查的源码行号")
    kind: Identifier = Field(description="提议证据的 Source、Sink、Guard 等语义类别")
    symbol: ShortText = Field(description="提议位置对应的方法或字段符号")
    reason: LongText = Field(description="为何该位置可能参与候选链路")


class ProposedPath(StrictAIModel):
    """L1 提议的 Source 到 Sink 路径索引。"""

    source_ref: int = Field(ge=0, le=255, description="suggested_sources 中提议 Source 的零基索引")
    sink_ref: int = Field(ge=0, le=255, description="suggested_sinks 中提议 Sink 的零基索引")
    method_ids: list[Identifier] = Field(default_factory=list, max_length=128, description="按调用顺序排列的候选方法链")
    reason: LongText = Field(description="该 Source 到 Sink 路径为何值得确定性闭链")


class L1TriageInput(StrictAIModel):
    """L1 暴露候选的 AI 分诊输入。"""

    semantic_bundle: DeterministicSemanticBundle = Field(description="本轮允许模型使用的候选事实与上下文切片")
    round: int = Field(default=0, ge=0, le=16, description="当前候选的零基分析轮次")
    previous_output: dict[str, JsonValue] | None = Field(default=None, description="上一轮严格输出；仅用于修正或继续分析")


class L1TriageOutput(StrictAIModel):
    """L1 分诊提议；不得直接成为正式漏洞证据。"""

    summary: LongText = Field(description="基于当前确定性输入的 L1 分诊摘要")
    triage_disposition: Literal["potential_chain", "exposure_only", "insufficient"] = Field(description="潜在链路、仅暴露或证据不足的分诊结果；不是最终漏洞裁决")
    suggested_sources: list[ProposedEvidence] = Field(default_factory=list, max_length=64, description="待后续确定性回查的 Source 提议")
    suggested_sinks: list[ProposedEvidence] = Field(default_factory=list, max_length=64, description="待后续确定性回查的 Sink 提议")
    suggested_paths: list[ProposedPath] = Field(default_factory=list, max_length=64, description="连接已提议 Source 与 Sink 的候选路径")
    guard_observations: list[ProposedEvidence] = Field(default_factory=list, max_length=64, description="输入切片中观察到的潜在 Guard 位置")
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=128, description="支撑摘要和分诊结果且可回查到输入上下文的引用")
    blocking_gaps: list[BlockingGap] = Field(default_factory=list, max_length=64, description="阻止本轮形成更强分诊结果的证据缺口")
    uncertainties: list[Uncertainty] = Field(default_factory=list, max_length=64, description="即使结束本轮仍需披露的不确定性")
    context_requests: list[ContextRequest] = Field(default_factory=list, max_length=32, description="仅当 analysis_complete=false 时请求的有界补充上下文")
    analysis_complete: bool = Field(description="当前 L1 阶段是否无需额外上下文即可结束；不代表漏洞成立")


class L2ReviewInput(StrictAIModel):
    """经确定性闭链后的 L2 深度复核输入。"""

    semantic_bundle: DeterministicSemanticBundle = Field(description="经确定性闭链后允许模型复核的事实与上下文")
    round: int = Field(default=0, ge=0, le=16, description="当前候选的零基复核轮次")
    l1_triage: L1TriageOutput | None = Field(default=None, description="可选的已校验 L1 分诊输出；不作为确定性证据")
    previous_output: dict[str, JsonValue] | None = Field(default=None, description="上一轮严格 L2 输出；仅用于继续复核")


class L2ReviewOutput(StrictAIModel):
    """L2 对候选的严格证据裁决。"""

    summary: LongText = Field(description="对确定性闭链候选的证据复核摘要")
    verdict: Literal["supports_candidate", "refutes_candidate", "unresolved"] = Field(description="当前证据支持、反驳或不足以解决候选的裁决；不得输出其他枚举")
    confidence_tier: Literal["low", "medium", "high"] = Field(description="裁决受当前证据支撑的置信等级")
    guard_status: Literal["absent", "present_effective", "present_bypassable", "present_partial", "unknown"] = Field(description="已观察 Guard 对候选链路的实际约束状态")
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=128, description="直接支撑 verdict 与 guard_status 的输入证据引用")
    blocking_gaps: list[BlockingGap] = Field(default_factory=list, max_length=64, description="阻止形成确定裁决的证据缺口")
    uncertainties: list[Uncertainty] = Field(default_factory=list, max_length=64, description="裁决必须同时披露的剩余不确定性")
    context_requests: list[ContextRequest] = Field(default_factory=list, max_length=32, description="仅在尚未结束复核时请求的有界补充上下文")
    flaw_holds: bool = Field(description="缺陷是否成立：存在真实调用点的缺陷，非 import/注释/声明/共现")
    exploitability: ExploitabilityAssessment = Field(description="可利用性逐项评估（§3）")
    harm: HarmAssessment = Field(description="危害影响评估（§3）")
    reachability_class: Literal["remote", "local", "supply_chain", "device"] = Field(description="可达性分级（§5）")
    impact_vector: ImpactVector = Field(description="CVSS 因子级描述；不得输出数值分数")
    reverse_exclusion: list[str] = Field(
        default_factory=list, max_length=32,
        description="逐项对照反向排除红线清单（§4），说明为何不算漏洞或为何排除；supports_candidate 时须给出",
    )
    confidence_rationale: str = Field(
        default="", min_length=0, max_length=2000,
        description="为既有 confidence_tier 补充的一句理由；不改动 confidence_tier 本身",
    )
    refutation_basis: list[
        Literal[
            "non_exported_provider",
            "fixed_local_target",
            "constant_sink_argument",
            "in_process_terminus",
            "no_real_call_site",
            "guard_fail_closed",
        ]
    ] = Field(
        default_factory=list, max_length=8,
        description=(
            "refutes_candidate 的静态确定性反证依据；每项必须与 candidate.deterministic_facts "
            "一致，决策层会逐项交叉验证，不一致或事实缺失即不予采信"
        ),
    )
    analysis_complete: bool = Field(description="当前 L2 阶段是否无需额外上下文即可结束；与 verdict 值相互独立")


class ExplorerEvidenceRef(StrictAIModel):
    """探索轨轻量证据引用（低信任）：仅指向可回查的源码位置。

    不复用 EvidenceReference（其 context_id/claim 必填，属确定性语义 bundle 的输入上下文
    引用）；探索轨证据由 T2.6 三档校验回查通过后归一化为正式证据。
    """

    path: RelativePath = Field(description="证据所在工作区相对路径（必填，可回查）")
    line: int | None = Field(default=None, ge=1, le=10_000_000, description="证据起始行")
    end_line: int | None = Field(default=None, ge=1, le=10_000_000, description="证据结束行；缺省表示单行")
    claim: LongText | None = Field(default=None, description="可选：该引用支撑的主张（供人工视图；校验后补全）")


class Hop(StrictAIModel):
    """数据流链上的一跳（结构化路径，评审 §4.2）。"""

    from_method_id: MethodId = Field(description="源方法 ID 建议（path#Class.method:line；可回查性由 T2.6 校验）")
    to_method_id: MethodId = Field(description="目标方法 ID 建议")
    call_site_line: int = Field(ge=1, le=10_000_000, description="调用点源码行号")
    arg_positions: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=list, max_length=32, description="攻击者可控参数位置（从 0 起，非负）"
    )
    resolved_via: Literal["direct_call", "virtual_call", "dynamic_invoke", "binder_transaction", "other"] = Field(
        description="调用解析方式"
    )


class ChainProposal(StrictAIModel):
    """从攻击面入口到 sink 的候选链（低信任建议，非正式 sources/sinks）。"""

    source: ShortText = Field(description="候选 source 表达式/方法")
    sink: ShortText = Field(description="候选 sink 方法/操作")
    hops: list[Hop] = Field(min_length=1, max_length=32, description="结构化逐跳路径；每跳须可对 call_sites 表回查")
    call_tree_refs: list[RelativePath] = Field(default_factory=list, max_length=16, description="可选：支撑该链的 call_tree 产物相对路径")
    evidence_refs: list[ExplorerEvidenceRef] = Field(default_factory=list, max_length=64, description="支撑本链的轻量证据引用（T2.6 回查后归一化）")
    confidence: Literal["low", "medium", "high"] = Field(description="模型对本链成立度的置信度")
    hypothesis: Literal["likely", "possible", "unlikely"] = Field(description="假设（非裁决）：是否倾向构成漏洞，评审 §4.1")
    impact_proposal: LongText = Field(description="影响面/攻击场景/漏洞类型描述（假设级，非结论）")
    reasoning: LongText = Field(description="构造本链的依据")
    needs_expansion: bool = Field(default=False, description="本链是否需进一步扩片取证")


class ReadRequest(StrictAIModel):
    """探索循环中的结构化读码请求。

    仅暴露四种检索操作（评审 R-4 决策）：入口来自确定性 api_entry_table/attack_surface
    （属信任边界，不让 Agent 自由枚举入口）；class_hierarchy / resolve_invoke_target 为
    call_tree 内部实现细节，不对模型暴露。
    """

    operation: Literal["get_method_body", "get_callees", "get_callers", "search_symbol"] = Field(description="call_tree 服务可执行操作")
    target: ShortText = Field(description="目标符号/方法/类名")
    path: RelativePath | None = Field(default=None, description="消歧用工作区相对路径")
    line: int | None = Field(default=None, ge=1, le=10_000_000, description="消歧用源码行号")
    reason: LongText = Field(description="为什么需要这份代码/调用关系")


class ComponentSummary(StrictAIModel):
    """对当前入口组件/代码的功能描述。"""

    component: ShortText = Field(description="组件类名")
    kind: Literal["activity", "service", "provider", "receiver", "other"] = Field(description="组件类型")
    exported: bool = Field(description="是否导出（可从外部触发）")
    summary: LongText = Field(description="组件/代码功能描述")


class ExplorerLoopState(StrictAIModel):
    """探索循环轮末状态（评审 §4.3：终止由代码判定，模型只声明意图）。"""

    done: bool = Field(description="是否已形成完整 sink 链、可结束循环")
    reason: ShortText = Field(description="结束或继续的原因说明（必填，便于审计）")


class ExplorerObservation(StrictAIModel):
    """探索 Agent（Agent1）单轮输出：低信任建议链 + 读码请求（方案 §2.4）。"""

    read_requests: list[ReadRequest] = Field(default_factory=list, max_length=8, description="本轮的读码请求")
    chain_proposals: list[ChainProposal] = Field(default_factory=list, max_length=8, description="本轮的候选链（低信任）")
    component_summary: ComponentSummary = Field(
        description="组件/代码功能描述（每轮绑定一个入口组件，attack_surface 保证可总结，故必填）"
    )
    loop: ExplorerLoopState = Field(description="循环状态")

    @model_validator(mode="after")
    def _done_requires_chain(self) -> ExplorerObservation:
        if self.loop.done and not self.chain_proposals:
            raise ValueError("loop.done=True 必须伴随至少一条 chain_proposal（评审 R-3）")
        return self


class ExplorerCandidateComponent(StrictAIModel):
    """探索候选的入口组件信息（大纲 §5.3 草案；entry_method 供归一化 locations）。"""

    kind: Literal["activity", "service", "provider", "receiver", "other"] = Field(description="组件类型（与 candidate.component 枚举对齐，含 other 兜底）")
    name: ShortText = Field(description="组件类名")
    exported: bool = Field(description="是否导出（可从外部触发）")
    entry_method: ShortText = Field(description="组件入口方法名（如 onCreate）")


class ExplorerCandidateValidation(StrictAIModel):
    """三档校验结果占位（T2.6 填充；生成时默认 None 即 pending）。

    判定规则（评审 R-4）：validated=全部跳 call_sites 回查通过；
    partially_validated=至少一跳可回查但链/证据不完整；unverified=引用不可回查或信息不足。
    """

    status: Literal["pending", "validated", "partially_validated", "unverified"] = Field(
        description="三档校验状态；判定规则见类 docstring"
    )
    notes: LongText | None = Field(default=None, description="校验结论/缺口说明")
    verified_hop_count: int | None = Field(default=None, ge=0, description="逐跳回查通过的跳数")
    failed_hop_indices: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=list, max_length=32, description="回查失败的跳索引（评审 R-2 明细载体，供审计）"
    )
    blocked_by_guard: bool = Field(default=False, description="是否被 Guard/授权确定性阻断")
    custom_sink_proposal: bool = Field(default=False, description="sink 未命中现有 taxonomy，标记为 custom sink 提案（评审 §4.5）")


class ExplorerCandidate(StrictAIModel):
    """探索轨编排层候选（非 AI 协议产物）：由每轮 Observation.chain_proposals 转换生成。

    - prompt_version / model 沿用产生该候选的 Observation 元数据透传（评审 R-3）；
    - 转换侧（T2.5）不得注入 extra 字段（评审 R-8）；
    - 归一化目标为 schemas/candidate.schema.json：顶层 sources/sinks/blocking_gaps 等
      由 chain_proposal + validation 映射生成（T0.6，评审 R-5/R-6）；本模型只承载
      探索轨原始候选事实，不直接写正式 sources/sinks。
    """

    schema_version: Literal["1.0.0"] = Field(description="ExplorerCandidate Schema 版本")
    candidate_id: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^expl_[0-9a-f]{20}$")] = Field(
        description="候选稳定 ID（expl_ + 20 位 hex）"
    )
    source: Literal["explorer_agent"] = Field(description="候选来源（低信任探索轨）")
    prompt_version: ShortText = Field(description="产生该候选的探索协议版本（如 explorer/1.0.0）")
    model: ShortText = Field(description="产生该候选的模型标识")
    component: ExplorerCandidateComponent = Field(description="入口组件信息")
    api_entry_ref: Identifier = Field(description="关联的 API 入口表条目 ID（如 act_..._onCreate）")
    chain_proposal: ChainProposal = Field(description="候选链（复用 T0.1 ChainProposal，低信任建议）")
    validation: ExplorerCandidateValidation | None = Field(default=None, description="三档校验结果；生成时为空占位")


class RepairInput(StrictAIModel):
    """只携带无效输出与校验错误的格式修复输入。"""

    target_output_model: Literal["PreflightOutput", "L1TriageOutput", "L2ReviewOutput", "FinalizationOutput"] = Field(description="必须恢复到的严格输出模型名称")
    invalid_output: JsonValue = Field(description="待做格式修复的原始解析结果；不得据此重新分析事实")
    validation_errors: list[LongText] = Field(min_length=1, max_length=64, description="目标模型校验失败的精简错误列表")
    output_schema_sha256: Sha256 = Field(description="目标输出 Schema 原始字节的 SHA-256")


class RepairOutput(StrictAIModel):
    """格式修复阶段返回的单个候选 JSON 对象。"""

    repaired_output: dict[str, JsonValue] = Field(description="仅修正结构和类型后的目标输出对象")
    analysis_complete: bool = Field(description="格式修复是否已完整结束；不表示重新完成语义分析")


class FinalizationInput(StrictAIModel):
    """合并确定性事实与最后一轮 AI 复核结果的输入。"""

    semantic_bundle: DeterministicSemanticBundle = Field(description="最终建议必须服从的确定性事实与上下文")
    l1_triage: L1TriageOutput | None = Field(default=None, description="L1 候选的最后一份严格分诊输出")
    l2_review: L2ReviewOutput | None = Field(default=None, description="L2 候选的最后一份严格复核输出")


class FinalizationOutput(StrictAIModel):
    """供编排层消费但仍需策略校验的最终 AI 建议。"""

    summary: LongText = Field(description="合并确定性事实与最后一轮严格输出后的最终摘要")
    verdict: Literal["supports_candidate", "refutes_candidate", "unresolved"] = Field(description="沿用证据语义的最终建议裁决，不直接写入人工 review_status")
    review_recommendation: Literal["pending_ai", "pending_manual", "ai_false_positive"] = Field(description="供编排层消费的复核建议，而非授权模型直接改变审核状态")
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=128, description="最终建议引用的输入证据；不得新增上游未提供的证据")
    blocking_gaps: list[BlockingGap] = Field(default_factory=list, max_length=64, description="最终建议仍需保留的阻断性缺口")
    uncertainties: list[Uncertainty] = Field(default_factory=list, max_length=64, description="最终建议仍需保留的不确定性")
    analysis_complete: bool = Field(description="finalization 是否完整结束；不得用来掩盖 unresolved 或缺口")


class AITraceEntry(StrictAIModel):
    """记录一次可复现 AI 调用所需的不可变摘要。"""

    prompt_id: Literal["preflight", "l1-triage", "l2-review", "repair", "finalization"] = Field(description="本次调用使用的 Prompt ID")
    prompt_version: Identifier = Field(description="精确加载且不允许回退的 Prompt 版本")
    analysis_track: Literal["preflight", "l1_triage", "l2_review", "repair", "finalization"] = Field(description="调用所属的分析阶段")
    round: int = Field(ge=0, le=16, description="候选在该分析阶段中的轮次")
    model: ShortText = Field(description="模型服务端使用的模型 ID")
    status: Literal["completed", "incomplete", "failed", "skipped"] = Field(description="本次逻辑调用的终态")
    system_template_sha256: Sha256 = Field(description="system 模板原始字节的 SHA-256")
    user_template_sha256: Sha256 = Field(description="user 模板原始字节的 SHA-256")
    system_rendered_sha256: Sha256 = Field(description="变量渲染后 system 文本 UTF-8 字节的 SHA-256")
    user_rendered_sha256: Sha256 = Field(description="变量渲染后 user 文本 UTF-8 字节的 SHA-256")
    input_schema_sha256: Sha256 = Field(description="输入 Schema 原始字节的 SHA-256")
    output_schema_sha256: Sha256 = Field(description="输出 Schema 原始字节的 SHA-256")
    analysis_complete: bool = Field(description="被记录输出是否完整结束对应分析阶段")


class AICacheDescriptor(StrictAIModel):
    """只含不可逆摘要和版本元数据的 AI 缓存身份描述。"""

    descriptor_version: Literal["1"] = Field(description="缓存身份描述符格式版本")
    provider_kind: Identifier = Field(description="模型服务协议类型")
    base_url_hash: Sha256 = Field(description="规范化模型端点的不可逆摘要")
    model: ShortText = Field(description="产生缓存输出的模型 ID")
    analyzer_version: Identifier = Field(description="分析器实现版本")
    prompt_id: Identifier = Field(description="产生输出的 Prompt ID")
    prompt_version: Identifier = Field(description="产生输出的精确 Prompt 版本")
    system_template_hash: Sha256 = Field(description="system 模板原始字节摘要")
    user_template_hash: Sha256 = Field(description="user 模板原始字节摘要")
    input_schema_hash: Sha256 = Field(description="输入 Schema 原始字节摘要")
    output_schema_hash: Sha256 = Field(description="输出 Schema 原始字节摘要")
    model_input_hash: Sha256 = Field(description="规范化模型输入对象摘要")
    input_slice_hash: Sha256 | None = Field(default=None, description="原始上下文切片摘要")
    request_hash: Sha256 = Field(description="实际模型请求载荷摘要")
    output_model_name: Identifier | None = Field(default=None, description="用于校验缓存输出的严格模型名称")
    output_model_version: Identifier | None = Field(default=None, description="严格输出模型的协议版本")
    protocol_version: Identifier | None = Field(default=None, description="结构化输出协议版本")
    analysis_track: Identifier | None = Field(default=None, description="缓存输出所属分析阶段")
    scope_hash: Sha256 | None = Field(default=None, description="候选稳定作用域摘要")
    fact_hash: Sha256 | None = Field(default=None, description="候选确定性事实摘要")
    context_hash: Sha256 | None = Field(default=None, description="参与分析的上下文摘要")
    prompt_hash: Sha256 | None = Field(default=None, description="组合 Prompt 模板身份摘要")
    schema_hash: Sha256 | None = Field(default=None, description="组合输入输出 Schema 身份摘要")
    temperature: float | None = Field(default=None, description="请求中影响模型输出的 temperature")
    max_output_tokens: int | None = Field(default=None, ge=1, description="请求中发送的最大输出 token 数")
    budget_policy_hash: Sha256 | None = Field(default=None, description="影响请求与缓存复用的预算策略摘要")

    @model_validator(mode="after")
    def require_complete_output_model_identity(self) -> AICacheDescriptor:
        if (self.output_model_name is None) != (self.output_model_version is None):
            raise ValueError("output model name/version 必须同时存在或同时缺省")
        return self


class AICacheEntry(StrictAIModel):
    """任务本地 AI 缓存的严格、版本化持久化记录。"""

    schema_version: Literal["1"] = Field(description="缓存持久化记录格式版本")
    descriptor: AICacheDescriptor = Field(description="决定缓存是否可复用的完整身份描述")
    accepted_output: dict[str, JsonValue] = Field(description="已通过严格输出模型校验的 JSON 对象")
    accepted_output_hash: Sha256 = Field(description="规范化 accepted_output 的 SHA-256")
    created_at: AwareDatetime = Field(description="首次写入该缓存记录的带时区时间")
    updated_at: AwareDatetime = Field(description="最近更新该缓存记录的带时区时间")

    @model_validator(mode="after")
    def require_ordered_timestamps(self) -> AICacheEntry:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不得早于 created_at")
        return self


AI_OUTPUT_MODEL_REGISTRY: dict[str, type[StrictAIModel]] = {
    model.__name__: model
    for model in (
        PreflightOutput,
        L1TriageOutput,
        L2ReviewOutput,
        RepairOutput,
        FinalizationOutput,
    )
}
AI_OUTPUT_MODEL_VERSIONS: dict[str, str] = {
    model_name: "1" for model_name in AI_OUTPUT_MODEL_REGISTRY
}


def get_ai_output_model(model_name: str, model_version: str) -> type[StrictAIModel] | None:
    """按精确名称和语义版本解析当前允许的严格输出模型。"""

    if AI_OUTPUT_MODEL_VERSIONS.get(model_name) != model_version:
        return None
    return AI_OUTPUT_MODEL_REGISTRY.get(model_name)


AI_MODEL_REGISTRY: dict[str, type[StrictAIModel]] = {
    model.__name__: model
    for model in (
        PreflightInput,
        PreflightOutput,
        L1TriageInput,
        L1TriageOutput,
        L2ReviewInput,
        L2ReviewOutput,
        RepairInput,
        RepairOutput,
        FinalizationInput,
        FinalizationOutput,
        DeterministicSemanticBundle,
        AITraceEntry,
    )
}

AI_SCHEMA_MODELS: dict[str, type[StrictAIModel]] = {
    "ai_preflight_input.schema.json": PreflightInput,
    "ai_preflight_output.schema.json": PreflightOutput,
    "ai_l1_triage_input.schema.json": L1TriageInput,
    "ai_l1_triage_output.schema.json": L1TriageOutput,
    "ai_l2_review_input.schema.json": L2ReviewInput,
    "ai_l2_review_output.schema.json": L2ReviewOutput,
    "ai_explorer_observation.schema.json": ExplorerObservation,
    "explorer_candidate.schema.json": ExplorerCandidate,
    "ai_repair_input.schema.json": RepairInput,
    "ai_repair_output.schema.json": RepairOutput,
    "ai_finalization_input.schema.json": FinalizationInput,
    "ai_finalization_output.schema.json": FinalizationOutput,
    "ai_deterministic_semantic_bundle.schema.json": DeterministicSemanticBundle,
    "ai_trace_entry.schema.json": AITraceEntry,
    "ai_cache_entry.schema.json": AICacheEntry,
}


class SchemaSerialization:
    """集中定义 committed schema 的稳定序列化规则。"""

    JSON_OPTIONS: ClassVar[dict[str, object]] = {
        "ensure_ascii": False,
        "indent": 2,
        "sort_keys": True,
    }

    @classmethod
    def bytes_for(cls, model: type[BaseModel]) -> bytes:
        document = json.dumps(model.model_json_schema(), **cls.JSON_OPTIONS)
        return (document + "\n").encode("utf-8")

    @classmethod
    def sha256_for(cls, model: type[BaseModel]) -> str:
        return hashlib.sha256(cls.bytes_for(model)).hexdigest()
