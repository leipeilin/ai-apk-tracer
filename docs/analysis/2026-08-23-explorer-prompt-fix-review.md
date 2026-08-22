# 任务评审：EXPLORER-PROMPT-FIX 实施方案

> **评审对象**：`docs/analysis/2026-08-22-explorer-prompt-fix-implementation-plan.md`（下文称"原方案"）；**第 2 轮**评审对象为验收方案与任务计划报告（见 §6）
> **评审日期**：2026-08-23
> **评审方式**：主 agent 逐项代码核对——`backend/app/analysis/ai_models.py`（ExplorerObservation/ChainProposal/Hop/ReadRequest/ComponentSummary/ExplorerLoopState/ExplorerInput 全量 Schema）、现行 `prompts/explorer/1.0.0/system.md`、`backend/tests/test_explorer_protocol.py::test_prompt_declares_required_and_enums` 断言集、`scripts/sync-ai-protocol.py`（--write/--check）、`prompts/registry.yaml`（explorer@1.0.0 哈希门禁）、`backend/app/analysis/ai.py`（strict-parse→单次 repair 状态机，:507-511）、`backend/app/analysis/explorer.py`、大纲 `docs/analysis/2026-08-21-explorer-track-implementation-plan.md`（§4.3 三加一 / T2.5）。
> **结论**：**方向正确、契约与 Schema 逐项一致，可执行**；首版存在 2 关键 + 3 中 + 1 低问题，修订后即可按 §3.4 步骤实施。

## 1. 评审结论摘要

- 根因诊断成立：现行 `system.md` 确无"只输出一个 JSON 对象"与完整字段契约，模型回退旧字段（`component_id`/`explorer_state`/`hypotheses`/顶层 `evidence_refs`）与 `schema_invalid` 现象自洽。
- 3.2 设计稿的"输出契约"与 `ai_models.py` 逐字段一致（顶层必填/可选、max 8、全部枚举、嵌套结构、`needs_expansion` 默认 false、`ExplorerEvidenceRef` 四字段）。
- 防回归 token 全保留，现有测试不会因换 prompt 转红；新增断言方向正确。
- 问题集中在**验收可复现性与统计强度**（R-1/R-2）、**上限声明完整性**（R-3）、**回退连带**（R-4）、**版本化决断**（R-5）。

## 2. 问题清单与处置记录

| 编号 | 严重度 | 问题摘要 | 处置建议 | 修订动作 |
|---|---|---|---|---|
| R-1 | 关键 | 验收探针依赖 `/tmp/explore_prompt_probe.py`（不入库）；`/tmp` 易失，§3.4 步骤 4 在实施时不可复现 | 采纳 | 探针固化为 `scripts/probe_explorer_prompt.py` 入库（或在方案中内联关键调用代码），走正式 registry 的 `OpenAICompatibleAnalyzer.explore_entry()` |
| R-2 | 关键 | "真实 AI 单次调用验证可解析"统计强度不足：原故障为 500 入口全灭的统计性失败，单次通过不代表修复 | 采纳 | 验收改为同一入口 ≥3 次连续 `status=completed` 且 `ExplorerObservation` 解析通过，或 2-3 个异构入口（activity + provider/receiver）各跑完整循环 |
| R-3 | 中 | 数组上限未声明全：Schema 有 `hops≤32`/`evidence_refs≤64`/`call_tree_refs≤16`/`arg_positions≤32`，而 Pydantic 校验在驱动归一化**之前**，超限即 `schema_invalid`，driver 无法兜底；prompt 仅声明顶层"最多 8 个" | 采纳 | 3.2 契约补上限：`hops（1-32）`、`evidence_refs（≤64）`、`call_tree_refs（≤16）`、`arg_positions（≤32）` |
| R-4 | 中 | 回退方案与新增测试断言冲突：风险表回退列仅"回退 system.md + 重新 sync"，但 §3.3 新增断言（"只输出一个 JSON 对象"、"禁止使用旧字段名"）在旧 prompt 下必红 | 采纳 | 回退方案补"测试断言同步回退"，或新增断言以参数化方式随 prompt 版本切换 |
| R-5 | 中 | 版本化悬而未决："维持 1.0.0；若评审要求再升 1.0.1"不是结论。两种选择均站得住（哈希门禁 + checkpoint fingerprint 已能区分新旧 run），但方案必须二选一并写理由 | 采纳（建议升 1.0.1） | 语义变化为实质性（新增严格输出契约），建议升 `explorer/1.0.1` 新 registry 条目，run 溯源与报告 `prompt_version` 可读性更好；若维持 1.0.0 需写明理由（哈希门禁充分） |
| R-6 | 低 | 证据 run `20260822T124055Z_2a80fc5a8735_34aedd85` 产物不在仓库内，审查依赖运行环境 | 可选 | 方案附录贴 `initial_validation_errors` 摘录（旧字段缺失/附加清单），使证据自包含 |

