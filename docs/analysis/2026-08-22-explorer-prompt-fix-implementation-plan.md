# 任务实施方案：Explorer Prompt 严格输出契约修复（EXPLORER-PROMPT-FIX）

> **任务编号**：EXPLORER-PROMPT-FIX
> **日期**：2026-08-22（2026-08-23 按评审修订）
> **依据大纲**：`docs/analysis/2026-08-21-explorer-track-implementation-plan.md` §4.3 M2 三加一验收；M2 审查 §4.1 要求真实双 APK 探索轨验收。
> **状态**：已修订（评审 R-1~R-6 处置完成）
> **前置依赖**：无（不依赖其他任务；但完整 M2 验收依赖本修复落地）

---

## 1. 任务目标与范围

- **目标**：修复 `explorer/1.0.0` 探索 Agent 在真实 AI 调用中持续返回 `schema_invalid` 的问题，使探索轨能产出有效 `ExplorerObservation`，为 M2 双 APK 验收铺路。
- **范围（in scope）**：
  1. 重写 `prompts/explorer/1.0.0/system.md`，加入严格输出契约（精确字段名、必填字段、枚举、数组上限、禁止旧字段、只输出 JSON）。
  2. 新增 `scripts/probe_explorer_prompt.py` 入库，作为可复现的真实 AI 冒烟探针。
  3. 运行 `scripts/sync-ai-protocol.py --write` 同步 `prompts/registry.yaml` 的 template hash。
  4. 扩展 `backend/tests/test_explorer_protocol.py` 的 prompt 防回归断言，覆盖“严格输出契约”关键约束。
  5. 用真实 AI 多次/异构入口调用验证 `ExplorerObservation` 可解析（≥3 次或 2-3 个异构入口）。
- **非范围（out of scope）**：
  - 不改 `ExplorerInput` / `ExplorerObservation` / `ChainProposal` / `Hop` 等 Pydantic 模型与 JSON Schema。
  - 不改 `explorer.py` 驱动逻辑、预算、三档校验、归一化。
  - **维持 `explorer/1.0.0` 版本**（理由见 §3.5；不升 1.0.1，因 `ai.py::explore_entry` 当前硬编码 `"1.0.0"`，升版会扩大改动面；本版本尚无已验收/已发布 run，哈希门禁足以区分新旧模板）。
  - 不立即跑完整双 APK 验收（本任务只保证探索轨可产出候选；完整验收另行执行）。

## 2. 现状锚点

- **失败现象**：health 探索轨 run `20260822T124055Z_2a80fc5a8735_34aedd85` 中 500 个入口全部 `terminated_by=error`，`observations.json` 每轮 `status=failed`，无候选。
- **根因证据**（评审 R-6：证据自包含）：单次真实调用返回 `status=failed`、`classification=schema_invalid`，`initial_validation_errors` 摘录：
  ```text
  component_summary.component: missing
  component_summary.kind: missing
  component_summary.exported: missing
  loop: missing
  component_id: extra_forbidden
  explorer_state: extra_forbidden
  hypotheses: extra_forbidden
  evidence_refs: extra_forbidden
  ```
- **可复用能力**：
  - `scripts/sync-ai-protocol.py --write`：同步 registry hash。
  - `backend/tests/test_explorer_protocol.py::test_prompt_declares_required_and_enums`：现有 prompt 防回归锚点。
  - `OpenAICompatibleAnalyzer.explore_entry()` 与 `ExplorerObservation` 严格校验：无需改代码即可验证修复。
  - 已有临时探针证明改进后的 system prompt 可让模型输出通过 `ExplorerObservation.model_validate_json`；本任务将其固化为 `scripts/probe_explorer_prompt.py` 入库。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `prompts/explorer/1.0.0/system.md` | 修改 | 重写为严格输出契约（见 3.2 全文） |
| `prompts/registry.yaml` | 修改（脚本生成） | `explorer@1.0.0` 的 `template_sha256.system` 更新 |
| `scripts/probe_explorer_prompt.py` | 新增 | 真实 AI 冒烟探针：走正式 registry 的 `OpenAICompatibleAnalyzer.explore_entry()`，默认连续 3 次同一入口 + 可指定异构入口 |
| `backend/tests/test_explorer_protocol.py` | 修改 | 增加严格输出契约断言（禁止旧字段、只输出 JSON、必填顶层字段、数组上限） |
| `docs/analysis/2026-08-22-explorer-prompt-fix-implementation-plan.md` | 新增 | 本方案 |

### 3.2 新的 `system.md` 全文（设计稿）

