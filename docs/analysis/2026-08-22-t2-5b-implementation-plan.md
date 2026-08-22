# 任务实施方案：T2.5b（探索 Agent 驱动循环）

> **任务编号**：T2.5b（T2.5 拆分第二子任务——协议层 T2.5a 已冻结 `60c8272`）
> **日期**：2026-08-22
> **依据大纲**：
> - 方案：`docs/analysis/2026-08-18-project-optimization-plan.md` §2.4（explorer.py 是循环驱动者——模型不自循环；每轮落盘可审计；预算强制终止产出部分链+缺口清单）
> - 实施计划 T2.5 + T2.5a 评审 §5（前置项：AI_OUTPUT_MODEL_REGISTRY 注册/entry_json 构造/预算裁剪）
> **状态**：起草
> **前置依赖**：T2.5a（协议冻结）、T2.4（CallTreeService）、T2.2（入口表）

---

## 1. 任务目标与范围

- **目标**：`backend/app/analysis/explorer.py`——`ExplorerOrchestrator` 受控检索循环：入口选择 → 轮循环（ExplorerInput 构造 → AI 协议执行 → read_requests 本地执行 → 上下文累积 → loop.done/预算终止）→ ExplorerCandidate 转换 → 观测落盘；orchestrator 集成 `explorer` 阶段。
- **范围**：
  - `explorer.py`（循环驱动 + 候选转换 + 落盘）；
  - `ai.py`：analyzer 公开方法 `explore_entry(model_input) -> dict`（复用 `_invoke_prompt` 状态机——render→cache→budget→transport→strict-parse→repair）；
  - `ai_models.py`：`AI_OUTPUT_MODEL_REGISTRY` 注册 `ExplorerObservation`（运行时输出解析，T2.5a 评审 §5 前置项）；
  - `orchestrator.py`：`explorer` 阶段（AI 阶段后，`explorer.enabled` 门禁，默认 false）；
  - 测试 `test_explorer.py`（FakeAnalyzer + 真实 index）。
- **非范围**：候选入 funnel（T2.7 归一化——本任务候选只落盘 `run_dir/explorer/candidates.json`）；三档校验（T2.6）；deep_dive（T2.8）。

## 2. 现状锚点

- **协议**（T2.5a 冻结）：`ExplorerInput`（round/预算/entry_json/attack_surface_json/prior_observations/code_context）+ 既有 `ExplorerObservation`（read_requests ≤8/chain_proposals ≤8/component_summary/loop + `_done_requires_chain`）+ `prompts/explorer/1.0.0` + registry。
- **analyzer 方法模式**（ai.py L279-334）：`triage_l1/review_l2` = 构造 model_input → `_invoke_prompt(prompt_id, version, input, output_model, track)`——explorer 方法同模式但 **model_input 由驱动层构造传入**（上下文累积是驱动职责）。
- **CallTreeService**（T2.4）：四操作检索（get_method_body/get_callees/get_callers/search_symbol——与 ReadRequest.operation 一一对应）。
- **ExplorerCandidate schema**（T0.1）：candidate_id=`expl_`+20hex / source="explorer_agent" / prompt_version / model / component(kind/name/exported/entry_method) / api_entry_ref / chain_proposal / validation=None 占位。
- **ExplorerSettings**：enabled（默认 false）/ max_candidates_per_run=50 / max_rounds_per_entry=4 / max_requests_per_entry=20。
- **预算计数**：analyzer 的 AI 请求计数（`_ai_requests_used`）经 `_invoke_prompt` 内部预算机制（cache/budget 状态机）——explorer 请求计入同一 run 级 AI 预算。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更 | 摘要 |
|---|---|---|
| `backend/app/analysis/explorer.py` | 新增 | ExplorerOrchestrator（循环/转换/落盘） |
| `backend/app/analysis/ai.py` | 修改 | `explore_entry` 公开方法（~12 行） |
| `backend/app/analysis/ai_models.py` | 修改 | AI_OUTPUT_MODEL_REGISTRY + ExplorerObservation（1 行） |
| `backend/app/analysis/orchestrator.py` | 修改 | `explorer` 阶段（~25 行） |
| `backend/tests/test_explorer.py` | 新增 | FakeAnalyzer 循环行为测试 |

### 3.2 `ExplorerOrchestrator` 设计

