# 任务实施方案：T2.7 归一化 + funnel 扩展

> **任务编号**：T2.7
> **日期**：2026-08-22
> **依据大纲**：`docs/analysis/2026-08-18-project-optimization-plan.md` §2.5（探索候选校验与合流）、§2.6（Funnel 扩展）、§2.0（接入原则：related_candidate_ids 关联不合并 identity）、§4.8（S1–S11 承接）；实施计划 `docs/analysis/2026-08-21-explorer-track-implementation-plan.md` T2.7 行
> **状态**：已修订（第 1 轮评审 R-1~R-7 全部采纳，评审文档 `2026-08-22-t2-7-review.md`）
> **前置依赖**：T2.6（已完成 cfc2a32：三档校验 explorer_validation.py）；T0.6（已完成：归一化映射表 + 可执行契约 `backend/tests/test_normalization_mapping.py`）

---

## 1. 任务目标与范围

- **目标**：探索轨 `validated` 候选归一化为正式 Candidate 形状并入 funnel 主链（探索轨与主链合流的最后一环）；funnel 支持 `candidate_source` 与 `explorer_promoted` / `explorer_partial` / `explorer_unverified` 三分流 disposition；identity 含 `candidate_source` 不跨源合并；同链规则候选以 `related_candidate_ids` 关联。
- **范围（in scope）**：
  1. 归一化模块 `backend/app/analysis/explorer_normalization.py`：validated ExplorerCandidate → Candidate（T0.6 映射表 10 项 required + 非 required 字段全量落地）；
  2. funnel 扩展 `backend/app/analysis/candidate_funnel.py`：`candidate_source`（rule/explorer/manual）+ 三分流 disposition 路由 + identity 不跨源 + `related_candidate_ids` 身份排除 + summary 计数；
  3. orchestrator 时序调整：explorer 阶段从"AI 阶段后"前移至"candidate_funnel 前"，归一化候选并入主链 candidates，funnel 后回填 `related_candidate_ids`；
  4. `schemas/candidate.schema.json` 显式定义 `candidate_source` 属性；
  5. 测试：归一化映射 / funnel 三分流 / identity 分源 / related 关联 / 集成时序。
- **非范围（out of scope）**：
  - `explorer_deep_dive`（T2.8）：partial 候选的深挖执行不在本任务，本任务只保证 partial 分流位（`explorer_partial` disposition 路由语义完备）；
  - `custom_sink_proposal` 的 taxonomy 命中判定（**边界决策 D1**，见 §3.6）；
  - 前端人工队列展示（T2.10）；
  - manual 候选导入实现（仅枚举预留）；
  - `ExplorerSettings.auto_promote`（T0.7 预留字段）：本任务实现的"validated 归一化 → funnel → L2 复核"即默认值 `false` 的行为；`auto_promote=true`（跳过 L2 直接升入正式候选池）不实现，字段保持预留。

## 2. 现状锚点

