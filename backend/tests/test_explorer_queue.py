"""探索候选人工队列测试（T2.10，验收 A-1~A-10、N-1~N-4）。"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.analysis.explorer_queue import build_explorer_queue
from app.config import Settings, SourceAnalysisSettings, StorageSettings
from app.main import create_app


def _candidate(
    status: str, *, confidence: str = "medium", verified: int | None = 1,
    hops: int = 2, deep_dive: dict | None = None,
    candidate_id: str = "expl_x",
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0", "candidate_id": candidate_id,
        "source": "explorer_agent", "prompt_version": "explorer/1.0.0", "model": "m",
        "component": {"kind": "activity", "name": "com.example.A", "exported": True,
                      "entry_method": "entry"},
        "api_entry_ref": "act_a",
        "chain_proposal": {
            "source": "A.entry(input)", "sink": "C.write(value)",
            "hops": [{"from_method_id": f"m{i}", "to_method_id": f"m{i+1}",
                      "call_site_line": i + 1, "resolved_via": "direct_call"}
                     for i in range(hops)],
            "confidence": confidence, "hypothesis": "likely",
            "impact_proposal": "i", "reasoning": "r", "evidence_refs": [],
        },
        "validation": {
            "status": status, "verified_hop_count": verified,
            "failed_hop_indices": [], "blocked_by_guard": False,
            "custom_sink_proposal": False, "notes": f"{status} notes",
        },
        "deep_dive": deep_dive,
    }


def _deep_dive(evidence_count: int, *, confirmed: int = 0) -> dict[str, Any]:
    return {
        "status": "completed", "prompt_version": "explorer-deep-dive/1.0.0",
        "model": "m", "requests_used": 1,
        "resolved_facts": [{"index": i, "conclusion": "confirmed", "reasoning": "r", "evidence": []}
                           for i in range(confirmed)],
        "evidence_refs": [{"path": "A.java", "line": i + 1} for i in range(evidence_count)],
        "remaining_gaps": [], "unverifiable_evidence_count": 0,
        "evidence_truncated_count": 0, "rounds": [],
    }


# ---------------------------------------------------------------------------
# 队列构建（A-1~A-6）
# ---------------------------------------------------------------------------


def test_queue_entry_shape() -> None:
    """A-1：投影形状（脱 hops 全文与轮审计）。"""

    queue = build_explorer_queue([
        _candidate("partially_validated", deep_dive=_deep_dive(2, confirmed=1),
                   candidate_id="expl_p"),
        _candidate("validated", candidate_id="expl_v"),
    ])
    entry = queue["entries"][0]
    assert set(entry) == {
        "candidate_id", "component", "chain", "validation", "deep_dive",
        "confidence", "sort_keys",
    }
    assert entry["chain"]["hop_count"] == 2  # hops 长度派生（R-7）
    assert "hops" not in entry and "rounds" not in json.dumps(entry)  # 脱全量
    assert entry["deep_dive"]["evidence_count"] == 2
    assert entry["deep_dive"]["confirmed_fact_count"] == 1


def test_queue_sorting_confidence_primary() -> None:
    """A-2（评审 R-1 排序）：置信度主键 → deep_dive 证据次键。

    高置信无深挖 > 中置信多证据 > 中置信少证据 > 低置信——
    unverified 不因无深挖证据系统性沉底。
    """

    queue = build_explorer_queue([
        _candidate("unverified", confidence="low", candidate_id="expl_low"),
        _candidate("unverified", confidence="high", candidate_id="expl_high"),
        _candidate("partially_validated", confidence="medium",
                   deep_dive=_deep_dive(3), candidate_id="expl_mid3"),
        _candidate("partially_validated", confidence="medium",
                   deep_dive=_deep_dive(1), candidate_id="expl_mid1"),
    ])
    order = [entry["candidate_id"] for entry in queue["entries"]]
    assert order == ["expl_high", "expl_mid3", "expl_mid1", "expl_low"]


def test_queue_sorting_hop_ratio() -> None:
    """A-3：同置信度同证据数 → 跳完整度次级。"""

    queue = build_explorer_queue([
        _candidate("unverified", confidence="high", verified=1, hops=2,
                   candidate_id="expl_half"),
        _candidate("unverified", confidence="high", verified=2, hops=2,
                   candidate_id="expl_full"),
    ])
    assert [e["candidate_id"] for e in queue["entries"]] == ["expl_full", "expl_half"]


def test_queue_counts_and_validated_excluded() -> None:
    """A-5/R-9：计数含 validated（对照）；列表主体排除 validated；queue_length。"""

    queue = build_explorer_queue([
        _candidate("validated", candidate_id="expl_v"),
        _candidate("partially_validated", candidate_id="expl_p"),
        _candidate("unverified", candidate_id="expl_u"),
        _candidate("pending", candidate_id="expl_n"),
    ])
    counts = queue["counts"]
    assert counts == {
        "validated": 1, "partially_validated": 1, "unverified": 1, "pending": 1,
        "total": 4, "queue_length": 3, "deep_dive_completed": 0,
    }
    statuses = [entry["validation"]["status"] for entry in queue["entries"]]
    assert "validated" not in statuses


def test_queue_empty_and_malformed() -> None:
    """A-6/N-2/N-3：空输入/畸形条目/未知置信度容错。"""

    assert build_explorer_queue([]) == {
        "entries": [],
        "counts": {"validated": 0, "partially_validated": 0, "unverified": 0,
                   "pending": 0, "total": 0, "queue_length": 0, "deep_dive_completed": 0},
    }
    queue = build_explorer_queue(["not-a-mapping", {"candidate_id": "x"}])
    assert queue["counts"]["total"] == 1  # 非 mapping 跳过；缺 validation 记 pending
    assert queue["entries"][0]["sort_keys"]["confidence_rank"] == 0  # 未知置信度


# ---------------------------------------------------------------------------
# API 端点（A-7~A-10、N-1/N-4）
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> tuple[TestClient, str]:
    settings = Settings(
        database_path=tmp_path / "tracer.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "data"),
        source_analysis=SourceAnalysisSettings(enabled=False),
    )
    client = TestClient(create_app(settings))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", "<manifest/>")
        archive.writestr("classes.dex", b"dex\n035\x00")
    response = client.post(
        "/api/runs",
        files={"file": ("sample.apk", buffer.getvalue(), "application/vnd.android.package-archive")},
        data={"authorized": "true", "source_analysis_enabled": "false"},
    )
    assert response.status_code == 202
    return client, response.json()["id"]


def test_endpoint_empty_state(tmp_path: Path) -> None:
    """A-7/N-4：无探索产物 → 空态 200。"""

    client, run_id = _client(tmp_path)
    response = client.get(f"/api/runs/{run_id}/explorer/candidates")
    assert response.status_code == 200
    payload = response.json()
    assert payload["entries"] == []
    assert payload["counts"]["total"] == 0


def test_endpoint_returns_sorted_queue(tmp_path: Path) -> None:
    """A-8：预置 candidates.json → 排序队列 + counts。"""

    client, run_id = _client(tmp_path)
    run_dir = client.app.state.storage.run_dir(run_id)
    (run_dir / "explorer").mkdir(parents=True)
    (run_dir / "explorer" / "candidates.json").write_text(json.dumps([
        _candidate("unverified", confidence="low", candidate_id="expl_low"),
        _candidate("partially_validated", confidence="high",
                   deep_dive=_deep_dive(2), candidate_id="expl_high"),
        _candidate("validated", candidate_id="expl_v"),
    ], ensure_ascii=False), "utf-8")

    response = client.get(f"/api/runs/{run_id}/explorer/candidates")
    assert response.status_code == 200
    payload = response.json()
    assert [e["candidate_id"] for e in payload["entries"]] == ["expl_high", "expl_low"]
    assert payload["counts"]["validated"] == 1
    assert payload["counts"]["queue_length"] == 2


def test_endpoint_corrupted_file_empty_state(tmp_path: Path) -> None:
    """N-1：candidates.json 损坏 → 空态 200。"""

    client, run_id = _client(tmp_path)
    run_dir = client.app.state.storage.run_dir(run_id)
    (run_dir / "explorer").mkdir(parents=True)
    (run_dir / "explorer" / "candidates.json").write_text("not-json{", "utf-8")
    response = client.get(f"/api/runs/{run_id}/explorer/candidates")
    assert response.status_code == 200
    assert response.json()["entries"] == []


def test_endpoint_404(tmp_path: Path) -> None:
    """A-9：不存在的 run → 404。"""

    client, _ = _client(tmp_path)
    response = client.get("/api/runs/20260822T000000Z_nope_nope/explorer/candidates")
    assert response.status_code == 404
