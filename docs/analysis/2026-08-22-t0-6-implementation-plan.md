# 任务实施方案：T0.6（ExplorerCandidate → Candidate 归一化映射表）

> **任务编号**：T0.6
> **日期**：2026-08-22
> **依据大纲**：
> - 方案：`docs/analysis/2026-08-18-project-optimization-plan.md` §2.5（归一化映射）、§5.7（L2 链路复用）
> - 评审：`docs/analysis/2026-08-18-project-optimization-plan-review.md` §4.6（归一化工作量、10 项 required 验证补充）
> - 实施计划：`docs/analysis/2026-08-21-explorer-track-implementation-plan.md` T0.6
> - 前置：T0.2（`ExplorerCandidate` 模型）、T0.5（目标 `candidate.schema.json` 结构确认）
> **状态**：起草
> **前置依赖**：T0.2（已提交 `daf48cd`）

---

## 1. 任务目标与范围

- **目标**：固化"ExplorerCandidate → Candidate"**字段级归一化映射表**（覆盖 `candidate.schema.json` required 全部 10 项 + 关键非 required 字段），并配套可执行的引用完整性/枚举合法性测试。
- **范围**：
  - 映射表文档 `docs/analysis/2026-08-22-t0-6-normalization-mapping.md`（人类可读规范，含 severity 关键词表与 `other` 处理语义）；
  - `backend/tests/test_normalization_mapping.py`（映射表可执行断言：目标覆盖/来源存在/枚举合法/other 语义/severity 关键词）。
- **非范围**：归一化实现（`normalizer.py`，T2.7）；funnel 接入（T2.7）；`related_candidate_ids` 关联（T2.7）。

## 2. 现状锚点

- `ExplorerCandidate`（`ai_models.py`，T0.2）：`prompt_version/model/component(kind,name,exported,entry_method)/api_entry_ref/chain_proposal(source,sink,hops,evidence_refs,confidence,hypothesis,impact_proposal)/validation(status,notes,verified_hop_count,failed_hop_indices,blocked_by_guard,custom_sink_proposal)`。
- 目标 `schemas/candidate.schema.json`（2026-08-22 复核）：required 10 项 = `rule_id/rule_version/component/severity_hint/confidence_tier/evidence_level/locations/sources/sinks/blocking_gaps`；`severity_hint` 枚举 critical/high/medium/low/informational/pending；`confidence_tier` low/medium/high；`component` 枚举 activity/service/provider/receiver；`locations` items object（规则侧 `_base` 为 `{"artifact","path","line"}`）；`sources`/`sinks` 宽松数组（规则侧 `_evidence` 为 `{kind,status,path,line,text}`）；`blocking_gaps` 宽松数组。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `docs/analysis/2026-08-22-t0-6-normalization-mapping.md` | 新增 | 归一化映射表（T0.6 核心交付物） |
| `backend/tests/test_normalization_mapping.py` | 新增 | 映射表可执行断言（MAPPING 常量 + 5 组测试） |

### 3.2 归一化映射规则（§3.2 即交付物规范，固化于映射表文档）

