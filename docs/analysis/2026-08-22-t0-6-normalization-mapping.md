# 归一化映射表：ExplorerCandidate → Candidate

> **任务编号**：T0.6
> **日期**：2026-08-22
> **依据**：方案 §2.5/§5.7、评审 §4.6（10 项 required 验证补充）、评审文档 `2026-08-22-t0-6-review.md`
> **用途**：T2.7 归一化实现的字段级规范；可执行契约见 `backend/tests/test_normalization_mapping.py`（`MAPPING`/`SEVERITY_KEYWORDS` 常量）。

---

## 1. 目标

将探索轨编排候选 `ExplorerCandidate` 归一化为现有规则候选形状（`schemas/candidate.schema.json`），使 validated 探索候选可并入现有 funnel/L2 链路。`rule_id` 固定 `EXPLORER_AGENT`（funnel identity 已排除该字段，不跨源合并）。

## 2. required 10 项映射

| # | candidate 字段 | 来源（ExplorerCandidate） | 转换方式 |
|---|---|---|---|
| 1 | `rule_id` | 常量 | `"EXPLORER_AGENT"` |
| 2 | `rule_version` | `prompt_version` 直通 | 探索协议版本（如 `explorer/1.0.0`） |
| 3 | `component` | `component.kind` 枚举映射 | activity/service/provider/receiver → 同值；`other` → **不产生候选**（T2.7 跳过并记录 `component_other_dropped`） |
| 4 | `severity_hint` | `chain_proposal.impact_proposal` 关键词启发式 | 关键词表 §5，默认 `medium`，初始档封顶 `high`；命中时 `blocking_gaps` 附 `EXPLORER_SEVERITY_HYPOTHESIS` |
| 5 | `confidence_tier` | `validation.status` 映射 | validated→`high`、partially_validated→`medium`、unverified→`low`、pending/None→`low` |
| 6 | `evidence_level` | 常量 | `"L2"` |
| 7 | `locations` | `chain_proposal.evidence_refs` 转换；空时 hops 定位 | 每条 → `{"artifact":"code","path":e.path,"line":e.line or e.end_line}`；空 → `{"artifact":"code","path":<hops[0] 解析 path>,"line":<hops[0].call_site_line>}`（近似定位） |
| 8 | `sources` | `chain_proposal.source` + `hops[0]` + `evidence_refs` | `[{"kind":"source_expression","status":"fact","path":<evidence_refs[0].path 或 hops[0] 解析 path>,"line":<evidence_refs[0].line 或 hops[0].call_site_line>,"text":source}]` |
| 9 | `sinks` | `chain_proposal.sink` + `hops[-1]` | `[{"kind":"sink_call","status":"fact","path":<hops[-1] 解析 path>,"line":<hops[-1].call_site_line>,"text":sink}]` |
| 10 | `blocking_gaps` | `validation` 组装（分支见 §4） | item 字段 `{code,message,critical,evidence_refs}`（对齐 `BlockingGap` 模型） |

## 3. 非 required 字段

- `title`：`"Explorer Candidate"`；`description`：`impact_proposal` 直通；`component_name`：`component.name`；
- `entry_points`：`[component.name]`；`entry_method_id`：`component.entry_method`；
- `authorization_status`：`"unknown"`；`dataflow_status`：`"not_proven"`；`guard_status`：`"unknown"`；`reachability_status`：`exported=True→reachable`、`False→conditional`；
- `analysis_status`：`"explorer_only"`；`deterministic_chain_verified`：`False`；
- `chain_id`：`candidate_id`；`prompt_version`/`model`：直通；
- **探索元数据不落 candidate**（`auxiliary` 为 boolean 不可承载）：`candidate_id`/`hypothesis`/severity 假设来源由 T2.7 经 `related_candidate_ids` 与探索产物关联。

## 4. blocking_gaps 组装分支（按序）

| 条件 | 产物 gap |
|---|---|
| `validation.notes` 非空 | `{"code":"EXPLORER_CHAIN_INCOMPLETE","message":notes,"critical":false,"evidence_refs":[]}` |
| `failed_hop_indices` 每条 i | `{"code":"EXPLORER_HOP_UNVERIFIED","message":"第 i 跳未通过 call_sites 回查","critical":false,"evidence_refs":[]}` |
| `custom_sink_proposal=True` | `{"code":"CUSTOM_SINK_PROPOSAL","message":"sink 未命中 taxonomy，待人工确认","critical":false,"evidence_refs":[]}` |
| `blocked_by_guard=True` | `{"code":"EXPLORER_GUARD_BLOCKED","message":"被 Guard/授权确定性阻断","critical":true,"evidence_refs":[]}` |
| severity 启发式命中 | `{"code":"EXPLORER_SEVERITY_HYPOTHESIS","message":"severity_hint 基于探索假设文本启发式，待 L2 复核","critical":false,"evidence_refs":[]}` |
| validated 且无上述 | `[]` |

## 5. severity 关键词启发式表

| 关键词（大小写不敏感，子串匹配，最小长度 ≥2） | severity_hint |
|---|---|
| `任意`、`远程`、`执行`、`泄露`、`敏感`、`提权`、`注入` | `high` |
| `拒绝服务`、`越权`、`绕过`、`数据` | `medium` |
| `信息`、`提示`、`低风险`、`暴露` | `low` |
| 未命中 | `medium`（默认） |

规则：**按表行序首个命中返回**（冲突时高优先级胜出）；初始档封顶 `high`（探索假设不直接判 `critical`，L2 复核确认后升级）；`root` 已删除（英文短词子串易误命中）。启发式结果仅作候选初始档位，不替代 L2 复核最终严重度。

## 6. method_id 解析规则

`from_method_id`/`to_method_id` 形如 `path#Class.method:line`：
- path 段：`split("#", 1)[0]`
- line 段：`rpartition(":")[2]`（解析失败时用 `call_site_line`）

## 7. 边界与风险

- `other` 组件 drop 而非强行映射（candidate 枚举无 other）；T2.7 记录审计。
- severity 启发式为低信任假设级映射，标记 `EXPLORER_SEVERITY_HYPOTHESIS`，最终由 L2 复核定档。
- 映射表变更必须同步更新 `test_normalization_mapping.py` 的 `MAPPING`/`SEVERITY_KEYWORDS`（防双源漂移）。
