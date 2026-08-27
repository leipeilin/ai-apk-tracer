# 任务验收方案：P-2 读码预算修复 + 并行探索

> 对应实施方案：`2026-08-27-p2-parallel-exploration-implementation-plan.md`
>
> **验收执行记录（2026-08-27 实施后）**：P2-1~P2-9 全过（**1336 全量** + lint + 配置验证
> None/4 生效 + 探针 dry-run 透传）；实施中追加修复——聚合层 FAILED（worker 异常）
> 与 SKIPPED（熔断）分离记录（原实现把 worker 异常吞成 short_circuited——测试暴露：
> NameError 被伪装成熔断假象），FAILED 记 error + worker_error 可审计。
> P2-9 全量 run 观察点由 T1 承接。

| 编号 | 验收项 | 方式 | 预期 |
|---|---|---|---|
| P2-1 | **读码预算入口局部化（D1 核心缺陷）** | 单测（FakeAnalyzer 2 入口各发 20+ 请求） | 每入口各自执行至 `max_requests_per_entry`（2 入口总执行 40——修复前全局池封顶 20）；**run 级统计精确断言**：`read_requests_used` = 各入口执行数之和 = 40（无预算截断场景） |
| P2-2 | 预算截断口径不回归 | 单测（A5-4 存量用例） | 完全重叠/部分重叠/预算截断行为不变（`test_requests_budget_truncation` 等） |
| P2-3 | `max_requests_per_run` None 语义（**全轨共享池**） | 单测 + yaml 加载验证 | None 时 140+ AI 调用不截断（**三处消费点**：explorer/deep_dive/L1L2 同放开——评审补充的影响面）；`default.yaml` null 生效；默认 140 兼容 |
| P2-4 | **并行执行 + 保序** | 单测（**FakeAnalyzer 响应前 `await asyncio.sleep(0)` 制造真实并发交错**——同步返回则 peak 恒 1） | 并发度 ≤ `entry_concurrency`（峰值捕获）；observations entries 与 candidates 严格按入口序（与串行版输出形状一致） |
| P2-5 | **并行熔断语义** | 单测（一入口 fail+circuit） | 熔断入口后未启动入口记 `short_circuited`；**in-flight 入口保留完整轮记录**（非截断）；`error`（非熔断）不触发全局跳过——其余入口继续（与串行 `skipped_short_circuit` 语义对齐）；`entries_explored` 计数口径正确 |
| P2-6 | 入口局部预算在并行下独立（与 P2-1 并发版互补） | 单测（**并发交错下** 2 入口各 20 请求） | 并发执行时两入口各自达到 20（`entry_read_used` 为 worker 局部变量——天然隔离，测试确认无共享回归）；无预算截断时总计 40 |
| P2-7 | 候选软上限语义（非 None） | 单测 | **检查在 worker 内、explore_entry 之前**——达到上限后未启动入口跳过；已启动入口候选全收（小幅超限可接受——"软上限"） |
| P2-8 | 零回归 | 全量 pytest + sync --check | 1309+ 全过（现有串行测试不受 D3 影响——entry_concurrency=1 语义等价串行） |
| P2-9 | 探针兼容 | probe dry-run | `entry_concurrency` 透传进 plan 输出；**统计口径并行下不变**（guidance_usage/seed_hit_rate 按 entry 聚合 + observations 保序——并行仅改变执行时序不改聚合口径） |

## 全量 run 观察点（P2-9 之后，T1 承接）

- **elapsed 压缩**：串行预估 ~5h（D1 修复后 278 入口真探索）→ 并行 4 路 ~1.5h；
- **read_requests 分布**：各入口请求量直方（修复前 4/131 有素材 → 修复后应接近全量入口有素材）；
- **各入口轮深/失败率**：D1+D2+D3 后的轮分布与 error 率（对照 8/22 的 65/131）；
- **provider 限流**：429/Retry-After 冷却频率（决定 entry_concurrency 是否回调）。

## 回退

- D1 独立（局部变量——revert 即回全局池，但不建议：缺陷确认）；
- D2 同 P-1 模式（默认值兼容）；
- D3 `entry_concurrency=1` 即语义等价串行（零行为差异回退位）——无需 revert 代码。

## 待办同步

- todo T1 恢复清单**加 `max_requests_per_run`**（第四个临时值）；
- todo 新增：`entry_concurrency` 与 `candidate_concurrency` 的统一并发治理（全量数据后定参）。
