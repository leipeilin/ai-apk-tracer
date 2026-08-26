# 任务验收方案：T1.4（资产/批量 API 端点）

> **任务编号**：T1.4
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t1-4-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 端点测试 + 全量回归 + 统一校验

---

## 1. 验收范围

- 四端点 + 门禁 + 脱敏 + BatchCreateRequest + app.state 组装 + 测试。
- **范围声明（D1 收窄）**：仅本地 APK 导入；包名列表导入 Phase 1 不实现（偏差已记录于实施稿 D1 与评审 R-4）。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 默认门禁 | `test_assets_endpoints_disabled_by_default` | 四端点 503 ASSETS_DISABLED |
| A-2 | 导入往返 | `test_import_asset_roundtrip` | 201 + 无 apk_path + 列表可见 + DB 落库 |
| A-3 | 授权强制（import） | `test_import_requires_authorization` | 422 AUTHORIZATION_CONFIRMATION_REQUIRED |
| A-4 | 重复导入冲突 | `test_import_duplicate_conflict` | 409 + details.asset_id |
| A-5 | 导入负例 | `test_import_rejects_invalid_inputs` | 422（registry 校验传导；含路径穿越文件名 → INVALID_APK_FILENAME，评审 R-3） |
| A-6 | 创建批次 | `test_create_batch_returns_pending` | 202 + pending + 快照 + run_batch 调度 + **响应无 assets_json（评审 R-1）** |
| A-7 | 授权强制（batches） | `test_create_batch_requires_authorization` | 422 |
| A-8 | 资产不存在 | `test_create_batch_missing_asset` | 404 |
| A-9 | 批次汇总查询 | `test_get_batch_summary` | 汇总字段（+ 无 assets_json，评审 R-1）；missing → 404 |
| A-10 | 请求模型校验 | `test_batch_request_model_validation` | 空列表 422 |
| A-11 | 单测通过 | `.venv/bin/python -m pytest tests/test_assets_api.py -q` | 全部通过 |
| A-12 | 全量回归 | `.venv/bin/python -m pytest -q` | **952 passed / 0 failed**（942 基线 + 10 新增，评审 R-5） |
| A-13 | 统一校验 | `scripts/check-all.sh` + `ruff check`（改动文件） | 通过 |

## 3. 回归标准

- [ ] 既有端点（runs/findings/review/cleanup）不受影响（main.py 组装为纯增量）。
- [ ] `assets.enabled=False` 默认下既有 test_api.py 全过（A-1 的默认配置回归）。
- [ ] `ruff check` 通过。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 未启用时导入已授权文件 | enabled=False + authorized=true | 503（门禁先于授权判定） |
| N-2 | import 无文件字段 | 缺 file form 字段 | 422 REQUEST_VALIDATION_ERROR |
| N-3 | batches body 非法 JSON | 缺字段/类型错 | 422 REQUEST_VALIDATION_ERROR |
| N-4 | get_batch 注入样例 | `GET /api/batches/x'; DROP TABLE batches;--` | 404（参数绑定，机制保证） |

## 5. 回退方案

- 任一验收点失败：修复后复验；设计缺陷（D1-D6 层面）上升评审第 2 轮。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审 6 项意见第 1 轮全部采纳（含 R-1 batches 响应剔除 assets_json 双份字段、R-2 测试 data_root 显式 tmp 隔离防工作区污染）。实施微调：新端点参数用 Annotated 风格（避免新增 B008——既有 create_run 的 `File(...)` 默认参数模式为历史债务，不扩散）；实际新增测试 11 项（计划 10，A-5 负例拆分更细），全量 953（预期 952，+1 为拆分增量）。剩余 2 个 ruff 提示（routes.py:96 B008 / :151 BLE001）为既有代码，非本次引入。

| 编号 | 结果（通过/失败/部分通过） | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | 默认 enabled=False 四端点 503 ASSETS_DISABLED；门禁先于授权（N-1） | - |
| A-2 | 通过 | 201 + 无 apk_path + 列表可见 + 元数据完整（sha256/source/status） | - |
| A-3 | 通过 | authorized=false → 422 AUTHORIZATION_CONFIRMATION_REQUIRED + 未落库 | - |
| A-4 | 通过 | 409 + details.asset_id=既有资产 | - |
| A-5 | 通过 | 空包名/非 .apk/路径穿越(INVALID_APK_FILENAME)/非 ZIP → 422 + 未落库 | - |
| A-6 | 通过 | 202 + pending + assets 快照 + **无 assets_json** + run_batch 调度（no-op 替身实证） | - |
| A-7 | 通过 | authorized=false → 422 | - |
| A-8 | 通过 | missing asset_id → 404 | - |
| A-9 | 通过 | 汇总字段（total/completed/failed/ai_skipped）+ 无 assets_json + missing 404 + 注入样例 404 | - |
| A-10 | 通过 | 空列表/缺字段 → 422 REQUEST_VALIDATION_ERROR | - |
| A-11 | 通过 | test_assets_api.py 11 项全过 | - |
| A-12 | 通过 | 全量 pytest：**953 passed / 0 failed**（942 + 11） | - |
| A-13 | 通过 | check-all（含前端构建）全过；新代码 ruff 零新增违规 | - |
| N-1 | 通过 | 门禁先于授权（A-1 断言） | - |
| N-2 | 通过 | 缺 file 字段 → REQUEST_VALIDATION_ERROR | - |
| N-3 | 通过 | 空 asset_ids/缺 authorized → REQUEST_VALIDATION_ERROR | - |
| N-4 | 通过 | 注入样例 404 + 表完好（A-9 断言） | - |
