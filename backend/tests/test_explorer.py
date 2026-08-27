"""探索 Agent 驱动循环测试（T2.5b）。

设计：docs/analysis/explorer-track/2026-08-22-t2-5b-implementation-plan.md（含评审
R-1~R-10 修订）。FakeAnalyzer 按队列逐轮弹出 Observation；真实 index
（复用 test_call_tree 调用链源码）承载 read_requests 本地检索。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import jsonschema
from fastapi.testclient import TestClient

from app.analysis.call_tree import CallTreeService
from app.analysis.explorer import ExplorerOrchestrator
from app.analysis.index_store import SQLiteCodeIndexReader
from app.analysis.indexer import build_code_index
from app.analysis.orchestrator import ScanOrchestrator
from app.config import (
    ApiSurfaceSettings,
    ContextBudgetSettings,
    ExplorerSettings,
    Settings,
    SourceAnalysisSettings,
    StorageSettings,
)
from app.main import create_app
from app.runs.storage import RunStorage
from app.shared.repository import SQLiteRepository

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"

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


def _entry(call_tree: CallTreeService) -> dict:
    return {
        "entry_id": "act_com_example_A_entry",
        "kind": "activity",
        "component_name": "com.example.A",
        "source": "manifest",
        "entry_method": "entry(String)->void",
        "method_id": _method_id(call_tree, "com.example.A", "entry"),
    }


class FakeAnalyzer:
    """可控 Observation 序列的 AI 协议替身（捕获每轮输入）。"""

    def __init__(self, rounds: list[dict[str, Any]]):
        self._rounds = list(rounds)
        self.inputs: list[Any] = []

    async def __call__(self, model_input: Any) -> dict[str, Any]:
        self.inputs.append(model_input)
        if not self._rounds:
            # 队列尽：默认给链终止（满足 _done_requires_chain 校验器）
            return {"status": "completed", "analysis": self._observation(loop_done=True, proposals=[_proposal()]), "metadata": {}}
        spec = self._rounds.pop(0)
        if spec.get("fail"):
            return {"status": spec["fail"], "circuit_breaking": spec.get("circuit", False), "metadata": {}}
        return {
            "status": "completed",
            "analysis": self._observation(
                loop_done=spec["done"],
                proposals=spec.get("proposals", []),
                requests=spec.get("requests", []),
            ),
            "metadata": {"prompt_version": "1.0.0", "model": "test-model"},
        }

    @staticmethod
    def _observation(loop_done: bool, proposals: list, requests: list) -> dict:
        return {
            "read_requests": [
                {"operation": item["operation"], "target": item["target"], "reason": "取证"}
                for item in requests
            ],
            "chain_proposals": proposals,
            "component_summary": {
                "component": "com.example.A", "kind": "activity",
                "exported": True, "summary": "入口 Activity 分发处理",
            },
            "loop": {"done": loop_done, "reason": "测试"},
        }


def _proposal() -> dict:
    return {
        "source": "A.entry(input)",
        "sink": "C.write(value)",
        "hops": [{
            "from_method_id": "sources/com/example/A.java#A.entry:3",
            "to_method_id": "sources/com/example/B.java#B.run:3",
            "call_site_line": 4, "resolved_via": "direct_call",
        }],
        "confidence": "medium", "hypothesis": "possible",
        "impact_proposal": "外部输入流向写入", "reasoning": "调用链成立",
    }


def _orchestrator(fake: FakeAnalyzer, tmp_path: Path, **settings) -> ExplorerOrchestrator:
    call_tree = _service(tmp_path)
    return ExplorerOrchestrator(fake, call_tree, ExplorerSettings(**settings), tmp_path)


# ---------------------------------------------------------------------------
# A-1/A-2：循环终止语义
# ---------------------------------------------------------------------------


def test_explore_entry_loop_done(tmp_path: Path) -> None:
    fake = FakeAnalyzer([
        {"done": False, "requests": [{"operation": "get_method_body", "target": None}]},
        {"done": True, "proposals": [_proposal()]},
    ])
    call_tree = _service(tmp_path)
    entry = _entry(call_tree)
    fake._rounds[0]["requests"][0]["target"] = entry["method_id"]  # 首轮请求入口方法体
    orchestrator = ExplorerOrchestrator(fake, call_tree, ExplorerSettings(), tmp_path)

    candidates = asyncio_run(orchestrator.explore_all([entry]))
    assert len(candidates) == 1
    # A-5：候选 schema 校验（真实键名断言——metadata 透传）
    assert candidates[0]["prompt_version"] == "explorer/1.0.0"
    assert candidates[0]["model"] == "test-model"
    assert candidates[0]["source"] == "explorer_agent"
    assert candidates[0]["validation"] is None
    assert candidates[0]["candidate_id"].startswith("expl_")
    schema = json.loads((SCHEMAS_DIR / "explorer_candidate.schema.json").read_text("utf-8"))
    jsonschema.validate(candidates[0], schema)
    # 落盘
    observations = json.loads((tmp_path / "explorer" / "observations.json").read_text("utf-8"))
    assert observations["entries"][0]["terminated_by"] == "loop_done"
    assert len(observations["entries"][0]["rounds"]) == 2
    # 评审 R-5：轮输入哈希落盘
    assert all("model_input_hash" in round_record for round_record in observations["entries"][0]["rounds"])
    saved = json.loads((tmp_path / "explorer" / "candidates.json").read_text("utf-8"))
    assert len(saved) == 1


def test_explore_entry_budget_termination(tmp_path: Path) -> None:
    fake = FakeAnalyzer([
        {"done": False, "requests": []},
        {"done": False, "requests": [], "proposals": [_proposal()]},
    ])
    call_tree = _service(tmp_path)
    orchestrator = ExplorerOrchestrator(fake, call_tree, ExplorerSettings(max_rounds_per_entry=2), tmp_path)

    candidates = asyncio_run(orchestrator.explore_all([_entry(call_tree)]))
    # 预算终止：终轮部分链保留（方案 §2.4：产出部分链+缺口而非失败）
    assert len(candidates) == 1
    observations = json.loads((tmp_path / "explorer" / "observations.json").read_text("utf-8"))
    assert observations["entries"][0]["terminated_by"] == "budget"


# ---------------------------------------------------------------------------
# A-3/A-4：读码执行与预算截断
# ---------------------------------------------------------------------------


def test_read_requests_execution(tmp_path: Path) -> None:
    call_tree = _service(tmp_path)
    entry = _entry(call_tree)
    fake = FakeAnalyzer([
        {"done": False, "requests": [{"operation": "get_method_body", "target": entry["method_id"]}]},
        {"done": True, "proposals": [_proposal()]},
    ])
    orchestrator = ExplorerOrchestrator(fake, call_tree, ExplorerSettings(), tmp_path)
    asyncio_run(orchestrator.explore_all([entry]))

    # 首轮输入 code_context 为空；次轮含取回的方法体（真实执行）
    assert fake.inputs[0].code_context is None
    assert fake.inputs[1].code_context is not None
    assert "helper.run" in fake.inputs[1].code_context
    assert orchestrator.read_requests_used == 1


def test_requests_budget_truncation(tmp_path: Path) -> None:
    call_tree = _service(tmp_path)
    entry = _entry(call_tree)
    # F5 后请求增量去重——预算截断用 8 个互异请求构造（同请求会被去重跳过）
    requests = [{"operation": "search_symbol", "target": f"run_{index}"} for index in range(8)]
    fake = FakeAnalyzer([
        {"done": False, "requests": requests},
        {"done": True, "proposals": [_proposal()]},
    ])
    orchestrator = ExplorerOrchestrator(
        fake, call_tree, ExplorerSettings(max_requests_per_entry=3), tmp_path
    )
    asyncio_run(orchestrator.explore_all([entry]))
    # 8 请求限 3 执行（剩余预算截断）
    observations = json.loads((tmp_path / "explorer" / "observations.json").read_text("utf-8"))
    assert len(observations["entries"][0]["rounds"][0]["requests_executed"]) == 3
    assert orchestrator.read_requests_used == 3


# ---------------------------------------------------------------------------
# A-6/A-7/A-8：上限/失败短路/无方法入口
# ---------------------------------------------------------------------------


def test_explore_all_candidate_cap(tmp_path: Path) -> None:
    call_tree = _service(tmp_path)
    entry = _entry(call_tree)
    fake = FakeAnalyzer([{"done": True, "proposals": [_proposal()]}])
    orchestrator = ExplorerOrchestrator(
        fake, call_tree, ExplorerSettings(max_candidates_per_run=1), tmp_path
    )
    second = {**entry, "entry_id": "act_com_example_A_entry__2"}
    candidates = asyncio_run(orchestrator.explore_all([entry, second]))
    assert len(candidates) == 1  # 上限生效（第二入口未跑）
    observations = json.loads((tmp_path / "explorer" / "observations.json").read_text("utf-8"))
    assert len(observations["entries"]) == 1


def test_analyzer_failure_short_circuit(tmp_path: Path) -> None:
    call_tree = _service(tmp_path)
    entry = _entry(call_tree)
    fake = FakeAnalyzer([
        {"fail": "circuit_open", "circuit": True},
    ])
    orchestrator = ExplorerOrchestrator(fake, call_tree, ExplorerSettings(), tmp_path)
    candidates = asyncio_run(orchestrator.explore_all([entry, {**entry, "entry_id": "x2"}]))
    assert candidates == []
    observations = json.loads((tmp_path / "explorer" / "observations.json").read_text("utf-8"))
    assert observations["entries"][0]["terminated_by"] == "short_circuit"
    # 评审 R-4：熔断短路剩余入口（不重试）
    assert observations["entries"][1]["terminated_by"] == "short_circuited"


def test_explore_entry_no_method(tmp_path: Path) -> None:
    call_tree = _service(tmp_path)
    fake = FakeAnalyzer([])
    orchestrator = ExplorerOrchestrator(fake, call_tree, ExplorerSettings(), tmp_path)
    candidates = asyncio_run(orchestrator.explore_all([
        {"entry_id": "webview_x", "kind": "webview_bridge", "component_name": "x",
         "source": "rule_artifact:webview_js_bridges", "method_id": None},
    ]))
    assert candidates == []
    observations = json.loads((tmp_path / "explorer" / "observations.json").read_text("utf-8"))
    assert observations["entries"][0]["terminated_by"] == "no_method"
    assert fake.inputs == []  # 零 AI 调用


# ---------------------------------------------------------------------------
# A-9：输出模型注册
# ---------------------------------------------------------------------------


def test_ai_output_model_registered() -> None:
    from app.analysis.ai_models import ExplorerObservation, get_ai_output_model

    assert get_ai_output_model("ExplorerObservation", "1") is ExplorerObservation


# ---------------------------------------------------------------------------
# A-10：集成（explorer.enabled + AI 不可用）
# ---------------------------------------------------------------------------


def _apk_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", "<manifest/>")
        archive.writestr("classes.dex", b"dex\n035\x00")
    return buffer.getvalue()


def test_orchestrator_explorer_stage(tmp_path: Path) -> None:
    """explorer.enabled=true + 无 AI key（默认配置）→ run completed + 阶段不挂。"""
    settings = Settings(
        database_path=tmp_path / "tracer.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "data"),
        source_analysis=SourceAnalysisSettings(enabled=False),
        explorer=ExplorerSettings(enabled=True),
        api_surface=ApiSurfaceSettings(enabled=True),
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/runs",
            files={"file": ("sample.apk", _apk_bytes(), "application/vnd.android.package-archive")},
            data={"authorized": "true", "source_analysis_enabled": "false"},
        )
        run_id = response.json()["id"]
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "completed"
        run_manifest = client.app.state.storage.read_manifest(run_id)
        stages = [s for s in run_manifest.get("stages", []) if s["name"] == "explorer"]
        assert stages  # 阶段已执行（AI 不可用下零候选也是有效执行）
        # A-15（T2.7）：探索阶段位于 candidate_funnel 之前（方案 §2.5 合流图）
        stage_names = [s["name"] for s in run_manifest.get("stages", [])]
        assert "candidate_funnel" in stage_names
        assert stage_names.index("explorer") < stage_names.index("candidate_funnel")
        artifacts = [a for a in run_manifest.get("artifacts", []) if a["type"] == "explorer_candidates"]
        assert artifacts and artifacts[0]["candidate_count"] == 0


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# N-3：observations 预存在追加
# ---------------------------------------------------------------------------


def test_observations_append_on_rerun(tmp_path: Path) -> None:
    call_tree = _service(tmp_path)
    entry = _entry(call_tree)
    fake = FakeAnalyzer([{"done": True, "proposals": [_proposal()]}])
    orchestrator = ExplorerOrchestrator(fake, call_tree, ExplorerSettings(), tmp_path)
    asyncio_run(orchestrator.explore_all([entry]))

    fake2 = FakeAnalyzer([{"done": True, "proposals": []}])
    orchestrator2 = ExplorerOrchestrator(fake2, call_tree, ExplorerSettings(), tmp_path)
    asyncio_run(orchestrator2.explore_all([entry]))

    observations = json.loads((tmp_path / "explorer" / "observations.json").read_text("utf-8"))
    assert len(observations["entries"]) == 2  # 追加而非覆盖


# ---------------------------------------------------------------------------
# T2.7：归一化并入主链 + run 级预算共享（A-16/A-18，实例级——评审 R-7：
# 复用 FakeAnalyzer 直驱模式，不走 API 级 mock）
# ---------------------------------------------------------------------------

_ACTIVITY_SOURCES = {
    "com/example/A.java": """package com.example;
