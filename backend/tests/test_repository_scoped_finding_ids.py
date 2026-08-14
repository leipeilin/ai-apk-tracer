from __future__ import annotations

from pathlib import Path

from app.shared.repository import SQLiteRepository


def _run(run_id: str) -> dict:
    return {
        "id": run_id,
        "trace_id": f"trace-{run_id}",
        "status": "completed",
        "stage": "completed",
        "apk_filename": "same.apk",
        "apk_sha256": "a" * 64,
        "config": {"source_analysis": {"enabled": True}},
        "manifest_path": f"/tmp/{run_id}/manifest.json",
    }


def _finding() -> dict:
    return {
        "id": "finding_same_semantic_hash",
        "rule_ids": ["TEST_RULE"],
        "title": "同一语义 Finding",
        "component": "activity",
        "component_name": "com.example.Demo",
        "severity": "pending",
        "confidence": "medium",
        "evidence_level": "L2",
    }


def test_same_finding_semantics_can_be_stored_in_multiple_runs(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "tracer.sqlite3")
    repository.initialize()
    repository.create_run(_run("run_one"))
    repository.create_run(_run("run_two"))
    first = _finding()
    second = _finding()

    repository.replace_findings("run_one", [first])
    repository.replace_findings("run_two", [second])

    assert first["base_id"] == second["base_id"] == "finding_same_semantic_hash"
    assert first["id"] == "run_one_finding_same_semantic_hash"
    assert second["id"] == "run_two_finding_same_semantic_hash"
    assert repository.list_findings("run_one")[0]["id"] == first["id"]
    assert repository.list_findings("run_two")[0]["id"] == second["id"]
