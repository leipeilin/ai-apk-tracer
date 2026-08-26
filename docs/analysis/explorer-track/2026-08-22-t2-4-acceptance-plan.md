# 任务验收方案：T2.4（call_tree on-demand 检索服务）

> **任务编号**：T2.4
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t2-4-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测（真实 index 调用链）+ 全量回归

---

## 1. 验收范围

- CallTreeService 七能力 + 有界树 + 落盘 + 测试。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式 | 预期结果 |
|---|---|---|---|
| A-1 | callees/callers 双向 | `test_get_callees_callers` | resolved 边双向 + 摘要字段齐 |
| A-2 | 方法体查询 | `test_get_method_body` | body 文本 + 行号 + 非 lifecycle 可查 |
| A-3 | 方法体截断 | `test_get_method_body_truncation` | 400 行上限 + truncated=true |
| A-4 | 符号解析歧义如实 | `test_resolve_invoke_target` | 同名多候选全返回 |
| A-5 | 类层次 | `test_class_hierarchy` | extends/subclasses 双向 |
| A-6 | 符号搜索 | `test_search_symbol` | 方法+类合并结果 |
| A-7 | 有界树全链 | `test_build_bounded_tree` | 节点/边正确 + truncated=false |
| A-8 | 树预算截断 | `test_bounded_tree_limits` | node_limit/depth_limit 双场景 + 环安全 |
| A-9 | 落盘 | `test_save_tree` | 文件回读一致 |
| A-10 | 入口表直通 | `test_get_entry_points_with_table` | binder method_id 直通 + manifest lifecycle 解析 |
| A-11 | 入口表降级 | `test_get_entry_points_degraded` | 空列表 + degraded 标注（其余能力不受影响） |
| A-12 | 单测通过 | `pytest tests/test_call_tree.py -q` | 全部通过 |
| A-13 | 全量回归 | `pytest -q` | 988+ 全部通过（T2.3 基线 988） |
| A-14 | 统一校验 | `scripts/check-all.sh` + `ruff check`（新文件） | 通过 |

## 3. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 不存在的 method_id | `get_method_body("missing#x:1")` | None（不抛错） |
| N-2 | 不存在的类 | `class_hierarchy("com.x.Missing")` | 空结构（extends/subclasses 空） |
| N-3 | 损坏入口表 | 非法 JSON | 容错降级（同 A-11 路径） |
| N-4 | 环调用 | A→B→A 构造 | build_bounded_tree 不死循环（visited） |

## 4. 回归标准

- [ ] 零 pipeline 改动（无 orchestrator/规则侧变更——服务独立）
- [ ] 全量 988 passed / 0 failed

## 5. 回退方案

- 任一验收点失败：修复后复验；设计缺陷上升评审第 2 轮。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审 7 项意见第 1 轮全部采纳（含高 R-1 树透传歧义 gaps——get_call_relations 已带回的数据不再被静默丢弃；R-2 body 预算 400→240 对齐 max_lines_per_context 同语义；R-5 lifecycle 解析提升公共函数）。实施中修正：descriptor 限定测试表达式改声明处简单名形态（`(String)->void`，T2.2 已知事实）；TRY004（结构校验改分支降级去 raise）；测试残留引用清理。全量 1002 passed / 0 failed（+14）。

| 编号 | 结果 | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | A.entry→B.run callees + 反向 callers + 摘要（path/line/descriptor/qualified_class） | - |
| A-2 | 通过 | body 文本 + 行号 + 非 lifecycle 方法可查 | - |
| A-3 | 通过 | 600 行方法 → 240 行截断 + truncated=true | - |
| A-4 | 通过 | log 同名两类 → 2 候选全返回 + descriptor 限定（简单名形态） | - |
| A-5 | 通过 | Sub extends=Base + Base subclasses 含 Sub（双形态匹配） | - |
| A-6 | 通过 | 搜索方法+类合并结果 | - |
| A-7 | 通过 | A→B→C 全链 3 节点 2 边 + gaps={} + truncated=None | - |
| A-8 | 通过 | node_limit（max_nodes=2 + edges ⊆ nodes 断言）+ depth_limit（5 层链 max_depth=3 → 4 节点） | - |
| A-8b | 通过 | Ring1↔Ring2 环 → 2 节点不死循环 + truncated=None（R-3） | - |
| A-9 | 通过 | 落盘回读一致（原子写） | - |
| A-10 | 通过 | binder implementation_method_id 直通 + manifest lifecycle 解析（SplashActivity.onCreate）+ webview None | - |
| A-11 | 通过 | 缺失 → degraded+hint；损坏 → 空列表；其余能力不受影响 | - |
| A-12 | 通过 | 14 项全过 | - |
| A-13 | 通过 | 全量 **1002 passed / 0 failed**（988+14） | - |
| A-14 | 通过 | check-all + ruff 全过 | - |
| N-1 | 通过 | 不存在 method_id → None | - |
| N-2 | 通过 | 不存在类 → 空结构 | - |
| N-3 | 通过 | 损坏 JSON → 容错降级（A-11） | - |
| N-4 | 通过 | 环安全（A-8b） | - |
