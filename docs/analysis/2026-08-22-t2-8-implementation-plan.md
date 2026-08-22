# 任务实施方案：T2.8 explorer_deep_dive 实现

> **任务编号**：T2.8
> **日期**：2026-08-22
> **依据大纲**：`docs/analysis/2026-08-18-project-optimization-plan.md` §2.4（deep_dive 协议：输入 partial 候选 + 缺失事实清单，输出可回查证据，禁止改写链；预算归属复核账本）、§2.5/§5.4（partial → 深挖或人工高优，不直接进正式 finding）、§4.4/决断 1、§5.5 配置（`max_requests_per_candidate=4`、`deep_dive_prompt_version`）
> **状态**：已闭合（评审 R-1~R-9 全部采纳，见 `2026-08-22-t2-8-review.md` 处置记录）
> **前置依赖**：T2.7 ✅（2b8e986：funnel `explorer_partial` 分流位、explorer 阶段在 funnel 前）；T0.3 ✅（DeepDiveInput/DeepDiveOutput 模型 + schema + prompt `explorer-deep-dive/1.0.0` + registry 注册 + `AI_OUTPUT_MODEL_REGISTRY` 已含 DeepDiveOutput）

---

## 1. 任务目标与范围

- **目标**：`partially_validated` 探索候选送 `explorer_deep_dive` 深挖（占复核预算），产出**回查通过的证据与事实判定**；L2 复核独立裁决不受影响（深挖产物不进 funnel 主链）。
- **范围（in scope）**：
  1. analyzer 协议入口（`ai.py`）：`deep_dive_entry(model_input: DeepDiveInput) -> dict`（复用 `_invoke_prompt` 状态机）；
  2. 驱动层（`explorer.py` 扩展）：`deep_dive_partials(candidates)`——missing_facts 确定性生成、code_context 确定性组装、多轮深挖循环（`max_requests_per_candidate` 上限）、**证据回查过滤**、结果写候选（`deep_dive` 字段，不改 hops 不改三档）；
  3. 模型扩展（`ai_models.py`）：`ExplorerCandidateDeepDive` + `DeepDiveRoundRecord`，`ExplorerCandidate.deep_dive` 可选字段；schema 同步（`sync-ai-protocol.py --write`）；
  4. orchestrator 集成（`_run_explorer_stage`）：深挖预算回调（run 级共享池）+ `deep_dive_counts` stage summary + 记账分列 `deep_dive_requests_used`；
  5. 测试：协议入口 / 深挖驱动 / 链不可变（M2 验收点）/ 证据回查 / 预算与容错 / 集成。
- **非范围（out of scope）**：
  - **深挖后档位升级**（partial→validated 自动升级）：不做（边界决策 D1，见 §3.7）；
  - 前端人工队列展示（T2.10）；
  - L2 复核路由变更（T2.11/T2.12 核验 agent）；
  - `unverified` 候选深挖（仅 partial 送深挖——方案 §2.4/§5.3 明确 partial 是深挖对象；unverified 留人工队列）。

## 2. 现状锚点

