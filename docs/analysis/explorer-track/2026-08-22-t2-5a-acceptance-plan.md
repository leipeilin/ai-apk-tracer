# 任务验收方案：T2.5a（探索 Agent 协议层）

> **任务编号**：T2.5a
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t2-5a-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest + sync 脚本产物一致性 + 全量回归

---

## 1. 验收范围

- ExplorerInput/ExplorerObservation 模型 + prompt 骨架 + registry 注册 + 测试。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式 | 预期结果 |
|---|---|---|---|
| A-1 | Observation round-trip | `test_observation_round_trip` | 校验+往返一致 |
| A-2 | 读码操作参数必填 | `test_read_request_operation_params` | 五操作参数约束生效 |
| A-3 | loop 必答 | `test_loop_state_required` | 缺 loop 拒绝 |
| A-4 | 预算字段边界 | `test_input_budget_fields` | ge 约束生效 |
| A-5 | ChainProposal 复用 | `test_chain_proposal_reuse` | T0.1 模型一致（枚举边界） |
| A-6 | registry 注册 | `test_registry_entry_registered` + 既有 test_prompt_registry | 条目四字段 + 哈希门禁通过 |
| A-7 | schema 文件同步 | sync 脚本幂等（二次运行零 diff） | 一致 |
| A-8 | 单测通过 | `pytest tests/test_explorer_protocol.py -q` | 全部通过 |
| A-9 | 全量回归 | `pytest -q` | 1002+ 全部通过 |
| A-10 | 统一校验 | check-all + ruff | 通过 |

## 3. 边界与负例

| 编号 | 场景 | 预期 |
|---|---|---|
| N-1 | 非法 operation 枚举 | 拒绝 |
| N-2 | chain_proposals 超 16 条 | 拒绝（max_length） |
| N-3 | round_index=0 | 拒绝 |
| N-4 | hypothesis 非法值 | 拒绝（T0.1 枚举） |

## 4. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审 8 项意见第 1 轮全部采纳（**R-1 致命盲区**：方案原"新增"口径与 T0.1 已交付的 ExplorerObservation 四模型冲突——收窄为"既有零改动 + 仅新增 ExplorerInput"；R-2 回退四操作维持既有 ReadRequest 枚举；R-3 维持 `_done_requires_chain` 校验器并锚定测试）。实施中同步更新 test_config 的"先声明后注册"断言（explorer 已注册后的状态反转——T0.7 时"未注册属预期"的断言随注册完成更新为"已注册匹配"）。sync 脚本幂等（--check 通过）。全量 1010 passed / 0 failed（+8）。

| 编号 | 结果 | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | ExplorerInput round-trip + 上下文字段（entry_json/attack_surface_json/prior/code 可空） | - |
| A-2 | 通过 | 四操作枚举（非法拒绝，含被排除的 resolve_invoke_target/class_hierarchy/get_entry_points）+ reason 必填 | - |
| A-3 | 通过 | done=true + 空链拒绝；done=false + 空提案合法（校验器回归锚定） | - |
| A-4 | 通过 | round_index/rounds_budget/requests_budget 边界拒绝 | - |
| A-5 | 通过 | ComponentSummary 结构化（kind 枚举/exported bool 类型强制） | - |
| A-6 | 通过 | registry 条目四字段 + 哈希真实 + 既有 test_prompt_registry 全过（哈希门禁自动覆盖） | - |
| A-7 | 通过 | sync --check 零漂移（幂等） | - |
| A-8 | 通过 | 8 项全过 | - |
| A-9 | 通过 | 全量 **1010 passed / 0 failed**（1002+8） | - |
| A-10 | 通过 | check-all + ruff 全过 | - |
| N-1 | 通过 | 非法 operation 拒绝（A-2 含四非法值） | - |
| N-2 | 通过 | （既有 max_length=8 锚定——Observation 往返含 8 上限由既有 schema 测试覆盖） | - |
| N-3 | 通过 | round_index=0 等拒绝（A-4） | - |
| N-4 | 通过 | hypothesis 非法值拒绝（既有模型枚举 + schema 测试覆盖） | - |
