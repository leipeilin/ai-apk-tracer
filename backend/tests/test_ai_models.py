from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.analysis.ai_models import (
    AI_SCHEMA_MODELS,
    AITraceEntry,
    ExplorerObservation,
    L1TriageOutput,
    L2ReviewOutput,
    PreflightOutput,
    SchemaSerialization,
)
from app.config import WORKSPACE_ROOT


def _l1_output() -> dict:
    return {
        "summary": "当前证据显示存在值得确定性验证的潜在线索。",
        "triage_disposition": "potential_chain",
        "analysis_complete": True,
    }


def _l2_output() -> dict:
    return {
        "summary": "确定性证据支持候选。",
        "verdict": "supports_candidate",
        "confidence_tier": "high",
        "guard_status": "absent",
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


def test_valid_strict_outputs() -> None:
    assert L1TriageOutput.model_validate(_l1_output()).analysis_complete is True
    assert L2ReviewOutput.model_validate(_l2_output()).verdict == "supports_candidate"
    assert PreflightOutput.model_validate(
        {"ok": True, "message": "结构化输出能力正常。", "analysis_complete": True}
    ).ok is True


@pytest.mark.parametrize("model,payload", [
    (L1TriageOutput, _l1_output()),
    (L2ReviewOutput, _l2_output()),
])
def test_analysis_complete_is_required(model, payload: dict) -> None:
    payload.pop("analysis_complete")

    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)

    assert error.value.errors()[0]["type"] == "missing"


def test_extra_fields_are_forbidden() -> None:
    payload = {**_l2_output(), "invented_field": "不得接受"}

    with pytest.raises(ValidationError) as error:
        L2ReviewOutput.model_validate(payload)

    assert any(item["type"] == "extra_forbidden" for item in error.value.errors())


@pytest.mark.parametrize("field,value", [
    ("verdict", "confirmed"),
    ("confidence_tier", "certain"),
    ("guard_status", "protected"),
])
def test_l2_rejects_wrong_enums(field: str, value: str) -> None:
    payload = {**_l2_output(), field: value}

    with pytest.raises(ValidationError) as error:
        L2ReviewOutput.model_validate(payload)

    assert any(item["type"] == "literal_error" for item in error.value.errors())


def test_l1_rejects_wrong_triage_disposition() -> None:
    payload = {**_l1_output(), "triage_disposition": "false_positive"}

    with pytest.raises(ValidationError, match="triage_disposition"):
        L1TriageOutput.model_validate(payload)


def test_strict_mode_rejects_type_coercion() -> None:
    payload = {**_l2_output(), "analysis_complete": 1}

    with pytest.raises(ValidationError) as error:
        L2ReviewOutput.model_validate(payload)

    assert any(item["type"] == "bool_type" for item in error.value.errors())


def test_trace_entry_requires_all_hashes_and_strict_fields() -> None:
    digest = "a" * 64
    trace = AITraceEntry.model_validate({
        "prompt_id": "l2-review",
        "prompt_version": "2.0.0",
        "analysis_track": "l2_review",
        "round": 0,
        "model": "unit-test-model",
        "status": "completed",
        "system_template_sha256": digest,
        "user_template_sha256": digest,
        "system_rendered_sha256": digest,
        "user_rendered_sha256": digest,
        "input_schema_sha256": digest,
        "output_schema_sha256": digest,
        "analysis_complete": True,
    })

    assert trace.output_schema_sha256 == digest


def test_committed_schemas_exactly_match_stable_model_generation() -> None:
    schema_root = Path(WORKSPACE_ROOT) / "schemas"

    for filename, model in AI_SCHEMA_MODELS.items():
        path = schema_root / filename
        raw = path.read_bytes()
        assert raw == SchemaSerialization.bytes_for(model), filename
        assert json.loads(raw.decode("utf-8")) == model.model_json_schema(), filename
        assert model.model_config["extra"] == "forbid"
        assert model.model_config["strict"] is True


