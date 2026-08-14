from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

import app.analysis.ai as ai_module
from app.analysis.ai import OpenAICompatibleAnalyzer
from app.analysis.ai_runtime import AIRuntime
from app.analysis.ai_scheduler import TaskCircuit, provider_controller_registry
from app.config import AISettings


API_KEY_ENV = "AI_RUNTIME_CONTROLS_TEST_KEY"
BASE_URL = "https://ai-runtime-controls.invalid/v1"


def _settings(**values: object) -> AISettings:
    defaults: dict[str, object] = {
        "enabled": True,
        "base_url": BASE_URL,
        "api_key_env": API_KEY_ENV,
        "model": "test-model",
        "allow_external_code": True,
        "timeout_seconds": 1,
        "retry_count": 0,
        "retry_base_seconds": 0,
        "retry_max_seconds": 0,
        "retry_jitter_seconds": 0,
    }
    return AISettings(**{**defaults, **values})


def _l2_output(summary: str = "L2 复核完成") -> dict[str, object]:
    return {
        "summary": summary,
        "verdict": "supports_candidate",
        "confidence_tier": "high",
        "guard_status": "absent",
        "evidence_refs": [{
            "context_id": "ctx-1",
            "path": "Demo.java",
            "line": 1,
            "end_line": 1,
            "claim": "缓存测试证据",
        }],
        "blocking_gaps": [],
        "uncertainties": [],
        "context_requests": [],
        "flaw_holds": True,
        "exploitability": {
            "entry_reachable": True,
            "propagation_proven": True,
            "sink_effective": True,
            "guard_bypassed": False,
            "authorization_absent": True,
            "exfiltration_channel": "confirmed",
        },
        "harm": {
            "impact_type": "data_disclosure",
            "impact_target": "本地敏感数据",
            "server_confirmation_required": False,
        },
        "reachability_class": "remote",
        "impact_vector": {
            "confidentiality": "partial",
            "integrity": "none",
            "availability": "none",
            "privileges_required": "none",
            "attack_complexity": "low",
            "user_interaction": "none",
        },
        "analysis_complete": True,
    }


def _response(output: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}]},
    )


def _candidate(name: str = "TEST") -> dict[str, object]:
    return {"rule_id": name, "evidence_level": "L2"}


def _slice(name: str = "slice") -> dict[str, object]:
    return {
        "slice_id": name,
        "contexts": [{
            "context_id": "ctx-1",
            "path": "Demo.java",
            "start_line": 1,
            "end_line": 1,
        }],
    }


@pytest.fixture(autouse=True)
def _clear_provider_registry(monkeypatch: pytest.MonkeyPatch):
    provider_controller_registry.clear()
    monkeypatch.setenv(API_KEY_ENV, "unit-test-secret-token")
    yield
    provider_controller_registry.clear()


def test_valid_cache_hit_returns_completed_without_http(tmp_path: Path) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _response(_l2_output())

    analyzer = OpenAICompatibleAnalyzer(
        _settings(),
        transport=httpx.MockTransport(handler),
    )
    analyzer.configure_cache(tmp_path / "ai-cache")

    async def run() -> tuple[dict, dict]:
        first = await analyzer.review_l2(_candidate("CACHE"), _slice("cache"))
        second = await analyzer.review_l2(_candidate("CACHE"), _slice("cache"))
        return first, second

    first, second = asyncio.run(run())

    assert request_count == 1
    assert first["status"] == second["status"] == "completed"
    assert first["analysis"] == second["analysis"]
    assert first["metadata"]["cache_hit"] is False
    assert first["metadata"]["cache_written"] is True
    assert second["metadata"]["cache_hit"] is True
    assert second["metadata"]["cache_key"] == first["metadata"]["cache_key"]
    assert second["metadata"]["attempts"] == 0


