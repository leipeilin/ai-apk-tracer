# 任务实施方案：T0.8（Asset/BatchScan 迁移设计）

> **任务编号**：T0.8
> **日期**：2026-08-22
> **依据大纲**：
> - 评审：`docs/analysis/2026-08-18-project-optimization-plan-review.md` §4.13（assets/batches 须走 schema_migrations）
> - 实施计划：`docs/analysis/2026-08-21-explorer-track-implementation-plan.md` T0.8（设计稿）、T1.1（实现）
> **状态**：起草
> **前置依赖**：T0.7（`assets`/`batch` 配置段已定）

---

## 1. 任务目标与范围

- **目标**：产出 `assets`/`batches` 表与 `runs` 关联列的**迁移设计稿**（数据模型 + 迁移版本 + 升级路径 + 回滚 + 测试方案），供 T1.1 实现。
- **范围**：
  - 数据模型设计（表结构、约束、索引）；
  - 迁移 v4 设计（`DATABASE_SCHEMA_VERSION` 3→4、`_migrate_assets_batches_v4`、`initialize` 流程接入）；
  - 升级路径/回滚策略/测试方案设计。
- **非范围**：迁移代码实现与测试（T1.1）；`assets/registry.py`/`batch.py` 功能（M1 T1.2/T1.3）；API/前端（T1.4/T1.5）。

## 2. 现状锚点（2026-08-22 复核）

- `repository.py`：`DATABASE_SCHEMA_VERSION = 3`（L15）；`initialize()`（L65）幂等建表（schema_migrations/runs/findings/review_history）+ 按 `schema_migrations` 已应用版本逐步迁移（L131-150）→ `_record_migration`（L152）→ `PRAGMA user_version`（L150）；迁移函数幂等（`_migrate_review_v2` L160：`PRAGMA table_info` 检查列 → `ALTER TABLE ADD COLUMN`）。
- `runs` 表现有列（L79-95）：id/trace_id/status/stage/apk_filename/apk_sha256/authorized/config_json/manifest_path/error_code/error_message/pipeline_version/schema_version/created_at/updated_at。
- 约定：迁移不重建/删除既有表；外键 `ON DELETE CASCADE`（findings）示例可见；全部 SQL 参数绑定（仓库红线）。

## 3. 详细实现方案（设计稿）

### 3.1 迁移版本

- `DATABASE_SCHEMA_VERSION`：`3` → `4`。
- 新迁移函数：`_migrate_assets_batches_v4(db)`，接入 `initialize()` 迁移链（`if 4 not in applied` 分支，风格同 v2/v3）。
- 幂等：`CREATE TABLE IF NOT EXISTS` + 列存在检查（`PRAGMA table_info`）。

### 3.2 `assets` 表

```sql
CREATE TABLE IF NOT EXISTS assets (
    id            TEXT PRIMARY KEY,
    package_name  TEXT NOT NULL,
    apk_filename  TEXT NOT NULL,
    apk_sha256    TEXT NOT NULL UNIQUE,
    source        TEXT NOT NULL DEFAULT 'local_upload',
    status        TEXT NOT NULL DEFAULT 'ready',
    last_run_id   TEXT REFERENCES runs(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_created_at ON assets(created_at DESC);
```

- `id`：与 run id 同风格（时间戳+随机后缀，由 `registry.py` 生成，此处仅表结构）；
- `source`：`local_upload`（本地 APK 导入）/`package_list`（包名列表，T1.2 扩展）；
- `status`：`ready`/`scanning`/`error`；
- `apk_sha256 UNIQUE`：防重复注册（§4.13 数据完整性）；`last_run_id` 可空关联最近 run。

### 3.3 `batches` 表

```sql
CREATE TABLE IF NOT EXISTS batches (
    id               TEXT PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'pending',
    max_ai_calls     INTEGER,
    max_wall_seconds INTEGER,
    ai_skipped_count INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    completed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);
```

- `status`：`pending`/`running`/`completed`/`failed`/`partial`（部分 run 失败）；
- `max_ai_calls`/`max_wall_seconds`：**batch 配置快照**（来自 `batch.max_ai_calls`/`max_wall_seconds`），预算变更不追溯历史批次（审计一致性，评审 §4.12）；
- `ai_skipped_count`：预算降级 run 数，聚合自 `runs.ai_skipped_by_batch_budget`（§3.4 新增列；评审 R-2），batch 汇总可审计。

### 3.4 `runs` 关联列（v4 迁移）

```sql
-- _migrate_assets_batches_v4 内，列存在检查后：
ALTER TABLE runs ADD COLUMN asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL;
ALTER TABLE runs ADD COLUMN batch_id TEXT REFERENCES batches(id) ON DELETE SET NULL;
ALTER TABLE runs ADD COLUMN ai_skipped_by_batch_budget INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_runs_batch_id ON runs(batch_id);
```

