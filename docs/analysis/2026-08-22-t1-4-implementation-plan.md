# 任务实施方案：T1.4（资产/批量 API 端点）

> **任务编号**：T1.4
> **日期**：2026-08-22
> **依据大纲**：
> - 方案：`docs/analysis/2026-08-18-project-optimization-plan.md` Phase 1 L145-149（四个端点）
> - 实施计划：`docs/analysis/2026-08-21-explorer-track-implementation-plan.md` T1.4
> - T1.2/T1.3 评审遗留预判项（authorized 语义、apk_path 脱敏）
> **状态**：起草
> **前置依赖**：T1.2（AssetRegistry）、T1.3（BatchOrchestrator）

---

## 1. 任务目标与范围

- **目标**：实现资产/批量四个端点——`GET /api/assets`、`POST /api/assets/import`、`POST /api/batches`、`GET /api/batches/{batch_id}`；`app.state` 组装 AssetRegistry/BatchOrchestrator。
- **范围**：
  - `routes.py` 新增四端点 + `_require_assets_enabled` 门禁 + `_public_asset` 脱敏；
  - `models.py` 新增 `BatchCreateRequest`；
  - `main.py` 组装 `app.state.asset_registry`/`app.state.batch_orchestrator`；
  - `tests/test_assets_api.py`。
- **非范围**：前端页面（T1.5）；`GET /api/batches` 列表端点（方案未要求，T1.5 需要时加）；`assets.max_concurrent_runs` 资产级并发治理（多 batch 并存，Phase 1 单批假设已文档化）。

## 2. 现状锚点

- **端点风格**（routes.py）：`request.app.state.*` 注入；`create_run`（L91-140）= multipart form + `authorized` Form 强制确认 + `background_tasks.add_task(orchestrator.scan, run_id)` + 202。
- **错误体系**：`AppError(message, code, status_code, details)` 基类可直接实例化（errors.py:9）；统一 handler（main.py:66-78）。
- **AssetsSettings.enabled 默认 False**（config.py:217）——门禁测试需显式开启。
- **registry/batch 交付物**：register（ValidationError/ConflictError 含 details.asset_id）/list_assets（含 apk_path 计算字段——服务端路径，须脱敏）/create_batch（NotFoundError/ValidationError）/get_batch（runs 聚合汇总）。
- **models.py 风格**：Pydantic BaseModel + Field 约束 + model_validator 业务校验。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/api/routes.py` | 修改 | 四端点 + 门禁 + `_public_asset`/`_public_batch` 脱敏 + 补 `AppError`/`status` 导入（评审 R-6） |
| `backend/app/api/models.py` | 修改 | `BatchCreateRequest`（authorized + asset_ids） |
| `backend/app/main.py` | 修改 | app.state 组装 registry/batch_orchestrator（受控越界，评审 §6） |
| `backend/tests/test_assets_api.py` | 新增 | 门禁/导入/批量端点测试（data_root 显式 tmp 隔离，评审 R-2） |

### 3.2 端点设计

```python
# routes.py（assets/batches 段，置于 runs 端点之后）

def _require_assets_enabled(request: Request) -> None:
    """assets.enabled 门禁（T1.2 决策：门禁归 API 层）。503 语义=功能未启用。"""
    if not request.app.state.settings.assets.enabled:
        raise AppError("资产批量功能未启用（assets.enabled=false）", "ASSETS_DISABLED", 503)

def _public_asset(asset: dict) -> dict:
    """脱敏（T1.2 评审遗留）：apk_path 为服务端路径，不外泄。"""
    return {key: value for key, value in asset.items() if key != "apk_path"}


def _public_batch(batch: dict) -> dict:
    """脱敏（评审 R-1）：剔除 assets_json 原始列（解析后 assets 已在），防双份字段固化进 API 契约。"""
    return {key: value for key, value in batch.items() if key != "assets_json"}


@router.get("/api/assets")
def list_assets(request: Request) -> dict:
    """资产列表（按 created_at 倒序，registry.list_assets）。"""
    _require_assets_enabled(request)
    return {"items": [_public_asset(a) for a in request.app.state.asset_registry.list_assets()]}


@router.post("/api/assets/import", status_code=status.HTTP_201_CREATED)
def import_asset(
    request: Request,
    file: UploadFile = File(...),
    package_name: str = Form(...),
    authorized: bool = Form(...),
) -> dict:
    """导入本地 APK 资产（同步注册：流式副本 + sha256/大小/zip 校验复用 registry）。

    authorized 强制确认（D2）；重复 sha256 → 409（details.asset_id 供前端跳转）。
    """
    _require_assets_enabled(request)
    if authorized is not True:
        raise ValidationError("必须确认拥有合法测试授权", "AUTHORIZATION_CONFIRMATION_REQUIRED")
    asset = request.app.state.asset_registry.register(
        file.file, file.filename or "upload.apk", package_name
    )
    return _public_asset(asset)


@router.post("/api/batches", status_code=status.HTTP_202_ACCEPTED)
def create_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    body: BatchCreateRequest,
) -> dict:
    """创建批量扫描（秒回 pending + 资产快照）并异步启动编排。"""
    _require_assets_enabled(request)
    if body.authorized is not True:
        raise ValidationError("必须确认拥有合法测试授权", "AUTHORIZATION_CONFIRMATION_REQUIRED")
    batch = request.app.state.batch_orchestrator.create_batch(body.asset_ids)
    background_tasks.add_task(request.app.state.batch_orchestrator.run_batch, batch["id"])
    return _public_batch(batch)


@router.get("/api/batches/{batch_id}")
def get_batch(batch_id: str, request: Request) -> dict:
    """批量进度与汇总（runs 聚合 + 降级原因分解）。"""
    _require_assets_enabled(request)
    return _public_batch(request.app.state.batch_orchestrator.get_batch(batch_id))