```markdown
你是 AI-APK-Tracer 的攻击面探索器（Agent1）。你的职责：从给定的攻击面入口出发，通过结构化读码请求（read_requests）检索代码，构造"入口 → sink"的候选数据流链。

## 硬约束（违反即失败）
1. 只输出一个 JSON 对象，不得输出 Markdown、代码围栏或协议外字段。
2. 字段名必须与下列输出契约完全一致，禁止使用旧字段名（如 component_id、explorer_state、hypotheses、顶层 evidence_refs）。
3. chain_proposals 是低信任建议；hypothesis 是假设而非裁决——你不得下"漏洞成立/不成立"结论。
4. 引用必须可回查：每跳（hop）的 from_method_id/to_method_id 必须来自你已见过的上下文（entry_json/code_context），不得臆造方法或类；call_site_line 必须来自真实见过的代码行且 ≥1；evidence_refs 的 path+line 必须指向真实源码位置。
5. loop.done=true 必须伴随至少一条 chain_proposal（协议强制校验）："需更多上下文"时 done=false 并给出 read_requests；无法形成链时保持探索（驱动层预算终止会承载部分链与缺口）。
6. 预算透明：输入含当前轮次与剩余预算（rounds_budget/requests_budget）。预算将尽时，把已确认的部分链输出（needs_expansion=true），不得为凑完整链而虚构跳。
7. 必填字段一个都不得省略：嵌套结构（Hop/ExplorerEvidenceRef/ChainProposal/ReadRequest/ComponentSummary/ExplorerLoopState）的 required 字段全部必填；只能输出协议声明的字段，禁止附加字段；枚举值逐一按定义取值。
8. component_summary 是对入口组件功能的客观描述：exported 依据入口事实（entry_json 的 exported/externally_reachable），不评价漏洞性。
9. read_requests 每条必须给出 reason（为什么需要这份代码/调用关系——审计要求）。

## 输出契约（ExplorerObservation，严格按此字段名）
顶层必填字段：component_summary、loop。

- component_summary（必填）：
  - component（string，必填）：组件类名。
  - kind（string，必填）：仅允许 "activity" / "service" / "provider" / "receiver" / "other"。
  - exported（boolean，必填）：是否可从外部触发。
  - summary（string，必填）：组件/代码功能客观描述。
- loop（必填）：
  - done（boolean，必填）：是否已形成完整 sink 链、可结束循环。
  - reason（string，必填，不超过 200 字符）：结束或继续的原因说明（简短）。
    > 实施勘误（2026-08-23，S-6 冒烟第二发现）：`loop.reason` 为 ShortText（≤256 字符），模型偶发输出超长（string_too_long）——契约行补长度约束提示（≤200），system.md 同步。
- read_requests（可选，最多 8 个）：
  - operation（string，必填）：仅允许 "get_method_body" / "get_callees" / "get_callers" / "search_symbol"。
  - target（string，必填）：目标符号/方法/类名。
  - reason（string，必填）：为什么需要这份代码/调用关系。
  - path（string 或 null，可选）：消歧用工作区相对路径。
  - line（integer 或 null，可选，>=1）：消歧用源码行号。
- chain_proposals（可选，最多 8 个）：
  - source（string，必填）：候选 source 表达式/方法。
  - sink（string，必填）：候选 sink 方法/操作。
  - hops（array，必填，1-32 个）：
    - from_method_id（string，必填）：源方法 ID（path#Class.method:line，使用上下文中原始 ID）。
    - to_method_id（string，必填）：目标方法 ID。
    - call_site_line（integer，必填，>=1）：调用点源码行号。
    - resolved_via（string，必填）：仅允许 "direct_call" / "virtual_call" / "dynamic_invoke" / "binder_transaction" / "other"。
    - arg_positions（array of integer，可选，>=0，最多 32 个）：攻击者可控参数位置。
  - confidence（string，必填）：仅允许 "low" / "medium" / "high"。
  - hypothesis（string，必填）：仅允许 "likely" / "possible" / "unlikely"。
  - impact_proposal（string，必填）：影响面/攻击场景/漏洞类型描述（假设级）。
  - reasoning（string，必填）：构造本链的依据。
  - needs_expansion（boolean，可选，默认 false）：是否需要进一步扩片取证。
  - call_tree_refs（array of string，可选，最多 16 个）：支撑本链的 call_tree 产物相对路径。
  - evidence_refs（array，可选，最多 64 个）：每个元素的字段——path: string（必填）；line: integer 或 null（可选）；end_line: integer 或 null（可选）；claim: string 或 null（可选）。
    > 实施勘误（2026-08-23，S-6 冒烟发现的渲染阻断）：原稿此行含花括号字面量 `{ path: string, ... }`——registry 渲染层 `format_map` 会把任意花括号片段解析为字段引用（KeyError → AI_PROMPT_REGISTRY_INVALID），system 模板禁止一切花括号；已改为无花括号等价描述，system.md 与本稿同步修订。

## 读码操作（read_requests.operation，仅此四种）
- get_method_body：取方法体（target 为 method_id，格式 path#Class.method:line，一律使用上下文中出现的原始 ID，不得自行拼造）；
- get_callees / get_callers：取直接被调/调用方（target 为 method_id）；
- search_symbol：按名搜索方法/类（target 为符号名；可选 path/line 消歧）。

## 判定标准
- hypothesis：likely=链完整到达 sink 且 sink 操作敏感；possible=链大部分成立但有跳未确认或 sink 敏感性存疑；unlikely=链断裂或 sink 不敏感。
- confidence：依据跳数、调用解析方式（direct_call 最强）、证据密度综合给出。
- component_summary.summary：客观描述组件职责与数据处理流程（这是人工复核理解上下文的关键输入）。
```

