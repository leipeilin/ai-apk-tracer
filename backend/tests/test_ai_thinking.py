"""回归：deepseek 思维模式默认开启导致 content 为空，需显式关闭。

事故（run 20260806T155116Z）：131/138 个 l2-review 请求初始响应为空字符串
（initial_response_hash = SHA256("")），repair 也失败，AI 完成率 2/147。
根因：deepseek-v4-flash 思维模式默认开启，推理 token 挤占 max_tokens=3000，
content 被推理耗尽为空。

后续回归（run 20260808T042228Z）：D11 补丁丢失 `_chat_payload` 的 `"model"`
字段，请求体缺 model → HTTP 400 request_incompatible → preflight 熔断。本文件
所有用例都必须断言 model 字段存在，防止再次引入同类字段丢失。
"""

from __future__ import annotations

import pytest

from app.analysis.ai import _chat_payload


def test_chat_payload_has_thinking_disabled_by_default_flag() -> None:
    payload = _chat_payload("test-model", [{"role": "user", "content": "x"}],
                            disable_thinking=True)

    assert payload["model"] == "test-model"
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0


def test_chat_payload_thinking_param_configurable() -> None:
    payload = _chat_payload(
        "test-model",
        [{"role": "user", "content": "x"}],
        disable_thinking=True,
        thinking_param="reasoning_effort",
    )

    assert payload["model"] == "test-model"
    assert "thinking" not in payload
    assert payload["reasoning_effort"] == {"type": "disabled"}


def test_chat_payload_thinking_disabled_by_default() -> None:
    payload = _chat_payload("test-model", [{"role": "user", "content": "x"}])

    assert payload["model"] == "test-model"
    assert "thinking" not in payload, "不启用 disable_thinking 时不得添加 thinking 参数"


def test_chat_payload_max_tokens_passthrough() -> None:
    payload = _chat_payload("test-model", [{"role": "user", "content": "x"}],
                            max_output_tokens=8000)

    assert payload["model"] == "test-model"
    assert payload["max_tokens"] == 8000


@pytest.mark.parametrize(
    ("disable_thinking", "thinking_param", "max_tokens"),
    [
        (False, "thinking", None),
        (True, "thinking", 8000),
        (True, "reasoning_effort", 3000),
    ],
)
def test_chat_payload_always_contains_required_fields(
    disable_thinking, thinking_param, max_tokens
) -> None:
    """回归：请求体必须始终包含 model 与 messages——缺 model 会触发
    HTTP 400 `missing field 'model'`，导致 preflight 熔断整个 AI 阶段。"""

    payload = _chat_payload(
        "test-model",
        [{"role": "user", "content": "x"}],
        max_output_tokens=max_tokens,
        disable_thinking=disable_thinking,
        thinking_param=thinking_param,
    )

    assert payload["model"] == "test-model"
    assert payload["messages"] == [{"role": "user", "content": "x"}]
    assert payload["response_format"] == {"type": "json_object"}
