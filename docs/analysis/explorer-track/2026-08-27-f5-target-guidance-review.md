# 任务审查报告：F5 目标组件引导（探索方向与规则轨联动）

> **审查对象**：
> - `2026-08-27-f5-target-guidance-implementation-plan.md`（实施方案）
> - `2026-08-27-f5-target-guidance-acceptance-plan.md`（验收方案）
> **审查方式**：逐条对照代码事实核验锚点真伪 + 方案逻辑完整性 + 验收可执行性
> **审查时间**：2026-08-27

---

## 一、总体结论

两份方案**整体质量高、可实施**，锚点真实、口径准确、验收可执行。但存在 **2 个需修订的实质问题**（P1）、**1 个方案逻辑隐患**（P2），以及若干可优化项。建议修订后批准。

**结论：有条件通过（修订 2 个 P1 后批准实施）。**

---

## 二、锚点真实性核验（全部属实）

| 方案声称 | 代码事实 | 结论 |
|---|---|---|
| `orchestrator.py:1156` `effective` 原序传入 `explore_all` | 实际在 `orchestrator.py:1149-1156`，`effective` 按 `api_entry_table` 原序、`method_id` 过滤后传入 | ✅ 属实（行号偏移 7 行，可接受） |
| `_aggregate_capabilities` 按 `component_name` join 的先例 | `attack_surface.py:254` 确实存在，按 `component_name` 精确匹配聚合成 `sensitive_capabilities` | ✅ 属实 |
| 规则候选挂载 `component_name` | `rule_runner.py:143` 候选含 `component_name` 字段 | ✅ 属实 |
| 空转变体证据（F2 V-2） | 已在 F2 核验记录在案（DataMessageCallbackService 4 轮重复） | ✅ 属实 |
| 约束 5 现表述（"done=true 须伴随 proposal"） | `system.md:8` 约束 5 确实硬性要求"done=true 必须伴随至少一条 chain_proposal" | ✅ 属实 |
| `ExplorerInput` 无 `known_findings` 字段 | `ai_models.py:420` 现字段仅 round_index/budget/entry_json/attack_surface_json/seed_hops/prior_observations/code_context | ✅ 属实，需新增字段 |

---

## 三、发现的问题

### P1-1【实质问题】`finding_components` 数据源在 explorer stage 时点不可用

方案 3.1 写：

```python
finding_components = {c["component_name"] for c in rule_candidates if c.get("component_name")}
```

**问题**：`rule_candidates` 是 `rule_prescan` 阶段的局部产物，而 `_run_explorer_stage`（`orchestrator.py:1104`）是**独立的 stage 方法**。方案未说明 `rule_candidates` 如何从 rule_prescan 阶段**传递/持久化**到 explorer stage。需明确：

- 是走 `run_dir` 落盘产物（如 `rule_prescan` 的 candidates 文件）再读取？
- 还是通过 `_run_explorer_stage` 的参数注入？

从代码看，rule_prescan 产出的 candidates 会落盘（`orchestrator.py` 有 `save_candidates` / artifact 注册先例），但方案未写明读取路径。**这是实施时最可能卡壳的点**，必须在方案中补充数据源接线细节。

### P1-2【实质问题】验收 A5-5 的"复读守卫"实现层次含糊，且与方案 3.3 存在口径缺口

- 方案 3.3 的红线是"**chain_proposals 不得复读 known_findings**"（复述已知问题不算新发现）。
- 但验收 A5-5 写的是"候选 sink 与 known_findings 的 **rule 对应 sink 完全相同** → 标记复读"。

**缺口 1**：3.3 红线的判定维度是"新链/新 sink/新数据流路径"，A5-5 却退化成"**sink 完全相同**"这一极窄判定——两者口径不一致。若模型复读了 finding 的**组件+问题类型**但换了一个相邻 sink（正是 3.3 担心的"偷懒复述"），A5-5 的机器兜底**拦不住**。

**缺口 2**：A5-5 写"驱动层或校验层拦截/降档"——"或"字暴露出实现层次未定。这是核心风险验收项（文档自认"A5-5 是本任务核心风险验收"），却在验收方案里没有确定落点。建议明确：复读守卫放在 `explorer_normalization.py`（已有 rule_sink_index 去重逻辑，`explorer_normalization.py:311-335` 有"同链口径 component_name 相等 且 sink 一致"的先例可复用）。

