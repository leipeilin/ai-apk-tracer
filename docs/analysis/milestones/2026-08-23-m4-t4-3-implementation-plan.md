# 任务实施方案：M4-T4.3（报告质量检查）

> **任务编号**：M4-T4.3
> **依据**：实施计划 §3.5 T4.3（"报告质量检查：AI/确定性内容混淆检测、引用回查、PoC 骨架一致性"——依赖 M3 ✓）
> **状态**：起草
> **前置**：M3-1/M3-2 ✅（ReportDocument 产物 + provenance 双值 + PoCSkeleton）

## 1. 目标与范围

新增 `backend/app/evaluation/report_quality.py`——对 ReportDocument（dict 形态）执行三项质量检查，输出结构化结果（供 T4.4 门槛与人工复核消费）：

1. **AI/确定性分离检查**：ai_draft 与 deterministic 键集无交叉混入；provenance ∈ 合法枚举；ai_draft 文本中不得出现"冒充确定性"的表述（简化：无 `deterministic` 字段名内嵌）；
2. **引用回查**：ai_draft.evidence_refs（ReportDocument 的确定性投影——M3-2 设计即防虚构）与 deterministic.sources/sinks 的 path/line 一致性——每条 pointer 的 path 在 deterministic 投影的 sources/sinks 中存在同 path 条目；
3. **PoC 骨架一致性**：executable_files_created == []；command_skeleton 全占位符或注释形态（含 `<` 或 `#` 前缀）；kind 与 component_kind 映射合理（binder_transaction → service、intent/uri → activity、provider_query → provider、broadcast → receiver）。

**范围**：`report_quality.py`（check_report_document(document: Mapping) -> dict：checks 三项 + verdict PASS/WARN/FAIL）+ 测试。**非范围**：T4.4 门槛接线；批量扫描 CLI（T4.4 一并）。

## 2. 现状锚点

- ReportDocument 结构（reporting/models.py）：ai_draft{summary/narrative/exploit_scenario/confidence_tier/provenance/prompt_version/model/analysis_complete/(fallback)}、deterministic{26 键投影}、poc_skeleton{component_kind/kind/steps/command_skeleton/notes/executable_files_created}；
- evidence_refs 不在 ReportDocument 顶层（M3-2 的 R-5 处置：ai_draft 无引用字段——引用回查改为 **deterministic 内部一致性**：sources/sinks 的 path 非 None 且 line ≥1；
- kind↔component 合理映射（poc.py _RULE_KIND_HINTS 的组件兜底表）。

## 3. 详细方案

```python
_POC_KIND_COMPONENTS = {
    "binder_transaction": {"service"}, "intent": {"activity"},
    "uri": {"activity", "other"}, "provider_query": {"provider"},
    "broadcast": {"receiver", "other"},
}

def check_report_document(document: Mapping) -> dict:
    # 检查 1 分离：ai_draft 与 deterministic 键集交集 == set()；
    #   provenance in {"ai_report_protocol", "projected_from_l2_review"}
    # 检查 2 引用：deterministic.sources/sinks 每条 dict 的 path 非空 str；
    #   line 为 None 或 int>=1（violations 列表）
    # 检查 3 PoC：executable_files_created == []；
    #   command_skeleton 每条含 "<" 或以 "#" 开头；
    #   kind 映射 component_kind（other 宽松）
    # verdict：FAIL（executable 非空或 provenance 非法）/ WARN（引用或映射违规）/
    #   PASS
```

文件：`backend/app/evaluation/report_quality.py` + `backend/tests/test_evaluation_report_quality.py`。

## 4. 风险

document 形态漂移（缺键）→ 检查容错记 violation（不抛）；映射表保守（other 兜底防误报）。
