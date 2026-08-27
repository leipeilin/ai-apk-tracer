# 任务实施方案：P-3 探索输出协议违规修复（T1 全量 run error 根因）

> **任务编号**：P-3（T1 全量 run 排查结论——用户指令 2026-08-28）
> **根因**（复现确证，探针 error_detail 实证）：T1 error 率 50.5%（100/198 入口零候选）
> 全部为 `schema_invalid`（HTTP 200 快速失败，非网络/超时/限流——48 次 4 并发压测全过
> 证伪服务端假设），两类协议违规：
> ① `loop.reason: ShortText(max 256)`——第 3/4 轮复杂探索总结超限 → `string_too_long`；
> ② F5 干净出口校验要求 reason 严格含中文"无敏感"——模型写英文/变体被拒 → `value_error`。
> repair 二次修复同样踩坑 → error → **整个入口弃掉（2-3 轮读码投入全损）**。
> **状态**：已按评审 `2026-08-28-p3-schema-violation-fix-review.md`（✅ 通过）澄清
> O-1（取值路径统一顶层 classification）/O-2（重试自增叠加语义）+ 验收四项精确性
> （P3-1 diff 校验 / P3-3 叠加断言 / P3-7 全量前置确认 / P3-6 存量适配）——待批准后实施

## 1. 目标与范围

| 项 | 层 | 修复 |
|---|---|---|
| L1 | 协议 | `reason` 字段 256→2000（Explorer + Verify 两协议同步） |
| L2 | 校验 | 干净出口关键词集扩中英变体 |
| L3 | 驱动 | schema_invalid 轮级重试 1 次（不弃入口） |
| 附 | 探针 | error_detail 记录（**已实施并实证**——本排查的锁定工具，随本任务提交） |

**非范围**：repair 协议改动（通用协议不动）；prompt 文案（约束 5 关键词表述已在——
模型不遵守是校验宽容度问题）；模型切换。

## 2. 详细方案

### L1 reason 字段放宽（ShortText 256 → ReasonText 2000）

```python
# ai_models.py：新类型（与 ExplorerContextText 同模式——专用上限）
ReasonText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
# ExplorerLoopState.reason / VerifyLoopState.reason: ShortText → ReasonText
```

- schema 同步：`ai_explorer_observation.schema.json` / `ai_verify_observation.schema.json`
  的 reason `maxLength 256 → 2000`（**只有 reason 字段**——防 P-1 误改 5 处的教训）；
- `sync-ai-protocol --write` 哈希更新；prompt_version 不升级（schema-only 变更，
  F5 known_findings 先例）；
- 2000 依据：审计说明的合理上限（256 显然不足——T1 实证；10K LongText 过宽——
  reason 是轮末状态说明非证据文本）。

### L2 干净出口关键词集（"无敏感"单串 → 中英变体集）

```python
# ai_models.py：_done_requires_chain
_CLEAN_EXIT_MARKERS = (
    "无敏感", "无危险", "无安全风险", "未发现敏感", "无可达敏感",
    "no sensitive", "not sensitive", "no security", "none found",
)
def _is_clean_exit(reason: str) -> bool:
    lowered = (reason or "").lower()
    return any(marker in lowered for marker in _CLEAN_EXIT_MARKERS)
```

- 替换 `"无敏感" not in reason` 为 `not _is_clean_exit(reason)`；
- 集合克制原则：防"需要更多上下文"类偷懒 reason 通过（这些词不含敏感语义标记）；
- 宽容度权衡声明：关键词过宽的代价（偷懒提前收工）有轮预算/候选预算/no_new_requests
  三层兜底——远小于 50% 入口误杀的损失（T1 实证）。

### L3 schema_invalid 轮级重试（弃入口 → 重试本轮一次）

```python
# explorer.py _explore_entry 轮循环内：
result = await self._ai_call(model_input)
self._ai_requests_used += 1  # 321 行首次自增保留（不变——L3 重试自增在其外叠加）
if (
    result.get("status") != "completed"
    and result.get("classification") == "schema_invalid"  # 顶层取值（评审 O-1——与现有代码层级一致）
):
    # L3：协议违规是单轮问题（ai.py 注释定位"不短路"）——重试本轮一次；
    # 仍失败则按原路径终止（重试计数入 observations 可审计）
    result = await self._ai_call(model_input)
    self._ai_requests_used += 1  # 重试叠加自增（评审 O-2：首次+重试共 +2——stage 级统计账）
```

- 重试后仍非 completed → 走原 error/short_circuit 分支（不变）；
- 重试成功 → 正常继续轮循环（等效"该轮重来"）；
- observations 轮记录加 `schema_retry: true` 标记（重试发生可审计）；
- 熔断不受影响（schema_invalid 本就非 circuit_breaking）；
- 调用量增量上界：失败轮数（T1 为 100 次）——验证期 AI 无上限可接受；
- 计费口径：run 级预算由 orchestrator 层 `budgeted_ai_call`（`_ai_budget_lock`）独立
  计费每次真实调用——L3 重试自动入账，与 stage 统计账不冲突（P-2 两本账结论）。

### 附：探针 error_detail（已实施——2026-08-28 排查时）

`probe_explorer_entry.py` 的 round_probes 增 `error_detail`（classification/http_status/
message/empty_initial_content/finish_reason/reasoning_tokens/protocol_relaxed/
initial_validation_errors）——本任务提交归档。

## 3. 风险

1. **L2 偷懒通过**（done+空链+写关键词收工）——三层预算兜底 + 探针
   `redundant_done_rounds` 可观测；接受；
2. **L3 重试调用量 +100**（最坏）——验证期无上限；定参后回归
   （重试语义保留，预算口径计入）；
3. **L1 token 消耗微增**（reason 可写 2000）——输出侧增量远小于上下文 40K。

## 4. 实施顺序与 T1 重跑

L1/L2（协议+校验，独立可测）→ L3（驱动）→ 零回归 → **T1 重跑**（全量探针，
预期 error 50.5% → <10%）→ 干净的四参数数据 + T2 golden 判决。