## 3. 认可项

- **契约-Schema 一致性**（逐项核对 `ai_models.py:265-352`）：顶层 `component_summary`/`loop` 必填、`read_requests`/`chain_proposals` 可选且 max 8；`kind`/`operation`/`resolved_via`/`confidence`/`hypothesis` 五组枚举逐字一致；`Hop` 五字段、`ExplorerEvidenceRef`（path 必填，line/end_line/claim 可选）一致；`loop.done=true 必须伴随至少一条 chain_proposal` 与 `model_validator` 语义对齐。
- **根因诊断有实证锚点**：现行 prompt 缺"只输出一个 JSON 对象"与显式字段契约；3.2 硬约束 1/2 正是缺口补丁。
- **防回归 token 全保留**：`reason`/`call_site_line`/`entry_json`/`component_summary`、七枚举（likely/possible/unlikely + 四操作）、"不得下/不得臆造/禁止附加字段"、"done=true 必须伴随至少一条 chain_proposal" 均见于 3.2 设计稿，`test_prompt_declares_required_and_enums` 换稿后不转红。
- **工具链引用真实**：`sync-ai-protocol.py --write/--check` 存在；registry `explorer@1.0.0` 哈希门禁存在；repair 状态机（`ai.py:507` strict-parse→单次 repair）真实存在，风险表"保留现有 repair 机制"有依据。
- **大纲引用真实**：`2026-08-21` 大纲 §4.3 "M2 专项验收（三加一口径）"、T2.5 探索轨条目存在；§3.5 一致性对照成立。
- **预算字段引用真实**：`ExplorerInput`（`ai_models.py:407-430`）确有 `round_index`/`rounds_budget`/`requests_budget`/`entry_json`/`code_context`，prompt 描述无虚构。
- **范围控制得当**：不改 Pydantic 模型/JSON Schema、不改 `explorer.py` 驱动与三档校验、不立即跑完整双 APK 验收。

## 4. 边界检查表

| 检查项 | 状态 |
|---|---|
| 契约-Schema 一致性 | ✅ 逐项核对通过 |
| 防回归（现有测试不转红） | ✅ token 全保留 |
| 验收可复现性 | 待修订（R-1） |
| 验收统计强度 | 待修订（R-2） |
| 上限声明完整性 | 待修订（R-3） |
| 回滚 | 待修订（R-4：回退需连带测试断言） |
| 版本化决断 | 待修订（R-5：二选一并写理由） |
| 证据自包含 | 可选修订（R-6） |
| registry 哈希同步 | ✅ 步骤 2 强制 `--write` + `--check` |
| 并发/预算 | 不涉及（不改驱动与预算逻辑） |

## 5. 闭合路径

R-1～R-5 为实施前置修订项（R-6 可选）。修订完成后无需二轮评审：五项均为局部文本修订（探针入库、验收样本量、契约上限、回退连带、版本结论），不改变方案结构与范围，主 agent 修订时在本文件"处置建议"列基础上落稿即可。

