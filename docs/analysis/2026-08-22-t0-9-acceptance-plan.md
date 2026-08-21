# 任务验收方案：T0.9（verify 核验协议）

> **任务编号**：T0.9
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/2026-08-22-t0-9-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测 + 生成脚本 check + registry 校验 + 全量回归

---

## 1. 验收范围

- 5 个新模型 + 2 个 schema + 2 个 prompt 文件 + registry 条目 + 校验测试。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | VerifyInput 有效解析 | 构造含 claims/evidence_refs/deterministic_facts 的输入 | 通过；`claims[0].kind` 正确回读 |
| A-2 | VerifyOutput 有效解析 | 构造含 verdict（supports_candidate）/confidence_tier/exploitability（6 字段）/refutation_basis/claims_verdicts/read_requests/loop 的输出 | 通过；L2 关键决策字段正确回读（评审 R-2） |
| A-3 | 必填缺失拒绝 | 缺 `claims`/`candidate_id`/`verdict`/`confidence_tier`/`flaw_holds`/`exploitability`/`loop`/`summary`/`reasoning` | `ValidationError`（`missing`） |
| A-4 | 枚举越界拒绝 | `kind="bogus"`、`verdict="supports"`（旧值拒绝，评审 R-2）、`conclusion="verified"`、`fact_type="bogus"`、`refutation_basis=["bogus"]` | `ValidationError` |
| A-5 | 盲验双重结构断言 | `VerifyInput.model_fields` 与 `VerifyChainFacts.model_fields` 均不含 `hypothesis`/`impact_proposal`/`confidence`/`reasoning`/`needs_expansion`（评审 R-1/R-3） | 断言通过 |
| A-6 | 边界约束 | `claims` 33 项、`index=-1`、`code_context` 10_001 字符 | `ValidationError` |
| A-7 | claims 空数组拒绝 | `claims=[]`（`min_length=1`） | `ValidationError`（`too_short`） |
| A-8 | prompt 文件存在 | `prompts/verify/1.0.0/{system,user}.md` | 通过 |
| A-9 | registry 条目与哈希 | `sync-ai-protocol.py --check` | 退出码 0；`verify@1.0.0` 条目一致 |
| A-9b | 渲染路径一致性 | `_prompt_variable("verify") == "verify_input_json"`；user.md placeholder 与 allowed_placeholders 匹配 | 通过 |
| A-10 | schema 生成一致 | 两个新 schema 文件存在且与模型一致 | `--check` 通过 |
| A-11 | 测试通过 | `cd backend && .venv/bin/python -m pytest tests/test_ai_models.py tests/test_prompt_registry.py -q` | 全部通过 |
| A-12 | 全量回归 | `.venv/bin/python -m pytest -q` | 除 3 个 pre-existing guard_verifier 失败外全部通过 |
| A-13 | 统一校验 | `scripts/check-all.sh` | 同上，无新增失败 |

## 3. 回归标准

- [ ] 既有协议（含 T0.3 explorer-deep-dive）schema/测试全部通过。
- [ ] `--write` 后 `--check` 幂等；`test_committed_schemas` 自动覆盖两个新 schema。
- [ ] T0.7 的 `test_prompt_version_declared_matches_registry` 更新预期：verify 已注册（先声明后注册闭合）。
- [ ] `ruff check` 通过。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | evidence 路径穿越 | `evidence[0].path="../../x"` | `ValidationError` |
| N-2 | `chain_facts` 为 None | 输入不含 chain_facts（规则候选） | 通过（可空） |
| N-3 | read_requests 超限 | 9 项 | `ValidationError`（max_length=8） |
| N-4 | VerifyOutput 含假设字段 | 输出加 `hypothesis` | `extra_forbidden`（核验输出不含假设语义） |
| N-5 | done 与空判定矛盾 | `loop.done=true` 且 `claims_verdicts=[]` | `ValidationError`（`_done_requires_verdicts` validator；评审 R-5） |
| N-6 | test_config 同步 | `test_prompt_version_declared_matches_registry` 断言 `(verify,1.0.0) in registered`（评审 R-4） | 断言通过 |

## 5. 回退方案

- 任一验收点失败：修复后复验；registry 条目可单独回退。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审 7 项意见第 1 轮全部采纳（含 2 项高严重度：R-1 chain_facts 剥离版、R-2 L2 关键决策字段对齐）。实施中 1 处测试代码 bug 已修（参数化路径导航对 list 字符串索引，重写为独立断言）。全量 903 passed + 3 个 pre-existing guard_verifier 失败（同前）。

| 编号 | 结果（通过/失败/部分通过） | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | VerifyInput 有效解析（claims/chain_facts/deterministic_facts 结构化） | - |
| A-2 | 通过 | VerifyOutput 有效解析（verdict=refutes_candidate、exploitability 6 字段、refutation_basis） | - |
| A-3 | 通过 | 必填缺失拒绝（含 confidence_tier/exploitability） | - |
| A-4 | 通过 | 枚举越界拒绝（verdict="supports" 旧值拒绝、kind/conclusion/fact_type/refutation_basis） | - |
| A-5 | 通过 | 盲验双重结构断言（VerifyInput/VerifyChainFacts 均无假设字段） | - |
| A-6 | 通过 | claims 33 项/index=-1/code_context 10_001/read_requests 9 → 拒绝 | - |
| A-7 | 通过 | claims=[] → `too_short` | - |
| A-8 | 通过 | 两个 prompt 文件存在 | - |
| A-9 | 通过 | `sync-ai-protocol.py --check` 退出码 0；verify@1.0.0 一致 | - |
| A-9b | 通过 | `_prompt_variable("verify") == "verify_input_json"` | - |
| A-10 | 通过 | 两个新 schema（9.2KB/10.9KB）与模型一致 | - |
| A-11 | 通过 | 三测试文件 110 项全过 | - |
| A-12 | 通过 | 全量 pytest：903 passed + 3 pre-existing | - |
| A-13 | 通过 | check-all：903 passed + 3 pre-existing；ruff 全过 | - |
| N-1 | 通过 | evidence 路径穿越 → 拒绝 | - |
| N-2 | 通过 | chain_facts=None（规则候选）通过 | - |
| N-3 | 通过 | read_requests 9 项 → 拒绝 | - |
| N-4 | 通过 | 输出含 hypothesis → `extra_forbidden` | - |
| N-5 | 通过 | done=true 且 claims_verdicts=[] → `_done_requires_verdicts` 拒绝 | - |
| N-6 | 通过 | test_config verify 断言改为已注册，通过 | - |
