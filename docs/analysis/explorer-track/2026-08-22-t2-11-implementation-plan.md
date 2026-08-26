# 任务实施方案：T2.11 核验 agent（verify_agent）

> **任务编号**：T2.11
> **日期**：2026-08-22
> **依据大纲**：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan.md` §2.7（核验 Agent：L2 agent 化演进——命题清单/盲验防锚定/受控取证循环/降级回退/三本账）；2026-08-21 决断 3
> **状态**：已闭合（评审 R-1~R-10 全部采纳，见 `2026-08-22-t2-11-review.md` 处置记录）
> **前置依赖**：T2.7 ✅（探索归一化候选形状）；T0.9 ✅（VerifyInput/VerifyOutput 协议层全就绪：模型/schema/prompt `verify/1.0.0`/registry/`RepairInput` 枚举已含 VerifyOutput）；T2.5b ✅（受控循环驱动模式）；T2.8 ✅（证据回查过滤模式）

---

## 1. 任务目标与范围

- **目标**：实现核验 agent 主体——`backend/app/analysis/verify_agent.py`：命题清单生成器（确定性代码从候选 sources/sinks/Guard 事实与探索 hops 生成）+ 盲验输入构造（剥离假设层）+ 受控取证循环（终止条件=命题全部判定，非模型自声明）；输出整体 observation 对齐 L2 关键决策字段（verdict/flaw_holds/exploitability/refutation_basis），供 T2.12 分流与适配层消费。
- **范围（in scope）**：
  1. analyzer 协议入口（`ai.py`）：`verify_entry(model_input: VerifyInput) -> dict`；
  2. `verify_agent.py` 新模块：`build_verify_claims` / `build_deterministic_facts` / `build_chain_facts`（纯函数，可单测）+ `VerifyAgent`（受控取证循环 + 聚合 + 一致性校验 + 落盘审计）；
  3. `explorer.py`：`_dispatch_read` 提升为模块级 `dispatch_read`（VerifyAgent 复用，单一实现防漂移）；
  4. 测试 `test_verify_agent.py`。
- **非范围（out of scope）**：
  - **分流与降级编排**（T2.12）：探索 validated 必进核验、规则 L2 替代、`fallback_to_single_turn_l2` 回退、funnel 路由、核验预算第三本账的 orchestrator 接线；
  - **适配层**（T2.12）：VerifyOutput → L2 其余字段（harm/reachability_class/impact_vector/reverse_exclusion/guard_status）补齐 + evidence_refs 类型转换（`ExplorerEvidenceRef` → `EvidenceReference`，context_id 回填）；
  - orchestrator/AI 阶段集成（T2.12）；前端展示。

## 2. 现状锚点

- **协议层（T0.9 全就绪）**：`VerifyInput{candidate_id, claims[1..32], chain_facts?, evidence_refs≤64, deterministic_facts≤64, code_context≤10000}`（**结构上无假设层字段**——顶层与嵌套均无，schema 冻结）；`VerifyOutput{summary, verdict∈{supports_candidate,refutes_candidate,unresolved}, confidence_tier, flaw_holds, exploitability(复用 L2 六字段模型), refutation_basis(6 值枚举), claims_verdicts≤32, evidence_refs≤64, read_requests≤8, loop{done,reason}, analysis_complete}`——schema 注记"整体判定须与 claims_verdicts 一致（T2.11 实现层校验）"；`VerifyClaim{index, statement, kind∈七类}`；`VerifyFact{fact_type∈七类, statement}`；`VerifyChainFacts{source, sink, hops, call_tree_refs?, evidence_refs?}`（剥离版 ChainProposal）。
- **消费锚点（DecisionEngine 不变的依据）**：`_trusted_ai_outcome`（decision.py:332-342 读 verdict）；`_ai_strong_support`（667-679：flaw_holds+exploitability 三真+high）；`_cross_validated_refutation_basis`（443-470：refutation_basis 需 deterministic_facts 交叉验证——T2.12 适配层补 context）；`_ai_contract_failure`（790-812：evidence_refs 门禁）。
- **L2 输入对照（盲验剥离对象）**：现行 L2 输入经 `_semantic_bundle`（ai.py:1169-1184）携带 candidate——其中 `_candidate_summary` 白名单（context_builder.py:943-948）**含 severity_hint/confidence_tier 语义**；verify 盲验输入结构上排除（构造层只产 facts/claims/chain_facts）。
- **analyzer 惯例**：`explore_entry`（ai.py:447-453）/`deep_dive_entry`（467-473）先例——`_invoke_prompt(prompt_id, "1.0.0", input, output, track)`；verify 用 `("verify", "1.0.0", VerifyInput, VerifyOutput, "verify")`；`_PROMPT_VERSIONS` 无 verify 条目（沿 explorer 先例硬编码，registry 哈希门禁 + `test_config` 注册对齐护栏兜底）。AITraceEntry.analysis_track 枚举不含 verify——verify 轨不写 ai-trace（同 explorer 轨，轮审计走 observations 落盘）。
- **循环先例**：`ExplorerOrchestrator._execute_read_requests`/`_dispatch_read`（explorer.py:599-627，duck-type call_tree 四操作，not_found 统一结构，8KB 截断）；`VerifyOutput.read_requests` 与 ExplorerObservation 同为 `list[ReadRequest]`——dispatch 零耦合可复用。
- **配置（T0.7 已就绪）**：`VerifySettings{enabled=false, prompt_version="verify/1.0.0", max_rounds_per_candidate=4, max_requests_per_candidate=12, fallback_to_single_turn_l2=true}`。代码外发门禁：verify 属 L2 演进，遵循主链语义（`ai.allow_external_code` 全局门禁，ai.py:908）——不引入 explorer 轨的 `explorer.allow_external_code`（语义不同：探索检索读回片段）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/analysis/ai.py` | 修改 | `verify_entry`（`_invoke_prompt` 复用） |
| `backend/app/analysis/explorer.py` | 修改 | `_dispatch_read` 提升模块级 `dispatch_read`（实例方法委托） |
| `backend/app/analysis/verify_agent.py` | 新增 | 命题生成 + 盲验构造 + `VerifyAgent` 取证循环 |
| `backend/tests/test_verify_agent.py` | 新增 | 全量测试 |

