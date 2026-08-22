# 任务实施方案：T2.6（探索候选三档校验）

> **任务编号**：T2.6
> **日期**：2026-08-22
> **依据大纲**：
> - 方案 §2.5（三档校验：跳回查规则 + Guard 阻断 + 三档输出）/§2.2（custom sink 不直接否决）
> - 实施计划 T2.6（三视角：规则零交集/输入未证实/代码事实反驳——与 schema 三档的映射见 §3.4 D1）
> - T0.1 冻结 schema：`ExplorerCandidateValidation`（status 四枚举/failed_hop_indices/verified_hop_count/blocked_by_guard/custom_sink_proposal/notes + 判定规则 docstring）
> **状态**：起草
> **前置依赖**：T2.5b（候选产出——validation 占位 None）

---

## 1. 任务目标与范围

- **目标**：`backend/app/analysis/explorer_validation.py`——对 ExplorerCandidate 做确定性回查，原地填充 `validation` 字段（三档 status + 明细），并在 explorer 阶段集成（候选落盘前校验 + stage summary 三档计数）。
- **范围**：`explorer_validation.py`（`validate_explorer_candidates`）+ `_run_explorer_stage` 集成（校验时机：reader 存活期内）+ 测试。
- **非范围**：归一化/funnel 合流（T2.7）；custom_sink_proposal 判定（D2 记录边界）；deep_dive（T2.8）。

## 2. 现状锚点

- **跳回查数据源**：`methods(id)` + `call_sites(method_id, start_line, resolved_target_id, resolve_status)`（方案 §2.5 原文规则："每跳 from/to_method_id、call_site_line 对 call_sites 表验证（resolved_target_id、resolve_status='resolved'）"）。
- **guard 复用**：`verify_candidate_guards(candidate, index_path)`——candidate 形态 `{manifest_facts, sources: [{path, line}]}`（guard_verifier L140-148：manifest debuggable=True 直接放行；release 下组件类 guard 检测）——探索候选首跳的 from_method_id 可解析出 path（`id.split("#")[0]`）+ call_site_line。
- **T0.1 判定规则（schema docstring 冻结）**：validated=全部跳回查通过；partially_validated=至少一跳可回查但链/证据不完整；unverified=引用不可回查或信息不足。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更 | 摘要 |
|---|---|---|
| `backend/app/analysis/explorer_validation.py` | 新增 | `validate_explorer_candidates`（跳回查 + guard + 三档） |
| `backend/app/analysis/orchestrator.py` | 修改 | `_run_explorer_stage` 校验集成（reader 存活期内 + summary 计数 + 候选重写） |
| `backend/tests/test_explorer_validation.py` | 新增 | 三档/明细/guard/集成测试 |

### 3.2 校验器设计

```python
def validate_explorer_candidates(
    candidates: list[dict[str, Any]],
    reader: Any,                      # SQLiteCodeIndexReader（methods/call_sites 回查）
    index_path: str,                  # analysis.sqlite3 路径（guard 检测）
    manifest_facts: dict[str, Any],   # {debuggable, target_sdk}（run 的 manifest 事实）
) -> dict[str, int]:
    """原地填充候选 validation（三档），返回 {status: count}（stage summary）。

    每跳回查（方案 §2.5）：
    1. from/to_method_id 存在于 methods 表；
    2. call_sites 存在 (method_id=from, start_line=call_site_line) 行，
       且 resolved_target_id == to_method_id 且 resolve_status == 'resolved'。
    三档：全跳通过→validated；≥1 跳通过→partially_validated；0 跳→unverified。

    blocked_by_guard：verify_candidate_guards 以首跳构造（path 取
    from_method_id.split("#")[0]，line=call_site_line）——命中即 True
    （候选链在 release 包被 debuggable guard 确定性阻断）。
    """
```

notes 按档位生成结论摘要（如 `"3/3 跳回查通过"` / `"1/2 跳回查通过；失败跳 [1]"` / `"跳均不可回查"`；guard 阻断追加说明）。

### 3.3 orchestrator 集成

`_run_explorer_stage`：`explore_all` 返回后（reader 关闭前）：
```python
counts = validate_explorer_candidates(
    candidates, reader, str(run_dir / "index" / "analysis.sqlite3"),
    {"debuggable": manifest.get("debuggable"), "target_sdk": manifest.get("target_sdk")},
)
# candidates.json 重写（含 validation）——ExplorerOrchestrator._write_candidates 复用
# stage summary 增加 validated/partially_validated/unverified 计数
```