- **协议层（T0.3 全就绪）**：`DeepDiveInput{candidate_id, chain_proposal, missing_facts≤32, existing_evidence_refs≤64, code_context≤10000}`、`DeepDiveOutput{summary, resolved_facts≤32, evidence_refs≤64, remaining_gaps≤32, analysis_complete}`（`ResolvedFact{claim_index, conclusion∈{confirmed,refuted,still_unknown}, evidence, reasoning}`）；prompt `explorer-deep-dive/1.0.0` 硬约束（不改链/不裁决/证据可回查/逐项作答/不完整诚实）；registry 已注册。
- **驱动先例**：`explorer.py` `_explore_entry` 轮循环（ExplorerInput 构造 → `ai_call` 回调 → 解析 → read_requests 本地执行 → 上下文累积 → 终止）；`ai_requests_used`/`read_requests_used` 记账；`_MAX_CONTEXT_BYTES_PER_REQUEST=8KB` 截断；轮记录含 `model_input_hash` 审计。
- **analyzer 先例**：`ai.py:431` `explore_entry` → `_invoke_prompt("explorer", "1.0.0", model_input, ExplorerObservation, "explorer")`；`_invoke_prompt` 状态机（render→cache→budget→transport→strict-parse→repair）对 deep_dive 零改动复用；`RepairInput.target_output_model` 枚举已含 `DeepDiveOutput`（T2.5a 扩展）。
- **校验（T2.6）**：`validation.failed_hop_indices`（partial 的失败跳明细）、`blocked_by_guard`、notes——missing_facts 生成素材；跳回查规则（`call_sites` 的 `(method_id, start_line)` → `resolved_target_id` 匹配）。
- **预算（T2.7 R-1 模式）**：run 级 `max_requests_per_run` 共享池（`_ai_budget_lock` + `_ai_requests_used`，不重置）；`ExplorerSettings.max_requests_per_candidate=4`（"单探索候选的 AI 请求上限"——deep_dive 消费）。
- **配置**：`explorer.deep_dive_prompt_version="explorer-deep-dive/1.0.0"`（config.py:192）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/analysis/ai.py` | 修改 | 新增 `deep_dive_entry`（`_invoke_prompt` 复用） |
| `backend/app/analysis/ai_models.py` | 修改 | `DeepDiveRoundRecord` + `ExplorerCandidateDeepDive`；`ExplorerCandidate.deep_dive` 可选字段 |
| `schemas/explorer_candidate.schema.json` | 重新生成 | `--write`（新 `$defs`） |
| `backend/app/analysis/explorer.py` | 修改 | `deep_dive_partials` + 私有辅助（missing_facts / context 组装 / 证据回查 / 轮循环）；构造函数加 `deep_dive_call` 可选参数 |
| `backend/app/analysis/orchestrator.py` | 修改 | `_run_explorer_stage`：深挖预算回调 + 调用 + `deep_dive_counts` summary + `deep_dive_requests_used` 记账 |
| `backend/tests/test_explorer.py` | 修改 | deep_dive 驱动测试（FakeDeepDiveAI）+ 集成测试扩展 |

### 3.2 接口/数据结构设计

```python
# ai.py
async def deep_dive_entry(self, model_input: DeepDiveInput) -> dict[str, Any]:
    """单轮深挖协议执行（T2.8）：与 explore_entry 同模式（复用状态机）。

    DeepDiveOutput 无 analysis_complete 兼容性障碍（字段同名但语义为
    "深挖已完整结束"——cache 判据沿用 no-op 放弃，对齐 explore_entry 注记）。
    """
    unavailable = self._analysis_unavailable_result()
    if unavailable is not None:
        return unavailable
    return await self._invoke_prompt(
        "explorer-deep-dive", "1.0.0", model_input, DeepDiveOutput, "explorer-deep-dive",
    )

# ai_models.py
class DeepDiveRoundRecord(StrictAIModel):
    """深挖轮审计记录（对齐 T2.5b 轮审计模式：输入哈希锚定可复现）。"""
    round_index: int = Field(ge=1)
    model_input_hash: Sha256
    prompt_version: ShortText
    model: ShortText
    status: Literal["completed", "error", "skipped", "output_invalid"]
    output: DeepDiveOutput | None = None   # 全量（审计）；失败轮为 None

class ExplorerCandidateDeepDive(StrictAIModel):
    """深挖结果（T2.8）：回查后的证据与事实判定。

    铁律：不改变 chain_proposal（含 hops）与 validation 三档（M2 验收
    4.3-5.4：深挖后 hops 不变，仅新增证据）；深挖产物留人工队列（T2.10），
    不进 funnel 主链（L2 复核独立裁决不受影响）。
    """
    status: Literal["completed", "incomplete", "failed", "skipped"]
    prompt_version: ShortText
    model: ShortText
    requests_used: int = Field(ge=0)
    resolved_facts: list[ResolvedFact] = Field(default_factory=list, max_length=32)
    evidence_refs: list[ExplorerEvidenceRef] = Field(default_factory=list, max_length=64)
    remaining_gaps: list[LongText] = Field(default_factory=list, max_length=32)
    unverifiable_evidence_count: int = Field(ge=0)
    evidence_truncated_count: int = Field(default=0, ge=0, description="跨轮累积超出 schema 上界被截断的证据数（评审 R-3）")
    rounds: list[DeepDiveRoundRecord] = Field(default_factory=list, max_length=16)

# ExplorerCandidate 追加：
deep_dive: ExplorerCandidateDeepDive | None = Field(default=None, description="T2.8 深挖结果；未深挖为 None")
```

```python
# explorer.py
class ExplorerOrchestrator:
    def __init__(self, ai_call, call_tree, settings, run_dir,
                 deep_dive_call: Callable[[DeepDiveInput], Awaitable[dict]] | None = None):
        ...
        self._deep_dive_call = deep_dive_call
        self._deep_dive_requests_used = 0

    @property
    def deep_dive_requests_used(self) -> int: ...

    async def deep_dive_partials(self, candidates: list[dict], reader: Any) -> dict[str, int]:
        """对 partially_validated 候选执行深挖（原地写 candidate["deep_dive"]）。

        reader 为索引只读句柄（评审 R-1：证据回查需 files 表——对齐
        validate_explorer_candidates 参数先例，禁止穿透 call_tree 私有成员）。
        返回 {partial_total, attempted, completed, incomplete, failed,
        skipped, requests_used, unverifiable_evidence_dropped}。
        deep_dive_call 未注入（None）时全 skipped（计数返回，不抛）。
        """
