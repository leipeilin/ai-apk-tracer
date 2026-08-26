# 任务验收方案：T2.7 归一化 + funnel 扩展

> **任务编号**：T2.7
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t2-7-implementation-plan.md`
> **状态**：已修订（第 1 轮评审 R-1~R-7 全部采纳：A-6/A-7/A-16 调整、新增 A-18）
> **验收方式**：pytest 单测（真实索引/真实 schema fixture）+ 全量回归 + 默认关闭行为断言

---

## 1. 验收范围

- 本方案覆盖 T2.7 全部交付物：归一化模块、funnel 扩展（candidate_source / 三分流 / identity 分源 / related 排除）、orchestrator 时序前移与集成、candidate.schema.json 扩展。
- 验收通过即视为任务完成、可进入提交。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 归一化产出合法 Candidate（10 项 required） | `test_explorer_normalization.py`：真实 ExplorerCandidate fixture（validated，含 hops/evidence_refs）→ `normalize_explorer_candidates` → `jsonschema.validate` 对 `schemas/candidate.schema.json`；断言 `rule_id="EXPLORER_AGENT"`、`evidence_level="L2"`、`confidence_tier="high"`、`candidate_source="explorer"`、sources/sinks 形状（kind/status/path/line/text/method_id） | schema 校验通过；关键字段值与 T0.6 映射表一致 |
| A-2 | T0.6 可执行契约不漂移 | 既有 `test_normalization_mapping.py` 全过（MAPPING/SEVERITY_KEYWORDS 与实现共享单一事实源：归一化模块 import 该常量） | 既有测试零改动全过 |
| A-3 | other 组件 drop + 审计 | 构造 `component.kind="other"` 的 validated 候选 → normalize | 不产出候选；`component_other_dropped=1` |
| A-4 | 仅 validated 归一化 | 同批构造 validated / partially_validated / unverified / validation=None 四类候选 → normalize | 只产出 validated 一条；计数 partial_kept=1、unverified_kept=1（含 None） |
| A-5 | severity 启发式与封顶 | impact_proposal 含"远程执行"→ high；含"仅提示信息"→ low；无关文本 → medium；任意文本不出现 critical | 启发式命中时 blocking_gaps 含 `EXPLORER_SEVERITY_HYPOTHESIS` |
| A-6 | blocking_gaps 分支组装 | 分别构造 failed_hop_indices=[1]（partial 档） / blocked_by_guard=True / notes 含"回查过程异常"的候选；另构造干净 validated（notes="3/3 跳回查通过"） | 对应 `EXPLORER_HOP_UNVERIFIED`(message 含"第 1 跳") / `EXPLORER_GUARD_BLOCKED`(critical=True) / `EXPLORER_CHAIN_INCOMPLETE`；**干净 validated 不产 `EXPLORER_CHAIN_INCOMPLETE`（评审 R-4：纯成功摘要不产 gap），无上述分支时 blocking_gaps 为 `[]`（或仅含 severity 假设 gap）** |
| A-7 | guard_blocked 双字段语义转换 | blocked_by_guard=True 的 validated 候选 → 归一化候选 → 断言 `guard_blocked=True` **且** `guard_blocks=[{type:"debuggable",...}]`（评审 R-3）→ 送 `CandidateFunnel.process` + `DecisionEngine.decide` | funnel：ai_required=False（不送 AI）；decision：evidence_decision=="blocked"（guard_blocks 列表驱动） |
| A-8 | path 前缀剥离 | evidence_refs[].path="sources/com/example/A.java" → 归一化 locations/sources path | 剥离为 "com/example/A.java"（索引口径）；hops 派生 path 不受影响 |
| A-9 | 归一化异常不中断 | 构造 hops 缺失/字段畸形的 validated 候选混入正常候选 | 异常候选跳过 + `normalization_errors=1`；正常候选照常产出 |
| A-10 | funnel 三分流路由 | `test_candidate_funnel.py`：三组候选（explorer_validation_status 分别为 validated/partially_validated/unverified，evidence_level=L2）→ `process` | disposition 分别为 explorer_promoted / explorer_partial / explorer_unverified；promoted 组 ai_required=True（L2 路由）；partial/unverified 组 ai_required=False |
| A-11 | identity 不跨源合并 | 同形状候选仅 `candidate_source` 不同（rule 缺省 vs explorer）→ `build_candidate_identity` | `deterministic_fact_hash` 不同（分源）；`CandidateFunnel.process` 分为两个身份组 |
| A-12 | related_candidate_ids 回填与身份安全 | 构造探索归一化候选与规则候选（同 component_name + 同 sink method_id）→ funnel → `link_related_candidates` → 再次 `build_candidate_identity` | 双向 related_candidate_ids 写入；identity recompute 与首次一致（排除字段生效）；幂等（二次调用不重复追加）；sink 不匹配的对照组无关联 |
| A-13 | 规则候选零行为变化（默认路径） | 既有 funnel 测试全过（不传 candidate_source 的候选走 deterministic_precheck 原路径）；构造无 candidate_source 候选对比 T2.7 前后 funnel 输出 | disposition/ai_required/summary 与基线一致 |
| A-14 | summary 计数 | 三分流候选过 funnel | summary 含 explorer_promoted / explorer_partial / explorer_unverified 计数 |
| A-15 | 集成时序：explorer 在 funnel 前 | API 级测试（`test_explorer.py` 扩展）：`explorer.enabled=true` 跑 run → 读 run_manifest.stages | explorer 阶段记录位置在 candidate_funnel 之前；stage summary 含 validation_counts 与 normalization_counts |
| A-16 | 集成：归一化候选并入主链 | **实例级**（评审 R-7）：复用 `test_explorer.py` FakeAnalyzer 模式 + 真实索引，直接构造 `ScanOrchestrator`（或驱动 `_run_explorer_stage`）→ 断言归一化候选并入 candidates | candidates 含 `rule_id=EXPLORER_AGENT` 候选；explorer/candidates.json 保留全三档原始形状 |
| A-17 | schema 扩展合法 | `jsonschema.validate` 对含 candidate_source 的候选 + `test_config`/schema 相关既有测试 | candidate.schema.json 新属性不破坏既有校验 |
| A-18 | run 级 AI 预算总量语义（评审 R-1） | `explorer.enabled=true` + `context_budget.max_requests_per_run=3`（小值）+ FakeAnalyzer 消耗 2 个探索请求 → 完整 run → 读 `ai_analysis` stage summary | 探索+规则 AI 两阶段实际请求总数 ≤ 3（共享同一预算池，无重置翻倍）；summary 含 `explorer_requests_used=2` / `ai_stage_requests_used` 分列，`requests_used` 为 run 累计值 |

## 3. 回归标准

- [ ] `cd backend && python -m pytest` 全量通过（基线 1029 passed / 0 failed，只增不减）；
- [ ] `scripts/check-all.sh` 通过（含 ruff）；
- [ ] 默认配置（`explorer.enabled=false`）行为不变：funnel 对无 candidate_source 候选路径零改动（A-13 断言 + 既有测试全过）；无探索候选进入任何既有产物；
- [ ] 无 lint 绕过注释；无硬编码密钥。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 归一化输入畸形（hops 缺失 / evidence_refs 元素非 dict / method_id 无 "#") | 混入正常批次 | 单候选跳过 + 计数 + warning 日志，批次不中断 |
| N-2 | explorer_validation_status 未知值 | funnel 收到 status="weird" 的 explorer 候选 | disposition 保守落 explorer_unverified，不送 AI |
| N-3 | candidate_source 未知值 | funnel 收到 candidate_source="unknown" | 走规则候选默认路径（非 explorer 分支），不崩 |
| N-4 | link 关联自引用 | 探索候选与探索候选同链 | 不与同源候选互写（仅 explorer→rule 方向关联） |
| N-5 | 探索候选与规则候选不同链 | component_name 或 sink method_id 不匹配 | 无 related_candidate_ids 写入 |
| N-6 | AI 探索全失败（熔断） | FakeAnalyzer 返回 circuit_breaking | 零候选 → 零归一化 → 主链照常完成（阶段不挂，沿用 T2.5b 语义） |

## 5. 回退方案

- 任一验收不过：`explorer.enabled=false` 保持默认关闭（主链行为与基线一致）；代码层按文件粒度回退（explorer_normalization.py 独立新增可整体移除；candidate_funnel/orchestrator 改动按 git revert 单文件回退）。

## 6. 验收记录（实施后填写）

> **验收日期**：2026-08-22。**结果：全部通过**。全量回归 1053 passed / 0 failed（基线 1029 + 新增 24）；`scripts/check-backend.sh` 通过（compileall + pytest + 规则契约 30）；改动文件 ruff 零错误（含顺带修复既有 11 项：candidate_funnel/orchestrator 的 UP035/UP040/FURB188/SIM103/BLE001/I001 与 test_explorer_validation/test_p0_closure 遗留 F841/F401）。

| 编号 | 结果 | 实测说明（测试函数） |
|---|---|---|
| A-1 | 通过 | `test_explorer_normalization.py::test_normalize_validated_produces_schema_valid_candidate`：jsonschema 通过 + 10 项映射断言 |
| A-2 | 通过 | `test_normalization_mapping.py` 既有断言零改动全过 + 新增 `test_severity_keywords_single_source`（反向 import 生产常量） |
| A-3 | 通过 | `test_normalize_other_component_dropped_with_audit` |
| A-4 | 通过 | `test_normalize_only_validated_status`（partial_kept=1 / unverified_kept=2 含 pending） |
| A-5 | 通过 | `test_severity_gap_attached_on_keyword_hit` + mapping 关键词测试 |
| A-6 | 通过 | `test_clean_validated_has_no_incomplete_gap` / `test_guard_gap_is_critical_and_dual_fields` / `test_error_notes_produces_incomplete_gap` |
| A-7 | 通过 | `test_guard_blocked_candidate_skips_ai_and_decides_blocked`：funnel ai_required=False **且** decision evidence_decision=="blocked"（R-3 双字段生效） |
| A-8 | 通过 | `test_evidence_refs_path_prefix_stripped` / `test_locations_fallback_to_first_hop` |
| A-9 | 通过 | `test_malformed_candidate_skipped_with_count` |
| A-10 | 通过 | `test_candidate_funnel.py::test_explorer_three_way_disposition_routing`（promoted 送 AI + partial/unverified 不送） |
| A-11 | 通过 | `test_identity_includes_candidate_source_no_cross_source_merge`（fact_hash 分源 + 两组） |
| A-12 | 通过 | `test_link_related_bidirectional_and_idempotent` / `test_link_related_no_match_on_different_chain` / `test_related_candidate_ids_and_explorer_id_excluded_from_identity`（多键去重 + method/location 交叉匹配） |
| A-13 | 通过 | 既有 funnel 测试全过 + `test_unknown_candidate_source_falls_back_to_rule_path`（coverage_insufficient 原路径） |
| A-14 | 通过 | `test_explorer_three_way_disposition_routing` summary 断言（1/1/2） |
| A-15 | 通过 | `test_explorer.py::test_orchestrator_explorer_stage` 扩展：stage_names 中 explorer < candidate_funnel |
| A-16 | 通过 | `test_explorer_stage_normalizes_validated_into_main_candidates`（真实索引 validated 链 → 归一化返回 + 原始形状落盘 + normalization_counts） |
| A-17 | 通过 | A-1 的 jsonschema（含 candidate_source）+ 既有 schema 测试全过 |
| A-18 | 通过 | `test_explorer_stage_budget_shared_with_ai_stage`（requests_used=1 累计口径 + explorer/ai_stage 分列）+ `test_explorer_stage_budget_cap_rejects_beyond_limit`（预算 1，第二轮被拒，fake.calls==1） |
| N-1~N-6 | 通过 | N-1 malformed_skip / N-2 weird→unverified / N-3 unknown source→rule 路径 / N-4 同源不互写 / N-5 不同链零关联 / N-6 熔断零候选（T2.5b 既有）+ 预算耗尽（A-18 补充） |
