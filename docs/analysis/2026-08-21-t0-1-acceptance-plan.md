# 任务验收方案：T0.1（ExplorerObservation Schema）

> **任务编号**：T0.1
> **日期**：2026-08-21
> **依据实施方案**：`docs/analysis/2026-08-21-t0-1-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测 + 生成脚本 check + 回归对比

---

## 1. 验收范围

- 覆盖 T0.1 全部交付物：`ExplorerObservation` 及子模型（`ai_models.py`）、`schemas/ai_explorer_observation.schema.json`、校验测试。
- 验收通过即视为 T0.1 完成，可进入提交。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 模型可解析有效输出 | 构造覆盖全部字段的有效 `ExplorerObservation` 负载，`ExplorerObservation.model_validate(...)` | 通过；`loop.done` 等值正确回读 |
| A-2 | 必填字段缺失拒绝 | 缺失 `component_summary` / `loop` / `ChainProposal.source` | 抛 `ValidationError`，错误类型 `missing`（评审 R-5） |
| A-2b | 空 hops 链拒绝 | `chain_proposals` 中 `hops=[]` | 抛 `ValidationError`，错误类型 `too_short`（`min_length=1`；评审 R-5） |
| A-3 | 多余字段拒绝 | 负载含 `invented_field` | `ValidationError` 含 `extra_forbidden` |
| A-4 | 枚举越界拒绝 | `hypothesis="confirmed"`、`confidence="certain"`、`resolved_via="nonsense"`、`ReadRequest.operation="get_bogus"`、`kind="widget"` | 均抛 `ValidationError` |
| A-5 | hops 逐跳约束 | `hops=[]` 或 `hops` 内 `call_site_line=0` / `arg_positions` 含负数 | 抛 `ValidationError` |
| A-6 | 空数组允许 | `read_requests=[]` 且 `chain_proposals=[]` 的合法首轮负载 | 校验通过（首轮无链合法） |
| A-7 | schema 文件生成且一致 | `scripts/sync-ai-protocol.py --check` | 退出码 0，无 drift；`schemas/ai_explorer_observation.schema.json` 存在且与模型序列化一致 |
| A-8 | 全量测试回归 | `cd backend && python -m pytest` | 全部通过（含既有用例，证明未破坏其他协议） |
| A-9 | lint 与统一校验 | `scripts/check-all.sh` | 通过 |

## 3. 回归标准

- [ ] 既有 AI 协议（preflight/l1-triage/l2-review/repair/finalization）schema 与测试全部通过，未被本任务改动。
- [ ] `prompts/registry.yaml` 摘要刷新后与 schema 文件一致（`--check` 通过）；未新增协议条目。
- [ ] `scripts/sync-ai-protocol.py --write` 后再 `--check` 幂等（不产生新 drift）。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 超大 hops / requests 数组 | `hops` 33 项、`read_requests` 9 项、`chain_proposals` 9 项 | 抛 `ValidationError`（max_length 生效） |
| N-2 | `arg_positions` 超长 | 33 个位置参数 | 抛 `ValidationError` |
| N-3 | `path` 路径穿越 | `path="../../etc/passwd"`、`path="C:\\x"` | 抛 `ValidationError`（`RelativePath` 的 `_require_relative_path`） |
| N-4 | `line` 越界 | `line=0` / `line=-1` | 抛 `ValidationError` |
| N-5 | 空 hops 的 chain | `chain_proposals` 中 `hops=[]` | 抛 `ValidationError`（`too_short`） |
| N-6 | 超长 method_id | `Hop.from_method_id` 为 513 字符 | 抛 `ValidationError`（`MethodId` max_length=512；评审 R-2） |
| N-7 | call_tree_refs 路径穿越 | `call_tree_refs=["../x"]` / `["C:\\x"]` | 抛 `ValidationError`（`RelativePath`） |
| N-8 | ReadRequest.reason 缺失 | `ReadRequest` 无 `reason` | 抛 `ValidationError`（`missing`） |
| N-9 | needs_expansion 类型错误 | `needs_expansion="yes"` | 抛 `ValidationError`（bool 严格校验） |
| N-10 | done 与空链矛盾 | `loop.done=true` 且 `chain_proposals=[]` | 抛 `ValidationError`（`model_validator`；评审 R-3） |
| N-11 | exported 类型错误 | `component_summary.exported="yes"` | 抛 `ValidationError` |

## 5. 回退方案

- 任一验收点失败：修复源码后复验；若暴露模型设计缺陷，返回讨论阶段（第 2 轮）处理。
- 极端情况下 `git checkout` 还原 `ai_models.py` / `schemas/` / `registry.yaml`，任务挂起待重设计。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-21）：全部验收点通过。注：全量 pytest 存在 3 个 `test_guard_verifier.py` 失败，经 `git stash` 隔离验证为**预先存在失败**（与本次改动无关），不在本任务范围，如实披露。另：A-5 验收过程中发现 `arg_positions` 缺 `ge=0` 约束，按 skill 流程走第 2 轮修复（模型补 `Annotated[int, Field(ge=0)]`，补充负例测试），复验通过。

| 编号 | 结果（通过/失败/部分通过） | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | `ExplorerObservation.model_validate` 有效负载通过，`loop.done`/`resolved_via` 正确 | - |
| A-2 | 通过 | 缺失 `component_summary`/`loop` → `missing` | - |
| A-2b | 通过 | 空 `hops` → `too_short`（评审 R-5） | - |
| A-3 | 通过 | `invented_field` → `extra_forbidden` | - |
| A-4 | 通过 | `hypothesis`/`confidence`/`resolved_via`/`operation`/`kind` 非法枚举全部拒绝 | - |
| A-5 | 通过 | 空 hops/call_site_line=0/超长 method_id/负数 arg_positions 全部拒绝（arg_positions 补约束后） | 通过 |
| A-6 | 通过 | 空数组首轮负载合法 | - |
| A-7 | 通过 | `sync-ai-protocol.py --write`/`--check` 均退出码 0，schema 文件与模型一致 | - |
| A-8 | 通过 | 全量 pytest：除 3 个 pre-existing guard_verifier 失败外全部通过（stash 验证无关） | - |
| A-9 | 通过 | `check-all.sh`：795+1 passed，3 pre-existing failed（同 A-8） | - |
| N-1 | 通过 | 33 hops → `ValidationError` | - |
| N-2 | 通过 | 33 arg_positions → `ValidationError` | - |
| N-3 | 通过 | `../../etc/passwd` → `ValidationError`（`_require_relative_path`） | - |
| N-4 | 通过 | `line=0` → `ValidationError` | - |
| N-5 | 通过 | 空 hops → `too_short` | - |
| N-6 | 通过 | 513 字符 method_id → `ValidationError` | - |
| N-7 | 通过 | 路径穿越（含 `\`）→ `ValidationError` | - |
| N-8 | 通过 | `ReadRequest` 缺 `reason` → `missing` | - |
| N-9 | 通过 | `needs_expansion="yes"` → `ValidationError` | - |
| N-10 | 通过 | `done=true` 且无链 → `model_validator` 拒绝 | - |
| N-11 | 通过 | `exported="yes"` → `ValidationError` | - |
