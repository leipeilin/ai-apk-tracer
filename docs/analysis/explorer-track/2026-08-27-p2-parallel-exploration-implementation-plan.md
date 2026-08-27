# 任务实施方案：P-2 读码预算修复 + 并行探索（三件套）

> **任务编号**：P-2（探索轨质量缺陷修复 + 验证阶段并行化，用户指令 2026-08-27）
> **背景**：并行化评估中发现两个存量/连带问题必须先行：
> **D1 读码预算全局池缺陷**——`max_requests_per_entry`（配置语义"单入口"）被实现为
> run 级全局池（`_read_requests_used` 入口间不重置）——8/22 run 实证 131 入口仅
> 前 4 个有读码素材（3+7+8+2=20 封顶），**127 入口零上下文盲探**（探针因单批入口
> 少而掩盖）；**D2 连带**——D1 修复后 AI 调用量 131 → 556-1112 次，
> `max_requests_per_run=140` 成为新截断点。
> **状态**：已按评审 `2026-08-27-p2-parallel-exploration-review.md`（有条件通过）修订
> P1-1/P1-2/P1-3 三处技术细节显式化 + 验收四项精确性补充；核验另发现 D2 影响面
> （`max_requests_per_run` 为 context_budget 全轨共享池——三处消费点）已并入 D2 节
> ——待用户批准后实施

## 1. 目标与范围

| 项 | 性质 | 内容 |
|---|---|---|
| D1 | **缺陷修复** | 读码预算入口局部化（对齐配置语义"单入口上限"） |
| D2 | 参数放开（P-1 同逻辑） | `max_requests_per_run: int \| None`——None 无上限（验证阶段临时） |
| D3 | 并行化 | `explore_all` 复用 `BoundedJobScheduler`（新配置 `entry_concurrency`） |

**非范围**：`max_rounds_per_entry`/`max_requests_per_entry` 值调整（数据后回归）；
`_MAX_CONTEXT_BYTES_PER_REQUEST`；verify/deep_dive 轨。

## 2. 详细方案

### D1 读码预算入口局部化

```python
# _explore_entry：入口局部计数（修复全局池缺陷）
entry_read_used = 0
for round_index in ...:
    requests_budget = self._settings.max_requests_per_entry - entry_read_used
    executed = self._execute_read_requests(...)
    entry_read_used += len(executed["records"])   # 执行数（预算截断不计）
# self._read_requests_used 保留——仅 run 级统计（探针/summary 口径不变）
```

- `_execute_read_requests` 现返回 `records/texts/deduplicated/new_available`——`len(records)` 即本轮执行数（预算截断与去重跳过均不入 records）；
- run 级统计 `_read_requests_used` 继续累加（`read_requests_used` 属性语义不变）。

### D2 `max_requests_per_run` 支持 None

- **config.py:185**：`int | None = Field(default=140, ge=1)`——None = 无上限；描述注明"验证阶段临时形态"；
- **config/default.yaml**：`max_requests_per_run: null`；
- **消费点（评审核验补充——共 3 处，context_budget 全轨共享池）**：
  `orchestrator.py:1138` `budgeted_ai_call`（explorer）、`:1149` `budgeted_deep_dive_call`
  （深挖）、`:1286` `_budgeted_ai_call`（L1/L2 复核）——三处检查均在
  `_ai_budget_lock` 锁内，统一改为 `is not None and ...` 短路；
- **影响面明示（比原方案更大）**：该预算是 **run 级全轨共享**——放开后探索/深挖/L1L2
  复核的 AI 调用全部无上限（D1 修复后 validated/partial 量涨 → L2 复核量同步涨，全部
  放行）。验证阶段可接受（这正是要采集的数据），但必须在 T1 恢复清单闭合；
- **两本账关系（评审 P1-2）**：orchestrator 层 `_ai_requests_used`（`_ai_budget_lock`
  保护——检查+计费，并发安全）是**控制账**；`ExplorerOrchestrator._ai_requests_used`
  （explorer.py:130，`await self._ai_call(...)` 后自增）是**纯统计账**（无检查语义，
  单线程事件循环下自增原子、竞态无害——最终一致）。D3 并行化不改变该分工；
