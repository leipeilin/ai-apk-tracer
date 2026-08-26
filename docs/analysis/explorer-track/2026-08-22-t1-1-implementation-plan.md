# 任务实施方案：T1.1（数据库迁移 v4：assets/batches + runs 关联列）

> **任务编号**：T1.1
> **日期**：2026-08-22
> **依据大纲**：
> - 设计稿：`docs/analysis/explorer-track/2026-08-22-t0-8-implementation-plan.md`（T0.8，已含评审修订）
> - 评审：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan-review.md` §4.13/§4.12
> - 实施计划：`docs/analysis/explorer-track/2026-08-21-explorer-track-implementation-plan.md` T1.1
> **状态**：起草
> **前置依赖**：T0.8 设计稿（已提交 `9f9cebb`）；基线双 APK 已建立（`528a3c8`）

---

## 1. 任务目标与范围

- **目标**：按 T0.8 设计稿实现 schema_migrations **v4**：`assets`/`batches` 两表 + `runs` 三新列（`asset_id`/`batch_id` 可空外键 + `ai_skipped_by_batch_budget` 降级标记列），并补全部迁移测试。
- **范围**：
  - `backend/app/shared/repository.py`：`DATABASE_SCHEMA_VERSION` 3→4、`_migrate_assets_batches_v4`、`initialize` 迁移链接入；
  - `backend/tests/test_repository_v4_migration.py`（新增）：7 组迁移测试；
  - `backend/tests/test_repository_migrations_and_review.py`（修改）：既有断言随 v4 同步（`versions == [(1,),(2,),(3,)]` → 含 `(4,)`）。
- **非范围**：`assets/registry.py`/`batch.py` 功能（T1.2/T1.3）；`create_run` 写入 asset_id/batch_id（T1.3 编排）；API/前端（T1.4/T1.5）；`_run_row` 显式投影新列（返回 dict 自然含 NULL/0 新键，向后兼容，T0.8 已声明）。

## 2. 现状锚点

- `repository.py`：`DATABASE_SCHEMA_VERSION=3`（L15）；`initialize()`（L65-150）幂等建表 + `applied` 集合逐版本迁移（v2: `_migrate_review_v2` L160、v3: `_migrate_scoped_finding_ids_v3` L201）；`_record_migration` L152（`INSERT OR IGNORE`）；迁移函数风格：`PRAGMA table_info` 列检查 + `ALTER TABLE ADD COLUMN`；`connect()` L54 `PRAGMA foreign_keys=ON`（FK 恒开启）。
- 既有测试：`_create_legacy_database`（v1 形状库构造）、`test_legacy_database_migration_is_idempotent` 断言 `versions == [(1,),(2,),(3,)]`（**须随 v4 同步**）、v3 中断重跑用 `DELETE FROM schema_migrations WHERE version=3` + `PRAGMA user_version=2` 模拟。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/shared/repository.py` | 修改 | 版本 3→4 + `_migrate_assets_batches_v4` + 迁移链接入 |
| `backend/tests/test_repository_v4_migration.py` | 新增 | 7 组 v4 迁移测试 |
| `backend/tests/test_repository_migrations_and_review.py` | 修改 | 既有幂等断言同步 v4 |

### 3.2 迁移实现（`repository.py`）

```python
DATABASE_SCHEMA_VERSION = 4  # L15：3 → 4

# initialize() 迁移链（v3 分支后追加）：
#     if 4 not in applied:
#         self._migrate_assets_batches_v4(db)
#         self._record_migration(db, 4)

@staticmethod
def _migrate_assets_batches_v4(db: sqlite3.Connection) -> None:
    """v4：资产/批量扫描层（assets/batches 表 + runs 关联列，T0.8 设计稿）。

    幂等：CREATE TABLE IF NOT EXISTS + 列存在检查；不触碰既有表数据。
    FK 恒开启（connect L54），ON DELETE SET NULL 生效。
    全部语句逐条 db.execute()（评审 R-1）：DDL 在挂起事务内执行，与 v2 ALTER
    行为一致——不用 executescript（其隐式 COMMIT 会提前提交 v2/v3 挂起事务，
    破坏 initialize"连接事务负责回滚"契约，见 repository.py:69）。
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS assets (
            id TEXT PRIMARY KEY,
            package_name TEXT NOT NULL,
            apk_filename TEXT NOT NULL,
            apk_sha256 TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL DEFAULT 'local_upload',
            status TEXT NOT NULL DEFAULT 'ready',
            last_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_assets_created_at ON assets(created_at DESC)")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            max_ai_calls INTEGER,
            max_wall_seconds INTEGER,
            ai_skipped_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status)")
    run_columns = {row[1] for row in db.execute("PRAGMA table_info(runs)").fetchall()}
    if "asset_id" not in run_columns:
        db.execute(
            "ALTER TABLE runs ADD COLUMN asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL"
        )
    if "batch_id" not in run_columns:
        db.execute(
            "ALTER TABLE runs ADD COLUMN batch_id TEXT REFERENCES batches(id) ON DELETE SET NULL"
        )
    if "ai_skipped_by_batch_budget" not in run_columns:
        db.execute(
            "ALTER TABLE runs ADD COLUMN ai_skipped_by_batch_budget INTEGER NOT NULL DEFAULT 0"
        )
    db.execute("CREATE INDEX IF NOT EXISTS idx_runs_batch_id ON runs(batch_id)")
```

