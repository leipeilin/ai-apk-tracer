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
    """评审 R-6：prompt 声明必填字段与枚举（防回归约束缺失）。

    EXPLORER-PROMPT-FIX：追加严格输出契约断言（只输出 JSON/禁止旧字段/
    顶层 loop 显式声明/字段名/数组上限——实施方案 §3.3；S-4）。
    """
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
    # 严格输出契约（EXPLORER-PROMPT-FIX S-4：schema_invalid 根因防护）
    assert "只输出一个 JSON 对象" in system, "prompt 缺少'只输出一个 JSON 对象'约束"
    assert "禁止使用旧字段名" in system, "prompt 缺少'禁止使用旧字段名'约束"
    assert "顶层必填字段：component_summary、loop" in system, "prompt 缺少顶层必填字段显式声明"
    for token in ("read_requests", "chain_proposals"):
        assert token in system, f"prompt 缺少字段名声明: {token}"
    # 数组上限声明（Pydantic 校验先于驱动归一化——超限即 schema_invalid）
    for token in ("1-32 个", "最多 32 个", "最多 16 个", "最多 64 个"):
        assert token in system, f"prompt 缺少数组上限声明: {token}"
    # M2-DEFECT-FIX D-3：无据产链禁令（首轮无 code_context 时模型编造 hops
    # → 跳回查必然失败 → validated=0 的质量根因防护）
    assert "禁止无据产链" in system, "prompt 缺少'禁止无据产链'约束"
    assert "code_context 为 null" in system, "prompt 缺少无上下文禁链条件声明"
    assert "由驱动层预算终止承载" in system, "prompt 缺少预算尽与禁链的优先级声明"
    # M2 收尾：空转禁令（探针新发现——4 入口 done=false 且 read_requests 空、
    # 4 轮零信息增益耗尽预算；信息稀少入口须主动取证不得静默放弃）
    assert "禁止空转轮" in system, "prompt 缺少'禁止空转轮'约束"
    assert "done=false 且 read_requests 为空" in system, "prompt 缺少空转轮判定条件声明"
    assert "get_callers/get_callees" in system, "prompt 缺少信息稀少入口的主动取证指导"
    # M4-SEED-HOPS：骨架链使用（评审 R-4 修正——三要素确定性 + D-3 不豁免）
    assert "骨架链使用" in system, "prompt 缺少'骨架链使用'约束"
    assert "复制进 chain_proposals 的 hops 即通过跳回查" in system, "prompt 缺少 seed 三要素可回查声明"
    assert "起点骨架而非结论" in system, "prompt 缺少 seed 语义边界声明"
    assert "约束 10 不因 seed 豁免" in system, "prompt 缺少 D-3 不豁免声明"
    # 约束 4 的可回查来源枚举补 seed_hops（评审 R-4）
    assert "entry_json/code_context/seed_hops" in system, "约束 4 未声明 seed_hops 来源"
