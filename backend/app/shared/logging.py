"""提供结构化日志配置与贯穿请求上下文的跟踪标识。"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from datetime import UTC, datetime

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")


class JsonFormatter(logging.Formatter):
    """将日志记录序列化为包含跟踪上下文的 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        """输出稳定字段，并按需附加扫描阶段、耗时和异常信息。"""

        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", trace_id_var.get()),
        }
        for key in ("run_id", "stage", "error_code", "duration_ms"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """配置输出到标准输出的根 JSON 日志处理器。"""

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


class TraceIdMiddleware(BaseHTTPMiddleware):
    """为每个请求绑定跟踪标识，并记录响应状态与耗时。"""

    async def dispatch(self, request: Request, call_next):
        """传播调用方跟踪标识或生成新标识，并在响应后恢复上下文。"""

        trace_id = request.headers.get("X-Trace-ID") or uuid.uuid4().hex
        request.state.trace_id = trace_id
        token = trace_id_var.set(trace_id)
        started = time.monotonic()
        try:
            response = await call_next(request)
            response.headers["X-Trace-ID"] = trace_id
            logging.getLogger("http").info(
                "%s %s %s",
                request.method,
                request.url.path,
                response.status_code,
                extra={"duration_ms": round((time.monotonic() - started) * 1000)},
            )
            return response
        finally:
            trace_id_var.reset(token)
