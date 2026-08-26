# 评估基线快照（M4-T4.4）

> 优化门槛（`backend.app.evaluation.gate`）的对照底座——快照须提交入库，改动前先跑门槛（`docs/evaluation-workflow.md`）。

| 快照 | 来源 run | 说明 |
|---|---|---|
| `m4-health.json` | `20260822T202633Z_2a80fc5a8735_7ecd4288`（com.xiaomi.health） | M2 验收 health 全量 run（2026-08-22）；golden v3；deepseek-v4-pro-0813（token-plan）；**seed-hops 之前的代码**（dd52f12 修复后） |
| `m4-shop.json` | `20260826T141857Z_1c55d3fb9f95_eada0e71`（com.xiaomi.shop） | shop 全量 run（2026-08-26，siliconflow deepseek-v4-flash）；**新代码 5d4e18a**（含 SEED-HOPS/攻击面注入/verify 修复 + F3 taxonomy 1.0.4 扩充前） |

**2026-08-27 F1 组件域过滤重刷**：golden 分母按 APK 域过滤（`scope_keys`/匹配键组件域判定——跨 APK case 进 `excluded_cases`）——
- `m4-shop`：hit_total **1**（仅 extra-close 在 shop 域）/ cond 1 / excluded 7；
- `m4-health`：hit_total **4**（4 个 health 真 hit case）/ cond 2 / excluded 3（extra-close/remote-aidl/account-broadcast）；
- 两基线 hit_rate 均 0.0（seed-hops 前/初期的真实水平——shop 新代码探索 0/1 未命中 extra_close_url 分支，见 gap-analysis 根因 4）。

**标注规范（F1 核验 V-7）**：`source_match_keys` 至少含一个 manifest 组件类名（或配 `scope_keys` FQCN 域键）——纯 helper 类/stub 类锚定的 case（如 account-broadcast 的 AccountChangedBroadcastHelper）会被所有真实 run 的域过滤排除而永不可评估。

## 生成命令

```bash
backend/.venv/bin/python -m backend.app.evaluation.runner --runs <run_id> > evaluation/baselines/<name>.json
```

## 注意

- 规则轨基线（`--results` 模式）待首次规则轨离线评估后补充；
- **新增指标或 prompt/生成方式/模型变更后须刷新基线**（gate 对 baseline 缺失指标 SKIP——守卫回归不守卫演进）；
- 快照含 per_run 明细（候选命中/三本账/wall-time/excluded_cases/component_scope）——可审计。