def test_invalid_cache_entry_is_a_miss_and_is_replaced(tmp_path: Path) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _response(_l2_output(f"HTTP response {request_count}"))

    cache_dir = tmp_path / "ai-cache"
    analyzer = OpenAICompatibleAnalyzer(_settings(), transport=httpx.MockTransport(handler))
    analyzer.configure_cache(cache_dir)

    async def run() -> tuple[dict, dict]:
        first = await analyzer.review_l2(_candidate("CORRUPT"), _slice("corrupt"))
        cache_path = cache_dir / "entries" / f"{first['metadata']['cache_key']}.json"
        cache_path.write_bytes(b"{not-valid-json")
        second = await analyzer.review_l2(_candidate("CORRUPT"), _slice("corrupt"))
        return first, second

    first, second = asyncio.run(run())

    assert request_count == 2
    assert first["analysis"]["summary"] == "HTTP response 1"
    assert second["analysis"]["summary"] == "HTTP response 2"
    assert second["metadata"]["cache_hit"] is False
    assert second["metadata"]["cache_written"] is True


def test_only_strictly_accepted_output_is_cached(tmp_path: Path) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _response({"summary": "invalid and incomplete"})

    cache_dir = tmp_path / "ai-cache"
    analyzer = OpenAICompatibleAnalyzer(_settings(), transport=httpx.MockTransport(handler))
    analyzer.configure_cache(cache_dir)

    result = asyncio.run(analyzer.review_l2(_candidate("INVALID"), _slice("invalid")))

    assert request_count == 2
    assert result["status"] == "failed"
    assert not (cache_dir / "entries").exists()


def test_repaired_accepted_output_is_cached(tmp_path: Path) -> None:
    request_count = 0
    initial = {key: value for key, value in _l2_output().items() if key != "analysis_complete"}
    repair = {"repaired_output": _l2_output("repaired"), "analysis_complete": True}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _response(initial if request_count == 1 else repair)

    analyzer = OpenAICompatibleAnalyzer(_settings(), transport=httpx.MockTransport(handler))
    analyzer.configure_cache(tmp_path / "ai-cache")

    async def run() -> tuple[dict, dict]:
        first = await analyzer.review_l2(_candidate("REPAIRED"), _slice("repaired"))
        second = await analyzer.review_l2(_candidate("REPAIRED"), _slice("repaired"))
        return first, second

    first, second = asyncio.run(run())

    assert request_count == 2
    assert first["status"] == second["status"] == "completed"
    assert first["analysis"]["summary"] == second["analysis"]["summary"] == "repaired"
    assert first["metadata"]["cache_written"] is True
    assert second["metadata"]["cache_hit"] is True


def test_cache_serialization_contains_no_request_or_provider_secrets(tmp_path: Path) -> None:
    raw_base_url = "https://private-provider.invalid/v1?token=provider-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(_l2_output())

    analyzer = OpenAICompatibleAnalyzer(
        _settings(base_url=raw_base_url),
        transport=httpx.MockTransport(handler),
    )
    cache_dir = tmp_path / "ai-cache"
    analyzer.configure_cache(cache_dir)
    result = asyncio.run(
        analyzer.review_l2(
            _candidate("PRIVATE_CANDIDATE_SENTINEL"),
            {
                "slice_id": "private",
                "contexts": [{
                    "context_id": "ctx-1",
                    "path": "Demo.java",
                    "start_line": 1,
                    "end_line": 1,
                    "content": "PRIVATE_CODE_SENTINEL",
                }],
            },
        )
    )
    cache_path = cache_dir / "entries" / f"{result['metadata']['cache_key']}.json"
    serialized = cache_path.read_text("utf-8")

    assert result["metadata"]["cache_hit"] is False
    for forbidden in (
        raw_base_url,
        "provider-secret",
        "unit-test-secret-token",
        "Authorization",
        "PRIVATE_CANDIDATE_SENTINEL",
        "PRIVATE_CODE_SENTINEL",
        "chat/completions",
    ):
        assert forbidden not in serialized


