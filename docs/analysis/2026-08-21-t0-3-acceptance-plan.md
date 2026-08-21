# 任务验收方案：T0.3（explorer_deep_dive 协议 Schema + prompt 骨架）

> **任务编号**：T0.3
> **日期**：2026-08-21
> **依据实施方案**：`docs/analysis/2026-08-21-t0-3-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测 + 生成脚本 check + registry 校验 + 回归

---

## 1. 验收范围

- 3 个新模型 + 2 个 schema 文件 + 2 个 prompt 文件 + registry 条目 + 校验测试。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | DeepDiveInput 有效解析 | 构造有效输入（含 chain_proposal + missing_facts + existing_evidence_refs） | 通过；`chain_proposal` 复用 T0.1 模型 |
| A-2 | DeepDiveOutput 有效解析 | 构造含 resolved_facts 的有效输出 | 通过；`claim_index`/`conclusion` 正确回读 |
| A-3 | 必填缺失拒绝 | 缺 `DeepDiveInput.chain_proposal` / `DeepDiveOutput.summary` / `ResolvedFact.reasoning` | `ValidationError`（`missing`） |
| A-4 | 枚举越界拒绝 | `conclusion="verified"`、`DeepDiveInput` 无自枚举（结构） | `conclusion` 非法 → `ValidationError` |
| A-5 | 边界约束 | `claim_index=-1`、`missing_facts` 33 项、`resolved_facts` 33 项 | 均抛 `ValidationError` |
| A-6 | prompt 文件存在 | `prompts/explorer-deep-dive/1.0.0/{system,user}.md` 存在 | 通过 |
| A-7 | registry 条目与哈希 | `sync-ai-protocol.py --check` | 退出码 0；`explorer-deep-dive@1.0.0` 条目 input/output model 与 schema 文件一致、哈希匹配 |
| A-8 | schema 生成一致 | 两个新 schema 文件存在且与模型一致 | `--check` 通过 |
| A-9 | prompt registry 测试 | `cd backend && .venv/bin/python -m pytest tests/test_prompt_registry.py -q` | 全部通过（新协议不破坏既有断言） |
| A-9b | 渲染路径一致性 | 断言 `_prompt_variable("explorer-deep-dive") == "explorer_deep_dive_input_json"`，且 registry 编译后 user.md placeholder 与 allowed_placeholders 匹配（评审 R-1，覆盖真实渲染路径） | 通过 |
| A-10 | 全量回归 | `.venv/bin/python -m pytest -q` | 除 3 个 pre-existing guard_verifier 失败外全部通过 |
| A-11 | 统一校验 | `scripts/check-all.sh` | 同上，无新增失败 |

## 3. 回归标准

- [ ] 既有 6 个协议（preflight/l1-triage/l2-review/repair/finalization + 无新增破坏）schema/测试全部通过。
- [ ] `--write` 后 `--check` 幂等。
- [ ] `test_committed_schemas_exactly_match_stable_model_generation` 自动覆盖两个新 schema。
- [ ] registry placeholder 与 `user.md` 一致（`deep_dive_input_json`）。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | `ResolvedFact.evidence` 路径穿越 | `evidence[0].path="../../x"` | `ValidationError` |
| N-2 | `claim_index` 非 int | `"0"` | `ValidationError`（strict int） |
| N-3 | `code_context` 超长 | 10_001 字符（LongText max 10_000，评审 R-3） | `ValidationError` |
| N-4 | `DeepDiveOutput` 含链字段 | 输出加 `chain_proposal` | `extra_forbidden`（结构上禁止改链） |
| N-5 | `existing_evidence_refs` 缺失 line | `{"path":"a.java"}` | 通过（line 可空；可回查性由 T2.6 校验） |

## 5. 回退方案

- 任一验收点失败：修复后复验；若暴露设计缺陷返回讨论（第 2 轮）。
- registry 条目可单独回退（不影响 schema/模型）。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-21）：全部验收点通过。评审 R-1（关键）placeholder 命名分歧已在实施前修订（`explorer_deep_dive_input_json`），A-9b 渲染路径一致性验证覆盖；registry 哈希由 `sync-ai-protocol.py --write` 自动填充后 `--check` 通过。全量 820 passed + 3 个 pre-existing guard_verifier 失败（同前）。

| 编号 | 结果（通过/失败/部分通过） | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | DeepDiveInput 有效解析，chain_proposal 复用 T0.1 模型 | - |
| A-2 | 通过 | DeepDiveOutput 有效解析，claim_index/conclusion 正确回读 | - |
| A-3 | 通过 | 缺 chain_proposal/candidate_id/summary/reasoning → `missing` | - |
| A-4 | 通过 | `conclusion="verified"` → `ValidationError` | - |
| A-5 | 通过 | claim_index=-1 / missing_facts 33 / resolved_facts 33 → 拒绝 | - |
| A-6 | 通过 | 两个 prompt 文件存在 | - |
| A-7 | 通过 | `--check` 退出码 0；registry 条目 input/output model、schema 文件、哈希一致 | - |
| A-8 | 通过 | 两个新 schema（8KB/4.5KB）与模型一致 | - |
| A-9 | 通过 | `test_prompt_registry.py` 全过（85 项模型+registry 测试） | - |
| A-9b | 通过 | `_prompt_variable("explorer-deep-dive") == "explorer_deep_dive_input_json"`，user.md 与 allowed_placeholders 一致 | - |
| A-10 | 通过 | 全量 pytest：820 passed + 3 pre-existing | - |
| A-11 | 通过 | `check-all.sh`：820 passed + 3 pre-existing | - |
| N-1 | 通过 | evidence `../../x` → `ValidationError` | - |
| N-2 | 通过 | `claim_index="0"` → strict int 拒绝 | - |
| N-3 | 通过 | `code_context` 10_001 字符 → `ValidationError`（LongText max 10_000） | - |
| N-4 | 通过 | 输出含 `chain_proposal` → `extra_forbidden`（结构禁改链） | - |
| N-5 | 通过 | `{"path":"a.java"}` 无 line → 通过（T2.6 回查语义） | - |