| # | candidate 字段 | 来源（ExplorerCandidate） | 转换方式 | 说明 |
|---|---|---|---|---|
| 1 | `rule_id` | 常量 | `"EXPLORER_AGENT"` | pattern `^[A-Z0-9_]+$` 合规；funnel identity 已排除该字段 |
| 2 | `rule_version` | `prompt_version` 直通 | 探索协议版本（如 `explorer/1.0.0`） | T0.2 已定义 |
| 3 | `component` | `component.kind` 枚举映射 | activity/service/provider/receiver → 同值；**`other` → 不产生候选**（T2.7 跳过并记录 `component_other_dropped`） | candidate 枚举无 `other` |
| 4 | `severity_hint` | `chain_proposal.impact_proposal` 关键词启发式 | 关键词表（§3.3）+ 默认 `medium`；**启发式命中时在 `blocking_gaps` 附 `EXPLORER_SEVERITY_HYPOTHESIS` 标记**（评审 R-1：`auxiliary` 为 boolean 不可承载对象，severity 假设来源改由缺口标记） | 评审 §4.6 待定级规则；初始档封顶 `high`（L2 复核再升级 critical）；后续 L2 修正 |
| 5 | `confidence_tier` | `validation.status` 映射 | validated→`high`、partially_validated→`medium`、unverified→`low`、**pending/None→`low`**（评审 R-2：首包未校验场景） | candidate 枚举 low/medium/high |
| 6 | `evidence_level` | 常量 | `"L2"` | |
| 7 | `locations` | `chain_proposal.evidence_refs` 转换；空时用 hops 定位 | 每条 evidence_ref → `{"artifact":"code","path":e.path,"line":e.line or e.end_line}`；空 → `{"artifact":"code","path":<hops[0].from_method_id 解析 path>,"line":<hops[0].call_site_line>}`（**近似定位**，评审 R-5） | 对齐 `_base` locations |
| 8 | `sources` | `chain_proposal.source` + `hops[0]` | `[{"kind":"source_expression","status":"fact","path":<evidence_refs[0].path 或 hops[0] 解析 path>,"line":<evidence_refs[0].line 或 hops[0].call_site_line>,"text":source}]`（评审 R-5：行优先取证据，hop 仅 fallback） | 对齐 `_evidence` 结构 |
| 9 | `sinks` | `chain_proposal.sink` + `hops[-1]` | `[{"kind":"sink_call","status":"fact","path":<hops[-1] 解析 path>,"line":<hops[-1].call_site_line>,"text":sink}]` | 同上 |
| 10 | `blocking_gaps` | `validation` 组装 | validated→`[]`；否则按序：`notes`→`{"code":"EXPLORER_CHAIN_INCOMPLETE","message":notes,"critical":false,"evidence_refs":[]}`、`failed_hop_indices`→每条 `{"code":"EXPLORER_HOP_UNVERIFIED","message":"第 i 跳未通过 call_sites 回查","critical":false,"evidence_refs":[]}`、`custom_sink_proposal`→`{"code":"CUSTOM_SINK_PROPOSAL","message":"sink 未命中 taxonomy，待人工确认","critical":false,"evidence_refs":[]}`、`blocked_by_guard`→`{"code":"EXPLORER_GUARD_BLOCKED","message":"被 Guard/授权确定性阻断","critical":true,"evidence_refs":[]}`、severity 启发式命中→`{"code":"EXPLORER_SEVERITY_HYPOTHESIS","message":"severity_hint 基于探索假设文本启发式，待 L2 复核","critical":false,"evidence_refs":[]}`（评审 R-3/R-4：字段对齐既有 `BlockingGap` 模型 `{code,message,critical,evidence_refs}`） | 对齐规则侧 blocking_gaps 数组 |

**method_id 解析规则（评审 R-5，T2.7 实现）**：`from_method_id`/`to_method_id` 形如 `path#Class.method:line` → path 段取 `split("#", 1)[0]`、line 段取 `rpartition(":")[2]`（失败时 path 用 method_id 原串、line 用 `call_site_line`）。

**非 required 字段**（映射表一并声明，供 T2.7 补全候选完整性）：
- `title`：常量 `"Explorer Candidate"`；`description`：`chain_proposal.impact_proposal` 直通；`component_name`：`component.name` 直通；
- `entry_points`：`[component.name]`；`entry_method_id`：`component.entry_method`；
- `authorization_status`：`"unknown"`；`dataflow_status`：`"not_proven"`；`guard_status`：`"unknown"`；`reachability_status`：由 `component.exported` 映射（True→`reachable`，False→`conditional`）；
- `analysis_status`：`"explorer_only"`；`deterministic_chain_verified`：`False`；
- `chain_id`：`candidate_id` 直通；`prompt_version`/`model`：`prompt_version`/`model` 直通；
- **探索元数据不落 candidate**（评审 R-1）：`candidate_id`/`hypothesis`/`severity_hint` 假设来源由 T2.7 经 `related_candidate_ids` 与探索产物关联，不写入 `auxiliary`（该字段为 boolean）。

### 3.3 severity_hint 关键词启发式表（固化于映射表文档，T2.7 实现）

| 关键词命中（大小写不敏感，子串匹配，最小长度 ≥2） | severity_hint |
|---|---|
| `任意`、`远程`、`执行`、`泄露`、`敏感`、`提权`、`注入` | `high` |
| `拒绝服务`、`越权`、`绕过`、`数据` | `medium` |
| `信息`、`提示`、`低风险`、`暴露` | `low` |
| 未命中 | `medium`（默认） |

