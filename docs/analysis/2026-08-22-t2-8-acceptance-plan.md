# 任务验收方案：T2.8 explorer_deep_dive 实现

> **任务编号**：T2.8
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/2026-08-22-t2-8-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测（真实索引 + FakeDeepDiveAI 协议替身）+ schema 同步门禁 + 全量回归

---

## 1. 验收范围

- T2.8 全部交付物：analyzer `deep_dive_entry` 协议入口、`deep_dive_partials` 驱动（missing_facts/context/回查/多轮/容错）、`ExplorerCandidate.deep_dive` 模型与 schema 同步、orchestrator 集成（预算回调 + summary + 记账）。
- 验收通过即视为任务完成、可进入提交。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 协议入口正确性 | `test_explorer.py`：mock `AIAnalyzer._invoke_prompt` → 调 `deep_dive_entry(DeepDiveInput)` | 以 `("explorer-deep-dive", "1.0.0", model_input, DeepDiveOutput, "explorer-deep-dive")` 参数调用；AI 不可用时返回 unavailable 结构（不抛） |
| A-2 | 仅 partial 送深挖 | 构造 validated / partially_validated / unverified / validation=None 四类候选 → `deep_dive_partials` | 仅 partial 候选获得 `deep_dive` 字段；其余为 None；计数 `partial_total=1, attempted=1` |
| A-3 | 链不可变（M2 验收 4.3-5.4） | 深挖前后 `copy.deepcopy(chain_proposal)` 对比 + `validation` 对比 | 逐字节相等（hops/evidence_refs/三档均不变）；仅新增 `deep_dive` 字段 |
| A-4 | missing_facts 确定性生成 | 构造 `failed_hop_indices=[1]` + `blocked_by_guard=True` 的 partial 候选 | missing_facts 含跳 i=1 的调用关系待证命题 + guard 可达性命题；与 validation 数据一致（确定性——同输入同输出） |
| A-5 | code_context 组装与门禁 | failed hops 方法体进入首轮 context（含真实源码行）；`allow_external_code=False` → context=None | context 有界（≤9500 字符）且含方法体内容；门禁关闭时不外发代码 |
| A-6 | 证据回查过滤 | FakeDeepDiveAI 返回混合证据：可回查 (path,line) / 文件不存在 / 行越界 / sources/ 前缀形态 | 仅可回查证据保留；`unverifiable_evidence_count` 精确计数；前缀形态剥离后命中 |
| A-7 | 事实判定保留与合并 | 轮 1 判 claim 0=still_unknown，轮 2 判 claim 0=confirmed | 最终 resolved_facts 含 claim 0=confirmed（后轮覆盖）；轮记录各含当轮全量 output |
| A-8 | 多轮终止三态 | ①`analysis_complete=True` 首轮即止 ②停滞（round≥2 且连续两轮无新增判定）→ incomplete ③跑满 4 轮 → incomplete | ①`requests_used=1, status=completed` ②③`status=incomplete` 且 `requests_used` 对应；轮记录数一致；②首轮全 still_unknown 不触发首轮终止（第 2 轮仍执行） |
| A-9 | AI 失败容错 | FakeDeepDiveAI 返回 `{"status":"error"}`（非熔断） | 该候选 `deep_dive.status="failed"`；批次不中断（其余候选正常） |
| A-10 | run 级预算耗尽 | budgeted 回调注入 `max_requests_per_run` 已耗尽状态（预置计数） | 候选 `status="skipped"`；`deep_dive_requests_used=0`；不抛 |
| A-11 | deep_dive_call 未注入 | 构造 ExplorerOrchestrator 不传 deep_dive_call | 全体 skipped + 计数返回（不抛） |
| A-12 | schema 同步 | `sync-ai-protocol.py --check` 无 drift；`ExplorerCandidate.model_validate`（含 deep_dive 字段）通过；`explorer_candidate.schema.json` 含 deep_dive 定义 | 哈希门禁通过；带 deep_dive 的候选落盘合法（jsonschema 校验通过） |
| A-13 | 集成：阶段编排 | 实例级 `_run_explorer_stage`（FakeExploreAI 产出 partial 候选 + FakeDeepDiveAI） | stage summary 含 `deep_dive_counts`（attempted/completed…）与 `deep_dive_requests_used`；candidates.json 含 deep_dive；`validation_counts` 不因深挖变化 |
| A-14 | 集成：L2 复核隔离 | A-13 场景的归一化返回 | 归一化候选列表**不含** partial（即使已深挖 completed）——D1：深挖不升级不归一化 |
| A-15 | 集成：预算分账 | A-13 场景后断言 | run 级 `_ai_requests_used == 探索调用数 + 深挖调用数`（共享池）；`deep_dive_requests_used` 独立计数 |
| A-16 | 落盘审计 | candidates.json 深挖字段含 rounds（model_input_hash/prompt_version/model/status/output） | 轮输入可哈希复现（对齐 T2.5b 审计原则） |
| A-17 | 批次级熔断短路（评审 R-5） | 多个 partial 候选 + FakeDeepDiveAI 首个返回 circuit_breaking/skipped | 第一个候选 failed/skipped 后，剩余候选批量 `status="skipped"` 且零 AI 调用（fake 调用数不增）、零上下文组装 |
| A-18 | 初始证据池（评审 R-9） | chain_proposal.evidence_refs 含可回查与不可回查项混合 → 深挖输入 existing_evidence_refs | 初始池仅含回查过滤存活项；DeepDiveInput.existing_evidence_refs 断言（fake 捕获输入）；过滤计数含初始丢弃 |