def test_key_protocol_schema_fields_have_specific_descriptions() -> None:
    l1 = L1TriageOutput.model_json_schema()
    l2 = L2ReviewOutput.model_json_schema()
    trace = AITraceEntry.model_json_schema()

    assert "不代表漏洞成立" in l1["properties"]["analysis_complete"]["description"]
    assert "可回查" in l1["properties"]["evidence_refs"]["description"]
    assert "支持、反驳" in l2["properties"]["verdict"]["description"]
    assert "实际约束状态" in l2["properties"]["guard_status"]["description"]
    assert trace["description"] == "记录一次可复现 AI 调用所需的不可变摘要。"


def test_ai_protocol_sync_check_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(WORKSPACE_ROOT / "scripts/sync-ai-protocol.py"), "--check"],
        cwd=WORKSPACE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_pure_analysis_strips_injected_fields() -> None:
    """回归：扩片 previous_output 不得携带代码注入字段。

    3.0.0 run（20260808T153906Z）12 候选 failed：orchestrator 注入
    verified/invalid_evidence_refs 等字段到 analysis，扩片时 previous_output
    原样传给模型，模型照抄导致 L2ReviewOutput extra_forbidden。
    """

    from app.analysis.ai import _pure_analysis

    injected = {
        "summary": "复核完成",
        "verdict": "unresolved",
        "analysis_track": "l2_review",
        "candidate_verdict": "unresolved",
        "promotion_recommended": False,
        "verified_evidence_refs": [],
        "invalid_evidence_refs": ["ctx-1"],
        "flaw_holds": False,
    }
    pure = _pure_analysis(injected)

    assert pure["summary"] == "复核完成"
    assert pure["verdict"] == "unresolved"
    assert pure["flaw_holds"] is False
    for field in ("analysis_track", "candidate_verdict", "promotion_recommended",
                  "verified_evidence_refs", "invalid_evidence_refs"):
        assert field not in pure, f"注入字段 {field} 必须被剥离"
    assert _pure_analysis(None) is None


# ---------------------------------------------------------------------------
# 探索轨 ExplorerObservation（T0.1）：低信任建议链 + 读码请求
# ---------------------------------------------------------------------------

def _explorer_observation() -> dict:
    return {
        "read_requests": [
            {"operation": "get_callees", "target": "com.example.WebHelper.loadUrl", "reason": "追查 loadUrl 的调用方"}
        ],
        "chain_proposals": [
            {
                "source": "Intent.getExtras().getString",
                "sink": "WebView.loadUrl",
                "hops": [
                    {"from_method_id": "sources/com/example/SplashActivity.java#onCreate:42",
                     "to_method_id": "sources/com/example/WebHelper.java#loadUrl:120",
                     "call_site_line": 55,
                     "arg_positions": [0],
                     "resolved_via": "direct_call"}
                ],
                "evidence_refs": [{"path": "sources/com/example/SplashActivity.java", "line": 42}],
                "confidence": "medium",
                "hypothesis": "likely",
                "impact_proposal": "外部 Intent 可控制 WebView 加载 URL，可能构成任意 URL 加载攻击面",
                "reasoning": "外部 intent 可控制 URL 并传入 loadUrl，未见 scheme 校验",
            }
        ],
        "component_summary": {
            "component": "com.example.SplashActivity",
            "kind": "activity",
            "exported": True,
            "summary": "启动入口，将外部 Intent 参数传入 WebView",
        },
        "loop": {"done": True, "reason": "已形成完整 sink 链"},
    }


def test_explorer_observation_valid() -> None:
    obs = ExplorerObservation.model_validate(_explorer_observation())
    assert obs.loop.done is True
    assert obs.chain_proposals[0].hops[0].resolved_via == "direct_call"
    # 轻量证据：path 必填、line/claim 可空
    assert obs.chain_proposals[0].evidence_refs[0].claim is None


def test_explorer_observation_first_round_empty_arrays_allowed() -> None:
    payload = _explorer_observation()
    payload["read_requests"] = []
    payload["chain_proposals"] = []
    payload["loop"] = {"done": False, "reason": "首轮，先请求上下文"}
    obs = ExplorerObservation.model_validate(payload)
    assert obs.read_requests == []
    assert obs.chain_proposals == []