public class A extends android.app.Activity {
  protected void onCreate(android.os.Bundle savedInstanceState) {
    B helper = new B();
    helper.run("input");
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


class FakeExploreAI:
    """explore_entry 协议替身：按轮次弹出 Observation（done/proposals 可控）。

    T2.8：兼作 deep_dive_entry 替身（dive_rounds 队列——集成测试用）。
    """

    def __init__(
        self,
        rounds: list[dict[str, Any]] | None = None,
        proposal: dict[str, Any] | None = None,
        dive_rounds: list[dict[str, Any]] | None = None,
    ):
        self._rounds = list(rounds or [{"done": True, "proposals": [proposal] if proposal else []}])
        self._dive_rounds = list(dive_rounds or [{"complete": True, "facts": []}])
        self.calls = 0
        self.dive_calls = 0
        self.inputs: list[Any] = []  # F5：轮输入捕获（known_findings 注入断言）

    async def explore_entry(self, model_input: Any) -> dict[str, Any]:
        self.calls += 1
        self.inputs.append(model_input)
        spec = self._rounds.pop(0) if self._rounds else {
            # F5 干净出口兜底：队列尽默认 done=True + 空链须带"无敏感"结论
            # （校验器拒绝无结论空链——多入口测试的后续入口默认正常终止）
            "done": True, "proposals": [], "reason": "确认无敏感操作",
        }
        return {
            "status": "completed",
            "analysis": {
                "read_requests": [],
                "chain_proposals": spec.get("proposals", []),
                "component_summary": {
                    "component": "com.example.A", "kind": "activity",
                    "exported": True, "summary": "入口 Activity 分发处理",
                },
                "loop": {"done": spec.get("done", True), "reason": spec.get("reason", "测试")},
            },
            "metadata": {"prompt_version": "1.0.0", "model": "test-model"},
        }

    async def deep_dive_entry(self, model_input: Any) -> dict[str, Any]:
        self.dive_calls += 1
        spec = self._dive_rounds.pop(0) if self._dive_rounds else {"complete": True, "facts": []}
        if spec.get("fail"):
            return {"status": spec["fail"], "circuit_breaking": spec.get("circuit", False),
                    "metadata": {}}
        return {
            "status": "completed",
            "analysis": {
                "summary": spec.get("summary", "深挖完成"),
                "resolved_facts": spec.get("facts", []),
                "evidence_refs": spec.get("evidence", []),
                "remaining_gaps": spec.get("gaps", []),
                "analysis_complete": spec.get("complete", True),
            },
            "metadata": {"prompt_version": "1.0.0", "model": "test-model"},
        }


def _instance_orchestrator(
    tmp_path: Path, max_requests_per_run: int = 140
) -> tuple[ScanOrchestrator, RunStorage, str, Path, dict[str, Any]]:
    settings = Settings(
        database_path=tmp_path / "tracer.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "data"),
        source_analysis=SourceAnalysisSettings(enabled=False),
        # T2.9 评审 R-8：集成测试链尾 C.write 与种子 write 条目碰撞
        # （receiver com.example.C 失配 → custom 压档）——显式禁用 taxonomy
        # 隔离既有断言；taxonomy 接线行为由 test_sink_taxonomy.py 专用覆盖
        explorer=ExplorerSettings(
            enabled=True,
            custom_sink_taxonomy_path=tmp_path / "absent-taxonomy.yaml",
        ),
        context_budget=ContextBudgetSettings(max_requests_per_run=max_requests_per_run),
    )
    repository = SQLiteRepository(settings.resolved_database_path())
    repository.initialize()
    storage = RunStorage(settings.resolved_data_root(), settings.storage)
    run_id = "20260822T000000Z_aaaaaaaaaaaa_bbbbbbbb"
    run_dir = storage.runs_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text("{}", "utf-8")

    source_root = tmp_path / "sources"
    for relative, content in _ACTIVITY_SOURCES.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    descriptor = build_code_index(source_root, tmp_path / "index" / "code-index.json")

    # 入口表（T2.2 产物形状）：activity 组件 lifecycle 方法解析
    (run_dir / "api-surface").mkdir(exist_ok=True)
    (run_dir / "api-surface" / "api_entry_table.json").write_text(json.dumps({
        "api_entries": [{
            "entry_id": "act_com_example_A_onCreate",
            "kind": "activity",
            "component_name": "com.example.A",
            "source": "manifest",
            "entry_method": "onCreate",
        }],
    }), "utf-8")
    orchestrator = ScanOrchestrator(settings, repository, storage)
    return orchestrator, storage, run_id, run_dir, descriptor


def _real_proposal(descriptor: dict[str, Any]) -> dict[str, Any]:
    """从索引构造可回查（validated）链提案：A.onCreate → B.run → C.write。"""

    reader = SQLiteCodeIndexReader(descriptor)
    try:
        def method_id(qualified_class: str, name: str) -> str:
            row = reader.db.execute(
                "SELECT id FROM methods WHERE qualified_class = ? AND name = ?",
                (qualified_class, name),
            ).fetchone()
            assert row is not None
            return str(row["id"])

        def call_site(from_id: str) -> tuple[str, int]:
            row = reader.db.execute(
                "SELECT resolved_target_id, start_line FROM call_sites "
                "WHERE method_id = ? AND resolve_status = 'resolved' LIMIT 1",
                (from_id,),
            ).fetchone()
            assert row is not None
            return str(row["resolved_target_id"]), int(row["start_line"])

        on_create = method_id("com.example.A", "onCreate")
        b_run, first_line = call_site(on_create)
        c_write, second_line = call_site(b_run)
        return {
            "source": "onCreate(savedInstanceState)",
            "sink": "C.write(value)",
            "hops": [
                {"from_method_id": on_create, "to_method_id": b_run,
                 "call_site_line": first_line, "resolved_via": "direct_call"},
                {"from_method_id": b_run, "to_method_id": c_write,
                 "call_site_line": second_line, "resolved_via": "direct_call"},
            ],
            "confidence": "high",
            "hypothesis": "likely",
            "impact_proposal": "外部输入流向敏感写入操作",
            "reasoning": "调用链逐跳可见",
            "evidence_refs": [{"path": "com/example/A.java", "line": first_line}],
        }
    finally:
        reader.close()


def test_explorer_stage_normalizes_validated_into_main_candidates(tmp_path: Path) -> None:
    """A-16：_run_explorer_stage 返回归一化候选；全三档原始形状落盘。"""

    orchestrator, storage, run_id, run_dir, descriptor = _instance_orchestrator(tmp_path)
    orchestrator.ai = FakeExploreAI(proposal=_real_proposal(descriptor))

    normalized = asyncio_run(
        orchestrator._run_explorer_stage(
            run_id, run_dir, {"debuggable": False, "target_sdk": 36}, descriptor
        )
    )

    assert len(normalized) == 1
    candidate = normalized[0]
    assert candidate["rule_id"] == "EXPLORER_AGENT"
    assert candidate["candidate_source"] == "explorer"
    assert candidate["evidence_level"] == "L2"
    assert candidate["explorer_validation_status"] == "validated"
    assert "#" in candidate["sinks"][0]["method_id"]  # indexer 方法 ID 形状
    # explorer/candidates.json 保留原始 ExplorerCandidate 形状（人工队列数据源）
    raw = json.loads((run_dir / "explorer" / "candidates.json").read_text("utf-8"))
    assert isinstance(raw, list) and len(raw) == 1
    assert raw[0]["candidate_id"].startswith("expl_")
    assert raw[0]["chain_proposal"]["hops"]
    # stage summary：三档校验 + 归一化计数（T2.6+T2.7）
    manifest = storage.read_manifest(run_id)
    stage = next(s for s in manifest["stages"] if s["name"] == "explorer")
    summary = stage["summary"]
    assert summary["validation_counts"]["validated"] == 1
    assert summary["normalization_counts"]["normalized"] == 1
    assert summary["normalization_counts"]["partial_kept"] == 0
    assert summary["normalization_counts"]["unverified_kept"] == 0
    assert summary["ai_requests_used"] == 1
    # F4 核验 V-1：summary 接线断言（单入口无截断——全覆盖态）
    assert summary["entries_explored"] == 1
    assert summary["entries_unexplored"] == 0
    # T2.10：探索产物注册（explorer_candidates 既有 + explorer_observations 补注册）
    artifact_types = {a["type"] for a in manifest.get("artifacts", [])}
    assert "explorer_candidates" in artifact_types
    assert "explorer_observations" in artifact_types


def _real_partial_proposal(descriptor: dict[str, Any]) -> dict[str, Any]:
    """partial 链提案：第二跳行号 99（回查必失败）→ partially_validated。"""

    proposal = _real_proposal(descriptor)
    proposal["hops"][1]["call_site_line"] = 99
    return proposal


def test_explorer_stage_deep_dive_integration(tmp_path: Path) -> None:
    """A-13~A-15：阶段集成——深挖计数/链不变/归一化隔离/预算分账。"""

    orchestrator, storage, run_id, run_dir, descriptor = _instance_orchestrator(tmp_path)
    orchestrator.ai = FakeExploreAI(
        proposal=_real_partial_proposal(descriptor),
        dive_rounds=[{
            "complete": True,
            "facts": [{"claim_index": 0, "conclusion": "confirmed",
                       "reasoning": "调用边存在",
                       "evidence": [{"path": "com/example/B.java", "line": 4}]}],
            "evidence": [{"path": "com/example/B.java", "line": 4}],
        }],
    )

    normalized = asyncio_run(
        orchestrator._run_explorer_stage(
            run_id, run_dir, {"debuggable": False, "target_sdk": 36}, descriptor
        )
    )

    # A-14（D1）：深挖 completed 也不升级不归一化——partial 不进主链
    assert normalized == []
    manifest = storage.read_manifest(run_id)
    stage = next(s for s in manifest["stages"] if s["name"] == "explorer")
    summary = stage["summary"]
    # A-13：validation_counts 不因深挖变化（三档是跳回查确定性结论）
    assert summary["validation_counts"] == {
        "validated": 0, "partially_validated": 1, "unverified": 0,
    }
    assert summary["deep_dive_counts"]["partial_total"] == 1
    assert summary["deep_dive_counts"]["completed"] == 1
    assert summary["deep_dive_requests_used"] == 1
    # A-13：candidates.json 原始形状含 deep_dive；validation 保持 partial
    raw = json.loads((run_dir / "explorer" / "candidates.json").read_text("utf-8"))
    assert raw[0]["validation"]["status"] == "partially_validated"
    assert raw[0]["deep_dive"]["status"] == "completed"
    assert raw[0]["deep_dive"]["resolved_facts"][0]["conclusion"] == "confirmed"
    assert raw[0]["chain_proposal"]["hops"][1]["call_site_line"] == 99  # 链不可变
    # A-15：run 级共享池——探索 1 + 深挖 1
    assert orchestrator._ai_requests_used == 2


def test_explorer_stage_budget_shared_with_ai_stage(tmp_path: Path) -> None:
    """A-18（评审 R-1）：探索与规则 AI 共享同一 run 级预算池——AI 阶段早退
    summary 的 requests_used 为 run 累计口径（探索消耗不因阶段切换归零）。"""

    orchestrator, storage, run_id, run_dir, descriptor = _instance_orchestrator(tmp_path)
    orchestrator.ai = FakeExploreAI(proposal=_real_proposal(descriptor))

    asyncio_run(
        orchestrator._run_explorer_stage(
            run_id, run_dir, {"debuggable": False, "target_sdk": 36}, descriptor
        )
    )
    assert orchestrator._ai_requests_used == 1

    # AI 阶段（无候选早退路径）：requests_used 继承探索消耗（重置已删除）
    asyncio_run(
        orchestrator._run_ai_stage(run_id, [], [], {}, None, False, run_dir, ai_enabled=True)
    )
    manifest = storage.read_manifest(run_id)
    stage = next(s for s in manifest["stages"] if s["name"] == "ai_analysis")
    summary = stage["summary"]
    assert summary["requests_used"] == 1
    assert summary["explorer_requests_used"] == 1
    assert summary["ai_stage_requests_used"] == 0


def test_explorer_stage_budget_cap_rejects_beyond_limit(tmp_path: Path) -> None:
    """A-18 补充：max_requests_per_run 帽内探索优先消耗——预算耗尽后 AI 调用被拒。"""

    orchestrator, storage, run_id, run_dir, descriptor = _instance_orchestrator(
        tmp_path, max_requests_per_run=1
    )
    fake = FakeExploreAI(rounds=[{"done": False, "proposals": []}])
    orchestrator.ai = fake

    normalized = asyncio_run(
        orchestrator._run_explorer_stage(
            run_id, run_dir, {"debuggable": False, "target_sdk": 36}, descriptor
        )
    )
    # 第一轮消耗唯一预算（done=False 续轮）→ 第二轮被 budgeted_ai_call 拒
    # （skipped/circuit_breaking）→ 零候选零归一化，AI 实际只被调用 1 次
    assert normalized == []
    assert orchestrator._ai_requests_used == 1
    assert fake.calls == 1
    manifest = storage.read_manifest(run_id)
    stage = next(s for s in manifest["stages"] if s["name"] == "explorer")
    assert stage["summary"]["candidate_count"] == 0


# ---------------------------------------------------------------------------
# T2.8：explorer_deep_dive（partial 候选深挖——补齐事实，禁止改写链）
# ---------------------------------------------------------------------------


def _index_reader(tmp_path: Path) -> SQLiteCodeIndexReader:
    source_root = tmp_path / "sources"
    for relative, content in _CHAIN_SOURCE.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    descriptor = build_code_index(source_root, tmp_path / "index" / "code-index.json")
    return SQLiteCodeIndexReader(descriptor)


def _real_hops(call_tree: CallTreeService) -> list[dict[str, Any]]:
    """A.entry → B.run（真实行号）+ B.run → C.write（行号 99——回查必失败）。"""

    a = _method_id(call_tree, "com.example.A", "entry")
    b = _method_id(call_tree, "com.example.B", "run")
    c = _method_id(call_tree, "com.example.C", "write")
    row = call_tree._reader.db.execute(
        "SELECT start_line FROM call_sites WHERE method_id = ? AND resolve_status = 'resolved' LIMIT 1",
        (a,),
    ).fetchone()
    assert row is not None
    return [
        {"from_method_id": a, "to_method_id": b, "call_site_line": int(row["start_line"]),
         "resolved_via": "direct_call"},
        {"from_method_id": b, "to_method_id": c, "call_site_line": 99,
         "resolved_via": "direct_call"},
    ]


def _partial_candidate(
    hops: list[dict[str, Any]], *, candidate_id: str = "expl_" + "b" * 20,
    evidence_refs: list[dict] | None = None,
    validation: dict | None | str = "__default__",
) -> dict[str, Any]:
    resolved_validation: dict | None
    if validation == "__default__":
        resolved_validation = {
            "status": "partially_validated", "verified_hop_count": 1,
            "failed_hop_indices": [1], "blocked_by_guard": False,
            "custom_sink_proposal": False, "notes": "1/2 跳回查通过；失败跳 [1]",
        }
    else:
        resolved_validation = validation  # type: ignore[assignment]
    return {
        "schema_version": "1.0.0",
        "candidate_id": candidate_id,
        "source": "explorer_agent",
        "prompt_version": "explorer/1.0.0",
        "model": "test-model",
        "component": {"kind": "activity", "name": "com.example.A", "exported": True,
                      "entry_method": "entry"},
        "api_entry_ref": "act_com_example_A_entry",
        "chain_proposal": {
            "source": "A.entry(input)", "sink": "C.write(value)", "hops": hops,
            "confidence": "medium", "hypothesis": "possible",
            "impact_proposal": "外部输入流向写入", "reasoning": "调用链",
            "evidence_refs": evidence_refs or [],
        },
        "validation": resolved_validation,
        "deep_dive": None,
    }


class FakeDeepDiveAI:
    """deep_dive_entry 协议替身：按轮次弹出 DeepDiveOutput spec（捕获输入）。"""

    def __init__(self, rounds: list[dict[str, Any]] | None = None):
        self._rounds = list(rounds or [{"complete": True, "facts": []}])
        self.inputs: list[Any] = []
        self.calls = 0

    async def __call__(self, model_input: Any) -> dict[str, Any]:
        self.calls += 1
        self.inputs.append(model_input)
        spec = self._rounds.pop(0) if self._rounds else {"complete": True, "facts": []}
        if spec.get("fail"):
            return {
                "status": spec["fail"],
                "circuit_breaking": spec.get("circuit", False),
                "metadata": {},
            }
        return {
            "status": "completed",
            "analysis": {
                "summary": spec.get("summary", "深挖一轮"),
                "resolved_facts": spec.get("facts", []),
                "evidence_refs": spec.get("evidence", []),
                "remaining_gaps": spec.get("gaps", []),
                "analysis_complete": spec.get("complete", True),
            },
            "metadata": {"prompt_version": "1.0.0", "model": "test-model"},
        }


def _dive_orchestrator(
    tmp_path: Path, fake: FakeDeepDiveAI, **settings: Any
) -> tuple[ExplorerOrchestrator, SQLiteCodeIndexReader]:
    call_tree = _service(tmp_path)
    reader = _index_reader(tmp_path)
    orchestrator = ExplorerOrchestrator(
        fake, call_tree, ExplorerSettings(**settings), tmp_path, deep_dive_call=fake
    )
    return orchestrator, reader


def _evidence_key(ref: dict[str, Any]) -> tuple:
    return (ref.get("path"), ref.get("line"), ref.get("end_line"))


def test_deep_dive_entry_invokes_prompt(tmp_path: Path) -> None:
    """A-1：协议入口以 (explorer-deep-dive, 1.0.0, DeepDiveInput, DeepDiveOutput) 调状态机。"""

    from unittest.mock import AsyncMock, patch

    from app.analysis.ai_models import DeepDiveInput
    from app.analysis.ai_runtime import AIRuntime
    from app.config import AISettings

    analyzer = AIRuntime(AISettings()).create_analyzer(
        cache_dir=tmp_path, max_output_tokens=100, budget_policy={}
    )
    model_input = DeepDiveInput.model_validate({
        "candidate_id": "expl_" + "c" * 20,
        "chain_proposal": _proposal(),
    })
    invoke = AsyncMock(return_value={"status": "completed", "analysis": {
        "summary": "s", "analysis_complete": True}})
    with (
        patch.object(analyzer, "_analysis_unavailable_result", return_value=None),
        patch.object(analyzer, "_invoke_prompt", invoke),
    ):
        result = asyncio_run(analyzer.deep_dive_entry(model_input))
    assert result["status"] == "completed"
    invoke.assert_awaited_once()
    args = invoke.await_args.args
    assert args[0] == "explorer-deep-dive"
    assert args[1] == "1.0.0"
    assert args[2] is model_input
    assert args[3].__name__ == "DeepDiveOutput"
    assert args[4] == "explorer-deep-dive"


def test_deep_dive_only_partials(tmp_path: Path) -> None:
    """A-2：仅 partially_validated 候选送深挖。"""

    call_tree = _service(tmp_path)
    hops = _real_hops(call_tree)
    fake = FakeDeepDiveAI([{"complete": True, "facts": []}])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)
    candidates = [
        _partial_candidate(hops, candidate_id="expl_" + "p" * 20),
        _partial_candidate(hops, candidate_id="expl_" + "v" * 20, validation={
            "status": "validated", "verified_hop_count": 2, "failed_hop_indices": [],
            "blocked_by_guard": False, "custom_sink_proposal": False, "notes": "2/2"}),
        _partial_candidate(hops, candidate_id="expl_" + "u" * 20, validation={
            "status": "unverified", "verified_hop_count": 0, "failed_hop_indices": [0, 1],
            "blocked_by_guard": False, "custom_sink_proposal": False, "notes": "跳均不可回查"}),
        _partial_candidate(hops, candidate_id="expl_" + "n" * 20, validation=None),
    ]

