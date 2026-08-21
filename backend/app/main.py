"""创建 FastAPI 应用并装配日志、存储、路由及统一异常处理。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from app.analysis.ai_runtime import AIRuntime
from app.api.routes import router
from app.assets.batch import BatchOrchestrator
from app.assets.registry import AssetRegistry
from app.config import WORKSPACE_ROOT, Settings, get_settings
from app.runs.storage import RunStorage
from app.shared.errors import AppError
from app.shared.logging import TraceIdMiddleware, configure_logging, trace_id_var
from app.shared.repository import SQLiteRepository

# 启动时加载 .env 到 os.environ，使 os.environ.get() 能读到密钥
load_dotenv()


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建并返回完成依赖装配的应用实例。"""

    settings = settings or get_settings()
    configure_logging(settings.log_level)
    repository = SQLiteRepository(settings.resolved_database_path())
    repository.initialize()
    storage = RunStorage(settings.resolved_data_root(), settings.storage)
    ai_runtime = AIRuntime(settings.ai)
    # 资产/批量组装（T1.4 D6：无条件组装，运行时门禁在 API 层）
    asset_registry = AssetRegistry(repository, storage, settings.resolved_assets_data_root())
    batch_orchestrator = BatchOrchestrator(settings, repository, storage, ai_runtime, asset_registry)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await ai_runtime.aclose()

    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
    app.state.settings = settings
    app.state.repository = repository
    app.state.storage = storage
    app.state.ai_runtime = ai_runtime
    app.state.asset_registry = asset_registry
    app.state.batch_orchestrator = batch_orchestrator
    app.add_middleware(TraceIdMiddleware)
    app.include_router(router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """为所有 HTTP 响应附加最小浏览器安全与禁缓存头。"""

        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    def request_trace_id(request: Request) -> str:
        """从请求状态或当前上下文取得跟踪标识。"""

        return getattr(request.state, "trace_id", None) or trace_id_var.get()

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        """将预期应用异常转换为稳定错误结构。"""

        trace_id = request_trace_id(request)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                "trace_id": trace_id,
            },
            headers={"X-Trace-ID": trace_id},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        """将请求模型校验失败转换为带跟踪标识的 422 响应。"""

        trace_id = request_trace_id(request)
        errors = []
        for error in exc.errors():
            sanitized = dict(error)
            if isinstance(sanitized.get("ctx"), dict):
                sanitized["ctx"] = {key: str(value) for key, value in sanitized["ctx"].items()}
            errors.append(sanitized)
        return JSONResponse(
            status_code=422,
            content={
                "error": {"code": "REQUEST_VALIDATION_ERROR", "message": "请求参数校验失败", "details": {"errors": errors}},
                "trace_id": trace_id,
            },
            headers={"X-Trace-ID": trace_id},
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        """记录未处理异常并返回不泄露内部细节的 500 响应。"""

        trace_id = request_trace_id(request)
        logging.getLogger(__name__).exception("未处理异常", extra={"trace_id": trace_id})
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "内部错误"}, "trace_id": trace_id},
            headers={"X-Trace-ID": trace_id},
        )

    frontend_dist = WORKSPACE_ROOT / "frontend" / "dist"
    if frontend_dist.is_dir():
        # SPA 静态托管 + 深链 fallback（T1.5 受控修复）：StaticFiles(html=True)
        # 对 /assets、/runs/:id 等客户端路由深链返回 404——改为 catch-all：
        # 真实静态文件直出（含路径穿越防护），其余未知路径回退 index.html
        # （react-router 客户端接管）。
        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str) -> FileResponse:
            candidate = (frontend_dist / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(frontend_dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(frontend_dist / "index.html")

    return app


app = create_app()