---

## 6. 第 2 轮评审：验收方案 + 任务计划报告（2026-08-23）

> **评审对象**：`docs/analysis/2026-08-23-explorer-prompt-fix-acceptance-plan.md`、`docs/analysis/2026-08-23-explorer-prompt-fix-task-plan-report.md`
> **评审方式**：独立只读子 agent（deepseek-v4-flash 视角）——逐项核验代码锚点（config.py dotenv 行为、ai.py key 读取路径、ai_runtime 构造先例、缓存 no-op 保障、探针入口数据可得性）与 §3.2 设计稿断言 token 逐字自洽性。
> **结论**：A-1~A-8 可判定、S-0~S-7 依赖顺序正确、A-2 断言 token 与 §3.2 设计稿逐字自洽（S-4 断言不会落空）；R-7（关键）+ R-8~R-12 全部采纳修订后通过。

### 问题清单与处置记录

| 编号 | 严重度 | 问题摘要 | 处置 | 修订动作 |
|---|---|---|---|---|
| R-7 | 高 | 探针经 `get_settings()` 拿不到 `.env` 中的 key（dotenv 仅作 Settings 字段来源不注入 os.environ，而 ai.py 经 os.environ 读 key）——A-6 正例路径断裂，N-1 负例恰好掩盖 | 采纳 | 任务报告 S-1 明确探针自行 `load_dotenv()` + os.environ 校验；验收 A-7 增审查点 |
| R-8 | 中 | A-6 冒烟结论绑定未提交的本地 AI 配置（config/default.yaml 的 token-plan base_url/model） | 采纳 | 验收 §6 回填时记录实际 base_url/model 并注明依赖未提交本地配置 |
| R-9 | 中 | A-9 回退演练需人工重放 §3.2 全文+识别新增断言边界，易引入文本漂移 | 采纳 | 演练改 git stash 方式（stash→checkout 演练→pop→复跑 A-1 diff + S-3 --write） |
| R-10 | 中 | S-1"经 get_settings() 构造 analyzer"参数表述含糊（需 AI 子配置） | 采纳 | 明确 `AIRuntime(get_settings().ai).create_analyzer(...)` |
| R-11 | 低 | 探针工程细节未列（sys.path/asyncio.run/入口数据路径与 entry_json 形状） | 采纳 | S-1 补充三点实现约束 |
| R-12 | 低 | A-9 演练记录无处回填（§6 无备注区） | 采纳 | 写入 A-9 行"实测说明"列 |

### 认可项（摘）

- A-2 断言 token 与 §3.2 设计稿逐字自洽（"只输出一个 JSON 对象"/"禁止使用旧字段名"/顶层 loop/1-32/最多 32·16·64 个——逐项核验存在）。
- 既有断言 token 全保留（test_explorer_protocol.py:190-203 全部 token 在设计稿中逐字存在——换稿不转红）。
- S-3 后于 S-2、S-6 后于 S-5 的依赖正确（避免哈希二次漂移/真实调用成本不替单测兜底）。
- A-6 3/3 门槛与 N-3/N-4 边界自洽；缓存 no-op 保障 3 次独立真实采样（`_cacheable_output` 要求 analysis_complete+evidence_refs——ExplorerObservation 均无）。
- N-1 负例成立（`_local_configuration_result` 发网前拦截缺 key）。
- 探针入口数据可得（run 20260822T124055Z… 的 api_entry_table.json 真实存在；ExplorerInput 构造有 explorer.py:154-162 先例）。
- DoD 与工作流阶段衔接完备；S-7 排除清单与工作区实况相符。

**闭合结论**：R-7~R-12 全部采纳并修订（任务报告 S-1/S-7、验收方案 A-7/§6 备注已落稿）；进入阶段 5 实施。
