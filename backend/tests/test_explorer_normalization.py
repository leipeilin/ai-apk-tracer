"""T2.7 归一化与关联测试（验收方案 A-1~A-9/A-12、N-1/N-4/N-5）。

fixture 使用真实 ExplorerCandidate 形状（schemas/explorer_candidate.schema.json），
归一化产物经 schemas/candidate.schema.json 校验；guard/decision 断言对齐
主链双字段语义（评审 R-3）。
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from app.analysis.candidate_funnel import CandidateFunnel
from app.analysis.explorer_normalization import (
    link_related_candidates,
    normalize_explorer_candidates,
    severity_hint_for_impact,
)
from app.findings.decision import DecisionEngine

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


def _hop(from_id: str, to_id: str, line: int) -> dict:
    return {
        "from_method_id": from_id,
        "to_method_id": to_id,
        "call_site_line": line,
        "arg_positions": [0],
        "resolved_via": "direct_call",
    }


def _explorer_candidate(
    *,
    status: str = "validated",
    notes: str | None = "2/2 跳回查通过",
    failed_hops: list[int] | None = None,
    blocked_by_guard: bool = False,
    kind: str = "activity",
    impact: str = "外部输入经处理流向敏感操作",
    evidence_refs: list[dict] | None = None,
    hops: list[dict] | None = None,
    validation: dict | None = None,
) -> dict:
    if hops is None:
        hops = [
            _hop(
                "sources/com/example/SplashActivity.java#onCreate:20",
                "sources/com/example/SplashActivity.java#handleIntent:35",
                28,
            ),
            _hop(
                "sources/com/example/SplashActivity.java#handleIntent:35",
                "sources/com/example/Store.java#write:80",
                40,
            ),
        ]
    if validation is None:
        validation = {
            "status": status,
            "notes": notes,
            "verified_hop_count": 2 if status == "validated" else 1,
            "failed_hop_indices": failed_hops or [],
            "blocked_by_guard": blocked_by_guard,
            "custom_sink_proposal": False,
        }
    return {
        "schema_version": "1.0.0",
        "candidate_id": "expl_aaaaaaaaaaaaaaaaaaaa",
        "source": "explorer_agent",
        "prompt_version": "explorer/1.0.0",
        "model": "test-model",
        "component": {
            "kind": kind,
            "name": "com.example.SplashActivity",
            "exported": True,
            "entry_method": "onCreate",
        },
        "api_entry_ref": "act_com_example_SplashActivity_onCreate",
        "chain_proposal": {
            "source": "getIntent().getExtras()",
            "sink": "Store.write",
            "hops": hops,
            "confidence": "high",
            "hypothesis": "likely",
            "impact_proposal": impact,
            "reasoning": "调用链逐跳可见",
            "evidence_refs": evidence_refs if evidence_refs is not None else [
                {"path": "sources/com/example/SplashActivity.java", "line": 20},
                {"path": "sources/com/example/Store.java", "line": 80},
            ],
        },
        "validation": validation,
    }


def _candidate_schema() -> dict:
    with (SCHEMAS_DIR / "candidate.schema.json").open(encoding="utf-8") as fp:
        return json.load(fp)


# ---------------------------------------------------------------------------
# A-1：归一化产出合法 Candidate（10 项 required）
# ---------------------------------------------------------------------------

def test_normalize_validated_produces_schema_valid_candidate() -> None:
    normalized, counts = normalize_explorer_candidates([_explorer_candidate()])

    assert len(normalized) == 1
    assert counts["validated_total"] == 1
    assert counts["normalized"] == 1
    candidate = normalized[0]
    jsonschema.validate(candidate, _candidate_schema())
    # T0.6 映射表 §2 关键字段
    assert candidate["rule_id"] == "EXPLORER_AGENT"
    assert candidate["rule_version"] == "explorer/1.0.0"
    assert candidate["component"] == "activity"
    assert candidate["evidence_level"] == "L2"
    assert candidate["confidence_tier"] == "high"
    assert candidate["severity_hint"] == severity_hint_for_impact("外部输入经处理流向敏感操作")
    # 探索轨关联字段
    assert candidate["candidate_source"] == "explorer"
    assert candidate["explorer_candidate_id"] == "expl_aaaaaaaaaaaaaaaaaaaa"
    assert candidate["explorer_validation_status"] == "validated"
    # sources/sinks 形状（映射表 §2 #8/#9 + method_id 扩展）
    assert candidate["sources"] == [{
        "kind": "source_expression", "status": "fact",
        "path": "com/example/SplashActivity.java", "line": 20,
        "text": "getIntent().getExtras()",
    }]
    assert candidate["sinks"][0]["kind"] == "sink_call"
    assert candidate["sinks"][0]["status"] == "fact"
    assert candidate["sinks"][0]["path"] == "sources/com/example/Store.java"
    assert candidate["sinks"][0]["line"] == 40
    assert candidate["sinks"][0]["text"] == "Store.write"
    assert candidate["sinks"][0]["method_id"] == "sources/com/example/Store.java#write:80"
    # 非 required（映射表 §3；description 留空防锚定——评审 R-2）
    assert "description" not in candidate
    assert candidate["title"] == "Explorer Candidate"
    assert candidate["dataflow_status"] == "not_proven"
    assert candidate["deterministic_chain_verified"] is False
    assert candidate["analysis_status"] == "explorer_only"
    assert candidate["reachability_status"] == "reachable"  # exported=True
    assert candidate["chain_id"] == "expl_aaaaaaaaaaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# A-3/A-4：other drop 与仅 validated 归一化
# ---------------------------------------------------------------------------

def test_normalize_other_component_dropped_with_audit() -> None:
    candidates = [_explorer_candidate(kind="other")]
    normalized, counts = normalize_explorer_candidates(candidates)
    assert normalized == []
    assert counts["component_other_dropped"] == 1
    assert counts["validated_total"] == 1


def test_normalize_only_validated_status() -> None:
    candidates = [
        _explorer_candidate(),
        _explorer_candidate(status="partially_validated", notes="1/2 跳回查通过；失败跳 [1]"),
        _explorer_candidate(status="unverified", notes="跳均不可回查"),
        _explorer_candidate(validation={"status": "pending"}),
    ]
    normalized, counts = normalize_explorer_candidates(candidates)
    assert len(normalized) == 1
    assert counts["validated_total"] == 1
    assert counts["partial_kept"] == 1
    assert counts["unverified_kept"] == 2  # unverified + pending


# ---------------------------------------------------------------------------
# A-5/A-6：severity 启发式与 blocking_gaps 分支组装
# ---------------------------------------------------------------------------

def test_severity_gap_attached_on_keyword_hit() -> None:
    normalized, _ = normalize_explorer_candidates(
        [_explorer_candidate(impact="存在任意远程执行风险")]
    )
    gaps = normalized[0]["blocking_gaps"]
    assert normalized[0]["severity_hint"] == "high"
    assert any(g["code"] == "EXPLORER_SEVERITY_HYPOTHESIS" for g in gaps)


def test_clean_validated_has_no_incomplete_gap() -> None:
    # 评审 R-4：T2.6 validated 的 notes 恒为纯成功摘要（"N/N 跳回查通过"），
    # 不得产出 EXPLORER_CHAIN_INCOMPLETE（映射表 §4 末行"validated 且无上述→[]"可达）
    normalized, _ = normalize_explorer_candidates([_explorer_candidate()])
    codes = [g["code"] for g in normalized[0]["blocking_gaps"]]
    assert "EXPLORER_CHAIN_INCOMPLETE" not in codes


def test_guard_gap_is_critical_and_dual_fields() -> None:
    normalized, _ = normalize_explorer_candidates(
        [_explorer_candidate(blocked_by_guard=True)]
    )
    candidate = normalized[0]
    gaps = [g for g in candidate["blocking_gaps"] if g["code"] == "EXPLORER_GUARD_BLOCKED"]
    assert gaps and gaps[0]["critical"] is True
    # 评审 R-3：双字段（funnel 读布尔；decision 只认 guard_blocks 列表）
    assert candidate["guard_blocked"] is True
    assert candidate["guard_blocks"] == [{
        "type": "debuggable",
        "path": "sources/com/example/SplashActivity.java",
        "line": 28,
        "method": "onCreate",
    }]


def test_error_notes_produces_incomplete_gap() -> None:
    # 防御分支：notes 含异常语义（T2.6 异常路径 status=unverified 不归一化，
    # 此处验证 validated+异常 notes 的防御行为）
    normalized, _ = normalize_explorer_candidates(
        [_explorer_candidate(notes="回查过程异常（索引查询失败）")]
    )
    codes = [g["code"] for g in normalized[0]["blocking_gaps"]]
    assert "EXPLORER_CHAIN_INCOMPLETE" in codes


# ---------------------------------------------------------------------------
# A-7：guard 双字段 → funnel 不送 AI + decision 判 blocked
# ---------------------------------------------------------------------------

def test_guard_blocked_candidate_skips_ai_and_decides_blocked() -> None:
    normalized, _ = normalize_explorer_candidates(
        [_explorer_candidate(blocked_by_guard=True)]
    )
    candidate = normalized[0]
    funnel_result = CandidateFunnel().process([candidate])
    assert funnel_result.candidates[0]["ai_required"] is False
    assert funnel_result.candidates[0]["funnel_disposition"] == "explorer_promoted"
    # decision 层（评审 R-3）：guard_blocks 列表驱动 blocked 语义
    decided = DecisionEngine().decide(dict(candidate))
    assert decided["evidence_decision"] == "blocked"


# ---------------------------------------------------------------------------
# A-8：evidence_refs 的 sources/ 前缀剥离（索引口径对齐）
# ---------------------------------------------------------------------------

def test_evidence_refs_path_prefix_stripped() -> None:
    normalized, _ = normalize_explorer_candidates(
        [_explorer_candidate(evidence_refs=[{"path": "sources/com/example/A.java", "line": 5}])]
    )
    candidate = normalized[0]
    assert candidate["locations"][0]["path"] == "com/example/A.java"
    assert candidate["sources"][0]["path"] == "com/example/A.java"
    # hops 派生 path 与 files.path 同源（T2.6 评审认可），不做前缀处理
    assert candidate["sinks"][0]["path"] == "sources/com/example/Store.java"


def test_locations_fallback_to_first_hop() -> None:
    normalized, _ = normalize_explorer_candidates(
        [_explorer_candidate(evidence_refs=[])]
    )
    candidate = normalized[0]
    assert candidate["locations"] == [{
        "artifact": "code",
        "path": "sources/com/example/SplashActivity.java",
        "line": 28,
    }]
    assert candidate["sources"][0]["path"] == "sources/com/example/SplashActivity.java"
    assert candidate["sources"][0]["line"] == 28


# ---------------------------------------------------------------------------
# A-9/N-1：畸形输入跳过不中断
# ---------------------------------------------------------------------------

def test_malformed_candidate_skipped_with_count() -> None:
    broken = _explorer_candidate()
    broken["chain_proposal"]["hops"] = []
    normalized, counts = normalize_explorer_candidates([_explorer_candidate(), broken])
    assert len(normalized) == 1
    assert counts["normalization_errors"] == 1


# ---------------------------------------------------------------------------
# A-12/N-4/N-5：related_candidate_ids 关联
# ---------------------------------------------------------------------------

def _funnel_processed(*candidates: dict) -> list[dict]:
    result = CandidateFunnel().process(list(candidates))
    return result.candidates


def test_link_related_bidirectional_and_idempotent() -> None:
    explorer = normalize_explorer_candidates([_explorer_candidate()])[0][0]
    rule = {
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "rule_version": "1",
        "component": "activity",
        "component_name": "com.example.SplashActivity",
        "severity_hint": "high",
        "confidence_tier": "medium",
        "evidence_level": "L2",
        "locations": [],
        "sources": [],
        "sinks": [{
            "kind": "sink_call", "status": "fact",
            "path": "sources/com/example/Store.java", "line": 80,
            "method_id": "sources/com/example/Store.java#write:80",
        }],
        "blocking_gaps": [],
    }
    processed = _funnel_processed(explorer, rule)
    counts = link_related_candidates(processed)

    assert counts["pair_count"] == 1
    assert counts["explorer_linked"] == 1
    explorer_ids = {
        c["candidate_id"]: c for c in processed if c.get("candidate_source") == "explorer"
    }
    rule_ids = {
        c["candidate_id"]: c for c in processed if c.get("candidate_source") != "explorer"
    }
    explorer_c = next(iter(explorer_ids.values()))
    rule_c = next(iter(rule_ids.values()))
    assert rule_c["candidate_id"] in explorer_c["related_candidate_ids"]
    assert explorer_c["candidate_id"] in rule_c["related_candidate_ids"]

    # 幂等：二次调用不重复追加
    counts_again = link_related_candidates(processed)
    assert counts_again["pair_count"] == 0
    assert len(explorer_c["related_candidate_ids"]) == 1
    assert len(rule_c["related_candidate_ids"]) == 1


def test_link_related_no_match_on_different_chain() -> None:
    explorer = normalize_explorer_candidates([_explorer_candidate()])[0][0]
    # 不同组件名
    rule_other_component = {
        "rule_id": "R1", "rule_version": "1", "component": "activity",
        "component_name": "com.example.Other", "severity_hint": "high",
        "confidence_tier": "medium", "evidence_level": "L2",
        "locations": [], "sources": [],
        "sinks": [{"path": "sources/com/example/Store.java", "line": 80,
                   "method_id": "sources/com/example/Store.java#write:80"}],
        "blocking_gaps": [],
    }
    # 同组件不同 sink
    rule_other_sink = {
        "rule_id": "R2", "rule_version": "1", "component": "activity",
        "component_name": "com.example.SplashActivity", "severity_hint": "high",
        "confidence_tier": "medium", "evidence_level": "L2",
        "locations": [], "sources": [],
        "sinks": [{"path": "sources/com/example/Other.java", "line": 9,
                   "method_id": "sources/com/example/Other.java#other:9"}],
        "blocking_gaps": [],
    }
    processed = _funnel_processed(explorer, rule_other_component, rule_other_sink)
    counts = link_related_candidates(processed)
    assert counts["pair_count"] == 0
    for candidate in processed:
        assert not candidate.get("related_candidate_ids")


def test_link_related_explorer_candidates_do_not_link_each_other() -> None:
    # N-4：同源候选（探索×探索）不互写——仅 explorer→rule 方向配对
    first = normalize_explorer_candidates([_explorer_candidate()])[0][0]
    second = normalize_explorer_candidates(
        [_explorer_candidate(impact="另一条链的敏感数据泄露描述")]
    )[0][0]
    second["candidate_id"] = None  # 强制 funnel 生成不同 candidate_id
    processed = _funnel_processed(first, second)
    counts = link_related_candidates(processed)
    assert counts["pair_count"] == 0


def test_link_related_location_fallback_without_method_id() -> None:
    # 规则 sink 缺 method_id 时退化 (path, line) 精确匹配
    explorer = normalize_explorer_candidates([_explorer_candidate()])[0][0]
    rule = {
        "rule_id": "R3", "rule_version": "1", "component": "activity",
        "component_name": "com.example.SplashActivity", "severity_hint": "high",
        "confidence_tier": "medium", "evidence_level": "L2",
        "locations": [], "sources": [],
        "sinks": [{"kind": "sink_call", "status": "fact",
                   "path": "sources/com/example/Store.java", "line": 40}],
        "blocking_gaps": [],
    }
    processed = _funnel_processed(explorer, rule)
    counts = link_related_candidates(processed)
    assert counts["pair_count"] == 1
