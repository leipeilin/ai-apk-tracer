# M0（基线与接口设计）实施提交审查报告

> **审查对象**：`docs/analysis/explorer-track/2026-08-21-explorer-track-implementation-plan.md` §3.1 M0（T0.1–T0.9）的实施提交，commit 范围 `34a3daa..26f7b0a`（11 个提交：T0.1–T0.9 共 9 个功能提交 + 2 个 chore，53 文件，+6930 行）
> **审查日期**：2026-08-22
> **审查方法**：
> - 逐任务核对交付物（Schema / prompt / 配置 / 映射表 / 设计稿）与任务描述的一致性；
> - 核对每任务的 implementation-plan / acceptance-plan / review 三文档及验收记录；
> - 独立运行全量 pytest 验证测试结论（非沙箱环境）；
> - 对 hops 可回查性、verify 盲验结构、verify 与 L2 协议兼容性做代码锚点核对（`indexer.py` / `index_store.py` / `ai_models.py` / `prompt_registry.py` / `config.py`）。
> **总体表态**：**通过，可进入 M1**。M0 交付完整、流程合规（每任务三文档 + 子 agent 评审 + 处置记录 + 验收记录齐备）、测试充分（全量 903 passed，3 个 guard_verifier 失败为 pre-existing，与 M0 无关）。发现 2 项中等、3 项轻微问题，均不阻塞，建议在 M1 开工前按 §5 处理。

---

## 1. 交付物核对（T0.1–T0.9）

| 任务 | 计划交付物 | 实际交付 | 结论 |
|---|---|---|---|
| T0.1 | `ExplorerObservation` Schema（hops/hypothesis/impact_proposal/read_requests/component_summary/loop.done） | `schemas/ai_explorer_observation.schema.json` + 模型 + 测试 | ✅ 字段齐备；`loop.done=True` 必须有 chain（validator `_done_requires_chain`） |
| T0.2 | `ExplorerCandidate` Schema（含 validation 三档占位） | `schemas/explorer_candidate.schema.json` + 模型 | ✅ validation 含 status/notes/verified_hop_count/failed_hop_indices/blocked_by_guard/custom_sink_proposal |
| T0.3 | `explorer_deep_dive` 协议 Schema + prompt 骨架 + registry | `schemas/ai_explorer_deep_dive_{input,output}.schema.json` + `prompts/explorer-deep-dive/1.0.0/` + registry 注册 | ⚠️ 文件命名/数量与计划描述不一致（见 §4.3），内容完整 |
| T0.4 | 规则产物 Schema（binder/receiver/webview，含 AMBIGUOUS/UNRESOLVED gap 透传） | 三个 schema + `test_rule_artifacts.py` | ✅ gap 经 `gaps[].code` 透传，测试覆盖两种 gap code |
| T0.5 | `api_entry_table` / `attack_surface` Schema | 两个 schema + 测试 | ✅ 来源字段按规则产物标注；入口 ID pattern 与草案一致 |
| T0.6 | 归一化映射表（文档 + 单测） | `2026-08-22-t0-6-normalization-mapping.md` + `test_normalization_mapping.py` | ✅ required 10 项全覆盖断言 + 字段路径存在性断言 |
| T0.7 | 配置模型扩展（全部默认关闭） | `config.py` 6 个 Settings 段 + `default.yaml` + `config.schema.json` + 测试 | ✅ 默认值、环境变量覆盖、负例均有测试 |
| T0.8 | Asset/BatchScan 迁移设计稿 | `2026-08-22-t0-8-implementation-plan.md`（设计稿，无代码） | ✅ 按任务定位交付；含版本/升级/回滚/测试方案 |
| T0.9 | verify 核验协议 Schema + prompt + registry | `ai_verify_{input,output}.schema.json` + `prompts/verify/1.0.0/` + registry 注册 + 12 组测试 | ✅ 盲验双重结构 + L2 对齐（详见 §3） |

## 2. 验收执行情况核对

