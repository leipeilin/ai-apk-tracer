"""探索 Agent 协议层测试（T2.5a）。

设计：docs/analysis/2026-08-22-t2-5a-implementation-plan.md（含评审
R-1~R-8 修订：既有输出模型零改动——本测试同时回归锚定其行为锚点：
四操作枚举/_done_requires_chain 校验器/结构化 ComponentSummary）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.analysis.ai_models import (
    ComponentSummary,
    ExplorerInput,
    ExplorerObservation,
    ReadRequest,
)

PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "prompts"
REGISTRY_PATH = PROMPTS_ROOT / "registry.yaml"


def _hop(**overrides):
    hop = {
        "from_method_id": "sources/com/example/A.java#A.entry:5",
        "to_method_id": "sources/com/example/B.java#B.run:9",
        "call_site_line": 6,
        "resolved_via": "direct_call",
    }
    hop.update(overrides)
    return hop


def _proposal(**overrides):
    proposal = {
        "source": "A.entry(intent)",
        "sink": "C.write(value)",
        "hops": [_hop()],
        "confidence": "medium",
        "hypothesis": "possible",
        "impact_proposal": "外部输入流向本地写入",
        "reasoning": "入口方法调用链经 B.run 到达 C.write",
    }
    proposal.update(overrides)
    return proposal


def _observation(**overrides):
    observation = {
        "read_requests": [],
        "chain_proposals": [_proposal()],
        "component_summary": {
            "component": "com.example.A",
            "kind": "activity",
            "exported": True,
            "summary": "入口 Activity，处理外部 Intent 并分发处理",
        },
        "loop": {"done": True, "reason": "已形成到 sink 的完整链"},
    }
    observation.update(overrides)
    return observation


def _input(**overrides):
    payload = {
        "round_index": 1,
        "rounds_budget": 4,
        "requests_budget": 20,
        "entry_json": json.dumps({
            "entry_id": "act_com_example_A_entry", "kind": "activity",
            "component_name": "com.example.A", "source": "manifest",
            "entry_method": "entry(android.content.Intent)->void",
        }),
        "attack_surface_json": json.dumps({
            "kind": "activity", "name": "com.example.A", "exported": True,
        }),
        "prior_observations": None,
        "code_context": None,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# ExplorerInput（本任务新增模型）
# ---------------------------------------------------------------------------


def test_explorer_input_round_trip() -> None:
    model = ExplorerInput.model_validate(_input())
    dumped = model.model_dump(mode="json")
    assert ExplorerInput.model_validate(dumped) == model
    assert model.round_index == 1
    assert "act_com_example_A_entry" in model.entry_json
    # 可选上下文字段（首轮为空、后续轮注入）
    assert model.prior_observations is None and model.code_context is None


def test_input_budget_boundaries() -> None:
    with pytest.raises(ValidationError):
        ExplorerInput.model_validate(_input(round_index=0))
    with pytest.raises(ValidationError):
        ExplorerInput.model_validate(_input(rounds_budget=0))
    with pytest.raises(ValidationError):
        ExplorerInput.model_validate(_input(requests_budget=-1))


# ---------------------------------------------------------------------------
# 既有模型行为回归锚定（评审 R-1/R-3：零改动的锚点验证）
# ---------------------------------------------------------------------------


def test_observation_round_trip() -> None:
    model = ExplorerObservation.model_validate(_observation())
    dumped = model.model_dump(mode="json")
    assert ExplorerObservation.model_validate(dumped) == model


def test_observation_done_requires_chain() -> None:
    """R-3 决断回归锚定：done=true 必须伴随链；done=false + 空提案合法。"""
    with pytest.raises(ValidationError):
        ExplorerObservation.model_validate(
            _observation(chain_proposals=[], loop={"done": True, "reason": "x"})
        )
    # done=false + 空提案（首轮探索）合法
    model = ExplorerObservation.model_validate(
        _observation(chain_proposals=[], loop={"done": False, "reason": "需更多上下文"})
    )
    assert model.loop.done is False


def test_read_request_four_operations() -> None:
    """R-2 决断回归锚定：操作面恒四操作（resolve_invoke_target/class_hierarchy 不暴露）。"""
    valid = ReadRequest.model_validate({
        "operation": "get_method_body",
        "target": "sources/com/example/A.java#A.entry:5",
        "reason": "确认入口方法的完整实现",
    })
    assert valid.operation == "get_method_body"
    assert valid.path is None and valid.line is None
    # 非法操作（含被排除的两个内部实现）拒绝
    for operation in ("resolve_invoke_target", "class_hierarchy", "get_entry_points", "bogus"):
        with pytest.raises(ValidationError):
            ReadRequest.model_validate({"operation": operation, "target": "x", "reason": "r"})
    # reason 必填（审计）
    with pytest.raises(ValidationError):
        ReadRequest.model_validate({"operation": "search_symbol", "target": "write"})


def test_component_summary_structured() -> None:
    model = ComponentSummary.model_validate({
        "component": "com.example.A", "kind": "activity",
        "exported": True, "summary": "入口组件",
    })
    assert model.kind == "activity"
    with pytest.raises(ValidationError):
        ComponentSummary.model_validate({
            "component": "x", "kind": "widget", "exported": True, "summary": "s",
        })
    with pytest.raises(ValidationError):
        ExplorerObservation.model_validate(_observation(component_summary={
            "component": "x", "kind": "activity", "exported": "yes", "summary": "s",
        }))


# ---------------------------------------------------------------------------
# registry 注册（A-6）与 prompt 防回归约束（R-6）
# ---------------------------------------------------------------------------


def test_registry_entry_registered() -> None:
    import yaml

    registry = yaml.safe_load(REGISTRY_PATH.read_bytes())
    entries = {item["id"]: item for item in registry["prompts"]}
    entry = entries["explorer"]
    assert entry["version"] == "1.0.0"
    assert entry["allowed_placeholders"] == ["explorer_input_json"]
    assert entry["input_model"] == "ExplorerInput"
    assert entry["output_model"] == "ExplorerObservation"
    assert entry["input_schema_file"] == "ai_explorer_input.schema.json"
    assert entry["output_schema_file"] == "ai_explorer_observation.schema.json"
    assert entry["template_sha256"]["system"] != "0" * 64  # sync 已生成真实哈希


def test_prompt_declares_required_and_enums() -> None:
    """评审 R-6：prompt 声明必填字段与枚举（防回归约束缺失）。"""
    system = (PROMPTS_ROOT / "explorer" / "1.0.0" / "system.md").read_text("utf-8")
    # 必填字段声明（嵌套 required 提示）
    for token in ("reason", "call_site_line", "entry_json", "component_summary"):
        assert token in system, f"prompt 缺少必填字段声明: {token}"
    # 枚举声明
    for token in ("likely", "possible", "unlikely", "get_method_body", "get_callees", "get_callers", "search_symbol"):
        assert token in system, f"prompt 缺少枚举声明: {token}"
    # 禁止项（不下结论/不臆造/禁附加字段）
    for token in ("不得下", "不得臆造", "禁止附加字段"):
        assert token in system, f"prompt 缺少禁止项约束: {token}"
    # loop.done 校验器语义对齐
    assert "done=true 必须伴随至少一条 chain_proposal" in system