### 3.2 接口/数据结构设计

```python
# verify_agent.py

def build_verify_claims(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    """命题清单生成器（确定性——方案 §2.7：不从 Agent1 描述生成）。

    六类命题按候选确定性字段触发（§3.3.1）；索引 0 起连续；上限 32（schema）。
    """

def build_deterministic_facts(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    """盲验事实（结构化 VerifyFact——剥离 severity/confidence/hypothesis 语义）。"""

def build_chain_facts(explorer_candidate: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """探索候选剥离版链事实（投影 chain_proposal，剥离五假设字段；无则 None）。"""

class VerifyAgent:
    """核验 agent 受控取证循环（L2 agent 化演进——方案 §2.7）。

    ai_call 回调（async (VerifyInput) -> dict）由调用方注入（T2.12 接预算
    包装；测试注入 FakeAnalyzer）。reader 为索引只读句柄（评审 R-1：证据
    回查需 files 表——沿 deep_dive_partials 先例）。终止条件=命题全部判定
    （代码判定，非模型自声明 loop.done）；轮数/读码预算耗尽产出已证命题+
    缺口清单（undecided_claim_indices）并终止。
    """

    def __init__(self, ai_call, call_tree, settings: VerifySettings, run_dir: Path, reader: Any) -> None: ...

    async def verify(
        self, candidate: Mapping[str, Any],
        explorer_candidate: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """单候选核验。返回聚合结果（§3.3.3），不改写输入候选。"""

    @property
    def ai_requests_used(self) -> int: ...
    @property
    def read_requests_used(self) -> int: ...
```

