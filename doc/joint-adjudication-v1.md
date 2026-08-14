# 联合裁决模型实现方案（v1）

> 方案日期：2026-08-09
> 状态：待实施
> 版本影响：l2-review 提示词（3.0.3→3.0.4）、decision.py、review_state.py、测试

## 1. 背景与动机

当前系统 AI 裁决 0% 被采信：139 个 ai_completed 候选全部 `evidence_decision=unresolved`。量化根因：

```
138/139  confidence_tier = low   ← 提示词规定"存在关键 blocking_gaps 时不得给 high"
139/139  exfiltration_channel = unverified  ← 红线 23 静态不可自证
136/139  AI 有明确方向判定（flaw=True/False）但全被压成 unresolved
  6 个   AI 强判（flaw+entry+prop+sink 全真）只差 exfil
```

用户观点（已对齐）：**不是让 AI 突破机制，而是"机制负责排除不可能（否决权）、AI 负责在可能空间内裁决（确认权）、两者不冲突即采信"**。这是分权制衡模型，替代当前"确定性独裁"（AI 判定必须确定性背书才采信 → 0% 采纳）。

可行性验证（run 194354Z）：136 个 AI 有判定的候选**零冲突**（guard_blocked=0、闭链反判=0）—— AI 在机制内是乖的，机制也筛得干净，不冲突时信任 AI 是安全的。

## 2. 核心模型：联合裁决

```
AI 判定（flaw_holds / exploitability / verdict / confidence_tier）
   ├── 与机制冲突（guard 阻断 / 闭链反判 / 红线命中 / validation_failure）
   │      → 机制否决 → unresolved / blocked / deterministically_refuted（现有逻辑不变）
   ├── 不冲突 + AI 强判（四要素≥3 真 + confidence=high）
   │      → supported / ai_false_positive（直接落地）
   ├── 不冲突 + AI 中判（flaw=True 但传播未证，或 flaw=False 无确定性反证）
   │      → ai_likely_supported / ai_likely_false_positive（倾向性结论，人工确认）
   └── 不冲突 + 证据不足（verdict=unresolved 且无方向）
          → unresolved（保持）
```

**关键原则**：
1. 机制保留否决权（冲突 → 否决 AI），防假阴性铁律不破坏
2. AI 判定按强度分档采信，不是全信也不是全不信
3. confidence_tier 语义改为"AI 对判定的信心"，而非"证据完备度"

## 3. 冲突矩阵（机制否决条件，不冲突 = AI 判定可被采信）

| 冲突条件 | AI 判定 | 结果 |
|---|---|---|
| guard_blocked（guard 阻断） | 任意 | blocked（机制否决，现有） |
| validation_failure（AI 合约失败） | 任意 | unresolved（现有） |
| deterministic_refutation_basis 非空 | 任意 | deterministically_refuted / ai_false_positive（现有） |
| deterministic_chain_verified=True 且 AI refutes | refutes | unresolved（机制否决 AI 否定） |
| 红线命中（EXFILTRATION_CHANNEL_UNVERIFIED 时 AI 仍 refutes） | refutes | 不得 refutes（现有红线 23） |

**不冲突的采信分档**（新增）：

| 分档 | 条件 | 结果 |
|---|---|---|
| **AI 强成立** | flaw=True + entry=True + prop=True + sink=True + confidence=high + 数据流已证 | supported |
| **AI 中成立** | flaw=True + entry=True（传播未证） | ai_likely_supported |
| **AI 否定** | flaw=False（确定性反证或语义否定，无冲突） | ai_likely_false_positive |
| **AI 强否定** | flaw=False + deterministic_basis 非空 | ai_false_positive（现有） |

## 4. 分层改动明细

### 4.1 提示词 l2-review 3.0.4

**目标**：解锁 confidence 表达 + 明确"机制内裁决"语义。

- **confidence_tier 语义重定义**：
  - 原：`存在关键 blocking_gaps 时不得给 high`
  - 新：confidence_tier 表示**AI 对自身裁决方向的信心**，不是证据完备度。缺陷成立判定有真实调用点支撑时允许给 high/medium；EXFILTRATION_CHANNEL_UNVERIFIED 等"可利用"要素缺口**只降级 confidence 到 medium，不禁止 high**（除非存在确定性冲突）。
- **verdict 规则补充**：
  - 机制（guard 阻断/确定性反证/闭链）已排除的候选，AI 判定不得与之冲突（保持现有红线）。
  - 机制未排除时，按四要素强度裁决：flaw+entry+prop+sink 全真可 supports_candidate（不再因 exfil unverified 强制 unresolved，改为 confidence 降级 + blocking_gap 如实披露）。
- **exfiltration_channel 规则保留**：红线 23 不变（静态不可自证时输出 unverified + gap），但**不再作为 verdict 的唯一闸门**——unverified 时 verdict 可为 supports_candidate（低置信）或 unresolved，由四要素其余项强度决定。