- **todo T1 同步**：恢复清单加 `max_requests_per_run`（第四个临时值）。

### D3 并行化（复用 BoundedJobScheduler）

**新配置**：`ExplorerSettings.entry_concurrency: int = Field(default=4, ge=1, le=16)`（对齐
`candidate_concurrency=4` 与 provider in-flight=4——恰好饱和管道不放大限流压力）。

**explore_all 重构**：

```python
jobs = [IndexedJob(index, entry) for index, entry in enumerate(entries)]
async def worker(job):
    if skipped_short_circuit:  # circuit 已由 scheduler 承担——worker 直跑
        ...
    return await self._explore_entry(job.value)  # (candidates, terminated_by, rounds)
scheduled = await run_indexed_jobs(
    jobs, worker,
    max_concurrency=self._settings.entry_concurrency,
    circuit=task_circuit,
    opens_circuit=lambda r: r[1] == "short_circuit",
)
```

- **熔断映射（评审 P1-1 显式化）**：`opens_circuit=lambda r: r[1] == "short_circuit"`——
  **仅** `short_circuit`（AI 熔断类失败：circuit_breaking/skipped——explorer.py:256-260
  分支）触发熔断；`error`（单入口 AI 失败，**非熔断类**）**不触发**——失败入口自身
  terminated_by=error，其余入口继续探索。与现串行版 `skipped_short_circuit` flag 语义
  严格对齐（串行版同样只在 short_circuit 后跳过剩余入口）；
- **保序**：scheduled.results 按 index 排序——observations entries 与 candidates 均按
  入口序汇总（与串行输出形状一致）；
- **候选上限（非 None 时）检查位置（评审 P1-3 显式化）**：在 **worker 函数内、
  `await self._explore_entry(...)` 之前**查全局累计——超限时 worker 直接返回空结果
  （记 skipped 口径），已启动入口候选全收（"软上限"语义，超限量 ≤
  entry_concurrency × 单入口峰值——验证阶段 None 无此路径）；
- **并发安全（评审 P1-2 显式化）**：asyncio 单线程事件循环——同步 SQLite 读码
  （毫秒级）无 await 点、天然原子；`ExplorerOrchestrator` 两个计数字段均为**纯统计账**
  （自增无检查语义——见 D2 两本账说明），交错自增最终一致无害；**控制账**
  （`max_requests_per_run`）在 orchestrator 层 `_ai_budget_lock` 内检查——并发安全。
  AI cache（hash 定址）/trace 落盘并发已被 L1/L2（candidate_concurrency=4）生产验证；
- **探针兼容**：probe_explorer_entry 直接调 `explore_all`——自动并行（2 入口并发
  2）；**统计口径并行下不变**（guidance_usage/seed_hit_rate 均按 entry 聚合，
  observations 保序）；行为注记进探针 plan 输出（entry_concurrency 透传）。

## 3. 风险

1. **provider 限流（429）频率上升**——Retry-After 冷却（provider controller）已有自愈
   先例；若全量 run 出现持续冷却，调低 `entry_concurrency`（一行配置）；
2. **并行下 AI 请求交错**——`ai_requests_used` 预算（非 None 时）判定在 await 点附近
   ——单线程原子；D2 放开后验证阶段无截断；
3. **候选上限软超限**（恢复上限后）——文档明示语义，验证阶段 None 无影响；
4. **D1 修复后调用量暴涨**（131→556-1112 次 AI 调用）——成本与时长上升（并行 4 路
   压回 ~1.5h），这正是要采集的无偏数据。

## 4. 实施顺序（依赖链）

D1（缺陷）→ D2（参数）→ D3（并行）→ T1 全量 run。D1/D2 独立可单测；D3 依赖 D1
（不修则并行下预算抢光更糟）。
