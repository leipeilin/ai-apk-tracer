from __future__ import annotations

import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

from app.analysis.ai_trace import AITraceStore
from app.config import StorageSettings
from app.runs.cleanup import CleanupService
from app.runs.storage import RunStorage
from app.shared.errors import ValidationError


def apk_bytes() -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("AndroidManifest.xml", "<manifest package='com.example' />")
        archive.writestr("classes.dex", b"dex\n035\x00")
    buffer.seek(0)
    return buffer


def create_run(storage: RunStorage) -> str:
    result = storage.ingest(apk_bytes(), "example.apk", "trace-test", {"source_analysis": {"enabled": False}})
    run_id = result["id"]
    run_dir = storage.run_dir(run_id)
    (run_dir / "findings" / "finding.json").write_text("{}", "utf-8")
    (run_dir / "reports" / "report.md").write_text("report", "utf-8")
    (run_dir / "reports" / "evidence" / "evidence.json").write_text("{}", "utf-8")
    return run_id


def test_prune_keeps_report_and_removes_apk(tmp_path: Path) -> None:
    storage = RunStorage(tmp_path / "data", StorageSettings())
    run_id = create_run(storage)
    result = CleanupService(storage).cleanup(run_id, "prune_intermediates")
    run_dir = storage.run_dir(run_id)
    assert result["status"] == "completed"
    assert not (run_dir / "input").exists()
    assert (run_dir / "findings" / "finding.json").exists()
    assert (run_dir / "reports" / "report.md").exists()


def test_full_delete_requires_confirmation(tmp_path: Path) -> None:
    storage = RunStorage(tmp_path / "data", StorageSettings())
    run_id = create_run(storage)
    with pytest.raises(ValidationError):
        CleanupService(storage).cleanup(run_id, "delete_run", False)
    assert storage.run_dir(run_id).exists()


def test_cleanup_rejects_symlink(tmp_path: Path) -> None:
    storage = RunStorage(tmp_path / "data", StorageSettings())
    run_id = create_run(storage)
    run_dir = storage.run_dir(run_id)
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_dir / "tmp" / "escape").symlink_to(outside, target_is_directory=True)
    result = CleanupService(storage).cleanup(run_id, "prune_intermediates")
    assert result["status"] == "partial"
    assert outside.exists()


def test_trace_jsonl_and_checkpoint_are_private_and_tamper_safe(tmp_path: Path) -> None:
    trace = AITraceStore(tmp_path / "ai-trace")
    path = trace.append("candidate-1", "l2_review", {
        "event": "round",
        "candidate_index": 0,
        "round": 0,
    })
    input_key = "a" * 64
    candidate = {"candidate_id": "candidate-1", "analysis_status": "ai_completed"}
    result = {"status": "completed", "trace": []}
    trace.save_completed(0, input_key, candidate, result)

    line = json.loads(path.read_text("utf-8").strip())
    restored = trace.completed(0, input_key)
    assert line["event"] == "round"
    assert line["schema_version"] == "1"
    assert restored is not None
    assert restored["candidate"] == candidate
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(trace.checkpoint_path.stat().st_mode) == 0o600

    checkpoint = json.loads(trace.checkpoint_path.read_text("utf-8"))
    checkpoint["completed"]["0"]["result"]["status"] = "failed"
    trace.checkpoint_path.write_text(json.dumps(checkpoint), "utf-8")
    assert trace.completed(0, input_key) is None


def test_prune_removes_run_trace_but_retains_shared_cache(tmp_path: Path) -> None:
    storage = RunStorage(tmp_path / "data", StorageSettings())
    run_id = create_run(storage)
    run_trace = AITraceStore(storage.run_dir(run_id) / "ai-trace")
    run_trace.append("candidate-1", "l2_review", {"event": "round"})
    shared_entry = storage.shared_ai_cache_dir() / "sentinel"
    shared_entry.write_text("cached", "utf-8")
    shared_entry.chmod(0o600)

    result = CleanupService(storage).cleanup(run_id, "prune_intermediates")

    assert result["status"] == "completed"
    assert not (storage.run_dir(run_id) / "ai-trace").exists()
    assert shared_entry.read_text("utf-8") == "cached"


def test_shared_cache_requires_explicit_manual_confirmation(tmp_path: Path) -> None:
    storage = RunStorage(tmp_path / "data", StorageSettings())
    entry = storage.shared_ai_cache_dir() / "sentinel"
    entry.write_text("cached", "utf-8")
    service = CleanupService(storage)

    with pytest.raises(ValidationError, match="confirm=true"):
        service.clear_shared_ai_cache()
    assert entry.exists()

    result = service.clear_shared_ai_cache(confirm=True)
    assert result["status"] == "completed"
    assert storage.shared_ai_cache_dir().is_dir()
    assert not entry.exists()


def test_delete_run_syncs_database_record(tmp_path: Path) -> None:
    """v2026-08-09：CleanupService.cleanup("delete_run") 必须同步删除 SQLite 记录，
    否则磁盘已删但数据库残留孤儿，前端 /api/runs 数量不刷新。

    此前 Bug：CleanupService 不持有 repository，仅删除磁盘目录，导致 routes.py
    必须散落调用 repository.delete_run_record；直接用 CleanupService 绕过 API
    时即触发不一致。修复后 CleanupService 接受可选 repository 参数，集成同步。
    """
    from app.shared.repository import SQLiteRepository

    data_root = tmp_path / "data"
    storage = RunStorage(data_root, StorageSettings())
    repo = SQLiteRepository(data_root / "tracer.sqlite3")
    repo.initialize()
    run_id = create_run(storage)
    repo.create_run({
        "id": run_id, "trace_id": "trace-test",
        "status": "completed", "stage": "rule_prescan",
        "apk_filename": "example.apk", "apk_sha256": "x" * 64,
        "config": {}, "manifest_path": "AndroidManifest.xml",
    })

    # 注入 repository 后 delete_run 必须同时清理数据库
    result = CleanupService(storage, repo).cleanup(run_id, "delete_run", confirm_delete=True)
    assert result["status"] == "completed"
    assert "db_record" in result["deleted"]

    # 磁盘 + 数据库双重清理，前端下次拉 /api/runs 不会看到孤儿
    assert not (data_root / "runs" / run_id).exists()
    with pytest.raises(Exception):  # NotFoundError
        repo.get_run(run_id)


def test_delete_run_without_repository_only_removes_disk(tmp_path: Path) -> None:
    """v2026-08-09：向后兼容——CleanupService 不传 repository 时仍能工作（仅磁盘清理），
    适用于无数据库场景（如 standalone 工具调用）。"""
    data_root = tmp_path / "data"
    storage = RunStorage(data_root, StorageSettings())
    run_id = create_run(storage)

    result = CleanupService(storage).cleanup(run_id, "delete_run", confirm_delete=True)
    assert result["status"] == "completed"
    assert "db_record" not in result["deleted"]
    assert not (data_root / "runs" / run_id).exists()