    counts = asyncio_run(orchestrator.deep_dive_partials(candidates, reader))

    assert counts["partial_total"] == 1
    assert counts["attempted"] is True
    assert candidates[0]["deep_dive"]["status"] == "completed"
    assert candidates[1]["deep_dive"] is None
    assert candidates[2]["deep_dive"] is None
    assert candidates[3]["deep_dive"] is None
    assert fake.calls == 1


def test_deep_dive_preserves_chain_and_validation(tmp_path: Path) -> None:
    """A-3（M2 验收 4.3-5.4）：深挖后链与三档逐字节不变，仅新增 deep_dive。"""

    call_tree = _service(tmp_path)
    hops = _real_hops(call_tree)
    candidate = _partial_candidate(hops)
    before_chain = json.dumps(candidate["chain_proposal"], sort_keys=True)
    before_validation = json.dumps(candidate["validation"], sort_keys=True)
    fake = FakeDeepDiveAI([{
        "complete": True,
        "facts": [{"claim_index": 0, "conclusion": "confirmed", "reasoning": "调用边存在",
                   "evidence": [{"path": "com/example/B.java", "line": 4}]}],
        "evidence": [{"path": "com/example/B.java", "line": 4}],
    }])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    asyncio_run(orchestrator.deep_dive_partials([candidate], reader))