- **全量测试**：审查者独立运行 `backend/.venv/bin/python -m pytest` → **903 passed, 3 failed**。3 个失败全部在 `tests/test_guard_verifier.py`，M0 系列 diff 未触碰 `guard_verifier.py` / `test_guard_verifier.py`，判定为 pre-existing，与各任务验收记录披露一致（T0.1 起每任务均以 stash 隔离验证并如实披露）。
- **三文档流程**：9 个任务的 implementation-plan / acceptance-plan / review 全部存在；acceptance-plan 均含"验收记录（实施后填写）"章节并逐项勾选；review 文档含严重度分级 + 处置记录（采纳/理由/修订位置）。
- **Schema 一致性门禁**：`test_committed_schemas_exactly_match_stable_model_generation`（`test_ai_models.py:151`）强制 schema 文件与 `model_json_schema()` 逐字节一致，新增 11 个 schema 均被覆盖。
- **Registry 哈希门禁**：`test_registry_uses_exact_patch_versions_and_raw_byte_hashes` 遍历 `registry.yaml` 全部条目校验 template/schema SHA-256；`prompt_registry.py:213` 校验 input/output 模型必须注册于 `AI_MODEL_REGISTRY`。
- **基线回归（缺项）**：M0 验收未执行通用门禁 §4.1"默认配置下基线 APK 产物 diff 为空"的实证（见 §4.1）。

## 3. 肯定项（关键设计点均已落实）

| 设计点 | 证据 | 结论 |
|---|---|---|
| hops 结构与索引 method_id 格式一致，T2.6 可回查 | `indexer.py:294` 方法 ID 实际格式 `{path}#{Class}.{method}:{start_line}`；`index_store.py:196` `call_sites.method_id` 外键 `REFERENCES methods(id)` | ✅ Hop 的 `from/to_method_id`（`path#Class.method:line`）+ `call_site_line` 可对 `call_sites` 逐跳回查 |
| 盲验结构保证 | `VerifyInput.model_fields` 与 `VerifyChainFacts.model_fields` 均不含 `hypothesis`/`impact_proposal`/`confidence`/`reasoning`/`needs_expansion`（测试 A-5） | ✅ 探索假设层在结构上不可能进入核验输入 |
| verify 与 L2 对齐（T0.9 评审 R-2 落实） | `VerifyOutput.verdict` 枚举 `supports_candidate/refutes_candidate/unresolved`；`exploitability` 6 字段与 `ExploitabilityAssessment` 完全复用；`refutation_basis` 六值与 L2 一致 | ✅ 对照 `L2ReviewOutput`（`ai_models.py:222`）逐字段核对一致 |
| 循环终止语义 | `ExplorerLoopState`/`VerifyLoopState` 的 `done` 仅声明意图，终止由代码判定；`_done_requires_chain`、`_done_requires_verdicts` validator 拒绝"空判定宣称完成" | ✅ 与评审 §4.3 一致 |
| 证据引用安全 | `ExplorerEvidenceRef.path` 用 `RelativePath`，测试覆盖路径穿越拒绝；schema 层 `additionalProperties: false` | ✅ |
| 迁移设计稿质量 | T0.8：FK 恒开启依据 `repository.py:54`（`PRAGMA foreign_keys=ON`）；`batches.ai_skipped_count` 聚合来源落库为 `runs.ai_skipped_by_batch_budget`；回滚门槛 `sqlite_version() ≥ 3.35`；大表 ALTER 锁窗口提示 | ✅ 与评审 §4.12/§4.13 逐条对应 |

## 4. 问题清单（按严重度排序）

### 4.1 [中] 通用门禁 §4.1"默认配置基线产物 diff 为空"未在 M0 验收中实证

**证据**：9 个任务的验收记录全部基于单元测试 / schema 校验 / registry 校验 / `sync-ai-protocol.py --check`；M0 系列无任何基线 APK（health/shop）run 或产物 diff 记录。§4.1 明确要求"默认配置下对基线 APK 跑 run，产物与当前基线 diff 为空"。

**影响**：M0 改动面（新增 schema/prompt/配置段，默认全关，`config.py` 新字段均有默认值）理论不触及现有运行路径，风险低；但门禁要求实证，且 M1 将首次触碰 `repository.py`（迁移），基线回归应在 M1 前建立。

**建议**：M1 开工前补跑一次基线双 APK 回归并记录（含现有产物内容不变的 diff 结果），或在 M0 验收文档中补充"改动面论证"（默认开关全关 + 现有代码路径零修改的逐文件核对）作为替代证据。

### 4.2 [中] VerifyOutput 证据引用类型与 L2 不一致，适配层需求未显式声明

