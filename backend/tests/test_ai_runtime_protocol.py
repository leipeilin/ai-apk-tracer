from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest

from app.analysis.ai import OpenAICompatibleAnalyzer
from app.analysis.prompt_registry import PromptRegistry
from app.config import AISettings


API_KEY_ENV = "AI_RUNTIME_PROTOCOL_TEST_KEY"
BASE_URL = "https://ai-runtime.invalid/v1"


def _settings(**values) -> AISettings:
    defaults = {
        "enabled": True,
        "base_url": BASE_URL,
        "api_key_env": API_KEY_ENV,
        "model": "test-model",
        "allow_external_code": True,
        "timeout_seconds": 1,
    }
    return AISettings(**{**defaults, **values})


def _response(content: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]},
    )


def _l1_output(disposition: str = "potential_chain") -> dict:
    return {
        "summary": "L1 分诊完成",
        "triage_disposition": disposition,
        "suggested_sources": [],
        "suggested_sinks": [],
        "suggested_paths": [],
        "guard_observations": [],
        "evidence_refs": [],
        "blocking_gaps": [],
        "uncertainties": [],
        "context_requests": [],
        "analysis_complete": True,
    }


def _l2_output(verdict: str = "supports_candidate") -> dict:
    return {
        "summary": "L2 复核完成",
        "verdict": verdict,
        "confidence_tier": "high",
        "guard_status": "absent",
        "evidence_refs": [],
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


def _finalization_output(
    verdict: str = "unresolved",
    recommendation: str = "pending_manual",
) -> dict:
    return {
        "summary": "最终归并完成",
        "verdict": verdict,
        "review_recommendation": recommendation,
        "evidence_refs": [],
        "blocking_gaps": [],
        "uncertainties": [],
        "analysis_complete": True,
    }


def _analyzer(monkeypatch, handler, *, prompt_registry=None) -> OpenAICompatibleAnalyzer:
    monkeypatch.setenv(API_KEY_ENV, "unit-test-token")
    return OpenAICompatibleAnalyzer(
        _settings(),
        transport=httpx.MockTransport(handler),
        retry_backoff_seconds=0,
        prompt_registry=prompt_registry,
    )


def test_preflight_uses_injected_registry_system_and_user_and_records_hashes(monkeypatch) -> None:
    class RecordingRegistry:
        def __init__(self) -> None:
            self.delegate = PromptRegistry()
            self.calls = []

        def render(self, prompt_id, version, variables):
            self.calls.append((prompt_id, version, variables))
            return self.delegate.render(prompt_id, version, variables)

    registry = RecordingRegistry()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return _response({
            "ok": True,
            "message": "严格协议可用",
            "acknowledged_capabilities": ["strict_json_object"],
            "analysis_complete": True,
        })

    result = asyncio.run(_analyzer(monkeypatch, handler, prompt_registry=registry).preflight())

    assert result["status"] == "passed"
    assert registry.calls[0][0:2] == ("preflight", "1.0.1")
    assert [item["role"] for item in captured["payload"]["messages"]] == ["system", "user"]
    metadata = result["metadata"]
    assert metadata["prompt_id"] == "preflight"
    assert metadata["prompt_version"] == "1.0.1"
    assert metadata["structured_output_mode"] == "json_object"
    assert len(metadata["messages_hash"]) == 64
    assert len(metadata["request_hash"]) == 64
    assert metadata["prompt_hash"] == metadata["prompt_template_hash"]
    assert metadata["prompt_hash_semantics"] == "template_sha256"
    assert metadata["legacy_prompt_hash_messages_sha256"] == metadata["messages_hash"]
    assert metadata["rendered_prompt_hash"] != metadata["prompt_template_hash"]
    assert set(metadata["template_sha256"]) == {"system", "user"}
    assert set(metadata["rendered_sha256"]) == {"system", "user"}
    assert set(metadata["schema_sha256"]) == {"input", "output"}


def test_l1_facade_passes_deterministic_fields_and_bounds_contexts(monkeypatch) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return _response(_l1_output())

    deterministic = {
        "authorization_matrix": {"exported": True},
        "dataflow": {"status": "candidate"},
        "guard": {"status": "absent"},
        "binder_transactions": [{"code": 1}],
        "receiver_binding": {"dynamic": True},
        "started_service_state_machine": {"state": "started"},
        "fragment_reflection": {"class": "DemoFragment"},
        "slot_overwrites": [{"slot": 0}],
        "operation_taxonomy": ["read"],
        "coverage": {"methods": 4},
    }
    candidate = {"rule_id": "L1_TEST", "evidence_level": "L1", **deterministic}
    contexts = [{"context_id": f"ctx-{index}", "content": "x"} for index in range(300)]

    result = asyncio.run(_analyzer(monkeypatch, handler).analyze(
        candidate,
        {"slice_id": "slice-l1", "contexts": contexts},
    ))

    assert result["status"] == "completed"
    assert result["analysis"]["promotion_recommended"] is True
    assert result["analysis"]["candidate_verdict"] == "potential_chain"
    assert result["analysis"]["analysis_track"] == "l1_triage"
    model_input = json.loads(captured["payload"]["messages"][1]["content"].splitlines()[-1])
    assert len(model_input["semantic_bundle"]["contexts"]) == 256
    for key, value in deterministic.items():
        assert model_input["semantic_bundle"]["candidate"][key] == value


@pytest.mark.parametrize(
    ("verdict", "promotion_recommended"),
    [
        ("supports_candidate", True),
        ("refutes_candidate", False),
        ("unresolved", False),
    ],
)
def test_l2_facade_preserves_verdict_track(
    monkeypatch, verdict: str, promotion_recommended: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(_l2_output(verdict))

    result = asyncio.run(_analyzer(monkeypatch, handler).analyze(
        {"rule_id": "L2_TEST", "evidence_level": "L2"},
        {"slice_id": "slice-l2", "contexts": []},
    ))

    assert result["status"] == "completed"
    assert result["analysis"]["promotion_recommended"] is promotion_recommended
    assert result["analysis"]["candidate_verdict"] == verdict
    assert result["analysis"]["analysis_track"] == "l2_review"


def test_finalize_uses_registered_schema_without_applying_review_recommendation(monkeypatch) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return _response(_finalization_output("refutes_candidate", "ai_false_positive"))

    result = asyncio.run(_analyzer(monkeypatch, handler).finalize(
        {"rule_id": "FINAL", "evidence_level": "L2"},
        {"slice_id": "final", "contexts": []},
        _l2_output("refutes_candidate"),
    ))

    assert result["status"] == "completed"
    assert result["analysis"]["analysis_track"] == "finalization"
    assert result["analysis"]["source_analysis_track"] == "l2_review"
    assert result["analysis"]["review_recommendation"] == "ai_false_positive"
    assert result["analysis"]["candidate_verdict"] == "refutes_candidate"
    assert result["analysis"]["promotion_recommended"] is False
    assert "review_status" not in result["analysis"]
    assert "FinalizationOutput" in captured["payload"]["messages"][0]["content"]


def test_checkpoint_identity_changes_with_endpoint_and_covers_finalization(monkeypatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "unit-test-token")
    candidate = {"rule_id": "CHECKPOINT", "evidence_level": "L2"}
    context_slice = {"slice_id": "checkpoint", "contexts": []}
    first = OpenAICompatibleAnalyzer(_settings(base_url="https://one.invalid/v1"))
    second = OpenAICompatibleAnalyzer(_settings(base_url="https://two.invalid/v1"))

    first_identity = first.checkpoint_identity(candidate, context_slice)
    second_identity = second.checkpoint_identity(candidate, context_slice)

    assert first_identity["provider_kind"] == "openai-compatible"
    assert first_identity["base_url_hash"] != second_identity["base_url_hash"]
    assert first_identity["config_fingerprint"] != second_identity["config_fingerprint"]
    assert len(first_identity["api_key_env_hash"]) == 64
    assert first_identity["prompt"]["template_hash"]
    assert first_identity["prompt"]["schema_hash"]
    assert first_identity["finalization_prompt"]["output_model"] == "FinalizationOutput"


@pytest.mark.parametrize(
    "invalid_output",
    [
        {key: value for key, value in _l2_output().items() if key != "analysis_complete"},
        {**_l2_output(), "unexpected": True},
        {**_l2_output(), "verdict": "maybe"},
    ],
)
def test_strict_schema_errors_never_default_analysis_complete(
    monkeypatch, invalid_output: dict
) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _response(invalid_output)

    result = asyncio.run(_analyzer(monkeypatch, handler).review_l2(
        {"rule_id": "STRICT", "evidence_level": "L2"},
        {"slice_id": "strict", "contexts": []},
    ))

    assert request_count == 2
    assert result["status"] == "failed"
    assert result["classification"] == "schema_invalid"
    assert "analysis" not in result


def test_repair_uses_registry_prompt_and_records_accepted_response_hash(monkeypatch) -> None:
    initial = {key: value for key, value in _l2_output().items() if key != "analysis_complete"}
    repair = {
        "repaired_output": _l2_output("unresolved"),
        "analysis_complete": True,
    }
    contents = [
        json.dumps(initial, ensure_ascii=False),
        json.dumps(repair, ensure_ascii=False),
    ]
    captured_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        content = contents[len(captured_payloads) - 1]
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    result = asyncio.run(_analyzer(monkeypatch, handler).review_l2(
        {"rule_id": "REPAIR", "evidence_level": "L2"},
        {"slice_id": "repair", "contexts": []},
    ))

    assert result["status"] == "completed"
    assert result["analysis"]["analysis_complete"] is True
    assert "JSON 格式修复器" in captured_payloads[1]["messages"][0]["content"]
    assert [item["role"] for item in captured_payloads[1]["messages"]] == ["system", "user"]
    metadata = result["metadata"]
    assert metadata["initial_response_hash"] == hashlib.sha256(contents[0].encode()).hexdigest()
    assert metadata["repair_response_hash"] == hashlib.sha256(contents[1].encode()).hexdigest()
    assert metadata["accepted_response_hash"] == metadata["repair_response_hash"]
    assert metadata["initial_attempts"] == 1
    assert metadata["repair_attempts"] == 1
    assert metadata["attempts"] == 2
    assert "total_latency_ms" in metadata


def test_markdown_wrapped_analysis_records_protocol_relaxation(monkeypatch) -> None:
    content = "```json\n" + json.dumps(_l2_output(), ensure_ascii=False) + "\n```"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    result = asyncio.run(_analyzer(monkeypatch, handler).review_l2(
        {"rule_id": "MARKDOWN", "evidence_level": "L2"},
        {"slice_id": "markdown", "contexts": []},
    ))

    assert result["status"] == "completed"
    assert result["metadata"]["protocol_relaxed"] is True
    assert result["metadata"]["protocol_relaxation"] == "markdown_fence"


def test_duplicate_json_keys_are_rejected_without_repair(monkeypatch) -> None:
    request_count = 0
    content = '{"summary":"first","summary":"second"}'

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    result = asyncio.run(_analyzer(monkeypatch, handler).review_l2(
        {"rule_id": "DUPLICATE", "evidence_level": "L2"},
        {"slice_id": "duplicate", "contexts": []},
    ))

    assert request_count == 1
    assert result["status"] == "failed"
    assert result["classification"] == "response_invalid"
    assert result["metadata"]["protocol_relaxed"] is False


def test_missing_content_envelope_does_not_crash(monkeypatch) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"choices": [{"message": {}}]})

    result = asyncio.run(_analyzer(monkeypatch, handler).review_l2(
        {"rule_id": "NO_CONTENT", "evidence_level": "L2"},
        {"slice_id": "missing-content", "contexts": []},
    ))

    assert request_count == 2
    assert result["status"] == "failed"
    assert result["classification"] == "schema_invalid"
    assert result["metadata"]["initial_response_hash"] is None
