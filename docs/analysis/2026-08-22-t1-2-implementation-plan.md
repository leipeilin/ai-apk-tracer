# 任务实施方案：T1.2（资产注册表 registry.py）

> **任务编号**：T1.2
> **日期**：2026-08-22
> **依据大纲**：
> - 设计稿：`docs/analysis/2026-08-22-t0-8-implementation-plan.md`（assets 表结构）
> - 方案：`docs/analysis/2026-08-18-project-optimization-plan.md` Phase 1（资产注册）
> - 实施计划：`docs/analysis/2026-08-21-explorer-track-implementation-plan.md` T1.2
> **状态**：起草
> **前置依赖**：T1.1（assets 表迁移，已提交 `4fe6ede`）、T0.7（`assets` 配置段与 `resolved_assets_data_root()`）

---

## 1. 任务目标与范围

- **目标**：实现资产注册表 `backend/app/assets/registry.py`——资产 CRUD（package_name/APK 副本/sha256 去重/状态/最近 run 关联），SQL 全参数绑定。
- **范围**：
  - `backend/app/assets/__init__.py` + `registry.py`（AssetRegistry 类：register/get/list/update_status/link_run/delete）；
  - APK 副本入库（流式写 + 大小上限 + sha256 + ZIP 结构校验，复用 runs/storage 既有模式与工具）；
  - `backend/tests/test_asset_registry.py`。
- **非范围**：批量编排（T1.3）、API 端点与配置门禁（T1.4，`assets.enabled` 检查在 API 层）、前端（T1.5）、包名列表导入（T1.4 扩展 `source='package_list'`）。

## 2. 现状锚点

