# 修复方案：CONTEXT_SLICE_MISMATCH（slice_id 生成键补全）

> **日期**：2026-08-14
> **前置**：`docs/analysis/2026-08-14-slice-mismatch-problem.md`（问题分析，已确认）
> **状态**：⏸ 待用户确认后实施

---

## 1. 方案总览

**根因一句话**：`_slice_id()` 只哈希 `rule_id + component_name + locations`，不含 `sources`/`sinks`/`propagation_paths`，导致同组件同 locations 的多条不同链候选共用同一 slice，AI 分析跨链污染、finding sinks 与 slice 内容不一致。

**修复核心**：把链身份纳入 slice_id 生成键，让**每条不同链拥有独立 slice**。

---

## 2. 具体修改

### 2.1 `backend/app/analysis/context_builder.py` `_slice_id()`（核心）

```python
def _slice_id(candidate: dict[str, Any]) -> str:
    stable = json.dumps({
        "rule_id": candidate.get("rule_id"),
        "component": candidate.get("component_name"),
        "locations": candidate.get("locations", []),
        # v2026-08-14 修复：链身份纳入 slice 键，防止同组件同 locations
        # 的多条不同链候选共用同一 slice（CONTEXT_SLICE_MISMATCH 根因）。
        "sources": _anchor_projection(candidate.get("sources", [])),
        "sinks": _anchor_projection(candidate.get("sinks", [])),
        "propagation_paths": _anchor_projection(candidate.get("propagation_paths", [])),
    }, ensure_ascii=False, sort_keys=True)
    return "slice_" + hashlib.sha256(stable.encode()).hexdigest()[:20]
```

新增辅助（同文件）：

```python
def _anchor_projection(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """锚点投影：只保留影响链身份的最小字段，避免无关字段抖动导致 slice 碎片化。"""
    return [
        {key: item.get(key) for key in ("path", "line", "kind", "method_name") if key in item}
        for item in items if isinstance(item, dict)
    ]
```

**设计要点**：
- 用 `_anchor_projection` 而非全量 json——sources/sinks 可能带大量 AI 扩展字段，全量哈希会让同一链因无关字段抖动生成不同 slice；投影只取 `path/line/kind/method_name` 四键，稳定且足够区分链
- 传播路径同理投影（它是链身份的一部分）

### 2.2 影响分析（改动前后行为对比）

| 场景 | 改动前 | 改动后 |
|---|---|---|
| 同组件同 locations 不同 sink | 共用 1 slice（AI 跨链污染） | 每链独立 slice（各看各的） |
| 同组件同 locations 同 sink | 共用 1 slice | 仍共用（投影相同）→ 行为不变 |
| 不同组件/不同 locations | 独立 slice | 独立 slice → 行为不变 |

**预期副作用**：slice 总数增加（78 候选 → 13 条不同链 → 最多 13 个 slice 而非 1 个）。这是**正确的代价**——AI 为每条链各看一次上下文。

### 2.3 聚合层（`aggregate.py`）无需改动

聚合的 `merged["sinks"] = primary.get("sinks")` 已经"来自同一个 primary"（正确）；修复 slice_id 后，primary 的 sinks 与其 slice_id 指向的 slice **天然一致**（因为 slice 是按 primary 的链生成的）。聚合层逻辑保持不变。

### 2.4 其他联动点核查（已确认无需改）

| 位置 | 是否需改 | 原因 |
|---|---|---|
| `orchestrator.py:217` candidate["slice_id"] 挂载 | ❌ | 挂载逻辑不变，只是 slice_id 值更精确 |
| `orchestrator.py:1041 _latest_slice` | ❌ | 按 slice_id 读目录，键值变化无影响 |
| `candidate_funnel.py` dedupe | ❌ | 基于 exact_candidate_projection，与 slice_id 无关 |
| `slices/candidates.json` | ❌ | 只是落盘，键值变化无影响 |

---

## 3. 测试计划

### 3.1 单元测试（`test_context_builder.py` 或新增）

1. **`test_slice_id_distinguishes_different_sinks`**：同 rule+component+locations、不同 sink 的两个候选 → 断言 slice_id 不同（这是本 bug 的最小回归）
2. **`test_slice_id_stable_for_same_chain`**：同 sink 的候选重复生成 → slice_id 相同（防抖动）
3. **`test_slice_id_ignores_unrelated_fields`**：sinks 带不同无关扩展字段（如 AI 加的自定义 key）→ slice_id 相同（验证投影设计）

### 3.2 回归验证

1. `scripts/check-all.sh` 全量（预期新增测试 + 既有全绿）
2. 用真实 run 110600Z 的 candidates 重放 `_slice_id()`，统计：78 候选 → 13 条不同链 → 13 个不同 slice_id（预期），确认不再碰撞

---

## 4. 风险与限制

| 风险 | 等级 | 缓解 |
|---|---|---|
| slice 数量增加 → AI 调用成本上升 | 中 | 这是正确的语义代价；funnel 的 representative 机制已限制实际进 AI 的候选数 |
| `_anchor_projection` 漏字段导致仍碰撞 | 低 | 测试 1 覆盖最小场景；后续若发现新碰撞按同模式补字段 |
| 历史 run 数据不回溯 | 低 | 修复只影响新 run；旧 run 保持现状（与 severity/元数据修复一致的处理策略） |

## 5. 建议的补充（可选，独立于本次修复）

**一致性校验器**（长期防线）：在 skill 收集器之外，产品侧增加"finding.sinks 与其 slice_id 的 slice candidate.sinks 一致"的运行时断言——本次是外部工具发现的，未来应在产品内自检。此项可作为后续独立任务，不阻塞本次修复。

---

## 6. 结论

**修复范围小**（1 个函数 + 1 个辅助函数 + 3 个测试），根因明确、改动无联动风险。**请确认后实施。**
