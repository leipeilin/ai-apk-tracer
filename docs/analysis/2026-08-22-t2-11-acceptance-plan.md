# 任务验收方案：T2.11 核验 agent（verify_agent）

> **任务编号**：T2.11
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/2026-08-22-t2-11-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测（真实索引 + FakeVerifyAI 协议替身）+ 协议契约既有测试回归 + 全量回归

---

## 1. 验收范围

- T2.11 全部交付物：`verify_entry` 协议入口、命题生成器、盲验构造（facts/chain_facts）、`VerifyAgent` 取证循环（终止/预算/回查/一致性/落盘）、`dispatch_read` 提升重构。
- 验收通过即视为任务完成、可进入提交。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 协议入口正确性 | mock `AIAnalyzer._invoke_prompt` → `verify_entry(VerifyInput)` | 以 `("verify", "1.0.0", model_input, VerifyOutput, "verify")` 调用；AI 不可用返回 unavailable |
| A-2 | 命题生成六类触发 | 构造含全部字段的候选（guard_status/authorization_status 非 unknown）→ `build_verify_claims` | 六类各 1 条，索引 0..5 连续；statement 含 path:line 锚点 |
| A-3 | 命题条件性 | 最小候选（无 guard/authorization 字段） | 仅 entry/source/propagation/sink 四条；字段缺失不崩 |
| A-4 | 命题确定性 | 同输入两次调用 | 输出逐字节相等（无随机/时序依赖） |
| A-5 | 盲验输入无假设层（M2 验收 4.3-6.1） | fake 捕获 VerifyInput → 序列化全文断言 | 不含 `severity_hint`/`confidence_tier`/`hypothesis`/`impact_proposal`/`blocking_gaps`/`reasoning`/`needs_expansion` 子串；所有 evidence_refs 的 claim 均为 null（评审 R-5） |
| A-6 | deterministic_facts 结构化 | `build_deterministic_facts` | fact_type 七类子集；statement 为位置/状态事实；guard_blocked 时 guard 事实含阻断陈述 |
| A-7 | chain_facts 剥离 | 探索候选（chain_proposal 含五假设字段 + evidence_refs 带 claim 文本）→ `build_chain_facts` | 仅含 source/sink/hops/evidence_refs/call_tree_refs；无五假设字段；**evidence_refs 的 claim 全为 None**（评审 R-5）；`explorer_candidate=None` → None |
| A-8 | 首轮 code_context 双路径 | ①sources/sinks 带 method_id（真实索引）②无 method_id 仅 path:line | ①context 含方法体；②context 含行窗口内容；均 ≤9500 字符（评审 R-9） |
| A-9 | 终止=命题全部判定（代码判定） | fake 轮 1 判全部 4 条 claims + `loop.done=false`（模型不自声明） | 仍终止：`terminated_by="all_claims_decided"`，`requests_used=1`——loop.done 不作为终止依据 |
| A-9b | 自声明 done 不提前终止（评审 R-3） | fake 轮 1 判 2 条 + `loop.done=true` | **不终止**：轮 2 继续（`requests_used=2`） |
| A-10 | 部分判定续轮 | 轮 1 判 2 条（2 条缺），轮 2 判余下 | `requests_used=2`；合并 verdicts 覆盖全部 index；后轮覆盖前轮同 index |
| A-11 | 轮数预算尽 | 4 轮均有未判定命题 | `terminated_by="round_budget"`；`analysis_complete=False`；已证命题保留；**undecided_claim_indices 非空**（缺口清单——评审 R-6） |
| A-12 | 读码请求执行 | fake 轮 1 返回 read_requests（get_method_body 真实 method_id） | 轮 2 输入 code_context 含取回内容；`read_requests_used` 计数 |
| A-13 | 读码预算耗尽（读码预算——非 AI 调用数，评审 R-10①） | max_requests_per_candidate=1 + 模型持续请求 | **预算耗尽且尚有未判定命题 → 提前终止**：`terminated_by="request_budget"`，已证命题保留（评审 R-4——省空转轮） |
| A-14 | 证据回查过滤 | fake 返回混合证据（可回查/文件缺失/行越界） | 仅可回查入池；`unverifiable_evidence_count` 精确；跨轮去重 |
| A-15 | 一致性规则 1/2 | fake 输出 supports+flaw_holds=False；refutes+flaw_holds=True | 均降级 unresolved + `consistency_downgraded=True` + note；claims 原文保留 |
| A-15b | 一致性规则 4（评审 R-2） | supports + 核心命题（propagation）still_unknown | 降级 unresolved |
| A-16 | 一致性规则 3 | supports + 核心命题（propagation）refuted | 降级 unresolved |
| A-17 | 规则 3 非核心不触发 | supports + guard_effective（非核心）refuted | 不降级（verdict 保留 supports） |
| A-18 | AI 失败容错 | fake 返回 error（非熔断） | `status="failed"`；不抛；返回含 rounds 审计 |
| A-19 | 熔断终态 | fake 返回 circuit_breaking | `status="skipped"`，`terminated_by="short_circuit"` |
| A-20 | 落盘审计 | verify 后读 `run_dir/verify/observations.json` | entries 追加；含 candidate_id/terminated_by/rounds（model_input_hash+output）；0600 权限 |
| A-21 | dispatch_read 重构零回归 | 既有 explorer 测试全过 | `_dispatch_read` 委托模块级函数后行为不变 |
| A-22 | 返回契约完整性 | verify() 返回 keys | status/terminated_by/output/rounds/requests_used/read_requests_used/undecided_claim_indices/consistency_downgraded 齐备 |

## 3. 回归标准