- **explorer 阶段位置**：`backend/app/analysis/orchestrator.py:277-281`（AI 阶段后独立运行，候选只落盘）——T2.5b 的临时安排，注释已注明"T2.7 归一化后入 funnel"；
- **`_run_explorer_stage`**：`orchestrator.py:888-956`（入口遍历 → 检索循环 → T2.6 三档校验 → `save_candidates` 落盘全三档）；
- **三档校验**：`backend/app/analysis/explorer_validation.py`（`validate_explorer_candidates` 原地填 `validation` 字段）；
- **funnel**：`backend/app/analysis/candidate_funnel.py`（`CandidateFunnel.process`：identity 三键分组 + `deterministic_precheck` disposition + AI 路由）；
- **可执行契约**：`backend/tests/test_normalization_mapping.py` 的 `MAPPING` / `SEVERITY_KEYWORDS`（T0.6 冻结，T2.7 实现必须满足）；
- **可复用能力**：
  - `guard_blocked` 字段语义（规则主链：`apply_guard_verification` 写入 → funnel `_pipeline_requires_ai` 不送 AI → decision 判 blocked）——探索候选的 `validation.blocked_by_guard` 直接转换复用该语义；
  - path 口径处理模式：`api_surface.py:275-284` 的条件式 `sources/` 前缀剥离；
  - jsonschema 校验模式：`test_explorer.py:173-174`（候选落盘 schema validate）；
  - identity 排除机制：`_PIPELINE_IDENTITY_EXCLUDED_FIELDS` / `_TOP_LEVEL_MUTABLE_FIELDS` 既有模式。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/analysis/explorer_normalization.py` | 新增 | 归一化（validated → Candidate）+ related_candidate_ids 关联 + `SEVERITY_KEYWORDS` 常量（生产侧单一事实源，评审 R-5） |
| `backend/app/analysis/candidate_funnel.py` | 修改 | candidate_source 三分流 disposition、identity 分源、排除字段、summary |
| `backend/app/analysis/orchestrator.py` | 修改 | explorer 阶段前移至 funnel 前；**删除 `_run_ai_stage` 内 `_ai_requests_used = 0` 重置（orchestrator.py:484，评审 R-1——每 run 新建实例已保证隔离，重置使前移后 run 级预算上限翻倍）**；归一化候选并入；funnel 后 related 回填；stage summary 扩展 |
| `schemas/candidate.schema.json` | 修改 | properties 显式加 `candidate_source`（enum rule/explorer/manual） |
| `docs/analysis/2026-08-22-t0-6-normalization-mapping.md` | 修改 | §3 description 行（不写，评审 R-2）与 §4 notes 分支条件收紧（评审 R-4）——契约同步（映射表 §7 纪律） |
| `backend/tests/test_normalization_mapping.py` | 修改 | `SEVERITY_KEYWORDS` 改为从生产模块 import 断言（依赖方向反转，评审 R-5）；notes 分支契约同步（R-4） |
| `backend/tests/test_explorer_normalization.py` | 新增 | 归一化映射 + 关联测试 |
| `backend/tests/test_candidate_funnel.py` | 修改 | 三分流 / 分源 identity / related 排除测试 |
| `backend/tests/test_explorer.py` | 修改 | 集成测试：阶段时序 + 归一化并入（扩展现有 `test_orchestrator_explorer_stage` 形态或新增） |

### 3.2 接口/数据结构设计

```python
# explorer_normalization.py

