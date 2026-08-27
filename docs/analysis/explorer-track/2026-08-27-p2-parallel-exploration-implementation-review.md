# 提交审查报告：`8d46b29` — P-2 读码预算修复 + 并行探索

> **审查对象**：`feat(explorer): P-2 读码预算修复 + 并行探索——D1 入口局部预算/D2 全轨上限 None/D3 BoundedJobScheduler 并行（含评审闭合）`
> **审查方式**：逐层代码 diff 核验 + 缺陷诊断正确性 + 并发语义正确性 + D2 影响面完整性 + 全量回归
> **审查时间**：2026-08-27

---

## 一、总体结论

**结论：⚠️ 有条件通过（发现 1 处真实缺陷，须修复后合入）。**

实施质量整体高，D1 缺陷修复精确、D3 并行化对 `BoundedJobScheduler` 语义理解准确、且额外修复了"worker 异常被吞伪装熔断"的问题。但 **D2（`max_requests_per_run` 支持 None）遗漏了一处消费点**——verify 轨的预算包装未加 None 短路，在 `max_requests_per_run: null` + verify 启用时会抛 `TypeError`。此为真实缺陷，需修复。

---

## 二、方案落地对照（逐项核验）

| 方案项 | 实现 | 结论 |
|---|---|---|
| D1 入口局部预算 | `entry_read_used` 局部变量 + `requests_budget = max_requests_per_entry - entry_read_used`（`explorer.py:284`）+ `entry_read_used += len(executed["records"])`（`explorer.py:360`） | ✅ 正确 |
| D1 `_read_requests_used` 保留 run 级统计 | `explorer.py:131` 未删，`read_requests_used` property 不变 | ✅ 正确 |
| D2 `max_requests_per_run: int \| None` | `config.py:185` 改类型 + `config/default.yaml` 设 `null` | ✅ 正确 |
| D2 三处消费点短路 | `orchestrator.py:1140`（explorer）、`1153`（deep_dive）、`1294`（L1L2）已加 None 短路 | ⚠️ 遗漏第 4 处（见问题 C-1） |
| D3 复用 BoundedJobScheduler | `explorer.py:192-199` `run_indexed_jobs` + `entry_concurrency` | ✅ 正确 |
| D3 熔断映射（评审 P1-1） | `opens_circuit=lambda r: r[1] == "short_circuit"`，`error` 不触发 | ✅ 正确 |
| D3 索引保序 | `scheduled.results` 按 index 排序（scheduler 保证），`entries[scheduled_result.index]` 取 entry_id | ✅ 正确 |
| D3 软上限检查在 worker 内（评审 P1-3） | `worker` 内 `_explore_entry` 之前检查 `candidate_total >= max_candidates_per_run`（`explorer.py:183-187`） | ✅ 正确 |
| 实施追加修复（FAILED/SKIPPED 分离） | `explorer.py:213-221` FAILED 记 `error` + `worker_error` | ✅ 正确 |

**评审 P1-1/P1-2/P1-3 全部闭合，且额外修复了 worker 异常吞没问题。**

---

## 三、发现的问题

### C-1【真实缺陷·P0】D2 遗漏 verify 轨消费点——`max_requests_per_run: null` 时抛 TypeError

**位置**：`orchestrator.py:730-748` `_budgeted_protocol_call`

```python
async def budgeted(model_input: Any) -> dict[str, Any]:
    async with self._ai_budget_lock:
        if self._ai_requests_used >= self.settings.context_budget.max_requests_per_run:  # ← 741 行，未加 None 短路
            return {"status": "skipped", "circuit_breaking": True,
                    "metadata": {"reason": "run_request_budget_exhausted"}}
```

