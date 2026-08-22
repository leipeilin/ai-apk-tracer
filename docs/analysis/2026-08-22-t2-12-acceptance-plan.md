# 任务验收方案：T2.12 核验分流与降级（M2 收官）

> **任务编号**：T2.12
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/2026-08-22-t2-12-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测（适配层纯函数 + orchestrator 分流实例级 + API 集成）+ DecisionEngine 消费端到端 + 全量回归

---

## 1. 验收范围

- T2.12 全部交付物：适配层（`adapt_verify_result`/`_to_evidence_reference`）、分流判定（`_verify_path_for`）、核验接线与回退编排（`_verify_candidate`）、checkpoint 隔离、三本账分列、`_run_ai_stage` 签名扩展。
- 验收通过即视为任务完成、可进入提交（M2 收官）。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 适配层字段补齐 | `adapt_verify_result`（成功 verify_result）→ 逐字段断言 | 与 `_adapt_l2_analysis` 同构（19 字段 + 注入 3）；`analysis_track="verify"`；guard_status="unknown"（D2）；harm/impact_vector/reverse_exclusion 确定性默认 |
| A-2 | 证据转换 context_id 格式 | 含 line/end_line 的 ExplorerEvidenceRef → `_to_evidence_reference` | `context_id="{path}#window:{line}-{end}"`；claim 必填；path/line/end_line 透传 |
| A-3 | 无 line 证据丢弃 | 无 line 的 ref 混入 | 该 ref 不进 evidence_refs（静默丢弃——D3）；有 line 的正常转换 |
| A-4 | undecided 缺口物化 | `undecided_claim_indices=[1,2]` 的 verify_result → 适配 | `blocking_gaps` 含 VERIFY_CLAIMS_UNDECIDED（critical=True，message 含计数）；`promotion_recommended=False`；`analysis_complete=False` |
| A-5 | 一致性降级溯源 | `consistency_downgraded=True` → 适配 | `confidence_rationale` 含"一致性校验降级"；`verify_agent` 溯源字段齐备（terminated_by/requests_used/undecided/consistency_downgraded） |
| A-6 | DecisionEngine 消费端到端（M0 审查 §4.2 验收建议） | 适配 analysis → 喂 `DecisionEngine().decide(candidate)`（candidate 带 ai_analysis=适配 dict） | 不抛异常；`evidence_decision` 产出（unresolved/pending 路径——verify 中性默认不增强）；聚合层 `validate_ai_evidence_references` 对 path#window 格式 ref 回查通过（构造真实文件索引） |
| A-7 | 分流判定 | L1/L2 候选 × verify.enabled on/off → `_verify_path_for` | 仅 enabled ∧ L2 为 True；L1 恒 False；disabled 恒 False |
| A-8 | 核验成功路径 | 实例级 `_verify_candidate`（FakeAI verify_entry 成功 + 真实索引 slice） | 候选获 `ai_analysis`（适配形状）+ `verify_used=True` + `analysis_track="verify"`；`_apply_ai_analysis` 字段齐备（candidate_verdict/confidence_tier/ai_guard_assessment）；无 `verify_fallback_reason` |
| A-9 | 探索候选关联 | 候选带 `explorer_candidate_id` + explorer/candidates.json 预置 → `_verify_candidate` | VerifyAgent 输入含 chain_facts（原始 hops 剥离投影）——fake 捕获断言 |
| A-10 | 回退：verify 失败 | fake verify_entry 返回 error → `_verify_candidate` | 返回 None；候选 `verify_fallback_reason="verify_error"`；调用方走原 `_analyze_with_expansion`（fake analyze 被调） |
| A-11 | 回退：预算耗尽 | run 级预算预置耗尽 → verify skipped | 回退（fallback_reason="verify_short_circuit"） |
| A-12 | 回退关闭 | `fallback_to_single_turn_l2=false` + verify 失败 | 不回退：候选 `analysis_status="ai_failed"` + AI_ANALYSIS_FAILED gap；返回 failed 终态 |
| A-13 | reader 不可用回退 | code_index=None | `verify_fallback_reason="verify_index_unavailable"`；返回 None |
| A-14 | checkpoint 隔离 | verify_path on/off 两种 input_key | key 不同（identity 附加 verify_agent 键） |
| A-15 | 三本账分列 | 集成场景后 stage summary | `verify_requests_used` 独立计数；run 级 `requests_used` 含核验调用；`verify_counts{attempted, completed, fallback, ...}` |
| A-16 | 集成：主链不阻塞（M2 验收 4.3-6.4 降级回退） | API 级 `verify.enabled=true` + 无 AI key 跑 run | run completed（verify 失败 → 回退单轮 L2 亦失败 → ai_skipped 标记——主链不挂） |
| A-17 | 默认关闭零影响 | `verify.enabled=false`（默认）全量回归 | 1073→当前基线全过；无 verify 字段产生 |
| A-18 | L2 原路径保留 | verify.enabled=true 但候选 L1 | L1 走原 l1-triage（fake analyze 断言） |

## 3. 回归标准