```python
class ExplorerOrchestrator:
    """探索轨循环驱动者（方案 §2.4：模型不自循环——每轮输入输出落盘可审计）。

    analyzer 为 AI 协议执行者（explore_entry）；call_tree 为本地检索服务
    （read_requests 的执行器）；reader 生命周期归 orchestrator（调用方）。
    """

    def __init__(self, analyzer, call_tree: CallTreeService,
                 settings: ExplorerSettings, run_dir: Path) -> None: ...

    async def explore_all(self, entries: list[dict]) -> list[dict[str, Any]]:
        """逐入口探索（入口=get_entry_points() 中 method_id 非 None 者——
        无方法起点的入口（webview/未解析组件）本轮跳过并记录）；候选累计
        达 max_candidates_per_run 即止（剩余入口记 skipped）。"""

    async def _explore_entry(self, entry: dict, attack_surface: dict | None) -> list[dict]:
        """单入口轮循环（max_rounds_per_entry / max_requests_per_entry 双预算）：

        每轮：
        1. ExplorerInput 构造（round_index/剩余预算/entry_json/attack_surface_json/
           prior_observations=前轮 component_summary+链摘要/code_context=取回累积）；
        2. result = await analyzer.explore_entry(model_input)——status != completed
           → 循环终止（terminated_by="error"，缺口=失败分类）；
        3. observation = ExplorerObservation.model_validate(result["analysis"])；
        4. read_requests 执行（本轮限额 = min(剩余请求预算, 8)）：
           operation → CallTreeService 对应方法；结果 JSON 序列化进 code_context
           （截断到 max 8KB/请求，防上下文爆炸）；
        5. observation 落盘（observations.json 追加本轮记录）；
        6. loop.done → 终止（terminated_by="loop_done"）；
        预算终止（轮/请求耗尽而未 done）→ terminated_by="budget"；
        最终：全部轮的 chain_proposals 转换为 ExplorerCandidate。
        """

    def _to_candidates(self, entry: dict, observation, prompt_version: str, model: str) -> list[dict]:
        """ExplorerCandidate 转换（T0.1 schema）：
        candidate_id="expl_"+uuid4().hex[:20]；component 从 entry 投影
        （kind 五类枚举映射：binder/webview_bridge→"other"；exported 从
        attack_surface 事实（无则 entry 事实，仍无则 False 保守）；entry_method
        = entry.entry_method 或 ""——schema minLength 1 兜底"unknown"）；
        api_entry_ref=entry.entry_id；validation=None 占位（T2.6 填充）。
        prompt_version/model 从 analyzer result metadata 透传（评审 R-3）。
        """
```

**观测落盘**（`run_dir/explorer/observations.json`，每轮原子追加读改写）：
```json
{"entries": [{"entry_id": "...", "terminated_by": "loop_done|budget|error|no_method",
               "rounds": [{"round_index": 1, "requests_executed": [...], "observation": {...}}],
               "candidate_count": 1}]}
```
候选落盘 `run_dir/explorer/candidates.json`（ExplorerCandidate 数组）+ manifest artifacts 注册（`{type: "explorer_candidates", path, candidate_count}`）。

### 3.3 ai.py 扩展

```python
async def explore_entry(self, model_input: ExplorerInput) -> dict[str, Any]:
    """使用严格 ExplorerInput/ExplorerObservation 执行单轮探索（T2.5b）。

    model_input 由驱动层（ExplorerOrchestrator）构造——上下文累积是驱动职责；
    本方法只执行协议（复用 render→cache→budget→transport→strict-parse 状态机）。
    """
    unavailable = self._analysis_unavailable_result()
    if unavailable is not None:
        return unavailable
    return await self._invoke_prompt(
        "explorer", "1.0.0", model_input, ExplorerObservation, "explorer",
    )
```

### 3.4 orchestrator 集成

AI 阶段（`ai_analysis` stage 记录）完成后：

```python
if self.settings.explorer.enabled:
    self._stage(run_id, "explorer")
    # CallTreeService（reader try/finally 生命周期）+ ExplorerOrchestrator
    # entries = call_tree.get_entry_points()（method_id 非 None 过滤）
    # candidates = await explorer_orchestrator.explore_all(entries)
    # _record_stage(run_id, "explorer", "completed", {
    #     "entry_count": N, "candidate_count": M,
    #     "terminated_by": {...计数}, "requests_used": K})
    # AI 不可用（preflight 失败/circuit）→ stage "skipped"（reason）不挂 run
```