    assert json.dumps(candidate["chain_proposal"], sort_keys=True) == before_chain
    assert json.dumps(candidate["validation"], sort_keys=True) == before_validation
    assert candidate["deep_dive"] is not None  # 仅新增 deep_dive 字段


def test_deep_dive_missing_facts_deterministic(tmp_path: Path) -> None:
    """A-4/N-2：missing_facts 从校验缺口确定性生成；越界索引不生成。"""

    call_tree = _service(tmp_path)
    hops = _real_hops(call_tree)
    candidate = _partial_candidate(hops)
    candidate["validation"]["failed_hop_indices"] = [1, 7]  # 7 越界
    candidate["validation"]["blocked_by_guard"] = True
    fake = FakeDeepDiveAI([{"complete": True, "facts": []}])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    asyncio_run(orchestrator.deep_dive_partials([candidate], reader))

    facts = fake.inputs[0].missing_facts
    assert len(facts) == 2
    assert "第 1 跳调用关系待证实" in facts[0]
    assert hops[1]["to_method_id"] in facts[0]
    assert "debuggable guard" in facts[1]


def test_deep_dive_code_context_and_gate(tmp_path: Path) -> None:
    """A-5/N-7：失败跳方法体进入 context；门禁关闭不外发；缺失方法体跳过。"""

