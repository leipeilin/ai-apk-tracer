# 任务验收方案：T0.6（归一化映射表）

> **任务编号**：T0.6
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t0-6-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测 + 文档交付检查 + 全量回归

---

## 1. 验收范围

- 映射表文档 + 可执行测试。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 映射表文档存在且完整 | `docs/analysis/explorer-track/2026-08-22-t0-6-normalization-mapping.md` 存在；含 10 项 required 映射 + 非 required 声明 + severity 关键词表 | 通过 |
| A-2 | 目标覆盖 | `test_normalization_mapping.py::test_mapping_covers_all_required_candidate_fields` | 通过（MAPPING keys ⊇ candidate required 10 项） |
| A-3 | 来源存在 | `test_mapping_sources_exist_in_explorer_candidate` | 通过（嵌套字段路径在模型存在） |
| A-4 | 常量/枚举合法 | `test_mapping_constant_and_enum_values_valid` | 通过（rule_id pattern / L2 / component / confidence_tier / severity_hint 枚举） |
| A-5 | other 处理语义 | `test_component_other_handling_declared` | 通过（显式 drop 声明，无非法枚举值） |
| A-6 | severity 关键词 | `test_severity_keyword_rules` | 通过（各关键词→期望档位 + 默认 medium） |
| A-7 | 测试通过 | `.venv/bin/python -m pytest tests/test_normalization_mapping.py -q` | 全部通过 |
| A-8 | 全量回归 | `.venv/bin/python -m pytest -q` | 除 3 个 pre-existing guard_verifier 失败外全部通过 |
| A-9 | 统一校验 | `scripts/check-all.sh` | 同上，无新增失败 |

## 3. 回归标准

- [ ] 既有测试（AI schema/规则产物）全部通过。
- [ ] 映射表测试不依赖 T2.7 归一化实现（仅验证映射声明本身）。
- [ ] `ruff check` 通过。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | candidate required 集合变化 | 若未来 candidate.schema.json required 增加字段而映射表未覆盖 | `test_mapping_covers_all_required_candidate_fields` 失败（防漂移） |
| N-2 | ExplorerCandidate 字段改名 | 若模型字段调整而映射表未同步 | `test_mapping_sources_exist_in_explorer_candidate` 失败 |
| N-3 | severity 关键词大小写/子串 | 样例文本含 `"任意"`（子串）或 `"REMOTE"`（大写） | 命中（大小写不敏感子串匹配） |
| N-4 | 多关键词冲突 | 文本同时含 high 与 low 关键词 | 高优先级档位胜出（关键词表按优先级顺序判定） |

## 5. 回退方案

- 任一验收点失败：修订映射表/测试后复验；映射表与测试须同步变更（防双源漂移）。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审 8 项意见第 1 轮全部处置（含致命 R-1：`auxiliary` 为 boolean 不可承载对象，改 blocking_gaps 标记）。实施中 1 处测试断言设计错误已修（英文 "REMOTE EXECUTION" 断言不匹配中文关键词表，改子串匹配语义）；ruff 3 处 import 排序自动修复。

| 编号 | 结果（通过/失败/部分通过） | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | `2026-08-22-t0-6-normalization-mapping.md` 存在，含 10 项映射 + 非 required + 关键词表 | - |
| A-2 | 通过 | MAPPING keys ⊇ candidate required 10 项 | - |
| A-3 | 通过 | 嵌套字段路径（chain_proposal/component/validation）存在性验证通过 | - |
| A-4 | 通过 | rule_id pattern / L2 / 枚举映射值均落在候选枚举 | - |
| A-5 | 通过 | other → drop_with_audit 显式声明，无非法枚举值 | - |
| A-6 | 通过 | 关键词命中/默认/子串/冲突优先级/封顶 high/root 删除 全部断言通过 | - |
| A-7 | 通过 | test_normalization_mapping.py 8 项全过 | - |
| A-8 | 通过 | 全量 pytest：881 passed + 3 pre-existing guard_verifier（同前） | - |
| A-9 | 通过 | check-all：881 passed + 3 pre-existing；ruff 全过 | - |
| N-1 | 通过 | candidate required 增加而映射表未覆盖 → 测试失败（防漂移验证成立） | - |
| N-2 | 通过 | 模型字段改名而映射表未同步 → 测试失败 | - |
| N-3 | 通过 | 子串匹配（关键词任意位置命中）验证通过 | - |
| N-4 | 通过 | 冲突样例（数据+敏感）→ high 胜出（行序） | - |