**verify() 返回形状**（T2.12 消费契约）：

```python
{
    "status": "completed" | "failed" | "skipped",      # 循环执行终态
    "terminated_by": "all_claims_decided" | "round_budget" | "request_budget"
                     | "error" | "short_circuit" | "no_claims",
    "output": {...},           # 聚合 VerifyOutput（末轮整体字段 + 合并 claims_verdicts
                               #  + 跨轮去重 evidence_refs + analysis_complete=全部判定）
    "rounds": [...],           # 轮审计（model_input_hash/output 全量——对齐 T2.5b/T2.8）
    "requests_used": int, "read_requests_used": int,
    "undecided_claim_indices": [...],  # 缺口清单物化（评审 R-6：尚无判定的命题索引）
    "consistency_downgraded": bool,   # 整体判定一致性降级标记（附 note 进 output）
}
```

### 3.3 算法/流程要点

**§3.3.1 命题生成（确定性，触发条件）**：

| kind | 触发 | statement 模板（数据源全为候选确定性字段） |
|---|---|---|
| entry_reachable | component_name 存在 | 入口组件 {component_name} 的入口是否可被外部触发（exported 或隐式 intent 可达） |
| source_controllability | sources 非空 | source（{path}:{line}）的值是否攻击者可控（源自入口参数/外部输入而非硬编码常量） |
| propagation | sources 与 sinks 均非空 | 攻击者可控值是否从 source 传播到 sink（{path}:{line}，无中途净化/终止/覆盖） |
| sink_behavior | sinks 非空 | sink（{path}:{line}）是否执行真实敏感操作（非空实现、非已失效包装） |
| guard_effective | guard_status 非 unknown 或 guard_blocked 为 True | Guard 检查是否有效阻断攻击路径（release 配置下 fail-closed） |
| authorization | authorization_status 非 unknown | 权限/签名级授权是否阻止外部应用触发该组件 |

- 探索候选的 hops 以 `chain_facts` 事实层随输入提供（不在命题文本里复述——防锚定与冗余）；六类命题并列保留（共 ≤6 条，cap 32 仅畸形候选触发——评审 R-10③：guard/authorization 属否定证明方向不弱化）。
- **触发条件语义**（评审 R-10②）：guard_status/authorization_status 字段缺失（get → None）与显式 `"unknown"` 均视为"未知不触发"（实现排除集 `{None, "unknown"}`）；guard_blocked 分支为防御性保留（funnel 对 guard_blocked 候选跳过 AI——正常流程不达，评审 R-10④）。

**§3.3.2 盲验构造**：

- `deterministic_facts`：七类 fact_type 按字段存在性生成（component/reachability/guard/authorization/source/sink），statement 只含位置/状态事实，**不含 severity_hint、confidence_tier、blocking_gaps 的 severity 语义、探索 hypothesis/impact_proposal**（M2 验收 4.3-6.1 trace 断言：核验请求输入不含假设层）；
- `chain_facts`：仅当 `explorer_candidate` 提供——投影 `{source, sink, hops, call_tree_refs, evidence_refs}`，剥离 `confidence/hypothesis/impact_proposal/reasoning/needs_expansion`；**evidence_refs 投影时 claim 置 None**（评审 R-5：claim 为提出者生成文本，防锚定）；
- **初始证据池**（chain_facts 外的 candidate 证据）同样 claim 置 None 后经回查过滤；
- `code_context` 首轮**双路径**（评审 R-9）：sources/sinks 带 `method_id` 时经 `call_tree.get_method_body` 取方法体；无 method_id 时按 `path:line` 行窗口切片（files 表 ±40 行——reader 通道，deep_dive 同模式）；8KB 段截断，总量 ≤9500；后续轮由模型 `read_requests` 驱动累积。

**§3.3.3 受控取证循环**：

