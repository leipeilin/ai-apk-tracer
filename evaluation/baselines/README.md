# 评估基线快照（M4-T4.4）

> 优化门槛（`backend.app.evaluation.gate`）的对照底座——快照须提交入库，改动前先跑门槛（`docs/evaluation-workflow.md`）。

| 快照 | 来源 run | 说明 |
|---|---|---|
| `m4-health.json` | `20260822T202633Z_2a80fc5a8735_7ecd4288`（com.xiaomi.health） | M2 验收 health 全量 run（2026-08-23）；golden v3（9 标注：6 hit + 3 conditional）；模型 deepseek-v4-pro-0813（token-plan）；explorer/verify prompt 修复后（dd52f12）但 **seed-hops 之前**（无骨架链） |
| `m4-shop.json` | `20260822T210017Z_1c55d3fb9f95_dc24a077`（com.xiaomi.shop） | M2 验收 shop 全量 run（2026-08-23）；其余同上——hit_rate=0.167（1/6，`remote-aidl-unguarded` 被命中） |

## 生成命令

```bash
backend/.venv/bin/python -m backend.app.evaluation.runner --runs <run_id> > evaluation/baselines/<name>.json
```

## 注意

- 规则轨基线（`--results` 模式）待首次规则轨离线评估后补充；
- **新增指标或 prompt/生成方式/模型变更后须刷新基线**（gate 对 baseline 缺失指标 SKIP——守卫回归不守卫演进）；
- 快照含 per_run 明细（候选命中/三本账/wall-time）——可审计。
