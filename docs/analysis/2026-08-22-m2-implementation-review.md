# M2（API surface + call tree + 探索轨合流）实施提交审查报告

> **审查对象**：`docs/analysis/2026-08-21-explorer-track-implementation-plan.md` §3.3 M2（T2.1–T2.12）的实施提交，commit 范围 `d1ed272..9cbec2e`（14 个提交，95 文件，+15533 行）
> **审查日期**：2026-08-22
> **审查方法**：
> - 逐任务核对交付物与验收记录（T2.1–T2.12 三文档，T2.5 拆 2.5a/2.5b）；
> - 独立运行全量 pytest（非沙箱）；
> - 代码级核对全链路：规则产物导出 → api_surface → attack_surface → call_tree → explorer 检索循环 → 三档校验 → 归一化/funnel 三分流 → deep_dive → custom sink 闭环 → 人工队列 → verify agent → 适配/分流/降级；
> - 核对 M2 专项验收（§4.3 三加一口径）的执行状态与验收归属。
> **总体表态**：**代码交付完整、质量高，但 M2 里程碑专项验收（§4.3 三加一口径）未执行**——真实双 APK 探索轨覆盖/负样本/成本/性能/核验试点验证均无执行记录，§4.3 全部 checkbox 未勾选，被 T2.12 计划注明"交接 §4.3 全量"但未明确交接对象与时限。因此本报告结论为：**M2 代码交付通过、验收未闭合**；在真实探索轨双 APK 验收完成前，不得宣称 M2 验收通过。

---

## 1. 交付物核对（T2.1–T2.12）

| 任务 | 交付 | 核对结论 |
|---|---|---|
| T2.1 规则产物导出 | `rule_runner.py` 产物提取（白名单键 + per-record jsonschema 校验 + gap 索引）+ `rules/shared/detector.py` 导出适配 | ✅ 产物协议校验与坏记录剔除粒度正确 |
| T2.2 api_surface | `api_surface.py` 读规则产物 + manifest 组装 `api_entry_table.json` | ✅ entry_method 实际格式修正、exported 四值域 |
| T2.3 attack_surface | `attack_surface.py` 四组件攻击面 | ✅ 真实生成器夹具消漂移、动态 exported 保守统一 |
| T2.4 call_tree | `call_tree.py` on-demand 检索（七能力 + 有界子树） | ✅ method_id 列值直取、body 240 行预算 |
| T2.5a/2.5b explorer | `ExplorerInput` 协议 + `explorer.py` 检索循环 | ✅ ai_call 预算回调、轮输入哈希落盘、8KB 截断 |
| T2.6 三档校验 | `explorer_validation.py` | ✅ hops 逐跳回查 + line_mismatch 诊断 + guard 阻断 + custom_sink 封顶 partial |
| T2.7 归一化 + funnel | `explorer_normalization.py` + `candidate_funnel.py` 三分流 | ✅ 映射表生产侧单一事实源、identity 分源、related 关联 |
| T2.8 deep_dive | `explorer.py` 扩展（DeepDive 协议） | ✅ 不改写链、不进主链、rounds/evidence 预算钳制 |
| T2.9 custom sink 闭环 | `sink_taxonomy.py` + `scripts/promote_custom_sink.py` + `versions.yaml` | ✅ promote → revalidate → golden 闭环 CLI |
| T2.10 人工队列 | `explorer_queue.py` + 前端 `ExplorerQueuePanel` | ✅ 服务端预排序、投影防响应膨胀 |
| T2.11 verify agent | `verify_agent.py` | ✅ 确定性命题生成、盲验剥离、一致性规则、undecided 物化 |
| T2.12 分流与降级 | `orchestrator.py`/`evidence.py`/适配层 | ✅ evidence_refs→EvidenceReference（path#window）、checkpoint 隔离、三本账、回退 |

## 2. 验证执行情况