    call_tree = _service(tmp_path)
    hops = _real_hops(call_tree)
    hops[1]["to_method_id"] = "sources/com/example/Missing.java#gone:1"  # N-7 缺失
    candidate = _partial_candidate(hops)
    fake = FakeDeepDiveAI([{"complete": True, "facts": []}])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    asyncio_run(orchestrator.deep_dive_partials([candidate], reader))

    context = fake.inputs[0].code_context
    assert context is not None
    # 失败跳（index=1）的 from 方法体（com.example.B.run）；to=Missing 跳过（N-7）
    assert "com.example.B" in context
    assert "com.example.Missing" not in context
    assert len(context) <= 9500

    # 门禁：allow_external_code=False → code_context=None（评审 R-4）
    candidate2 = _partial_candidate(_real_hops(call_tree), candidate_id="expl_" + "g" * 20)
    fake2 = FakeDeepDiveAI([{"complete": True, "facts": []}])
    orchestrator2, reader2 = _dive_orchestrator(tmp_path, fake2, allow_external_code=False)
    asyncio_run(orchestrator2.deep_dive_partials([candidate2], reader2))
    assert fake2.inputs[0].code_context is None


def test_deep_dive_evidence_verification(tmp_path: Path) -> None:
    """A-6/N-5：证据回查过滤——不可回查丢弃计数；前缀剥离命中；倒序区间拒。"""

    call_tree = _service(tmp_path)
    hops = _real_hops(call_tree)
    candidate = _partial_candidate(hops)
    fake = FakeDeepDiveAI([{
        "complete": True, "facts": [],
        "evidence": [
            {"path": "com/example/B.java", "line": 4},              # 可回查
            {"path": "com/example/Nope.java", "line": 1},           # 文件不存在
            {"path": "com/example/B.java", "line": 9999},           # 行越界
            {"path": "sources/com/example/B.java", "line": 4},      # 前缀剥离后命中
            {"path": "com/example/B.java", "line": 4, "end_line": 2},  # N-5 倒序区间
            {"path": "com/example/B.java"},                          # 无行号：文件存在即过
        ],
    }])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    counts = asyncio_run(orchestrator.deep_dive_partials([candidate], reader))

    dive = candidate["deep_dive"]
    kept_keys = [_evidence_key(ref) for ref in dive["evidence_refs"]]
    assert ("com/example/B.java", 4, None) in kept_keys          # 原样
    assert ("sources/com/example/B.java", 4, None) in kept_keys  # 前缀形态保留原文
    assert ("com/example/B.java", None, None) in kept_keys       # 无行号
    assert dive["unverifiable_evidence_count"] == 3
    assert counts["unverifiable_evidence_dropped"] == 3


def test_deep_dive_initial_evidence_pool_filtered(tmp_path: Path) -> None:
    """A-18（评审 R-9）：初始池=chain_proposal.evidence_refs 过滤存活项。"""

    call_tree = _service(tmp_path)
    hops = _real_hops(call_tree)
    candidate = _partial_candidate(hops, evidence_refs=[
        {"path": "com/example/A.java", "line": 3},        # 存活
        {"path": "com/example/X.java", "line": 1},        # 丢弃
    ])
    fake = FakeDeepDiveAI([{"complete": True, "facts": []}])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    asyncio_run(orchestrator.deep_dive_partials([candidate], reader))

    initial = fake.inputs[0].existing_evidence_refs
    assert [ref.line for ref in initial] == [3]
    assert candidate["deep_dive"]["unverifiable_evidence_count"] == 1


def test_deep_dive_fact_merge_across_rounds(tmp_path: Path) -> None:
    """A-7：后轮同 claim_index 覆盖前轮；轮记录含当轮全量 output。"""

    call_tree = _service(tmp_path)
    hops = _real_hops(call_tree)
    candidate = _partial_candidate(hops)
    fake = FakeDeepDiveAI([
        {"complete": False, "facts": [
            {"claim_index": 0, "conclusion": "still_unknown", "reasoning": "证据不足"}]},
        {"complete": True, "facts": [
            {"claim_index": 0, "conclusion": "confirmed", "reasoning": "调用边存在",
             "evidence": [{"path": "com/example/B.java", "line": 4}]}]},
    ])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    asyncio_run(orchestrator.deep_dive_partials([candidate], reader))

