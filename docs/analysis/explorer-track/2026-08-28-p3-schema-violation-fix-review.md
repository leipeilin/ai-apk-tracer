# 任务审查报告：P-3 探索输出协议违规修复

> **审查对象**：
> - `2026-08-28-p3-schema-violation-fix-implementation-plan.md`（实施方案）
> - `2026-08-28-p3-schema-violation-fix-acceptance-plan.md`（验收方案）
> **审查方式**：逐条对照代码事实核验锚点真伪 + 根因诊断合理性 + L1/L2/L3 正确性 + 验收可执行性
> **审查时间**：2026-08-28

---

## 一、总体结论

**结论：✅ 通过（建议澄清 2 个轻微实施细节后批准）。**

这是**截至目前根因诊断最扎实的一份方案**——不是靠"感觉 error 率高"，而是用探针 `error_detail` 实证锁定 `schema_invalid`（string_too_long + value_error 两类），并用"48 次 4 并发压测全过"证伪了服务端假设。三层修复（L1 放宽 / L2 宽容 / L3 重试）层次清晰、职责分离、各自可回退。锚点全部属实。

---

## 二、锚点真实性核验（全部属实 ✅）

| 方案声称 | 代码事实 | 结论 |
|---|---|---|
| `reason` 字段 256 上限 | `ExplorerLoopState.reason`（`ai_models.py:339`）与 `VerifyLoopState.reason`（`:583`）均为 `ShortText`（`max_length=256`） | ✅ 属实 |
| 干净出口校验用 `"无敏感"` 单串 | `_done_requires_chain`（`ai_models.py:357`）`"无敏感" not in (self.loop.reason or "")` | ✅ 属实 |
| `schema_invalid` 分类存在 | `ai.py:729` ValidationError → `invalid_classification = "schema_invalid"` | ✅ 属实 |
| L3 取值 `result.get("error",{}).get("classification")` | `_analysis_failure`（`ai.py:1042-1044`）确有 `error.classification` | ✅ 路径可达（见观察 O-1） |
| schema_invalid 时 status="failed" | `_analysis_failure` 返回 `status: "failed"`（`ai.py:1036`） | ✅ L3 触发条件成立 |
| 探针 error_detail 已实施 | `probe_explorer_entry.py:151` `_extract_error_detail` 已存在 | ✅ 属实 |

---

## 三、发现的问题（均为轻微，不阻塞）

### O-1【轻微】L3 取值路径层级与现有代码不一致

方案 L3 写 `result.get("error", {}).get("classification")`（取嵌套 error），但现有 `_explore_entry`（`explorer.py:322-331`）判断用的是**顶层** `result.get("status")` / `result.get("circuit_breaking")`。

`_analysis_failure` 返回结构**同时有**顶层 `classification`（`ai.py:1037`）和嵌套 `error.classification`（`:1044`）——两条路径都能取到，功能不受影响。但**建议统一用顶层 `result.get("classification")`**，与现有代码层级一致，避免实施者困惑。

### O-2【轻微】L3 重试的 `_ai_requests_used += 1` 与现有自增（`explorer.py:321`）的配合需明确

现有代码：

```python
result = await self._ai_call(model_input)
self._ai_requests_used += 1   # ← 321 行，无条件自增（首次调用）
```

L3 伪代码里又写了 `self._ai_requests_used += 1`（重试）。**两处自增需配合**：首次自增（321 行）保留 + 重试自增（L3 块内）→ 总共 +2。伪代码未体现"321 行首次自增仍保留"，实施者需理解这是**叠加**而非替代。建议方案补一句明确"重试自增在首次自增（321 行）之外叠加"。

（注：这里自增的是 `ExplorerOrchestrator._ai_requests_used`（stage 级统计），run 级预算池由 orchestrator 层 `budgeted_ai_call` 的 `_ai_budget_lock` 独立计费——两本账不冲突，与 P-2 审查时确认的一致。）

### O-3【说明】L2 关键词 `"none found"` 的边界

