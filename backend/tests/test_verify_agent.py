"""核验 agent 测试（T2.11，验收方案 A-1~A-22、N-1~N-7）。

fixture 复用 test_explorer 模式：真实索引（A.entry→B.run→C.write 调用链）
承载首轮上下文与 read_requests 取证；FakeVerifyAI 按轮弹出 VerifyOutput spec。
设计：docs/analysis/explorer-track/2026-08-22-t2-11-implementation-plan.md（含评审 R-1~R-10 修订）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.analysis.call_tree import CallTreeService
from app.analysis.index_store import SQLiteCodeIndexReader
from app.analysis.indexer import build_code_index
from app.analysis.verify_agent import (
    VerifyAgent,
    build_chain_facts,
    build_deterministic_facts,
    build_verify_claims,
)
from app.config import ExplorerSettings, VerifySettings

_CHAIN_SOURCE = {
    "com/example/A.java": """package com.example;
public class A {
  public void entry(String input) {
    B helper = new B();
    helper.run(input);
  }
}
""",
    "com/example/B.java": """package com.example;
public class B {
  public void run(String value) {
    C sink = new C();
    sink.write(value);
  }
}
""",
    "com/example/C.java": """package com.example;
public class C {
  public void write(String value) {
  }
}
""",
}


def _service(tmp_path: Path) -> CallTreeService:
    source_root = tmp_path / "sources"
    for relative, content in _CHAIN_SOURCE.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    descriptor = build_code_index(source_root, tmp_path / "index" / "code-index.json")
    return CallTreeService(tmp_path, SQLiteCodeIndexReader(descriptor), ExplorerSettings().call_tree)


def _method_id(call_tree: CallTreeService, qualified_class: str, name: str) -> str:
    rows = call_tree._reader.db.execute(
        "SELECT id FROM methods WHERE qualified_class = ? AND name = ?",
        (qualified_class, name),
    ).fetchall()
    assert rows
    return str(rows[0]["id"])


def _reader(tmp_path: Path) -> SQLiteCodeIndexReader:
    source_root = tmp_path / "sources"
    for relative, content in _CHAIN_SOURCE.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    descriptor = build_code_index(source_root, tmp_path / "index" / "code-index.json")
    return SQLiteCodeIndexReader(descriptor)


def _candidate(**overrides: Any) -> dict[str, Any]:
    candidate = {
        "candidate_id": "cand_verify_0001",
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "component": "activity",
        "component_name": "com.example.A",
        "severity_hint": "high",          # 假设层（盲验须剥离——A-5 断言）
        "confidence_tier": "medium",      # 假设层
        "evidence_level": "L2",
        "locations": [{"artifact": "code", "path": "com/example/A.java", "line": 3}],
        "sources": [{"kind": "source_expression", "status": "fact",
                     "path": "com/example/A.java", "line": 3, "text": "entry(input)"}],
        "sinks": [{"kind": "sink_call", "status": "fact",
                   "path": "com/example/C.java", "line": 4, "text": "write(value)"}],
        "blocking_gaps": [],              # 假设层（severity 语义）
        "guard_status": "unknown",
        "authorization_status": "unknown",
        "reachability_status": "reachable",
        "dataflow_status": "not_proven",
        "deterministic_chain_verified": False,
    }
    candidate.update(overrides)
    return candidate


def _explorer_candidate(call_tree: CallTreeService) -> dict[str, Any]:
    a = _method_id(call_tree, "com.example.A", "entry")
    b = _method_id(call_tree, "com.example.B", "run")
    c = _method_id(call_tree, "com.example.C", "write")
    row = call_tree._reader.db.execute(
        "SELECT start_line FROM call_sites WHERE method_id = ? AND resolve_status = 'resolved' LIMIT 1",
        (a,),
    ).fetchone()
    assert row is not None
    return {
        "candidate_id": "expl_" + "a" * 20,
        "chain_proposal": {
            "source": "A.entry(input)", "sink": "C.write(value)",
            "hops": [
                {"from_method_id": a, "to_method_id": b, "call_site_line": int(row["start_line"]),
                 "resolved_via": "direct_call"},
                {"from_method_id": b, "to_method_id": c, "call_site_line": 4,
                 "resolved_via": "direct_call"},
            ],
            "confidence": "high", "hypothesis": "likely",        # 假设层（剥离）
            "impact_proposal": "外部输入流向敏感写入",            # 假设层
            "reasoning": "调用链可见",                             # 假设层
            "needs_expansion": False,                             # 假设层
            "evidence_refs": [
                {"path": "com/example/A.java", "line": 3, "claim": "提出者主张的锚定文本"},  # R-5 剥离
            ],
        },
    }


class FakeVerifyAI:
    """verify_entry 协议替身：按轮弹出 VerifyOutput spec（捕获每轮输入）。"""

    def __init__(self, rounds: list[dict[str, Any]] | None = None):
        self._rounds = list(rounds or [{}])
        self.inputs: list[Any] = []
        self.calls = 0

    async def __call__(self, model_input: Any) -> dict[str, Any]:
        self.calls += 1
        self.inputs.append(model_input)
        spec = self._rounds.pop(0) if self._rounds else {}
        if spec.get("fail"):
            return {
                "status": spec["fail"],
                "circuit_breaking": spec.get("circuit", False),
                "metadata": {},
            }
        return {
            "status": "completed",
            "analysis": {
                "summary": spec.get("summary", "核验一轮"),
                "verdict": spec.get("verdict", "unresolved"),
                "confidence_tier": spec.get("confidence_tier", "medium"),
                "flaw_holds": spec.get("flaw_holds", False),
                "exploitability": spec.get("exploitability", {
                    "entry_reachable": True, "propagation_proven": False,
                    "sink_effective": False, "guard_bypassed": False,
                    "authorization_absent": True, "exfiltration_channel": "unverified",
                }),
                "claims_verdicts": spec.get("verdicts", []),
                "evidence_refs": spec.get("evidence", []),
                "read_requests": spec.get("requests", []),
                "loop": {"done": spec.get("done", True), "reason": spec.get("loop_reason", "测试")},
                "analysis_complete": spec.get("complete", True),
            },
            "metadata": {"prompt_version": "1.0.0", "model": "test-model"},
        }


def _agent(
    tmp_path: Path, fake: FakeVerifyAI, **settings: Any
) -> tuple[VerifyAgent, CallTreeService]:
    call_tree = _service(tmp_path)
    reader = _reader(tmp_path)
    agent = VerifyAgent(fake, call_tree, VerifySettings(**settings), tmp_path, reader)
    return agent, call_tree


def asyncio_run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# A-1：协议入口
# ---------------------------------------------------------------------------


def test_verify_entry_invokes_prompt(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, patch

    from app.analysis.ai_models import VerifyInput
    from app.analysis.ai_runtime import AIRuntime
    from app.config import AISettings

    analyzer = AIRuntime(AISettings()).create_analyzer(
        cache_dir=tmp_path, max_output_tokens=100, budget_policy={}
    )
    model_input = VerifyInput.model_validate({
        "candidate_id": "cand_x",
        "claims": [{"index": 0, "statement": "入口可达？", "kind": "entry_reachable"}],
    })
    invoke = AsyncMock(return_value={"status": "completed", "analysis": {
        "summary": "s", "verdict": "unresolved", "confidence_tier": "low",
        "flaw_holds": False, "exploitability": {
            "entry_reachable": False, "propagation_proven": False, "sink_effective": False,
            "guard_bypassed": False, "authorization_absent": True, "exfiltration_channel": "unverified",
        }, "loop": {"done": True, "reason": "t"}, "analysis_complete": True,
    }})
    with (
        patch.object(analyzer, "_analysis_unavailable_result", return_value=None),
        patch.object(analyzer, "_invoke_prompt", invoke),
    ):
        result = asyncio_run(analyzer.verify_entry(model_input))
    assert result["status"] == "completed"
    invoke.assert_awaited_once()
    args = invoke.await_args.args
    assert args[0] == "verify"
    assert args[1] == "1.0.0"
    assert args[2] is model_input
    assert args[3].__name__ == "VerifyOutput"
    assert args[4] == "verify"


# ---------------------------------------------------------------------------
# A-2~A-4：命题生成器
# ---------------------------------------------------------------------------


def test_build_claims_six_kinds() -> None:
    claims = build_verify_claims(_candidate(
        guard_status="present_bypassable", authorization_status="none",
    ))
    kinds = [claim["kind"] for claim in claims]
    assert kinds == [
        "entry_reachable", "source_controllability", "propagation",
        "sink_behavior", "guard_effective", "authorization",
    ]
    assert [claim["index"] for claim in claims] == list(range(6))
    assert "com/example/A.java:3" in claims[1]["statement"]
    assert "com/example/C.java:4" in claims[3]["statement"]


def test_build_claims_minimal() -> None:
    claims = build_verify_claims({"component_name": "com.example.A"})
    assert [claim["kind"] for claim in claims] == ["entry_reachable"]


def test_build_claims_deterministic() -> None:
    candidate = _candidate(guard_status="absent")
    first = json.dumps(build_verify_claims(candidate), sort_keys=True)
    second = json.dumps(build_verify_claims(candidate), sort_keys=True)
    assert first == second


def test_build_claims_missing_status_not_triggered() -> None:
    # R-10②：字段缺失（None）与 "unknown" 同样不触发
    claims = build_verify_claims(_candidate())
    kinds = {claim["kind"] for claim in claims}
    assert "guard_effective" not in kinds and "authorization" not in kinds


# ---------------------------------------------------------------------------
# A-5/A-6/A-7：盲验构造
# ---------------------------------------------------------------------------


def test_blind_input_contains_no_hypothesis_layer(tmp_path: Path) -> None:
    """A-5（M2 验收 4.3-6.1）：核验请求输入不含探索假设层（trace 断言口径）。"""

    fake = FakeVerifyAI([{"verdicts": [
        {"index": 0, "conclusion": "confirmed", "reasoning": "入口为公开组件"},
    ]}])
    agent, call_tree = _agent(tmp_path, fake)

    asyncio_run(agent.verify(_candidate(), _explorer_candidate(call_tree)))

    serialized = json.dumps(
        fake.inputs[0].model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    )
    for banned in (
        "severity_hint", "confidence_tier", "hypothesis", "impact_proposal",
        "blocking_gaps", "reasoning", "needs_expansion",
    ):
        assert banned not in serialized, f"盲验输入泄漏假设层字段：{banned}"
    # R-5：所有 evidence_refs 的 claim 均为 null（提出者文本剥离）
    for ref in fake.inputs[0].chain_facts.evidence_refs:
        assert ref.claim is None


def test_build_facts_structured() -> None:
    facts = build_deterministic_facts(_candidate(
        guard_status="absent", guard_blocked=True, authorization_status="none",
    ))
    types = [fact["fact_type"] for fact in facts]
    assert types == ["component", "reachability", "guard", "authorization", "source", "sink"]
    assert "确定性阻断" in next(f["statement"] for f in facts if f["fact_type"] == "guard")
    assert "com/example/C.java:4" in next(f["statement"] for f in facts if f["fact_type"] == "sink")


def test_chain_facts_stripped(tmp_path: Path) -> None:
    call_tree = _service(tmp_path)
    chain = build_chain_facts(_explorer_candidate(call_tree))
    assert chain is not None
    assert set(chain) == {"source", "sink", "hops", "call_tree_refs", "evidence_refs"}
    assert len(chain["hops"]) == 2
    assert all(ref["claim"] is None for ref in chain["evidence_refs"])
    assert build_chain_facts(None) is None
    assert build_chain_facts({"chain_proposal": {"hops": []}}) is None


# ---------------------------------------------------------------------------
# A-8：首轮上下文双路径
# ---------------------------------------------------------------------------


def test_initial_context_dual_path(tmp_path: Path) -> None:
    fake = FakeVerifyAI([{"verdicts": []}])
    agent, call_tree = _agent(tmp_path, fake)

    # 路径①：sinks 带 method_id → 方法体
    sinks_method = _method_id(call_tree, "com.example.C", "write")
    asyncio_run(agent.verify(_candidate(sinks=[{
        "kind": "sink_call", "status": "fact", "path": "com/example/C.java",
        "line": 4, "text": "write(value)", "method_id": sinks_method,
    }])))
    assert fake.inputs[0].code_context is not None
    assert "write" in fake.inputs[0].code_context

    # 路径②：无 method_id 仅 path:line → 行窗口（R-9）
    fake2 = FakeVerifyAI([{"verdicts": []}])
    agent2, _ = _agent(tmp_path, fake2)
    asyncio_run(agent2.verify(_candidate()))
    assert fake2.inputs[0].code_context is not None
    assert "entry" in fake2.inputs[0].code_context
    assert len(fake2.inputs[0].code_context) <= 9500


# ---------------------------------------------------------------------------
# A-9/A-9b/A-10/A-11：循环终止语义
# ---------------------------------------------------------------------------


def _verdicts_for(claims_count: int, upto: int, conclusion: str = "confirmed") -> list[dict]:
    return [
        {"index": index, "conclusion": conclusion, "reasoning": "取证判定"}
        for index in range(min(upto, claims_count))
    ]


def test_terminate_all_decided(tmp_path: Path) -> None:
    """A-9：模型不自声明（done=false）但命题全判定 → 代码终止。"""

    candidate = _candidate(guard_status="absent")  # 5 条命题
    fake = FakeVerifyAI([{"verdicts": _verdicts_for(5, 5), "done": False, "complete": False}])
    agent, _ = _agent(tmp_path, fake)

    result = asyncio_run(agent.verify(candidate))

    assert result["terminated_by"] == "all_claims_decided"
    assert result["requests_used"] == 1
    assert result["undecided_claim_indices"] == []
    assert result["output"]["analysis_complete"] is True


def test_loop_done_does_not_terminate(tmp_path: Path) -> None:
    """A-9b（评审 R-3）：loop.done=true 但命题未全判定 → 循环继续。"""

    candidate = _candidate()  # 4 条命题
    fake = FakeVerifyAI([
        {"verdicts": _verdicts_for(4, 2), "done": True, "complete": True},
        {"verdicts": _verdicts_for(4, 4), "done": True},
    ])
    agent, _ = _agent(tmp_path, fake)

    result = asyncio_run(agent.verify(candidate))

    assert fake.calls == 2  # done=true 未终止——轮 2 继续
    assert result["terminated_by"] == "all_claims_decided"
    assert result["requests_used"] == 2


def test_partial_then_complete_merge(tmp_path: Path) -> None:
    """A-10：后轮覆盖前轮同 index（still_unknown → confirmed）。"""

    candidate = _candidate()  # 4 条
    fake = FakeVerifyAI([
        {"verdicts": [
            {"index": 0, "conclusion": "still_unknown", "reasoning": "证据不足"},
            {"index": 1, "conclusion": "confirmed", "reasoning": "入口导出"},
        ]},
        {"verdicts": _verdicts_for(4, 4)},
    ])
    agent, _ = _agent(tmp_path, fake)

    result = asyncio_run(agent.verify(candidate))

    merged = {v["index"]: v["conclusion"] for v in result["output"]["claims_verdicts"]}
    assert merged == {0: "confirmed", 1: "confirmed", 2: "confirmed", 3: "confirmed"}


def test_round_budget_exhaustion(tmp_path: Path) -> None:
    """A-11：轮数预算尽——已证命题保留 + 缺口清单物化（R-6）。"""

    candidate = _candidate()  # 4 条命题，每轮只判 0 条
    fake = FakeVerifyAI([{"verdicts": [], "done": False, "complete": False}] * 4)
    agent, _ = _agent(tmp_path, fake)

    result = asyncio_run(agent.verify(candidate))

    assert result["terminated_by"] == "round_budget"
    assert result["requests_used"] == 4
    assert result["output"]["analysis_complete"] is False
    assert result["undecided_claim_indices"] == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# A-12/A-13：取证读码
# ---------------------------------------------------------------------------


def test_read_requests_executed(tmp_path: Path) -> None:
    """A-12：read_requests 取回内容进入下一轮输入。"""

    candidate = _candidate()  # 4 条
    a_method = None
    agent, call_tree = _agent(tmp_path, FakeVerifyAI())
    a_method = _method_id(call_tree, "com.example.A", "entry")
    fake = FakeVerifyAI([
        {"verdicts": _verdicts_for(4, 1), "done": False, "requests": [
            {"operation": "get_method_body", "target": a_method, "reason": "入口取证"},
        ]},
        {"verdicts": _verdicts_for(4, 4)},
    ])
    agent._ai_call = fake

    asyncio_run(agent.verify(candidate))

    assert agent.read_requests_used == 1
    assert "entry" in fake.inputs[1].code_context


def test_request_budget_early_termination(tmp_path: Path) -> None:
    """A-13（评审 R-4）：读码预算耗尽且尚有未判定命题 → 提前终止省空转轮。"""

    candidate = _candidate()  # 4 条
    agent, call_tree = _agent(tmp_path, FakeVerifyAI(), max_requests_per_candidate=1)
    a_method = _method_id(call_tree, "com.example.A", "entry")
    fake = FakeVerifyAI([{"verdicts": _verdicts_for(4, 1), "done": False, "requests": [
        {"operation": "get_method_body", "target": a_method, "reason": "取证"},
    ]}] * 4)
    agent._ai_call = fake

    result = asyncio_run(agent.verify(candidate))

    assert result["terminated_by"] == "request_budget"
    assert result["requests_used"] == 1  # 无空转轮
    assert result["undecided_claim_indices"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# A-14：证据回查过滤
# ---------------------------------------------------------------------------


def test_evidence_filtered(tmp_path: Path) -> None:
    candidate = _candidate()  # 4 条
    fake = FakeVerifyAI([{
        "verdicts": _verdicts_for(4, 4),
        "evidence": [
            {"path": "com/example/B.java", "line": 4},
            {"path": "com/example/Nope.java", "line": 1},
            {"path": "com/example/B.java", "line": 9999},
        ],
    }])
    agent, _ = _agent(tmp_path, fake)

    result = asyncio_run(agent.verify(candidate))

    kept = result["output"]["evidence_refs"]
    assert [ref["line"] for ref in kept] == [4]
    assert "不可回查证据已丢弃 2 条" in result["output"]["evidence_filter_note"]


# ---------------------------------------------------------------------------
# A-15/A-15b/A-16/A-17：一致性校验
# ---------------------------------------------------------------------------


def test_consistency_rules_flaw_conflict(tmp_path: Path) -> None:
    """A-15：supports+flaw_holds=False / refutes+flaw_holds=True 均降级。"""

    candidate = _candidate()
    fake = FakeVerifyAI([{
        "verdicts": _verdicts_for(4, 4), "verdict": "supports_candidate", "flaw_holds": False,
    }])
    agent, _ = _agent(tmp_path, fake)
    result = asyncio_run(agent.verify(candidate))
    assert result["consistency_downgraded"] is True
    assert result["output"]["verdict"] == "unresolved"
    assert "flaw_holds=False" in result["output"]["consistency_note"]

    fake2 = FakeVerifyAI([{
        "verdicts": _verdicts_for(4, 4, conclusion="refuted"),
        "verdict": "refutes_candidate", "flaw_holds": True,
    }])
    agent2, _ = _agent(tmp_path, fake2)
    result2 = asyncio_run(agent2.verify(_candidate()))
    assert result2["consistency_downgraded"] is True
    assert result2["output"]["verdict"] == "unresolved"


def test_consistency_rule_core_unknown(tmp_path: Path) -> None:
    """A-15b（评审 R-2）：supports + 核心命题 still_unknown → 降级。"""

    candidate = _candidate()
    fake = FakeVerifyAI([{
        "verdicts": [
            {"index": 0, "conclusion": "still_unknown", "reasoning": "传播未证"},
            {"index": 1, "conclusion": "confirmed", "reasoning": "入口导出"},
            {"index": 2, "conclusion": "confirmed", "reasoning": "传播成立"},
            {"index": 3, "conclusion": "confirmed", "reasoning": "sink 有效"},
        ],
        "verdict": "supports_candidate", "flaw_holds": True,
    }])
    agent, _ = _agent(tmp_path, fake)

    result = asyncio_run(agent.verify(candidate))

    assert result["consistency_downgraded"] is True
    assert result["output"]["verdict"] == "unresolved"
    assert "still_unknown" in result["output"]["consistency_note"]
    # claims 原文保留（人工可辨——降级不改写命题判定）
    assert result["output"]["claims_verdicts"][0]["conclusion"] == "still_unknown"


def test_consistency_rule_core_refuted(tmp_path: Path) -> None:
    """A-16：supports + 核心命题 refuted → 降级。"""

    candidate = _candidate()
    fake = FakeVerifyAI([{
        "verdicts": [
            {"index": 0, "conclusion": "confirmed", "reasoning": "入口导出"},
            {"index": 1, "conclusion": "confirmed", "reasoning": "可控"},
            {"index": 2, "conclusion": "refuted", "reasoning": "传播中断"},
            {"index": 3, "conclusion": "confirmed", "reasoning": "sink 有效"},
        ],
        "verdict": "supports_candidate", "flaw_holds": True,
    }])
    agent, _ = _agent(tmp_path, fake)

    result = asyncio_run(agent.verify(candidate))
    assert result["consistency_downgraded"] is True


def test_consistency_non_core_refuted_not_triggered(tmp_path: Path) -> None:
    """A-17：非核心命题（guard_effective）refuted 不触发降级。"""

    candidate = _candidate(guard_status="present_bypassable")  # 5 条，index 4=guard
    fake = FakeVerifyAI([{
        "verdicts": [
            {"index": 0, "conclusion": "confirmed", "reasoning": "入口导出"},
            {"index": 1, "conclusion": "confirmed", "reasoning": "可控"},
            {"index": 2, "conclusion": "confirmed", "reasoning": "传播成立"},
            {"index": 3, "conclusion": "confirmed", "reasoning": "sink 有效"},
            {"index": 4, "conclusion": "refuted", "reasoning": "Guard 可绕过"},
        ],
        "verdict": "supports_candidate", "flaw_holds": True,
    }])
    agent, _ = _agent(tmp_path, fake)

    result = asyncio_run(agent.verify(candidate))
    assert result["consistency_downgraded"] is False
    assert result["output"]["verdict"] == "supports_candidate"


# ---------------------------------------------------------------------------
# A-18~A-22：容错与契约
# ---------------------------------------------------------------------------


def test_ai_failure_tolerated(tmp_path: Path) -> None:
    fake = FakeVerifyAI([{"fail": "error"}])
    agent, _ = _agent(tmp_path, fake)

    result = asyncio_run(agent.verify(_candidate()))

    assert result["status"] == "failed"
    assert result["terminated_by"] == "error"
    assert result["undecided_claim_indices"] == [0, 1, 2, 3]
    assert len(result["rounds"]) == 1


def test_circuit_skipped(tmp_path: Path) -> None:
    fake = FakeVerifyAI([{"fail": "circuit_open", "circuit": True}])
    agent, _ = _agent(tmp_path, fake)

    result = asyncio_run(agent.verify(_candidate()))

    assert result["status"] == "skipped"
    assert result["terminated_by"] == "short_circuit"


def test_observation_persisted(tmp_path: Path) -> None:
    """A-20：轮审计落盘（追加 + model_input_hash + output 全量）。"""

    fake = FakeVerifyAI([{"verdicts": _verdicts_for(4, 4)}])
    agent, _ = _agent(tmp_path, fake)
    asyncio_run(agent.verify(_candidate()))

    path = tmp_path / "verify" / "observations.json"
    payload = json.loads(path.read_text("utf-8"))
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["terminated_by"] == "all_claims_decided"
    assert entry["rounds"][0]["model_input_hash"]
    assert entry["rounds"][0]["output"]["verdict"]


def test_result_contract_complete(tmp_path: Path) -> None:
    """A-22：返回契约字段齐备（T2.12 消费——含 claims 供适配层 join）。"""

    fake = FakeVerifyAI([{"verdicts": _verdicts_for(4, 4)}])
    agent, _ = _agent(tmp_path, fake)
    result = asyncio_run(agent.verify(_candidate()))

    assert set(result) == {
        "status", "terminated_by", "output", "claims", "rounds",
        "requests_used", "read_requests_used",
        "undecided_claim_indices", "consistency_downgraded",
    }
    assert result["status"] == "completed"
    assert result["requests_used"] == 1
    assert [claim["index"] for claim in result["claims"]] == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# N-1~N-7：边界与负例
# ---------------------------------------------------------------------------


def test_no_claims_skipped(tmp_path: Path) -> None:
    """N-1（评审 R-8）：无可证命题（component_name 也缺）→ 快速返回不构造输入。"""

    fake = FakeVerifyAI()
    agent, _ = _agent(tmp_path, fake)

    result = asyncio_run(agent.verify({"candidate_id": "x"}))

    assert result["status"] == "skipped"
    assert result["terminated_by"] == "no_claims"
    assert fake.calls == 0


def test_output_invalid_fails(tmp_path: Path) -> None:
    """N-3：模型输出违反 schema（缺 required）→ failed。"""

    class BadOutputAI(FakeVerifyAI):
        async def __call__(self, model_input: Any) -> dict[str, Any]:
            self.calls += 1
            self.inputs.append(model_input)
            return {"status": "completed", "analysis": {"summary": "缺字段"},
                    "metadata": {}}

    agent, _ = _agent(tmp_path, BadOutputAI())
    result = asyncio_run(agent.verify(_candidate()))
    assert result["status"] == "failed"
    assert result["rounds"][0]["status"] == "output_invalid"


def test_unknown_read_operation_not_found(tmp_path: Path) -> None:
    """N-4：不存在目标 → not_found 统一结构（dispatch 兜底；协议层四操作
    枚举已挡未知操作，此处验证实现层兜底口径）。"""

    candidate = _candidate()
    fake = FakeVerifyAI([
        {"verdicts": _verdicts_for(4, 1), "done": False, "requests": [
            {"operation": "get_method_body", "target": "missing#Nope:1", "reason": "取证"},
        ]},
        {"verdicts": _verdicts_for(4, 4)},
    ])
    agent, _ = _agent(tmp_path, fake)

    asyncio_run(agent.verify(candidate))

    # 目标缺失统一 not_found，第二轮输入含该结构
    assert agent.read_requests_used == 1
    assert "not_found" in (fake.inputs[1].code_context or "")


def test_observation_corrupted_reinit(tmp_path: Path) -> None:
    """N-5：observations.json 损坏 → 重新初始化不抛。"""

    verify_dir = tmp_path / "verify"
    verify_dir.mkdir(parents=True)
    (verify_dir / "observations.json").write_text("not-json{", "utf-8")

    fake = FakeVerifyAI([{"verdicts": _verdicts_for(4, 4)}])
    agent, _ = _agent(tmp_path, fake)
    result = asyncio_run(agent.verify(_candidate()))

    assert result["status"] == "completed"
    payload = json.loads((verify_dir / "observations.json").read_text("utf-8"))
    assert len(payload["entries"]) == 1


def test_evidence_end_line_inverted_dropped(tmp_path: Path) -> None:
    """N-6：end_line < line 倒序区间证据 → 回查失败丢弃。"""

    fake = FakeVerifyAI([{
        "verdicts": _verdicts_for(4, 4),
        "evidence": [{"path": "com/example/B.java", "line": 4, "end_line": 2}],
    }])
    agent, _ = _agent(tmp_path, fake)

    result = asyncio_run(agent.verify(_candidate()))
    assert result["output"]["evidence_refs"] == []


def test_repeated_verify_independent(tmp_path: Path) -> None:
    """N-7：连续 verify 同候选——独立执行无状态残留。"""

    fake = FakeVerifyAI([{"verdicts": _verdicts_for(4, 4)}] * 2)
    agent, _ = _agent(tmp_path, fake)

    first = asyncio_run(agent.verify(_candidate()))
    second = asyncio_run(agent.verify(_candidate()))

    assert first["requests_used"] == 1 and second["requests_used"] == 1
    payload = json.loads((tmp_path / "verify" / "observations.json").read_text("utf-8"))
    assert len(payload["entries"]) == 2


# ---------------------------------------------------------------------------
# T2.12：适配层（adapt_verify_result / _to_evidence_reference）
# ---------------------------------------------------------------------------

from app.analysis.verify_agent import (
    adapt_verify_result,
    evidence_contexts_for,
)


def _verify_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "completed",
        "terminated_by": "all_claims_decided",
        "output": {
            "summary": "全部命题已判定",
            "verdict": "supports_candidate",
            "confidence_tier": "high",
            "flaw_holds": True,
            "exploitability": {
                "entry_reachable": True, "propagation_proven": True, "sink_effective": True,
                "guard_bypassed": False, "authorization_absent": True,
                "exfiltration_channel": "unverified",
            },
            "claims_verdicts": [
                {"index": 0, "conclusion": "confirmed", "reasoning": "入口导出",
                 "evidence": []},
                {"index": 4, "conclusion": "refuted", "reasoning": "guard 无效",
                 "evidence": []},
            ],
            "evidence_refs": [
                {"path": "com/example/B.java", "line": 4, "end_line": 4, "claim": None},
                {"path": "com/example/C.java", "claim": None},
            ],
            "refutation_basis": [],
            "analysis_complete": True,
        },
        "claims": [
            {"index": 0, "kind": "entry_reachable", "statement": "入口可达？"},
            {"index": 4, "kind": "guard_effective", "statement": "guard 有效？"},
        ],
        "rounds": [{"round_index": 1, "model_input_hash": "0" * 64,
                    "prompt_version": "verify/1.0.0", "model": "test-model",
                    "status": "completed", "output": {}}],
        "requests_used": 1,
        "read_requests_used": 2,
        "undecided_claim_indices": [],
        "consistency_downgraded": False,
    }
    result.update(overrides)
    return result


def test_adapt_fields_complete() -> None:
    """A-1：适配层字段补齐（L2 同构 + 确定性默认 + verify 溯源）。"""

    analysis = adapt_verify_result(_verify_result())

    assert analysis["analysis_track"] == "verify"
    assert analysis["guard_status"] == "unknown"
    assert analysis["verdict"] == "supports_candidate"
    assert analysis["promotion_recommended"] is True
    assert analysis["harm"]["impact_type"] == "other"
    assert analysis["reachability_class"] == "local"
    assert analysis["impact_vector"]["confidentiality"] == "none"
    assert analysis["reverse_exclusion"] == []
    assert analysis["verified_evidence_refs"] == analysis["evidence_refs"]
    assert analysis["invalid_evidence_refs"] == []
    assert analysis["verify_agent"]["terminated_by"] == "all_claims_decided"
    # 评审 R-6：guard_effective 命题判定进溯源
    assert analysis["verify_agent"]["guard_claim_verdict"] == {
        "conclusion": "refuted", "reasoning": "guard 无效"}


def test_adapt_evidence_reference_conversion() -> None:
    """A-2/A-3：context_id=path#window 格式；无 line 证据静默丢弃。"""

    analysis = adapt_verify_result(_verify_result())
    refs = analysis["evidence_refs"]
    assert len(refs) == 1  # 无 line 的 C.java 证据被丢弃（D3）
    assert refs[0]["context_id"] == "com/example/B.java#window:4-4"
    assert refs[0]["claim"].startswith("verify agent 回查通过")
    contexts = evidence_contexts_for(analysis)
    assert contexts == [
        {"context_id": "com/example/B.java#window:4-4", "kind": "code_window",
         "path": "com/example/B.java", "start_line": 4, "end_line": 4},
    ]


