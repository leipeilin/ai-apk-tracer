# 任务实施方案：M4-SEED-HOPS（探索骨架链——validated=0 根本修复）

> **任务编号**：M4-SEED-HOPS
> **日期**：2026-08-23
> **依据**：实施计划 §3.5 M4（"prompt 迭代属 M4"——验收记录 §4.3 执行记录）；四轮定向探针实证结论（产链 3→0→1→1 波动、~17% 产链率——措辞与信息注入均钝感，属生成方式问题）
> **状态**：已闭合（六阶段完整执行——评审 R-1~R-7 全部采纳；验收 A-1~A-9 通过，见 acceptance-plan 回填）
> **前置依赖**：M2 机械链路（✓）；指引 `2026-08-23-m2m3-forward-guidance.md` §9（根本性稳定方案 = seed hops）

---

## 1. 任务目标与范围

把探索产链从"模型从零发明 hops"改为"**确定性骨架 + 受限扩展**"：驱动层用 call_tree 的 resolved 调用边构造入口第一跳 seed（确定性、可回查），注入模型输入；模型的职责变为"沿 seed 方向评估与扩展、判定 source/sink 语义"——LLM 对给定素材的重组远比开放生成稳定（根本稳定化）。

**范围（in scope）**：
1. `backend/app/analysis/ai_models.py`——`SeedHop` 模型 + `ExplorerInput.seed_hops` 可选字段；
2. `backend/app/analysis/explorer.py`——首轮前构造 seed（`get_callees`，上限 N=8）+ 每轮注入；
3. `prompts/explorer/1.0.0/system.md`——seed 使用约束（骨架非结论；产链第一跳优先取 seed；seed 之外的第一跳须自行取证）；
4. `backend/tests/`——seed 构造/注入/协议断言测试 + registry 哈希同步；
5. 探针复验（`scripts/probe_explorer_entry.py`——行为级验收）。

**非范围**：T4.1~T4.4 评估指标闭环（后续任务）；深挖/核验轨不动；`SeedHop` 不进 ExplorerObservation 输出（seed 是输入侧事实，输出契约零改动——跳回查口径不变）。

## 2. 现状锚点

- **四轮探针实证**：v1（3 链）→v2（0）→v3（1）→v4（1，含攻击面注入）——产链率 ~17%，门槛 50%；D-3 四轮 100% 遵守；`MainActivity` 3 轮 loop_done 即产链（信息足够时模型表现良好）——瓶颈在"第一跳从哪来"。
- `get_callees(method_id)`（call_tree.py:154-165）返回 resolved 边 + gaps——seed 数据源现成（`callees` 的 method summaries 含 method_id）。
- `ExplorerInput`（ai_models.py:408-425）：现有字段 round_index/rounds_budget/requests_budget/entry_json/attack_surface_json/prior_observations/code_context——加 `seed_hops` 不破坏既有字段。
- 输出侧 `ExplorerObservation.chain_proposals.hops` 的跳回查（explorer_validation）不区分 hop 来源——seed 起点的 hop 天然通过回查（call_sites resolved）。
- registry schema 由 `sync-ai-protocol.py --write` 从 ai_models 生成——字段新增后须重同步（哈希门禁）。

## 3. 详细实现方案

### 3.1 SeedHop 模型（ai_models.py）

```python
class SeedHop(StrictAIModel):
    """探索骨架链第一跳（驱动层从 call_tree resolved 边构造——确定性可回查）。"""
    from_method_id: MethodId = Field(description="入口方法 ID")
    to_method_id: MethodId = Field(description="被调方法 ID（resolved 调用边）")
```

`ExplorerInput` 加可选字段：

```python
seed_hops: list[SeedHop] = Field(default_factory=list, max_length=16,
    description="入口第一跳骨架（确定性 resolved 调用边——构造链时优先以此为起点扩展，不必自行虚构第一跳）")
```

### 3.2 驱动层 seed 构造（explorer.py）

- `_explore_entry` 循环前构造一次：`seed_hops = self._build_seed_hops(entry)`——`get_callees(method_id)["callees"]` 取前 8 个的 method_id 组装 `[SeedHop(from_method_id=入口, to_method_id=callee)]`；gaps/空 callees → 空列表（降级为现状行为）。
- 每轮 `model_input` 构造注入同一份 seed（幂等——与 attack_surface_json 同模式）。
- 异常容错：seed 构造失败（call_tree 异常）记 warning 返回空列表——探索不因 seed 阻塞。

### 3.3 prompt 约束（system.md 硬约束 12）

```markdown
12. 骨架链使用：输入的 seed_hops 是入口第一跳的确定性调用边（已验证可回查）。
构造 chain_proposals 时第一跳优先从 seed_hops 中选取（from/to 直接可用，call_site_line
仍须从你读过的代码确认）；若 seed 无合适方向，须先 read_requests 取证再构造，不得虚构
第一跳。seed_hops 是起点骨架而非结论——source/sink 语义与后续跳仍由你判定。
```

### 3.4 文件变更清单

| 文件 | 变更 |
|---|---|
| `backend/app/analysis/ai_models.py` | SeedHop + ExplorerInput.seed_hops |
| `backend/app/analysis/explorer.py` | _build_seed_hopes + 首轮构造 + 每轮注入 |
| `prompts/explorer/1.0.0/system.md` | 硬约束 12 + 输入说明补 seed_hops |
| `prompts/registry.yaml` | sync --write 哈希同步 |
| `backend/tests/test_explorer.py` | seed 构造/注入/降级测试 |
| `backend/tests/test_explorer_protocol.py` | 约束 12 token 断言 |

### 3.5 风险与回退

| 风险 | 对策 | 回退 |
|---|---|---|
| seed 指向 SDK 内部类（无敏感方向） | seed 是"起点选项"非指令——模型可弃用并自行取证（约束 12 双路径） | seed_hops 空列表即回退现状 |
| 输入 token 膨胀 | N=8 上限 + method_id 短文本 | 调小 N |
| 模型照抄 seed 当结论（无 source/sink 语义） | 约束 12 显式"骨架非结论"；跳回查仍全量校验（输出侧不放宽） | revert prompt |
| registry 哈希链 | sync --write 一次同步（既有流程） | — |

## 4. 与大纲一致性对照

- 实施计划 §3.5 M4 承接"prompt 迭代"职责 ✓（本任务是迭代中"生成方式"层的根本手段）；
- 不改变 ExplorerObservation 输出契约/三档校验/归一化主链（大纲 §2.4-2.6 零改动）✓；
- 复用 get_callees/ExplorerOrchestrator 既有注入模式（attack_surface_json 先例）——零重复造轮 ✓。