> 语义（评审 R-6）：`impact_proposal` 为低信任假设文本，启发式结果仅作候选初始档位，**初始档封顶 `high`**——探索假设不直接判 `critical`，L2 复核确认后再升级；关键词 `root` 已删除（英文短词子串易误命中如 uproot）；命中时 `blocking_gaps` 附 `EXPLORER_SEVERITY_HYPOTHESIS` 标记假设来源（`auxiliary` 为 boolean 不可承载）。优先级：按表行序判定，首个命中即返回。

### 3.4 测试方案（`backend/tests/test_normalization_mapping.py`）

测试内固化 `MAPPING` 常量（映射表的可执行形态：key=目标 candidate 字段，value=来源路径/常量/枚举映射/转换方式）：

1. **test_mapping_covers_all_required_candidate_fields**：加载 `schemas/candidate.schema.json`，`MAPPING.keys()` ⊇ required 10 项（缺失即失败）；
2. **test_mapping_sources_exist_in_explorer_candidate**：对每条 `source` 字段路径（如 `component.kind`、`chain_proposal.source`、`validation.status`），解析 `ExplorerCandidate.model_fields` 存在性（嵌套路径逐段验证）；
3. **test_mapping_constant_and_enum_values_valid**：常量值（`rule_id` 匹配 `^[A-Z0-9_]+$`、`evidence_level`="L2"）与枚举映射值（`component`/`confidence_tier`/`severity_hint` 落在 candidate.schema.json 对应枚举）校验；
4. **test_component_other_handling_declared**：`MAPPING["component"]` 的转换声明含 `other` 显式处理（drop 语义），且不产生非法枚举值；
5. **test_severity_keyword_rules**：`SEVERITY_KEYWORDS` 表逐条断言（样例文本命中→期望档位）+ 默认 `medium`；**按行序首个命中返回**，补冲突样例（文本同时含 high 与 low 关键词 → high 胜出；评审 R-7）；最小长度 ≥2 生效（"uproot" 不命中）；初始档封顶 `high`（含"任意代码执行"→`high` 而非 `critical`）；
6. **test_confidence_tier_pending_default**：`validation=None` 与 `validation.status="pending"` → `confidence_tier="low"`（评审 R-2）；
7. **test_blocking_gaps_assembly_spec**：`MAPPING["blocking_gaps"]` 声明覆盖 notes/failed_hop_indices/custom_sink_proposal/blocked_by_guard/severity 假设五类分支，item 字段含 `message`（对齐 `BlockingGap`，评审 R-3/R-4）；
8. **test_locations_fallback_with_empty_evidence_refs**：`evidence_refs=[]` 时用 `hops[0].from_method_id` 解析 path/line 的 fallback 断言（评审 R-8）；`method_id` 解析（`split("#",1)[0]` path、`rpartition(":")` line）样例断言。

### 3.5 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性说明 |
|---|---|---|
| 方案 §2.5 归一化映射（10 项 required 来源） | §3.2 映射表逐项对应 | 一致 |
| 评审 §4.6 验证补充（severity_hint←impact_proposal、confidence_tier←三档、evidence_level 固定 L2、locations←evidence_refs） | §3.2 第 4/5/6/7 行 | 一致 |
| 评审 §5.7（Agent1 提出、L2 定状态） | severity 启发式标记 hypothesis、最终由 L2 复核修正 | 一致 |
| 方案 §2.5（`component` 归一化、`blocking_gaps` 三档填充） | §3.2 第 3/10 行（含 `other` drop 语义） | 细化 |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| 映射表与 T2.7 实现漂移 | 归一化行为不一致 | 测试内 MAPPING 常量作为可执行契约，T2.7 实现须过此测试 | 修订映射表+测试同步 |
| severity 启发式误判 | 候选严重度初始档偏高/偏低 | `auxiliary.explorer_severity_hypothesis` 标记假设来源；L2 复核修正 | 收紧关键词表 |
| `other` 组件被静默丢弃 | 探索候选丢失 | T2.7 显式记录 `component_other_dropped` | 映射表声明 drop 语义 + 记录 |

## 5. 依赖

- 前置：T0.2（已提交）；依赖 `schemas/candidate.schema.json`（既有）与 `ExplorerCandidate` 模型字段（T0.2）。