### 4.2 decision.py（联合裁决核心）

在 `decide_candidate` 的 else 分支（当前全 unresolved）前插入**联合裁决逻辑**：

```
新增常量：
_AI_LIKELY_SUPPORTED = "ai_likely_supported"
_AI_LIKELY_FALSE_POSITIVE = "ai_likely_false_positive"

新增判定（在现有 conflict 分支之后、else unresolved 之前）：
  elif _ai_strong_support(candidate, analysis):
      evidence_decision = "supported"        # AI 强成立 + 数据流已证
  elif _ai_likely_supported(candidate, analysis):
      evidence_decision = "ai_likely_supported"  # flaw=True + entry=True
  elif _ai_likely_false_positive(candidate, analysis):
      evidence_decision = "ai_likely_false_positive"  # flaw=False 无冲突
```

辅助判定函数（纯函数，可单测）：
- `_ai_strong_support`: verdict in SUPPORTS + flaw=True + entry/prop/sink 全真 + confidence=high + dataflow in _PROVEN_DATAFLOW
- `_ai_likely_supported`: verdict in SUPPORTS（或 promotion）+ flaw=True + entry=True（prop 可 False）
- `_ai_likely_false_positive`: verdict in REFUTES + flaw=False + 无 deterministic_basis + 无冲突（guard_blocked 排除）

**注意**：`_ai_contract_failure` 的 `_applicable_critical_gap` 当前会在 critical gap 存在时返回 CRITICAL_BLOCKING_OR_COVERAGE_GAP → validation_failure → unresolved。**需要放宽**：仅当 gap 属于"确定性冲突类"（如 guard/闭链冲突）才判 failure；EXFILTRATION_CHANNEL_UNVERIFIED/DATAFLOW_NOT_PROVEN 这类"证据不足类" gap 不再触发 validation_failure（改为影响 confidence 分档）。

### 4.3 review_state.py

`derive_review_state` 增加两个新状态映射：
- `ai_likely_supported` → status=`pending_manual`（reason=`ai_likely_supported_needs_confirmation`），进入人工快速确认队列
- `ai_likely_false_positive` → status=`pending_manual`（reason=`ai_likely_false_positive_needs_confirmation`）
- 聚合层 `aggregate_review_states` 不变（pending_manual 保守合并）

### 4.4 测试

新增 `tests/test_joint_adjudication.py`：
1. AI 强成立 → supported（flaw+entry+prop+sink+conf=high+proven）
2. AI 中成立（flaw=True entry=True prop=False）→ ai_likely_supported
3. AI 否定（flaw=False 无冲突）→ ai_likely_false_positive
4. 冲突（guard_blocked + AI supports）→ blocked
5. 冲突（闭链 + AI refutes）→ unresolved
6. exfil unverified 不再强制 unresolved（flaw+entry+prop 全真 → ai_likely_supported）
7. confidence 解锁：存在 exfil gap 时仍允许 high（提示词测试）

## 5. 预期效果（run 194354Z 模拟）

```
当前：139 个 unresolved → 人工全看
实施后：
  32 个  ai_likely_false_positive（AI 否定，人工快速过）
  98+6 个 ai_likely_supported（AI 判成立，人工确认传播/外溢）
  3 个   unresolved（证据不足，保留）
人工从"139 从头验证"→"104 确认 AI 结论 + 32 确认否定"，工作量降一个量级
```

## 6. 风险与边界

1. **ai_likely_supported 不改变 severity**：倾向成立 ≠ 确认漏洞，severity 仍由 determine_severity 定级（pending），报告明确标注"AI 倾向"。
2. **AI 否定误采风险**：flaw=False 是 AI 语义判断，无确定性背书时仅给 ai_likely_false_positive（保留人工确认），不直接删除候选——防假阴性铁律保留。
3. **confidence 解锁后 AI 可能虚高**：用 confidence_rationale 字段约束（要求写理由），且 high 仅当四要素强判时生效。
4. **exfil 红线不破**：红线 23 仅放宽"verdict 闸门"，exfiltration_channel=unverified + gap 生成逻辑不变，报告仍如实披露。

## 7. 验证方法

1. `check-all.sh` 全量回归（新增联合裁决测试）
2. 用 run 194354Z 的 candidates 直接跑 `DecisionEngine().apply()` 模拟，对比实施前后 evidence_decision 分布
3. 真实 API 端到端：3.0.4 提示词 + 生产路径重建请求，确认 confidence 解锁 + verdict 不再被 exfil 单点闸死
4. 重跑后对比：ai_likely_* 占比、人工复核队列缩小幅度

## 8. 实施顺序

1. 提示词 3.0.4（解锁 confidence + verdict 规则）→ 注册 + 切换 + 测试
2. decision.py 联合裁决（新分档 + _applicable_critical_gap 放宽）→ 单测
3. review_state.py 新状态映射 → 单测
4. 全量回归 + 模拟验证
