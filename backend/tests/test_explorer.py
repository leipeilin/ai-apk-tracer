"""探索 Agent 驱动循环测试（T2.5b）。

设计：docs/analysis/2026-08-22-t2-5b-implementation-plan.md（含评审
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
from app.config import (
    ApiSurfaceSettings,
    ExplorerSettings,
    Settings,
    SourceAnalysisSettings,
    StorageSettings,
)
from app.main import create_app

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
    requests = [{"operation": "search_symbol", "target": "run"} for _ in range(8)]
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