`"none found"` 英文变体可能出现在"no relevant methods found"（继续探索语境）这类 reason 里。但方案已声明偷懒 reason（"需要更多上下文"/"more context"）仍拒（P3-2 覆盖），且有三层预算兜底 + `redundant_done_rounds` 可观测。**可接受**，仅提示 `"none found"` 是集内边界最宽的词，若 T1 重跑发现偷懒通过，优先审视它。

### O-4【待确认】L1 引用的"P-1 误改 5 处教训"

方案 L1 提到"防 P-1 误改 5 处的教训"（reason 字段只改 maxLength，不动其他 5 处）。此引用指向 `8830704`（P-1 验证参数放开）。**未逐条核实 P-1 是否真的误改过 5 处**，但该引用的目的是强调"仅改 reason 字段"这一正确原则，不影响方案正确性。若需严谨，可回看 P-1 提交确认。

---

## 四、验收方案的评估

### 做得好的地方

1. **P3-7（T1 重跑）设为核心验收 + 明确的量化门槛**（error 率 50.5% → <10%）——把"修复是否有效"落到可测量的数字上，而非"看起来修好了"。
2. **P3-8（golden 判决）明确依赖 P3-7 的干净数据**——依赖链清晰，不本末倒置。
3. **P3-5 非 schema_invalid 失败不重试**——防止与 transport 层既有重试叠加（双重重试会导致调用量爆炸），这是很细的工程考量。
4. **回退方案**：L1/L2/L3 各自独立可 revert + schema 哈希随 sync 恢复。

### 需补充/确认的验收项

| 项 | 问题 | 建议 |
|---|---|---|
| P3-1 | "schema 仅 reason 字段 maxLength 变更"的断言，需**明确如何验证"其他字段未动"** | 补一条：diff 校验 schema 文件仅 reason 字段的 maxLength 变化（或 sync --check 前置后置 diff） |
| P3-3 | `ai_requests_used` 计 2 次的断言，需与 O-2 的"叠加自增"对应 | 明确断言 stage 级 `ai_requests_used` = 首次 1 + 重试 1 = 2 |
| P3-7 | "278 入口修正取样后"——**需确认探针 `--max-entries` 参数已真实支持全量**（方案正文说"已证可选 278"，但 T1 实跑 198 因默认取样） | 补明确：重跑命令显式 `--max-entries 300` 且验证 selected 数量 = 278 |
| P3-6 | "存量 F5 干净出口测试适配新关键词集"——**F5 的测试可能硬编码了"无敏感"单串** | 确认 F5 相关测试（`test_explorer_protocol.py`）的断言需随 L2 更新 |

---

## 五、审查结论与建议

| 优先级 | 事项 | 动作 |
|---|---|---|
| 轻微 | O-1 取值路径层级统一 | 建议改顶层 `result.get("classification")` |
| 轻微 | O-2 自增叠加明确 | 补一句"重试自增在 321 行首次自增之外叠加" |
| 说明 | O-3 `none found` 边界 | 提示 T1 重跑关注 |
| 待确认 | O-4 P-1 教训引用 | 可回看确认 |
| 补充 | P3-1/P3-3/P3-7/P3-6 四个验收项精确性 | 逐一补明确（见上表） |

---

## 六、总体评价

这份方案最大的价值在于**根因诊断的实证严谨性**：用探针 `error_detail` 锁定了 100% 的 error 都是 `schema_invalid`（而非网络/限流），并用 48 次压测证伪了服务端假设，把"error 率高"这个模糊现象精确拆解为**两个具体的协议违规点**（reason 超 256 + 干净出口关键词过严）。这为 L1/L2 的修复提供了明确靶点，而非盲目调参。

三层修复的**职责分离**也很清晰：L1 治本（给足字段空间）、L2 治标（宽容关键词）、L3 兜底（违规不弃入口）。验收把 T1 重跑设为"error 率 <10%"的硬门槛，形成闭环。

**结论：✅ 通过，建议澄清 O-1/O-2 两处实施细节后批准。**
