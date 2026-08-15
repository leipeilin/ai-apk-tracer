# 动态 Receiver 修复方案（基于 282 条候选问题分析，v2 预算机制修正）

> **日期**：2026-08-15
> **依据**：`docs/analysis/2026-08-15-dynamic-receiver-282-candidates-analysis.md`（含 67% 应用自身勘误 + 预算机制复核）
> **样本**：com.mi.health（run `20260815T125744Z_2a80fc5a8735_ef5915ff`）282 条 candidate / 277 条 findings
> **性质**：可执行工程方案。所有数字均由样本实测，改动点均给出代码锚点与验收口径。

---

## 0. 方案总览（v2 修正后的问题重心）

| 阶段 | 措施 | 治理点 | 实测影响 | 风险 |
|---|---|---|---|---|
| **R-1** | 规则侧输出 `receiver_flag_tier`（flag 分级） | 85% 候选是"flag 无法排除暴露" | 分级字段下发，供预算排序 | 低 |
| **R-2** | funnel L1 预算按可判定性排序 | **20 条预算里 17 条是 gap 形态、仅 3 条干净** | 干净暴露面优先进预算 | 中（排序规则需守门） |
| **R-3** | 修 P0-3 去重（owner/flag/action 聚合） | findings 277 条各自为代表 | 复核量下降 | 低 |
| **R-4** | 注册点 owner/业务模块分组展示 | 应用自身 190 条（67%）与 SDK 混排 | 前端可读性 | 低 |

> ⚠️ **v1→v2 关键修正**：原方案假设"275 条白烧 AI 预算"，复核实为**预算机制已在工作**（`max_l1_candidates_per_run=20`，255 条 deferred）。因此**不再需要"降量"**（预算已降），核心变为 **"把 20 条预算花在刀刃上"**——R-2 是主修复，R-1 为其提供分级输入。

---

## 1. R-1：规则侧 flag 分级（输入侧）

### 现状
`rules/shared/receiver_registration.py` 已算出 `flag_status`（exported/legacy_unspecified/unknown/not_exported/local），`_dynamic_receiver_exposures`（detector.py:1896）已带出 `status`，但**候选未结构化下发分级字段**——funnel 无法据此排序。

### 改法
在 `_dynamic_receiver_binding_candidates`（detector.py，`_dynamic_receiver_exposures` 消费点）组装候选时新增：

```python
# R-1（2026-08-15）：flag 分级——exported 是"确认暴露"（AI 可判定），
# legacy_unspecified/unknown 是"无法排除暴露"（AI 判定输入不足）。
# 供 funnel L1 预算按可判定性排序（R-2）。
flag_status = exposure.get("status")
candidate["receiver_flag_tier"] = (
    "confirmed_exported" if flag_status == "exported" else
    "unresolved_flag"  # legacy_unspecified / unknown
)
```

- **字段**：`receiver_flag_tier` ∈ {`confirmed_exported`, `unresolved_flag`}（两值，避免过度细分）
- 同时把 `flag_status` 原值带上（诊断用）
- 该字段进 `_candidate_summary` 白名单（context_builder）与 `deterministic_facts`（供 AI 区分"确认暴露"与"无法排除"）

### 验收
- 单测：`exported` → `confirmed_exported`；`unknown`/`legacy_unspecified` → `unresolved_flag`；`not_exported`/`local` 不产出（已 reportable=False）
- 样本复跑：282 条候选带 `receiver_flag_tier`，`confirmed_exported` 数量 ≈ 43（与 flag 分布一致）

---

## 2. R-2：funnel L1 预算按可判定性排序（主修复）

### 现状
`candidate_funnel.py:515-521`：`l1_representatives.sort(key=candidate_risk_score)`——只按 `risk_score`/`review_priority` 排序，DYNAMIC_RECEIVER 候选未设这两个字段 → **排序退化为文件遍历序** → 20 条预算里 17 条 gap 形态、仅 3 条干净。

### 改法
排序键加入可判定性权重（`candidate_funnel.py:512`）：

```python
def _l1_ai_sort_key(candidate):
    # R-2（2026-08-15）：预算优先"可判定的干净暴露面"——
    # confirmed_exported（AI 可判定）> unresolved_flag（AI 输入不足）> 默认。
    tier = candidate.get("receiver_flag_tier")
    tier_priority = 3 if tier == "confirmed_exported" else 2 if tier == "unresolved_flag" else 1
    # 无三大 gap（flag/target/action 未解析）的干净形态再优先
    gap_codes = {str(g.get("code")) for g in candidate.get("blocking_gaps") or [] if isinstance(g, dict)}
    clean = int(not (gap_codes & {"RECEIVER_FLAG_UNKNOWN", "RECEIVER_TARGET_UNRESOLVED", "RECEIVER_ACTION_UNRESOLVED"}))
    return (tier_priority, clean, candidate_risk_score(candidate))
```