```text
claims ← build_verify_claims(candidate)
（评审 R-8：claims 为空 → status=skipped + terminated_by=no_claims 快速返回，
  不构造 VerifyInput——schema minItems=1 防线前移）
verdicts ← {}（claim_index → 最新判定）；evidence_pool ← claim 置 None 后回查过滤的初始证据
for round_index in 1..max_rounds_per_candidate(4):
    VerifyInput{candidate_id, claims, chain_facts, evidence_refs=evidence_pool,
                deterministic_facts, code_context(累积)}
    → ai_call → VerifyOutput 解析（失败→error/short_circuit 终态）
    ├─ read_requests 执行（dispatch_read；读码预算=max_requests_per_candidate
    │   (12) − 已用；8KB 截断注入 code_context）
    ├─ evidence 回查过滤（deep_dive 同模式：files 表存在+行界，双形态路径，
    │   不可回查丢弃计数 unverifiable_evidence_count；claim 置 None）
    ├─ claims_verdicts 合并（后轮同 index 覆盖前轮）
    ├─ 轮记录追加（model_input_hash + output 全量）
    ├─ 终止（代码判定）：verdicts 覆盖全部 claims index → all_claims_decided
    └─ 读码预算耗尽且尚有未判定命题 → request_budget 提前终止（评审 R-4：
       省空转轮——不再发起无法取证的 AI 轮）
轮数尽 → round_budget
聚合：末轮整体字段（summary/verdict/confidence_tier/flaw_holds/exploitability/
  refutation_basis）+ 合并 verdicts + evidence_pool + analysis_complete=(全部判定)
  + undecided_claim_indices（缺口清单物化——评审 R-6）
一致性校验（§3.3.4）→ 落盘 run_dir/verify/observations.json（追加）
```

- `loop.done` 仅记录（审计），**不作为终止依据**（方案 §2.7 原文；`_done_requires_verdicts` 弱校验不能替代实现层防线——评审 R-3）；
- 熔断类失败（circuit_breaking/skipped）→ `status="skipped"` + `terminated_by="short_circuit"`（单候选语义；批次级短路由 T2.12 编排层处理——本任务单候选调用无批次）。

**§3.3.4 整体判定一致性校验**（schema 注记的实现层落地）：

- 规则 1：`verdict=supports_candidate` 但 `flaw_holds=False` → 降级 unresolved；
- 规则 2：`verdict=refutes_candidate` 但 `flaw_holds=True` → 降级 unresolved；
- 规则 3：`verdict=supports_candidate` 但合并 verdicts 中存在**核心命题**（kind ∈ {entry_reachable, source_controllability, propagation, sink_behavior}）refuted → 降级 unresolved；
- **规则 4**（评审 R-2）：`verdict=supports_candidate` 但任一核心命题 still_unknown → 降级 unresolved（prompt 硬约束 3 的确定性落地——防模型虚报 supports 直通 `_ai_strong_support`）；`refutes_candidate` 无 refuted 佐证豁免（P1-5 下游 `_cross_validated_refutation_basis` fail-closed 兜底）；
- 降级动作：`output.verdict="unresolved"` + output 追加 note（`consistency_note: "整体判定与命题判定不一致，已确定性降级"`）+ `consistency_downgraded=True`（人工视图重点标记——方案 §2.7 冲突项标记语义）；claims_verdicts 保留原文（人工可辨）。

**落盘审计**：`run_dir/verify/observations.json`——`{"entries": [{candidate_id, terminated_by, requests_used, read_requests_used, rounds, ...}]}` 追加模式（对齐 explorer/observations.json；0600 权限）。

### 3.4 与大纲一致性对照

