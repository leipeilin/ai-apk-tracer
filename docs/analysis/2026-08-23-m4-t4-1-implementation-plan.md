# 任务实施方案：M4-T4.1（golden 探索轨命中标注）

> **任务编号**：M4-T4.1
> **日期**：2026-08-23
> **依据**：实施计划 §3.5 T4.1（"golden 扩展：正样本 8 项 + 负样本 + 探索轨命中标注"）；M2 验收记录 §2.3（覆盖映射表因 validated=0 无法执行——标注结构先建，评估随 T4.2 接通）
> **状态**：起草
> **前置**：golden v2 dataset 已含动态终审正负样本（manifest 实读确认）——T4.1 增量收敛为**探索轨命中标注层**，不重复建正负样本

---

## 1. 任务目标与范围

给 golden case 增加探索轨命中期望标注（`explorer_expected`）与命中判定器——使"探索候选 ↔ golden 标注"的覆盖映射（M2 验收 §2.3 的 8 项表）可机器判定，为 T4.2 批量评估提供探索轨指标的数据基础。

**范围**：
1. `backend/app/evaluation/golden.py`——`ExplorerExpectation` 模型 + `GoldenCase.explorer_expected` 可选字段 + case 级命中判定方法；
2. `evaluation/golden/v1/cases/`——8 个正样本 case 补 `explorer_expected` 标注（数据工作，标注键来自既有 sources/sinks 的 symbol）；
3. `evaluation/golden/v1/manifest.json`——dataset_version v2→v3（描述补探索轨标注说明）；
4. 测试：schema 校验/命中判定/无标注 case 的跳过语义。

**非范围**：T4.2 批量评估（runner/metrics 的探索轨指标接入——下一任务）；负样本扩充（v2 已含动态终审负样本——实读 manifest 确认）；真实 run 的探索候选数据（评估执行时才有）。

## 2. 现状锚点

- golden case 现有结构（cases/remote-aidl-unguarded.json 实读）：id/category/**label**/rule/component/entry/operation/expected/sources(**path+symbol+kind**)/sinks/tags/provenance——**sources/sinks 已有 symbol 标注**，探索轨匹配键可直接派生；
- `GoldenCase`（golden.py:90）为 StrictModel（extra=forbid）——新字段须可选（default None，向后兼容 v2 无标注 case）；
- metrics.calculate_metrics（metrics.py:35）只算规则轨二元指标——探索轨命中率由 T4.2 在此扩展；
- 正样本标注候选（从 v2 manifest 的 positive 中选）：remote-aidl-unguarded / provider-query-helper-delegation / sport-binder-unguarded-effect / router-validation-overwritten / fragment-external-class-name / extra-close-url-unregistered-dos。
- **口径修正（M3/M4 实施审查 4.1，2026-08-23）**：原方案声称 8 候选"对应 M2 验收覆盖映射表条目"不实——该清单为 golden positive 的选择（含规则轨合成样本，非全部属 M2 动态终审 8 项）；且遗漏了动态终审成立的 shop V-02（`extra-close-url-unregistered-dos`）——已补标注（hit），标注集为 **6 hit + 3 conditional = 9**。manifest 描述与测试集合同步修正。

## 3. 详细实现方案

### 3.1 ExplorerExpectation 模型（golden.py）

```python
class ExplorerExpectation(StrictModel):
    """探索轨命中期望（T4.1——M2 验收 §2.3 覆盖映射的机器判定基础）。"""
    expectation: Literal["hit", "miss", "conditional"]
    source_match_keys: list[str] = Field(min_length=1, max_length=8,
        description="探索候选 source 表达式/组件名的匹配键（任一子串命中即 source 命中）")
    sink_match_keys: list[str] = Field(min_length=1, max_length=8,
        description="探索候选 sink 方法/操作名的匹配键（任一子串命中即 sink 命中）")
    notes: str | None = Field(default=None, description="标注依据（审计引用）")

    def matches(self, candidate_source: str, candidate_sink: str) -> bool:
        """命中判定：source 与 sink 各含任一匹配键（子串、大小写不敏感）。"""
        src, snk = candidate_source.lower(), candidate_sink.lower()
        return (any(k.lower() in src for k in self.source_match_keys)
                and any(k.lower() in snk for k in self.sink_match_keys))
```

`GoldenCase` 加可选字段 `explorer_expected: ExplorerExpectation | None = None`。

### 3.2 命中判定入口（golden.py）

```python
def explorer_hit(case: GoldenCase, candidate: Mapping) -> bool:
    """探索候选是否命中该 case 的探索标注。

    candidate 取探索候选的 chain_proposal（source/sink 文本字段）；
    case 无 explorer_expected 或 expectation != "hit" → False（miss/conditional
    不进二元命中——与规则轨 label 语义对齐）。
    """
```

### 3.3 数据标注（8 个正样本 case）

每个 case 的 `explorer_expected`：`expectation="hit"`；`source_match_keys`/`sink_match_keys` 从该 case 既有 sources/sinks 的 `symbol` 派生（取类名/方法名核心词——如 remote-aidl-unguarded：source ["SportApiStub", "onTransact"]、sink ["startSport"]）；`notes` 引用标注依据（对应 M2 验收映射表条目/动态终审记录）。

### 3.4 文件变更清单

| 文件 | 变更 |
|---|---|
| `backend/app/evaluation/golden.py` | ExplorerExpectation + GoldenCase.explorer_expected + explorer_hit |
| `evaluation/golden/v1/cases/<8 个>.json` | 补 explorer_expected 标注 |
| `evaluation/golden/v1/manifest.json` | dataset_version v3 + 描述补探索轨标注层说明 |
| `backend/tests/test_evaluation_golden.py`（或既有文件） | schema 兼容/命中判定/miss 语义测试 |

### 3.5 风险与回退

| 风险 | 对策 | 回退 |
|---|---|---|
| 标注键与真实探索候选表述不匹配（模型 source 写法不同） | 子串+大小写不敏感宽松匹配；T4.2 实测后再校准键 | 键调整（数据级） |
| StrictModel extra=forbid 破坏 v2 case 兼容 | 字段可选 default None——旧 case 不动即兼容（测试覆盖） | — |
| conditional 语义混入二元指标 | explorer_hit 只认 "hit"（与规则轨 label 语义对齐——文档写明） | — |

## 4. 与大纲一致性

T4.1 原文"正样本 8 项 + 负样本 + 探索轨命中标注"——正负样本主体已在 golden v2 落地（manifest 实读确认，含动态终审 12 项与 shop/OwnSystem 负样本）；本任务完成剩余的**探索轨命中标注**（8 正样本 + 判定器），负样本无探索轨标注需求（miss 语义由无标注/expectation 表达）——大纲条目收敛完成，偏差（正负样本前置已做）在验收记录说明。
