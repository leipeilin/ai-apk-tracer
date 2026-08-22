# 任务实施方案：T2.12 核验分流与降级（M2 收官）

> **任务编号**：T2.12
> **日期**：2026-08-22
> **依据大纲**：`docs/analysis/2026-08-18-project-optimization-plan.md` §2.7（分流 M2 试点：探索 validated 必进 + 规则 L2 以核验替代单轮 review、单轮 L2 保留为 A/B 对照与降级基线；降级回退主链永不阻塞；核验预算独立记账第三本账）；M0 审查 §4.2（适配层：verify 输出补齐 L2 其余字段 + evidence_refs 类型转换 context_id 回填）；2026-08-21 决断 3
> **状态**：已闭合（评审 R-1~R-11 全部采纳，见 `2026-08-22-t2-12-review.md` 处置记录——根因链修复：ai_evidence_contexts 显式注入 + track="verify" 证据需求识别 + A-6 生产路径端到端）
> **前置依赖**：T2.11 ✅（464c15e：VerifyAgent 主体 + verify() 返回契约）；T2.7 ✅（探索归一化候选形状 + run 级预算共享池）

---

## 1. 任务目标与范围

- **目标**：核验 agent 接入 AI 阶段主链——`verify.enabled` 时 L2 候选（含探索 validated 归一化候选）以 VerifyAgent 替代单轮 L2 review；agent 失败/预算耗尽自动回退现有单轮 L2（主链永不阻塞，候选标记 fallback 来源）；适配层将 VerifyOutput 转为 L2 analysis 形状（补齐其余字段以确定性默认值 + evidence_refs 类型转换），DecisionEngine 证据校验可消费；核验预算独立记账（第三本账，经 run 级 requests_used 自动被 batch 帽覆盖）。
- **范围（in scope）**：
  1. **适配层**（`verify_agent.py` 扩展）：`adapt_verify_result` 纯函数——VerifyOutput 聚合 → `_adapt_l2_analysis` 同构 dict + 证据转换（`path#window:N-M` context_id 格式）；
  2. **orchestrator 分流**（`_run_ai_stage`）：`_verify_path_for` 判定（verify.enabled ∧ evidence_level=L2）+ `_verify_candidate`（VerifyAgent 接线：run 级预算包装回调 + call_tree/reader 按需构造 + explorer_candidate 关联回读 + 回退编排）+ checkpoint identity 隔离 + stage summary 三本账分列；
  3. `_run_ai_stage` 签名加 `code_index`（`_run` 调用点同步）；
  4. 测试：适配层/分流判定/回退/集成/DecisionEngine 消费端到端。
- **非范围（out of scope）**：
  - funnel 路由改动：分流点在 AI 阶段（funnel 的 `ai_required`/`ai_eligible` L2 路由语义不变——核验替换的是"AI 阶段内 L2 候选的执行路径"而非 funnel 路由；任务行文件预估据此调整，见 §3.6 D1）；
  - L1 候选核验（方案 §2.7：L1 攻击面验证为 M4 评估后的扩展项）；
  - A/B 对照基建（单轮 L2 保留为降级基线即满足"保留"语义；系统化 A/B 属 M4 评估）；
  - 前端展示（verify 溯源字段随候选/finding 自然透出，专用视图后续任务）。

## 2. 现状锚点