- **全量测试**：审查者独立运行 `backend/.venv/bin/python -m pytest` → **1147 passed / 0 failed**（35s），与 T2.10 验收记录一致。
- **红线确认**：`backend/app` 源码无 `import rules` / `from rules`（grep 确认）；但"grep 断言写进测试"未落地（见 §4.2）。
- **协议注册**：`explorer/1.0.0` 已注册进 `prompts/registry.yaml`（含模板/输入/输出哈希），T0.7"先声明后注册"闭合。
- **M1 审查闭环**：`d1ed272` 修复 guard_verifier 环境依赖（§4.1）与 SPA catch-all `/api/*` 404 语义（§4.2），并有对应测试。
- **M0 审查闭环**：T2.12 适配层实现 M0 §4.2 的 evidence_refs 类型转换需求（`_to_evidence_reference` + DecisionEngine 端到端测试 A-6）；实施计划同步计数/命名。

## 3. 肯定项（核心设计点均已落实）

| 事项 | 证据 | 结论 |
|---|---|---|
| 全链路合流与默认关闭短路 | orchestrator：`api_surface.enabled` / `explorer.enabled` 双开关包住新阶段；默认 false 零路径进入 | ✅ |
| 探索轨预算计费闭环 | `_run_explorer_stage` 的 `budgeted_ai_call`/`budgeted_deep_dive_call` 与 `_budgeted_protocol_call` 全部经 run 级 `max_requests_per_run` 包装（直调绕过计费被评审 R-1 排除）；三本账分列（explorer/verify/ai_stage） | ✅ |
| 检索循环状态机 | `loop.done` 仅声明、终止由代码判定；每轮输入哈希落盘；8KB 上下文截断；跑满预算产出部分链 + 缺口 | ✅ |
| 三档校验严格性 | `_verify_hops` 要求 `call_sites(method_id, start_line)` 存在且 `resolved_target_id == to_method_id`、`resolve_status='resolved'`；伪造 method_id 测试覆盖判 unverified | ✅ |
| funnel 三分流 | `explorer_promoted` 走 L2 同等路由；`explorer_partial`/`explorer_unverified` 不送 AI（不占预算）；identity 含 `candidate_source` 分源；`related_candidate_ids` 关联不合并 | ✅ |
| deep_dive 职责分离 | 输出不含链、hops/validation 不可变（验收 4.3-5.4）；不自动升档；深挖证据进人工队列视图 | ✅ |
| verify 盲验与 L2 对齐 | 命题清单由候选确定性字段生成（非 Agent1 描述）；`VerifyChainFacts` 结构无假设字段；verdict/exploitability/refutation_basis 对齐 L2 | ✅ |
| 适配层（M0 §4.2） | `_to_evidence_reference` 生成 `path#window:N-M` context_id（聚合层 `validate_ai_evidence_references` 已支持回查）；无 line 证据静默丢弃计数；DecisionEngine 端到端测试通过 | ✅ |
| 降级回退主链不阻塞 | A-16：`verify.enabled=true` + 无 AI key → run completed；失败/预算尽/索引不可用/意外异常四路回退 + `verify_fallback_reason` 溯源 | ✅ |
| custom sink 闭环 | promote CLI（run 候选锚点提取 / 直接方法名两用法）→ taxonomy 版本化 → revalidate 升档报告 → golden 用例生成（GoldenCase 模型对齐） | ✅ |
| 人工队列 | 服务端预排序（置信度 → deep_dive 证据 → 跳回查完整度），unverified 不被系统性埋没 | ✅ |
| 前端 | `ExplorerQueuePanel` 计数徽标 + 空态引导；接入 RunDetailPage 轮询 | ✅ |

## 4. 问题清单（按严重度排序）

### 4.1 [高] M2 §4.3 专项验收（三加一口径）未执行——checkbox 全部未勾选，验收被"交接"但无归属

**证据**：

