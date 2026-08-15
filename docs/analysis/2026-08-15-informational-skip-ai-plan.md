# 实现方案：L1 informational 候选默认不进 AI（建议 2 落地）

> **日期**：2026-08-15
> **依据**：`2026-08-15-dynamic-receiver-282-candidates-analysis.md` §4 建议 2 +
> informational 语义实证评估（今日完成）+ 用户补充观察（P2-6 误报实例）
> **状态**：✅ **已实施完成**（提交 698104b，2026-08-15 23:08 确认）
> **遗留（非缺陷）**：§3.3 review_status 联动为可选二期，方案明确范围外；exposure_only 保留面
> 若后续实证"送 AI 零增量"再评估收紧——新决策，不在本方案范围。

---

## 1. 结论（语义评估实证）

L1/informational 候选 = **"暴露事实或无法判定"，不承载"漏洞成立"判定**，三组证据：

| 证据 | 数据 |
|---|---|
| AI 在 informational 上零增量 | 3 run 共 34 条 ai_completed informational：verdict 全为 potential_chain/exposure_only/insufficient，**0 supported、flaw_holds 全 None** |
| 真实漏洞从不落在 informational | 110600Z 两份报告（ACTIVITY_INTENT_TO_SENSITIVE_SINK）全部 L2 + ai_likely_supported；125744Z 人工增强报告明说"产物全部 L2 severity=pending" |
| 人工复核无 informational 反例 | 588 条 informational **0 confirmed / 0 false_positive** |

**用户补充观察（采纳）**：P2-6 规则 `WbShareResultActivity`（动态验证确认误报、fixed=True）AI 判 supports_candidate + flaw=True——该误报发生在 **L2 规则**，恰好证明：
1. informational 不进 AI 安全（误报不在 informational）；
2. **L2 的 AI 判定必须靠 P1-5 交叉验证兜底**（该候选凭 fixed_local_target 反证闭环为误报）；
3. **informational 治理与 P1-5 是互补关系，不是替代**——本方案只动 L1 预算，L2 的 AI 判定与 P1-5 机制完全不动。

## 2. 数字口径确认（用户要求复核）

**110600Z "informational 114 vs L1 113"差 1 的根因**：`IMPLICIT_BROADCAST_SENSITIVE_DATA` 1 条，`evidence_level=L2` 但 `severity=informational`（`evidence_decision=deterministically_refuted`）。

> **推论（实现判据）**：severity=informational 与 evidence_level=L1 **不完全等价**。若按 severity 判"不进 AI"，会误伤这条 L2 候选（它需要保留 AI 通道以便 P1-5 交叉验证闭环）。**判据必须用 `evidence_level == "L1"`，不用 severity**。

其余口径（findings 库）：
- exposure_only：46（110600Z）/ 45（124147Z）/ 90（125744Z），84/90 为 high 置信
- 规则分布：ACTIVITY_EXPORTED 28-35、SERVICE/RECEIVER/DYNAMIC_RECEIVER_EXPORTED、PROVIDER_READ_WRITE_PERMISSION_MISSING、CLEARTEXT
- unresolved：67/69/269 条（AI 不可判）

## 3. 实现设计

### 3.1 配置开关（`config/default.yaml` + `backend/app/config.py`）

```yaml
l1_skip_ai: true   # 已确认：默认 true 直接生效；false 完全回退旧行为
```

### 3.2 funnel 判定逻辑（`backend/app/analysis/candidate_funnel.py`）

改动点：L1 代表候选进入预算（`l1_representatives`）前加过滤。

```
现状（process()）：
  if representative.get("evidence_level") == "L1":
      l1_representatives.append(representative_index)   # 全部进预算排序

改后：
  if representative.get("evidence_level") == "L1":
      if self.l1_skip_ai and _l1_skip_ai(representative):
          continue          # 不进预算：保留候选+gap+人工队列，不送 AI
      l1_representatives.append(representative_index)
```

**例外判据（已确认含其他规则族）**——`_l1_skip_ai(candidate)` 返回 True 表示"跳过 AI"：

```python
def _l1_skip_ai(candidate) -> bool:
    # 只作用于 L1；L2（含 severity=informational 形态）完全不受影响
    if candidate.get("evidence_level") != "L1":
        return False
    # 例外①：R-1 receiver 确定性 clean 可判定面（exported + 无三大 gap）
    if candidate.get("receiver_flag_tier") == "confirmed_exported_clean":
        return False
    # 例外②：其他规则族的 L1 确定性暴露面——funnel 层已确认"暴露事实"
    # （无 critical gap 的 L1：exposure_only / high_risk_uncertain），AI 有可判定输入
    if candidate.get("funnel_disposition") in {"exposure_only", "high_risk_uncertain"}:
        return False
    # 其余 L1（coverage_insufficient 有 gap / deterministically_refuted 已被反驳）→ 不进 AI
    return True
```

**判据依据（125744Z 实测）**：L1 375 条 = exposure_only 83（ACTIVITY 35 / SERVICE 21 / RECEIVER 17 / PROVIDER 8 / DYNAMIC_RECEIVER 2，全 informational）+ high_risk_uncertain 1 + coverage_insufficient 279（DYNAMIC_RECEIVER gap 形态，282 报告核心问题）+ deterministically_refuted 12。

