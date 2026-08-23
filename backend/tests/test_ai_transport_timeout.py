"""AI transport 总时长兜底测试（M2-DEFECT-FIX D-2）。"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.analysis.ai_transport import AITransport


class _Settings:
    """最小 settings 替身（超时字段族 + 重试零等待）。"""

    base_url = "https://fake.local/v1"
    model = "test-model"
    api_key_env = "FAKE_KEY"
    connect_timeout_seconds = 5.0
    read_timeout_seconds = 120.0
    write_timeout_seconds = 5.0
    pool_timeout_seconds = 5.0
    request_timeout_seconds = 0.3  # 兜底短窗（测试注入）
    max_concurrent = 2
    provider_max_in_flight = 2
    provider_max_cooldown_seconds = 0.0
    retry_count = 0  # 单次尝试（不重试——直接断言 network 失败）
    retry_base_seconds = 0.0
    retry_max_seconds = 0.0
    retry_jitter_seconds = 0.0


class _HangingClient:
    """永挂的 httpx 客户端替身（wait_for 兜底的触发源）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        _HangingClient.calls = getattr(_HangingClient, "calls", 0) + 1
        await asyncio.sleep(999)
        raise AssertionError("不应到达")  # pragma: no cover


class _FastClient:
    """快速 200 的 httpx 客户端替身。"""

    def __init__(self) -> None:
        self.calls = 0

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]},
                              request=httpx.Request("POST", url))


def _transport(client: Any) -> AITransport:
    transport = AITransport(_Settings(), client=client)
    return transport


def test_request_total_timeout_falls_to_network() -> None:
    """A-5：永挂 post → 总时长兜底触发 → network 失败（可重试语义）。"""

    transport = _transport(_HangingClient())
    result = asyncio.run(transport.post_chat_completions(
        {"model": "test-model", "messages": []}, {"Authorization": "Bearer x"}))
    assert result.response is None
    assert result.failure == "network"
    assert result.attempts == 1  # 单次尝试即兜底失败
    asyncio.run(transport.aclose())


def test_fast_request_unaffected() -> None:
    """A-6：正常快速请求不受兜底影响。"""

    client = _FastClient()
    transport = _transport(client)
    result = asyncio.run(transport.post_chat_completions(
        {"model": "test-model", "messages": []}, {"Authorization": "Bearer x"}))
    assert result.response is not None and result.response.status_code == 200
    assert result.failure is None
    assert client.calls == 1
    asyncio.run(transport.aclose())


def test_derived_request_timeout_default() -> None:
    """A-7：未显式配置时总时长兜底 = read_timeout + 60（评审 R-2 动态派生）。"""

    from app.config import AISettings

    settings = AISettings(base_url="https://fake", model="m", read_timeout_seconds=300.0)
    assert settings.request_timeout_seconds == 360.0
    explicit = AISettings(base_url="https://fake", model="m",
                          read_timeout_seconds=300.0, request_timeout_seconds=90.0)
    assert explicit.request_timeout_seconds == 90.0


def test_retry_after_timeout_exhausts_attempts() -> None:
    """兜底触发后重试路径：多次尝试均挂 → attempts 计数正确 → network。"""

    class _RetrySettings(_Settings):
        retry_count = 1  # 2 次尝试
        retry_max_seconds = 0.0

    client = _HangingClient()
    transport = AITransport(_RetrySettings(), client=client)
    result = asyncio.run(transport.post_chat_completions(
        {"model": "test-model", "messages": []}, {"Authorization": "Bearer x"}))
    assert result.failure == "network"
    assert result.attempts == 2
    asyncio.run(transport.aclose())
