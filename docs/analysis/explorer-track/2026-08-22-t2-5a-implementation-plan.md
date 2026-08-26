# 任务实施方案：T2.5a（探索 Agent 协议层：ExplorerInput/ExplorerObservation）

> **任务编号**：T2.5a（T2.5 拆分第一子任务——协议先行，"先声明后注册"原则）
> **日期**：2026-08-22
> **依据大纲**：
> - 方案：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan.md` §2.4（ExplorerObservation strict schema：chain_proposals/read_requests/component_summary/loop.done）+ §4.1/§4.3（低信任假设语义/循环状态机）
> - 实施计划 T2.5（拆分说明见 §3.4 D1）
> **状态**：起草
> **前置依赖**：T0.1（ChainProposal/Hop/ExplorerEvidenceRef 模型已交付）、T2.4（call_tree 服务——read_requests 的操作面）

---

## 1. 任务目标与范围

- **目标**：定义探索 Agent 的 AI 协议——`ExplorerInput`（每轮输入）与 `ExplorerObservation`（每轮输出 strict schema）+ prompt 骨架（`prompts/explorer/1.0.0/`）+ registry 注册（哈希门禁）。
- **范围**：`ai_models.py` 新模型 + 注册；prompt 两文件；`schemas/ai_explorer_{input,output}.schema.json`（sync 脚本生成）；registry.yaml 条目；模型/协议测试。
- **非范围**：驱动循环 explorer.py（T2.5b）；orchestrator 集成（T2.5b）；ExplorerCandidate 转换（T2.5b——T0.1 已定候选 schema）。

## 2. 现状锚点（评审 R-1 修正：既有模型已由 T0.1 交付）

- **既有探索协议模型**（ai_models L307-352，T0.1 交付）：
  - `ReadRequest`：**四操作**（get_method_body/get_callees/get_callers/search_symbol——评审 R-4 决策：resolve_invoke_target/class_hierarchy 为 call_tree 内部实现不对模型暴露）+ `target/path/line`（消歧）+ `reason`（必填审计）；
  - `ComponentSummary`：结构化（component/kind/exported/summary）；
  - `ExplorerLoopState`：done + reason（ShortText，审计必填）；
  - `ExplorerObservation`：read_requests(≤8)/chain_proposals(≤8)/component_summary（必填）/loop + `_done_requires_chain` 校验器（done=true 必须伴随至少一条链——**"无链可达"不表达为 done=true**，由驱动层预算终止承载部分链+缺口清单，评审 R-3 决断：维持校验器）；
  - `ai_explorer_observation.schema.json` 已提交并注册 `AI_SCHEMA_MODELS`。
- **本任务实际范围**：既有四模型**不动**；补齐 `ExplorerInput`（不存在——驱动层输入模型）+ prompt 骨架 + registry 条目 + 测试。
- **DeepDive 协议先例**（T0.3）：`DeepDiveInput/DeepDiveOutput` + prompt + registry——本任务同模式。
- **sync 机制**：`scripts/sync-ai-protocol.py`（条目手工登记两注册表，脚本生成哈希与 schema 文件）。
- **api_entry_table 字段**（T2.2，R-5 对齐）：六类条目异构字段（binder 有 implementation_method_id、webview 有 bridge_*、dynrcv 有 export_status/externally_reachable）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 内容摘要 |
|---|---|---|
| `backend/app/analysis/ai_models.py` | 修改 | ExplorerReadRequest/ExplorerLoopState/ExplorerEntryContext/ExplorerInput/ExplorerObservation + 双注册表 |
| `prompts/explorer/1.0.0/system.md` | 新增 | 探索器角色 + 硬约束 + 判定标准 |
| `prompts/explorer/1.0.0/user.md` | 新增 | 占位符 `explorer_input_json` |
| `prompts/registry.yaml` | 修改 | explorer 1.0.0 条目（sync 生成哈希） |
| `schemas/ai_explorer_{input,output}.schema.json` | 新增 | sync 脚本生成 |
| `backend/tests/test_explorer_protocol.py` | 新增 | 模型边界/round-trip/registry 校验 |

### 3.2 模型设计（评审 R-1 修订：既有四模型不动，仅新增 ExplorerInput）

```python
class ExplorerInput(StrictAIModel):
    """每轮输入（explorer.py 构造：上下文累积 + 预算透明）。

    entry_json/attack_surface_json 为 api_entry_table/attack_surface 条目
    的 JSON 序列化文本（六类入口字段异构——扁平文本注入优于投影模型，
    对齐 DeepDiveInput.code_context 先例；评审 R-5：投影模型会丢字段或
    大量可空）。
    """
    round_index: int = Field(ge=1, description="当前轮次（从 1 起）")
    rounds_budget: int = Field(ge=1, description="总轮数预算（max_rounds_per_entry）")
    requests_budget: int = Field(ge=0, description="剩余读码请求预算")
    entry_json: LongText = Field(description="本轮入口的 api_entry_table 条目 JSON")
    attack_surface_json: LongText | None = Field(default=None, description="入口所属组件的 attack_surface 条目 JSON（攻击面上下文——承载方案'索引摘要'语义，评审 R-7 归并说明）")
    prior_observations: LongText | None = Field(default=None, description="前轮累积摘要（component_summary + 已取回代码事实）")
    code_context: LongText | None = Field(default=None, description="本轮 read_requests 取回的代码片段/调用关系")