def test_retry_policy_uses_configured_count_exponential_jitter_and_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_count = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(503, json={"error": {"message": "temporary"}})

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(ai_module.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(ai_module.random, "uniform", lambda lower, upper: upper)
    analyzer = OpenAICompatibleAnalyzer(
        _settings(
            retry_count=3,
            retry_base_seconds=0.2,
            retry_max_seconds=0.5,
            retry_jitter_seconds=0.1,
        ),
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(analyzer.review_l2(_candidate("RETRY"), _slice("retry")))

    assert request_count == 4
    assert result["status"] == "failed"
    assert result["metadata"]["attempts"] == 4
    assert sleeps == pytest.approx([0.3, 0.5, 0.5])


def test_retry_after_and_shared_provider_cooldown_are_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(ai_module.asyncio, "sleep", record_sleep)
    response = httpx.Response(429, headers={"Retry-After": "1000"})
    analyzer = OpenAICompatibleAnalyzer(
        _settings(
            retry_count=0,
            retry_max_seconds=0.1,
            provider_max_cooldown_seconds=0.2,
        ),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                429,
                headers={"Retry-After": "1000"},
                json={"error": {"message": "limited"}},
            )
        ),
    )

    asyncio.run(analyzer._retry_backoff(response, 0))
    result = asyncio.run(analyzer.review_l2(_candidate("LIMIT"), _slice("limit")))
    shared = OpenAICompatibleAnalyzer(_settings())._provider_controller()
    controller = analyzer._provider_controller()

    assert sleeps == [0.1]
    assert result["classification"] == "rate_limited"
    assert shared is controller
    assert 0 < controller.cooldown_remaining <= 0.2
    assert controller.stats.cooldown_updates == 1


def test_provider_gate_bounds_concurrent_http_sends() -> None:
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return _response(_l2_output())

    analyzer = OpenAICompatibleAnalyzer(
        _settings(provider_max_in_flight=2),
        transport=httpx.MockTransport(handler),
    )

    async def run() -> list[dict]:
        return await asyncio.gather(*(
            analyzer.review_l2(_candidate(f"CONCURRENT-{index}"), _slice(f"slice-{index}"))
            for index in range(6)
        ))

    results = asyncio.run(run())

    assert all(result["status"] == "completed" for result in results)
    assert peak == 2
    assert analyzer._provider_controller().stats.peak_in_flight == 2


def test_repair_rechecks_task_circuit_through_provider_gate() -> None:
    request_count = 0
    circuit = TaskCircuit()
    invalid = {key: value for key, value in _l2_output().items() if key != "analysis_complete"}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        circuit.open("test-open")
        return _response(invalid)

    analyzer = OpenAICompatibleAnalyzer(_settings(), transport=httpx.MockTransport(handler))
    analyzer.set_task_circuit(circuit)

    result = asyncio.run(analyzer.review_l2(_candidate("CIRCUIT"), _slice("circuit")))

    assert request_count == 1
    assert result["status"] == "failed"
    assert result["metadata"]["initial_attempts"] == 1
    assert result["metadata"]["repair_attempts"] == 0


def test_ai_settings_defaults_preserve_two_attempts_and_bounds() -> None:
    settings = AISettings()

    assert settings.retry_count == 1
    assert settings.retry_base_seconds == 0.05
    assert settings.retry_max_seconds == 30.0
    assert settings.retry_jitter_seconds == 0.05
    assert settings.max_concurrent == 6
    assert settings.candidate_concurrency == 4
    assert settings.provider_max_in_flight == 4
    assert settings.connect_timeout_seconds == 10.0
    assert settings.read_timeout_seconds == 120.0
    assert settings.write_timeout_seconds == 30.0
    assert settings.pool_timeout_seconds == 10.0
    assert settings.provider_max_cooldown_seconds == 60.0
    assert settings.cache_max_entry_bytes == 2097152

    for field, value in (
        ("candidate_concurrency", 0),
        ("provider_max_in_flight", 0),
        ("provider_max_cooldown_seconds", 3601),
        ("retry_count", 11),
        ("retry_base_seconds", -1),
        ("retry_max_seconds", 601),
        ("retry_jitter_seconds", 61),
        ("cache_max_entry_bytes", 1023),
    ):
        with pytest.raises(ValidationError):
            AISettings(**{field: value})


