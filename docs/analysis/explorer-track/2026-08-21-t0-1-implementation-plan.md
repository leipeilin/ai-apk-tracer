# 任务实施方案：T0.1（ExplorerObservation Schema）

> **任务编号**：T0.1
> **日期**：2026-08-21
> **依据大纲**：
> - 方案：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan.md` §2.4（探索 Agent 协议）、§5.3（探索候选 Schema 草案）
> - 评审：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan-review.md` §4.2（hops 结构）、§4.3（循环状态机）、§5.4（每轮协议草案）
> - 实施计划：`docs/analysis/explorer-track/2026-08-21-explorer-track-implementation-plan.md` T0.1
> **状态**：起草
> **前置依赖**：无（独立任务）

---

## 1. 任务目标与范围

- **目标**：定义探索 Agent（Agent1）单轮输出的 strict Schema `ExplorerObservation`，并纳入仓库既有 AI Schema 生成/校验体系（Pydantic 模型 → `sync-ai-protocol.py` 生成 schema 文件）。
- **范围（in scope）**：
  - 在 `backend/app/analysis/ai_models.py` 新增 `ExplorerObservation` 及子模型（`Hop` / `ChainProposal` / `ReadRequest` / `ComponentSummary` / `ExplorerLoopState`）；
  - 注册进 `AI_SCHEMA_MODELS`，生成 `schemas/ai_explorer_observation.schema.json`；
  - 新增模型校验测试。
- **非范围（out of scope）**：
  - 探索 Agent prompt（`prompts/explorer/1.0.0/`，属 T2.5）；
  - `ExplorerCandidate` Schema（T0.2）；
  - `explorer.py` 循环驱动实现（T2.5）；
  - `call_tree.py` 服务（T2.4）。

## 2. 现状锚点

- **AI 边界模型**：`backend/app/analysis/ai_models.py`——`StrictAIModel`（`extra="forbid", strict=True, validate_default=True`）为所有边界模型的基类；`EvidenceReference` / `ContextRequest` / `BlockingGap` 等通用子结构可直接复用。
- **Schema 生成**：`AI_SCHEMA_MODELS`（L407）注册「schema 文件名 → Pydantic 模型」；`scripts/sync-ai-protocol.py --write` 从模型生成 schema 文件并更新 `prompts/registry.yaml` 摘要，`--check` 校验一致性。
- **命名约定**：AI 协议 schema 统一 `ai_*.schema.json` 前缀（`ai_l1_triage_output` / `ai_l2_review_input` 等）。
- **测试**：`backend/tests/test_ai_models.py` 以模型直接验证（有效输出 / 缺必填 / 多余字段 / 错误枚举）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/analysis/ai_models.py` | 修改 | 新增 6 个模型 + 注册 `AI_SCHEMA_MODELS` 一条 |
| `schemas/ai_explorer_observation.schema.json` | 新增（生成） | 由 `sync-ai-protocol.py --write` 生成 |
| `backend/tests/test_ai_models.py` | 修改 | 新增 ExplorerObservation 校验用例（有效/必填/额外字段/枚举/hops） |
| `prompts/registry.yaml` | 修改（自动） | `--write` 会刷新摘要；本任务不新增协议条目，仅确保不产生 drift |

> 文件名说明：实施计划 T0.1 字面写 `schemas/explorer_observation.schema.json`，此处按仓库既有 AI Schema 约定注册为 `ai_explorer_observation.schema.json`（`ai_` 前缀是本仓库 AI 协议 schema 的统一命名，`sync-ai-protocol.py` 与 registry 均依赖该约定）。属实现细节细化，不改变大纲定义的对象语义；如评审有异议按评审流程处理。

### 3.2 模型定义（字段级）

```python
# —— 新增类型（置于模块既有类型区，紧邻 Identifier 定义；评审 R-2）——
# 探索轨的 method_id 是"低信任建议"：格式正确性（path#Class.method:line 可回查）
# 由 T2.6 三档校验层的 call_sites 回查判定，不在 schema 层做严格 pattern 前置，
# 避免 LLM 输出带签名/构造器/泛型/内部类写法时频繁校验失败。
MethodId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]