**证据**：`L2ReviewOutput.evidence_refs` 为 `EvidenceReference`（`ai_models.py:222`，`context_id` 必填、`claim` 必填）；`VerifyOutput.evidence_refs` 为 `ExplorerEvidenceRef`（`ai_models.py:517`，仅 `path` 必填，无 `context_id`）。`VerifyOutput` docstring 声明"verdict/confidence_tier/flaw_holds/exploitability/refutation_basis 与 L2ReviewOutput 对齐（DecisionEngine 消费路径不变）"，但只提到 `harm/reachability_class/impact_vector/reverse_exclusion` 等字段由 T2.12 适配层补齐，**未提及 evidence_refs 的类型转换（ExplorerEvidenceRef → EvidenceReference，需 context_id 回填）**。

**影响**：若 T2.12 仅补 L2 字段而忽略证据引用转换，DecisionEngine 的证据校验（要求 context_id 可回查）将无法消费 verify 输出，T2.12 验收会落空。

**建议**：在 T2.11/T2.12 实施计划中显式加入"verify 输出证据引用 → EvidenceReference 转换（context_id 从输入上下文映射回填）"条目，并纳入 M2 验收（构造含 ExplorerEvidenceRef 的 verify 输出，断言适配后 DecisionEngine 证据校验通过）。

### 4.3 [低] M0 交付物计数与文件命名未同步更新

**证据**：实施计划 M0 交付物写"10 个新 Schema（含 verify 输入/输出）"，实际新增 **11 个** schema 文件（deep_dive input/output 各计 1）；T0.3 计划文件名 `schemas/explorer_deep_dive_observation.schema.json` 与实际 `ai_explorer_deep_dive_{input,output}.schema.json` 不一致（`git diff --name-only 538fc05..26f7b0a -- schemas`：11 新增 + 1 修改）。

**影响**：命名更符合项目 `ai_*_input/output` 惯例，属合理改进；但计划文档与实现不符，后续里程碑验收对照时会产生计数歧义。

**建议**：更新实施计划 M0 交付物计数为"11 个新 Schema"，T0.3 文件名同步为实际命名。

### 4.4 [低] config.schema.json 将新 6 段声明为 required，与运行时行为不一致

**证据**：`schemas/config.schema.json` required 增至 13 项（原 7 + 新 6）；而 `config.py` 各 Settings 段均有默认实例（`ExplorerSettings()` 等），缺段时 pydantic 正常加载；代码中无任何运行时引用 `config.schema.json` 做校验（仅 `test_config.py` 读取）。

**影响**：当前无运行时影响；若后续该 schema 被前端或用户配置校验使用，将拒绝不含新段的旧配置，产生误拒绝。

**建议**：明确 schema 使用边界（文档/校验工具），或将新 6 段从 required 移出（与运行时默认值语义一致）。

### 4.5 [低] `explorer/1.0.0` 先声明后注册的运行时失败路径提示不足

**证据**：`config/default.yaml` 默认 `explorer.prompt_version: explorer/1.0.0`，`prompts/registry.yaml` 未注册该协议（有意为之，`test_prompt_version_declared_matches_registry` 显式断言未注册属预期）；若 T2.5 前误开 `explorer.enabled=true`，`prompt_registry.load` 将抛"未知 Prompt"。

**影响**：属显式失败而非静默降级，可接受；但错误信息未指明"需先完成 T2.5 注册"。

**建议**：在 T2.5 前的配置注释或 registry 错误信息中补充指引（可选，非阻塞）。

## 5. 结论与建议

**结论**：M0 实施提交质量高、流程合规、测试充分、关键设计点（hops 可回查、盲验结构、L2 对齐、循环终止语义、迁移设计）均已落实并经代码核对；全量测试结论与提交声明一致（903 passed + 3 pre-existing）。**通过，建议放行进入 M1。**

**放行前建议**（均不阻塞）：

1. 补基线双 APK 默认配置回归或改动面论证（§4.1）；
2. 在 T2.11/T2.12 计划中显式加入 verify 证据引用类型转换条目（§4.2）；
3. 同步实施计划中的 schema 计数与 T0.3 文件名（§4.3）；
4. 明确 config.schema.json 使用边界或调整 required（§4.4）。

> 备注：本报告为审查结论，审查中执行的测试（903 passed / 3 pre-existing）与各任务验收记录一致；3 个 `test_guard_verifier.py` 失败经 diff 核对与 M0 改动无关，属既有问题，建议另行安排修复（不在 M0 范围）。