def test_runtime_reuses_one_client_and_applies_split_timeouts() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _response(_l2_output())

    settings = _settings(
        connect_timeout_seconds=1.5,
        read_timeout_seconds=2.5,
        write_timeout_seconds=3.5,
        pool_timeout_seconds=4.5,
    )
    runtime = AIRuntime(settings, transport=httpx.MockTransport(handler))

    async def scenario() -> None:
        first = runtime.create_analyzer()
        second = runtime.create_analyzer()
        assert first._ai_transport is second._ai_transport is runtime.transport
        await first.review_l2(_candidate("FIRST"), _slice("first"))
        client = runtime.transport.client
        await second.review_l2(_candidate("SECOND"), _slice("second"))
        assert runtime.transport.client is client
        assert runtime.transport.client_create_count == 1
        assert client.timeout.connect == 1.5
        assert client.timeout.read == 2.5
        assert client.timeout.write == 3.5
        assert client.timeout.pool == 4.5
        await runtime.aclose()
        assert client.is_closed is True

    asyncio.run(scenario())
    assert request_count == 2


def test_runtime_auth_circuit_blocks_later_analyzers_without_http() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(403, json={"error": {"message": "denied"}})

    runtime = AIRuntime(_settings(), transport=httpx.MockTransport(handler))

    async def scenario() -> tuple[dict, dict]:
        first = runtime.create_analyzer()
        second = runtime.create_analyzer()
        first_result = await first.review_l2(_candidate("AUTH-1"), _slice("auth-1"))
        second_result = await second.review_l2(_candidate("AUTH-2"), _slice("auth-2"))
        await runtime.aclose()
        return first_result, second_result

    first_result, second_result = asyncio.run(scenario())
    assert request_count == 1
    assert first_result["classification"] == second_result["classification"] == "auth_failed"
    assert first_result["circuit_breaking"] is True
    assert second_result["circuit_breaking"] is True
    assert second_result["metadata"]["attempts"] == 0


def test_runtime_global_gate_bounds_requests_across_analyzers() -> None:
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return _response(_l2_output())

    runtime = AIRuntime(
        _settings(max_concurrent=2, provider_max_in_flight=4),
        transport=httpx.MockTransport(handler),
    )

    async def scenario() -> None:
        analyzers = [runtime.create_analyzer() for _ in range(3)]
        results = await asyncio.gather(*(
            analyzers[index % len(analyzers)].review_l2(
                _candidate(f"GLOBAL-{index}"), _slice(f"global-{index}")
            )
            for index in range(8)
        ))
        assert all(result["status"] == "completed" for result in results)
        await runtime.aclose()

    asyncio.run(scenario())
    assert peak == 2


@pytest.mark.parametrize("status", [408, 425, 429, 500, 503])
def test_all_retryable_http_statuses_are_retried(status: int) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(status, json={"error": {"message": "retry"}})
        return _response(_l2_output())

    analyzer = OpenAICompatibleAnalyzer(
        _settings(retry_count=1),
        transport=httpx.MockTransport(handler),
        retry_backoff_seconds=0,
    )
    result = asyncio.run(analyzer.review_l2(_candidate(f"RETRY-{status}"), _slice()))

    assert result["status"] == "completed"
    assert result["metadata"]["attempts"] == 2
    assert request_count == 2


def test_invalid_evidence_refs_are_not_cached(tmp_path: Path) -> None:
    request_count = 0
    output = _l2_output()
    output["evidence_refs"] = [{
        "context_id": "ctx-1",
        "path": "Wrong.java",
        "line": 10,
        "end_line": None,
        "claim": "引用路径不匹配",
    }]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _response(output)

    analyzer = OpenAICompatibleAnalyzer(_settings(), transport=httpx.MockTransport(handler))
    analyzer.configure_cache(tmp_path / "ai-cache")
    context_slice = {
        "slice_id": "invalid-ref",
        "contexts": [{
            "context_id": "ctx-1",
            "path": "Right.java",
            "start_line": 1,
            "end_line": 20,
        }],
    }

    async def scenario() -> tuple[dict, dict]:
        first = await analyzer.review_l2(_candidate("REF"), context_slice)
        second = await analyzer.review_l2(_candidate("REF"), context_slice)
        return first, second

    first, second = asyncio.run(scenario())
    assert first["metadata"]["cache_written"] is False
    assert second["metadata"]["cache_hit"] is False
    assert request_count == 2