**保留面的预算占用（重放 3 run 实测，2026-08-15 定稿修正）**：例外保留 ≠ 占用预算。`_pipeline_requires_ai` 对 L1 `exposure_only` 返回 False（v2026-08-09 既有逻辑：纯 manifest 事实、无代码上下文，AI 无内容可分析不送），因此：
- exposure_only 83 条**从不进 AI 预算**（ai_required 全 False），例外② 的语义 = "保持不进 AI + 候选/gap/人工队列保留"，不存在"送 AI 零增量需收紧"的问题；
- 真正占用预算的只有 high_risk_uncertain（125744Z 1 条）与新 run 中 receiver clean 面（例外①，R-1 分级生效后 `confirmed_exported_clean` 有代码上下文才送 AI）。
- 重放：125744Z 挡掉 291 / 保留 84 / 进预算 1；110600Z 挡掉 85 / 保留 42 / 进预算 0；124147Z 挡掉 86 / 保留 42 / 进预算 0。与方案预测一致（291/84）。

### 3.3 复核状态联动（可选，单独实施）

不进 AI 的 L1 候选 `review_status=pending_ai`（"待 AI 复核"）语义失真——实际不送 AI。可选联动：这类候选的 review 状态改为反映"规则确定性暴露事实，待人工复核"。**风险**：review_status 由 decision 层聚合决定，改动面大；本方案标记为可选二期，核心先做预算侧。

### 3.4 与既有机制的关系（明确不动的部分）

| 机制 | 关系 |
|---|---|
| R-1 flag 分级 | **保留并配合**：clean 面是"不进 AI"的唯一例外 |
| R-2 预算排序 | **保留**：L1 预算仍按 tier 排序，只是候选池缩为"clean 面"（+ 未来分级面） |
| R-3 去重聚合 | 不动 |
| P1-5 交叉验证 | **完全不动**：L2 的 AI 判定 + P1-5 反证闭环照旧（WbShareResultActivity 类误报仍靠它兜底）——互补非替代 |
| 规则侧 detector.py | 不动（分级已在 R-1 落地） |

## 4. 预期收益

- 全规则族 L1 候选不再占用 AI 预算（基线 run 该形态 76 条同受影响）
- 预算集中到：L2 闭合链（P1-5 需要）+ L1 clean 可判定面（AI 有输入）
- 人工队列不丢（exposure_only 保留）

## 5. 测试计划

新增（`backend/tests/test_pipeline_v2_funnel.py`）：
1. `test_l1_skip_ai_default`：开关默认 true 时，L1 无分级候选不进 l1_representatives（预算 0 占用），候选仍在输出、analysis_status=rule_only、不产生 ai_skipped gap
2. `test_l1_skip_ai_clean_exception`：receiver_flag_tier=confirmed_exported_clean 的 L1 候选仍进预算；confirmed_exported_gap/unresolved_flag 不进
3. `test_l1_skip_ai_switch_off`：l1_skip_ai=false 时行为与现状完全一致（预算候选集不变）
4. `test_l1_skip_ai_l2_untouched`：L2 候选（含 severity=informational 的 IMPLICIT_BROADCAST_SENSITIVE_DATA 形态）不受影响，仍走 AI + P1-5
5. 回归：全量套件（当前 755 passed + 3 预存在失败不变）

## 6. 风险

| 风险 | 缓解 |
|---|---|
| L1 里未来出现值得 AI 深挖的形态被挡 | clean 例外按分级扩展；且 L1 候选/切片不删除，人工可查、可手动升级 |
| review_status=pending_ai 语义失真 | 二期联动（§3.3），明确标注不影响本次预算正确性 |
| 误伤 L2 informational（severity 判据陷阱） | 判据用 evidence_level=L1（§2 已实证 1 条 L2 informational 存在） |
| 翻默认后基线数字变化 | A/B：l1_skip_ai=false/true 各跑一次，对比 budget 使用与 findings 决策分布后定稿 |

## 7. 实施步骤

1. `config/default.yaml` + `backend/app/config.py` 加 `l1_skip_ai: true`
2. `candidate_funnel.py`：`__init__` 读开关；`process()` L1 预算前过滤 + `_l1_clean_ai_exception`
3. 新增 5 个测试（§5），跑相关测试 + 全量回归
4. 更新文档 `docs/updates/2026-08-15-informational-l1-skip-ai.md`
5. （可选二期）review_status 联动

## 8. 待用户确认

> 已确认（2026-08-15 22:51）：
> 1. `l1_skip_ai` 默认 **true**；
> 2. clean 例外**包含其他规则族的 L1 确定性暴露面**（例外②：funnel_disposition ∈ {exposure_only, high_risk_uncertain}）。
> 已按此定稿实施。

- ~~1. `l1_skip_ai` 默认 true（推荐，实证充分）还是先 false 守门 A/B 后翻？~~ → **true 定稿**
- ~~2. clean 例外暂只含 confirmed_exported_clean（receiver 规则）是否够？~~ → **扩展为通用判据（例外②）**
