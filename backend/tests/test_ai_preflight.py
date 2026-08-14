from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from app.analysis.ai import OpenAICompatibleAnalyzer
from app.analysis.ai_trace import AITraceStore, candidate_input_key
from app.analysis.orchestrator import ScanOrchestrator
from app.config import AISettings, Settings, SourceAnalysisSettings, StorageSettings
from app.runs.storage import RunStorage
from app.shared.repository import SQLiteRepository


API_KEY_ENV = "AI_PREFLIGHT_TEST_KEY"
BASE_URL = "https://ai-preflight.invalid/v1"


def _ai_settings(**values) -> AISettings:
    defaults = {
        "enabled": True,
        "base_url": BASE_URL,
        "api_key_env": API_KEY_ENV,
        "model": "test-model",
        "allow_external_code": True,
        "timeout_seconds": 1,
    }
    return AISettings(**{**defaults, **values})


def _analyzer(monkeypatch, handler, **settings) -> OpenAICompatibleAnalyzer:
    monkeypatch.setenv(API_KEY_ENV, "unit-test-token")
    return OpenAICompatibleAnalyzer(
        _ai_settings(**settings),
        transport=httpx.MockTransport(handler),
        retry_backoff_seconds=0,
    )


def _success_response(content: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(content)}}]}


def _preflight_payload() -> dict:
    return {
        "ok": True,
        "message": "严格输出能力正常",
        "acknowledged_capabilities": [
            "strict_json_object",
            "required_fields",
            "forbid_extra_fields",
        ],
        "analysis_complete": True,
    }


def _analysis_payload() -> dict:
    return {
        "summary": "分析完成",
        "verdict": "unresolved",
        "confidence_tier": "medium",
        "guard_status": "unknown",
        "evidence_refs": [],
        "blocking_gaps": [],
        "uncertainties": [],
        "context_requests": [],
        "flaw_holds": False,
        "exploitability": {
            "entry_reachable": True,
            "propagation_proven": False,
            "sink_effective": False,
            "guard_bypassed": False,
            "authorization_absent": True,
            "exfiltration_channel": "unverified",
        },
        "harm": {
            "impact_type": "data_tamper",
            "impact_target": "应用内部数据",
            "server_confirmation_required": True,
        },
        "reachability_class": "remote",
        "impact_vector": {
            "confidentiality": "none",
            "integrity": "partial",
            "availability": "none",
            "privileges_required": "none",
            "attack_complexity": "high",
            "user_interaction": "none",
        },
        "analysis_complete": True,
    }


def _candidate(rule_id: str) -> dict:
    return {
        "rule_id": rule_id,
        "evidence_level": "L2",
        "analysis_status": "rule_only",
        "deterministic_chain_verified": True,
        "dataflow_status": "intraprocedural",
        "authorization_status": "unknown",
        "impact_status": "potential",
        "sources": [{"path": "Demo.java", "line": 1}],
        "sinks": [{"path": "Demo.java", "line": 2}],
        "propagation_paths": [{"from": "source", "to": "sink"}],
        "blocking_gaps": [],
    }


def _orchestrator(tmp_path: Path) -> ScanOrchestrator:
    settings = Settings(
        database_path=tmp_path / "test.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "data"),
        source_analysis=SourceAnalysisSettings(enabled=True),
        ai=_ai_settings(),
    )
    repository = SQLiteRepository(settings.resolved_database_path())
    repository.initialize()
    storage = RunStorage(settings.resolved_data_root(), settings.storage)
    return ScanOrchestrator(settings, repository, storage)


def test_preflight_403_is_single_request_and_does_not_leak_response(monkeypatch) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            403,
            json={"error": {"message": "denied unit-test-token private-body"}},
        )

    result = asyncio.run(_analyzer(monkeypatch, handler).preflight())

    assert len(requests) == 1
    assert result["status"] == "failed"
    assert result["classification"] == "auth_failed"
    assert result["recoverable"] is False
    assert result["http_status"] == 403
    assert result["metadata"]["attempts"] == 1
    assert "unit-test-token" not in result["message"]
    assert "private-body" not in json.dumps(result)


@pytest.mark.parametrize(
    ("status_code", "error", "classification"),
    [
        (404, {"message": "missing"}, "model_not_found"),
        (400, {"code": "model_not_found", "message": "missing"}, "model_not_found"),
        (400, {"message": "response_format unsupported"}, "request_incompatible"),
        (422, {"message": "invalid request"}, "request_incompatible"),
    ],
)
def test_preflight_classifies_non_retryable_model_and_request_errors(
    monkeypatch, status_code: int, error: dict, classification: str
) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(status_code, json={"error": error})

    result = asyncio.run(_analyzer(monkeypatch, handler).preflight())

    assert request_count == 1
    assert result["classification"] == classification
    assert result["recoverable"] is False