def test_completed_output_without_evidence_refs_is_not_cached(tmp_path: Path) -> None:
    request_count = 0
    output = _l2_output()
    output["evidence_refs"] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _response(output)

    cache_dir = tmp_path / "ai-cache"
    analyzer = OpenAICompatibleAnalyzer(_settings(), transport=httpx.MockTransport(handler))
    analyzer.configure_cache(cache_dir)

    async def scenario() -> tuple[dict, dict]:
        first = await analyzer.review_l2(_candidate("EMPTY-REFS"), _slice("empty-refs"))
        second = await analyzer.review_l2(_candidate("EMPTY-REFS"), _slice("empty-refs"))
        return first, second

    first, second = asyncio.run(scenario())

    assert first["status"] == second["status"] == "completed"
    assert first["metadata"]["cache_written"] is False
    assert second["metadata"]["cache_hit"] is False
    assert request_count == 2
    assert not (cache_dir / "entries").exists()


def test_ai_runtime_metadata_extracted_from_trace_into_finding_top_level() -> None:
    """v2026-08-14：AI 运行元数据（prompt_version/schema_hash/provider/model）此前
    只在 ai-trace 文件，finding 顶层从未写入导致前端 AI observation 显示"未记录"。
    _ai_runtime_metadata_from_trace 必须从最后成功轮 result.metadata 提取并映射。"""
    from app.analysis.orchestrator import ScanOrchestrator

    trace = [
        {
            "round": 0,
            "slice_id": "s1",
            "result": {
                "status": "incomplete",
                "metadata": {"prompt_version": "1.0.0", "model": "old"},
            },
        },
        {
            "round": 1,
            "slice_id": "s2",
            "result": {
                "status": "completed",
                "metadata": {
                    "prompt_id": "l2-review",
                    "prompt_version": "3.0.5",
                    "prompt_template_version": "3.0.5",
                    "prompt_template_hash": "ph1",
                    "schema_hash": "sh1",
                    "schema_sha256": {"input": "ish", "output": "osh"},
                    "output_model_version": "v2",
                    "provider_kind": "openai-compatible",
                    "base_url_hash": "buh",
                    "model": "gpt-test",
                    "cache_hit": False,
                    "latency_ms": 1234,
                },
            },
        },
    ]

    extracted = ScanOrchestrator._ai_runtime_metadata_from_trace(trace)
    assert extracted["prompt_version"] == "3.0.5"
    assert extracted["prompt_hash"] == "ph1"
    assert extracted["schema_hash"] == "sh1"
    assert extracted["input_schema_hash"] == "ish"
    assert extracted["output_schema_hash"] == "osh"
    assert extracted["ai_schema_version"] == "v2"
    assert extracted["provider_kind"] == "openai-compatible"
    assert extracted["provider"] == "buh"
    assert extracted["model"] == "gpt-test"
    assert extracted["cache_hit"] is False
    # 只取最后成功轮（round 0 的 incomplete 元数据被跳过）
    assert extracted["model"] == "gpt-test"


def test_ai_runtime_metadata_empty_trace_returns_empty() -> None:
    """无 AI 轮次时返回空 dict，_apply_ai_analysis 不写 None 污染 finding。"""
    from app.analysis.orchestrator import ScanOrchestrator

    assert ScanOrchestrator._ai_runtime_metadata_from_trace([]) == {}
    assert ScanOrchestrator._ai_runtime_metadata_from_trace([{"result": {"status": "failed"}}]) == {}