### P2【逻辑隐患】优先级排序（3.1）与"探索独立性"存在张力

方案 3.1 把有 finding 的组件排前，3.3 又要求"不得复读 finding"。二者叠加的语义风险是：**排序是"预算优先给高价值入口"，注入是"告诉模型已知问题"**。但这两者**共同假设**"有 finding 的组件值得优先/深挖"，而验收 A5-6 的"方向覆盖验证"又要求"含 finding 组件产出率不低于基线"——这实际上是在**诱导确认性偏差**（confirmation bias）：把预算和上下文都偏向已知 finding，可能**压制对全新攻击面的探索**（而这恰恰是探索轨的价值所在）。

这不是 bug，但**方案缺一个"反方向保护"的说明**：如何保证排序不排挤无 finding 但真正高危的组件入口（如 `extra-close` 这类 golden 目标可能恰是无 finding 组件）？建议在方案中补一句"排序仅影响预算分配顺序，不改变 `entries_explored` 覆盖口径；无 finding 组件仍会被探索（非跳过），只是靠后"。

---

## 四、验收方案的其它评估

### 做得好的地方

1. **A5-5 自我标注为核心风险验收**，且 A5-6 要求"B 类 sink 保持归零 + D-3 不回退"——**防回退意识到位**，与 F2 的探针基线衔接良好。
2. **A5-8 golden 命中设为可选加分项**，并诚实标注成本（~1h）——边界清晰，不把昂贵验证塞进合入门槛。
3. **回退方案独立成块**（三层各自可 revert）——低耦合，工程纪律好。

### 需补充/修正的验收项

| 项 | 问题 | 建议 |
|---|---|---|
| A5-2 | 断言"无 → null / 空 → 不注入"三态，但**未覆盖"多组件同名 finding"**（撞名组件，F1 刚解决过此问题） | 补一条：同名组件 finding 归属按 component_name 精确匹配（复用 F1 的 scope_keys 思路或直接精确匹配） |
| A5-3 | 断言约束 14 token，但**漏了约束 5 放宽后的新表述**——3.4 说"约束 5 放宽"，A5-3 却只断言约束 14 和"约束 5 干净出口语义声明" | 需明确：约束 5 是**改写**（放宽）还是**新增并列条款**？若是改写，协议断言要同时覆盖"done=true 仍须 proposal"的旧语义是否被破坏 |
| A5-6 | "含 finding 组件产出候选率不低于基线"——**基线从哪来？** 探针 6 入口里是否真有含 finding 的组件？方案正文举例 MainTabActivity，但未确认它在 6 入口内 | 需先确认探针入口集里有含 finding 的组件，否则 A5-6 的"方向覆盖验证"无法落地 |
| A5-4 | 重复请求检测的"规范键"（method_id+kind+symbol frozenset）定义在 3.4，但**未说明"部分重叠"如何处理**（只写了"完全重叠"触发终止） | 补明确：部分重叠是否也算重复？v8 的空转证据是"4 轮重复**相同** read_requests"，但实际空转可能是"高比例重叠非完全重叠" |

---

## 五、审查结论与建议

| 优先级 | 事项 | 动作 |
|---|---|---|
| P1 | 3.1 数据源接线（rule_candidates 如何到达 explorer stage） | 补充落盘/注入路径 |
| P1 | A5-5 复读守卫实现层次 + 与 3.3 红线口径对齐 | 明确放 `explorer_normalization.py`；把判定从"sink 相同"扩到"组件+问题类型相同即复读" |
| P2 | 排序的确认性偏差保护 | 补一句"排序不改变覆盖口径、无 finding 组件仍探索" |
| 补充 | A5-2 撞名、A5-3 约束5改写语义、A5-4 部分重叠、A5-6 基线可用性 | 逐一补明确 |

**特别提醒**：A5-5 是文档自己认定的"核心风险验收"，但它目前是两份文档中**最含糊的一项**（实现层次未定 + 判定口径过窄）。若不修订，F5 最核心的价值验证（引导不导致复读）将无法被真正验收。建议优先修 A5-5。
