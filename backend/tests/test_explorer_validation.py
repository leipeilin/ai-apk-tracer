"""探索候选三档校验测试（T2.6）。

设计：docs/analysis/2026-08-22-t2-6-implementation-plan.md（含评审 R-1~R-8
修订）。真实 index（调用链 + guard 组件）承载回查；手造候选覆盖三档。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import jsonschema
from fastapi.testclient import TestClient

from app.analysis.explorer_validation import validate_explorer_candidates
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

_SOURCES = {
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
    # guard 组件（复用 test_guard_verifier 的行布局语义：handleIntent 带 debuggable guard）
    "com/example/DebugActivity.java": """package com.example;
public class DebugActivity {
  protected void onNewIntent(android.content.Intent intent) {
    handleIntent(intent);
  }
  private void handleIntent(android.content.Intent intent) {
    if ((getApplicationInfo().flags & 2) == 0) {
      return;
    }
  }
}
""",
}


def _reader(tmp_path: Path) -> SQLiteCodeIndexReader:
    source_root = tmp_path / "sources"
    for relative, content in _SOURCES.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    descriptor = build_code_index(source_root, tmp_path / "index" / "code-index.json")
    return SQLiteCodeIndexReader(descriptor)


def _method_ids(reader: SQLiteCodeIndexReader) -> dict[tuple[str, str], str]:
    rows = reader.db.execute("SELECT id, qualified_class, name FROM methods").fetchall()
    return {(row["qualified_class"], row["name"]): row["id"] for row in rows}


def _call_site(reader: SQLiteCodeIndexReader, from_id: str) -> tuple[str, int]:
    row = reader.db.execute(
        "SELECT resolved_target_id, start_line FROM call_sites WHERE method_id = ? AND resolve_status = 'resolved' LIMIT 1",
        (from_id,),
    ).fetchone()
    return row["resolved_target_id"], row["start_line"]


def _candidate(hops: list[dict[str, Any]], candidate_id: str = "expl_" + "a" * 20) -> dict:
    return {
        "schema_version": "1.0.0",
        "candidate_id": candidate_id,
        "source": "explorer_agent",
        "prompt_version": "explorer/1.0.0",
        "model": "test-model",
        "component": {"kind": "activity", "name": "com.example.A", "exported": True, "entry_method": "entry"},
        "api_entry_ref": "act_com_example_A_entry",
        "chain_proposal": {
            "source": "A.entry(input)", "sink": "C.write(value)",
            "hops": hops,
            "confidence": "medium", "hypothesis": "possible",
            "impact_proposal": "外部输入流向写入", "reasoning": "调用链",
        },
        "validation": None,
    }


def _hop(from_id: str, to_id: str, line: int) -> dict:
    return {"from_method_id": from_id, "to_method_id": to_id, "call_site_line": line, "resolved_via": "direct_call"}


_RELEASE_FACTS = {"debuggable": False, "target_sdk": 36}
_DEBUG_FACTS = {"debuggable": True, "target_sdk": 36}


# ---------------------------------------------------------------------------
# A-1~A-3：三档
# ---------------------------------------------------------------------------


def test_validated_full_hops(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    ids = _method_ids(reader)
    a = ids[("com.example.A", "entry")]
    b = ids[("com.example.B", "run")]
    ab_to, ab_line = _call_site(reader, a)
    bc_to, bc_line = _call_site(reader, b)

    candidate = _candidate([
        _hop(a, ab_to, ab_line),
        _hop(b, bc_to, bc_line),
    ])
    counts = validate_explorer_candidates([candidate], reader, str(tmp_path / "index" / "analysis.sqlite3"), _RELEASE_FACTS)

    assert counts == {"validated": 1, "partially_validated": 0, "unverified": 0}
    validation = candidate["validation"]
    assert validation["status"] == "validated"
    assert validation["verified_hop_count"] == 2
    assert validation["failed_hop_indices"] == []
    assert validation["blocked_by_guard"] is False
    assert "2/2 跳回查通过" in validation["notes"]
    # A-6：填充后 schema 合法
    schema = json.loads((SCHEMAS_DIR / "explorer_candidate.schema.json").read_text("utf-8"))
    jsonschema.validate(candidate, schema)


def test_partially_validated(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    ids = _method_ids(reader)
    a = ids[("com.example.A", "entry")]
    ab_to, ab_line = _call_site(reader, a)

    candidate = _candidate([
        _hop(a, ab_to, ab_line),
        _hop("sources/missing.java#X.nope:1", ab_to, 99),  # 伪 method_id
    ])
    counts = validate_explorer_candidates([candidate], reader, "", _RELEASE_FACTS)

    assert counts["partially_validated"] == 1
    validation = candidate["validation"]
    assert validation["status"] == "partially_validated"
    assert validation["verified_hop_count"] == 1
    assert validation["failed_hop_indices"] == [1]


def test_unverified(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    candidate = _candidate([
        _hop("sources/x.java#X.a:1", "sources/y.java#Y.b:1", 5),
        _hop("sources/z.java#Z.c:1", "sources/w.java#W.d:1", 6),
    ])
    counts = validate_explorer_candidates([candidate], reader, "", _RELEASE_FACTS)
    assert counts["unverified"] == 1
    assert candidate["validation"]["status"] == "unverified"
    assert candidate["validation"]["verified_hop_count"] == 0


def test_line_mismatch_diagnostic(tmp_path: Path) -> None:
    """评审 R-1：行号不匹配但调用边存在——notes 诊断标记。"""
    reader = _reader(tmp_path)
    ids = _method_ids(reader)
    a = ids[("com.example.A", "entry")]
    ab_to, _ = _call_site(reader, a)

    candidate = _candidate([_hop(a, ab_to, 999)])  # 错误行号（边存在）
    validate_explorer_candidates([candidate], reader, "", _RELEASE_FACTS)
    notes = candidate["validation"]["notes"]
    assert "行号不匹配" in notes


# ---------------------------------------------------------------------------
# A-4：guard 阻断
# ---------------------------------------------------------------------------


def test_blocked_by_guard(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    ids = _method_ids(reader)
    debug_on_new_intent = ids[("com.example.DebugActivity", "onNewIntent")]
    debug_handle = ids[("com.example.DebugActivity", "handleIntent")]
    to_id, line = _call_site(reader, debug_on_new_intent)
    assert to_id == debug_handle

    candidate = _candidate([_hop(debug_on_new_intent, to_id, line)])
    index_path = str(tmp_path / "index" / "analysis.sqlite3")

    # release 包：debuggable guard 阻断（入口不可达）
    counts = validate_explorer_candidates([candidate], reader, index_path, _RELEASE_FACTS)
    assert candidate["validation"]["blocked_by_guard"] is True
    assert "guard" in candidate["validation"]["notes"]
    assert counts["validated"] == 1  # 跳回查通过（guard 是独立维度）

    # debug 包：guard 不阻断
    debug_candidate = _candidate([_hop(debug_on_new_intent, to_id, line)])
    validate_explorer_candidates([debug_candidate], reader, index_path, _DEBUG_FACTS)
    assert debug_candidate["validation"]["blocked_by_guard"] is False


# ---------------------------------------------------------------------------
# A-5/N-1/N-2/N-5
# ---------------------------------------------------------------------------


def test_validation_counts(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    ids = _method_ids(reader)
    a = ids[("com.example.A", "entry")]
    ab_to, ab_line = _call_site(reader, a)
    candidates = [
        _candidate([_hop(a, ab_to, ab_line)]),
        _candidate([_hop(a, ab_to, ab_line), _hop("sources/m.java#M.x:1", ab_to, 9)], "expl_" + "b" * 20),
        _candidate([_hop("sources/m.java#M.x:1", "sources/n.java#N.y:1", 9)], "expl_" + "c" * 20),
    ]
    counts = validate_explorer_candidates(candidates, reader, "", _RELEASE_FACTS)
    assert counts == {"validated": 1, "partially_validated": 1, "unverified": 1}


def test_empty_and_malformed_candidates(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    # N-2：空列表
    assert validate_explorer_candidates([], reader, "", _RELEASE_FACTS) == {
        "validated": 0, "partially_validated": 0, "unverified": 0,
    }
    # N-1：hops 结构异常（缺失）
    malformed = _candidate([])
    malformed["chain_proposal"]["hops"] = []
    counts = validate_explorer_candidates([malformed], reader, "", _RELEASE_FACTS)
    assert counts["unverified"] == 1
    assert "无法回查" in malformed["validation"]["notes"]


def test_custom_sink_flag_default_false(tmp_path: Path) -> None:
    """N-5（评审 R-7）：D2 边界——custom_sink_proposal 保守 false 且不影响档位。"""
    reader = _reader(tmp_path)
    ids = _method_ids(reader)
    a = ids[("com.example.A", "entry")]
    ab_to, ab_line = _call_site(reader, a)
    # 未知 sink 名（未命中任何已知 taxonomy——按 D2 不标记）
    candidate = _candidate([_hop(a, ab_to, ab_line)])
    candidate["chain_proposal"]["sink"] = "TotallyUnknown.exoticSink(value)"
    validate_explorer_candidates([candidate], reader, "", _RELEASE_FACTS)
    assert candidate["validation"]["status"] == "validated"
    assert candidate["validation"]["custom_sink_proposal"] is False


# ---------------------------------------------------------------------------
# A-7：集成（阶段 summary + candidates.json 含 validation）
# ---------------------------------------------------------------------------


def _apk_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", "<manifest/>")
        archive.writestr("classes.dex", b"dex\n035\x00")
    return buffer.getvalue()


def test_orchestrator_stage_summary(tmp_path: Path) -> None:
    """explorer 阶段集成：summary 含 validation_counts；零候选（AI 不可用）也输出计数。"""
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
        stage = next(s for s in run_manifest.get("stages", []) if s["name"] == "explorer")
        assert "validation_counts" in stage["summary"]
        assert stage["summary"]["validation_counts"] == {
            "validated": 0, "partially_validated": 0, "unverified": 0,
        }
        # candidates.json 存在（空数组——零候选场景）
        candidates = json.loads(
            (client.app.state.storage.run_dir(run_id) / "explorer" / "candidates.json").read_text("utf-8")
        )
        assert candidates == []
