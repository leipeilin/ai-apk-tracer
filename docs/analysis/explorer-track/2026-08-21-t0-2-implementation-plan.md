# 任务实施方案：T0.2（ExplorerCandidate Schema）

> **任务编号**：T0.2
> **日期**：2026-08-21
> **依据大纲**：
> - 方案：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan.md` §5.3（探索候选 Schema 草案）、§2.5（归一化映射）
> - 实施计划：`docs/analysis/explorer-track/2026-08-21-explorer-track-implementation-plan.md` T0.2
> **状态**：起草
> **前置依赖**：T0.1（复用 `ChainProposal` 等模型）

---

## 1. 任务目标与范围

- **目标**：定义探索轨**编排层候选** `ExplorerCandidate`（由 `explorer.py` 从每轮 `ExplorerObservation.chain_proposals` 转换生成，供三档校验与后续归一化为正式 candidate）。
- **范围**：
  - `ai_models.py` 新增 `ExplorerCandidateComponent` / `ExplorerCandidateValidation` / `ExplorerCandidate`；
  - 注册 `AI_SCHEMA_MODELS` 生成 `schemas/explorer_candidate.schema.json`；
  - 新增校验测试。
- **非范围**：
  - 探索 Agent prompt（T2.5）、`explorer.py` 转换逻辑（T2.5）；
  - 三档校验实现（`explorer_validation.py`，T2.6）；
  - "ExplorerCandidate → Candidate 归一化映射表"（T0.6）。

## 2. 现状锚点

- 复用 T0.1（已提交 `34a3daa`）：`StrictAIModel` / `ChainProposal` / `MethodId` / `ShortText` / `Identifier` / `LongText` / `RelativePath`。
- 归一化目标 `schemas/candidate.schema.json`（required 10 项，`additionalProperties: true` 的宽松规则候选 schema）——本任务不产出归一化映射（T0.6），但组件 `kind` 枚举对齐其 `component` 枚举（activity/service/provider/receiver）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/analysis/ai_models.py` | 修改 | 新增 3 个模型 + 注册 `AI_SCHEMA_MODELS` 一条 |
| `schemas/explorer_candidate.schema.json` | 新增（生成） | `sync-ai-protocol.py --write` 生成 |
| `backend/tests/test_ai_models.py` | 修改 | 新增 ExplorerCandidate 校验用例 |

> **注册决策**：`ExplorerCandidate` 是**编排层产物**（非 AI 协议输入输出），但注册进 `AI_SCHEMA_MODELS`（key `explorer_candidate.schema.json`，不带 `ai_` 前缀）以复用既有生成机制（`sync-ai-protocol.py --write`）与免费获得 `test_committed_schemas_exactly_match_stable_model_generation` 一致性测试。该 key 不在 `prompts/registry.yaml` 声明，`sync-ai-protocol.py` 对未注册 prompt 的 schema 不做强校验，无冲突。模型 docstring 注明"编排层产物"防止语义混淆。

### 3.2 模型定义（字段级）

```python
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
```

### 3.3 生成与校验流程

1. `ai_models.py` 添加 3 模型并注册；
2. `scripts/sync-ai-protocol.py --write` 生成 schema 并刷新 registry 摘要；
3. `--check` 确认无 drift；
4. 补 `test_ai_models.py` 用例并全量回归。

### 3.4 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性说明 |
|---|---|---|
| 方案 §5.3 草案 `component`（kind/name/exported/entry_method） | `ExplorerCandidateComponent` 四字段 | 与草案一致 |
| 方案 §5.3 草案 `candidate_id: expl_<20hex>` | pattern `^expl_[0-9a-f]{20}$` | 细化约束 |
| 方案 §5.3 草案 `source: explorer_agent` / `prompt_version` / `model` / `api_entry_ref` | Literal + ShortText + Identifier | 一致 |
| 方案 §5.3 草案 `chain_proposal`（含 hops/hypothesis） | 复用 T0.1 `ChainProposal` | 复用，避免重复定义 |
| 方案 §5.3 草案 `validation: null` | `ExplorerCandidateValidation \| None`（含三档+pending/custom_sink/guard 标记） | 细化占位结构，语义不变 |
| 方案 §2.5 归一化（candidate 10 项 required） | 本任务不涉及映射（T0.6），仅 `kind` 枚举对齐 | 边界保持 |
| 评审 R-6：复用字段映射责任 | `chain_proposal.confidence`→`confidence_tier`、`impact_proposal`→`severity_hint`、`hops`→`sources/sinks`、`validation`→`blocking_gaps` 等映射**归 T0.6**，本任务仅承载原始事实 | 映射责任显式归属 T0.6 |

### 3.5 错误处理

- 校验失败由 `StrictAIModel` 既有机制抛 `ValidationError`；
- `candidate_id` 不合规（非 `expl_` 前缀/长度错）由 pattern 拒绝；
- `validation` 为空时下游（T2.6）以 `pending` 语义处理（本任务仅类型占位）。

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| 注册进 `AI_SCHEMA_MODELS` 引发语义混淆（非 AI 协议） | 维护者误解 | docstring 显式注明"编排层产物，非 AI 协议"；key 不带 `ai_` 前缀区分 | 若评审反对，改手写 schema 并加一致性测试 |
| `kind` 含 `other` 与 candidate.component 枚举（4 项）不对齐 | 归一化时映射缺口 | 映射表（T0.6）处理 `other`；本任务保留 `other` 兜底（组件可能是非标准入口） | 归一化时降级 |

## 5. 依赖

- 前置：T0.1（已提交）；复用 `ChainProposal` / `StrictAIModel` 等。
