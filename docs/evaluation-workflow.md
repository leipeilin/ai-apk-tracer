# 评估流程与优化门槛（M4-T4.4）

> 适用：AI 输出质量（prompt/协议/生成方式）改动的合入纪律——**golden 指标不劣于基线才可默认开启**。

## 1. 基线快照

```bash
# 探索轨（run 产物评估——M4-T4.2）
backend/.venv/bin/python -m backend.app.evaluation.runner \
    --runs <run_id> > evaluation/baselines/<name>.json

# 规则轨（离线 golden——results 文件为 case_id→result 映射 JSON）
backend/.venv/bin/python -m backend.app.evaluation.runner \
    --results evaluation/results/<name>.json > evaluation/baselines/<name>.json
```

- 快照提交入库（`evaluation/baselines/`——首次创建目录后加入版本控制）；
- **新增指标时必须先刷新基线再启用门槛**（gate 对 baseline 缺失的指标 SKIP——守卫回归不守卫演进）。

## 2. 门槛判定（合入前必跑）

```bash
backend/.venv/bin/python -m backend.app.evaluation.gate \
    --current evaluation/baselines/<candidate>.json \
    --baseline evaluation/baselines/<baseline>.json \
    --tolerance f1=0.02   # 可选容差（默认 0 严格）
echo $?   # 0 = ALLOW（可默认开启）；1 = BLOCK（须先修复劣化或显式说明）
```

白名单指标：`aggregate.explorer_hit_rate` / `aggregate.conditional_hit_rate`（evaluate_runs）+ `metrics.candidate.precision/recall/f1`（evaluate_results）。两报告结构不可混用（gate 会以结构不匹配 BLOCK）。

## 3. 默认开启检查点

以下改动合入前须过门槛（历史实际流程的文档化——EXPLORER-PROMPT-FIX / M2 收尾-2 verify 重写均按此执行）：

1. prompt 措辞/约束变更（explorer/verify/report）；
2. 生成方式变更（如 M4-SEED-HOPS 骨架链）；
3. 输出 schema 字段/枚举变更（须 sync 后）；
4. 模型/温度等推理配置变更。

快速验证用定向 harness（分钟级——`scripts/probe_explorer_entry.py` / `scripts/probe_verify_entry.py`），全量判定用本门槛。

## 4. BLOCK 处置

- 指标劣化 → 修复后再合（或 --tolerance 显式放宽并记录理由）；
- current 缺指标 → 恢复指标产出；
- 结构不匹配 → 检查是否混用了 evaluate_runs / evaluate_results 报告。