### 3.4 关键设计决策

**D1：实施计划三视角与 schema 三档的映射（方案澄清）**
- 实施计划 T2.6 行的三视角是**观察角度**而非独立档位："代码事实反驳"≈ 跳回查失败（partially/unverified 的成因）；"输入未证实"≈ unverified（链不可回查则输入端无法证实）；"规则零交集"≈ T2.7 归一化的 funnel identity 语义（`rule_id=EXPLORER_AGENT` 不跨源合并，天然零交集）。**主实现以 T0.1 冻结 schema 三档为准**（本决策记录于评审文档）。

**D2：custom_sink_proposal 本任务不实现（保守 default false）**
- 方案 §2.2 要求 sink 未命中 taxonomy 标记 custom——但 backend **无集中 sink taxonomy 注册表**（taxonomy 是候选 sink 的字段语义散布于 funnel/aggregate）；链首实现的判定数据源不存在。记录为已知边界：T2.7 归一化（sink → 正式 sinks 映射）时以归一化结果评估 taxonomy 命中，届时补标记。

**D3：guard 用首跳定位（非全跳）**
- guard 阻断的语义是"入口在 release 不可达"——入口=链首（from_method_id）；全跳 guard 检测语义不明（中段 guard 不否定入口可达性）。首跳构造 {path, line} 传入既有 guard 检测。

**D4：校验失败不挂阶段（容错边界）**
- 单候选校验异常（如 hops 结构异常）→ 该候选 unverified + notes 记录异常摘要；`validate_explorer_candidates` 自身不抛（阶段主链保护）。

### 3.5 测试方案（`test_explorer_validation.py`）

真实 index（复用 test_explorer 调用链源码 + guard 组件源码）：

1. **test_validated_full_hops**：候选 hops 全真实（A.entry→B.run 的 call_sites）→ validated + verified_hop_count + failed_hop_indices=[]；
2. **test_partially_validated**：1 真实跳 + 1 伪跳（to_method_id 不存在）→ partially_validated + failed_hop_indices=[1]；
3. **test_unverified**：全伪 → unverified；
4. **test_blocked_by_guard**：首跳在 guard 组件（FLAG_DEBUGGABLE early return）+ release manifest（debuggable 非 True）→ blocked_by_guard=True；debug 包（debuggable=True）→ False；
5. **test_validation_counts**：混合三候选 → 计数 {validated:1, partially_validated:1, unverified:1}；
6. **test_schema_validation_populated**：填充后候选通过 explorer_candidate.schema.json 校验；
7. **test_orchestrator_stage_summary**（集成）：explorer 阶段（AI 不可用零候选）→ summary 计数字段存在；轻量直调路径（真实 index + 手造候选 → stage 断言在 test_explorer 的集成测试扩展）。

### 3.6 与大纲一致性对照

| 大纲条目 | 实现 | 一致性 |
|---|---|---|
| 方案 §2.5 跳回查规则（methods/call_sites/resolved） | §3.2 逐条 | 一致 |
| 方案 §2.5 Guard/授权阻断 | D3（首跳 guard） | 一致 |
| 方案 §2.5 三档输出 | schema 冻结三档 | 一致 |
| 方案 §2.2 custom sink 不否决 | D2 边界记录（default false 不否决——语义保守符合） | 部分（显式记录） |
| 实施计划三视角 | D1 映射澄清 | 一致（视角映射） |

## 4. 风险与回退

| 风险 | 对策 | 回退 |
|---|---|---|
| call_site_line 与 call_sites.start_line 粒度偏差（模型给源码行 vs 表存调用行） | 回查用 (method_id, start_line) 精确匹配；不匹配即跳失败（保守） | 放宽为行范围匹配（需实证偏差） |
| guard 首跳 path 解析失败（method_id 形态异常） | 构造前 try/except → 跳过 guard（blocked_by_guard=False + notes） | - |
| 校验性能（大候选量×跳数） | 单查询批量（IN 子句）；候选量受 max_candidates_per_run 限 | - |

## 5. 依赖

- 前置：T2.5b（候选）；运行时：guard_verifier/index（既有）。