- **VerifyAgent 契约（T2.11）**：`verify(candidate, explorer_candidate=None) -> {status, terminated_by, output, rounds, requests_used, read_requests_used, undecided_claim_indices, consistency_downgraded}`；output 为聚合 VerifyOutput（evidence_refs 已 files 表回查过滤+claim 置 None）。
- **L2 analysis 形状**：`_adapt_l2_analysis`（ai.py:1267-1274）= L2ReviewOutput 全字段 + 注入 `promotion_recommended`/`candidate_verdict`/`analysis_track="l2_review"`；`verified_evidence_refs`/`invalid_evidence_refs` 由 orchestrator（orchestrator.py:726-731）回查写回。
- **消费敏感性（评审依据）**：`harm`/`reachability_class`/`impact_vector`/`reverse_exclusion` **生产代码零消费**（纯前向兼容存储——默认值任意安全）；`guard_status="unknown"` 保守中性（`_positive_gates_pass` 不过 → pending）；`refutation_basis` 非空时逐项交叉验证 fail-closed（不增强）。
- **证据回查双通道**：orchestrator `_verify_ai_evidence_refs`（按 slice contexts 的 context_id）；聚合层 `validate_ai_evidence_references`（evidence.py:396-504）**已支持 `path#window:N-M` context_id 格式回查**（707-714）与 `ai_evidence_contexts` 显式注入（669-674）——适配层采用 path#window 格式使聚合层零改动可回查。
- **预算架构**：run 级共享池（`_ai_budget_lock` + `_ai_requests_used`，T2.7 R-1）；batch 帽计数 = `stages[ai_analysis].summary.requests_used` 累加（batch.py:11-12）——核验调用计入 run 级即自动覆盖（"第三本账 batch 帽覆盖"语义）。
- **checkpoint 隔离**：`candidate_input_key`（ai_trace.py:30-42）对 analyzer_identity 做 canonical_hash——调用方附加 `{"verify_agent": ...}` 键安全（identity 为自由 dict，仅被序列化）。
- **trace/final_slice 前置**：`_apply_ai_analysis`（orchestrator.py:1133-1167）的 trace 元素经 `_ai_runtime_metadata_from_trace` 从 `result.metadata` 提取元数据——verify 路径 trace 元素构造 `{"round": n, "result": {"status": "completed", "metadata": {prompt_version, model, ...}}}`；final_slice 仅消费 `contexts`/`request_history`（传候选原 slice 即可）。
- **explorer/candidates.json**：`save_candidates`（explorer.py:633-641）在归一化前落盘**全三档**原始形状；归一化候选带 `explorer_candidate_id`——AI 阶段读一次建 `{candidate_id: entry}` 映射供 `verify(candidate, explorer_candidate)`。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/analysis/verify_agent.py` | 修改 | `adapt_verify_result` + `_to_evidence_reference`（适配层纯函数；溯源含 guard_claim_verdict——评审 R-6） |
| `backend/app/analysis/orchestrator.py` | 修改 | `_run_ai_stage` 加 code_index（默认 None——R-8）；`_verify_path_for`/`_verify_candidate`（分流/回退/异常捕获 R-5/统一尾部 R-4/ai_evidence_contexts 注入 R-1）；checkpoint identity 隔离；summary 三本账分列；`_budgeted_protocol_call` 工厂（R-10） |
| `backend/app/findings/evidence.py` | 修改 | `_required_ai_evidence`：track="verify" 按 l2_review 语义取需求（评审 R-2） |
| `backend/tests/test_verify_agent.py` | 修改 | 适配层测试 |
| `backend/tests/test_verify_routing.py` | 新增 | 分流/回退/端到端（A-6 生产路径——R-3）/集成测试 |

### 3.2 适配层设计（`adapt_verify_result`）

```python
def adapt_verify_result(verify_result: Mapping[str, Any]) -> dict[str, Any]:
    """VerifyOutput 聚合 → L2 analysis dict（_adapt_l2_analysis 同构 + verify 溯源）。

    铁律（M0 审查 §4.2）：补齐字段全部确定性默认值（harm/reachability_class/
    impact_vector 生产代码零消费；guard_status="unknown" 保守中性——不增强
    不削弱）；evidence_refs 转 EvidenceReference（context_id=path#window 格式，
    聚合层 validate_ai_evidence_references 零改动可回查）。
    """

    output = verify_result["output"]
    refs = [_to_evidence_reference(ref) for ref in output.get("evidence_refs") or []]
    undecided = list(verify_result.get("undecided_claim_indices") or [])
    verdict = output.get("verdict")
    return {
        "summary": output.get("summary"),
        "verdict": verdict,
        "confidence_tier": output.get("confidence_tier"),
        "guard_status": "unknown",                     # 保守中性
        "evidence_refs": refs,
        "blocking_gaps": (
            [{"code": "VERIFY_CLAIMS_UNDECIDED", "critical": True,
              "message": f"核验预算内未完成全部命题判定（未判定 {len(undecided)} 项）",
              "evidence_refs": []}]
            if undecided else []
        ),
        "uncertainties": [],
        "context_requests": [],
        "flaw_holds": output.get("flaw_holds"),
        "exploitability": output.get("exploitability"),
        "harm": {"impact_type": "other", "impact_target": "verify agent 适配默认值（未评估）",
                 "server_confirmation_required": False},
        "reachability_class": "local",                 # 零消费中性默认
        "impact_vector": {"confidentiality": "none", "integrity": "none",
                          "availability": "none", "privileges_required": "low",
                          "attack_complexity": "high", "user_interaction": "none"},
        "reverse_exclusion": [],
        "confidence_rationale": (
            f"verify agent 核验：terminated_by={verify_result.get('terminated_by')}，"
            f"命题未判定 {len(undecided)} 项"
            + ("，整体判定经一致性校验降级" if verify_result.get("consistency_downgraded") else "")
        ),
        "refutation_basis": output.get("refutation_basis") or [],
        "analysis_complete": bool(output.get("analysis_complete")),
        "promotion_recommended": verdict == "supports_candidate" and not undecided,
        "candidate_verdict": verdict,
        "analysis_track": "verify",
        "verified_evidence_refs": refs,                # VerifyAgent 已 files 表回查——全 verified
        "invalid_evidence_refs": [],
        "verify_agent": {                              # 溯源（前端/审计）
            "terminated_by": verify_result.get("terminated_by"),
            "requests_used": verify_result.get("requests_used"),
            "read_requests_used": verify_result.get("read_requests_used"),
            "undecided_claim_indices": undecided,
            "consistency_downgraded": verify_result.get("consistency_downgraded"),
        },
    }