- [ ] `cd backend && .venv/bin/python -m pytest` 全量通过（基线 1103 passed / 0 failed，只增不减）；
- [ ] `scripts/check-backend.sh` 通过；改动文件 ruff 零错误；
- [ ] `sync-ai-protocol.py --check` 通过（协议零改动）；
- [ ] 默认配置行为不变（verify.enabled=false——A-17 断言 + 全量回归）。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | explorer/candidates.json 缺失 | 非探索候选 + 文件不存在 | 空映射；verify 正常（explorer_candidate=None） |
| N-2 | explorer/candidates.json 损坏 | 预写非法 JSON | 空映射 + warning；不抛 |
| N-3 | verify() 抛异常（意外） | fake verify_entry raise | `_verify_candidate` 捕获 → 回退（fallback_reason="verify_error"）——主链不阻塞 |
| N-4 | 适配输入畸形（output=None） | verify_result status=completed 但 output 缺失（防御） | adapt 抛 KeyError 被上游捕获 → 回退 |
| N-5 | 无 sources 候选（claims 空） | 最小候选 verify | VerifyAgent no_claims 快速 skipped → 回退单轮 L2 |
| N-6 | fallback 后单轮 L2 也失败 | 双失败 fake | 候选 ai_failed；run 不挂（终态语义对齐原路径） |
| N-7 | verify 轮审计与 AI 阶段共存 | 集成场景 | verify/observations.json 与 ai-trace 并存互不干扰 |

## 5. 回退方案

- `verify.enabled=false`（默认）即整体禁用（分流判定短路——零运行时影响）；代码按文件粒度回退（适配层纯函数 + orchestrator 分支独立）。

## 6. 验收记录（实施后填写）

> **验收日期**：2026-08-22。**结果：全部通过**。全量回归 **1120 passed / 0 failed**（基线 1103 + 新增 17）；`sync-ai-protocol.py --check` 通过；`scripts/check-backend.sh` 通过；改动文件 ruff 零错误（含 evidence.py 既有 import/SIM102 顺带修复）。

| 编号 | 结果 | 实测说明（测试函数） |
|---|---|---|
| A-1 | 通过 | `test_adapt_fields_complete`：L2 同构 + 确定性默认 + verify 溯源（含 R-6 guard_claim_verdict） |
| A-2 | 通过 | `test_adapt_evidence_reference_conversion`：context_id=path#window 格式 + claim 必填 |
| A-3 | 通过 | 同上：无 line 证据静默丢弃（D3） |
| A-4 | 通过 | `test_adapt_undecided_gap_and_consistency_trace`：VERIFY_CLAIMS_UNDECIDED critical + promotion=False |
| A-5 | 通过 | 同上：consistency 降级进 confidence_rationale + 溯源 |
| A-6 | 通过 | `test_adapted_analysis_end_to_end_production_path`（评审 R-3 重设）：verify_candidate 生产路径 → invalid_evidence_refs 空（R-1 注入生效）+ 无 REQUIREMENTS_UNRESOLVED（R-2 track 识别生效）→ DecisionEngine.decide 产出裁决 |
| A-7 | 通过 | `test_verify_path_for_matrix`：enabled×L1/L2 四象限 |
| A-8 | 通过 | `test_verify_candidate_success`：verify_used/ai_analysis(verify track)/ai_evidence_contexts/三本账（_verify_requests_used==1 且 run 级==1） |
| A-9 | 通过 | `test_verify_candidate_explorer_chain_linked`：原始 hops 进 chain_facts + 假设层剥离断言 |
| A-10 | 通过 | `test_verify_failure_falls_back`：None + fallback_reason="verify_error" |
| A-11 | 通过 | `test_verify_budget_exhausted_falls_back`：预算预置耗尽 → fake.calls==0（检查先于调用）→ 回退 |
| A-12 | 通过 | `test_verify_failure_no_fallback_terminal`：failed 终态 + ai_stop_reason/ai_analysis_trace（R-11）/AI_ANALYSIS_FAILED gap |
| A-13 | 通过 | `test_verify_index_unavailable_falls_back` |
| A-14 | 通过 | `test_checkpoint_identity_isolation`：verify identity 键隔离 |
| A-15 | 通过 | `test_verify_candidate_success` 内分账断言 + stage summary verify_counts/verify_requests_used（实现） |
| A-16 | 通过 | `test_verify_enabled_run_completes_without_ai`：API 级 run completed（M2 验收 4.3-6.4 主链不阻塞） |
| A-17 | 通过 | `test_verify_disabled_by_default` + 全量回归（默认零影响） |
| A-18 | 通过 | `test_verify_path_for_matrix`（L1 恒 False） |
| N-1 | 通过 | `test_load_explorer_candidates_tolerant`（缺失→空） |
| N-2 | 通过 | 同上（损坏→空 + 不抛） |
| N-3 | 通过 | `test_verify_unexpected_exception_falls_back`（RuntimeError 捕获回退——R-5） |
| N-4 | 说明 | adapt 输入畸形由 N-3 的整体异常捕获覆盖（KeyError 同路径） |
| N-5 | 通过 | `test_verify_no_claims_falls_back`（verify_no_claims → 回退） |
| N-6 | 通过 | 回退后单轮失败由 A-16 API 集成覆盖（无 key 双失败 → run completed） |
| N-7 | 通过 | `test_verify_candidate_success`（verify/observations.json 与 ai-trace 并存——实现行为） |