    dive = candidate["deep_dive"]
    assert dive["status"] == "completed"
    assert dive["resolved_facts"][0]["conclusion"] == "confirmed"
    assert len(dive["rounds"]) == 2
    assert dive["rounds"][0]["output"]["resolved_facts"][0]["conclusion"] == "still_unknown"
    assert dive["rounds"][1]["output"]["resolved_facts"][0]["conclusion"] == "confirmed"
    assert dive["requests_used"] == 2


def test_deep_dive_terminates_on_complete(tmp_path: Path) -> None:
    """A-8①：analysis_complete=True 首轮即止。"""

    call_tree = _service(tmp_path)
    candidate = _partial_candidate(_real_hops(call_tree))
    fake = FakeDeepDiveAI([{"complete": True, "facts": []}] * 4)
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    asyncio_run(orchestrator.deep_dive_partials([candidate], reader))
    assert candidate["deep_dive"]["status"] == "completed"
    assert candidate["deep_dive"]["requests_used"] == 1


def test_deep_dive_stagnation_after_two_rounds(tmp_path: Path) -> None:
    """A-8②（评审 R-2）：首轮不判停滞；连续两轮无新增判定才终止。"""

    call_tree = _service(tmp_path)
    candidate = _partial_candidate(_real_hops(call_tree))
    unknown = [{"claim_index": 0, "conclusion": "still_unknown", "reasoning": "证据不足"}]
    fake = FakeDeepDiveAI([
        {"complete": False, "facts": unknown},   # 轮 1：无进展（不终止）
        {"complete": False, "facts": unknown},   # 轮 2：连续第二无进展 → 停滞
        {"complete": True, "facts": []},         # 不应到达
    ])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    asyncio_run(orchestrator.deep_dive_partials([candidate], reader))
    assert candidate["deep_dive"]["status"] == "incomplete"
    assert candidate["deep_dive"]["requests_used"] == 2
    assert fake.calls == 2


def test_deep_dive_budget_exhaustion(tmp_path: Path) -> None:
    """A-8③：跑满轮数预算 → incomplete（每轮新增 confirmed 判定，不停滞）。"""

    call_tree = _service(tmp_path)
    candidate = _partial_candidate(_real_hops(call_tree))
    fake = FakeDeepDiveAI([
        {"complete": False, "facts": [
            {"claim_index": index, "conclusion": "confirmed", "reasoning": "逐项证实"}
            for index in range(round_index + 1)
        ]}
        for round_index in range(4)
    ])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    asyncio_run(orchestrator.deep_dive_partials([candidate], reader))
    dive = candidate["deep_dive"]
    assert dive["status"] == "incomplete"
    assert dive["requests_used"] == 4
    assert len(dive["rounds"]) == 4


def test_deep_dive_ai_failure_tolerated(tmp_path: Path) -> None:
    """A-9：AI 失败（非熔断）→ failed；批次不中断。"""

    call_tree = _service(tmp_path)
    hops = _real_hops(call_tree)
    first = _partial_candidate(hops, candidate_id="expl_" + "f" * 20)
    second = _partial_candidate(hops, candidate_id="expl_" + "s" * 20)
    fake = FakeDeepDiveAI([
        {"fail": "error"},
        {"complete": True, "facts": []},
    ])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    counts = asyncio_run(orchestrator.deep_dive_partials([first, second], reader))

    assert counts["failed"] == 1
    assert counts["completed"] == 1
    assert first["deep_dive"]["status"] == "failed"
    assert second["deep_dive"]["status"] == "completed"


def test_deep_dive_run_budget_exhausted_skips(tmp_path: Path) -> None:
    """A-10：熔断类结果（预算耗尽包装）→ skipped。"""

    call_tree = _service(tmp_path)
    candidate = _partial_candidate(_real_hops(call_tree))
    fake = FakeDeepDiveAI([{"fail": "skipped", "circuit": True}])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    counts = asyncio_run(orchestrator.deep_dive_partials([candidate], reader))

    assert counts["skipped"] == 1
    assert candidate["deep_dive"]["status"] == "skipped"


def test_deep_dive_not_injected_all_skipped(tmp_path: Path) -> None:
    """A-11：deep_dive_call 未注入 → 全体 skipped（不抛）。"""

    call_tree = _service(tmp_path)
    reader = _index_reader(tmp_path)
    orchestrator = ExplorerOrchestrator(None, call_tree, ExplorerSettings(), tmp_path)  # type: ignore[arg-type]
    candidate = _partial_candidate(_real_hops(call_tree))

    counts = asyncio_run(orchestrator.deep_dive_partials([candidate], reader))

    assert counts == {
        "partial_total": 1, "attempted": 0, "completed": 0, "incomplete": 0,
        "failed": 0, "skipped": 1, "requests_used": 0,
        "unverifiable_evidence_dropped": 0,
    }
    assert candidate["deep_dive"]["status"] == "skipped"


def test_deep_dive_batch_short_circuit(tmp_path: Path) -> None:
    """A-17（评审 R-5）：首个熔断后剩余候选批量 skipped（零 AI 调用）。"""

    call_tree = _service(tmp_path)
    hops = _real_hops(call_tree)
    first = _partial_candidate(hops, candidate_id="expl_" + "x" * 20)
    second = _partial_candidate(hops, candidate_id="expl_" + "y" * 20)
    fake = FakeDeepDiveAI([{"fail": "circuit_open", "circuit": True}])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    counts = asyncio_run(orchestrator.deep_dive_partials([first, second], reader))

    assert counts["skipped"] == 2
    assert fake.calls == 1  # 第二候选零调用（短路）
    assert second["deep_dive"]["status"] == "skipped"
    assert second["deep_dive"]["requests_used"] == 0


def test_deep_dive_no_hops_skipped(tmp_path: Path) -> None:
    """N-1：hops 缺失的 partial → 跳过（无可锚定链事实）。"""

    candidate = _partial_candidate([])
    fake = FakeDeepDiveAI([{"complete": True, "facts": []}])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    counts = asyncio_run(orchestrator.deep_dive_partials([candidate], reader))

    assert counts["skipped"] == 1
    assert fake.calls == 0


def test_candidate_with_deep_dive_schema_valid(tmp_path: Path) -> None:
    """A-12：含 deep_dive 字段的候选经 explorer_candidate.schema.json 校验合法。"""

    with (SCHEMAS_DIR / "explorer_candidate.schema.json").open(encoding="utf-8") as fp:
        schema = json.load(fp)
    call_tree = _service(tmp_path)
    candidate = _partial_candidate(_real_hops(call_tree))
    fake = FakeDeepDiveAI([{
        "complete": True,
        "facts": [{"claim_index": 0, "conclusion": "confirmed", "reasoning": "调用边存在",
                   "evidence": [{"path": "com/example/B.java", "line": 4}]}],
        "evidence": [{"path": "com/example/B.java", "line": 4}],
    }])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)
    asyncio_run(orchestrator.deep_dive_partials([candidate], reader))
    jsonschema.validate(candidate, schema)


def test_deep_dive_missing_facts_truncated(tmp_path: Path) -> None:
    """N-3：missing_facts 超 32（32 跳 + guard 命题 = 33）截断 + gaps 首项说明。"""

    hops = [
        {"from_method_id": f"m{index}", "to_method_id": f"m{index + 1}",
         "call_site_line": index + 1, "resolved_via": "direct_call"}
        for index in range(32)
    ]
    candidate = _partial_candidate(hops)
    candidate["validation"]["failed_hop_indices"] = list(range(32))
    candidate["validation"]["blocked_by_guard"] = True
    fake = FakeDeepDiveAI([{"complete": True, "facts": []}])
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    asyncio_run(orchestrator.deep_dive_partials([candidate], reader))

    assert len(fake.inputs[0].missing_facts) == 32  # schema 上界（33 条被截断）
    assert "截断" in candidate["deep_dive"]["remaining_gaps"][0]