**注意**（评审 R-1 修订）：**逐条 `db.execute()`** 而非 `executescript`——后者会隐式 COMMIT 迁移链中段挂起的 v2/v3 事务（Python sqlite3 模块行为），破坏 initialize 的回滚契约；既有建表段（L73）可用 executescript 是因其位于连接初始、无挂起事务。三处 `ALTER` 均为可空列或带默认值列，满足 SQLite ADD COLUMN 约束（FK=ON 下 REFERENCES 列须默认 NULL）。

### 3.3 测试方案（`test_repository_v4_migration.py` 新增）

1. **test_v4_fresh_database_creates_assets_batches**：全新库 `initialize()` → assets/batches 表存在、runs 含三新列、`user_version==4`、`schema_migrations` 含 (1,)(2,)(3,)(4,)；
2. **test_v4_upgrade_from_v1_legacy_preserves_data**：`_create_legacy_database` 构造 v1 库（含 runs/findings/review_history 数据）→ `initialize()` → 新表新列就位、既有数据逐行完好（run/finding/history 数量与内容）、v4 已记录；
3. **test_v4_upgrade_from_v3_with_migration_records**（叠加路径，T0.8 测试项 6；评审 R-2 定稿构造）：全新库 `initialize()` → `DELETE FROM schema_migrations WHERE version=4` + `PRAGMA user_version=3` + `DROP TABLE assets`/`batches`（runs 三新列保留——模拟"v3 库含 v1/v2/v3 迁移记录"的真实旧库升级路径，覆盖"v4 记录缺失"场景；"列缺失"场景由 N-1 半迁移与 A-2 v1 升级互补覆盖；忠实 v3 数据形状由 A-2 的 v1→v4 全链升级覆盖）→ 再次 `initialize()` → v4 就位且数据无损；
4. **test_v4_interrupted_migration_reruns_idempotently**（中断恢复，T0.8 测试项 6）：完整 `initialize()` 后 `DELETE FROM schema_migrations WHERE version=4`（模拟 v4 完成但未记录的崩溃窗口）→ 再次 `initialize()` → 不报错、表结构正确、数据无损；
5. **test_v4_foreign_key_set_null_on_asset_delete**（外键行为）：插入 asset + 关联 run（`runs.asset_id`）→ 删除 asset 行 → 该 run 的 `asset_id` 为 NULL（`ON DELETE SET NULL` 生效）；batch 同理；
6. **test_v4_large_runs_table_migrates_correctly**（大表，T0.8 测试项 7）：v3 库预插 1,000 行 runs → `initialize()` → 1,000 行完好、三新列默认值正确（NULL/NULL/0）；
7. **既有测试同步（非新用例）**：更新 `test_legacy_database_migration_is_idempotent` 的 versions 断言为 `[(1,),(2,),(3,),(4,)]`（schema 演进的必要同步，非绕过）。

**计数与映射（评审 R-6）**：新文件含 **6 组**新测试（上述 1-6）+ 1 处既有断言同步；T0.8 测试项 3（重复 initialize 幂等）无专设新用例，由既有双调用（test L100-101，升级后断言含 v4）与 N-2 兜底覆盖。

### 3.4 与 T0.8 设计稿一致性对照

| 设计稿条目 | 本方案实现 | 一致性 |
|---|---|---|
| §3.1 版本 3→4 + `_migrate_assets_batches_v4` | §3.2 同名函数 + 迁移链接入 | 一致 |
| §3.2 assets 表（含 UNIQUE sha256/last_run_id 外键/索引） | SQL 逐字段一致 | 一致 |
| §3.3 batches 表（预算快照 + ai_skipped_count） | SQL 一致 | 一致 |
| §3.4 runs 三列（asset_id/batch_id/ai_skipped_by_batch_budget）+ idx_runs_batch_id | 一致 | 一致 |
| §3.7 测试 7 项（升级/初始化/幂等/外键/安全/叠加/大表） | §3.3 七组测试映射（SQL 安全由全参数绑定保证，代码走查 + 既有风格） | 一致 |
| 评审 R-1~R-7 处置（FK 恒开启/聚合来源/大表/叠加/`_run_row` 兼容/回滚版本/仅建结构） | 实现与声明一致；`_run_row` 自然兼容；回滚为文档（不实现）；v4 不写 M1 语义行 | 一致 |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| 迁移中断半状态 | 半迁移状态 | 幂等设计（IF NOT EXISTS + 列检查）+ 逐条 execute 保持事务原子性（评审 R-1）；中断重跑测试覆盖 | 重跑 initialize 自愈 |
| 既有测试断言漂移 | CI 失败 | L104 断言同步 v4 | 回退提交 |
| 真实旧库升级失败 | 用户数据风险 | v1/v3 构造升级测试 + 大表测试；真实库以 `.backup` 副本冒烟（A-9，评审 R-3） | 保留旧库文件重建（设计稿 §3.6） |

> 大表迁移说明（评审 R-4 修正）：SQLite `ADD COLUMN` 为 O(1) 元数据变更、**不重写表**（T0.8 设计稿"重建表持排他锁"表述失实，以其为准的"非高峰迁移"缓解随之修正）；大表测试保留其数据完好断言价值。

## 5. 依赖

- 前置：T0.8 设计稿；基线已建立（M1 里程碑级基线对照使用，任务级验收以迁移测试为准）。
