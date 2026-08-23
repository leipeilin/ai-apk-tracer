# 任务实施方案：M3-2（report prompt 协议——AI 报告草稿真协议接入）

> **任务编号**：M3-2
> **日期**：2026-08-23
> **依据**：M3-1 方案 §6 遗留（"真 prompt 协议：provider 换实现——衔接点已锁定"）+ M3-1 评审 R-3（大纲 T3.2 探索假设描述种子——数据源就绪时补）+ 实施计划 §4.4 T3.1
> **状态**：起草
> **前置**：M3-1 ✅（provider 抽象 + `test_provider_injection_point` 锁定衔接点）

---

## 1. 任务目标与范围

把 M3-1 的投影 provider 升级为**真 AI 报告协议**：注册 `prompts/report/1.0.0/`，`OpenAICompatibleAnalyzer.report_entry`（照抄 verify_entry 状态机），generator 接真 provider（provenance="ai_report_protocol"），并兑现大纲 T3.2 的**探索假设描述种子**（ReportInput 携带 finding 的假设层字段——M3-1 评审 R-3 处置）。

**范围**：
1. `backend/app/analysis/ai_models.py`——`ReportInput`/`ReportDraftOutput`（StrictAIModel）+ 注册 AI_MODEL_REGISTRY/AI_SCHEMA_MODELS；
2. `backend/app/analysis/ai.py`——`report_entry`（复用 `_invoke_prompt("report", "1.0.0", ...)`）；
3. `prompts/report/1.0.0/{system.md,user.md}`——严格输出契约（M2 收尾-2 verify 重写模式：字段逐字声明+枚举+禁附加字段——schema_invalid 教训前置）；
4. `prompts/registry.yaml` + `schemas/`——sync --write 注册（含 config 声明对齐测试）；
5. `backend/app/config.py`——ReportSettings.prompt_version（先声明后注册——T0.9 原则）；
6. `backend/app/reporting/generator.py`——真 provider（AI 失败回退投影——降级不阻塞，沿 verify fallback 语义）+ 大纲 T3.2 种子（ReportInput 从 finding 投影假设层字段：hypothesis/impact_proposal/component_summary——M1 规则轨 finding 无这些字段时为 null）；
7. 测试 + 真实 V-01 端到端。

**非范围**：UI（T3.4 后置）；routes 端点不变（provider 内部升级对 API 零改动）；核验/探索协议不动。

## 2. 现状锚点（全部实读核验）

- registry 条目结构（registry.yaml 首条 preflight 实读）：id/version/system_file/user_file/allowed_placeholders/input_model/output_model/schema files + sync 生成的四组哈希；
- `AI_MODEL_REGISTRY`（ai_models.py:749）/`AI_SCHEMA_MODELS`（:773）——新模型加入两个 dict；
- `_invoke_prompt(prompt_id, version, model_input, output_model, track)`（ai.py:497）——verify_entry 即此模式（ai.py:477-495）；
- schema 生成位置：`schemas/ai_*.schema.json`（sync 脚本管理）；
- user.md 模式（verify 实读）：防注入声明一句 + `{verify_input_json}` 占位符；
- M3-1 的 `reporting/models.ReportDraft` 是普通 BaseModel——**AI 协议模型独立命名 `ReportDraftOutput`**（StrictAIModel），provider 桥接转换（避免与 reporting 域模型耦合）；
- `_prompt_variable("report")` → `report_input_json`（惯例自动派生）。

## 3. 详细实现方案

### 3.1 AI 协议模型（ai_models.py）