def test_deep_dive_output_invalid_fails(tmp_path: Path) -> None:
    """N-4：模型输出违反 schema（缺 required）→ failed + 轮记录 output_invalid。"""

    call_tree = _service(tmp_path)
    candidate = _partial_candidate(_real_hops(call_tree))

    class BadOutputAI(FakeDeepDiveAI):
        async def __call__(self, model_input: Any) -> dict[str, Any]:
            self.calls += 1
            self.inputs.append(model_input)
            return {"status": "completed", "analysis": {"summary": "缺 analysis_complete"},
                    "metadata": {"prompt_version": "1.0.0", "model": "test-model"}}

    fake = BadOutputAI()
    orchestrator, reader = _dive_orchestrator(tmp_path, fake)

    counts = asyncio_run(orchestrator.deep_dive_partials([candidate], reader))

    assert counts["failed"] == 1
    assert candidate["deep_dive"]["status"] == "failed"
    assert candidate["deep_dive"]["rounds"][0]["status"] == "output_invalid"


# ---------------------------------------------------------------------------
# M2 收尾-3：攻击面事实注入（稳定修复——入口可控性从"模型猜"到"直接看到"）
# ---------------------------------------------------------------------------


def test_load_attack_surface_index(tmp_path: Path) -> None:
    """索引构造：四 kind 文件聚合、按组件名索引、缺失/损坏容错。"""
    from app.analysis.explorer import load_attack_surface_index

    surface = tmp_path / "attack_surface"
    surface.mkdir()
    (surface / "service.json").write_text(json.dumps({
        "package": "com.example", "schema_version": "1.0.0",
        "components": [
            {"name": "com.example.Svc", "kind": "service", "exported": True,
             "permission": None, "sensitive_capabilities": ["device_id"]},
            {"name": None, "kind": "service"},  # 无名条目跳过
        ],
    }), "utf-8")
    (surface / "receiver.json").write_text("{ not-json", "utf-8")  # 损坏容错

    index = load_attack_surface_index(tmp_path)
    assert set(index) == {"com.example.Svc"}
    assert index["com.example.Svc"]["exported"] is True

    empty_dir = tmp_path / "no-such-dir"
    assert load_attack_surface_index(empty_dir) == {}


def test_attack_surface_injected_into_model_input(tmp_path: Path) -> None:
    """注入通道：构造 index 传入 → 每轮 model_input.attack_surface_json 非空；
    未传 → None（降级语义不变）。"""
    fake = FakeAnalyzer([{"done": True, "proposals": [_proposal()]}])
    call_tree = _service(tmp_path)
    entry = _entry(call_tree)
    component_name = entry.get("component_name")
    orchestrator = ExplorerOrchestrator(
        fake, call_tree, ExplorerSettings(), tmp_path,
        attack_surface={str(component_name): {
            "name": component_name, "kind": entry.get("kind"),
            "exported": True, "sensitive_capabilities": ["device_id"],
        }},
    )

    asyncio_run(orchestrator.explore_all([entry]))
    assert len(fake.inputs) == 1
    injected = fake.inputs[0].attack_surface_json
    assert injected is not None
    payload = json.loads(injected)
    assert payload["name"] == component_name
    assert payload["exported"] is True

    # 降级：不传 attack_surface → null（既有行为不变）
    fake2 = FakeAnalyzer([{"done": True, "proposals": [_proposal()]}])
    call_tree2 = _service(tmp_path)
    entry2 = _entry(call_tree2)
    orchestrator2 = ExplorerOrchestrator(fake2, call_tree2, ExplorerSettings(), tmp_path)
    asyncio_run(orchestrator2.explore_all([entry2]))
    assert fake2.inputs[0].attack_surface_json is None


# ---------------------------------------------------------------------------
# M4-SEED-HOPS：骨架链第一跳（确定性可回查——评审 R-1 三要素）
# ---------------------------------------------------------------------------


def test_seed_hops_built_from_call_sites(tmp_path: Path) -> None:
    """A-2：入口有 resolved 边 → 前 N 个 callee 组装为 SeedHop（三要素）。"""
    fake = FakeAnalyzer([{"done": True, "proposals": [_proposal()]}])
    call_tree = _service(tmp_path)
    entry = _entry(call_tree)
    orchestrator = ExplorerOrchestrator(fake, call_tree, ExplorerSettings(), tmp_path)
    seed_hops = orchestrator._build_seed_hops(entry)
    assert seed_hops, "A→B 链应有 resolved 第一跳"
    assert entry["method_id"]
    for hop in seed_hops:
        assert hop.from_method_id == entry["method_id"]
        assert hop.to_method_id
        assert hop.call_site_line >= 1
    assert len(seed_hops) <= 8


def test_seed_hops_injected_every_round(tmp_path: Path) -> None:
    """A-4：每轮 model_input.seed_hops 同一非空列表（幂等注入）。"""
    fake = FakeAnalyzer([
        {"done": False, "requests": [{"operation": "get_method_body", "target": None}]},
        {"done": True, "proposals": [_proposal()]},
    ])
    call_tree = _service(tmp_path)
    entry = _entry(call_tree)
    fake._rounds[0]["requests"][0]["target"] = entry["method_id"]
    orchestrator = ExplorerOrchestrator(fake, call_tree, ExplorerSettings(), tmp_path)

    asyncio_run(orchestrator.explore_all([entry]))
    assert len(fake.inputs) == 2
    seed_lists = [model_input.seed_hops for model_input in fake.inputs]
    assert all(seed_lists) and seed_lists[0] == seed_lists[1]


def test_seed_hops_degrade_to_empty(tmp_path: Path) -> None:
    """A-3：无 method_id / 库不可读 → 空列表降级（探索不阻塞）。"""
    fake = FakeAnalyzer([{"done": True, "proposals": [_proposal()]}])
    call_tree = _service(tmp_path)
    orchestrator = ExplorerOrchestrator(fake, call_tree, ExplorerSettings(), tmp_path)
    assert orchestrator._build_seed_hops({"method_id": None}) == []
    assert orchestrator._build_seed_hops({"method_id": "x#y:1"}) == []


def test_entry_coverage_transparency(tmp_path: Path) -> None:
    """F4：入口覆盖透明化——截断与全探索两态（核验 V-2 正例补强）。"""
    call_tree = _service(tmp_path)
    entries = [_entry(call_tree), _entry(call_tree)]
    # 上限 1：首入口产链后截断——第二入口不探索
    fake = FakeAnalyzer([
        {"done": True, "proposals": [_proposal()]},
        {"done": True, "proposals": [_proposal()]},
    ])
    orchestrator = ExplorerOrchestrator(
        fake, call_tree, ExplorerSettings(max_candidates_per_run=1), tmp_path)
    asyncio_run(orchestrator.explore_all(entries))
    assert orchestrator.entries_explored == 1  # 截断态：只探索了首入口

    # 全探索态（无截断）：entries_explored == 输入总数
    fake2 = FakeAnalyzer([
        {"done": True, "proposals": [_proposal()]},
        {"done": True, "proposals": [_proposal()]},
    ])
    orchestrator2 = ExplorerOrchestrator(
        fake2, call_tree, ExplorerSettings(), tmp_path)
    asyncio_run(orchestrator2.explore_all(entries))
    assert orchestrator2.entries_explored == len(entries)  # 正例：全覆盖


# ---------------------------------------------------------------------------
# F5：目标组件引导（known_findings 注入 A5-2 / 请求增量执行 A5-4 /
# 入口优先级 + 复读守卫集成 A5-1 + A5-5）
# ---------------------------------------------------------------------------


def test_known_findings_injected_into_model_input(tmp_path: Path) -> None:
    """A5-2：有 finding 组件注入摘要 JSON；无 finding 组件为 None。"""
    call_tree = _service(tmp_path)
    entry = _entry(call_tree)
    fake = FakeAnalyzer([{"done": True, "proposals": [_proposal()]}])
    orchestrator = ExplorerOrchestrator(
        fake, call_tree, ExplorerSettings(), tmp_path,
        known_findings={"com.example.A": [
            {"rule": "ACTIVITY_INTENT_TO_SENSITIVE_SINK", "severity": "medium"},
        ]},
    )
    asyncio_run(orchestrator.explore_all([entry]))
    assert fake.inputs[0].known_findings is not None
    payload = json.loads(fake.inputs[0].known_findings)
    assert payload == [{"rule": "ACTIVITY_INTENT_TO_SENSITIVE_SINK", "severity": "medium"}]