- **assets 表**（T1.1 已迁移）：`id/package_name/apk_filename/apk_sha256(UNIQUE)/source('local_upload')/status('ready')/last_run_id(REFERENCES runs ON DELETE SET NULL)/created_at/updated_at` + `idx_assets_status/idx_assets_created_at`。
- **APK 入库模式**（`runs/storage.py:53-95`）：流式写（1MB chunk）→ `max_apk_size_mb` 上限（ValidationError `APK_TOO_LARGE`）→ sha256 增量计算 → `validate_apk_zip`（结构校验）→ `os.replace` 原子落位 → 0o600 文件/0o700 目录权限。
- **id 风格**：`{UTC时间戳}_{sha256[:12]}_{uuid[:8]}`（storage.py:71）。
- **repository**：`connect()`（FK=ON + WAL + 事务上下文管理器）；CRUD 风格 = `with self.connect() as db` + 全参数绑定 + `NotFoundError/ConflictError/ValidationError`（errors.py:17-36）+ 白名单字段更新（update_run L393-397 先例）。
- **配置**：`AssetsSettings{enabled, max_concurrent_runs, data_root}` + `Settings.resolved_assets_data_root()`（T0.7）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/assets/__init__.py` | 新增 | 空包初始化 |
| `backend/app/assets/registry.py` | 新增 | AssetRegistry（CRUD + APK 副本入库） |
| `backend/tests/test_asset_registry.py` | 新增 | 注册/去重/查询/更新/删除/安全用例 |

### 3.2 `AssetRegistry` 类设计

```python
class AssetRegistry:
    """资产注册表：APK 副本（内容寻址）+ 元数据 CRUD（T0.8 assets 表）。

    registry 为纯领域模块：不读配置、不做 API 门禁（assets.enabled 检查在
    API 层，T1.4）；SQL 全部参数绑定（实施计划 T1.2 红线）；事务与 FK 由
    注入的 repository.connect() 管理（与 runs/findings 同库）。
    资产 id 与 run id 同风格（{UTC}_{sha[:12]}_{uuid[:8]}），docstring 此处
    标注防混淆（评审 R-5）。
    """

    def __init__(self, repository: SQLiteRepository, assets_root: Path, limits) -> None:
        # assets_root 由调用方传入 resolved_assets_data_root()；构造时建目录 0o700（评审 R-7）
        # limits 为 storage.StorageLimits 同源对象：max_apk_size_mb/max_zip_entries/
        # max_uncompressed_mb 三参数同源注入，避免与 run 入库行为分叉（评审 R-2）
        ...

    # --- 注册（APK 副本 + 元数据） ---
    def register(self, source: BinaryIO, filename: str, package_name: str) -> dict:
        """流式接收 APK，内容寻址落副本后登记元数据；同 sha256 重复注册抛 ConflictError。

        流程（对齐 runs/storage.py:53-95 既有模式）：
        1. 入参校验：package_name 非空（ValidationError）、filename 经 Path().name
           取 basename + `.apk` 扩展名校验（复用 storage.py:50 先例 INVALID_APK_EXTENSION，
           评审 R-3/R-7）；
        2. 临时文件流式写（1MB chunk）+ limits.max_apk_size_mb 上限（APK_TOO_LARGE）；
        3. sha256 增量计算 + validate_apk_zip（limits 三参数同源）结构校验；
        4. 副本内容寻址落位 assets_root/<sha256[:2]>/<sha256>/<basename>（os.replace
           原子写 + 0o600）；异常时 finally 清理临时文件（对齐 storage 模式）；
        5. INSERT assets 行；同 sha256 UNIQUE 冲突 → ConflictError("ASSET_ALREADY_REGISTERED",
           details={"asset_id": 既有id}）。**冲突时不清理副本**（评审 R-1）：内容寻址天然
           幂等——同 sha256 内容必然一致，覆盖写无害，保留即安全；并发场景保留更是唯一
           正确行为（避免删掉他方在用副本）。
        """

    # --- 查询 ---
    def get(self, asset_id: str) -> dict: ...          # NotFoundError
    def list_assets(self, status: str | None = None) -> list[dict]: ...  # status 过滤 + created_at DESC

    # --- 更新（白名单字段，对齐 update_run 先例） ---
    def update_status(self, asset_id: str, status: str) -> dict: ...   # ready/scanning/error
    def link_run(self, asset_id: str, run_id: str) -> dict: ...:
        # 先校验 run 存在（repository.get_run 不存在抛 NotFoundError——避免 FK 裸
        # IntegrityError 逃逸，评审 R-4），再 UPDATE assets.last_run_id（T1.3 编排调用）

    # --- 删除 ---
    def delete(self, asset_id: str) -> None: ...:
        # 先删 DB 记录（成功后副本即孤儿），再删副本目录；删目录复用
        # runs/storage.safe_remove_tree（防软链接攻击面，评审 R-4）；目录删除失败
        # 仅记录日志不回滚 DB（孤儿目录可被 storage.cleanup 扫描兜底）

    # --- 内部 ---
    def _asset_row(self, row) -> dict: ...              # 行规范化（snake_case 直传 + apk_path 计算字段）
    def _apk_path(self, sha256: str, basename: str) -> Path: ...  # 普通方法（评审 R-5）：assets_root/<sha[:2]>/<sha256>/<basename>