@pytest.mark.parametrize(
    ("status_code", "classification"),
    [
        (408, "transient_failure"),
        (425, "transient_failure"),
        (429, "rate_limited"),
        (503, "transient_failure"),
    ],
)
def test_preflight_retryable_http_errors_retry_only_once(
    monkeypatch, status_code: int, classification: str
) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(status_code, json={"error": {"message": "temporary"}})

    result = asyncio.run(_analyzer(monkeypatch, handler).preflight())

    assert request_count == 2
    assert result["classification"] == classification
    assert result["recoverable"] is True
    assert result["metadata"]["attempts"] == 2


def test_preflight_network_error_retries_only_once_without_leaking_exception(monkeypatch) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        raise httpx.ConnectError("network unit-test-token private-detail", request=request)

    result = asyncio.run(_analyzer(monkeypatch, handler).preflight())

    assert request_count == 2
    assert result["classification"] == "transient_failure"
    assert result["recoverable"] is True
    assert result["metadata"]["attempts"] == 2
    assert "unit-test-token" not in json.dumps(result)
    assert "private-detail" not in json.dumps(result)


def test_preflight_accepts_markdown_wrapped_json_by_default(monkeypatch) -> None:
    """默认非严格协议：preflight 与普通分析同级，剥离围栏后接受并记录放宽轨迹。"""

    request_count = 0
    content = "```json\n" + json.dumps(_preflight_payload()) + "\n```"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    result = asyncio.run(_analyzer(monkeypatch, handler).preflight())

    assert request_count == 1
    assert result["status"] == "passed"
    assert result["metadata"]["preflight_strict_protocol"] is False
    assert result["metadata"]["protocol_relaxed"] is True
    assert result["metadata"]["protocol_relaxation"] == "markdown_fence"
    assert result["metadata"].get("format_repair_attempted") is not True


def test_preflight_rejects_markdown_wrapped_json_when_strict(monkeypatch) -> None:
    """显式开启严格协议后保持旧语义：一次不合格即熔断，不进入 repair。"""

    request_count = 0
    content = "```json\n" + json.dumps(_preflight_payload()) + "\n```"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    analyzer = _analyzer(monkeypatch, handler, preflight_strict_protocol=True)
    result = asyncio.run(analyzer.preflight())

    assert request_count == 1
    assert result["status"] == "failed"
    assert result["classification"] == "response_invalid"
    assert result["circuit_breaking"] is True
    assert result["metadata"]["preflight_strict_protocol"] is True
    assert result["metadata"]["protocol_relaxed"] is False
    assert result["metadata"].get("format_repair_attempted") is not True


def test_preflight_circuit_breaks_when_relaxed_and_repair_both_fail(monkeypatch) -> None:
    """宽松解析与 repair 都失败时仍熔断，避免带着不可用协议跑全量候选。"""

    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "模型只输出了自然语言，没有 JSON。"}}]},
        )

    result = asyncio.run(_analyzer(monkeypatch, handler).preflight())

    assert request_count >= 2
    assert result["status"] == "failed"
    assert result["circuit_breaking"] is True
    assert result["metadata"]["format_repair_attempted"] is True


def test_disabled_ai_preflight_does_not_make_request(monkeypatch) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        raise AssertionError("AI 关闭时不应发送请求")

    settings = _ai_settings(enabled=False)
    analyzer = OpenAICompatibleAnalyzer(settings, transport=httpx.MockTransport(handler))

    result = asyncio.run(analyzer.preflight())

    assert request_count == 0
    assert result["status"] == "skipped"
    assert result["classification"] == "disabled"


def test_successful_preflight_is_code_free_then_analyze_runs_normally(monkeypatch) -> None:
    request_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        request_payloads.append(payload)
        if len(request_payloads) == 1:
            return httpx.Response(200, json=_success_response(_preflight_payload()))
        return httpx.Response(200, json=_success_response(_analysis_payload()))

    analyzer = _analyzer(monkeypatch, handler)
    preflight = asyncio.run(analyzer.preflight())
    result = asyncio.run(analyzer.analyze(
        {"rule_id": "CODE_SENTINEL", "evidence_level": "L2"},
        {"slice_id": "slice-test", "contexts": [{"content": "CODE_SENTINEL"}]},
    ))

    assert preflight["status"] == "passed"
    assert preflight["classification"] == "configured"
    assert result["status"] == "completed"
    assert len(request_payloads) == 2
    assert "CODE_SENTINEL" not in json.dumps(request_payloads[0])
    assert "candidate" not in json.dumps(request_payloads[0])
    assert "CODE_SENTINEL" in json.dumps(request_payloads[1])


