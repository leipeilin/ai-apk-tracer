"""进程级 AI 运行时，复用 transport/client 与全局并发闸门。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from app.analysis.ai_transport import AITransport


class AIRuntime:
    """FastAPI 生命周期内共享的 AI 网络资源。"""

    def __init__(
        self,
        settings: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.global_semaphore = asyncio.Semaphore(int(getattr(settings, "max_concurrent", 6)))
        self.transport = AITransport(
            settings,
            transport=transport,
            client=client,
            global_semaphore=self.global_semaphore,
        )
        self._closed = False

    def create_analyzer(
        self,
        *,
        cache_dir: str | Path | None = None,
        max_output_tokens: int | None = None,
        budget_policy: dict[str, Any] | None = None,
    ):
        from app.analysis.ai import OpenAICompatibleAnalyzer

        analyzer = OpenAICompatibleAnalyzer(self.settings, ai_transport=self.transport)
        if cache_dir is not None:
            analyzer.configure_cache(cache_dir)
        analyzer.configure_budget_identity(max_output_tokens, budget_policy)
        return analyzer

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.transport.aclose()