```

### 3.3 算法/流程要点

**深挖单候选循环**（`_deep_dive_one`）：

```text
输入：partial 候选（validation.status == "partially_validated" 且 hops 非空）
missing_facts ← 确定性生成（§3.3.1）
evidence_pool ← chain_proposal.evidence_refs 经 §3.3.3 回查过滤的存活项
                （评审 R-9：初始池显式定义；不可回查项计入过滤计数）
context_pool ← 空方法体集合
max_rounds ← min(max_requests_per_candidate, 16)（评审 R-3：schema rounds 上界钳制）
for round_index in 1..max_rounds:
    code_context ← §3.3.2 组装（首轮：failed hops 方法体；后续轮：追加
                   前轮回查通过 evidence 所在方法体 + 末跳 to 方法的 callees）
    DeepDiveInput{candidate_id, chain_proposal（原样只读）, missing_facts,
                  existing_evidence_refs=evidence_pool, code_context}
    → deep_dive_call → DeepDiveOutput 解析
    ├─ status != completed → failed/skipped 终态；熔断类（circuit_breaking/
    │   skipped）置批次短路标志——剩余 partial 候选批量 skipped（评审 R-5：
    │   不再组装上下文/调用 AI，对齐 explore_all 短路语义）
    ├─ 证据回查过滤（§3.3.3）：本轮 evidence_refs + resolved_facts[].evidence
    │   逐条回查；不可回查的丢弃 + unverifiable_evidence_count 累积；
    │   evidence_pool 按 (path, line, end_line) 跨轮去重，超出 64 上界
    │   截断 + evidence_truncated_count 计数（评审 R-3）
    ├─ resolved 合并：后轮同 claim_index 覆盖前轮（模型可修正判定）
    ├─ 轮记录追加（model_input_hash + 全量 output）
    ├─ analysis_complete=True → status=completed 终止
    └─ 停滞（评审 R-2）：round_index ≥ 2 且连续两轮无新增 confirmed/refuted
       判定 → status=incomplete 终止（首轮不判停滞——第 2 轮有扩展上下文机会）
预算尽 → status=incomplete
```

**§3.3.1 missing_facts 确定性生成**（素材=T2.6 校验缺口）：
- 每个失败跳（`failed_hop_indices[i]`，索引界内）→ `"第 {i} 跳调用关系待证实：{from_method_id} 第 {call_site_line} 行未命中指向 {to_method_id} 的 resolved 调用边"`；
- `blocked_by_guard=True` → `"入口可达性：该入口在 release 包是否被 debuggable guard 阻断"`；
- 截断 32 项（schema max；超出丢弃并计数入 notes 性质字段——不扩 schema，计入 `remaining_gaps` 首项说明）。

**§3.3.2 code_context 确定性组装**：
- 首轮：失败跳的 `from_method_id`/`to_method_id` 方法体（`call_tree.get_method_body`，method_id 缺 "#" 或不存在 → 跳过）；
- 后续轮：追加前轮回查通过 evidence 的 `(path, line)` 所在方法体（`search_symbol`/文件行定位——简化：`get_method_body` 不支持按行定位，改用 files 表行切片：`(path, line-40, line+40)` 有界窗口）+ 末跳 `to_method_id` 的 `get_callees` 结果；
- 每段 JSON 序列化 8KB 截断（复用 `_MAX_CONTEXT_BYTES_PER_REQUEST`）；总量 ≤ 9500 字符（schema 10000 上界留余量，超出截断保留前序段）；
- **门禁**（评审 R-4）：`explorer.allow_external_code=False` 时 code_context=None——**探索主循环同步补同门禁**（`_explore_entry` 的 read_requests 结果注入 code_context 前检查；T2.5b 既有缺口补齐，同轨口径统一；注意与 `ai.allow_external_code`（AI 层全局熔断，ai.py:884）语义区分，本处用 explorer 键）。

**§3.3.3 证据回查**（确定性，T2.6 同源信任）：
- `path` 在 `files` 表存在（尝试原样与剥离 `sources/` 前缀两种形态，对齐归一化口径）；
- `line` 为 None → 仅文件存在即通过；`line`/`end_line` 均须落在文件总行数内且 `line ≤ end_line`；
- 不满足 → 丢弃该 evidence + `unverifiable_evidence_count += 1`；
- **fact 结论保留**（confirmed/refuted/still_unknown 不因 evidence 过滤降级——结论可信度交人工复核；丢弃仅作用于证据本身，可审计计数）。

**预算与记账**：
- run 级共享池：`deep_dive_call` 回调由 orchestrator 以 `_ai_budget_lock` + `_ai_requests_used >= max_requests_per_run` 包装（T2.7 R-1 同模式：检查+自增+调用 `analyzer.deep_dive_entry`）；耗尽返回 `{"status": "skipped", "circuit_breaking": True, ...}` → 该候选 `deep_dive.status="skipped"`（并触发 R-5 批次短路）；
- 分账：`ExplorerOrchestrator.deep_dive_requests_used`（探索检索 `ai_requests_used` / 深挖 `deep_dive_requests_used` 分列；run 级总账仍 `_ai_requests_used`）；
- **三本账导出公式**（评审 R-7）：探索检索账 = explorer stage 的 `ai_requests_used`；深挖账 = explorer stage 的 `deep_dive_requests_used`；复核账 = ai_analysis summary 的 `ai_stage_requests_used + deep_dive_requests_used`（deep_dive 调用计入复核账——方案 §2.4"深挖与 L2 复核占复核预算"）。ai_analysis 的 `explorer_requests_used` 快照语义=进入 AI 阶段时探索轨总消耗（检索+深挖），注释澄清防误读。

**orchestrator 集成**（`_run_explorer_stage`，validate 之后 save_candidates 之前）：

```python
async def budgeted_deep_dive_call(model_input: DeepDiveInput) -> dict[str, Any]:
    async with self._ai_budget_lock:
        if self._ai_requests_used >= budget.max_requests_per_run:
            return {"status": "skipped", "circuit_breaking": True,
                    "metadata": {"reason": "run_request_budget_exhausted"}}
        self._ai_requests_used += 1
    return await self.ai.deep_dive_entry(model_input)