- 排序：`confirmed_exported + clean` > `confirmed_exported` > `unresolved_flag + clean` > 默认
- **全局适用**（非 DYNAMIC_RECEIVER 专属）：无 `receiver_flag_tier` 的候选 tier_priority=1，排序靠后但不会被挤出（预算内按序）

### 验收（双口径）
- **口径 A（R-2 关闭）**：行为不变（现排序）——回归测试保障
- **口径 B（R-2 开启）**：样本复跑，20 条预算中干净形态占比从 3/20 提升至 ≥15/20；`confirmed_exported` 全部进预算（43 条若超预算，按 clean 优先）
- 新增单测：构造 mixed tier 候选，断言排序键优先级正确

---

## 3. R-3：修 P0-3 去重（owner/flag/action 聚合）

### 现状
`build_candidate_identity`（candidate_funnel.py:564）的 `chain_key` 含注册点行号/调用差异 → 该规则 277 条 findings 各自为代表，未合并。

### 改法
`chain_key` 对该规则剔除注册点差异，保留语义要素：`flag_tier + owner（注册点包前缀）+ action`（`chain_key` 构造处，candidate_funnel.py 附近 `_chain_key_parts`）：

```python
# R-3（2026-08-15）：DYNAMIC_RECEIVER 按 flag 分级 + owner + action 聚合，
# 剔除注册点行号/调用点差异——同形态（同 owner 同 flag 同 action）合并为组。
if candidate.get("flow_kind") == "receiver_exposure":
    parts = [
        candidate.get("receiver_flag_tier") or "tier_unknown",
        _registration_owner(candidate),   # com/xiaomi/fitness 等包前缀
        _sorted_actions(candidate),
    ]
```

### 验收
- 样本复跑：该规则 findings 从 277 组 → 数十组（预计 30-60），复核量下降
- **硬约束**：`confirmed_exported` 与 `unresolved_flag` 不合并（跨 tier 保持独立组）
- 回归：既有去重测试（test_pipeline_v2_funnel）全过

---

## 4. R-4：注册点 owner/业务模块分组展示（前端，可选）

### 现状
282 条候选平铺列表，应用自身（fitness 190 条）与 SDK 混排。

### 改法
`FindingsPanel` 按 `receiver_flag_tier` + 注册点 owner 分组（`flow_kind=receiver_exposure` 专属视图）：

- 组头：`confirmed_exported · 应用自身（fitness）` / `unresolved_flag · SDK（autonavi）` 等
- `confirmed_exported` 组置顶（AI 已判定的真实暴露面优先展示）
- 该字段经 `_candidate_summary` 白名单下发（与 R-1 共用）

### 验收
- 前端 tsc 通过；人工浏览：fitness 模块 confirmed_exported 组首屏可见

---

## 5. 落地顺序与工程约定

```
R-1 规则侧 flag 分级         ← 输入侧，独立
  └─ R-2 预算按可判定性排序   ← 依赖 R-1 字段，主修复
R-3 去重聚合                 ← 可与 R-1 并行
R-4 前端分组展示             ← 依赖 R-1 字段，最后
```

**必须遵守**：
- 每次改动新建 `docs/updates/YYYY-MM-DD-<英文描述>.md` 单独文件
- R-1 新字段 `receiver_flag_tier` 进 `_candidate_summary` 白名单 + `deterministic_facts`（AI 可见）
- 本地统一校验 `scripts/check-backend.sh`；前端 `npx tsc -b --noEmit`
- 涉及同一语义的常量/正则单一来源（flag 分级枚举如复用，标注手动同步点）

---

## 6. 验收指标（以 com.mi.health 复算）

| 指标 | 现状 | 目标（R-1+R-2+R-3 后） |
|---|---|---|
| 20 条预算中干净形态 | 3/20 | ≥15/20 |
| `confirmed_exported` 进预算 | 部分（未按 flag 优先） | 全部（预算内） |
| 该规则 findings 组数 | 277 | 30-60 组 |
| AI unresolved 占比（该规则） | 269/277 | 预算内干净形态判定质量提升（目标以 R-2 复跑实测） |
| 前端分组 | 平铺 | confirmed_exported 组置顶可见 |

> ⚠️ **守门**：R-2 改变预算选择策略，**默认关闭**（配置开关 `funnel.l1_priority_clean`，默认 false），先复跑对比口径 A/B，确认干净形态判定质量后翻默认——与 §5 守门同流程。
