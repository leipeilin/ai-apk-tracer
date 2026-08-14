# 实现方案：产品侧自检（finding.sinks ↔ slice 一致性）

> **日期**：2026-08-14
> **前置**：`docs/updates/2026-08-14-slice-id-fix.md`（根因修复已落地）
> **目标**：不再依赖外部工具（skill 收集器）发现 mismatch，产品侧自检
> **状态**：✅ 已确认（含存量回溯形态 B 补充，2026-08-14 用户确认后实施）

---

## 0. 双形态自检（用户确认后补充）

实测存量 run 110600Z：260 finding 中 **mismatch 109（42%）**、slice 缺失 98（37.7%）、一致仅 53（20.4%）——污染面远超单个案例，存量回溯与扫描期自检同等重要。

| 形态 | 时机 | 用途 |
|---|---|---|
| **A. 扫描期自检** | 新 run 写盘时 | 防未来再产生 mismatch（内嵌 orchestrator） |
| **B. 存量回溯自检** | 对已完成 run 批量跑 | 暴露历史污染（109/260 这类）+ 可选补标 |

两形态共享同一纯函数，一套测试覆盖。

---

## 1. 自检位置

**落点**：`backend/app/analysis/orchestrator.py:287-298`（aggregation 阶段写盘循环）

```python
for finding in findings:
    finding["app"] = app
    scope_finding_id(run_id, finding)
    path = run_dir / "findings" / f"{finding['id']}.json"
    path.write_text(...)          # 写 finding
    evidence = run_dir / "reports" / "evidence" / f"{finding['id']}.json"
    evidence.write_text(json.dumps({
        "finding": finding,
        "manifest_components": ...,
        "permission_definitions": ...,
        "context_slice": self._latest_slice(run_dir, finding.get("slice_id")),
    }, ...))                       # 写 evidence（含 context_slice）
```

**为什么这里**：
1. finding 与 `_latest_slice()` 两者都在手，对比零额外 IO
2. 在 `replace_findings`（:299 落库）**之前**——若发现 mismatch 可标记后再落库，避免"坏数据进库"
3. 与 evidence 落盘（context_slice 已写）天然同处

## 2. 自检逻辑

### 2.1 核心比对

对每个 finding：
1. 取 `finding["sinks"]`（聚合 primary 的链尾）
2. 取 `context_slice["candidate"]["sinks"]`（slice 的链尾）
3. 对两者做**锚点投影规范化**（复用 `context_builder._anchor_projection` 语义：path/line/kind/method_name 四键）后比对**集合相等**（去重 + 排序）

```python
def _finding_slice_sink_mismatch(finding: dict, context_slice: dict | None) -> list[dict]:
    """返回 mismatch 详情列表；一致时返回 []。"""
    if not context_slice:
        return [{"code": "SLICE_UNAVAILABLE", "critical": False}]
    finding_sinks = _anchor_projection(finding.get("sinks", []))
    slice_sinks = _anchor_projection((context_slice.get("candidate") or {}).get("sinks", []))
    if sorted(finding_sinks, key=json.dumps) == sorted(slice_sinks, key=json.dumps):
        return []
    return [{
        "code": "FINDING_SLICE_SINK_MISMATCH",
        "critical": True,
        "finding_sinks": finding_sinks,
        "slice_sinks": slice_sinks,
        "slice_id": context_slice.get("slice_id"),
    }]
```

### 2.2 处理方式（自检结果落地）

| 结果 | 动作 |
|---|---|
| **一致** | 无操作（正常 finding） |
| **不一致** | ① 在 finding 写入 `blocking_gaps` 追加 `{code: "FINDING_SLICE_SINK_MISMATCH", critical: True, finding_sinks, slice_sinks, slice_id}`；② 累加到 run 级 `integrity_mismatches` 计数（写 manifest）；③ **仍落盘 + 落库**（不拒绝）——因为 mismatch 是"可追溯性问题"不是"数据无效"，且 critical gap 已使 severity 自动变 pending（determine_severity 已有逻辑），不静默丢 finding |
| **slice 缺失** | 追加 `{code: "SLICE_UNAVAILABLE", critical: False}` 到 blocking_gaps（不阻断，仅记录） |

