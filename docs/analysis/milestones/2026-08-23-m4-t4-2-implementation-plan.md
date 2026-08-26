# 任务实施方案：M4-T4.2（批量评估——探索轨指标 + 三本账 + wall-time）

> **任务编号**：M4-T4.2
> **依据**：实施计划 §3.5 T4.2（"多 APK 输入，输出 precision/recall/F1、AI 调用数（三本账）、wall-time"）；T4.1 已交付 explorer_hit 接口
> **状态**：起草
> **前置**：T4.1 ✅（explorer_hit 三通道命中）；规则轨 P/R/F1 已在 metrics.calculate_metrics

---

## 1. 目标与范围

新增**run 产物级评估**：从已完成的 run_dir（探索候选 + manifest 统计）对 golden 标注计算探索轨指标，与三本账/wall-time 汇总输出——多 APK 输入 = 多 run_dir 聚合。

**范围**：
1. `backend/app/evaluation/runner.py`——新函数 `evaluate_explorer_against_golden(run_dir, cases)`（探索候选 ↔ golden hit/conditional 命中率）+ `summarize_run_costs(run_dir)`（三本账 + wall-time 从 manifest stages 提取）+ `evaluate_runs(run_dirs, ...)` 聚合入口 + CLI `--runs` 模式；
2. 测试：合成 run_dir fixture（探索候选 JSON + manifest）的命中率/条件命中率/三本账/wall-time/聚合。

**非范围**：规则轨指标（已有 calculate_metrics 不动）；T4.3 报告质量检查；T4.4 门槛。

## 2. 现状锚点（实读）

- `explorer_hit(case, chain_proposal)`（golden.py，T4.1）——三通道命中，hit-only；
- 探索候选落盘：`run_dir/explorer/candidates.json`（list，每条含 `chain_proposal`（source/sink/hops）——T4.1 评审 R-5 已核验结构）；
- run manifest：`run_dir/manifest.json` 的 `stages[]`——explorer 阶段 summary（ai_requests_used/read_requests_used/deep_dive_requests_used）+ AI 阶段（requests_used/explorer_requests_used/ai_stage_requests_used/verify_requests_used）+ created_at/completed_at（顶层）——shop run 实读确认；
- runner.py 现有 CLI：--manifest/--results（离线模式）——新增 --runs（逗号分隔 run 目录名）模式。

## 3. 详细方案

### 3.1 evaluate_explorer_against_golden(run_dir, cases)

```python
def evaluate_explorer_against_golden(run_dir: Path, cases) -> dict:
    # 读 explorer/candidates.json（缺失 → 空列表容错）
    proposals = [c.get("chain_proposal") for c in loaded if isinstance(c.get("chain_proposal"), Mapping)]
    hit_cases = [c for c in cases if c.explorer_expected and c.explorer_expected.expectation == "hit"]
    conditional_cases = [... == "conditional"]
    hit_ids = {c.id for c in hit_cases if any(explorer_hit(c, p) for p in proposals)}
    conditional_hit_ids = {...}
    return {
        "explorer_hit_rate": ratio(len(hit_ids), len(hit_cases)),
        "explorer_hits": sorted(hit_ids), "explorer_hit_total": len(hit_cases),
        "conditional_hit_rate": ..., "conditional_hits": ..., "conditional_total": ...,
        "proposals_total": len(proposals),
    }
```

### 3.2 summarize_run_costs(run_dir)

从 manifest stages 提取：`explorer_requests`（explorer 阶段 ai_requests_used）+ `deep_dive_requests` + `verify_requests`（AI 阶段 verify_requests_used）+ `ai_stage_requests` + `total_requests`（AI 阶段 requests_used）+ `wall_seconds`（completed_at - created_at）+ `finding_count`（aggregation 阶段）。

### 3.3 evaluate_runs 聚合 + CLI

```python
def evaluate_runs(run_dirs: list[Path], manifest_path=DEFAULT) -> dict:
    # 每 run：探索轨指标 + 成本；聚合：平均命中率 + 总三本账 + 总 wall-time
```
CLI：`--runs run_id1,run_id2`（RUNS_ROOT 解析）→ JSON 输出。

### 3.4 文件与测试

| 文件 | 变更 |
|---|---|
| backend/app/evaluation/runner.py | 三函数 + CLI --runs |
| backend/tests/test_evaluation_runner_runs.py（新） | 合成 run_dir fixture：候选命中/未命中/条件/容错（无候选文件）三本账提取 wall-time 聚合 |

### 3.5 风险

manifest stages 字段名漂移 → 提取容错（缺字段 None/0 + warning）；时间解析失败 → wall_seconds None。

## 4. 大纲一致性

T4.2 原文三条（P/R/F1 规则轨已有/三本账/wall-time）+ 探索轨指标（T4.1 消费）——本任务全量交付 run 级评估闭环；"多 APK 输入"经 --runs 聚合承载。
