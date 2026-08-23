"""提供扫描任务、发现项复核、报告与清理相关的 HTTP API。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile, status
from fastapi.responses import PlainTextResponse

from app.analysis.orchestrator import ScanOrchestrator
from app.api.models import BatchCreateRequest, CleanupRequest, ReviewRequest
from app.findings.report import build_report_payload, render_markdown
from app.reporting.generator import generate_report_document, save_report_document
from app.runs.cleanup import CleanupService
from app.runs.run_config import build_run_config
from app.shared.errors import AppError, NotFoundError, ValidationError
from app.shared.logging import trace_id_var

router = APIRouter()


def _safe_config_snapshot(config: object) -> dict:
    """仅暴露前端解释任务所需的 AI 配置字段。"""

    if not isinstance(config, dict) or not isinstance(config.get("ai"), dict):
        return {}
    ai = config["ai"]
    return {"ai": {
        key: ai[key]
        for key in ("enabled", "allow_external_code", "provider_kind", "model")
        if key in ai
    }}


def _public_run(run: dict) -> dict:
    """返回不包含服务地址、密钥名或其他内部配置的任务副本。"""

    public = dict(run)
    public["config"] = _safe_config_snapshot(run.get("config"))
    manifest = public.get("manifest")
    if isinstance(manifest, dict):
        public["manifest"] = {**manifest, "config": _safe_config_snapshot(manifest.get("config"))}
    return public


def _finding_artifact_paths(
    directory: Path, finding: dict, suffix: str
) -> list[Path]:
    """按 scoped ID 优先、base ID 兜底返回 finding 历史兼容路径。"""

    identifiers = [str(finding["id"]), str(finding.get("base_id") or "")]
    return [
        directory / f"{identifier}{suffix}"
        for index, identifier in enumerate(identifiers)
        if identifier and identifier not in identifiers[:index]
    ]


def _existing_or_scoped_path(directory: Path, finding: dict, suffix: str) -> Path:
    candidates = _finding_artifact_paths(directory, finding, suffix)
    return next(
        (path for path in candidates if path.is_file() and not path.is_symlink()),
        candidates[0],
    )


@router.get("/health")
def health() -> dict:
    """返回进程存活状态，不触发外部依赖检查。"""

    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request) -> dict:
    """探测 SQLite 可用性并返回服务就绪状态。"""

    repository = request.app.state.repository
    ok = repository.ping()
    return {"status": "ok" if ok else "degraded", "checks": {"sqlite": "ok" if ok else "failed"}}


@router.get("/api/runs")
def list_runs(request: Request) -> dict:
    """按创建时间倒序返回扫描任务列表。"""

    return {"items": [_public_run(run) for run in request.app.state.repository.list_runs()]}


# FastAPI UploadFile 参数的模块级单例（B008：不得在参数默认值中调用函数）
_APK_UPLOAD = File(...)


@router.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
def create_run(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = _APK_UPLOAD,
    authorized: bool = Form(...),
    source_analysis_enabled: bool = Form(default=True),
) -> dict:
    """接收已授权 APK，完成安全入库后异步启动扫描。

    未确认合法测试授权时拒绝创建任务。
    """

    if authorized is not True:
        raise ValidationError("必须确认拥有合法测试授权", "AUTHORIZATION_CONFIRMATION_REQUIRED")
    settings = request.app.state.settings
    storage = request.app.state.storage
    repository = request.app.state.repository
    trace_id = trace_id_var.get()
    config = build_run_config(settings, source_analysis_enabled=source_analysis_enabled)
    ingested = storage.ingest(file.file, file.filename or "upload.apk", trace_id, config)
    run = repository.create_run({
        "id": ingested["id"],
        "trace_id": trace_id,
        "status": "queued",
        "stage": "queued",
        "apk_filename": Path(file.filename or "upload.apk").name,
        "apk_sha256": ingested["sha256"],
        "config": config,
        "manifest_path": str(storage.run_dir(ingested["id"]) / "manifest.json"),
    })
    orchestrator = ScanOrchestrator(
        settings,
        repository,
        storage,
        request.app.state.ai_runtime,
    )
    background_tasks.add_task(orchestrator.scan, run["id"])
    return _public_run(run)


@router.get("/api/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    """返回任务状态，并在可用时补充清单与应用包信息。"""

    run = request.app.state.repository.get_run(run_id)
    try:
        manifest = request.app.state.storage.read_manifest(run_id)
        run["manifest"] = manifest
        run["pipeline_version"] = manifest.get("pipeline_version", run.get("pipeline_version", "1.0.0"))
        run["schema_version"] = manifest.get("schema_version", run.get("schema_version", "1.0.0"))
        run["artifact_schema_versions"] = manifest.get("artifact_schema_versions", {})
        run["stages"] = manifest.get("stages", [])
        run["file_size"] = manifest.get("apk", {}).get("size_bytes")
        index_manifest = request.app.state.storage.run_dir(run_id) / "index" / "manifest.json"
        if index_manifest.is_file() and not index_manifest.is_symlink():
            parsed = json.loads(index_manifest.read_text("utf-8"))
            run["package_name"] = parsed.get("package")
            run["app_name"] = parsed.get("package")
    except (AppError, OSError, ValueError):
        run["manifest"] = None
        run["stages"] = []
    return _public_run(run)


@router.get("/api/runs/{run_id}/findings")
def list_findings(run_id: str, request: Request) -> dict:
    """返回指定任务的全部聚合发现项。"""

    return {"items": request.app.state.repository.list_findings(run_id)}


@router.get("/api/runs/{run_id}/explorer/candidates")
def explorer_candidates(run_id: str, request: Request) -> dict:
    """探索候选人工队列（T2.10，方案 §2.0/§5.4）。

    partial/unverified/pending 主体（validated 仅计数对照——已并入主链）；
    服务端预排序：置信度 ↓ → deep_dive 证据 ↓ → 跳回查完整度 ↓。
    产物缺失/损坏 → 空态（探索轨未启用是常态，非 404）。
    """

    request.app.state.repository.get_run(run_id)  # 404 校验
    from app.analysis.explorer_queue import build_explorer_queue

    candidates_path = (
        request.app.state.storage.run_dir(run_id) / "explorer" / "candidates.json"
    )
    raw: list = []
    if candidates_path.is_file():
        try:
            loaded = json.loads(candidates_path.read_text("utf-8"))
            if isinstance(loaded, list):
                raw = loaded
        except (json.JSONDecodeError, OSError):
            raw = []
    return build_explorer_queue(raw)


@router.get("/api/findings/{finding_id}/slice")
def finding_slice(finding_id: str, request: Request) -> dict:
    """返回发现项最新切片；中间切片已清理时回退到报告证据副本。"""

    finding = request.app.state.repository.get_finding(finding_id)
    slice_id = finding.get("slice_id")
    if not isinstance(slice_id, str) or not re.fullmatch(r"slice_[0-9a-f]{20}", slice_id):
        raise NotFoundError("finding slice", finding_id)
    run_dir = request.app.state.storage.run_dir(finding["run_id"])
    slice_dir = run_dir / "slices" / slice_id
    if not slice_dir.is_symlink() and slice_dir.is_dir():
        rounds = sorted(path for path in slice_dir.glob("round-*.json") if path.is_file() and not path.is_symlink())
        if rounds:
            return {
                "finding_id": finding["id"],
                "slice_id": slice_id,
                "round_count": len(rounds),
                "latest_round": rounds[-1].name,
                "slice": json.loads(rounds[-1].read_text("utf-8")),
                "source": "live_slice",
            }
    evidence_dir = run_dir / "reports" / "evidence"
    for evidence_path in _finding_artifact_paths(evidence_dir, finding, ".json"):
        if not evidence_path.is_file() or evidence_path.is_symlink():
            continue
        evidence = json.loads(evidence_path.read_text("utf-8"))
        if isinstance(evidence.get("context_slice"), dict):
            return {
                "finding_id": finding["id"],
                "slice_id": slice_id,
                "round_count": len(evidence["context_slice"].get("request_history", [])) + 1,
                "latest_round": "evidence-closure",
                "slice": evidence["context_slice"],
                "source": "report_evidence",
            }
    raise NotFoundError("finding slice", finding_id)


@router.patch("/api/findings/{finding_id}/review")
def review_finding(finding_id: str, body: ReviewRequest, request: Request) -> dict:
    """更新发现项人工复核状态并保留状态变更历史。"""

    repository = request.app.state.repository
    updated = repository.review_finding(
        finding_id,
        body.status.value,
        body.reason,
        actor=body.actor,
        request_id=body.request_id,
        basis=body.basis,
        expected_status=body.expected_status.value if body.expected_status else None,
    )
    run_dir = request.app.state.storage.run_dir(updated["run_id"])
    finding_path = _existing_or_scoped_path(run_dir / "findings", updated, ".json")
    finding_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), "utf-8")
    evidence_path = _existing_or_scoped_path(
        run_dir / "reports" / "evidence", updated, ".json"
    )
    if evidence_path.is_file() and not evidence_path.is_symlink():
        evidence = json.loads(evidence_path.read_text("utf-8"))
        evidence["finding"] = updated
        evidence["review_synced_at"] = updated.get("updated_at")
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), "utf-8")
    report_path = _existing_or_scoped_path(run_dir / "reports", updated, ".md")
    if report_path.is_file() and not report_path.is_symlink():
        stale_path = report_path.with_suffix(".stale.json")
        stale_path.write_text(json.dumps({
            "finding_id": updated["id"],
            "reason": "review_status_changed",
            "updated_at": updated.get("updated_at"),
        }, ensure_ascii=False, indent=2), "utf-8")
    return updated


@router.get("/api/findings/{finding_id}/report", response_class=PlainTextResponse)
def finding_report(finding_id: str, request: Request) -> PlainTextResponse:
    """生成并保存发现项 Markdown 报告；L1 提示项不可生成正式报告。"""

    repository = request.app.state.repository
    finding = repository.get_finding(finding_id)
    run = repository.get_run(finding["run_id"])
    run["manifest"] = request.app.state.storage.read_manifest(run["id"])
    payload = build_report_payload(finding, run)
    markdown = render_markdown(payload)
    reports_dir = request.app.state.storage.run_dir(run["id"]) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    report_path = _existing_or_scoped_path(reports_dir, finding, ".md")
    report_path.write_text(markdown, "utf-8")
    stale_path = report_path.with_suffix(".stale.json")
    if stale_path.is_file() and not stale_path.is_symlink():
        stale_path.unlink()
    return PlainTextResponse(markdown, media_type="text/markdown; charset=utf-8")


@router.post("/api/findings/{finding_id}/report-draft")
async def generate_finding_report_draft(finding_id: str, request: Request) -> dict:
    """生成报告草稿 + PoC 骨架 + 修复建议（M3-1——AI 草稿与确定性证据分离）。

    门禁：仅 confirmed finding（L1/informational 拒绝）；allow_executable_poc
    必须 false（零可执行产物）。落盘 run_dir/reports/drafts/{finding_id}.json。

    async：为 M3-2 真 prompt 协议的 async provider 铺路（评审 R-9——当前
    repository/落盘为同步 IO，事件循环阻塞可接受，届时统一异步化）。
    """
    repository = request.app.state.repository
    finding = repository.get_finding(finding_id)
    # M3-2（评审 R-4）：真协议接线——共享 runtime transport；AI 失败由
    # generator 降级回投影（报告永不因 AI 阻塞）
    analyzer = request.app.state.ai_runtime.create_analyzer()
    document = await generate_report_document(
        finding, settings=request.app.state.settings.report, analyzer=analyzer)
    run_dir = request.app.state.storage.run_dir(finding["run_id"])
    save_report_document(document, run_dir)
    return document.model_dump(mode="json")


@router.post("/api/runs/{run_id}/cleanup")
def cleanup_run(run_id: str, body: CleanupRequest, request: Request) -> dict:
    """执行任务数据清理，并同步移除不再有效的数据库记录。

    v2026-08-09：``delete_run`` 模式的数据库同步逻辑整合到 CleanupService 内部
    （接受可选 ``repository`` 参数）；本端点保留 ``clear_sensitive_content`` 的
    findings 清理（属于清理模式但不动 run 记录本身）。
    """

    repository = request.app.state.repository
    repository.get_run(run_id)
    result = CleanupService(request.app.state.storage, repository).cleanup(
        run_id, body.mode.value, body.confirm_delete
    )
    if body.mode.value == "clear_sensitive_content":
        repository.clear_findings(run_id)
    return result


# ----------------------------------------------------------------------
# 资产与批量扫描（T1.4；门禁/授权/脱敏设计见
# docs/analysis/2026-08-22-t1-4-implementation-plan.md）
# ----------------------------------------------------------------------


def _require_assets_enabled(request: Request) -> None:
    """assets.enabled 门禁（T1.2 决策：门禁归 API 层，领域模块无门禁）。

    503 语义=功能未启用（非请求校验 422 / 不存在 404）。
    """

    if not request.app.state.settings.assets.enabled:
        raise AppError("资产批量功能未启用（assets.enabled=false）", "ASSETS_DISABLED", 503)


def _public_asset(asset: dict) -> dict:
    """脱敏（T1.2 评审遗留）：apk_path 为服务端路径，不外泄。"""

    return {key: value for key, value in asset.items() if key != "apk_path"}


def _public_batch(batch: dict) -> dict:
    """脱敏（T1.4 评审 R-1）：剔除 assets_json 原始列（解析后 assets 已在）。"""

    return {key: value for key, value in batch.items() if key != "assets_json"}


@router.get("/api/assets")
def list_assets(request: Request) -> dict:
    """按创建时间倒序返回资产列表。"""

    _require_assets_enabled(request)
    items = [_public_asset(asset) for asset in request.app.state.asset_registry.list_assets()]
    return {"items": items}


@router.post("/api/assets/import", status_code=status.HTTP_201_CREATED)
def import_asset(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    package_name: Annotated[str, Form(...)],
    authorized: Annotated[bool, Form(...)],
) -> dict:
    """导入本地 APK 资产（同步注册：流式副本 + sha256/大小/ZIP 校验复用 registry）。

    未确认合法测试授权时拒绝导入（与 create_run 同级安全语义，T1.4 D2）；
    重复 sha256 返回 409（details.asset_id 供前端跳转既有资产）。
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
    """返回批量进度与汇总（runs 聚合 + 降级原因分解）。"""

    _require_assets_enabled(request)
    return _public_batch(request.app.state.batch_orchestrator.get_batch(batch_id))
