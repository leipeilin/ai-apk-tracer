"""共享 OpenAI-compatible HTTP transport、重试与进程级流量控制。"""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.analysis.ai_scheduler import (
    CircuitOpenError,
    TaskCircuit,
    get_provider_controller,
)

_RETRYABLE_STATUSES = {408, 425, 429}
_SECRET_PATTERN = re.compile(r"(?i)(bearer\s+|api[_-]?key[=:]\s*)[^\s,;]+")


@dataclass(frozen=True, slots=True)
class AITransportResult:
    response: httpx.Response | None
    attempts: int
    failure: str | None = None
    failure_http_status: int | None = None


class AITransport:
    """持有单个可复用 AsyncClient，并统一实施超时、重试和并发上限。"""

    def __init__(
        self,
        settings: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        global_semaphore: asyncio.Semaphore | None = None,
        retry_backoff_seconds: float | None = None,
    ) -> None:
        self.settings = settings
        self._raw_transport = transport
        self._client = client
        self._owns_client = client is None
        self._global_semaphore = global_semaphore or asyncio.Semaphore(
            int(getattr(settings, "max_concurrent", 6))
        )
        self._retry_backoff_seconds = retry_backoff_seconds
        self._closed = False
        self._fatal_failure: tuple[str, int | None] | None = None
        self.client_create_count = 0

    @property
    def client(self) -> httpx.AsyncClient:
        if self._closed:
            raise RuntimeError("AI transport 已关闭")
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=float(getattr(self.settings, "connect_timeout_seconds", 10.0)),
                    read=float(getattr(self.settings, "read_timeout_seconds", getattr(self.settings, "timeout_seconds", 120.0))),
                    write=float(getattr(self.settings, "write_timeout_seconds", 30.0)),
                    pool=float(getattr(self.settings, "pool_timeout_seconds", 10.0)),
                ),
                transport=self._raw_transport,
            )
            self.client_create_count += 1
        return self._client

    async def post_chat_completions(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        circuit: TaskCircuit | None = None,
    ) -> AITransportResult:
        """发送请求并统一实施 runtime fatal、任务熔断、双层并发和有限重试。

        每次真实 HTTP 尝试同时持有 provider lease 与 runtime semaphore；429 的 Retry-After
        会发布到进程共享 controller。鉴权/模型错误锁存为当前 transport 的 fatal failure，
        后续候选零请求失败；网络异常和可重试状态只在配置的 attempt 上限内退避。
        """

        attempts = 0
        configured_attempts = getattr(self.settings, "retry_max_attempts", None)
        max_attempts = (
            int(configured_attempts)
            if configured_attempts is not None
            else 1 + int(getattr(self.settings, "retry_count", 1))
        )
        controller = self.provider_controller()
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        # M2-DEFECT-FIX D-2：单次请求总时长兜底（墙钟）——防御中间层 keepalive
        # 重置 httpx 分项超时的长挂起（实测连接 15 分钟无数据且 read_timeout
        # 未触发）；未显式配置时由 config 派生 read_timeout + 60
        request_timeout = float(getattr(self.settings, "request_timeout_seconds", 180.0) or 180.0)
        for attempt in range(max_attempts):
            if self._fatal_failure is not None:
                failure, status = self._fatal_failure
                return AITransportResult(None, attempts, failure, status)
            if circuit is not None and circuit.is_open:
                return AITransportResult(None, attempts, "circuit_open")
            timed_out = False
            try:
                async with controller.lease(circuit):
                    await self._global_semaphore.acquire()
                    try:
                        if self._fatal_failure is not None:
                            failure, status = self._fatal_failure
                            return AITransportResult(None, attempts, failure, status)
                        if circuit is not None and circuit.is_open:
                            raise CircuitOpenError(circuit.reason or "task circuit is open")
                        attempts += 1
                        response = None
                        try:
                            response = await asyncio.wait_for(
                                self.client.post(url, headers=headers, json=payload),
                                request_timeout,
                            )
                        except TimeoutError:
                            # 总时长兜底触发——wait_for 已取消底层请求（semaphore
                            # 释放与 lease 退出必经 finally/__aexit__——评审 R-5）
                            timed_out = True
                            response = None
                    finally:
                        self._global_semaphore.release()
            except CircuitOpenError:
                return AITransportResult(None, attempts, "circuit_open")
            except httpx.HTTPError:
                timed_out = True  # 网络异常与总时长兜底共用重试路径
                response = None
            if timed_out or response is None:
                if attempt + 1 < max_attempts:
                    await self.retry_backoff(None, attempt)
                    continue
                return AITransportResult(None, attempts, "network")

            fatal_failure = fatal_response_classification(response)
            if fatal_failure is not None and self._fatal_failure is None:
                self._fatal_failure = (fatal_failure, response.status_code)
            retry_after = retry_after_seconds(response)
            if response.status_code == 429 and retry_after is not None:
                controller.note_rate_limit(retry_after)
            if is_retryable_status(response.status_code) and attempt + 1 < max_attempts:
                await self.retry_backoff(response, attempt)
                continue
            return AITransportResult(response, attempts)
        return AITransportResult(None, attempts, "network")

    async def retry_backoff(self, response: httpx.Response | None, attempt: int) -> None:
        """按 Retry-After 或指数退避休眠，并以配置上限截断单次等待。

        无法解析 Retry-After 时退回 base+jitter；该等待不扩大尝试次数，也不绕过 provider
        controller 已发布的共享 cooldown。
        """

        maximum = float(getattr(self.settings, "retry_max_seconds", 30.0))
        if maximum <= 0:
            return
        retry_after = retry_after_seconds(response)
        if retry_after is not None:
            delay = retry_after
        else:
            base = (
                self._retry_backoff_seconds
                if self._retry_backoff_seconds is not None
                else float(getattr(self.settings, "retry_base_seconds", 0.05))
            )
            jitter = (
                0.0
                if self._retry_backoff_seconds is not None
                else float(getattr(self.settings, "retry_jitter_seconds", 0.05))
            )
            delay = base * (2**attempt) + random.uniform(0, jitter)
        delay = min(max(delay, 0.0), maximum)
        if delay:
            await asyncio.sleep(delay)

    def provider_controller(self):
        return get_provider_controller(
            base_url=self.settings.base_url.rstrip("/"),
            model=self.settings.model,
            api_key_env=self.settings.api_key_env,
            max_in_flight=int(getattr(self.settings, "provider_max_in_flight", 4)),
            max_cooldown=float(getattr(self.settings, "provider_max_cooldown_seconds", 60.0)),
        )

    async def aclose(self) -> None:
        """关闭 transport；对僵尸连接（对端消失的 keep-alive 空闲连接）的关闭
        握手加 10s 墙钟兜底——T1-v3 实证：run 完成落盘后 aclose 因对端消失
        的连接无限挂起（进程不退出 1h+）。超时后放弃由 OS 回收连接。"""
        if self._closed:
            return
        self._closed = True
        if self._owns_client and self._client is not None:
            try:
                await asyncio.wait_for(self._client.aclose(), 10.0)
            except (TimeoutError, httpx.HTTPError):
                pass  # 关闭挂起/失败——连接由 OS 最终回收（数据已落盘）


def is_retryable_status(status: int) -> bool:
    return status in _RETRYABLE_STATUSES or status >= 500


def retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)
        except (TypeError, ValueError, OverflowError):
            return None


def fatal_response_classification(response: httpx.Response) -> str | None:
    """识别对当前 runtime 内所有候选均不可恢复的鉴权或模型错误。"""

    if response.status_code in {401, 403}:
        return "auth_failed"
    if response.status_code == 404:
        return "model_not_found"
    try:
        body = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error", body)
    if not isinstance(error, dict):
        return None
    marker = " ".join(
        str(error.get(field, "")).lower()
        for field in ("code", "type", "message")
    )[:2000]
    if any(value in marker for value in (
        "model_not_found",
        "model not found",
        "model does not exist",
        "model doesn't exist",
        "unknown model",
        "invalid model",
        "no such model",
    )):
        return "model_not_found"
    return None


def sanitize_transport_error(error: BaseException | str) -> str:
    """仅保留异常类型和脱敏后的短消息，不泄露凭据或完整请求。"""

    if isinstance(error, BaseException):
        value = f"{type(error).__name__}: {error}"
    else:
        value = str(error)
    return _SECRET_PATTERN.sub(r"\1[REDACTED]", value)[:500]
