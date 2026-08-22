"""核验分流与降级测试（T2.12，验收方案 A-7~A-18、N-1~N-6）。

实例级直驱 _verify_candidate（真实索引 + FakeVerifyEntryAI 协议替身）+
API 级集成（verify.enabled 主链不阻塞）。设计：docs/analysis/
2026-08-22-t2-12-implementation-plan.md（含评审 R-1~R-11 修订）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.analysis.indexer import build_code_index
from app.analysis.orchestrator import ScanOrchestrator
from app.analysis.verify_agent import evidence_contexts_for
from app.config import (
    Settings,
    SourceAnalysisSettings,
    StorageSettings,
    VerifySettings,
)
from app.main import create_app
from app.runs.storage import RunStorage
from app.shared.repository import SQLiteRepository

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


def _build_index(tmp_path: Path) -> dict[str, Any]:
    source_root = tmp_path / "sources"
    for relative, content in _CHAIN_SOURCE.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    return build_code_index(source_root, tmp_path / "index" / "code-index.json")


def _method_id(code_index: dict[str, Any], qualified_class: str, name: str) -> str:
    from app.analysis.index_store import SQLiteCodeIndexReader

    reader = SQLiteCodeIndexReader(code_index)
    try:
        row = reader.db.execute(
            "SELECT id FROM methods WHERE qualified_class = ? AND name = ?",
            (qualified_class, name),
        ).fetchone()
        assert row is not None
        return str(row["id"])
    finally:
        reader.close()


def _orchestrator(
    tmp_path: Path, *, verify_enabled: bool = True,
    fallback: bool = True, ai: Any = None, run_suffix: int = 0,
) -> tuple[ScanOrchestrator, RunStorage, str, Path, dict[str, Any]]:
    settings = Settings(
        database_path=tmp_path / "tracer.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "data"),
        source_analysis=SourceAnalysisSettings(enabled=False),
        verify=VerifySettings(enabled=verify_enabled, fallback_to_single_turn_l2=fallback),
    )
    repository = SQLiteRepository(settings.resolved_database_path())
    repository.initialize()
    storage = RunStorage(settings.resolved_data_root(), settings.storage)
    run_id = f"20260822T000000Z_{run_suffix:012d}_bbbbbbbb"
    run_dir = storage.runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text("{}", "utf-8")
    code_index = _build_index(tmp_path)
    orchestrator = ScanOrchestrator(settings, repository, storage)
    if ai is not None:
        orchestrator.ai = ai
    return orchestrator, storage, run_id, run_dir, code_index


def _l2_candidate(**overrides: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "candidate_id": "cand_route_0001",
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "evidence_level": "L2",
        "component": "activity",
        "component_name": "com.example.A",
        "severity_hint": "high",
        "confidence_tier": "medium",
        "locations": [{"artifact": "code", "path": "com/example/A.java", "line": 3}],
        "sources": [{"kind": "source_expression", "status": "fact",
                     "path": "com/example/A.java", "line": 3, "text": "entry(input)"}],
        "sinks": [{"kind": "sink_call", "status": "fact",
                   "path": "com/example/C.java", "line": 4, "text": "write(value)"}],
        "blocking_gaps": [],
        "guard_status": "unknown",
        "authorization_status": "unknown",
        "reachability_status": "reachable",
        "deterministic_chain_verified": False,
    }
    candidate.update(overrides)
    return candidate


def _slice_document() -> dict[str, Any]:
    return {"contexts": [], "request_history": []}


class FakeVerifyEntryAI:
    """verify_entry 协议替身（可控 verdicts/证据/失败形态；捕获输入）。"""

    def __init__(self, *, verdicts: list[dict] | None = None, fail: str | None = None,
                 circuit: bool = False, raise_exc: bool = False):
        self._verdicts = verdicts or [
            {"index": index, "conclusion": "confirmed", "reasoning": "取证判定",
             "evidence": [{"path": "com/example/B.java", "line": 4}]}
            for index in range(4)
        ]
        self._fail = fail
        self._circuit = circuit
        self._raise = raise_exc
        self.inputs: list[Any] = []
        self.calls = 0

    async def verify_entry(self, model_input: Any) -> dict[str, Any]:
        self.calls += 1
        self.inputs.append(model_input)
        if self._raise:
            raise RuntimeError("verify 意外异常")
        if self._fail:
            return {"status": self._fail, "circuit_breaking": self._circuit, "metadata": {}}
        return {
            "status": "completed",
            "analysis": {
                "summary": "全部命题判定完成",
                "verdict": "supports_candidate",
                "confidence_tier": "high",
                "flaw_holds": True,
                "exploitability": {
                    "entry_reachable": True, "propagation_proven": True,
                    "sink_effective": True, "guard_bypassed": False,
                    "authorization_absent": True, "exfiltration_channel": "unverified",
                },
                "claims_verdicts": self._verdicts,
                "evidence_refs": [{"path": "com/example/B.java", "line": 4}],
                "read_requests": [],
                "loop": {"done": True, "reason": "全部判定"},
                "analysis_complete": True,
            },
            "metadata": {"prompt_version": "1.0.0", "model": "test-model"},
        }


def _call_verify(
    orchestrator: ScanOrchestrator, run_dir: Path, code_index: dict[str, Any],
    candidate: dict[str, Any], *, explorer_map: dict | None = None,
) -> dict[str, Any] | None:
    return asyncio.run(orchestrator._verify_candidate(
        candidate, _slice_document(), run_dir, code_index,
        explorer_map if explorer_map is not None else {},
        trace_store=None, candidate_index=0, input_key="k",
    ))


# ---------------------------------------------------------------------------
# A-7：分流判定
# ---------------------------------------------------------------------------


def test_verify_path_for_matrix(tmp_path: Path) -> None:
    orchestrator, _, _, _, _ = _orchestrator(tmp_path, verify_enabled=True)
    l2 = _l2_candidate()
    l1 = _l2_candidate(evidence_level="L1")
    assert orchestrator._verify_path_for(l2) is True
    assert orchestrator._verify_path_for(l1) is False

    disabled, _, _, _, _ = _orchestrator(tmp_path, verify_enabled=False, run_suffix=1)
    assert disabled._verify_path_for(l2) is False


# ---------------------------------------------------------------------------
# A-8/A-9：核验成功路径 + 探索候选关联
# ---------------------------------------------------------------------------


def test_verify_candidate_success(tmp_path: Path) -> None:
    fake = FakeVerifyEntryAI()
    orchestrator, _, _, run_dir, code_index = _orchestrator(tmp_path, ai=fake)
    candidate = _l2_candidate()

    result = _call_verify(orchestrator, run_dir, code_index, candidate)

    assert result is not None and result["status"] == "completed"
    assert result["stop_reason"] == "verify_completed"
    assert candidate["verify_used"] is True
    assert candidate.get("verify_fallback_reason") is None
    # 适配写入（R-1 的 ai_evidence_contexts 注入 + _apply_ai_analysis 字段）
    assert candidate["ai_analysis"]["analysis_track"] == "verify"
    assert candidate["ai_analysis"]["verdict"] == "supports_candidate"
    assert candidate["ai_evidence_contexts"] == evidence_contexts_for(
        candidate["ai_analysis"])
    assert candidate["confidence_tier"] == "high"
    # A-15：第三本账分账 + run 级共享池
    assert orchestrator._verify_requests_used == fake.calls == 1
    assert orchestrator._ai_requests_used == 1


def test_verify_candidate_explorer_chain_linked(tmp_path: Path) -> None:
    fake = FakeVerifyEntryAI()
    orchestrator, _, _, run_dir, code_index = _orchestrator(tmp_path, ai=fake)
    a_method = _method_id(code_index, "com.example.A", "entry")
    b_method = _method_id(code_index, "com.example.B", "run")
    explorer_entry = {
        "candidate_id": "expl_" + "d" * 20,
        "chain_proposal": {
            "source": "A.entry(input)", "sink": "C.write(value)",
            "hops": [{"from_method_id": a_method, "to_method_id": b_method,
                      "call_site_line": 4, "resolved_via": "direct_call"}],
            "confidence": "high", "hypothesis": "likely",
            "impact_proposal": "假设层", "reasoning": "推理",
            "evidence_refs": [{"path": "com/example/A.java", "line": 3}],
        },
    }
    candidate = _l2_candidate(explorer_candidate_id="expl_" + "d" * 20)

    result = _call_verify(orchestrator, run_dir, code_index, candidate,
                          explorer_map={explorer_entry["candidate_id"]: explorer_entry})

    assert result is not None and result["status"] == "completed"
    # 原始链投影进核验输入（hops 供 chain_facts；假设层剥离）
    chain_facts = fake.inputs[0].chain_facts
    assert chain_facts is not None and len(chain_facts.hops) == 1
    serialized = json.dumps(fake.inputs[0].model_dump(mode="json"), ensure_ascii=False)
    assert "hypothesis" not in serialized and "impact_proposal" not in serialized


def test_load_explorer_candidates_tolerant(tmp_path: Path) -> None:
    """N-1/N-2：candidates.json 缺失/损坏 → 空映射容错。"""

    orchestrator, _, _, run_dir, _ = _orchestrator(tmp_path)
    assert orchestrator._load_explorer_candidates(run_dir) == {}

    verify_dir = run_dir / "explorer"
    verify_dir.mkdir()
    (verify_dir / "candidates.json").write_text("not-json{", "utf-8")
    assert orchestrator._load_explorer_candidates(run_dir) == {}


# ---------------------------------------------------------------------------
# A-10~A-13：回退矩阵
# ---------------------------------------------------------------------------


def test_verify_failure_falls_back(tmp_path: Path) -> None:
    """A-10：verify 失败（非熔断）→ None（回退信号）+ fallback 标记。"""

    fake = FakeVerifyEntryAI(fail="error")
    orchestrator, _, _, run_dir, code_index = _orchestrator(tmp_path, ai=fake)
    candidate = _l2_candidate()

    result = _call_verify(orchestrator, run_dir, code_index, candidate)

    assert result is None
    assert candidate["verify_fallback_reason"] == "verify_error"
    assert "verify_used" not in candidate


def test_verify_budget_exhausted_falls_back(tmp_path: Path) -> None:
    """A-11：run 级预算耗尽 → verify skipped → 回退。"""

    fake = FakeVerifyEntryAI()
    orchestrator, _, _, run_dir, code_index = _orchestrator(tmp_path, ai=fake)
    orchestrator._ai_requests_used = orchestrator.settings.context_budget.max_requests_per_run
    candidate = _l2_candidate()

    result = _call_verify(orchestrator, run_dir, code_index, candidate)

    assert result is None
    assert candidate["verify_fallback_reason"] == "verify_short_circuit"
    assert fake.calls == 0  # 预算检查先于调用


def test_verify_failure_no_fallback_terminal(tmp_path: Path) -> None:
    """A-12：fallback=false → 不回退（失败终态 + 终态字段对齐——评审 R-11）。"""

    fake = FakeVerifyEntryAI(fail="error")
    orchestrator, _, _, run_dir, code_index = _orchestrator(
        tmp_path, fallback=False, ai=fake)
    candidate = _l2_candidate()

    result = _call_verify(orchestrator, run_dir, code_index, candidate)

    assert result is not None and result["status"] == "failed"
    assert candidate["analysis_status"] == "ai_failed"
    assert candidate["ai_stop_reason"].startswith("verify agent")
    # 失败轮审计进 trace（评审 R-11：终态字段对齐原路径）
    trace = candidate["ai_analysis_trace"]
    assert len(trace) == 1
    assert trace[0]["result"]["metadata"]["verify_round_status"] == "error"
    gaps = candidate["ai_blocking_gaps"]
    assert gaps and gaps[0]["code"] == "AI_ANALYSIS_FAILED"


def test_verify_index_unavailable_falls_back(tmp_path: Path) -> None:
    """A-13：code_index 不可用 → 回退（主链不阻塞）。"""

    fake = FakeVerifyEntryAI()
    orchestrator, _, _, run_dir, _ = _orchestrator(tmp_path, ai=fake)
    candidate = _l2_candidate()

    result = _call_verify(orchestrator, run_dir, None, candidate)

    assert result is None
    assert candidate["verify_fallback_reason"] == "verify_index_unavailable"


def test_verify_unexpected_exception_falls_back(tmp_path: Path) -> None:
    """N-3：verify() 意外异常 → 捕获回退（评审 R-5）。"""

    fake = FakeVerifyEntryAI(raise_exc=True)
    orchestrator, _, _, run_dir, code_index = _orchestrator(tmp_path, ai=fake)
    candidate = _l2_candidate()

    result = _call_verify(orchestrator, run_dir, code_index, candidate)

    assert result is None
    assert candidate["verify_fallback_reason"] == "verify_error"


def test_verify_no_claims_falls_back(tmp_path: Path) -> None:
    """N-5：无命题候选（claims 空）→ verify skipped → 回退单轮 L2。"""

    fake = FakeVerifyEntryAI()
    orchestrator, _, _, run_dir, code_index = _orchestrator(tmp_path, ai=fake)
    candidate = _l2_candidate(sources=[], sinks=[], locations=[],
                              component_name=None)

    result = _call_verify(orchestrator, run_dir, code_index, candidate)

    assert result is None
    assert candidate["verify_fallback_reason"] == "verify_no_claims"


# ---------------------------------------------------------------------------
# A-14：checkpoint 隔离
# ---------------------------------------------------------------------------


def test_checkpoint_identity_isolation() -> None:
    from app.analysis.ai_trace import candidate_input_key

    candidate = _l2_candidate()
    slice_document = _slice_document()
    base_identity = {"analyzer": "Fake", "prompt": "l2"}
    verify_identity = {**base_identity, "verify_agent": "verify/1.0.0"}

    key_l2 = candidate_input_key(candidate, slice_document, base_identity)
    key_verify = candidate_input_key(candidate, slice_document, verify_identity)
    assert key_l2 != key_verify


# ---------------------------------------------------------------------------
# A-16/A-17：API 集成——主链不阻塞 + 默认关闭
# ---------------------------------------------------------------------------


def _apk_bytes() -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", "<manifest/>")
        archive.writestr("classes.dex", b"dex\n035\x00")
    return buffer.getvalue()


def test_verify_enabled_run_completes_without_ai(tmp_path: Path) -> None:
    """A-16（M2 验收 4.3-6.4）：verify.enabled + AI 不可用 → 主链不阻塞。"""

    settings = Settings(
        database_path=tmp_path / "tracer.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "data"),
        source_analysis=SourceAnalysisSettings(enabled=False),
        verify=VerifySettings(enabled=True),
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/runs",
            files={"file": ("sample.apk", _apk_bytes(), "application/vnd.android.package-archive")},
            data={"authorized": "true", "source_analysis_enabled": "false"},
        )
        assert response.status_code == 202
        run_id = response.json()["id"]
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "completed"


def test_verify_disabled_by_default(tmp_path: Path) -> None:
    """A-17：默认配置 verify 关闭——分流判定短路（零运行时影响）。"""

    settings = Settings(
        database_path=tmp_path / "tracer.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "data"),
        source_analysis=SourceAnalysisSettings(enabled=False),
    )
    assert settings.verify.enabled is False
    orchestrator, _, _, _, _ = _orchestrator(tmp_path, verify_enabled=False)
    assert orchestrator._verify_path_for(_l2_candidate()) is False