def normalize_explorer_candidates(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """T2.6 校验后的 ExplorerCandidate 列表 → (归一化 Candidate 列表, 计数摘要)。

    只归一化 validation.status == "validated" 且 component.kind != "other" 的候选；
    partial / unverified / other 不产出（分别由 explorer/candidates.json 人工队列
    与 T2.8 deep_dive 消费）。计数：{validated_total, normalized,
    component_other_dropped, guard_blocked_promoted, partial_kept, unverified_kept}。
    """

def link_related_candidates(candidates: list[dict[str, Any]]) -> dict[str, int]:
    """funnel 后回填：探索归一化候选与规则候选同链时双向写 related_candidate_ids。

    返回 {explorer_linked, rule_candidate_linked, pair_count}。
    """
```

**归一化候选字段**（T0.6 映射表 §2/§3 全量 + 关联扩展字段）：

| 字段 | 值 | 来源 |
|---|---|---|
| required 10 项 | 按 T0.6 映射表逐项（rule_id=`EXPLORER_AGENT`、rule_version=prompt_version、component 枚举映射、severity_hint=启发式封顶 high、confidence_tier←三档、evidence_level=`L2`、locations←evidence_refs/hops、sources/sinks←链首尾） | 映射表 §2 |
| `candidate_source` | `"explorer"` | 本任务新增（方案 §2.6） |
| `explorer_candidate_id` | ExplorerCandidate.candidate_id（`expl_…`） | 探索产物关联（T0.6 §3"探索元数据关联"） |
| `explorer_validation_status` | `validation.status` | funnel 三分流依据 |
| `guard_blocked` / `guard_blocks` | `validation.blocked_by_guard=True` 时**同写**：`guard_blocked=True` + `guard_blocks=[{"type":"debuggable","path":<hops[0] 解析 path>,"line":<hops[0].call_site_line>,"method":<hops[0] 解析方法名>}]` | 复用主链双字段语义（评审 R-3：funnel `_pipeline_requires_ai` 读顶层布尔跳 AI；decision.py:880-884 判 blocked 只认 `guard_blocks` 列表；`apply_guard_verification` 契约"两字段必须同写同删"，guard_verifier.py:209-213）。blocked 元素形状对齐 `verify_candidate_guards` 产物（guard_verifier.py:188-193） |
| `title` | `"Explorer Candidate"`（常量，来源提示） | 映射表 §3 修订（评审 R-2） |
| `description` | **不写（留空）**——与规则候选同构，AI 完成后由 `_apply_ai_analysis` 以 analysis.summary 回填（orchestrator.py:1100-1101 既有机制）；impact_proposal 留在探索产物侧供 T2.10 人工视图 | 评审 R-2：`_candidate_summary` 白名单含 title/description（context_builder.py:944），直通 impact_proposal 会锚定 L2 复核，违背大纲 §2.5"独立裁决不受 Agent1 hypothesis 左右"。severity_hint/confidence_tier 仍在白名单但属已声明假设级（附 EXPLORER_SEVERITY_HYPOTHESIS gap / 三档校验事实），可接受 |
| `sinks[].method_id` | `hops[-1].to_method_id` | 映射表 §2 #9 的实现细化：供 related 匹配（sink 方法级口径，与 M2 验收"同一链"判定一致） |
| 其余非 required | `component_name`/`entry_points`/`entry_method_id`/`authorization_status="unknown"`/`dataflow_status="not_proven"`/`guard_status="unknown"`/`reachability_status←exported`/`analysis_status="explorer_only"`/`deterministic_chain_verified=False`/`chain_id=candidate_id`/`prompt_version`/`model` | 映射表 §3 |

**funnel 扩展**（`candidate_funnel.py`）：

```python
DISPOSITIONS = {..., "explorer_promoted", "explorer_partial", "explorer_unverified"}

_EXPLORER_DISPOSITION_BY_STATUS = {
    "validated": "explorer_promoted",
    "partially_validated": "explorer_partial",
    "unverified": "explorer_unverified",
    "pending": "explorer_unverified",  # 未校验保守归 unverified
}
# explorer_validation_status 缺失/未知 → explorer_unverified（保守不送 AI）
```

- `process()` 中：`candidate_source == "explorer"` 的候选 `funnel_disposition` 由上表映射覆盖（不走 `deterministic_precheck`）；其余候选（含缺省）走原逻辑，**行为零变化**；
- AI 路由：`explorer_promoted` → 与规则 L2 候选同等（`_pipeline_requires_ai`：guard_blocked 优先短路，L2 且 `deterministic_chain_verified≠True` → ai_required=True）；`explorer_partial` / `explorer_unverified` → ai_required=False（不送 AI、不占 AI 预算）；
- identity：`candidate_source` **不加**排除字段——facts 投影自动纳入 → `deterministic_fact_hash` 分源，探索候选与规则候选永不同组（方案 §2.6"identity 计算包含 candidate_source"）；探索候选间语义相同提案（同 hops 派生形状）仍可同组合并复核；
- `_PIPELINE_IDENTITY_EXCLUDED_FIELDS` += {`related_candidate_ids`, `explorer_candidate_id`}（写回/追溯字段不参与身份，否则 funnel 后回填导致 `_pipeline_identity_compatible` recompute 不一致）；`_TOP_LEVEL_MUTABLE_FIELDS` += 同两字段（exact key 排除）；
- `explorer_validation_status` **保留在 facts**（档位不同=链的校验事实不同，不应合并）。

**related_candidate_ids 同链判定**（M2 验收 §4.3.1"同一链"口径：source 组件一致且 sink 方法一致）：

- 前提：候选已经 funnel 处理（`candidate_id` 已生成）；
- 匹配：`component_name` 相等 **且** sink 方法级匹配——归一化候选 `sinks[0].method_id`（`hops[-1].to_method_id`）与规则候选任一 sink 的 `method_id` 相等；两侧缺 `method_id` 时退化 `(path, line)` 精确相等；
- 动作：探索候选写 `related_candidate_ids` += 规则候选 `candidate_id` 列表，规则候选写探索候选 id；幂等（重复调用不重复追加）；
- 多对多：一条探索候选可关联多条规则候选（反之亦然），全量扫描 O(n×m) 但候选量级（数百）可接受。

### 3.3 算法/流程要点

**orchestrator 时序调整**（`_run` 主流程）：

```text
rule_prescan → guard 验证（现有，仅规则候选）
→ api_surface（可选，现有）
→ explorer（可选，前移）：检索循环 → T2.6 三档校验 → save_candidates(全三档落盘)
   → normalize_explorer_candidates(仅 validated) → 返回归一化列表
→ candidates.extend(归一化候选)
→ candidate_funnel（规则 + 探索合流；探索候选三分流 disposition）
→ link_related_candidates(candidates)（funnel 后：candidate_id 已生成）
→ code_slicing → ai_analysis → evidence → decision → aggregate（现有，无改动）
```

- `_run_explorer_stage` 签名变更：返回 `list[dict]`（归一化候选）；内部在 `validate_explorer_candidates` 与 `save_candidates` 之后调用 `normalize_explorer_candidates`（纯字段变换，不依赖 reader 存活）；
- **删除 `_run_ai_stage` 内 `self._ai_requests_used = 0` 重置**（orchestrator.py:484，评审 R-1：每 run 新建实例已保证隔离；详见 §3.5 预算语义）；
- stage summary 扩展：现有 `validation_counts` 基础上增加 `normalization_counts`；`ai_analysis` stage summary 的 `requests_used` 保持 run 累计口径并分列 `explorer_requests_used` / `ai_stage_requests_used`（评审 R-1）；
- 原"AI 阶段后"的 explorer 块删除（整体前移，无重复执行）。

**归一化边界处理**：

- `component.kind == "other"`：drop + `component_other_dropped` 计数（映射表 §2 #3：candidate 枚举无 other）；
- `validation` 缺失或 `status != "validated"`：跳过（partial_kept / unverified_kept 计数）；
- `evidence_refs` 为空：locations 回退 `hops[0]` 定位（映射表 §2 #7：`from_method_id` 解析 path + `call_site_line`）；
- `evidence_refs[].path` 带 `sources/` 前缀：条件式剥离（`removeprefix("sources/")`，对齐 `api_surface.py:281` 模式与索引 path 口径 `indexer.py:109`——无前缀相对路径）；hops 派生 path 与 `files.path` 同源（T2.6 评审认可），不剥离；
- 单候选归一化异常（字段畸形）：跳过该候选 + LOGGER.warning + 计数 `normalization_errors`，不中断批次（阶段主链保护，同 T2.6 模式）；
- **severity 关键词启发式**：`SEVERITY_KEYWORDS` 常量**定义于生产模块** `explorer_normalization.py`（评审 R-5：生产代码不得 import `backend/tests` 模块——tests 不随生产包分发）；`test_normalization_mapping.py` 改为从生产模块 import 并保留契约断言（依赖方向反转：测试依赖生产，防漂移纪律不降级）；按行序首个命中、未命中默认 medium、封顶 high（命中时附 `EXPLORER_SEVERITY_HYPOTHESIS` gap）；
- **blocking_gaps 组装**：按映射表 §4（修订版）分支序：notes→`EXPLORER_CHAIN_INCOMPLETE`（**仅当 status != validated 或 notes 含异常语义**——评审 R-4：T2.6 实现中 validated 候选 notes 恒非空（"N/N 跳回查通过"，explorer_validation.py:80），纯成功摘要不产 gap，保映射表 §4 末行"validated 且无上述→[]"可达）、failed_hop_indices→`EXPLORER_HOP_UNVERIFIED`、custom_sink_proposal→`CUSTOM_SINK_PROPOSAL`（T2.6 现状恒 false，见 D1）、blocked_by_guard→`EXPLORER_GUARD_BLOCKED`(critical)、severity 启发式命中→`EXPLORER_SEVERITY_HYPOTHESIS`）；item 字段 `{code,message,critical,evidence_refs:[]}` 对齐 `BlockingGap` 模型。

**funnel 三分流实现要点**：

- 探索分支集中在 `process()` 的 disposition 赋值处与 ai_required 赋值处，改动点小且显式；
- `explorer_partial` / `explorer_unverified` 候选保留在 candidates 列表与产物（可审计），仅不送 AI——**注意**：T2.7 运行时只有 validated 进入 funnel（归一化过滤），partial/unverified 的 disposition 路由是**能力层交付**（funnel 对三种 status 的路由语义完备 + 测试覆盖），T2.8 deep_dive 与 T2.10 人工队列直接消费；
- 探索候选不进 L1 预算逻辑（evidence_level=L2，天然走 L2 路径）。

### 3.4 与大纲一致性对照

| 大纲条目（引用） | 本方案实现方式 | 一致性说明 |
|---|---|---|
| §2.5 归一化：validated → Candidate（10 项映射） | `normalize_explorer_candidates` 按 T0.6 映射表逐项落地 | 不变（映射表为大纲 §2.5 的字段级固化） |
| §2.5 合流点在 candidate_funnel 之前（合流图） | explorer 阶段前移至 funnel 前，归一化候选 extend 进 candidates | 不变（T2.5b"AI 阶段后"是 T2.7 前的临时安排，其注释已预告本次前移） |
| §2.5 最终判定复用 L2 链路 | 归一化候选 evidence_level=L2 → funnel L2 路由 → 切片 → L2 复核 → evidence → DecisionEngine | 不变（主链零改动，探索候选按规则 L2 候选同等路由） |
| §2.6 candidate_source（rule/explorer/manual） | funnel 显式分支 + schema 枚举定义；规则候选缺省不写（语义=rule） | 不变 |
| §2.6 三分流 disposition | `_EXPLORER_DISPOSITION_BY_STATUS` 映射 + ai_required 路由 | 不变 |
| §2.6 identity 含 candidate_source | candidate_source 进 facts 投影（不加排除）→ deterministic_fact_hash 分源 | 不变 |
| §2.0/§4.8 related_candidate_ids 关联不合并 identity | `link_related_candidates`（component_name + sink 方法匹配）；identity 排除字段保 recompute 一致 | 不变 |
| §2.4 预算归属：探索检索占探索预算（记账） | T2.5b 已分记 `ai_requests_used`/`read_requests_used`；本任务删除计数重置保证探索与规则 AI 共享同一 run 级预算池（评审 R-1，§3.5） | 细化（记账沿用；预算总量语义按共享池落地） |

### 3.5 时序前移的影响分析（设计论证）

- **方案依据**：§2.5 合流图明确"探索候选（经过三档校验）→ CandidateFunnel.process() → 现有 code_slicing / ai_analysis / evidence / aggregation"——单 run 内闭环要求探索候选在 funnel 前产生；
- **AI 预算语义**（前移的必然配套，评审 R-1）：`_run_ai_stage` 内 `self._ai_requests_used = 0` 重置（orchestrator.py:484）必须**删除**——`ScanOrchestrator` 每 run 新建实例（`__init__` 归零已保证隔离），重置在"探索先跑"时序下会使探索消耗与规则 AI 各享一份全额预算（run 总量上限 ≈ 2×`max_requests_per_run`），架空 T2.5b"防绕过计费"决定。删除后语义：**探索与规则 AI 共享同一 run 级预算池，探索优先消耗**——规则候选 L2 复核可用预算 = max − 探索消耗。`ai_analysis` stage summary 的 `requests_used` 保持 run 累计口径（=探索+规则 AI；它是 T1.3 batch 预算的持久化事实源，orchestrator.py:612），并分列 `explorer_requests_used` / `ai_stage_requests_used` 供审计；缓解：①探索轨默认关闭，开启属显式 opt-in；②探索自身有 `max_rounds_per_entry=4`/`max_requests_per_entry=20`/`max_candidates_per_run=50` 有界预算；③分账记账实测后可评估是否需要探索子预算帽（**超出本任务范围**，记录为 T2.8+ 观察项）；
- **阶段顺序变更的回归面**：`run_manifest.stages` 顺序变化（explorer 从最后段移到中段）；现有测试仅断言 stage 存在与产物存在（`test_orchestrator_explorer_stage`），无顺序断言；默认关闭时零影响。

### 3.6 边界决策记录

| 编号 | 决策 | 理由 | 状态 |
|---|---|---|---|
| D1 | `custom_sink_proposal` taxonomy 命中判定**延后至 T2.9**，T2.7 维持 T2.6 现状（保守 false） | taxonomy 判定逻辑在 rules 侧（`rules/shared/dataflow.py` `classify_operation_taxonomy`），backend → rules 零依赖红线（M2 验收 4.3.5）禁止 import；T2.9 任务本体即"taxonomy 版本化文件"，届时以版本化文件作为 backend 可读数据源接通判定，与本任务不重复建设 | 待评审确认（T2.6 评审移交项①） |
| D2 | `auto_promote` 不在本任务消费（保持 T0.7 预留） | 默认 false 的语义（走 L2 复核）即本任务实现的默认路径；true（跳过复核直接升入）与"探索轨低信任"原则冲突，方案未定义其启用条件，不擅自实现 | 待评审确认 |
| D3 | partial / unverified 候选**不进主链 candidates 列表**（留在 `explorer/candidates.json`） | M2 验收 4.3.2 硬约束"未通过校验的探索候选 0 条进入正式 finding"；方案 §5.3 流程图：partial → deep_dive（T2.8，输入需 ExplorerCandidate 形状含 hops）/ unverified → 人工队列——两者主存留地在探索产物 | 按方案执行 |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| R-1 探索 AI 调用前移后与规则候选共享同一 run 级预算池，探索优先消耗 | 规则候选 L2 复核可用预算减少（AI 分析降级/skip 增多） | 删除 `_run_ai_stage` 内计数重置（评审 R-1，§3.5）保证总量不突破；探索默认关闭 + 有界预算；stage summary 分列 `explorer_requests_used`/`ai_stage_requests_used` 可审计；M2 验收成本口径实测后评估探索子预算帽 | `explorer.enabled=false` 一键回退（回退时还原 orchestrator.py:484 重置删除） |
| R-2 探索假设文本泄入 L2 复核输入（锚定） | L2 复核独立性受损（确认偏误） | 归一化不写 description（评审 R-2）；title 用常量；severity_hint/confidence_tier 属已声明假设级（gap 标注） | 无需回退（字段留空即规则候选同构） |
| R-3 `value_flow_reaches_sink_argument` 恒 False（归一化候选无 flow_kind），AI `in_process_terminus` 反证被交叉验证采信 | 探索候选可能被过快标 ai_false_positive | 该事实对探索候选是诚实的（链未经 taint 引擎证明值流）；L2 复核对切片的裁决不受影响；**观察路径（评审 R-6）：M2 验收覆盖指标实测时记录探索候选 ai_false_positive 占比** | 无需回退（行为符合确定性红线：AI 反证 + 确定性事实联合裁决） |
| R-4 时序前移改动 `_run` 主流程，波及既有阶段编排 | run 主链回归风险 | 全量回归门禁（1029 基线）；默认关闭路径 diff 为空断言；预算语义修订配套验收 A-18 | 阶段块整体回退（explorer 块移回原位 + 还原计数重置删除） |
| R-5 related_candidate_ids 匹配漏报（规则候选 sink 无 method_id 时退化 (path,line) 精确匹配） | 关联不全（人工视图对照缺） | 关联是增强非判定路径，漏报不产生错误结论；匹配口径与 M2 验收"同一链"判定一致，测试固化 | 无需回退 |
| R-6 归一化候选切片构建空转（anchors path 不命中索引） | L2 复核输入为空切片，AI 无法判定 | path 剥离对齐索引口径；sinks path 与 `files.path` 同源；空切片有 limitations 标注（context_builder 已兼容，见 §2 锚点）；归一化 candidates 落盘可审计 | 归一化候选不阻断主链（extend 失败不影响规则候选） |

## 5. 依赖

- 前置任务：T2.6（cfc2a32）、T0.6（映射表与可执行契约）、T2.5b（explorer 阶段与预算回调）
- 需要的输入产物：T2.6 校验后的 `ExplorerCandidate`（含 `validation` 字段）；`schemas/candidate.schema.json`；`test_normalization_mapping.py` 的 `MAPPING`/`SEVERITY_KEYWORDS` 常量