**为什么不拒绝落库**：mismatch 的 finding 仍然是有价值的人工复核材料（本次 005cfbbae 正是靠它定位根因）；拒绝落盘会导致"坏数据隐形"（找不到 → 更糟）。标记 + 计数 + pending 定级已足够暴露。

### 2.3 manifest 记录

`self.storage.update_manifest(run_id, ...)` 增加：
```python
finding_slice_mismatches=mismatch_count,
```

## 3. 代码修改清单

| 文件 | 修改 |
|---|---|
| `backend/app/analysis/orchestrator.py` | aggregation 写盘循环中调用自检；新增模块级 `_finding_slice_sink_mismatch()`；manifest 加 `finding_slice_mismatches` |
| `backend/app/analysis/context_builder.py` | 已存在 `_anchor_projection`（复用，无需改） |

## 4. 测试计划

| 测试 | 断言 |
|---|---|
| **`test_finding_slice_sink_match_passes`** | finding.sinks == slice.sinks → 返回 [] |
| **`test_finding_slice_sink_mismatch_reported`** | finding.sinks(221) vs slice.sinks(124) → 返回 mismatch 详情（code/critical/finding_sinks/slice_sinks/slice_id） |
| **`test_slice_missing_reported_noncritical`** | context_slice=None → SLICE_UNAVAILABLE non-critical |
| **`test_integration_aggregation_marks_mismatch`**（orchestrator 级，可选） | 写盘后 finding.blocking_gaps 含 FINDING_SLICE_SINK_MISMATCH + manifest.finding_slice_mismatches ≥ 1 |

## 5. 风险与边界

| 项 | 说明 |
|---|---|
| **sinks 空数组** | finding.sinks 为空且 slice.sinks 为空 → 相等，不误报 |
| **多 sink 顺序差异** | 投影后排序比较，忽略顺序 |
| **slize 为 None（旧 run/未切片候选）** | 非 critical 记录，不误伤 |
| **性能** | 每 finding 一次投影 + 排序，O(k log k)，k=sink 数（个位数），可忽略 |
| **历史 run** | 不回溯（与 slice_id 修复一致），只对新 run 生效 |

## 6. 验证方式

1. 单元测试 3-4 个全绿
2. 用 run 110600Z 的 evidence（mismatch 现场：finding 221 vs slice 124）构造 fixture 跑自检函数 → 确认报出
3. `scripts/check-all.sh` 全量 + 规则契约 29

---

## 7. 形态 B：存量回溯自检（CLI）

### 7.1 入口

`scripts/check-finding-slice-consistency.py <run_id> [--fix] [--export CSV_PATH]`

- `<run_id>`：必填，目标任务 ID（可接受 `all` 扫描全部 run）
- `--fix`：可选，给 mismatch finding 的 blocking_gaps 补 `FINDING_SLICE_SINK_MISMATCH` 标记 + manifest 记 `finding_slice_mismatches`
- `--export`：可选，导出明细 CSV（finding_id, mismatch 类别, finding_sinks, slice_sinks, slice_id）

### 7.2 行为（默认只读，不修改任何文件）

| 模式 | 读 | 写 |
|---|---|---|
| 默认 | findings/*.json + reports/evidence/*.json + manifest | 无 |
| `--fix` | 同上 | finding JSON blocking_gaps 追加 + manifest 计数 |

### 7.3 输出

```
run 20260809T110600Z_1c55d3fb9f95_98fbe158
  findings: 260 | mismatch: 109 | slice_missing: 98 | consistent: 53
  mismatch 明细（--export 时写 CSV）：
    <finding_id> | FINDING_SLICE_SINK_MISMATCH | finding=[PreferenceUtil.java:221] | slice=[PreferenceUtil.java:124]
```

### 7.4 实现

- 复用纯函数 `_finding_slice_sink_mismatch()`（形态 A 同一实现）
- 纯 Python + stdlib（json/glob/argparse），无第三方依赖
- `--fix` 的写操作保持幂等（已有标记不重复追加）

---

**方案已确认（含形态 B），按此实施。**