orchestrator = ExplorerOrchestrator(
    budgeted_ai_call, call_tree, explorer_settings, run_dir, budgeted_deep_dive_call
)
candidates = await orchestrator.explore_all(effective)
validation_counts = validate_explorer_candidates(candidates, reader, ...)
deep_dive_counts = await orchestrator.deep_dive_partials(candidates, reader)  # T2.8（R-1：reader 参数；reader 存活期内）
orchestrator.save_candidates(candidates)                                     # 含 deep_dive 字段落盘
normalized_candidates, normalization_counts = normalize_explorer_candidates(candidates)
# stage summary += {"deep_dive_counts": deep_dive_counts,
#                   "deep_dive_requests_used": orchestrator.deep_dive_requests_used}
```

（`_run_explorer_stage` 唯一生产调用点已在 `explorer.enabled` 门内——集成处无需重复门控，评审 R-6。）

**容错边界**：
- 单候选深挖异常（构造/解析/回查）→ `deep_dive.status="failed"` + LOGGER.warning，不中断批次（T2.6 同模式）；
- **批次短路**（评审 R-5）：深挖调用返回熔断类结果（`circuit_breaking` 或 `skipped`）→ 剩余 partial 候选批量 `deep_dive.status="skipped"`（不组装上下文、不调用 AI，对齐 `explore_all` 短路语义）；
- `deep_dive_call=None`（测试直驱或未注入）→ 全体 skipped + 计数返回；
- DeepDiveOutput 解析失败（模型违反 schema）→ status="failed"（轮记录 `output_invalid`）——repair 状态机已在 `_invoke_prompt` 内兜底一轮，此处为最终失败。

### 3.4 与大纲一致性对照

| 大纲条目（引用） | 本方案实现方式 | 一致性说明 |
|---|---|---|
| §2.4 深挖：输入 partial 候选 + 缺失事实清单，输出可回查证据，禁止改写链 | `DeepDiveInput`（T0.3 冻结）+ 驱动层不改 `chain_proposal`/`hops`/`validation`；证据回查过滤 | 不变 |
| §2.4 深挖=补齐事实，L2=独立裁决 | 深挖产物只写 `candidate.deep_dive`，不进 funnel、不产生正式候选、不触发 L2 | 不变（天然隔离——归一化只取 validated） |
| §2.4 预算归属：深挖占"复核预算"，分开统计 | run 级共享池（T2.7 R-1 模式）+ `deep_dive_requests_used` 分账 + stage summary 分列 | 记账分列一致；run 级总帽共享为 T2.7 已定架构 |
| §2.4 配置 `max_requests_per_candidate: 4` | 每候选深挖 AI 调用上限（多轮循环终止条件之一） | 不变 |
| §5.3 partial → 深挖或人工高优，不直接进正式 finding | 深挖不升级档位、不归一化；结果留 `candidates.json`（T2.10 人工队列高优素材） | 不变（D1） |
| M2 验收 4.3-5.4：深挖后 hops 不变仅新增证据 | 测试断言：deep copy 前后 `chain_proposal` 逐字节相等 + 仅新增 `deep_dive` 字段 | 不变 |
| 实施计划 T2.8 行 | 全覆盖（协议入口/驱动/预算/隔离） | 不变 |

### 3.5 设计论证：深挖为何不自动升级档位（D1）

- 三档是**跳回查的确定性结论**（call_sites 表回查）；深挖的 `resolved_facts.conclusion` 是 **AI 判定**（低信任输入原则 §2.2：探索输出必须经确定性门禁）；
- M2 验收"深挖后链结构未被改写（仅新增证据）"——升级 validated 意味着归一化进 funnel 主链，等价于让 AI 判定改变确定性门禁结论，违反"低信任输入，高信任输出"；
- 行号修正路径（深挖 evidence 指向正确调用点 → 确定性重查）受"不改 hops"约束不可实现（修正 `call_site_line` 即改写链）；
- 深挖的真实价值：为 T2.10 人工队列高优条目补齐可回查证据 + 逐项事实判定，降低人工复核成本——这正是方案 §5.3"深挖或人工高优"的语义。

### 3.6 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| R-1 深挖消耗 run 级共享预算，挤占规则候选 L2 复核（T2.7 风险延续） | 复核预算减少 | 深挖对象只有 partial（探索候选的子集）+ 每候选 ≤4 调用有界；`deep_dive_requests_used` 分账可审计；M2 验收成本口径实测 | 探索轨整体 `explorer.enabled=false` 一键回退（深挖随阶段关闭） |
| R-2 模型输出臆造证据（不可回查） | 证据污染人工队列 | 逐条回查过滤 + `unverifiable_evidence_count` 计数（不静默丢弃）；fact 结论保留但 evidence 已过滤，人工可辨 | 过滤逻辑独立函数，可收紧（如要求 evidence 落在方法体行界内） |
| R-3 多轮深挖停滞判定误伤（首轮全 still_unknown 但第二轮能突破） | 深挖提前终止 | 停滞判定=round_index≥2 且**连续两轮**无新增 confirmed/refuted（评审 R-2：首轮不判停滞，第 2 轮有扩展上下文机会）；预算 4 轮兜底 | 参数化（stagnation 判定阈值）——本任务不扩配置，行为测试固化 |
| R-4 candidates.json 体积膨胀（轮记录含全量 output） | 产物可读性 | resolved_facts ≤32/evidence ≤64/rounds ≤4（schema 上界）；每轮 output 受 max_output_tokens 限制 | 轮记录改存摘要（去掉 output 全量）——schema 兼容（字段可选化） |
| R-5 schema 再生成引发既有测试漂移（explorer_candidate.schema.json $defs 变化） | 回归 | `--write` 后跑全量；既有测试对 $defs 无计数断言（T2.6 经验） | git revert schema + 模型字段 |

### 3.7 边界决策记录

| 编号 | 决策 | 理由 | 状态 |
|---|---|---|---|
| D1 | 深挖**不自动升级** partial→validated（不归一化进 funnel） | §3.5 论证：AI 判定不得改变确定性门禁结论；M2 验收"hops 不变仅新增证据" | 待评审确认 |
| D2 | `unverified` 候选**不送深挖**（仅 partial） | 方案 §2.4/§5.3：深挖对象=partial；unverified 零跳通过（无可锚定的链事实，深挖输入 `chain_proposal` 不可信），留人工队列 | 按方案执行 |
| D3 | 证据回查标准=文件存在 + 行界（非 call_sites 调用边） | 深挖证据语义="源码位置"（代码事实存在）而非"调用边存在"；调用边回查属 T2.6 跳回查职责 | 待评审确认 |
| D4 | fact 结论不因 evidence 过滤降级 | 结论+reasoning 是模型判断的完整记录（人工复核素材）；静默降级会丢失模型观点；丢弃仅作用于不可回查的证据 | 待评审确认 |

## 4. 依赖

- 前置任务：T2.7（2b8e986）、T0.3（协议层）、T2.6（校验缺口素材）、T2.5b（驱动模式）
- 工具链：`scripts/sync-ai-protocol.py --write`（schema 再生成 + registry 哈希门禁）
