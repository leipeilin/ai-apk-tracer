# 修复方案：三处决策层与证据矛盾

> **日期**：2026-08-14
> **前置**：三处矛盾已用真实数据逐一确认（见 §1 根因分析）
> **状态**：⏸ 待用户确认后实施

---

## 1. 根因分析（真实数据确认）

### 矛盾 ①：6×MainTabActivity 被标 `ai_likely_false_positive`

**现象**：6 个 MainTabActivity finding，AI `flaw_holds=False`、`verdict=unresolved`、`false_positive_basis=[]`，决策层标 `ai_likely_false_positive`。

**真实数据**（86311b4d 样例）：
```json
{
  "flaw_holds": false,
  "verdict": "unresolved",
  "confidence_rationale": "数据流未证明，符号解析歧义，外溢通道未验证，判定方向不确定",
  "confidence_tier": "low",
  "blocking_gaps": [DATAFLOW_NOT_PROVEN, SYMBOL_TARGET_AMBIGUOUS, EXFILTRATION_CHANNEL_UNVERIFIED]
}
```

**根因**：`_ai_likely_false_positive`（decision.py:603-614）**只看 `flaw_holds is False`**，不检查：
1. AI 是否在**证据严重不足**下否定（存在 critical blocking_gaps 时，flaw=False 是"没找到成立的证据"而非"找到不成立的证据"）
2. 是否有确定性反证背书（`false_positive_basis=[]` 证实无）

**违反铁律**："未找到证据 ≠ 证据表明不成立"。AI 在 3 个 critical gap 下说"缺陷不成立"，本质是证据不足，不是确定性否定。

### 矛盾 ②：`3fe8a217` 被标 `ai_likely_supported`

**现象**：AI `verdict=refutes_candidate` 但 `flaw_holds=True` + `propagation_proven=True`；决策层走 `_ai_likely_supported` → `ai_likely_supported`。

**真实数据**：
```json
{
  "verdict": "refutes_candidate",
  "flaw_holds": true,
  "exploitability": {propagation_proven: true, exfiltration_channel: "absent"}
}
```
skill 人工核验确认**实际是误报**（removePref key 固定常量 `back_url/back_name/backurl` 不可控）。

**根因**：**AI 自身输出自相矛盾**——verdict=refutes（想否决）但 flaw_holds=True/propagation_proven=True（成立信号）。决策层 `_REFUTATION_OUTCOMES` 分支（790 行）要求 `deterministic_basis`，AI refutes 无 basis → 被忽略 → 落到 `_ai_likely_supported`（flaw=True + entry=True）。**决策层采信了 AI 错误的那套信号（flaw=True），忽略了 AI 正确的否定意图（verdict=refutes）**。

### 矛盾 ③：`89da4b67` 被标 `supported`

**现象**：`deterministic_chain_verified=True` + `dataflow_status=intraprocedural` → 决策层 820-825 行 `supported`。AI summary 明说"调用点未提供……攻击者无法直接触发"，skill 源码+索引双确认**全库无调用点**（红线 13 死代码）。

**真实数据**：
```json
{
  "deterministic_chain_verified": true,
  "dataflow_status": "intraprocedural",
  "chain_id": null, "entry_method_id": null, "path_model": null
}
```

**根因**：规则层的 `deterministic_chain_verified=True` 是**方法内传播证明**（intraprocedural），**不验证调用点存在性**——死代码方法也能 chain_verified=True。决策层 820-825 行把"方法内链已验证"当"漏洞成立"，漏掉"该方法根本没被调用"这个前提。

---

## 2. 修复方案（三处独立，可分开实施）

### 修复 A：`_ai_likely_false_positive` 加"证据充分性"门控（矛盾 ①）

**文件**：`backend/app/findings/decision.py` `_ai_likely_false_positive`

**改法**：AI 否定信号只有在**没有 critical blocking_gaps** 时才可信——证据不足下的 flaw=False 不是确定性否定，降为 unresolved。

```python
def _ai_likely_false_positive(candidate, analysis) -> bool:
    """AI 否定：flaw=False 且无 critical gap 才可信。

    v2026-08-14 修复：AI 在证据严重不足（critical gap：DATAFLOW_NOT_PROVEN/
    SYMBOL_TARGET_AMBIGUOUS/EXFILTRATION_CHANNEL_UNVERIFIED）下判 flaw=False，
    本质是"没找到成立的证据"而非"找到不成立的证据"（违反"未找到证据≠不成立"）。
    只有证据充分（无 critical gap）时的 flaw=False 才作为否定信号采信。
    """
    if analysis.get("flaw_holds") is not False:
        return False
    for source in (candidate, analysis):
        for field in ("blocking_gaps", "ai_blocking_gaps"):
            for gap in source.get(field, []) or []:
                if isinstance(gap, Mapping) and gap.get("critical", True) is True:
                    return False  # 证据不足下的否定不可信
    return True
```

