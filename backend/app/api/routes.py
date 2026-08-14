"""提供扫描任务、发现项复核、报告与清理相关的 HTTP API。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile, status
from fastapi.responses import PlainTextResponse

from app.analysis.orchestrator import ScanOrchestrator
from app.api.models import CleanupRequest, ReviewRequest
from app.findings.report import build_report_payload, render_markdown
from app.runs.cleanup import CleanupService
from app.shared.errors import NotFoundError, ValidationError
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


@router.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
def create_run(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
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
    config = {
        "analysis_platform_api": settings.analysis_platform_api,
        "source_analysis": {
            **settings.source_analysis.model_dump(mode="json"),
            "enabled": source_analysis_enabled,
        },
        "ai": {
            "enabled": settings.ai.enabled,
            "allow_external_code": settings.ai.allow_external_code,
            "provider_kind": "openai-compatible",
            "model": settings.ai.model,
        },
    }
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
    except Exception:
        run["manifest"] = None
        run["stages"] = []
    return _public_run(run)


@router.get("/api/runs/{run_id}/findings")
def list_findings(run_id: str, request: Request) -> dict:
    """返回指定任务的全部聚合发现项。"""

    return {"items": request.app.state.repository.list_findings(run_id)}


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