- 实施计划 `docs/analysis/2026-08-21-explorer-track-implementation-plan.md` §4.3 的 **全部 checkbox 均为 `- [ ]` 未勾选**，包括：health/shop 双 APK 各 ≥5 条 validated/partially_validated、8 项覆盖 ≥6 且 ≥4 validated、负样本（V-04/V-05/V-06、shop 140、OwnSystem）不进候选池、三本账成本实测、call_tree ≤2s/入口性能实测、`ai_likely_supported` 占比与单轮 L2 基线对比；
- T2.12 实施计划 §4 依赖末行明确："交接 M2 验收：探索轨三加一验收（§4.3 全量）含核验 agent 四条试点验收点（盲验 trace 断言/命题一致性/循环语义/降级回退——T2.11 已实测前三，本任务实测降级回退）"——即 §4.3 被**有意延后**，但未写明交接给 M4 还是独立补跑、何时完成；
- T2.1–T2.12 的验收记录全部为单元测试/回归/协议校验层面，无任何真实双 APK（health/shop）+ 真实 AI 的探索轨 run 记录（docs/analysis 无 m2 探索轨 run 文档，与 M1 的 `m1-baseline-runs.md` 形成对照）。

**影响**：M2 的"验收通过"目前仅覆盖代码交付与单测；§4.3 定义的覆盖/负样本/成本/性能/核验试点五项核心指标均无数据。真实 AI 链路（探索检索循环、verify 盲验、deep_dive）尚未在任何真实 APK 上跑通验证，M3（报告/PoC）的输入质量与 M4 的 golden 命中率基线均依赖该验收。

**建议**：

1. 明确 §4.3 验收归属与时限：在实施计划中把 §4.3 状态改为"移交 M4（或独立补跑任务）"，写明执行时点与验收门槛，禁止在未完成时宣称 M2 验收通过；
2. 补跑真实探索轨双 APK 验收（health/shop + 真实 AI），逐项勾选 §4.3 并产出 run 记录文档（沿用 M1 基线记录格式）；
3. 若因成本/时间需延后，至少补一个"单 APK 探索轨冒烟"（真实 jadx + 真实 AI，验证检索循环 → 三档校验 → 归一化 → funnel → verify 全链路可跑通），避免 M3 在未经验证的链路上叠加。

### 4.2 [中] §4.3-5"backend 无 import rules（grep 断言写进测试）"未落地为测试

**证据**：`backend/app` 实际无 `import rules` / `from rules`（已 grep 确认，红线现状良好），但 backend/tests 中不存在任何 grep/源码扫描断言测试（验收记录也未提及该断言）。验收标准明确要求"grep 断言写进测试"。

**建议**：补一个源码扫描测试（如遍历 `backend/app` 源码断言无 `import rules` / `from rules`），固化该零依赖红线，防止后续合流时被破坏。

### 4.3 [中] §4.3-5"默认配置下探索轨零影响（产物 diff 为空）"未用基线实测

**证据**：M1 已建立 health/shop 基线（`m1-baseline-runs.md` + `scripts/baseline-manifest.py`），但 M2 改动后未见任何基线 diff 记录；M2 对 orchestrator/funnel/ai.py 改动较大（合流、三分流、预算池共享调整），默认关闭的短路虽由单测覆盖，但"产物 diff 为空"的实证缺失。

**建议**：用 M1 基线 APK 各跑一次默认配置 run，`baseline-manifest.py` 对比清单聚合哈希，记录 diff 结果（含 `findings_count` 一致性），与 §4.1 一并完成。

### 4.4 [低] 提交顺序与验收数字快照易误读

**证据**：M2 父链顺序为 T2.1→…→T2.8→T2.11→T2.12→T2.9→T2.10（T2.9/T2.10 最后提交，符合计划"可与 T2.7 并行"）；各任务验收记录的全量数字为提交时点快照（T2.11:1103、T2.12:1120，均小于 T2.9:1138、T2.10:1147），当前 HEAD 为 1147。数字本身自洽，但按任务编号阅读会误读为回归。

**建议**：M2 收尾文档注明提交顺序与最终基线（1147），验收记录的数字注明"提交时点快照"。

### 4.5 [低] sink_taxonomy 种子覆盖待真实 APK 验证

**证据**：`rules/sink_taxonomy/versions.yaml`（80 行）种子提炼自 `rules/shared/dataflow.py classify_operation_taxonomy`；custom_sink_proposal 判定在真实 APK 上的命中/误标效果无实证（依赖 §4.1 的双 APK 验收）。