class ExplorerEvidenceRef(StrictAIModel):
    """探索轨轻量证据引用（低信任）：仅指向可回查的源码位置。

    评审 R-1/R-8：不复用 EvidenceReference（其 context_id/claim 必填，属确定性语义
    bundle 的输入上下文引用）；探索轨证据由 T2.6 回查通过后归一化为正式证据。
    """

    path: RelativePath = Field(description="证据所在工作区相对路径（必填，可回查）")
    line: int | None = Field(default=None, ge=1, le=10_000_000, description="证据起始行")
    end_line: int | None = Field(default=None, ge=1, le=10_000_000, description="证据结束行；缺省表示单行")
    claim: LongText | None = Field(default=None, description="可选：该引用支撑的主张（供人工视图；T2.6 校验后补全）")

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
    reason: ShortText = Field(description="结束或继续的原因说明（必填，便于审计；评审 R-7）")

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
```

**注册**：`AI_SCHEMA_MODELS["ai_explorer_observation.schema.json"] = ExplorerObservation`。

**设计决策说明**：
- 不加 `analysis_complete` 字段：本协议终止信号唯一化为 `loop.done`（大纲 §2.4 与评审 §4.3 均以 `loop.done` 表达终止），避免与仓库其他协议双通道不一致；
- `chain_proposals` / `read_requests` 允许空数组（首轮可能无链、只有读码请求）；
- 枚举严格限定（`confidence`/`hypothesis`/`resolved_via`/`operation`/`kind`），延续仓库 AI 输出 strict 约定；
- `hops.min_length=1`：无 hops 的 chain 无回查基础，直接非法。

**评审修订说明（2026-08-21，第 1 轮闭合）**：
- R-1/R-8：`evidence_refs` 改用轻量 `ExplorerEvidenceRef`（path 必填、line/end_line/claim 可空），不复用 `EvidenceReference`（context_id/claim 必填语义不适用低信任探索轨）；T2.6 回查后归一化为正式证据。
- R-2：`Hop.from/to_method_id` 改用 `MethodId`（max_length=512，无严格 pattern）；格式正确性由 T2.6 `call_sites` 回查判定，不做 AI 侧严格前置。
- R-3：`ExplorerObservation` 增加 `model_validator(after)`——`loop.done=True` 必须伴随至少一条 `chain_proposal`。
- R-4：`ReadRequest` 仅暴露四种检索操作属有意裁剪，已在 docstring 文档化理由（入口属确定性信任边界；class_hierarchy/resolve_invoke_target 为内部细节）。
- R-7：`ExplorerLoopState.reason` 改为必填（审计价值）；`component_summary` 保持必填（每轮绑定入口组件，attack_surface 保证可总结）。

### 3.3 生成与校验流程

1. 在 `ai_models.py` 添加模型并注册；
2. 运行 `scripts/sync-ai-protocol.py --write` 生成 schema 文件并刷新 registry 摘要；
3. 运行 `scripts/sync-ai-protocol.py --check` 确认无 drift；
4. 补充 `test_ai_models.py` 用例并全量跑测试。

### 3.4 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性说明 |
|---|---|---|
| 方案 §2.4 chain_proposals 含 source/sink/hops/证据/置信度/hypothesis/impact_proposal/是否需扩片 | `ChainProposal` 全部字段对应 | 不变 |
| 方案 §2.4 read_requests（get_method_body/get_callees/get_callers/search_symbol） | `ReadRequest.operation` 四枚举 | 不变 |
| 方案 §2.4 component_summary：组件功能描述 | `ComponentSummary`（component/kind/exported/summary） | 细化字段结构，语义不变 |
| 方案 §2.4 loop:{done} 循环状态机 | `ExplorerLoopState{done, reason}` | 增加 reason 便于审计，done 语义不变 |
| 评审 §4.2 hops 逐跳结构 | `Hop` 五字段（from/to/line/arg_positions/resolved_via） | 与评审建议一致 |
| 评审 §4.1 hypothesis/impact_proposal 假设非裁决 | 枚举 `likely/possible/unlikely` + 描述字段 | 与评审建议一致 |
| 评审 §5.4 每轮协议草案 | 输出四元素（read_requests/chain_proposals/component_summary/loop） | 一致 |

### 3.5 错误处理

- 模型校验失败由既有 Pydantic strict 机制抛 `ValidationError`，沿用仓库 `repair` 协议兜底路径（本任务不涉及，仅说明兼容性）；
- `sync-ai-protocol.py --check` 检出 drift 时按既有脚本退出码 1 处理，跑 `--write` 修复。

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| 枚举过严导致探索 Agent 输出频繁校验失败 | 探索轨候选产出率低 | 枚举贴近 call_tree 能力边界（四种操作）与常见调用形态；`resolved_via` 含 `other` 兜底 | 放宽枚举需走评审修订 |
| 文件名与任务表字面不一致 | 追溯混淆 | 方案文档显式记录命名决策（`ai_` 前缀约定） | 如评审要求可改回字面名并同步 registry 约定 |
| schema 生成影响 registry 摘要 | 无关 drift | `--write` 统一刷新，`--check` 验收把关 | `git checkout` 还原 registry.yaml |

## 5. 依赖

- 无前置任务；复用 `ai_models.py` 既有类型（`StrictAIModel`/`Identifier`/`ShortText`/`LongText`/`RelativePath`/`EvidenceReference`）。