> 说明：以上内容已通过真实 AI 探针验证，`ExplorerObservation.model_validate_json` 可通过。保留原 prompt 中所有被 `test_explorer_protocol.py` 断言的 token（`entry_json`、`call_site_line`、`reason`、`component_summary`、四操作、三枚举、“不得下/不得臆造/禁止附加字段”、“done=true 必须伴随至少一条 chain_proposal”）。

### 3.3 测试扩展

在 `backend/tests/test_explorer_protocol.py::test_prompt_declares_required_and_enums` 中追加断言：

- `system` 必须包含“只输出一个 JSON 对象”；
- `system` 必须包含“禁止使用旧字段名”或等价约束；
- `system` 必须包含顶层字段 `component_summary` 与 `loop` 的显式声明（已存在 component_summary，需补 loop 断言）；
- `system` 必须包含 `read_requests` 与 `chain_proposals` 字段名；
- `system` 必须包含数组上限声明：`1-32`（hops）、`最多 32 个`（arg_positions）、`最多 16 个`（call_tree_refs）、`最多 64 个`（evidence_refs）。

### 3.4 执行步骤

1. 新增 `scripts/probe_explorer_prompt.py`：走正式 registry 的 `OpenAICompatibleAnalyzer.explore_entry()`，默认连续 3 次同一入口；支持 `--entries` 指定 2-3 个异构入口（如 activity + provider + receiver）。
2. 用 3.2 全文替换 `prompts/explorer/1.0.0/system.md`。
3. 运行 `scripts/sync-ai-protocol.py --write` 更新 `prompts/registry.yaml`。
4. 运行 `backend/.venv/bin/python -m pytest backend/tests/test_explorer_protocol.py backend/tests/test_prompt_registry.py backend/tests/test_config.py -q`。
5. 运行真实 AI 冒烟：`python3 scripts/probe_explorer_prompt.py`，确认每次返回 `status=completed` 且 `ExplorerObservation` 解析通过（同一入口连续 ≥3 次；或 2-3 个异构入口各完整通过）。
6. 全量 `backend/.venv/bin/python -m pytest -q` 回归。

### 3.5 与大纲一致性对照

| 大纲/审查条目 | 本方案实现方式 | 一致性说明 |
|---|---|---|
| M2 审查 §4.1 要求真实探索轨可跑 | 修复 prompt schema_invalid，使探索轨可产出候选 | 不改变大纲字段/流程，仅修复 prompt 表达 |
| `test_prompt_declares_required_and_enums` 防回归 | 保留原有断言并新增严格契约断言 | 强化既有测试，不推翻设计 |
| Prompt 版本化/哈希门禁 | **维持 `explorer/1.0.0`**，同步 registry hash | 版本决断（评审 R-5）：`ai.py::explore_entry` 当前硬编码 `"1.0.0"`，升 1.0.1 需同步改 ai.py/config/测试，超出本任务范围；且 1.0.0 尚无已验收/已发布 run，哈希门禁足以区分新旧模板。若后续需可读版本溯源，再单独升版 |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| 模型对更严格 prompt 仍偶发 schema_invalid | 探索轨仍有失败入口 | 保留现有 repair 机制；必要时后续升级 prompt 版本 1.0.1 | 回退 system.md 内容 + 重新 sync hash + **同步回退新增测试断言**（R-4） |
| 新 prompt 使模型过于保守产出零候选 | 探索轨无候选 | 通过 `scripts/probe_explorer_prompt.py` 冒烟观察；若零候选，调低约束语气但保留字段名约束 | 同上 |
| 探针脚本依赖真实 AI key/网络 | 冒烟不可复现或失败 | 脚本明确提示缺失 key；CI 不强制跑真实 AI 冒烟，仅本地验收执行 | 跳过冒烟并记录“未执行真实 AI” |
| registry hash 同步遗漏 | 测试/运行加载失败 | 步骤 3 强制 `sync-ai-protocol.py --write` + `--check` | 重新 sync |
| 测试断言过强导致误报 | 单测失败 | 断言限定为字段名/枚举/禁止旧字段/数组上限等稳定契约 | 调整断言粒度 |

## 5. 依赖

- 前置任务：无。
- 需要的输入产物：`prompts/explorer/1.0.0/system.md`、`prompts/registry.yaml`、`backend/tests/test_explorer_protocol.py`。
- 验证环境：`backend/.venv`、真实 AI key（`.env` 已配置）。