def test_ai_stage_without_l2_candidates_does_not_preflight(tmp_path: Path) -> None:
    class NoRequestAI:
        async def preflight(self):
            raise AssertionError("没有 L2 候选时不应执行 preflight")

    orchestrator = _orchestrator(tmp_path)
    orchestrator.ai = NoRequestAI()
    recorded = []
    orchestrator._record_stage = lambda run_id, stage, status, summary: recorded.append(
        {"stage": stage, "status": status, "summary": summary}
    )
    asyncio.run(orchestrator._run_ai_stage(
        "run-test",
        [{"rule_id": "L1", "evidence_level": "L1", "analysis_status": "rule_only"}],
        [],
        {},
        None,
        True,
        tmp_path,
    ))
    assert recorded[0]["status"] == "skipped"
    assert recorded[0]["summary"]["preflight"]["classification"] == "no_candidates"


def test_preflight_403_opens_task_circuit_for_all_l2_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(403, json={"error": {"message": "denied"}})

    orchestrator = _orchestrator(tmp_path)
    orchestrator.ai = _analyzer(monkeypatch, handler)
    recorded = []
    orchestrator._record_stage = lambda run_id, stage, status, summary: recorded.append(
        {"run_id": run_id, "stage": stage, "status": status, "summary": summary}
    )
    candidates = [_candidate("ONE"), {**_candidate("TWO"), "auxiliary": True}, {
        "rule_id": "L1",
        "evidence_level": "L1",
        "analysis_status": "rule_only",
    }]
    deterministic_fields = [
        "deterministic_chain_verified",
        "dataflow_status",
        "authorization_status",
        "impact_status",
        "sources",
        "sinks",
        "propagation_paths",
    ]
    before = [
        {field: copy.deepcopy(candidate.get(field)) for field in deterministic_fields}
        for candidate in candidates[:2]
    ]

    asyncio.run(orchestrator._run_ai_stage(
        "run-test",
        candidates,
        [0],
        {0: {"slice_id": "slice-one", "contexts": []}},
        object(),
        True,
        tmp_path,
    ))

    assert request_count == 1
    assert [candidate["analysis_status"] for candidate in candidates[:2]] == [
        "ai_skipped",
        "ai_skipped",
    ]
    assert candidates[2]["analysis_status"] == "rule_only"
    assert all(candidate["ai_preflight"]["classification"] == "auth_failed" for candidate in candidates[:2])
    assert recorded[0]["summary"]["preflight"]["http_status"] == 403
    assert recorded[0]["summary"]["circuit_open"] is True
    after = [
        {field: copy.deepcopy(candidate.get(field)) for field in deterministic_fields}
        for candidate in candidates[:2]
    ]
    assert after == before


def test_recoverable_preflight_failure_marks_all_l2_as_failed(
    tmp_path: Path, monkeypatch
) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(503, json={"error": {"message": "temporary"}})

    orchestrator = _orchestrator(tmp_path)
    orchestrator.ai = _analyzer(monkeypatch, handler)
    recorded = []
    orchestrator._record_stage = lambda run_id, stage, status, summary: recorded.append(summary)
    candidates = [_candidate("ONE"), _candidate("TWO")]

    asyncio.run(orchestrator._run_ai_stage(
        "run-test",
        candidates,
        [0, 1],
        {},
        object(),
        True,
        tmp_path,
    ))

    assert request_count == 2
    assert all(candidate["analysis_status"] == "ai_failed" for candidate in candidates)
    assert recorded[0]["preflight"]["classification"] == "transient_failure"
    assert recorded[0]["circuit_open"] is True
    assert recorded[0]["analyzed"] == 0


