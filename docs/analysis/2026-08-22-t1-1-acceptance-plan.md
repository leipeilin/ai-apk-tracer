# 任务验收方案：T1.1（数据库迁移 v4）

> **任务编号**：T1.1
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/2026-08-22-t1-1-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 迁移测试 + 真实库冒烟 + 全量回归

---

## 1. 验收范围

- 迁移 v4 实现（repository.py）+ 7 组新测试 + 既有断言同步。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 新库初始化 | `test_v4_fresh_database_creates_assets_batches` | 通过（新表 + runs 三新列 + versions 1-4 + user_version=4） |
| A-2 | v1 旧库升级 | `test_v4_upgrade_from_v1_legacy_preserves_data` | 通过（数据逐行完好） |
| A-3 | v3 库（含迁移记录）升级 | `test_v4_upgrade_from_v3_with_migration_records` | 通过（叠加路径） |
| A-4 | 中断恢复幂等 | `test_v4_interrupted_migration_reruns_idempotently` | 通过（删 v4 记录重跑自愈） |
| A-5 | 外键 SET NULL | `test_v4_foreign_key_set_null_on_asset_delete`（测试连接须 `PRAGMA foreign_keys=ON` 前置或复用 repository.connect，防裸连接 FK=OFF 假阴性；评审 R-5） | 通过（删 asset/batch 后 run 关联列置 NULL） |
| A-6 | 大表迁移 | `test_v4_large_runs_table_migrates_correctly` | 通过（1000 行完好 + 新列默认值） |
| A-7 | 既有测试同步 | `test_legacy_database_migration_is_idempotent` 断言含 (4,) 且通过 | 通过 |
| A-8 | 迁移测试全过 | `.venv/bin/python -m pytest tests/test_repository_v4_migration.py tests/test_repository_migrations_and_review.py -q` | 全部通过 |
| A-9 | 真实库冒烟 | 复制本地 `.ai-apk-tracer/tracer.sqlite3`（**用 `sqlite3 <src> ".backup <dst>`**，防 WAL 未 checkpoint 丢数据，评审 R-3）→ 对副本执行 `initialize()` 冒烟（命令行 python，载体见验收记录） | 副本迁移成功：表结构就位 + **实测 N 个 run 完好**（数量以副本实测为准）+ `sqlite3 dump` runs 行数不变 |
| A-10 | 全量回归 | `.venv/bin/python -m pytest -q` | 除 3 个 pre-existing guard_verifier 失败外全部通过 |
| A-11 | 统一校验 | `scripts/check-all.sh` + `ruff check` | 通过，无新增失败 |
| A-12 | API 兼容断言（评审灰色点采纳） | 迁移后 `repository.get_run()` 返回含 `asset_id`/`batch_id`/`ai_skipped_by_batch_budget` 三新键（NULL/NULL/0），固化 T0.8 R-5 的 `_run_row` 兼容声明 | 断言通过 |

## 3. 回归标准

- [ ] 迁移不触碰既有表数据（A-2/A-9 双重验证）。
- [ ] `create_run`/`get_run`/findings 读写路径测试全过（`_run_row` 自然兼容新列）。
- [ ] 基线判据：迁移不改 run 产物生成路径（repository 仅元数据库）；M1 里程碑级以基线双 APK 对照（`2026-08-22-m1-baseline-runs.md` §3 流程）执行，本任务以 A-9 真实库冒烟替代。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | v4 半迁移（表已建、列未加）中断 | 构造：全新 initialize 后 `DROP INDEX idx_runs_batch_id` + 手工去除三列不可行（SQLite 无 DROP COLUMN 前置）——改用等价构造：**新建库手工执行 v4 建表 DDL（仅 assets/batches）+ 不加 runs 列 + 无 v4 记录** → `initialize()` | 幂等补齐（列检查分支补列，评审 R-5 构造要点） |
| N-2 | 重复 initialize（同进程/跨进程） | 连续两次 `initialize()` | 不报错、无重复索引/表 |
| N-3 | 既有 runs 行新列默认值 | 升级后查询旧行 | `asset_id IS NULL`、`batch_id IS NULL`、`ai_skipped_by_batch_budget=0` |
| N-4 | 重复 apk_sha256 插入 | 插入两条同 sha256 的 asset（连接 `PRAGMA foreign_keys=ON`，评审 R-5） | UNIQUE 约束拒绝（`IntegrityError`） |

## 5. 回退方案

- 任一验收点失败：修复后复验；迁移设计缺陷返回 T0.8 设计稿修订（第 2 轮）。
- 真实库风险：本地原库 `.ai-apk-tracer/tracer.sqlite3` 不直接迁移（A-9 用副本冒烟）；生产升级路径以设计稿 §3.6 回滚策略保障。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审 6 项 + 1 建议第 1 轮全部采纳（含高严重度 R-1：迁移改逐条 `db.execute()` 保持挂起事务原子性）。实施中 ruff 修复 `repository.py` 既有 import 排序 7 处（该文件首次纳入 ruff 检查暴露）。
>
> **重要现象（如实披露）**：长期存在的 3 个 `test_guard_verifier.py` 失败在本任务后**消失**（全量 915 passed / 0 failed）。根因定位：`test_guard_verifier.py:23` 直接 glob **真实** `.ai-apk-tracer/runs/*/` 目录（环境状态依赖）——M0 期间 8 个历史 run 的状态触发失败，基线建立时新增 2 个 run 后目录状态改变而翻绿。该测试隔离缺陷建议后续修复（不在 T1.1 范围）；此前各任务验收中"3 个 pre-existing 失败"的排除判断由此获得最终解释。

| 编号 | 结果（通过/失败/部分通过） | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | 新库初始化：versions [1,2,3,4] + user_version=4 + 表/列/约束就位 | - |
| A-2 | 通过 | v1 库升级：数据逐行完好（run_one + scoped finding）+ 旧行默认值 (None,None,0) | - |
| A-3 | 通过 | 叠加路径（回退 v4 记录 + DROP 两表 → 再升级）通过 | - |
| A-4 | 通过 | 中断恢复（删 v4 记录重跑）幂等自愈 | - |
| A-5 | 通过 | FK=ON 连接删 asset/batch → run 关联列置 NULL | - |
| A-6 | 通过 | 1,000 行大表迁移完好 + 新列默认值全对 + ADD COLUMN O(1) | - |
| A-7 | 通过 | 既有断言同步 [(1,),(2,),(3,),(4,)] 且通过 | - |
| A-8 | 通过 | 迁移测试 9+4 项全过 | - |
| A-9 | 通过 | 真实库 `.backup` 副本冒烟（命令行 python）：versions [1,2,3,4]、**实测 10 个 run 完好**、三新列/两新表就位、旧行默认值 (None,None,0) | - |
| A-10 | 通过 | 全量 pytest：**915 passed / 0 failed**（guard 现象见上方披露） | - |
| A-11 | 通过 | check-all + ruff 全过 | - |
| A-12 | 通过 | `get_run` 返回三新键断言（NULL/NULL/0）通过 | - |
| N-1 | 通过 | 半迁移（表已建列未加）幂等补列测试通过 | - |
| N-2 | 通过 | 重复 initialize 不报错（A-1/A-3/A-4 内含双跑） | - |
| N-3 | 通过 | 旧行默认值 (None, None, 0)（A-2 内断言） | - |
| N-4 | 通过 | 重复 apk_sha256 → IntegrityError 拒绝 | - |