**建议**：随 §4.1 双 APK 验收记录 custom sink 命中率与人工确认数；若误标率高，调整 receiver 宽松匹配口径（当前 None/空 receiver 宽松命中是有意偏离，需数据校准）。

## 5. 结论与建议

**结论**：M2 的**代码交付**质量高——全链路（规则产物 → 攻击面 → 检索循环 → 三档校验 → 归一化/funnel 三分流 → deep_dive → custom sink 闭环 → 人工队列 → verify 分流降级）实现完整、与设计一致，测试充分（1147 passed / 0 failed 独立复现），M0/M1 审查意见全部闭环。但 **M2 里程碑验收未闭合**：§4.3 三加一口径（真实双 APK 探索轨覆盖/负样本/成本/性能/核验试点）未执行，checkbox 全空，验收被"交接"但无归属与时限。

**放行建议**（按优先级）：

1. 明确 §4.3 验收归属与时限，禁止在未完成时宣称 M2 验收通过（§4.1）；
2. 补跑真实探索轨双 APK 验收（或至少单 APK 全链路冒烟）并逐项勾选 §4.3（§4.1）；
3. 补"无 import rules"grep 断言测试与默认配置基线 diff 记录（§4.2/§4.3）；
4. M2 收尾文档注明提交顺序与最终基线（§4.4）。

> 备注：本报告为审查结论；审查中独立验证：全量 pytest 1147 passed / 0 failed、backend 无 import rules、explorer/1.0.0 已注册、§4.3 checkbox 全空、M1 审查修复已落地（d1ed272）。

---

## 6. 处置记录（2026-08-23 回填，M2-ACCEPTANCE-CLOSURE 执行）

前置阻塞（计划外发现）：审查后启动双 APK 验收时发现 explorer prompt 在真实 AI 调用下持续 `schema_invalid`（模型回退旧字段名）——500 入口全灭。已按独立任务 **EXPLORER-PROMPT-FIX** 修复（提交 d2f6ed3：system.md 严格输出契约重写 + 探针 + 冒烟 3/3），验收解除阻塞后执行闭环。

| 审查项 | 处置结果 | 证据 |
|---|---|---|
| 4.1 [高] §4.3 验收未执行 | **已执行**（部分达标）：机械链路项全部通过（负样本/成本/性能/回归边界/核验试点 15 项勾选）；质量项未达标（覆盖 ≥5 validated / 映射表 ≥6——validated=0，模型输出质量限制，如实记录） | `2026-08-23-m2-acceptance-runs.md` + 实施计划 §4.3 已勾选 |
| 4.2 [中] 缺 grep 断言与基线 diff | **已完成**：`test_no_rules_import.py`（AST 扫描）入库且通过；默认配置双 APK diff 判定（findings 一致 + 差异全归因） | 验收记录 §1 |
| 4.3 [中] 后端内存查询风险 | **记录未处置**（超出验收范围——观察项移交 M4 性能指标） | 验收记录 §5 |
| 4.4 [低] 提交顺序易误读 | **已注明**：验收记录头部列父链顺序与最终基线（1148——含新测试） | 验收记录头部 |
| 4.5 [低] sink_taxonomy 种子待实证 | **已实证**：health 真实 run custom sink 31/50 标记（种子命中 19）；误标率待 M4 人工确认统计 | 验收记录 §2.1 |

**验收发现的新缺陷**（移交后续）：
1. `extract_decoded_manifest` 的 `communicate()`/`rmtree` 无超时保护（大 APK 可阻塞 run）；
2. AI 调用偶发长挂起疑似 read_timeout 未触发（建议总时长兜底 `asyncio.wait_for`）；
3. 探索/核验 prompt 的模型输出合规率（repair 率高、validated=0、verify 全 fallback）——prompt 迭代属 M4 评估范畴。

**M2 里程碑结论**：机械链路验收通过（探索→校验→归一化→深挖→核验→分流→回退→人工队列全链路真实环境端到端 + 默认配置零回归）；质量验收部分未达标且如实记录（改进路径明确）。按本审查 §5 放行建议 1 的约束：**不宣称 M2 验收整体通过**，达成项与未达成项均在实施计划 §4.3 checkbox 与验收记录中逐项明示。
