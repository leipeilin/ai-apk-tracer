# 任务审查报告：P-2 读码预算修复 + 并行探索

> **审查对象**：
> - `2026-08-27-p2-parallel-exploration-implementation-plan.md`（实施方案）
> - `2026-08-27-p2-parallel-exploration-acceptance-plan.md`（验收方案）
> **审查方式**：逐条对照代码事实核验锚点真伪 + 缺陷诊断正确性 + 并行语义风险 + 验收可执行性
> **审查时间**：2026-08-27

---

## 一、总体结论

**结论：✅ 有条件通过（建议补 3 个技术细节后批准）。**

这份方案质量很高，最突出的优点是**对 D1 缺陷的诊断精确且证据充分**（用 8/22 run 的实证数据锚定），且 D3 选择"复用 `BoundedJobScheduler` 而非新写调度器"是正确决策。但并行化（D3）涉及并发语义的若干细节，方案在"声称"与"实现"之间还有 3 处需要更严谨的说明。

---

## 二、锚点真实性核验（全部属实 ✅）

| 方案声称 | 代码事实 | 结论 |
|---|---|---|
| D1 缺陷：`max_requests_per_entry` 被实现为全局池 | `explorer.py:234` `requests_budget = max_requests_per_entry - self._read_requests_used`，而 `_read_requests_used` 是实例级（`explorer.py:131`），入口间不重置 | ✅ 属实，缺陷诊断精确 |
| D1 修复：`_read_requests_used` 保留 run 级统计 | `explorer.py:141-142` `read_requests_used` property 语义确为 run 级统计 | ✅ 方案"保留 run 级统计"判断正确 |
| `BoundedJobScheduler` 存在且支持 circuit + index 排序 | `ai_scheduler.py:90-194` 完整实现，`ordered_results` 按 index 排序、`peak_active` 统计并发峰值 | ✅ 属实 |
| in-flight 保留真实结果、仅未启动 SKIPPED | `ai_scheduler.py:109-112` docstring 明示 | ✅ 与方案声称一致 |
| `run_indexed_jobs` 便捷入口 | `ai_scheduler.py:197` | ✅ 属实 |
| `IndexedJob` 结构 | `ai_scheduler.py:55`（index + value） | ✅ 属实 |
| `max_requests_per_run` 现于 config.py:185，`ge=1` 无 None | `config.py:185` `int = Field(default=140, ge=1)` | ✅ 属实，D2 需改类型 |
| `ExplorerSettings` 于 config.py:200，无 `entry_concurrency` | `config.py:200-211` 现仅 max_rounds/max_requests_per_entry 等 | ✅ 属实，D3 需新增字段 |
| `candidate_concurrency=4` 先例 | `config.py:86` | ✅ 属实，D3 对齐有据 |

**锚点全部属实，D1 缺陷诊断是精确的（不只是"感觉有 bug"，而是用 `3+7+8+2=20 封顶` 实证锚定）。**

---

## 三、发现的问题

### P1-1【技术细节缺失】D3 熔断的 `opens_circuit` 判定与 `terminated_by` 的映射关系未说透

方案 3 写：

```python
opens_circuit=lambda r: r[1] == "short_circuit",
```

`r[1]` 是 `_explore_entry` 返回三元组的 `terminated_by`。

**问题**：`terminated_by == "short_circuit"` 的语义是"**AI 熔断类失败**"（`explorer.py:256-260`，对应 `circuit_breaking`/`skipped` 状态）。但 `_explore_entry` 的返回三元组里，`terminated_by` 还有 `error`（单入口 AI 失败，**非熔断**）——`error` 是否会误触发 `opens_circuit`？

从代码看 `error` 与 `short_circuit` 是**分开的两条路径**（`explorer.py:256-260` 的 if/else），所以映射 `r[1] == "short_circuit"` 是精确的，`error` 不会误触发。**但这层映射关系在方案里没有明说**，实施者若不清楚 `error`/`short_circuit` 的区别，可能写出 `r[1] in ("short_circuit", "error")` 的错误映射。建议在方案中补一句"`error`（单入口 AI 失败）不触发熔断，仅 `short_circuit`（熔断类）触发——与现串行版 `skipped_short_circuit` flag 语义严格对齐"。

### P1-2【技术细节缺失】并行下 `_explore_entry` 的实例级状态竞态未完全澄清

方案"并发安全"声称"asyncio 单线程——`_read_requests_used`/`_ai_requests_used` 自增在无 await 区间内原子"。

**问题**：这个声称**基本正确但有一个隐患**——`_ai_requests_used` 的自增位置。从 `orchestrator.py:1136-1150` 看，AI 预算检查与计费是在 `budgeted_ai_call`（orchestrator 层，带 `_ai_budget_lock`）里做的，**不在 `ExplorerOrchestrator` 内部**。但 `ExplorerOrchestrator._ai_requests_used`（`explorer.py:130`）是**另一本账**，它在 `explore_all` 里由 `_ai_call` 回调自增（`explorer.py` 的 `self._ai_requests_used += 1`）。