@pytest.mark.parametrize("field", ["component_summary", "loop"])
def test_explorer_observation_required_missing(field: str) -> None:
    payload = _explorer_observation()
    payload.pop(field)
    with pytest.raises(ValidationError) as error:
        ExplorerObservation.model_validate(payload)
    assert error.value.errors()[0]["type"] == "missing"


def test_explorer_observation_empty_hops_rejected() -> None:
    payload = _explorer_observation()
    payload["chain_proposals"][0]["hops"] = []
    with pytest.raises(ValidationError) as error:
        ExplorerObservation.model_validate(payload)
    assert error.value.errors()[0]["type"] == "too_short"


def test_explorer_observation_extra_fields_forbidden() -> None:
    payload = {**_explorer_observation(), "invented_field": "x"}
    with pytest.raises(ValidationError) as error:
        ExplorerObservation.model_validate(payload)
    assert any(item["type"] == "extra_forbidden" for item in error.value.errors())


@pytest.mark.parametrize("field,values", [
    ("hypothesis", ["confirmed", "no"]),
    ("confidence", ["certain", "max"]),
    ("resolved_via", ["nonsense", "indirect"]),
])
def test_explorer_observation_rejects_wrong_enums(field: str, values: list[str]) -> None:
    for value in values:
        payload = _explorer_observation()
        payload["chain_proposals"][0][field] = value
        with pytest.raises(ValidationError):
            ExplorerObservation.model_validate(payload)


@pytest.mark.parametrize("value", ["get_bogus", "resolve_invoke_target", "class_hierarchy"])
def test_read_request_rejects_operations_outside_whitelist(value: str) -> None:
    payload = _explorer_observation()
    payload["read_requests"][0]["operation"] = value
    with pytest.raises(ValidationError):
        ExplorerObservation.model_validate(payload)


@pytest.mark.parametrize("value", ["widget", "fragment"])
def test_component_summary_rejects_wrong_kind(value: str) -> None:
    payload = _explorer_observation()
    payload["component_summary"]["kind"] = value
    with pytest.raises(ValidationError):
        ExplorerObservation.model_validate(payload)


def test_explorer_observation_done_requires_chain() -> None:
    payload = _explorer_observation()
    payload["chain_proposals"] = []
    payload["loop"] = {"done": True, "reason": "应非法：done 但无链"}
    with pytest.raises(ValidationError, match="loop.done=True"):
        ExplorerObservation.model_validate(payload)


def test_explorer_observation_path_traversal_rejected() -> None:
    payload = _explorer_observation()
    payload["chain_proposals"][0]["evidence_refs"][0]["path"] = "../../etc/passwd"
    with pytest.raises(ValidationError):
        ExplorerObservation.model_validate(payload)


def test_explorer_observation_bounds_rejected() -> None:
    # 33 hops 超 max_length=32
    payload = _explorer_observation()
    hop = payload["chain_proposals"][0]["hops"][0]
    payload["chain_proposals"][0]["hops"] = [hop] * 33
    with pytest.raises(ValidationError):
        ExplorerObservation.model_validate(payload)
    # call_site_line=0 越界（ge=1）
    payload = _explorer_observation()
    payload["chain_proposals"][0]["hops"][0]["call_site_line"] = 0
    with pytest.raises(ValidationError):
        ExplorerObservation.model_validate(payload)
    # 超长 method_id（max_length=512）
    payload = _explorer_observation()
    payload["chain_proposals"][0]["hops"][0]["from_method_id"] = "x" * 513
    with pytest.raises(ValidationError):
        ExplorerObservation.model_validate(payload)
    # arg_positions 负数拒绝（ge=0；A-5 验收点）
    payload = _explorer_observation()
    payload["chain_proposals"][0]["hops"][0]["arg_positions"] = [-1]
    with pytest.raises(ValidationError):
        ExplorerObservation.model_validate(payload)


def test_explorer_loop_state_requires_reason() -> None:
    payload = _explorer_observation()
    payload["loop"].pop("reason")
    with pytest.raises(ValidationError) as error:
        ExplorerObservation.model_validate(payload)
    assert error.value.errors()[0]["type"] == "missing"
