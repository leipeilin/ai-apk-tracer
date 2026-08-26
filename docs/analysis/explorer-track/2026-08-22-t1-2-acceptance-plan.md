# 任务验收方案：T1.2（资产注册表 registry.py）

> **任务编号**：T1.2
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t1-2-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测 + 全量回归

---

## 1. 验收范围

- AssetRegistry（6 方法 + 内容寻址副本入库）+ 测试。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 注册持久化 | `test_register_persists_asset_and_copy` | 通过（元数据 + 副本内容寻址落位） |
| A-2 | sha256 去重 | `test_register_duplicate_sha256_conflict` | ConflictError + details.asset_id + 无重复副本 |
| A-3 | 大小上限拒绝 | `test_register_rejects_oversize` | ValidationError(APK_TOO_LARGE) + 无残留 |
| A-4 | 非 ZIP 拒绝 | `test_register_rejects_non_zip` | validate_apk_zip 既有错误 + 无残留 |
| A-5 | get 未找到 | `test_get_not_found` | NotFoundError |
| A-6 | list 过滤/排序 | `test_list_assets_filters_by_status` | status 过滤 + created_at DESC |
| A-7 | 状态更新白名单 | `test_update_status_whitelist` | 合法枚举通过；非法拒绝；不存在 → NotFoundError |
| A-8 | link_run + FK 联动 | `test_link_run_updates_last_run_id` | last_run_id 更新；删 run 后置 NULL（T1.1 FK 行为） |
| A-9 | 删除清副本 | `test_delete_removes_record_and_copy` | 记录 NotFound + 副本目录消失 |
| A-10 | SQL 注入安全 | `test_sql_injection_safety` | 注入样例安全返回（参数绑定实证） |
| A-11 | 测试通过 | `.venv/bin/python -m pytest tests/test_asset_registry.py -q` | 全部通过 |
| A-12 | 全量回归 | `.venv/bin/python -m pytest -q` | 915+ 全部通过（T1.1 后基线 915 passed / 0 failed） |
| A-13 | 统一校验 | `scripts/check-all.sh` + `ruff check` | 通过 |

## 3. 回归标准

- [ ] T1.1 迁移测试不受影响（assets 表无 schema 变更）。
- [ ] registry 不触碰 runs/findings 表写入路径。
- [ ] `ruff check` 通过。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 空 package_name | `register(..., package_name="")`（`test_register_rejects_bad_inputs`） | ValidationError（必填校验） |
| N-2 | 非法文件名（路径穿越） | `filename="../../x.apk"`（同上测试） | ValidationError（basename + 扩展名校验） |
| N-3 | register 中途异常（zip 校验失败） | 有效字节流后接非法结构 | 副本不落位（finally 清理 + os.replace 延后） |
| N-4 | delete 不存在 id | `delete("missing")` | NotFoundError（幂等拒绝而非静默） |
| N-5 | 0 字节流 | 空文件注册（同 4b 测试） | zip 校验拒绝（魔数缺失） |

## 5. 回退方案

- 任一验收点失败：修复后复验；设计缺陷上升评审第 2 轮。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审 8 项意见第 1 轮全部采纳（含高危 R-1 冲突清副本自毁数据 → 改为保留副本 + 断言补强）。实施中两处修正：① 文件名校验从"sanitize 取 basename"改为**显式拒绝路径分隔符**（安全默认，N-2 直接 422）；② `ConflictError` 补充 `details` 参数（与 `ValidationError` 对齐，向后兼容——原构造无 details 致 ConflictError 无法携带既有 asset_id）。全量 926 passed / 0 failed（T1.1 基线 915 + 11 新增）。

| 编号 | 结果（通过/失败/部分通过） | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | 注册持久化：元数据字段 + 内容寻址副本（`<sha[:2]>/<sha256>/demo.apk`）内容一致 | - |
| A-2 | 通过 | 冲突 ConflictError + details.asset_id/apk_sha256 + **既有副本仍存在且内容一致**（R-1 断言）+ 无重复记录 | - |
| A-3 | 通过 | `max_apk_size_mb=0` 超限 → APK_TOO_LARGE + 无 `.incoming-*` 残留 | - |
| A-4 | 通过 | 非 ZIP 字节流 → validate_apk_zip 拒绝 + 无残留 | - |
| A-5 | 通过 | `get("missing")` → NotFoundError | - |
| A-6 | 通过 | status 过滤 + created_at DESC（双资产三态验证） | - |
| A-7 | 通过 | `scanning` 通过；`archived` → INVALID_ASSET_STATUS；不存在 → NotFoundError | - |
| A-8 | 通过 | link_run 更新 last_run_id；`missing_run` → NotFoundError（FK 裸异常不逃逸）；删 run → NULL（T1.1 FK 联动） | - |
| A-9 | 通过 | 删除后记录 NotFound + 副本目录消失 + 二次删除 NotFound | - |
| A-10 | 通过 | 三类注入样例安全返回 NotFoundError + 表结构完好 + 注册仍正常 | - |
| A-11 | 通过 | test_asset_registry.py 11 项全过 | - |
| A-12 | 通过 | 全量 pytest：**926 passed / 0 failed** | - |
| A-13 | 通过 | check-all（含前端构建）+ ruff 全过 | - |
| N-1 | 通过 | 空/空白 package_name → PACKAGE_NAME_REQUIRED（4b 用例） | - |
| N-2 | 通过 | `../../evil.apk` → INVALID_APK_FILENAME（显式拒绝路径分隔符） | - |
| N-3 | 通过 | 非 ZIP/超限均无 `.incoming-*` 残留（finally 清理） | - |
| N-4 | 通过 | `delete("missing")` → NotFoundError | - |
| N-5 | 通过 | 0 字节流 → zip 校验拒绝（4b 用例） | - |