**问题**：
- 这是 `max_requests_per_run` 的**第 4 处消费点**（verify 轨核验协议调用的预算包装），被 `_verify_candidate`（`orchestrator.py:787`）使用；
- 提交声称"三处消费点统一短路"（explorer/deep_dive/L1L2），**遗漏了 verify 轨这一处**；
- 当 `config/default.yaml` 设 `max_requests_per_run: null`（本提交已设），且 verify 轨启用（`VerifySettings.enabled=True`）时，`None >= 140` 抛 `TypeError: '>=' not supported between instances of 'NoneType' and 'int'`。

**触发条件**：verify 轨默认 `enabled=False`（`config.py:230`），所以**默认配置下不触发**——这是它未被测试发现的原因。但一旦开启 verify（L2 agent 化演进方向），配合 `null` 上限立即崩溃。

**修复**：第 741 行补 None 短路，与其他三处对齐：
```python
if (
    self.settings.context_budget.max_requests_per_run is not None
    and self._ai_requests_used >= self.settings.context_budget.max_requests_per_run
):
```

**严重度评估**：P0（真实缺陷）但**影响面受限**（verify 默认关闭）。若本验证阶段不启用 verify，可降为"修复后合入"的必改项；但作为全轨上限统一的 D2 改动，**消费点必须全部覆盖**，不能留缺口。

---

## 四、测试覆盖核验

新增 5 个测试用例（提交声称 6，另 1 个疑在其他测试文件）：

| 验收项 | 测试 | 结论 |
|---|---|---|
| P2-1 入口局部预算 | `test_read_budget_per_entry_local` | ✅ |
| P2-4 并行 + 保序 | `test_parallel_concurrency_and_ordering` | ✅ |
| P2-5 并行熔断语义 | `test_parallel_circuit_semantics` | ✅ |
| P2-6 预算并行独立 | `test_read_budget_isolated_under_parallel` | ✅ |
| P2-7 软上限 | `test_soft_cap_parallel` | ✅ |

**全量回归：1341 passed / 0 failed**（实测，40.49s），较提交声称 1336 多 5 个（测试数量有后续变动），**无失败**。

**测试缺口**：5 个测试**均未覆盖 `max_requests_per_run=None` + verify 启用的组合**——这正是 C-1 缺陷漏网的原因。建议补一个用例：`max_requests_per_run=None` + `verify.enabled=True` 时，verify 调用不抛异常。

---

## 五、验收声明核对

提交声称 P2-1~P2-9 全过 + 评审 P1-1/P1-2/P1-3 闭合。核验：

| 项 | 结论 |
|---|---|
| P2-1~P2-7 | 代码 + 测试双覆盖，✅ |
| P2-8 零回归 | 1341 passed 实测 ✅ |
| P2-9 探针透传 | `probe_explorer_entry.py:214` `entry_concurrency` 透传进 plan ✅ |
| 评审 P1-1/P1-2/P1-3 | 代码实现逐项闭合 ✅ |
| **D2 影响面完整性** | ⚠️ 遗漏 verify 轨（C-1） |

---

## 六、总结

| 维度 | 评价 |
|---|---|
| D1 缺陷修复 | ✅ 精确（`entry_read_used` 局部化，run 级统计保留） |
| D3 并行化 | ✅ 优秀（scheduler 语义理解准确，熔断/保序/软上限/FAILED 分离全部正确） |
| D2 None 上限 | ⚠️ 三处消费点正确，**遗漏 verify 轨第 4 处** |
| 测试覆盖 | ⚠️ 并行/D1 覆盖好，但 None+verify 组合缺失 |
| 回归 | ✅ 1341 passed 无失败 |

**结论：⚠️ 有条件通过。** 需修复 C-1（`orchestrator.py:741` 补 None 短路）并补一个 verify+None 组合测试后合入。其余部分质量优秀，D3 并行化尤其扎实。

---

## 附：审查数据源

- `git show 8d46b29` 完整 diff
- `explorer.py` / `orchestrator.py` / `config.py` / `ai_scheduler.py` 实读核对
- 全量 pytest 实测（1341 passed）
- `max_requests_per_run` 全仓搜索（14 处匹配，定位到第 4 消费点）
