"""ExplorerCandidate → Candidate 归一化映射表可执行断言（T0.6）。

映射表规范见 docs/analysis/2026-08-22-t0-6-normalization-mapping.md；
本文件固化 MAPPING / SEVERITY_KEYWORDS 作为可执行契约（防双源漂移），
T2.7 归一化实现须满足这些断言。
"""

import json
import re
from pathlib import Path

from app.analysis.ai_models import (
    ChainProposal,
    ExplorerCandidate,
    ExplorerCandidateComponent,
    ExplorerCandidateValidation,
)

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"

# ---------------------------------------------------------------------------
# 可执行契约：归一化映射声明（与映射表文档 §2/§3 一致）
# ---------------------------------------------------------------------------

# source_kind:
#   constant  -> {"kind":"constant","value":...}
#   source    -> {"kind":"source","path":"component.kind"}
#   enum_map  -> {"kind":"enum_map","path":...,"mapping":{...},"none_default":...}
#   transform -> {"kind":"transform","from":[...],"branches":[...]}
MAPPING: dict[str, dict] = {
    "rule_id": {"kind": "constant", "value": "EXPLORER_AGENT"},
    "rule_version": {"kind": "source", "path": "prompt_version"},
    "component": {
        "kind": "enum_map",
        "path": "component.kind",
        "mapping": {"activity": "activity", "service": "service", "provider": "provider", "receiver": "receiver"},
        "other_handling": "drop_with_audit",
    },
    "severity_hint": {"kind": "transform", "from": ["chain_proposal.impact_proposal"], "cap": "high"},
    "confidence_tier": {
        "kind": "enum_map",
        "path": "validation.status",
        "mapping": {"validated": "high", "partially_validated": "medium", "unverified": "low", "pending": "low"},
        "none_default": "low",
    },
    "evidence_level": {"kind": "constant", "value": "L2"},
    "locations": {"kind": "transform", "from": ["chain_proposal.evidence_refs", "chain_proposal.hops"]},
    "sources": {"kind": "transform", "from": ["chain_proposal.source", "chain_proposal.hops", "chain_proposal.evidence_refs"]},
    "sinks": {"kind": "transform", "from": ["chain_proposal.sink", "chain_proposal.hops"]},
    "blocking_gaps": {
        "kind": "transform",
        "from": ["validation"],
        "branches": ["notes", "failed_hop_indices", "custom_sink_proposal", "blocked_by_guard", "severity_hypothesis"],
        "item_fields": ["code", "message", "critical", "evidence_refs"],
    },
}

SEVERITY_KEYWORDS: list[tuple[list[str], str]] = [
    (["任意", "远程", "执行", "泄露", "敏感", "提权", "注入"], "high"),
    (["拒绝服务", "越权", "绕过", "数据"], "medium"),
    (["信息", "提示", "低风险", "暴露"], "low"),
]

_NESTED_MODELS = {
    "chain_proposal": ChainProposal,
    "component": ExplorerCandidateComponent,
    "validation": ExplorerCandidateValidation,
}


def _load_candidate_schema() -> dict:
    with (SCHEMAS_DIR / "candidate.schema.json").open(encoding="utf-8") as fp:
        return json.load(fp)


def _severity_for_impact(impact_proposal: str) -> str:
    """关键词启发式（映射表 §5）：按行序首个命中返回；未命中默认 medium；初始档封顶 high。"""
    text = impact_proposal.lower()
    for keywords, level in SEVERITY_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return level
    return "medium"


def _method_id_parts(method_id: str) -> tuple[str, str | None]:
    """method_id `path#Class.method:line` 解析（映射表 §6）。"""
    path = method_id.split("#", 1)[0]
    line = method_id.rpartition(":")[2]
    return path, line or None


def _path_exists(path: str) -> bool:
    parts = path.split(".")
    model = ExplorerCandidate
    for index, part in enumerate(parts):
        if part not in model.model_fields:
            return False
        if index < len(parts) - 1:
            model = _NESTED_MODELS.get(part, model)
    return True


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