def test_ai_result_cannot_override_deterministic_guard_or_dataflow(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    candidate = _candidate("GUARD")
    candidate["guard_status"] = "present_effective"
    candidate["dataflow_status"] = "interprocedural"
    candidate["authorization_status"] = "strongly_protected"
    analysis = _analysis_payload()
    analysis["guard_status"] = "absent"
    orchestrator._apply_ai_analysis(
        candidate,
        analysis,
        [],
        {"contexts": [], "request_history": []},
    )
    assert candidate["guard_status"] == "present_effective"
    assert candidate["dataflow_status"] == "interprocedural"
    assert candidate["authorization_status"] == "strongly_protected"
    assert candidate["ai_guard_assessment"] == "absent"


def test_unrecoverable_analyze_error_opens_circuit_before_next_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(200, json=_success_response(_preflight_payload()))
        return httpx.Response(403, json={"error": {"message": "denied"}})

    orchestrator = _orchestrator(tmp_path)
    orchestrator.ai = _analyzer(monkeypatch, handler)
    recorded = []
    orchestrator._record_stage = lambda run_id, stage, status, summary: recorded.append(summary)
    candidates = [_candidate("ONE"), _candidate("TWO")]
    run_dir = tmp_path / "run"
    (run_dir / "ai-cache").mkdir(parents=True)

    asyncio.run(orchestrator._run_ai_stage(
        "run-test",
        candidates,
        [0, 1],
        {
            0: {"slice_id": "slice-one", "contexts": []},
            1: {"slice_id": "slice-two", "contexts": []},
        },
        object(),
        True,
        run_dir,
    ))

    assert request_count == 2
    assert candidates[0]["analysis_status"] == "ai_failed"
    assert candidates[1]["analysis_status"] == "ai_skipped"
    assert recorded[0]["circuit_open"] is True
    assert recorded[0]["analyzed"] == 1


def test_request_too_large_does_not_open_circuit(tmp_path, monkeypatch):
    """分析请求体超限时只失败当前候选，不熔断后续候选。"""

    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=_success_response(_analysis_payload()))

    settings = _ai_settings(max_request_bytes=100)
    monkeypatch.setenv(API_KEY_ENV, "unit-test-token")
    analyzer = OpenAICompatibleAnalyzer(
        settings, transport=httpx.MockTransport(handler), retry_backoff_seconds=0,
    )
    large_slice = {"slice_id": "s", "contexts": [], "candidate": {"x": "a" * 200}}
    result = asyncio.run(analyzer.analyze({"rule_id": "R"}, large_slice))
    assert result["status"] == "failed"
    assert result["classification"] == "input_too_large"
    assert result["circuit_breaking"] is False
    assert result["metadata"]["request_bytes"] > 100
    assert request_count == 0


def test_preflight_includes_temperature(tmp_path, monkeypatch):
    """preflight 必须携带 temperature 参数以与 analyze 保持一致。"""

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        body = _json.loads(request.content)
        captured["temperature"] = body.get("temperature")
        return httpx.Response(200, json=_success_response(_preflight_payload()))

    analyzer = _analyzer(monkeypatch, handler)
    asyncio.run(analyzer.preflight())
    assert captured["temperature"] == 0


def test_generic_400_does_not_open_circuit(tmp_path, monkeypatch):
    """preflight 通过后，分析阶段 400 只影响当前候选，后续候选仍执行。"""

    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(200, json=_success_response(_preflight_payload()))
        if request_count == 2:
            return httpx.Response(400, json={"error": {"message": "context too long"}})
        return httpx.Response(200, json=_success_response(_analysis_payload()))

    orchestrator = _orchestrator(tmp_path)
    orchestrator.ai = _analyzer(monkeypatch, handler)
    recorded = []
    orchestrator._record_stage = lambda run_id, stage, status, summary: recorded.append(summary)
    candidates = [_candidate("ONE"), _candidate("TWO")]
    run_dir = tmp_path / "run"
    (run_dir / "ai-cache").mkdir(parents=True)

    asyncio.run(orchestrator._run_ai_stage(
        "run-test", candidates, [0, 1],
        {
            0: {"slice_id": "slice-one", "contexts": []},
            1: {"slice_id": "slice-two", "contexts": []},
        },
        object(), True, run_dir,
    ))

    assert request_count == 3
    assert candidates[0]["analysis_status"] == "ai_failed"
    assert candidates[1]["analysis_status"] in ("completed", "ai_completed")
    assert recorded[0]["circuit_open"] is False


def test_parse_structured_response_handles_markdown_wrappers():
    """_parse_structured_response should handle ```json wrappers and mixed text."""

    from app.analysis.ai import _parse_structured_response
    # Pure JSON
    assert _parse_structured_response('{"ok": true}') == {"ok": True}
    # Markdown wrapped
    assert _parse_structured_response('```json\n{"ok": true}\n```') == {"ok": True}
    # JSON with surrounding text
    assert _parse_structured_response('Here is the result:\n{"ok": true}\nDone.') == {"ok": True}
    # Nested braces in strings
    result = _parse_structured_response('{"msg": "hello {world}"}')
    assert result == {"msg": "hello {world}"}
