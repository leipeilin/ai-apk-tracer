# 任务实施方案：F5 目标组件引导（探索方向与规则轨联动）

> **任务编号**：F5（gap-fix-plan backlog 升级——用户指令 2026-08-27）
> **依据**：`acceptance/2026-08-26-explorer-output-gap-analysis.md` 根因 4 + F2 核验 V-2（重复请求空转变体）
> **状态**：已按评审 `2026-08-27-f5-target-guidance-review.md`（有条件通过）修订 P1-1/P1-2/P2 及 4 补充项（详见验收方案"评审修订记录"）——待用户批准后实施

## 1. 目标与范围

让探索轨的方向选择消费规则轨的确定性产出（finding 组件域），解决"自由探索不覆盖已知问题组件"的方向盲区。三层递进 + 一个附带修复：

1. **入口优先级**（驱动层）：有 finding 的组件入口排在探索序列前部（预算先给高价值入口）；
2. **finding 上下文注入**（输入层）：探索某组件时注入"该组件已有 finding 摘要"（rule_id + severity——一行事实），模型知道该组件已被确认存在哪类问题，往相邻攻击面深入；
3. **prompt 约束**（生成层）：指导模型利用 finding 上下文（优先深挖同类组件、验证相邻面），但**保持探索独立性**（不得复读 finding——候选须是新链而非 finding 复述）；
4. **附带（F2 核验 V-2）**：重复 read_requests 检测（同轮与历史轮的请求集合比对——重复即终止该轮）+ "确认无敏感"的合法终止语义（done=true + empty proposals + 无敏感结论 reasoning——打破"必须产链或必须新请求"的死锁）。

**非范围**：验证轨改动；golden 标注调整；taxonomy 扩充。

## 2. 现状锚点

- 入口序：`orchestrator.py:1156` 的 `effective` 按 api_entry_table 原序传入 `explore_all`（无优先级概念）；
- 探索输入：`explorer.py` 的轮次 payload 含 `attack_surface_json`/`code_context`/`seed_hops`——**无规则轨信息**；
- finding 域可提取：`rule_prescan` 的候选已按组件挂载（`attack_surface` 的 `sensitive_capabilities` 聚合同源——`_aggregate_capabilities` 按 component_name join 的先例）；
- 空转变体证据：v8 探针 DataMessageCallbackService 4 轮重复相同 read_requests（F2 核验 V-2）。

## 3. 详细方案

### 3.1 入口优先级（驱动层）

**数据源接线**（评审 P1-1）：`rule_candidates` 是 rule_prescan 阶段的局部产物，`_run_explorer_stage`（`orchestrator.py:1104`）现签名不含它。采用**参数注入**（比读落盘文件干净——无 IO/schema 漂移）：主流程调用点（`orchestrator.py:227`）传入，此时 `candidates` 是纯 rule_prescan 产物（228 行才 extend explorer 结果）：

```python
# orchestrator.py:227 调用处：传入 rule 候选
normalized_explorer = await self._run_explorer_stage(
    run_id, run_dir, manifest, code_index, candidates)
```

```python
# _run_explorer_stage：effective 排序——有 finding 的组件入口优先
finding_components = {c["component_name"] for c in rule_candidates if c.get("component_name")}
effective.sort(key=lambda e: 0 if (e.get("component_name") or "") in finding_components else 1)
```
稳定排序（同级保原序——同组件入口相邻，上下文局部性好）。

**确认性偏差保护**（评审 P2）：排序**仅影响预算分配顺序，不改变 `entries_explored` 覆盖口径**——无 finding 组件的入口仍会被完整探索（非跳过），只是排在序列靠后。探索轨"发现全新攻击面"的价值不受排挤：预算上限触顶时截断的是低优先级尾部，而 `max_candidates_per_run` 的截断本就存在（73/198 覆盖率现状），F5 只是把截断从"随机"变"有据"。

### 3.2 finding 上下文注入（输入层）

探索每入口首轮 payload 增 `known_findings`（该组件的 finding 摘要，无则 null）：

```json
{"known_findings": [{"rule": "ACTIVITY_INTENT_TO_SENSITIVE_SINK", "severity": "medium"}]}
```
数据源：rule_prescan 候选按 component_name 过滤（`_aggregate_capabilities` 同源模式，新 helper）。

**撞名组件归属**（评审补充项）：known_findings 匹配按 `component_name` **精确字符串相等**（F1 撞名教训——`com.a.MainActivity` 与 `com.b.MainActivity` 不得互相污染），不做后缀/模糊匹配。

### 3.3 prompt 约束 14（生成层）

> 14. 目标组件引导：输入的 known_findings 是规则引擎已确认的该组件问题（rule/severity 事实）。利用它定向深挖——**优先验证同类问题的相邻攻击面**（如已知 intent 注入则深查其他 extra 分支/其他入口方法）。**探索独立性红线**：chain_proposals 不得复读 known_findings（复述已知问题不算新发现——须是新链/新 sink/新数据流路径）。

### 3.4 附带：重复请求检测 + 干净出口（F2 核验 V-2）

- `_explore_entry` 维护已执行 read_requests 的规范键集合（method_id+kind+symbol 的 frozenset）；**请求去重执行**——每轮只执行相对历史的**增量请求**（重复请求直接跳过、不消耗执行），增量为空 → 无新信息，触发终止（terminated_by="no_new_requests"）。**部分重叠自然覆盖**（评审补充项）：v8 证据是完全重叠，但高比例部分重叠同样空转——去重执行语义下部分重叠轮只跑增量，非重叠部分仍获探索，仅零增量才终止；
- 约束 5 **改写**（非新增并列）：原文"done=true 必须伴随至少一条 chain_proposal" 放宽为"done=true 须伴随 proposal **或** reasoning 含'确认无敏感操作'结论"（干净出口——打破"必须产链或必须新请求"死锁）。

### 3.5 复读守卫（机器兜底，评审 P1-2 缺口 2）

**落点**：`explorer_normalization.py`——`normalize_explorer_candidates` 增参 `known_findings_index`（组件→rule→sink 键集合，orchestrator 1190 行调用处构造传入）。

**判定口径**（对齐 3.3 红线原文"新链/新 sink/新数据流路径均无"）：**三键全同才算复读**——`component_name` 相同 + 问题类型（rule_id）相同 + sink 键一致（复用 `_sink_keys` 同链口径：method_id 精确匹配，缺失退化 (path, line)——`explorer_normalization.py:311-335` `link_related_candidates` 先例）。

> 评审建议"组件+问题类型相同即复读"**口径过宽**：同组件同类问题的**相邻新 sink** 正是 F5 复发检测的核心价值（remote-aidl/sport-binder 同模式），按该口径会被机器兜底误杀。故仅拦"三键全同"（确定复读），语义级复读（同问题换链）由 A5-5 探针级复核。

**处置**：复读候选标记 `replayed_finding=true` + 降档 unverified + gap 记录（`EXPLORER_FINDING_REPLAY`——critical=false，"复述规则轨已知问题"）。

## 4. 风险

1. **复读风险**（3.3 红线 + 3.5 机器兜底双层缓解 + 验收 A5-5 复读检查）——注入可能让模型偷懒复述 finding；
2. **优先级排序破坏可复现性**（run 间入口序变化——observations 按 entry_id 记录不受影响）；
3. `known_findings` token 增量（单组件 finding 数通常 ≤5——可控）；
4. **确认性偏差**（3.1 保护说明缓解——排序不改覆盖口径；A5-6 加"无 finding 组件入口仍正常产出"断言）。