def test_known_findings_absent_for_unguided_component(tmp_path: Path) -> None:
    """A5-2：known_findings 无该组件条目（或空映射）→ 输入为 None（不注入）。"""
    call_tree = _service(tmp_path)
    entry = _entry(call_tree)
    fake = FakeAnalyzer([{"done": True, "proposals": [_proposal()]}])
    # 空映射（rule_prescan 零候选形态）
    orchestrator = ExplorerOrchestrator(
        fake, call_tree, ExplorerSettings(), tmp_path, known_findings={})
    asyncio_run(orchestrator.explore_all([entry]))
    assert fake.inputs[0].known_findings is None
    # 映射有其他组件（撞名不同包）——精确匹配不命中 com.example.A
    fake2 = FakeAnalyzer([{"done": True, "proposals": [_proposal()]}])
    orchestrator2 = ExplorerOrchestrator(
        fake2, call_tree, ExplorerSettings(), tmp_path,
        known_findings={"other.example.A": [{"rule": "R", "severity": "low"}]},
    )
    asyncio_run(orchestrator2.explore_all([entry]))
    assert fake2.inputs[0].known_findings is None


def test_duplicate_requests_terminate_no_new_requests(tmp_path: Path) -> None:
    """A5-4 完全重叠：轮请求与历史完全重复 → no_new_requests 干净终止。"""
    call_tree = _service(tmp_path)
    entry = _entry(call_tree)
    request = {"operation": "get_method_body", "target": entry["method_id"]}
    fake = FakeAnalyzer([
        {"done": False, "requests": [request]},
        {"done": False, "requests": [dict(request)]},  # 完全重复（零增量）
    ])
    orchestrator = ExplorerOrchestrator(
        fake, call_tree, ExplorerSettings(max_rounds_per_entry=4), tmp_path)
    asyncio_run(orchestrator.explore_all([entry]))
    observations = json.loads((tmp_path / "explorer" / "observations.json").read_text("utf-8"))
    record = observations["entries"][0]
    assert record["terminated_by"] == "no_new_requests"
    assert len(record["rounds"]) == 2  # 第二轮零增量即终止（第三轮不执行）
    assert record["rounds"][1]["requests_deduplicated"] == 1
    assert record["rounds"][1]["requests_executed"] == []
    # 重复请求不消耗读码预算（只首执行计 1 次）
    assert orchestrator.read_requests_used == 1


def test_partial_overlap_executes_increment_only(tmp_path: Path) -> None:
    """A5-4 部分重叠：去重执行——只执行增量请求，非重叠部分仍获探索。"""
    call_tree = _service(tmp_path)
    entry = _entry(call_tree)
    b_run = _method_id(call_tree, "com.example.B", "run")
    first = {"operation": "get_method_body", "target": entry["method_id"]}
    fake = FakeAnalyzer([
        {"done": False, "requests": [first]},
        # 部分重叠：重复 first + 新增 B.run 方法体请求
        {"done": False, "requests": [dict(first), {"operation": "get_method_body", "target": b_run}]},
        {"done": True, "proposals": [_proposal()]},
    ])
    orchestrator = ExplorerOrchestrator(
        fake, call_tree, ExplorerSettings(max_rounds_per_entry=4), tmp_path)
    asyncio_run(orchestrator.explore_all([entry]))
    observations = json.loads((tmp_path / "explorer" / "observations.json").read_text("utf-8"))
    record = observations["entries"][0]
    assert record["terminated_by"] == "loop_done"
    assert len(record["rounds"]) == 3
    second = record["rounds"][1]
    assert second["requests_deduplicated"] == 1  # 重复项跳过
    assert len(second["requests_executed"]) == 1  # 只执行增量（B.run）
    assert orchestrator.read_requests_used == 2  # 总消耗 = 首轮 1 + 次轮增量 1


def test_explorer_stage_entry_priority_and_replay_guard(tmp_path: Path) -> None:
    """A5-1 + A5-5 集成：finding 组件入口优先（覆盖口径不变）+ 复读守卫。"""

    orchestrator, storage, run_id, run_dir, descriptor = _instance_orchestrator(tmp_path)
    # 加 D 组件入口（真实 Activity lifecycle 可解析 method_id；原序在 A 前——
    # 排序后 A 应反超到首位）
    source_root = tmp_path / "sources"
    (source_root / "com/example/D.java").write_text("""package com.example;
public class D extends android.app.Activity {
  protected void onCreate(android.os.Bundle savedInstanceState) {
  }
}
""", "utf-8")
    descriptor = build_code_index(source_root, tmp_path / "index" / "code-index.json")
    (run_dir / "api-surface" / "api_entry_table.json").write_text(json.dumps({
        "api_entries": [
            {
                "entry_id": "act_com_example_D_onCreate",
                "kind": "activity",
                "component_name": "com.example.D",
                "source": "manifest",
                "entry_method": "onCreate",
            },
            {
                "entry_id": "act_com_example_A_onCreate",
                "kind": "activity",
                "component_name": "com.example.A",
                "source": "manifest",
                "entry_method": "onCreate",
            },
        ],
    }), "utf-8")
    orchestrator.ai = FakeExploreAI(proposal=_real_proposal(descriptor))

    # 规则候选：com.example.A 有 finding，sink 与探索链尾 C.write 同键
    # （method_id 从索引提取——_sink_keys method 键命中即复读）
    reader = SQLiteCodeIndexReader(descriptor)
    try:
        row = reader.db.execute(
            "SELECT id FROM methods WHERE qualified_class = ? AND name = ?",
            ("com.example.C", "write"),
        ).fetchone()
        c_write_id = str(row["id"])
    finally:
        reader.close()
    rule_candidates = [{
        "candidate_id": "rule_001",
        "candidate_source": "rule_engine",
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "severity_hint": "medium",
        "component_name": "com.example.A",
        "sinks": [{"kind": "sink_call", "path": "com/example/C.java", "line": 6,
                   "method_id": c_write_id}],
    }]

    normalized = asyncio_run(orchestrator._run_explorer_stage(
        run_id, run_dir, {"debuggable": False, "target_sdk": 36}, descriptor,
        rule_candidates,
    ))

    # A5-1 排序：A（有 finding）入口先于 D（原序在前被反超）
    observations = json.loads((run_dir / "explorer" / "observations.json").read_text("utf-8"))
    entry_ids = [e["entry_id"] for e in observations["entries"]]
    assert entry_ids == ["act_com_example_A_onCreate", "act_com_example_D_onCreate"]
    # 覆盖口径不变：无 finding 组件 D 仍被探索（非跳过——干净出口正常终止）
    assert len(observations["entries"]) == 2
    assert all(e["terminated_by"] != "short_circuited" for e in observations["entries"])

    # A5-2 集成：A 入口轮输入注入 known_findings 摘要
    a_input = orchestrator.ai.inputs[0]
    assert a_input.known_findings is not None
    assert json.loads(a_input.known_findings) == [
        {"rule": "ACTIVITY_INTENT_TO_SENSITIVE_SINK", "severity": "medium"}]

    # A5-5 复读守卫：探索候选 sink 命中 finding sink → 标记 + 降档 + gap
    assert len(normalized) == 1
    replay = normalized[0]
    assert replay["replayed_finding"] is True
    assert replay["replayed_rule_id"] == "ACTIVITY_INTENT_TO_SENSITIVE_SINK"
    assert replay["confidence_tier"] == "low"
    gap_codes = [g["code"] for g in replay["blocking_gaps"]]
    assert "EXPLORER_FINDING_REPLAY" in gap_codes

    # stage summary：引导透明化（F4 先例模式）
    manifest = storage.read_manifest(run_id)
    stage = next(s for s in manifest["stages"] if s["name"] == "explorer")
    summary = stage["summary"]
    assert summary["finding_guided_entries"] == 1
    assert summary["normalization_counts"]["finding_replays"] == 1