```

> 既有 `ExplorerObservation`（含 ReadRequest 四操作/ComponentSummary/ExplorerLoopState/`_done_requires_chain`）与 `ai_explorer_observation.schema.json` **零改动**（R-1）；ComponentSummary.exported 由模型从 entry 事实总结（低信任），T2.5b 转换层与 attack_surface 事实对照（转换层职责，R-5 处置）。

### 3.3 prompt 骨架要点

**system.md**（对齐 deep-dive 风格——硬约束 + 判定标准；评审 R-6 补防回归约束）：
1. 角色：从攻击面入口出发的受控探索器——用 read_requests 取码，形成"入口→sink"候选链；
2. 硬约束（违反即失败）：
   - 只输出建议链（chain_proposals），不得下漏洞成立结论（hypothesis 是假设非裁决——§4.1）；
   - 每跳（hop）的 from/to method_id 必须来自已见上下文（code_context/entry），不得臆造；调用点行号必须来自真实见过的代码且 ≥1；
   - 证据引用可回查（path+line 指向真实源码）；
   - component_summary 是对组件功能的客观描述（exported 依据入口事实，不评价漏洞性）；
   - **loop.done=true 必须伴随至少一条 chain_proposal**（协议校验器强制——"需更多上下文"表达为 done=false + read_requests；无链可达时维持探索直至预算由驱动层终止）；
   - 预算透明：输入含剩余轮数/请求预算——预算将尽时输出部分链（needs_expansion 标注）；
   - **必填字段一个都不得省略**（嵌套 Hop/ExplorerEvidenceRef 的 required 同样）；只能输出协议声明字段，禁止附加字段；枚举值逐一按定义取值；
3. 判定标准：hypothesis 三档（likely=链完整且 sink 敏感/possible=链大部分成立/unlikely=链断裂或不敏感）；confidence 依据（跳数/解析方式/证据密度）；read_requests 的 reason 必填（审计）。

**user.md**：`{explorer_input_json}` 占位符。

### 3.4 关键设计决策

**D1：T2.5 拆分为 a（协议）/b（驱动循环）两子任务**
- T2.5 原行含 prompt+协议+驱动+集成+落盘——单任务交付面过大（预计 1000+ 行）；协议先行是既有原则（ExplorerSettings.prompt_version 描述："先声明后注册（T2.5），注册前不得运行时解析"）；T2.5b 依赖本协议冻结后实施。

**D2：操作面维持既有四操作（评审 R-2 修订：回退 resolve_invoke_target）**
- 既有 `ReadRequest` 的评审 R-4 决策（T0.1）：resolve_invoke_target/class_hierarchy 为 call_tree 内部实现不对模型暴露（防滥用）；get_entry_points 由 explorer.py 首轮注入（entry_json）。**本方案不改操作面**——原"五操作超集"表述撤销（与方案 §2.4 一致而非超集偏离）。

**D2b：既有模型零改动（评审 R-1 决断）**
- ExplorerObservation/ReadRequest/ComponentSummary/ExplorerLoopState 及其校验器、已提交 schema 均不动——本任务只新增 ExplorerInput 并注册协议（prompt+registry）。同名冲突风险消除。

**D3：ExplorerInput 扁平文本上下文（attack_surface_context/prior_observations/code_context 为 LongText）**
- 与 DeepDiveInput.code_context 同模式（JSON 序列化文本注入）；避免深层嵌套模型的 token 膨胀；explorer.py（T2.5b）负责序列化与累积摘要。

**D4：chain_proposals/read_requests 均可空但 loop 必答**
- 末轮（done=true）可无 read_requests；首轮可无 chain_proposals——loop.done 是唯一循环控制信号（评审 §4.3 状态机）。

### 3.5 测试方案（`test_explorer_protocol.py`）

1. **test_explorer_input_round_trip**：合法 ExplorerInput（entry_json/attack_surface_json/prior/code_context + 预算）校验 + 往返；
2. **test_input_budget_boundaries**：round_index=0 / rounds_budget=0 / requests_budget=-1 拒绝（N-3）；
3. **test_observation_done_requires_chain**：既有校验器行为回归（done=true + 空 proposals 拒绝；done=false + 空提案合法——R-3 决断的回归锚定）；
4. **test_read_request_four_operations**：四操作枚举（非法 operation 拒绝 N-1；resolve_invoke_target 已在枚举外）；reason 必填；
5. **test_component_summary_structured**：ComponentSummary 结构化字段（kind 枚举/exported bool）；
6. **test_registry_entry_registered**：explorer/1.0.0 条目四字段（id/version/占位符/模型名/schema 文件）+ 既有 test_prompt_registry 哈希门禁自动覆盖（sync 后）；
7. **test_prompt_declares_required_and_enums**（评审 R-6，仿 test_prompt_registry L205/L362 模式）：system.md 文本断言必填字段清单与枚举声明（防 prompt 防回归约束缺失）。

### 3.6 与大纲一致性对照

| 大纲条目 | 实现 | 一致性 |
|---|---|---|
| §2.4 ExplorerObservation 四字段（chain_proposals/read_requests/component_summary/loop.done） | §3.2 模型逐一对应 | 一致 |
| §2.4 读码操作（get_method_body/get_callees/get_callers/search_symbol） | 五操作（+resolve_invoke_target） | 一致（超集含 T2.4 能力） |
| §4.1 假设非裁决（hypothesis） | prompt 硬约束 1 + ChainProposal 枚举 | 一致 |
| §4.3 循环状态机 | loop.done + reason 必答 | 一致 |
| 禁止模型自循环 | read_requests 由 explorer.py 本地执行（prompt 硬约束 + D2 操作面收窄） | 一致 |

## 4. 风险与回退

| 风险 | 对策 | 回退 |
|---|---|---|
| 操作面过宽致模型滥用 | 五操作均有界（call_tree 内部 LIMIT） | 收窄操作枚举（协议版本演进） |
| prompt 语义与 T2.5b 驱动不匹配 | T2.5b 实施时按本协议冻结迭代 | 修订 prompt（版本化） |
| token 膨胀（上下文累积） | LongText 上限 + T2.5b 累积摘要策略 | - |

## 5. 依赖

- 前置：T0.1（ChainProposal）；sync-ai-protocol.py（既有）。