```python
class ReportInput(StrictAIModel):
    """报告草稿输入（M3-2：confirmed finding 的结构化投影 + 探索假设种子——大纲 T3.2）。"""
    finding_id: Identifier
    rule_id: ShortText
    component_name: ShortText
    severity_hint: ShortText | None          # 规则确定性提示
    deterministic_summary: LongText          # finding 的确定性事实摘要（sources/sinks/guard 等）
    explorer_hypothesis: LongText | None     # 探索假设种子（T3.2——规则轨 finding 为 None）
    explorer_impact_proposal: LongText | None
    explorer_component_summary: LongText | None
    evidence_refs: list[EvidencePointer 兼容结构]  # 轻量（path/line/note）
    l2_review: L2 复核结论投影（verdict/confidence_tier/flaw_holds/harm）

class ReportDraftOutput(StrictAIModel):
    """报告草稿输出（严格契约）。"""
    summary: LongText
    vulnerability_narrative: LongText
    exploit_scenario: LongText
    confidence_tier: Literal["low", "medium", "high"]
    analysis_complete: bool
```

（EvidencePointer 复用 reporting.models 还是独立？——AI 协议侧独立 `ReportEvidenceRef`（path/line/end_line/note）避免跨域依赖。）

### 3.2 analyzer 方法（ai.py）

`report_entry(model_input: ReportInput) -> dict`——照抄 verify_entry（`_invoke_prompt("report", "1.0.0", model_input, ReportDraftOutput, "report")`）。

### 3.3 prompt（严格契约——schema_invalid 教训前置）

system.md 结构（verify 重写版同款）：职责一句 + 硬约束（只输出一个 JSON/字段名逐字/禁附加字段/必填全给/诚实标注不确定/AI 与确定性分离——叙述须基于输入事实不得虚构引用）+ 输出契约区（ReportDraftOutput 逐字段：summary/narrative/exploit_scenario/confidence_tier 枚举/analysis_complete）+ 输入说明（deterministic_summary 为可信任事实；explorer_* 为低信任假设种子——引用时标注；l2_review 为独立复核结论）。

user.md：防注入声明 + `{report_input_json}`。

### 3.4 generator 真 provider + 降级

```python
async def ai_report_provider(finding) -> ReportDraft:
    """真协议 provider：analyzer.report_entry(ReportInput 投影) → ReportDraft。
    AI 失败（schema_invalid/network/fatal）→ 回退投影 provider（降级不阻塞，
    provenance 保持 projected_from_l2_review——诚实标注）。"""
```

- `generate_report_document` 加 `analyzer: OpenAICompatibleAnalyzer | None` 参数（None = 投影模式——测试/离线）；
- 投影构造 ReportInput（探索种子三字段：candidate_source=="explorer" 的 finding 取其假设层字段——规则轨 None）。

### 3.5 文件变更清单

| 文件 | 变更 |
|---|---|
| ai_models.py | ReportInput/ReportDraftOutput/ReportEvidenceRef + 两 dict 注册 |
| ai.py | report_entry |
| prompts/report/1.0.0/{system.md,user.md} | 新建（严格契约） |
| registry.yaml + schemas/ | sync --write |
| config.py | ReportSettings.prompt_version="report/1.0.0" |
| reporting/generator.py | ai_report_provider + ReportInput 投影 + 降级回退 |
| tests | 模型注册/prompt 契约断言/provider 降级/端到端（fake + 真实 V-01） |

### 3.6 风险与回退

| 风险 | 对策 | 回退 |
|---|---|---|
| AI 输出 schema_invalid（report 是新协议） | prompt 严格契约前置（verify 教训）+ repair 状态机已有 + **降级回退投影**（报告永不因 AI 阻塞） | provider 传 None |
| 大纲 T3.2 种子字段规则轨缺失 | 可选 None + prompt 说明（种子仅 explorer 来源） | — |
| provenance 语义漂移 | 成功=ai_report_protocol；降级=projected_from_l2_review（测试断言） | — |

## 4. 与大纲一致性

T3.1（报告 prompt 协议）本任务完成；T3.2 种子补齐（评审 R-3 处置闭环）；落点偏差（reporting/ vs findings/report_generator.py）随 M3-2 回写大纲（验收记录注明）。