- 可空外键 `ON DELETE SET NULL`：删除 asset/batch 不级联删 run（保留 run 证据，符合"复核完成前不自动丢失证据"的 retention 语义）；
- 既有 runs 行 `asset_id`/`batch_id` 均为 NULL（向后兼容，无默认值注入）；
- **`ai_skipped_by_batch_budget`（评审 R-2）**：预算降级标记落库列（`INTEGER NOT NULL DEFAULT 0`），`batches.ai_skipped_count` 聚合来源；
- **FK 恒开启（评审 R-1）**：`connect()` 已执行 `PRAGMA foreign_keys=ON`（`repository.py:54`），`ON DELETE SET NULL` 生效；迁移在 `connect()` 事务内执行，FK 全程生效；
- **`_run_row` 兼容（评审 R-5）**：runs 加列后 `dict(row)` 返回含 `asset_id`/`batch_id`/`ai_skipped_by_batch_budget`（NULL/0），API 层按未知/空值容忍或显式过滤，前端忽略未知字段；
- **大库影响（评审 R-3）**：SQLite `ALTER TABLE ADD COLUMN` 需重建表并持排他写锁；runs 数据量大时迁移窗口拉长，测试含大表场景。

### 3.5 升级路径（旧库）

- 旧库（v3，无 assets/batches、runs 无 asset_id/batch_id）→ `initialize()`：
  - `CREATE TABLE IF NOT EXISTS` 建 assets/batches（不触碰既有表）；
  - runs 加列（`PRAGMA table_info` 检查后 `ALTER TABLE`，含三列）；
  - `_record_migration(db, 4)` → `PRAGMA user_version=4`；
  - 既有 runs/findings/review_history 数据完好（两新列 NULL、`ai_skipped_by_batch_budget=0`，findings 依赖 runs 不受影响）。
- 中断恢复：`schema_migrations` 未记录 v4 → 重复启动重跑 v4（幂等）。
- **v4 仅建结构（评审 R-7）**：迁移阶段不产生 `assets.source='package_list'`、`batches.status='partial'` 等 M1 语义行；枚举值由 T1.2/T1.3 写入。

### 3.6 回滚策略

- **默认**：不删 assets/batches（避免破坏已写数据）；提供"保留旧库文件 + 重建新库"的运维说明（storage.data_root 下旧 `tracer.sqlite3` 改名保留）。
- **显式回滚（文档说明，不自动执行）**：`DROP TABLE assets; DROP TABLE batches; ALTER TABLE runs DROP COLUMN asset_id; ALTER TABLE runs DROP COLUMN batch_id; ALTER TABLE runs DROP COLUMN ai_skipped_by_batch_budget;`（**回滚前先 `SELECT sqlite_version()` 校验 ≥3.35**，评审 R-6）+ 手动 `DELETE FROM schema_migrations WHERE version=4`。数据安全：仅运维手动执行。

### 3.7 测试方案（T1.1 实现时执行）

1. **旧库升级**：构造 v3 形状库（含既有 runs/findings 数据）→ `initialize()` → 表结构正确（4 表 + runs 三新列）、既有数据完好、`user_version=4`、`schema_migrations` 含 4；
2. **新库初始化**：全新库 `initialize()` → 含 assets/batches + runs 三新列；
3. **幂等**：重复 `initialize()` 不报错、不重复迁移、数据无损坏；
4. **外键行为**：删除 asset 后 `runs.asset_id` 置 NULL（`ON DELETE SET NULL`，FK 恒开启）；
5. **SQL 安全**：全部参数绑定（迁移脚本无字符串拼接）；
6. **叠加路径（评审 R-4）**：复用既有 legacy 构造风格，构造含 `schema_migrations` v2/v3 记录的 v3 库 → 升级 v4（覆盖真实旧库升级）；删 `schema_migrations` v4 记录后重启 → 幂等重跑 v4（中断恢复）；
7. **大表迁移（评审 R-3）**：runs 含大量行（如 1000+）时 v4 迁移正确且数据无损（评估 ALTER 锁窗口）。

### 3.8 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性说明 |
|---|---|---|
| 评审 §4.13（assets/batches 走 schema_migrations、旧库升级、禁止内联改表） | 迁移 v4 + 版本化 `_migrate_` + 升级路径测试设计 | 一致 |
| 评审 §4.12（batch 预算帽 + `ai_skipped_by_batch_budget` 可审计） | `batches.max_ai_calls` 快照 + `ai_skipped_count` | 一致 |
| 方案 Phase 1（run 关联 asset_id/batch_id） | runs 加两可空外键列 | 一致 |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| 迁移破坏旧库数据 | 历史 run/finding 丢失 | 幂等迁移 + 升级测试先行 + ON DELETE SET NULL | 旧库文件保留 + 重建 |
| 大库迁移锁窗口（评审 R-3） | runs 量大时 ALTER 重建表耗时 | 升级测试含大表场景；文档注明排他锁窗口 | 非高峰执行迁移 |
| 预算快照过时 | batch 汇总与当前配置不符 | 快照只读（创建时固化），文档注明 | 不追溯 |

## 5. 依赖

- 前置：T0.7（配置段）；实现依赖 T1.1（本设计稿落地）。
