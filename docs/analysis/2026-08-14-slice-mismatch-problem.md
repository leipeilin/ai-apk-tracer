# 问题分析：CONTEXT_SLICE_MISMATCH（链尾拼接 / slice 共用）

> **日期**：2026-08-14
> **关联**：finding_005cfbbae73465a350f5（已标记误报，但本 bug 独立存在）
> **状态**：✅ 已确认存在（数据 + 源码双重证据）
> **触发点**：`apk-finding-review` skill 的 `collect_evidence.py:276-285` 报 `CONTEXT_SLICE_MISMATCH`

---

## 1. 问题现象

聚合 finding 的 `sinks` 字段与其 `slice_id` 指向的 canonical slice 的 `sinks` **不一致**：

| 对象 | sink | 来源 |
|---|---|---|
| **finding_005cfbbae...** `.sinks` | `PreferenceUtil.java:221 editorEdit.commit`（removePref） | 聚合 finding |
| **finding** `.slice_id` → `slice_bb21709c...` | `PreferenceUtil.java:124 editorEdit.putBoolean`（setBooleanPref） | canonical slice |

skill 受控收集器逐项比对发现不一致 → 拒绝证据闭包 → `CONTEXT_SLICE_MISMATCH`。

## 2. 根因（已定位到代码行）

### 2.1 `_slice_id()` 生成键缺失 sinks

`backend/app/analysis/context_builder.py:793-799`：

```python
def _slice_id(candidate: dict[str, Any]) -> str:
    stable = json.dumps({
        "rule_id": candidate.get("rule_id"),
        "component": candidate.get("component_name"),
        "locations": candidate.get("locations", []),
    }, ensure_ascii=False, sort_keys=True)
    return "slice_" + hashlib.sha256(stable.encode()).hexdigest()[:20]
```

**只哈希 `rule_id + component_name + locations`，不含 `sources`/`sinks`/`propagation_paths`。** 同一规则 + 同一组件 + 相同 locations 的不同链候选 → **相同 slice_id**。

### 2.2 数据证据：42+ 候选共享同一 slice

`run 110600Z` 的 `slices/candidates.json`（funnel 后 + slice 分配后）显示，MainTabActivity 的 `ACTIVITY_INTENT_TO_SENSITIVE_SINK` 候选 **全部** 的 `slice_id=slice_bb21709c48f77eccd217`，但 sinks 各不相同：

| sink 行 | 含义 | 候选数（抽样） |
|---|---|---|
| 84 / 100 / 102 / 104 / 126 / 128 | PreferenceUtil 各 put/apply 分支 | ~30 |
| 124 | setBooleanPref putBoolean | ~8 |
| 217 / 219 / 221 | removePref apply/commit | ~10 |
| 128 (SplashCommonUtils) | autoJump | ~5 |

这些是**不同的链**（不同 Sink = 不同危害），却共用同一个 slice（其 candidate 是 putBoolean:124 的 7dac60cb07 等）。

### 2.3 聚合层"正确"地保留 primary 的 sinks

`backend/app/findings/aggregate.py:53-55`：

```python
# Source、Sink 与传播路径必须来自同一个 primary，禁止跨成员拼出虚假闭合链。
merged["sources"] = list(primary.get("sources", []))
merged["sinks"] = list(primary.get("sinks", []))
merged["propagation_paths"] = list(primary.get("propagation_paths", []))
```

聚合本身不拼接（符合设计），**但 primary 的 `sinks`（commit:221）与它继承来的 `slice_id`（bb21709c=putBoolean:124）不是同一条链**——因为 slice_id 是建 slice 时挂的（orchestrator.py:217），基于错误的 `_slice_id()`。

### 2.4 不一致的产生路径

```
规则产出 42 个不同 sink 的候选（各自独立链）
  → funnel：非 exact 重复，全部保留为 representative
  → code_slicing：_slice_id() 只哈希 rule+component+locations
      → 42 个候选全部得到相同 slice_id = slice_bb21709c（其 candidate 是 putBoolean:124）
  → AI 分析：全部候选共用 bb21709c 上下文（AI 看的是 putBoolean 链）
  → 聚合：primary = L2+deterministic 排序最高者（sink=commit:221）
      → finding.sinks = commit:221（来自 primary）
      → finding.slice_id = bb21709c（建 slice 时挂的）
  → 不一致！finding 声称的链尾（removePref commit）≠ slice 里的链尾（setBooleanPref putBoolean）
```

## 3. 影响范围

| 影响 | 严重度 | 说明 |
|---|---|---|
| **证据可追溯性破坏** | 高 | finding 的 sinks 与它引用的 slice 内容对不上，人工复核/AI 复核无法回查"这条 sink 的上下文" |
| **AI 分析失真** | 高 | AI 实际看的是 putBoolean:124 的上下文，却要为 commit:221 的候选下结论——跨链污染 |
| **skill 核验阻断** | 中 | CONTEXT_SLICE_MISMATCH 导致收集器拒绝证据闭包，核验流程中断（本 finding 即此情况） |
| **误报率上升** | 中 | 本 finding 的误报（removePref 链不成立）部分源于 AI 被 slice 污染 + 链尾拼接的表象 |

**影响面估计**：`_slice_id()` 是全局逻辑，**所有规则的所有候选**都可能受影响（只要同组件同 locations 有多个 sink）。run 110600Z 共 260 findings，含此模式的 finding 数量需统计（后续验证）。

## 4. 为什么之前没发现

- `_slice_id()` 是建 slice 时静默执行，无一致性校验
- finding 本身看起来"自洽"（sinks 来自 primary，slice_id 来自候选），只有外部逐项比对（skill 收集器）才暴露
- 之前的证据核验（apk-finding-review）聚焦单候选，未批量对账 sinks vs slice

## 5. 相关但独立的问题

1. **primary 选择依赖排序**：aggregate.py:37-47 的 primary 排序用 `(evidence_level, deterministic, rule_id, sources, sinks, paths)` 字典序——同分时 sinks 决定谁当 primary，导致 finding 的 sinks 可能"随机"落在某条链上（本次落在 commit:221）。
2. **`locations` 是否真的相同**：42 个候选的 locations 相同才导致 slice 碰撞——需确认 locations 字段是否包含 sink 位置（若包含则不应碰撞）。**（待验证）**