**效果**：6×MainTabActivity 的 flaw=False 因有 3 个 critical gap → `_ai_likely_false_positive` 返回 False → 落到 `unresolved`（符合"未找到证据≠不成立"）。

### 修复 B：AI verdict=refutes 且无 basis 时，不采信 AI 的 flaw=True（矛盾 ②）

**文件**：`backend/app/findings/decision.py` 主逻辑（790-806 行附近）

**改法**：当 AI `verdict ∈ _REFUTATION_OUTCOMES`（AI 明确想否决）但无 deterministic_basis 时，**AI 内部信号矛盾**（verdict=refutes vs flaw=True）——此时不采信任一方向，落 `unresolved` + 记录矛盾 gap，避免采信错误信号。

```python
# 在 verdict 判定后、_ai_likely_supported 之前插入：
elif (
    verdict in _REFUTATION_OUTCOMES
    and analysis.get("flaw_holds") is True
):
    # v2026-08-14 修复：AI 输出自相矛盾（verdict=refutes 但 flaw_holds=True）。
    # 无确定性 basis 时两套信号冲突，不采信任一方向 → unresolved + 矛盾 gap，
    # 避免采信 AI 错误的成立信号（3fe8a217 案例）。
    evidence_decision = "unresolved"
    false_positive_basis = []
    candidate.setdefault("ai_blocking_gaps", []).append({
        "code": "AI_VERDICT_FLAW_CONFLICT",
        "critical": True,
        "message": "AI verdict=refutes 但 flaw_holds=True，输出自相矛盾，不采信",
    })
```

**效果**：3fe8a217 的 verdict=refutes + flaw=True 矛盾 → unresolved + 矛盾 gap（人工可见），不再被错误标 ai_likely_supported。

### 修复 C：`deterministic_chain_verified` 决策加"调用点存在性"前提（矛盾 ③）

**文件**：`backend/app/findings/decision.py` 820-825 行

**改法**：`supported` 分支增加调用点存在性校验——`entry_method_id` 为空或方法无 callers 时，链已验证不代表漏洞成立（死代码）。

```python
elif (
    candidate.get("deterministic_chain_verified") is True
    and candidate.get("dataflow_status") in _PROVEN_DATAFLOW
    and candidate.get("entry_method_id")  # v2026-08-14：链已验证必须绑定真实入口
):
    evidence_decision = "supported"
    ...
```

**效果**：89da4b67 的 `entry_method_id=None`（死代码，无入口）→ 不再 `supported` → 落到 unresolved（配合 AI summary 的"无调用点"线索，人工可见）。

---

## 3. 联动影响

| 修复 | 影响面 | 风险 |
|---|---|---|
| A | 所有 flaw=False 候选：证据不足的否定不再采信 | 低——只收紧，不放松 |
| B | verdict=refutes + flaw=True 的候选（罕见） | 低——矛盾输入本就不可信 |
| C | `supported` 分支要求 entry_method_id | 需确认无合法 `supported` 候选 entry_method_id 为空（回归测试覆盖） |

## 4. 测试计划

| 测试 | 断言 |
|---|---|
| A1：flaw=False + critical gap → 非 ai_likely_false_positive | 6×MainTabActivity 场景落 unresolved |
| A2：flaw=False + 无 gap → 仍 ai_likely_false_positive | 正常否定不受影响 |
| B1：verdict=refutes + flaw=True → unresolved + AI_VERDICT_FLAW_CONFLICT gap | 3fe8a217 场景 |
| C1：chain_verified=True 但 entry_method_id=None → 非 supported | 89da4b67 场景 |
| C2：chain_verified=True + entry_method_id 存在 → 仍 supported | 正常链不受影响 |

用 run 110600Z 真实数据回归：修复后 6×MainTabActivity / 3fe8a217 / 89da4b67 的 evidence_decision 应符合预期。

## 5. 验证方式

1. 新增测试 + 既有 decision 测试全绿
2. 用 run 110600Z 的 3 个矛盾 finding 真实数据重放 DecisionEngine，确认修复后决策正确
3. `scripts/check-all.sh` 全量 + 规则契约 29

---

**请确认后实施。**