```

### 3.3 关键设计决策

1. **APK 副本内容寻址**（`assets_root/<sha256[:2]>/<sha256>/<filename>`）：天然防重复（同 sha256 唯一资产，UNIQUE 约束同源）、删除即删目录（无引用计数复杂度）；区别于 run 的 `runs/<run_id>/input/app.apk`（run 隔离副本语义不同，互不影响）。
2. **重复注册语义**：`ConflictError("ASSET_ALREADY_REGISTERED", details={"asset_id": 既有id})`——显式冲突（409）而非静默幂等返回，调用方（T1.4 API/前端）可提示"该 APK 已注册"并跳转既有资产；**冲突时保留副本**（内容寻址幂等，评审 R-1）。
3. **registry 与 repository 关系**：注入 `SQLiteRepository` 复用 `connect()`（事务/FK/WAL 统一管理），assets 表 SQL 留在 registry（领域内聚，不膨胀 repository）；**不新建独立连接**（避免双写同库的连接管理重复）。
4. **`package_name` 来源**：注册时调用方提供（必填，T1.4 API form 字段）——注册阶段不解析 APK 内 manifest（反编译前置，过重）；T1.5 前端表单承载。
5. **`source` 字段**：本任务仅 `local_upload`；`package_list` 归 **T1.4** 导入扩展（T0.8 设计稿"T1.2 扩展"为笔误，评审 R-6 加注澄清）。
6. **`max_apk_size_mb` 上限复用**：`storage.limits` 同源配置（`storage.max_apk_size_mb`），registry 构造时由调用方传入上限值（不直接读 settings，保持纯领域）。
7. **`enabled` 门禁归属**：registry 不管配置（T1.4 API 层检查 `settings.assets.enabled`，未启用返回明确错误）。
8. **大小上限/zip 校验的错误码**：复用 `APK_TOO_LARGE` 与 `validate_apk_zip` 既有错误码（与 run 入库一致的用户语义）。

### 3.4 测试方案（`test_asset_registry.py`）

1. **test_register_persists_asset_and_copy**：注册（内存 APK 字节流）→ 元数据字段正确（id 风格/package_name/sha256/source/status='ready'）+ 副本落位内容寻址路径 + 内容一致；
2. **test_register_duplicate_sha256_conflict**：同字节流二次注册 → `ConflictError` 且 details 含既有 asset_id + **既有副本仍存在且内容一致**（评审 R-1 断言补强：不清副本）；
3. **test_register_rejects_oversize**：超过上限字节流 → `ValidationError(APK_TOO_LARGE)` + 无残留（临时文件清理，对齐 storage finally 模式）；
4. **test_register_rejects_non_zip**：非 ZIP 魔数字节流 → `validate_apk_zip` 的既有 ValidationError + 无残留；
4b. **test_register_rejects_bad_inputs**（N-1/N-2/N-5，评审 R-3）：空 package_name → ValidationError；`filename="../../x.apk"` 路径穿越 → ValidationError（basename + 扩展名校验）；非 `.apk` 扩展名 → `INVALID_APK_EXTENSION`；0 字节流 → zip 校验拒绝；
5. **test_get_not_found**：`get("missing")` → `NotFoundError`；
6. **test_list_assets_filters_by_status**：两个资产（ready/error）→ `list_assets()` 全量 + `list_assets(status="ready")` 过滤 + DESC 排序；
7. **test_update_status_whitelist**：`update_status` 成功路径 + 非法 status（非白名单枚举）拒绝；`update_status` 不存在 id → NotFoundError；
8. **test_link_run_updates_last_run_id**：`link_run` 后 `get()["last_run_id"]` 更新；删除 run（repository.delete_run_record）→ asset.last_run_id 置 NULL（FK SET NULL，T1.1 行为联动）；
9. **test_delete_removes_record_and_copy**：删除后记录 NotFound + 副本目录不存在；
10. **test_sql_injection_safety**：`get("a'; DROP TABLE assets;--")` 等注入样例 → 安全返回 NotFoundError（参数绑定实证）。

### 3.5 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性 |
|---|---|---|
| 实施计划 T1.2（package name/apk path/sha256/来源/状态/最近 run_id；SQL 全参数绑定） | §3.2 六方法逐项覆盖；参数绑定为红线（测试 10 实证） | 一致 |
| T0.8 设计稿 assets 表（UNIQUE 去重/last_run_id FK/仅建结构→语义值由功能写入） | 注册写 `source='local_upload'`（首个功能语义值）；FK 行为测试 8 验证 | 一致 |
| 方案 Phase 1（资产注册、APK 归档） | 内容寻址副本 + 大小/zip 校验复用 | 一致 |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| 副本写入中断残留 | 孤儿文件 | 临时文件 + `os.replace` 原子落位 + finally 清理临时文件（对齐 storage 模式） | 内容寻址路径幂等（重复写同内容无害） |
| 同库双模块写（repository + registry） | 锁竞争 | 统一走 repository.connect()（WAL + 短事务） | 无（既有机制） |
| UNIQUE 冲突竞态（并发注册同 APK） | 一个成功一个 ConflictError | UNIQUE 约束兜底；**冲突保留副本**（评审 R-1，防误删在用副本） | 既有资产生效 |

## 5. 依赖

- 前置：T1.1（assets 表）、T0.7（assets 配置）；运行时复用 `apk_validation.validate_apk_zip`、`errors`、`repository.connect`。
