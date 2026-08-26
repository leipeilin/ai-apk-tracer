# 任务实施方案：M4-T4.4（优化门槛——golden 不劣于基线才可默认开启）

> **任务编号**：M4-T4.4
> **依据**：实施计划 §3.5 T4.4（"优化门槛：golden 指标不劣于基线才可默认开启——评估流程文档"）；依赖 T4.2 ✓
> **状态**：起草

## 1. 目标与范围

建立"改动须过门槛才可默认开启"的回归纪律，两部分：

1. **门槛判定函数**（`backend/app/evaluation/gate.py`——`compare_against_baseline(current, baseline, tolerances) -> dict`）：指标对比（precision/recall/f1/explorer_hit_rate/conditional_hit_rate——None 指标跳过；劣化超容差 → gate=BLOCK，容差内 → ALLOW）；容差默认全零（不劣于基线）可显式放宽；
2. **评估流程文档**（`docs/evaluation-workflow.md`）：基线快照机制（`python -m backend.app.evaluation.runner --runs ... > evaluation/baselines/<name>.json` 提交入库）+ 门槛判定命令（`python -m backend.app.evaluation.gate --current x.json --baseline y.json`）+ 默认开启检查点（prompt/协议改动合入前必跑——EXPLORER-PROMPT-FIX/M2 收尾系列的实际流程文档化）。

**范围**：gate.py + 流程文档 + 测试。**非范围**：CI 集成（后续按需）；自动基线刷新。

## 2. 现状锚点

- T4.2 输出结构：aggregate.explorer_hit_rate/conditional_hit_rate + metrics 层（规则轨 precision/recall/f1——evaluate_results 的 metrics 键）；
- 容差语义：劣化幅度 ≤ tolerance 视为不劣于（浮点噪声防护——默认 0 严格）。

## 3. 详细方案

```python
def compare_against_baseline(current: Mapping, baseline: Mapping,
                             tolerances: Mapping[str, float] | None = None) -> dict:
    # 指标键：从 current/baseline 共有的 *_rate / precision / recall / f1 键自动发现
    # 每指标：baseline 缺 → SKIP；current 缺 → BLOCK（新增指标缺失视为劣化）
    #         current < baseline - tol → BLOCK（记 deficit）
    # gate: BLOCK 任一 → "BLOCK" else "ALLOW"
```

CLI：`--current/--baseline`（JSON 文件）+ `--tolerance f1=0.02,...` 可选。

## 4. 风险

指标键自动发现的误判（非指标键含 rate）→ 白名单前缀/显式键集；文档与实现漂移 → 文档含命令实录。
