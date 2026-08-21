# 任务验收方案：T0.2（ExplorerCandidate Schema）

> **任务编号**：T0.2
> **日期**：2026-08-21
> **依据实施方案**：`docs/analysis/2026-08-21-t0-2-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测 + 生成脚本 check + 回归对比

---

## 1. 验收范围

- 覆盖 T0.2 全部交付物：3 个新模型（`ai_models.py`）、`schemas/explorer_candidate.schema.json`、校验测试。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 有效候选解析 | 构造覆盖全部字段的有效负载，`ExplorerCandidate.model_validate(...)` | 通过；`chain_proposal` 复用 T0.1 模型可嵌套解析 |
| A-2 | validation 空占位 | 负载不提供 `validation` | 校验通过，`validation is None`（pending 语义） |
| A-3 | candidate_id 约束 | `candidate_id="expl_"+"a"*20` 通过；`"expl_"+("a"*19)`、`"cand_xxx"`、非 hex | 前者通过，其余抛 `ValidationError` |
| A-4 | 必填缺失拒绝 | 缺失 `chain_proposal` / `component` / `source` | 抛 `ValidationError`（`missing`） |
| A-5 | 多余字段拒绝 | 负载含 `invented_field` | `extra_forbidden` |
| A-6 | 枚举越界拒绝 | `source="rule"`、`schema_version="2.0.0"`、`component.kind="widget"` | 抛 `ValidationError` |
| A-7 | validation 结构约束 | `validation.status="confirmed"`、`verified_hop_count=-1` | 抛 `ValidationError` |
| A-8 | 嵌套 ChainProposal 约束透传 | `chain_proposal.hops=[]`（T0.1 的 min_length=1） | 抛 `ValidationError`（`too_short`） |
| A-9 | schema 生成且一致 | `sync-ai-protocol.py --check` | 退出码 0；`explorer_candidate.schema.json` 与模型一致 |
| A-10 | 全量测试回归 | `cd backend && .venv/bin/python -m pytest` | 除 3 个 pre-existing guard_verifier 失败外全部通过（与 T0.1 相同的 `test_guard_verifier.py` 3 个失败，T0.1 已 stash 验证 pre-existing，评审 R-1） |
| A-11 | 统一校验 | `scripts/check-all.sh` | 同上，无新增失败 |

## 3. 回归标准

- [ ] 既有 AI 协议 schema 与测试全部通过（含 T0.1 的 ExplorerObservation 用例）。
- [ ] `sync-ai-protocol.py --write` 后 `--check` 幂等。
- [ ] `test_committed_schemas_exactly_match_stable_model_generation` 自动覆盖新注册模型（含 `explorer_candidate.schema.json`）。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 超长 `component.name` | 513 字符 | 抛 `ValidationError`（ShortText max_length=256） |
| N-2 | `api_entry_ref` 非法字符 | `"act 非法空格"` | 抛 `ValidationError`（Identifier pattern） |
| N-3 | `prompt_version` 超长 | 257 字符 | 抛 `ValidationError` |
| N-4 | `verified_hop_count` 越界 | `-1` / 非 int 字符串 | 抛 `ValidationError` |
| N-5 | `validation.status` 缺失 | `validation={"notes":"x"}` | 抛 `ValidationError`（`missing`） |
| N-6 | 嵌套 chain 超长 hops | `chain_proposal.hops` 33 项 | 抛 `ValidationError`（max_length=32） |
| N-7 | component 子对象必填缺失 | 缺 `component.entry_method` / `component.name` | 抛 `ValidationError`（`missing`） |
| N-8 | api_entry_ref 超长 | `api_entry_ref` 161 字符 | 抛 `ValidationError`（Identifier max_length=160） |
| N-9 | 嵌套 hops 越界透传 | `chain_proposal.hops[].call_site_line=0` | 抛 `ValidationError`（T0.1 的 ge=1 透传） |
| N-10 | validation bool 类型错误 | `blocked_by_guard="yes"` / `custom_sink_proposal=1` | 抛 `ValidationError` |

## 5. 回退方案

- 任一验收点失败：修复源码后复验；若暴露设计缺陷返回讨论（第 2 轮）。
- 极端情况 `git checkout` 还原相关文件。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-21）：全部验收点通过。实施中一处测试编写缺陷（ExplorerCandidate 未导入测试文件导致 NameError）已修复复验。全量 pytest 仍有 3 个 `test_guard_verifier.py` 失败，与 T0.1 完全相同的 pre-existing 集合（stash 已隔离验证），不阻塞本任务。

| 编号 | 结果（通过/失败/部分通过） | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | 有效候选解析，`chain_proposal` 嵌套 T0.1 模型正常 | - |
| A-2 | 通过 | 不提供 `validation` → `validation is None`（pending 占位） | - |
| A-3 | 通过 | `expl_`+20hex 通过；19 位/非 hex/大写/前缀错均拒绝 | - |
| A-4 | 通过 | 缺 `chain_proposal`/`component`/`source`/`candidate_id` → `missing` | - |
| A-5 | 通过 | `invented_field` → `extra_forbidden` | - |
| A-6 | 通过 | `source="rule"`/`schema_version="2.0.0"`/`kind="widget"` 拒绝 | - |
| A-7 | 通过 | `status="confirmed"`/`verified_hop_count=-1`/`failed_hop_indices=[-1]` 拒绝 | - |
| A-8 | 通过 | 空 `hops` → `too_short`（T0.1 约束透传） | - |
| A-9 | 通过 | `sync-ai-protocol.py --check` 退出码 0；`explorer_candidate.schema.json` 与模型一致（12.6KB） | - |
| A-10 | 通过 | 全量 pytest：49 项模型测试全过；812 passed + 3 pre-existing guard_verifier 失败（同 T0.1） | - |
| A-11 | 通过 | `check-all.sh`：812 passed + 3 pre-existing | - |
| N-1 | 通过 | `component.name` 257 字符 → `ValidationError` | - |
| N-2 | 通过 | `api_entry_ref` 含空格 → `ValidationError` | - |
| N-3 | 通过 | `prompt_version` 257 字符 → `ValidationError` | - |
| N-4 | 通过 | `verified_hop_count=-1` → `ValidationError` | - |
| N-5 | 通过 | `validation={"notes":"x"}` 缺 status → `missing` | - |
| N-6 | 通过 | 嵌套 hops 33 项 → `ValidationError` | - |
| N-7 | 通过 | 缺 `component.entry_method` → `ValidationError` | - |
| N-8 | 通过 | `api_entry_ref` 161 字符 → `ValidationError` | - |
| N-9 | 通过 | 嵌套 `call_site_line=0` 透传 → `ValidationError` | - |
| N-10 | 通过 | `blocked_by_guard="yes"` → `ValidationError` | - |