| 大纲条目（引用） | 本方案实现方式 | 一致性说明 |
|---|---|---|
| §2.7 验证任务结构化为命题清单（不从 Agent1 描述生成） | `build_verify_claims` 纯确定性模板（候选字段→命题） | 不变 |
| §2.7 盲验防锚定（剥离 hypothesis/impact_proposal/confidence/reasoning） | VerifyInput 结构冻结 + 构造层只产 facts/claims/chain_facts（剥离版） | 不变 |
| §2.7 受控取证循环（复用 explorer 模式；终止=命题全部判定非自声明；预算耗尽产出已证+缺口） | VerifyAgent 轮循环 + 代码判定终止 + `terminated_by` 三态 | 不变 |
| §2.7 输出沿用 verdict/flaw_holds/exploitability/evidence_refs，DecisionEngine 消费不变 | VerifyOutput 五字段对齐 L2（T0.9 冻结）；T2.11 只做聚合+一致性校验，DecisionEngine 改造为零 | 不变（分流/适配=T2.12） |
| §2.7 降级回退（agent 失败回退单轮 L2） | 循环失败返回 `status=failed/skipped`——回退编排在 T2.12（`fallback_to_single_turn_l2`） | 边界保持（任务级） |
| §2.7 冲突项人工视图标记 | `consistency_downgraded` + note | 不变 |
| 实施计划 T2.11 行 | prompt ✓（T0.9 骨架无需增强——硬约束已含一致性/盲验/四操作）；命题生成器/盲验构造/取证循环全覆盖 | 不变 |

### 3.5 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| R-1 命题模板过粗（模型难以判定） | still_unknown 高发 | 命题含 path:line 锚点 + chain_facts 事实层；M4 以 ai_likely_supported 占比实测（方案 §2.7 预期不归零） | 模板迭代（prompt/构造层独立演进） |
| R-2 一致性规则 3 过严（非核心命题 refuted 误伤 supports） | 整体被误降级 | 规则 3 限定核心命题 kind 四类；降级保 claims 原文可人工翻案 | 规则参数化（本任务不扩配置，测试固化） |
| R-3 多轮 evidence 池膨胀 | 输入超 schema | 跨轮 (path,line,end_line) 去重 + ≤64 截断计数（T2.8 R-3 同模式） | 同 deep_dive 回退（截断改摘要） |
| R-4 read_requests 无界消耗 | 读码预算耗尽 | requests 预算硬顶（12，VerifySettings）+ 每轮 ≤8（schema） | 预算参数已配置化 |
| R-5 verify_entry 协议失败（render/transport） | 单候选核验失败 | 循环 error 终态 + 调用方（T2.12）回退单轮 L2——主链不阻塞（降级设计前移） | `verify.enabled=false` |

### 3.6 边界决策记录

| 编号 | 决策 | 理由 | 状态 |
|---|---|---|---|
| D1 | T2.11 **不做 orchestrator/funnel 集成**（分流/预算接线/回退全在 T2.12） | 实施计划 T2.11/T2.12 任务边界（T2.12 行明列 candidate_funnel 路由与降级）；先主体后编排可独立验收 | 按计划执行 |
| D2 | 代码外发遵循 **ai.allow_external_code 全局门禁**（不引入 explorer 键） | verify=L2 演进；L2 review 切片代码外发本就受全局门禁约束——同语义不分叉 | 待评审确认 |
| D3 | `verify_entry` 版本**硬编码 "1.0.0"**（不入 `_PROMPT_VERSIONS`） | explorer/deep_dive 先例；registry 哈希门禁 + test_config 护栏兜底 | 沿先例 |
| D4 | 循环内 evidence 回查过滤（deep_dive 同模式） | 低信任输入原则（§2.2 探索输出必须回查）；核验证据同样须可回查 | 按方案执行 |

## 4. 依赖

- 前置任务：T2.7（候选形状）、T0.9（协议层）、T2.5b（循环模式）、T2.8（证据回查/dispatch 复用模式）
- 交接 T2.12：verify() 返回契约（§3.2）+ 分流编排点（_run_ai_stage）+ 适配层字段清单（harm 等 7 字段 + EvidenceReference 转换）