## 3. 回归标准

- [ ] `cd backend && .venv/bin/python -m pytest` 全量通过（基线 1053 passed / 0 failed，只增不减）；
- [ ] `scripts/check-backend.sh` 通过；
- [ ] `sync-ai-protocol.py --check` 通过（schema/registry 哈希无 drift）；
- [ ] 改动文件 ruff 零错误；
- [ ] 默认配置（`explorer.enabled=false`）行为不变：深挖随探索阶段整体关闭，主链零影响。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | hops 缺失的 partial | `chain_proposal.hops=[]` 且 status=partially_validated（畸形） | 跳过深挖（无锚定事实），计入 skipped |
| N-2 | failed_hop_indices 越界索引 | 索引 ≥ len(hops) | 对应命题不生成（界内检查），不抛 |
| N-3 | missing_facts 超 32 | 失败跳 >32（畸形大批量） | 截断 32 + remaining_gaps 首项说明截断 |
| N-4 | 模型输出 schema 违规 | FakeDeepDiveAI 返回缺 required 字段的 analysis | status="failed"（轮记录 output_invalid）；repair 状态机兜底后仍失败才到此处 |
| N-5 | 证据 end_line < line | 构造倒序区间证据 | 回查失败丢弃 + 计数 |
| N-6 | 深挖后 candidates.json 重写幂等 | 连续两次 deep_dive_partials（同候选） | 第二次重新深挖（非幂等追加）——results 覆盖（deep_dive 单次执行语义，由调用方保证一次） |
| N-7 | call_tree 方法体缺失 | failed hop 的 method_id 在索引中不存在 | context 段跳过（不抛）；深挖仍执行（输入可能仅含链事实） |

## 5. 回退方案

- 任一验收不过：`explorer.enabled=false`（默认）即完全禁用深挖路径（deep_dive_call 仅在探索阶段内构造）；代码按文件粒度回退（ai.py/explorer.py/orchestrator.py 独立小改 + ai_models schema revert）。

## 6. 验收记录（实施后填写）