def test_adapt_undecided_gap_and_consistency_trace() -> None:
    """A-4/A-5：undecided 缺口物化 + 一致性降级溯源。"""

    undecided = adapt_verify_result(_verify_result(
        undecided_claim_indices=[1, 2],
        output={**_verify_result()["output"], "analysis_complete": False},
    ))
    assert undecided["blocking_gaps"] == [{
        "code": "VERIFY_CLAIMS_UNDECIDED", "critical": True,
        "message": "核验预算内未完成全部命题判定（未判定 2 项）",
        "evidence_refs": [],
    }]
    assert undecided["promotion_recommended"] is False
    assert undecided["analysis_complete"] is False

    downgraded = adapt_verify_result(_verify_result(consistency_downgraded=True))
    assert "一致性校验降级" in downgraded["confidence_rationale"]
    assert downgraded["verify_agent"]["consistency_downgraded"] is True


def test_adapted_analysis_end_to_end_production_path(tmp_path: Path) -> None:
    """A-6（评审 R-3）：生产路径端到端——verify_candidate → DecisionEngine.decide。

    断言 invalid_evidence_refs 为空（R-1 的 ai_evidence_contexts 注入生效）、
    无 AI_EVIDENCE_REQUIREMENTS_UNRESOLVED（R-2 的 track 识别生效）、
    evidence_decision 不因证据校验失败而拦截。
    """

    from app.findings.decision import DecisionEngine
    from app.findings.evidence import verify_candidate

    reader = _reader(tmp_path)
    code_index = json.loads(
        (tmp_path / "index" / "code-index.json").read_text("utf-8")
    )
    candidate = _candidate(candidate_id="cand_e2e_0001")
    candidate.update({
        "scope_key": "scope_x", "chain_key": "chain_x",
        "deterministic_fact_hash": "facts_x",
        "entry_points": ["com.example.A"],
        "ai_evidence_contexts": None,
    })

    analysis = adapt_verify_result(_verify_result())
    candidate["ai_analysis"] = analysis
    candidate["ai_evidence_contexts"] = evidence_contexts_for(analysis)
    candidate["analysis_track"] = "verify"
    candidate["candidate_verdict"] = analysis["verdict"]
    candidate["confidence_tier"] = analysis["confidence_tier"]

    evidence = verify_candidate(candidate, code_index, reader)
    gap_codes = [gap.get("code") for gap in evidence.get("ai_evidence_blocking_gaps") or []]
    assert evidence["invalid_evidence_refs"] == []
    assert "AI_EVIDENCE_REQUIREMENTS_UNRESOLVED" not in gap_codes
    assert "AI_EVIDENCE_REF_INVALID" not in gap_codes

    decided = DecisionEngine().decide(dict(candidate))
    assert decided["evidence_decision"] in {
        "supported", "ai_likely_supported", "unresolved", "pending_manual",
        "blocked",
    }