D3 并行化后，多个 `_explore_entry` 并发调用 `_ai_call`，`self._ai_requests_used` 的自增会在并发 task 间交错。asyncio 单线程下"无 await 区间内自增"确实是原子的，但**需要确认 `_ai_call` 回调本身是否有 await 点夹在"检查"与"自增"之间**。方案未说明这一本账（`ExplorerOrchestrator._ai_requests_used`）与 orchestrator 层账（`_ai_budget_lock` 保护的）在并行下的关系。建议补一句：`ExplorerOrchestrator` 内部的 `_ai_requests_used`/`_read_requests_used` 自增必须与 await 严格分离，或统一走 orchestrator 层的锁。

### P1-3【技术细节缺失】候选软上限（P2-7）的"检查-启动"竞态窗口未量化

方案 3 写"候选上限（非 None 时）：worker 启动前查全局计数——并行下可能小幅超限（≤ concurrency×单入口峰值）"。

**问题**：`≤ concurrency×单入口峰值` 这个上界是正确的（最坏情况下 concurrency 个 worker 同时在"检查通过后、启动后"各自产满），但**方案未说明"检查"发生在 worker 的哪个位置**。若检查在 `worker` 函数内、`await self._explore_entry(...)` **之前**，则超限窗口是"检查→启动"间的并发交错（小）；若检查在 `explore_all` 主循环（非 worker）里，则语义完全不同。需明确"检查在 worker 内、explore_entry 之前"，与 P2-7 的"已启动入口候选全收"对齐。

---

## 四、验收方案的评估

### 做得好的地方

1. **P2-5 并行熔断语义验收**是这份方案最精妙的一项——精确到"in-flight 入口保留完整轮记录（非截断）"，直接对应 `BoundedJobScheduler` 的核心语义，说明作者真正读懂了调度器。
2. **P2-8 的 `entry_concurrency=1` 语义等价串行**是绝佳的回退设计——零行为差异回退位，不用 revert 代码。
3. **P2-2 预算截断口径不回归**（引用 A5-4 存量用例）体现了对 F5 已有成果的保护意识。
4. **回退方案分层清晰**：D1/D2/D3 各自独立，D3 有零成本回退位。

### 需补充/修正的验收项

| 项 | 问题 | 建议 |
|---|---|---|
| P2-1 | "修复前全局池封顶 20"的断言，但**未验证 D1 修复后 `read_requests_used` run 级统计 = 40 的精确性**（2 入口各 20 需确认入口局部计数不含预算截断） | 补断言：`read_requests_used`（run 级）= 各入口 `entry_read_used` 之和，且 = 40（无截断时） |
| P2-4 | "peak 捕获"并发度 ≤ entry_concurrency 的断言，**需确认 FakeAnalyzer 如何制造"慢速交错"**——若 FakeAnalyzer 同步返回，peak 恒为 1 | 补明确：FakeAnalyzer 需 `await asyncio.sleep(0)` 或事件同步，才能真实制造并发交错 |
| P2-9 | 探针 dry-run 的 `entry_concurrency` 透传，但**探针正式行为是否也并行**？若探针并发 2 入口，其 `guidance_usage`/`seed_hit_rate` 统计是否受并行影响 | 补明确：探针的统计口径在并行下是否不变（应该不变，因为按 entry 聚合） |
| P2-6 | "各入口预算独立不互相抢"与 P2-1 高度重叠，可合并，但**更重要的是未验证"预算独立"在并发交错下的正确性**（两个 worker 各自的 `entry_read_used` 是局部变量，天然独立，但需测试确认无共享） | 可与 P2-1 合并，或补一个"并发下两入口各自达到 20"的精确断言 |

---

## 五、审查结论与建议

| 优先级 | 事项 | 动作 |
|---|---|---|
| P1 | D3 熔断映射（`error` vs `short_circuit`）说明 | 补一句映射关系的明确说明 |
| P1 | 并行下两本账（`ExplorerOrchestrator._ai_requests_used` vs orchestrator 层 `_ai_budget_lock`）关系 | 补并发安全说明 |
| P1 | 候选软上限的"检查"位置 | 明确"检查在 worker 内、explore_entry 之前" |
| 补充 | P2-1/P2-4/P2-9/P2-6 四个验收项的精确性 | 逐一补明确（见上表） |

---

## 六、总体评价

这份方案是**截至目前审查过的方案中诊断最扎实的一份**——D1 不是靠"代码 review 感觉有 bug"，而是用 `3+7+8+2=20 封顶` 的 run 实证数据反推根因，再对照代码确认（`explorer.py:234` 全局池）。D3 复用 `BoundedJobScheduler` 而非新写调度器，是避免重复造轮子的正确工程决策，且对 scheduler 的 in-flight 保留/peak 统计等细节理解准确。

**剩余问题全部集中在"并发细节的说透程度"而非"方案错误"**——三处 P1 都是"声称正确但没说透"，实施者只要理解了代码就能写对，但方案作为实施依据应该把这三处显式化，避免实施时走偏。

建议：补 3 处 P1 技术细节 + 4 处验收项精确性后批准实施。