def test_mapping_covers_all_required_candidate_fields() -> None:
    schema = _load_candidate_schema()
    missing = [field for field in schema["required"] if field not in MAPPING]
    assert not missing, f"映射表未覆盖 candidate required 字段: {missing}"


def test_mapping_sources_exist_in_explorer_candidate() -> None:
    for target, spec in MAPPING.items():
        paths: list[str] = []
        if spec.get("path"):
            paths.append(spec["path"])
        if spec.get("from"):
            paths.extend(spec["from"])
        for path in paths:
            assert _path_exists(path), f"{target} 来源字段路径不存在于模型: {path}"


def test_mapping_constant_and_enum_values_valid() -> None:
    schema = _load_candidate_schema()
    properties = schema["properties"]

    # 常量
    assert re.fullmatch(r"^[A-Z0-9_]+$", MAPPING["rule_id"]["value"])
    assert MAPPING["evidence_level"]["value"] in properties["evidence_level"]["enum"]
    assert MAPPING["severity_hint"]["cap"] in properties["severity_hint"]["enum"]

    # enum_map 映射值落在目标枚举
    for field, spec in MAPPING.items():
        if spec.get("kind") != "enum_map":
            continue
        enum = properties[field].get("enum")
        if enum is None:
            continue
        for value in spec["mapping"].values():
            assert value in enum, f"{field} 映射值 {value} 不在候选枚举 {enum}"
        if spec.get("none_default"):
            assert spec["none_default"] in enum

    # component 枚举映射源（ExplorerCandidateComponent.kind）与目标无冲突
    component_spec = MAPPING["component"]
    assert "other" not in component_spec["mapping"]


def test_component_other_handling_declared() -> None:
    spec = MAPPING["component"]
    assert spec["other_handling"] == "drop_with_audit"
    assert "other" not in spec["mapping"]


def test_severity_keyword_rules() -> None:
    # 各关键词命中
    assert _severity_for_impact("存在任意文件执行") == "high"
    assert _severity_for_impact("可能远程数据泄露") == "high"
    assert _severity_for_impact("拒绝服务风险") == "medium"
    assert _severity_for_impact("仅提示信息") == "low"
    assert _severity_for_impact("无关描述") == "medium"
    # 子串匹配（关键词出现在文本任意位置，与位置无关）
    assert _severity_for_impact("前置后置执行说明") == "high"
    # 按行序首个命中（冲突时 high 胜出）
    assert _severity_for_impact("包含数据泄露，属敏感信息") == "high"
    # 初始档封顶 high（不判 critical）
    assert _severity_for_impact("任意代码执行") == "high"
    # 最小长度：root 已删除，避免 uproot 误命中
    assert _severity_for_impact("component uproot calls") == "medium"


def test_confidence_tier_pending_default() -> None:
    spec = MAPPING["confidence_tier"]
    assert spec["mapping"]["pending"] == "low"
    assert spec["none_default"] == "low"


def test_blocking_gaps_assembly_spec() -> None:
    spec = MAPPING["blocking_gaps"]
    assert set(spec["branches"]) == {"notes", "failed_hop_indices", "custom_sink_proposal", "blocked_by_guard", "severity_hypothesis"}
    assert set(spec["item_fields"]) == {"code", "message", "critical", "evidence_refs"}
    # 与既有 BlockingGap 模型字段对齐（message 而非 detail）
    from app.analysis.ai_models import BlockingGap

    for field in spec["item_fields"]:
        assert field in BlockingGap.model_fields, f"blocking_gaps item 字段 {field} 不在 BlockingGap 模型"


def test_locations_fallback_with_empty_evidence_refs() -> None:
    # method_id 解析（映射表 §6）
    path, line = _method_id_parts("sources/com/example/SplashActivity.java#onCreate:42")
    assert path == "sources/com/example/SplashActivity.java"
    assert line == "42"
    # evidence_refs=[] 时 fallback 到 hops[0].from_method_id 解析
    method_id = "sources/com/example/WebHelper.java#loadUrl:120"
    fallback_path, _ = _method_id_parts(method_id)
    assert fallback_path == "sources/com/example/WebHelper.java"