def _to_evidence_reference(ref: Mapping[str, Any]) -> dict[str, Any] | None:
    """ExplorerEvidenceRef → EvidenceReference（context_id=path#window 格式）。

    无 line 证据不可定位进 window 格式——静默丢弃（保守削弱不拦截；计数由
    verify_agent 的 evidence_filter_note 承载）。
    """
    path, line = ref.get("path"), ref.get("line")
    if not path or not isinstance(line, int):
        return None
    end = ref.get("end_line") if isinstance(ref.get("end_line"), int) else line
    return {
        "context_id": f"{path}#window:{line}-{end}",
        "path": path, "line": line, "end_line": end,
        "claim": f"verify agent 回查通过的证据位置（{path}:{line}）",
    }
```

> `refs` 构造时过滤 None（`[r for r in (... ) if r is not None]`）。

### 3.3 orchestrator 分流设计

**判定**（`_verify_path_for(candidate)`）：`self.settings.verify.enabled and candidate.get("evidence_level") == "L2"`——L1 不进（M4 扩展项）；探索归一化候选（explorer_promoted）天然是 L2 → 覆盖"探索 validated 必进"。

**`_run_ai_stage` 集成**（analyze_job 内，checkpoint 恢复之后）：

```python
verify_path = self._verify_path_for(candidate)
if verify_path:
    verify_result = await self._verify_candidate(
        candidate, slice_document, run_dir, code_index,
        explorer_candidates_map, trace_store=trace_store,
        candidate_index=candidate_index, input_key=input_key,
    )
    if verify_result is not None:          # 核验成功（已写入候选）
        return verify_result                # {"status": "completed", "stop_reason": "verify_completed", ...}
    # None = 回退信号（fallback 来源已标记）→ 落入下方原 _analyze_with_expansion