- [ ] `cd backend && .venv/bin/python -m pytest` 全量通过（基线 1073 passed / 0 failed，只增不减）；
- [ ] `scripts/check-backend.sh` 通过；改动文件 ruff 零错误；
- [ ] `sync-ai-protocol.py --check` 通过（协议零改动——本任务不动 schema/prompt）；
- [ ] 默认配置行为不变（verify_agent 不接线 orchestrator——零运行时影响）。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 无 sources/sinks 的候选 | 空列表 | 命题仅 entry_reachable 等可选类；**claims 为空（component_name 也缺）→ status=skipped + terminated_by=no_claims 快速返回**（评审 R-8——不构造 VerifyInput） |
| N-2 | claims 超 32 | 构造超限触发字段 | 截断 32（优先序保留核心类） |
| N-3 | VerifyOutput 解析失败 | fake 返回缺 required 的 analysis | `status="failed"`；轮记录 output_invalid 语义 |
| N-4 | read_requests 操作未知 | fake 返回 operation="class_hierarchy" | not_found 统一结构（dispatch 兜底） |
| N-5 | observations.json 损坏 | 预写非法 JSON | 重新初始化（warning 日志），不抛 |
| N-6 | evidence end_line<line | 倒序区间证据 | 回查失败丢弃+计数 |
| N-7 | 连续 verify 同候选 | 两次调用 | 独立执行（无状态残留）；observations 双 entry |

## 5. 回退方案

- verify_agent.py 独立新模块（零接线）——整体删除即回退；ai.py `verify_entry` 单方法；explorer.py dispatch 提升为纯重构（git revert 单文件）。

## 6. 验收记录（实施后填写）

> **验收日期**：2026-08-22。**结果：全部通过**。全量回归 **1103 passed / 0 failed**（基线 1073 + 新增 30）；`sync-ai-protocol.py --check` 通过（协议零改动）；`scripts/check-backend.sh` 通过；改动文件 ruff 零错误。

| 编号 | 结果 | 实测说明（测试函数） |
|---|---|---|
| A-1 | 通过 | `test_verify_entry_invokes_prompt`：五参（verify/1.0.0/VerifyInput/VerifyOutput/"verify"）断言 |
| A-2 | 通过 | `test_build_claims_six_kinds`：六类按序 + index 连续 + path:line 锚点 |
| A-3 | 通过 | `test_build_claims_minimal`：最小候选仅 entry_reachable |
| A-4 | 通过 | `test_build_claims_deterministic`：两次 JSON 相等 |
| A-5 | 通过 | `test_blind_input_contains_no_hypothesis_layer`：七字段子串断言 + chain_facts evidence claim 全 None（含 explorer_candidate 路径） |
| A-6 | 通过 | `test_build_facts_structured`：七类 fact_type + guard_blocked 阻断陈述 |
| A-7 | 通过 | `test_chain_facts_stripped`：五字段投影 + claim None + 空值防御 |
| A-8 | 通过 | `test_initial_context_dual_path`：method_id 方法体 / path:line 行窗口双路径 + ≤9500 |
| A-9 | 通过 | `test_terminate_all_decided`：done=false 仍终止（代码判定） |
| A-9b | 通过 | `test_loop_done_does_not_terminate`：done=true 未全判定 → 轮 2 继续（R-3） |
| A-10 | 通过 | `test_partial_then_complete_merge`：后轮覆盖同 index（still_unknown→confirmed） |
| A-11 | 通过 | `test_round_budget_exhaustion`：round_budget + 已证保留 + undecided 物化（R-6） |
| A-12 | 通过 | `test_read_requests_executed`：取回内容入轮 2 输入 + 计数 |
| A-13 | 通过 | `test_request_budget_early_termination`：读码预算尽且未全判定 → request_budget 提前终止（R-4，无空转轮） |
| A-14 | 通过 | `test_evidence_filtered`：2 项不可回查丢弃 + filter_note |
| A-15 | 通过 | `test_consistency_rules_flaw_conflict`：规则 1/2 双向 |
| A-15b | 通过 | `test_consistency_rule_core_unknown`：规则 4 + claims 原文保留（R-2） |
| A-16 | 通过 | `test_consistency_rule_core_refuted`：规则 3 |
| A-17 | 通过 | `test_consistency_non_core_refuted_not_triggered`：guard_effective refuted 不触发 |
| A-18 | 通过 | `test_ai_failure_tolerated`：failed + rounds 审计 |
| A-19 | 通过 | `test_circuit_skipped`：short_circuit 终态 |
| A-20 | 通过 | `test_observation_persisted`：追加 + model_input_hash + output 全量 |
| A-21 | 通过 | 既有 explorer 测试全过（117 项——dispatch_read/filter_evidence 提升为模块级后零行为变化） |
| A-22 | 通过 | `test_result_contract_complete`：八字段契约断言 |
| N-1 | 通过 | `test_no_claims_skipped`：no_claims 快速返回零调用（R-8） |
| N-2 | 说明 | 六类命题上限 6 条（<32），截断为防御性代码（pending[:32]）；`test_build_claims_six_kinds` 断言上限行为 |
| N-3 | 通过 | `test_output_invalid_fails`：缺 required → failed + output_invalid 轮记录 |
| N-4 | 通过 | `test_unknown_read_operation_not_found`：协议层四操作枚举挡未知操作；实现层 dispatch 兜底 not_found（目标缺失场景） |
| N-5 | 通过 | `test_observation_corrupted_reinit`：损坏 JSON 重新初始化 |
| N-6 | 通过 | `test_evidence_end_line_inverted_dropped`：倒序区间丢弃 |
| N-7 | 通过 | `test_repeated_verify_independent`：独立执行 + 双 entry |
