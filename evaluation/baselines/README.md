# 评估基线快照（M4-T4.4）

> 优化门槛（`backend.app.evaluation.gate`）的对照底座——快照须提交入库，改动前先跑门槛（`docs/evaluation-workflow.md`）。

| 快照 | 来源 run | 说明 |
|---|---|---|
| `m4-health.json` | `20260822T202633Z_2a80fc5a8735_7ecd4288`（com.xiaomi.health） | M2 验收 health 全量 run（2026-08-22）；golden v3（9 标注：6 hit + 3 conditional）；模型 deepseek-v4-pro-0813（token-plan）；**seed-hops 之前的代码**（dd52f12 修复后） |
| `m4-shop.json` | `20260822T210017Z_1c55d3fb9f95_dc24a077`（com.xiaomi.shop） | M2 验收 shop 全量 run（2026-08-22）；其余同上 |

**2026-08-26 审查 R-1/R-2 修正与重刷**：词边界匹配修复后双基线重刷——
`explorer_hit_rate` 均 **0.0**（shop 原 0.167 经真实反查确认为 QQ SDK `AuthActivity` 对合成 case `router-validation-overwritten` 泛化键（"Intent extras"/"loadUrl"）的**假阳**，词边界修复后归零——本 README 早前"remote-aidl-unguarded 被命中"的归因同样是错的，实际命中对象即该假阳）。**seed-hops 之前的探索轨对 golden hit 集真实命中为零**——该基线如实反映此事实，后续 seed-hops/攻击面注入的新 run 应在此基线上看到改善。

## 生成命令

```bash
backend/.venv/bin/python -m backend.app.evaluation.runner --runs <run_id> > evaluation/baselines/<name>.json
```

## 注意

- 规则轨基线（`--results` 模式）待首次规则轨离线评估后补充；
- **新增指标或 prompt/生成方式/模型变更后须刷新基线**（gate 对 baseline 缺失指标 SKIP——守卫回归不守卫演进）；
- 快照含 per_run 明细（候选命中/三本账/wall-time）——可审计。