```

```python
# models.py 新增
class BatchCreateRequest(BaseModel):
    """创建批量扫描请求（authorized 与 asset_ids 同体提交，D2）。"""
    authorized: bool
    asset_ids: list[str] = Field(min_length=1, max_length=100)
```

### 3.3 关键设计决策

**D1：包名列表导入（`source='package_list'`）Phase 1 不实现**
- 方案 L147 提及"导入本地 APK 或包名列表"，但：assets 表 `apk_filename/apk_sha256` NOT NULL（T0.8 结构）；无 APK 文件的包名资产无法进入批量扫描（batch 流程从资产副本 ingest）；方案 Phase 1 验收（L160-164）仅覆盖"本地 APK 导入"；
- 方案 L138 目标句"给定 package list，批量创建 run"的"按包批量"语义由 **asset_ids 子集**承载（POST /api/batches 可指定任意资产子集=按包名筛选后的集合；按包名筛选资产由前端组合 `GET /api/assets?package` 类查询，T1.5 评估）；
- **决策**：T1.4 仅实现 APK 上传导入（`local_upload`）；包名列表导入延后至有真实场景时先扩展表结构（nullable 化或独立表）再实现。实施计划的 T1.4 行含"包名列表"字样——按验收实质收窄，记录偏差。

**D2：authorized 授权确认覆盖 import 与 batches**
- 单 run 上传（create_run L104）已强制授权确认；资产导入是"持有 APK"行为、批量扫描是"分析行为"——与单 run 同级的安全语义（T1.3 评审遗留预判项落地）；
- import 用 Form 字段（multipart 请求）；batches 用 JSON body 字段（无文件）。

**D3：门禁错误用 AppError 直连实例（503, ASSETS_DISABLED）**
- 功能未启用非请求校验错误（422 不当）亦非不存在（404 不当）；503（Service Unavailable）语义最准；
- 不新增异常子类（单一使用点，AppError 基类可直接实例化——errors.py:9 契约支持）。

**D4：import 同步返回 201（非 202）**
- 注册（流式副本拷贝）在请求内同步完成（同 create_run 的 ingest 模式），无后续异步阶段——202 语义（已接受待处理）不准确，201（Created）准确；batches 是异步编排（BackgroundTask）→ 202（与 create_run 一致）。

**D5：`GET /api/batches`（列表）不实现**
- 方案 API 清单（L146-149）无此端点；前端按 batch_id 查看（从资产 last_run_id/批次跳转）；T1.5 若需列表再补（避免无消费方的过度设计）。

**D6：app.state 组装无条件执行（enabled=False 亦组装）**
- 领域模块无门禁（T1.2/T1.3 决策）；组装轻量（registry mkdir 空目录无害）；运行时门禁在端点层（D3）。

### 3.4 测试方案（`test_assets_api.py`）

TestClient + `Settings(assets=AssetsSettings(enabled=True), ...)`；batch 端点测试将 `app.state.batch_orchestrator.run_batch` 替换为 no-op（防真实 decompile 重执行——编排逻辑已在 test_batch.py 覆盖，API 层只验协议）：

1. **test_assets_endpoints_disabled_by_default**：enabled=False → 四端点全部 503 `ASSETS_DISABLED`（含默认配置回归）；
2. **test_import_asset_roundtrip**：导入成功 201 + 响应无 `apk_path`（脱敏）+ `GET /api/assets` 列表可见 + DB 落库；
3. **test_import_requires_authorization**：authorized=False → 422 `AUTHORIZATION_CONFIRMATION_REQUIRED`（不落库）；
4. **test_import_duplicate_conflict**：同 APK 二次导入 → 409 `ASSET_ALREADY_REGISTERED` + details.asset_id；
5. **test_import_rejects_invalid_inputs**：空 package_name / 非 .apk / 非 ZIP → 422（registry 校验传导）；
6. **test_create_batch_returns_pending**：JSON body → 202 + pending + assets 快照 + run_batch 被调度（no-op 替身记录调用）；
7. **test_create_batch_requires_authorization**：authorized=False → 422；
8. **test_create_batch_missing_asset**：不存在的 asset_id → 404；
9. **test_get_batch_summary**：创建后 GET → 汇总字段（total_runs=0 等）；missing → 404；
10. **test_batch_request_model_validation**：asset_ids 空列表 → 422（REQUEST_VALIDATION_ERROR）。

### 3.5 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性 |
|---|---|---|
| 方案 L146-149 四端点 | §3.2 逐一对应 | 一致（batches 列表 D5 不做——清单本身无此端点） |
| 方案 L147 包名列表导入 | D1 收窄为 APK 导入（验收实质对齐，偏差记录） | 偏差（显式） |
| 实施计划 T1.4（校验 sha256 与大小上限） | registry.register 复用（T1.2 已实现，API 层零重复） | 一致 |
| T1.2 评审遗留（apk_path 脱敏） | `_public_asset` | 一致 |
| T1.3 评审遗留（authorized 适用性） | D2：import/batches 均强制 | 一致 |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| import 大文件同步拷贝阻塞请求 | 请求慢 | 与 create_run 的 ingest 同模式（既有行为）；大小上限兜底（APK_TOO_LARGE） | 无 |
| background run_batch 与响应竞态（batches） | 响应含 pending，执行即刻开始 | create_batch 返回 pending + run_batch 抢占（T1.3 已防 TOCTOU） | 无 |
| enabled=False 时 ASSETS_DISABLED 503 语义 | 前端需处理 | T1.5 前端按 code 分支提示 | 无 |

## 5. 依赖

- 前置：T1.2/T1.3 交付物；main.py 组装（本任务）。