> **验收日期**：2026-08-22。**结果：全部通过**。全量回归 **1073 passed / 0 failed**（基线 1053 + 新增 20）；`sync-ai-protocol.py --check` 哈希门禁通过（explorer_candidate.schema.json 含 deep_dive $defs）；`scripts/check-backend.sh` 通过；改动文件 ruff 零错误（含顺带修复：ai.py 既有 6 项——I001/BLE001×3/S110/TRY004，及 explorer.py 遗留 noqa 清理——`_dispatch_read` 异常具体化）。

| 编号 | 结果 | 实测说明（测试函数） |
|---|---|---|
| A-1 | 通过 | `test_deep_dive_entry_invokes_prompt`：mock `_invoke_prompt` 断言五参与 explore_entry 同构 |
| A-2 | 通过 | `test_deep_dive_only_partials`：四类候选仅 partial 获 deep_dive |
| A-3 | 通过 | `test_deep_dive_preserves_chain_and_validation`：chain/validation JSON 逐字节相等（M2 4.3-5.4 锚点） |
| A-4 | 通过 | `test_deep_dive_missing_facts_deterministic`：跳命题 + guard 命题；越界索引 [7] 不生成（N-2 并） |
| A-5 | 通过 | `test_deep_dive_code_context_and_gate`：失败跳方法体入 context（≤9500）；`allow_external_code=False` → None；缺失方法体跳过（N-7 并） |
| A-6 | 通过 | `test_deep_dive_evidence_verification`：3 项不可回查丢弃计数；前缀剥离命中；倒序区间拒（N-5 并） |
| A-7 | 通过 | `test_deep_dive_fact_merge_across_rounds`：轮 2 confirmed 覆盖轮 1 still_unknown；轮记录含全量 output |
| A-8 | 通过 | 三态：`test_deep_dive_terminates_on_complete`（1 轮）/ `test_deep_dive_stagnation_after_two_rounds`（首轮不判停滞，2 轮止）/ `test_deep_dive_budget_exhaustion`（每轮新判定跑满 4 轮） |
| A-9 | 通过 | `test_deep_dive_ai_failure_tolerated`：failed + 批次继续（second completed） |
| A-10 | 通过 | `test_deep_dive_run_budget_exhausted_skips`：circuit_breaking → skipped |
| A-11 | 通过 | `test_deep_dive_not_injected_all_skipped`：计数 dict 全量断言 |
| A-12 | 通过 | `test_candidate_with_deep_dive_schema_valid`：jsonschema 对再生成 schema 校验通过 |
| A-13 | 通过 | `test_explorer_stage_deep_dive_integration`：deep_dive_counts/partial 保持/validation_counts 不变/candidates.json 含 deep_dive |
| A-14 | 通过 | 同上：`normalized == []`（D1——深挖 completed 也不归一化） |
| A-15 | 通过 | 同上：`_ai_requests_used == 2`（探索 1 + 深挖 1 共享池）+ summary 分列 |
| A-16 | 通过 | fact_merge/integration 的 rounds 断言（model_input_hash/prompt_version/model/status/output） |
| A-17 | 通过 | `test_deep_dive_batch_short_circuit`：fake.calls==1，第二候选零调用零上下文 |
| A-18 | 通过 | `test_deep_dive_initial_evidence_pool_filtered`：初始池仅存活项 + 过滤计数含初始丢弃 |
| N-1 | 通过 | `test_deep_dive_no_hops_skipped`（零 AI 调用） |
| N-2 | 通过 | A-4 内（越界索引 [7] 不生成命题） |
| N-3 | 通过 | `test_deep_dive_missing_facts_truncated`（32 跳 + guard = 33 → 截断 + gaps 首项说明） |
| N-4 | 通过 | `test_deep_dive_output_invalid_fails`（缺 analysis_complete → failed + output_invalid 轮记录） |
| N-5 | 通过 | A-6 内（end_line < line 拒） |
| N-6 | 说明 | 单次执行语义由调用方（_run_explorer_stage 每候选仅深挖一次）保证；重复调用覆盖非追加——实现行为，无专用测试 |
| N-7 | 通过 | A-5 内（Missing 方法体跳过） |