result = await self._analyze_with_expansion(...)   # 原路径（回退或非 verify 候选）
```

- **checkpoint identity 隔离**：verify_path 时 `analyzer_identity = {**identity, "verify_agent": self.settings.verify.prompt_version}`——verify 与 L2 结果命名空间隔离；
- **explorer_candidates_map**：`_run_ai_stage` 开头读一次 `run_dir/explorer/candidates.json`（容错：损坏/缺失 → 空映射），`{entry["candidate_id"]: entry}`；`_verify_candidate` 内 `map.get(candidate.get("explorer_candidate_id"))` 取原始链。

**`_verify_candidate`**（核心新方法）：

```python
async def _verify_candidate(candidate, slice_document, run_dir, code_index,
                            explorer_candidates_map, *, trace_store, candidate_index, input_key):
    """单候选核验 + 适配写入；失败按 fallback_to_single_turn_l2 编排回退。

    返回 None = 回退信号（调用方走原单轮 L2 路径）；
    返回 dict = verify 终态结果（已写入候选，含 trace）。
    """
    verify_settings = self.settings.verify
    database_path = str((code_index or {}).get("database_path") or "")
    reader = None
    if database_path and Path(database_path).is_file():
        reader = SQLiteCodeIndexReader(code_index or {})
    if reader is None:
        candidate["verify_fallback_reason"] = "verify_index_unavailable"
        return None                          # 回退（主链不阻塞）
    try:
        call_tree = CallTreeService(run_dir, reader, CallTreeSettings())
        async def budgeted_verify_call(model_input):
            async with self._ai_budget_lock:
                if self._ai_requests_used >= self.settings.context_budget.max_requests_per_run:
                    return {"status": "skipped", "circuit_breaking": True,
                            "metadata": {"reason": "run_request_budget_exhausted"}}
                self._ai_requests_used += 1
                self._verify_requests_used += 1      # 第三本账分账
            return await self.ai.verify_entry(model_input)
        agent = VerifyAgent(budgeted_verify_call, call_tree, verify_settings, run_dir, reader)
        explorer_candidate = explorer_candidates_map.get(candidate.get("explorer_candidate_id"))
        verify_result = await agent.verify(candidate, explorer_candidate)
    finally:
        reader.close()

    if verify_result["status"] == "completed":
        analysis = adapt_verify_result(verify_result)
        trace = [_verify_round_trace(round_record) for round_record in verify_result["rounds"]]
        candidate["verify_used"] = True
        self._apply_ai_analysis(candidate, analysis, trace, slice_document)
        return {"status": "completed", "stop_reason": "verify_completed", "trace": trace}

    # 失败/跳过（failed/skipped）→ 回退编排
    if verify_settings.fallback_to_single_turn_l2:
        candidate["verify_fallback_reason"] = f"verify_{verify_result['terminated_by']}"
        return None
    # 不回退：对齐 _analyze_with_expansion 失败语义（主链不阻塞）
    skipped = verify_result["status"] == "skipped"
    candidate["analysis_status"] = "ai_skipped" if skipped else "ai_failed"
    message = f"verify agent {verify_result['terminated_by']}"
    if skipped:
        candidate["ai_skip_reason"] = message
    candidate.setdefault("ai_blocking_gaps", []).append({
        "code": "AI_ANALYSIS_SKIPPED" if skipped else "AI_ANALYSIS_FAILED",
        "critical": not candidate.get("deterministic_chain_verified", False),
        "message": message,
    })
    return {"status": "skipped" if skipped else "failed", "stop_reason": message,
            "trace": [], "circuit_breaking": False, "message": message}
```

- `_verify_round_trace(round_record)`：`{"round": r["round_index"]-1, "result": {"status": "completed", "metadata": {"prompt_version": 去前缀, "model": r["model"], ...}}}`（`_ai_runtime_metadata_from_trace` 提取元数据的最小形状）；
- **熔断传播**：verify 失败一律回退（fallback=True）——熔断场景下单轮 L2 同样熔断立即失败（同一 analyzer），行为一致；fallback=False 时按失败终态（不重试）。
- **_verify_requests_used**：`__init__` 归零 + summary 分列（`verify_requests_used`——第三本账）。

**stage summary 扩展**：`verify_counts {attempted, completed, fallback, failed_no_fallback}` + `verify_requests_used`（三本账齐备：`explorer_requests_used`/`deep_dive_requests_used`（explorer stage）/`ai_stage_requests_used`+`verify_requests_used`（ai stage））。

### 3.4 与大纲一致性对照

| 大纲条目（引用） | 本方案实现方式 | 一致性说明 |
|---|---|---|
| §2.7 分流：探索 validated 必进 + 规则 L2 以核验替代 | `_verify_path_for`：verify.enabled ∧ L2（探索归一化候选即 L2） | 不变 |
| §2.7 单轮 L2 保留为 A/B 对照与降级基线 | 回退路径即原 `_analyze_with_expansion`（零改动保留）；A/B 系统化对照属 M4 | 不变（任务级边界） |
| §2.7 降级回退：agent 失败/预算耗尽自动回退，主链永不阻塞 | `fallback_to_single_turn_l2=True` 默认回退 + fallback 来源标记；False 时失败终态不阻塞；reader 不可用回退 | 不变 |
| §2.7 核验预算独立记账（第三本账，batch 帽覆盖） | `_verify_requests_used` 分账 + run 级共享池（requests_used 累计——batch 帽事实源自动覆盖） | 不变 |
| M0 审查 §4.2：补齐 L2 其余字段 + evidence_refs 类型转换（context_id 回填） | `adapt_verify_result`（确定性默认值 + `path#window:N-M` context_id——聚合层已支持回查，零改动） | 不变（验收含 DecisionEngine 消费断言） |
| L1 不进核验（§2.7 M4 扩展项） | `_verify_path_for` 限 L2 | 不变 |

