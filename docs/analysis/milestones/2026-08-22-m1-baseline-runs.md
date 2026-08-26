# M1 基线双 APK 产物记录（通用门禁 §4.1 基线）

> **日期**：2026-08-22
> **来源承诺**：`docs/analysis/milestones/2026-08-22-m0-change-scope-justification.md` §4——M1 T1.1 开工前建立基线双 APK 产物 diff 基线。
> **基线代码**：`a1c8773`（M0 审查落实后 HEAD）
> **配置**：默认配置（`ai.enabled=true`、`model=deepseek-v4-flash`、新开关全关），经 `POST /api/runs`（`authorized=true`）完整 run。

---

## 1. 基线 run 记录

| APK | run_id | apk_sha256（前 16） | findings | 确定性产物文件数 | 清单聚合哈希（前 16） |
|---|---|---|---|---|---|
| `3.57.0_20260709221757_mihealth.apk`（health） | `20260821T173022Z_2a80fc5a8735_1346a79b` | `2a80fc5a87353c2c` | 365 | 60,515 | `24c9266d95beeffb` |
| `com.xiaomi.shop_5.53.0.20260527.apk`（shop） | `20260821T173843Z_1c55d3fb9f95_69a38e3d` | `1c55d3fb9f953e67` | 151 | 29,871 | `395634f1cb4dccaf` |

清单文件（本地，`.ai-apk-tracer/` 已 gitignore，不入库）：
- `.ai-apk-tracer/baselines/m1-health-baseline.json`
- `.ai-apk-tracer/baselines/m1-shop-baseline.json`

## 2. diff 口径（`scripts/baseline-manifest.py`）

**纳入**（确定性产物，字节级 diff 适用）：`decompile/sources/**`、`decompile/resources/**`、`index/code-index.json`、`index/analysis.sqlite3`、`rule-results/**`、`slices/**`、`manifest.json` 确定性字段子集（剔除 `created_at/completed_at/updated_at/trace_id/run_id/stages/analysis_incomplete/cleanup_history`）。

**排除**（非确定或临时）：`ai-cache/`、`ai-trace/`、`findings/`、`reports/`（含 AI 决策字段与模型输出，非字节级可复现；以 `findings_count` 作数量基线）；`logs/`、`tmp/`、`rule-work/`、`input/`。

> 说明：`findings/` 文件名含 run_id 前缀且内容含 AI 结论字段（`evidence_decision/review_status` 等），模型输出存在非确定性，故不纳入字节级 diff；M1 改动面（repository 迁移/资产层）不触 findings 生成逻辑，数量基线（365/151）+ `rule-results`（确定性候选）已覆盖回归判据。若 M1 后 findings_count 变化，须逐项归因。

## 3. M1 各任务对照流程（通用门禁 §4.1 执行方法）

1. 在待验收的 HEAD 上，按同样方式对 health/shop 各跑一次默认配置 run；
2. `python3 scripts/baseline-manifest.py <new_run_id> <tmp-baseline.json>`；
3. 对比基线清单与本轮清单：文件集合一致 + 逐文件 sha256 一致 + `findings_count` 一致 → "默认配置产物 diff 为空"通过；
4. `index/analysis.sqlite3` 为二进制文件，若字节级 diff 非空，须以 `sqlite3 dump` 内容对比复核后再判定（页分配可能引入字节级噪声）；
5. 清单聚合哈希（本基线：health `24c9266d95beeffb` / shop `395634f1cb4dccaf`）可直接作快速判据。

## 4. 基线 APK 来源

本地历史 run 的上传副本（`.ai-apk-tracer/runs/<历史 run>/input/app.apk`），sha256 与基线记录一致；仓库不含 APK 文件（`.ai-apk-tracer/` gitignore）。重跑基线须使用相同 sha256 的 APK。