- 门禁：`explorer.enabled`（默认 false）；AI 可用性由 analyzer 内部（`_analysis_unavailable_result`）判定——explore_all 每入口首调即返回失败 → 零候选 + stage skipped（不重试全部入口——首入口失败后短路剩余入口）。

### 3.5 关键设计决策

**D1：候选不入 funnel（本任务边界）**
- T2.7 才做归一化+funnel 扩展；本任务候选落盘 + stage 记录——探索轨产出可审计先行，消费链路后续接通。

**D2：无方法起点入口跳过（no_method）**
- webview/未解析组件入口无 method_id——ReadRequest 无法表达检索起点（target 需 method_id）；记 observations（terminated_by="no_method"）供审计，T2.6+ 评估是否补路径（如 bridge_path 定位）。

**D3：AI 失败短路与预算语义**
- 单入口首轮 AI 失败（transport/preflight/circuit）→ 该入口终止（error）且**短路剩余入口**（同类失败必然重复——预算保护）；候选保留已成功入口的部分产出。

**D4：code_context 截断（8KB/请求）**
- call_tree 结果（方法体 240 行/调用关系列表）JSON 序列化后截断——上下文累积轮间膨胀控制；截断标注（`truncated: true` 字段保留）。

**D5：component 投影的保守兜底**
- exported 无事实来源时 False（保守——ExplorerCandidateComponent 必填 bool；attack_surface 保守高估语义在 T2.3 已定，此处投影事实优先、缺失兜底 False 而非高估——候选层"未证实导出"倾向保守，与攻击面层"宁可高估"形成双层语义）。

### 3.6 测试方案（`test_explorer.py`）

FakeAnalyzer（Observation 序列可编程：`queue` 逐轮弹出；`explore_entry` 返回 `{status, analysis, metadata}`）+ 真实 index（T2.4 测试的调用链源码）：

1. **test_explore_entry_loop_done**：首轮（read_requests + done=false）→ 取码执行 → 次轮（链 + done=true）→ 候选 1 个（schema 校验）+ observations 两轮记录 + terminated_by=loop_done；
2. **test_explore_entry_budget_termination**：done 恒 false，max_rounds=2 → 两轮后终止（terminated_by=budget）+ 终轮部分链保留；
3. **test_read_requests_execution**：首轮请求 get_method_body(A.entry) → 次轮输入的 code_context 含方法体文本（FakeAnalyzer 捕获输入断言）；
4. **test_requests_budget_truncation**：首轮 8 请求但剩余预算 3 → 仅执行 3（observations.requests_executed 长度）；
5. **test_candidate_conversion**：candidate_id pattern/source/prompt_version/model 透传/validation 占位/component 投影（kind 映射 + exported 兜底）；
6. **test_explore_all_candidate_cap**：max_candidates_per_run=1 → 首入口产出后止（第二入口 skipped）；
7. **test_analyzer_failure_short_circuit**：首入口 AI 失败 → 零候选 + 剩余入口短路 + 阶段不挂；
8. **test_explore_entry_no_method**：无 method_id 入口 → terminated_by=no_method 零轮；
9. **test_ai_output_model_registered**：get_ai_output_model("ExplorerObservation", "1") 返回模型；
10. **test_orchestrator_explorer_stage**（集成）：explorer.enabled=true + AI 不可用（无 key 默认配置）→ run completed + stage explorer skipped。

## 4. 风险与回退

| 风险 | 对策 | 回退 |
|---|---|---|
| 上下文累积膨胀（轮间） | D4 截断 + prior_observations 摘要（非全量回放） | 减 max_rounds |
| AI 失败重试浪费预算 | D3 短路 | - |
| 观测落盘竞态（单协程序列化） | explorer 阶段单协程内追加（无并发写） | - |
| FakeAnalyzer 与真实协议偏差 | explore_entry 直接调 _invoke_prompt（集成模式同 L1/L2）；测试 10 真实 AI 不可用路径 | - |

## 5. 依赖

- 前置：T2.5a（协议）、T2.4（CallTreeService）、T2.2（入口表文件）。