### 3.5 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| R-1 verify 消耗 run 级预算挤压规则候选（探索/深挖/核验/单轮 L2 四方共享） | 预算竞争 | 核验轮数（4）+ 读码（12）有界；三本账分列可审计；fallback 语义防双烧（verify 失败回退后单轮 L2 继续用剩余预算——属设计内） | `verify.enabled=false` 一键回退（默认即关） |
| R-2 适配层默认值语义漂移（未来字段被消费） | 裁决偏移 | 默认值全部中性/保守（unknown/local/none 枚举端点）；`analysis_track="verify"` + `verify_agent` 溯源字段可审计过滤 | 适配层纯函数独立可改 |
| R-3 无 line 证据静默丢弃削弱 | 证据不完整 | prompt 硬约束要求 path+line（绝大多数带行号）；`evidence_filter_note` 计数可审计 | 后续可扩展全窗 context_id |
| R-4 checkpoint 命名空间泄漏（verify 结果被 L2 checkpoint 恢复串用） | 结果错配 | identity 附加 `verify_agent` 键隔离（canonical_hash 安全附加） | 单测断言 key 不同 |
| R-5 回退后双份 AI 消耗（verify N 轮 + 单轮 L2） | 预算浪费 | 属"主链永不阻塞"的设计代价；fallback 仅在 verify 失败时发生（成功率实测于 M2 验收） | fallback_to_single_turn_l2=false |

### 3.6 边界决策记录

| 编号 | 决策 | 理由 | 状态 |
|---|---|---|---|
| D1 | **funnel 零改动**（任务行"candidate_funnel.py（路由）"调整为 AI 阶段内分流） | funnel 的 L2 路由（ai_required/ai_eligible）语义不变；核验替换的是 AI 阶段执行路径而非路由判定——改 funnel 反而混淆"谁送 AI"与"AI 怎么执行"两层 | 待评审确认 |
| D2 | guard_status 默认 `"unknown"`（非 absent） | unknown 保守中性（gate 不过 → pending）；absent 会增强（过 gate）——核验轨不得增强 | 按方案执行 |
| D3 | 无 line 证据**静默丢弃**（不进 invalid 列表） | invalid 非空触发 AI_EVIDENCE_REF_INVALID critical gap 拦截（对有效但不可定位证据过严）；丢弃保守削弱不拦截 | 待评审确认 |
| D4 | fallback=True 时 verify 熔断也回退单轮 L2 | 同一 analyzer 熔断态下单轮 L2 立即失败（行为收敛一致）；避免 verify 分支单独实现熔断语义 | 按方案执行 |
| D5 | 适配层 verified_evidence_refs 直填（不重走 `_verify_ai_evidence_refs`） | VerifyAgent 已对证据做 files 表回查（回查通过才入池）；`_verify_ai_evidence_refs` 按 slice contexts 查（合成 context_id 不在其中会误判 invalid）；聚合层仍会做 path#window 回查兜底 | 待评审确认 |

## 4. 依赖

- 前置任务：T2.11（VerifyAgent/adapt 输入）、T2.7（预算共享池/归一化候选）、M0 审查 §4.2（适配层需求）
- 交接 M2 验收：探索轨三加一验收（§4.3 全量）含核验 agent 四条试点验收点（盲验 trace 断言/命题一致性/循环语义/降级回退——T2.11 已实测前三，本任务实测降级回退）——**已由 M2-ACCEPTANCE-CLOSURE 执行**（2026-08-23，见 `2026-08-23-m2-acceptance-runs.md`：降级回退真实 run 触发 52 次 fallback 主链不阻塞 ✓；R-5 的 fallback 成功率实测 = 0%（52/52 fallback——模型输出合规率问题，移交 M4 prompt 迭代））
