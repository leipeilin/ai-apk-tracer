from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.analysis.ai_recovery import (
    AIRecoveryStore,
    AIWorkCandidateRecord,
    AIWorkManifest,
    CANDIDATES_SUBDIR,
    PREFLIGHT_FILENAME,
    SanitizedPassedPreflightResult,
    WORK_MANIFEST_FILENAME,
    canonical_json_bytes,
    canonical_json_hash,
)


_DIGESTS = {letter: letter * 64 for letter in "abcdef"}


def _candidate(index: int = 1, **overrides: object) -> AIWorkCandidateRecord:
    values: dict[str, object] = {
        "candidate_id": f"candidate-{index}",
        "scope_key": _DIGESTS["a"],
        "chain_key": _DIGESTS["b"],
        "deterministic_fact_hash": _DIGESTS["c"],
        "slice_id": f"slice-{index}",
        "slice_hash": _DIGESTS["d"],
    }
    values.update(overrides)
    return AIWorkCandidateRecord.model_validate(values)


def _manifest(*candidates: AIWorkCandidateRecord, **overrides: object) -> AIWorkManifest:
    now = datetime.now(timezone.utc)
    values: dict[str, object] = {
        "attempt_id": "attempt-1",
        "run_id": "run-1",
        "analyzer": "openai-compatible-analyzer-v1",
        "provider": "openai-compatible",
        "model": "unit-test-model",
        "config_fingerprint": _DIGESTS["e"],
        "candidates": candidates or (_candidate(),),
        "created_at": now,
        "updated_at": now,
        "state": "in_progress",
    }
    values.update(overrides)
    return AIWorkManifest.model_validate(values)


def _preflight_result() -> SanitizedPassedPreflightResult:
    return SanitizedPassedPreflightResult(
        status="passed",
        classification="configured",
        recoverable=False,
        circuit_breaking=False,
        http_status=200,
        attempts=1,
        response_hash=_DIGESTS["f"],
    )


def _completion_result(label: str = "complete") -> dict[str, object]:
    return {
        "analysis_status": "ai_completed",
        "summary": label,
        "verdict": "unresolved",
        "analysis_complete": True,
        "evidence_refs": [],
    }


def test_round_trip_manifest_preflight_and_candidate_completion(tmp_path: Path) -> None:
    store = AIRecoveryStore(tmp_path / "ai-cache")
    manifest = _manifest()

    assert store.initialize_work_manifest(manifest).written
    assert store.save_passed_preflight(
        attempt_id=manifest.attempt_id,
        config_fingerprint=manifest.config_fingerprint,
        result=_preflight_result(),
    ).written
    assert store.save_candidate_completion(
        attempt_id=manifest.attempt_id,
        run_id=manifest.run_id,
        candidate=manifest.candidates[0],
        final_result=_completion_result(),
    ).written

    assert store.load_work_manifest(manifest) == manifest
    preflight = store.load_passed_preflight(
        attempt_id=manifest.attempt_id,
        config_fingerprint=manifest.config_fingerprint,
    )
    completion = store.load_candidate_completion(
        attempt_id=manifest.attempt_id,
        run_id=manifest.run_id,
        candidate=manifest.candidates[0],
    )
    assert preflight is not None and preflight.result.status == "passed"
    assert completion is not None and completion.final_result["summary"] == "complete"


def test_manifest_requires_exact_attempt_run_config_and_candidate_order(tmp_path: Path) -> None:
    first, second = _candidate(1), _candidate(2, scope_key=_DIGESTS["f"])
    manifest = _manifest(first, second)
    store = AIRecoveryStore(tmp_path / "ai-cache")
    assert store.initialize_work_manifest(manifest).written

    for stale in (
        manifest.model_copy(update={"attempt_id": "attempt-2"}),
        manifest.model_copy(update={"run_id": "run-2"}),
        manifest.model_copy(update={"config_fingerprint": _DIGESTS["a"]}),
        manifest.model_copy(update={"candidates": (second, first)}),
    ):
        assert store.load_work_manifest(stale) is None
    assert store.load_work_manifest(manifest) is not None


def test_manifest_keyword_expectations_preserve_candidate_order(tmp_path: Path) -> None:
    manifest = _manifest(_candidate(2), _candidate(1))
    store = AIRecoveryStore(tmp_path / "ai-cache")
    assert store.initialize_work_manifest(manifest).written

    loaded = store.load_work_manifest(
        attempt_id=manifest.attempt_id,
        run_id=manifest.run_id,
        analyzer=manifest.analyzer,
        provider=manifest.provider,
        model=manifest.model,
        config_fingerprint=manifest.config_fingerprint,
        candidates=manifest.candidates,
    )
    assert loaded is not None
    assert [item.candidate_id for item in loaded.candidates] == ["candidate-2", "candidate-1"]


def test_preflight_attempt_and_config_mismatches_invalidate(tmp_path: Path) -> None:
    manifest = _manifest()
    store = AIRecoveryStore(tmp_path / "ai-cache")
    assert store.save_passed_preflight(
        attempt_id=manifest.attempt_id,
        config_fingerprint=manifest.config_fingerprint,
        result=_preflight_result(),
    ).written

    assert store.load_passed_preflight(
        attempt_id="attempt-2",
        config_fingerprint=manifest.config_fingerprint,
    ) is None
    assert store.load_passed_preflight(
        attempt_id=manifest.attempt_id,
        config_fingerprint=_DIGESTS["a"],
    ) is None


@pytest.mark.parametrize(
    "change",
    [
        {"candidate_id": "candidate-other"},
        {"scope_key": _DIGESTS["f"]},
        {"chain_key": _DIGESTS["f"]},
        {"deterministic_fact_hash": _DIGESTS["f"]},
        {"slice_hash": _DIGESTS["f"]},
    ],
)
def test_candidate_identity_and_slice_changes_invalidate(
    tmp_path: Path, change: dict[str, object]
) -> None:
    candidate = _candidate()
    store = AIRecoveryStore(tmp_path / "ai-cache")
    assert store.save_candidate_completion(
        attempt_id="attempt-1",
        run_id="run-1",
        candidate=candidate,
        final_result=_completion_result(),
    ).written

    assert store.load_candidate_completion(
        attempt_id="attempt-1",
        run_id="run-1",
        candidate=candidate.model_copy(update=change),
    ) is None


def test_candidate_attempt_and_run_changes_invalidate(tmp_path: Path) -> None:
    candidate = _candidate()
    store = AIRecoveryStore(tmp_path / "ai-cache")
    assert store.save_candidate_completion(
        attempt_id="attempt-1",
        run_id="run-1",
        candidate=candidate,
        final_result=_completion_result(),
    ).written

    assert store.load_candidate_completion(
        attempt_id="attempt-2", run_id="run-1", candidate=candidate
    ) is None
    assert store.load_candidate_completion(
        attempt_id="attempt-1", run_id="run-2", candidate=candidate
    ) is None


def test_corrupt_invalid_utf8_and_duplicate_json_are_misses(tmp_path: Path) -> None:
    manifest = _manifest()
    root = tmp_path / "ai-cache"
    store = AIRecoveryStore(root)
    assert store.initialize_work_manifest(manifest).written
    path = root / WORK_MANIFEST_FILENAME

    for corrupt in (b"{", b"\xff", b'{"schema_version":"1","schema_version":"1"}'):
        path.write_bytes(corrupt)
        assert store.load_work_manifest(manifest) is None


def test_oversize_and_non_regular_files_are_misses(tmp_path: Path) -> None:
    manifest = _manifest()
    root = tmp_path / "ai-cache"
    store = AIRecoveryStore(root)
    assert store.initialize_work_manifest(manifest).written
    path = root / WORK_MANIFEST_FILENAME
    path.write_bytes(b"x" * 129)
    assert AIRecoveryStore(root, max_entry_bytes=128).load_work_manifest(manifest) is None

    path.unlink()
    path.mkdir()
    assert store.load_work_manifest(manifest) is None


def test_symlink_root_checkpoint_and_destination_are_refused(tmp_path: Path) -> None:
    manifest = _manifest()
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "ai-cache"
    linked_root.symlink_to(real_root, target_is_directory=True)
    linked_store = AIRecoveryStore(linked_root)
    assert linked_store.load_work_manifest(manifest) is None
    assert linked_store.initialize_work_manifest(manifest).written is False

    safe_root = tmp_path / "safe"
    safe_store = AIRecoveryStore(safe_root)
    assert safe_store.initialize_work_manifest(manifest).written
    outside = tmp_path / "outside.json"
    outside.write_text("unchanged", encoding="utf-8")
    destination = safe_root / WORK_MANIFEST_FILENAME
    destination.unlink()
    destination.symlink_to(outside)
    assert safe_store.load_work_manifest(manifest) is None
    assert safe_store.initialize_work_manifest(manifest).written is False
    assert outside.read_text("utf-8") == "unchanged"


def test_atomic_writes_use_private_modes_and_leave_no_temps(tmp_path: Path) -> None:
    root = tmp_path / "ai-cache"
    store = AIRecoveryStore(root)
    manifest = _manifest()
    assert store.initialize_work_manifest(manifest).written
    assert store.initialize_work_manifest(manifest.model_copy(update={"state": "completed"})).written
    assert store.save_candidate_completion(
        attempt_id=manifest.attempt_id,
        run_id=manifest.run_id,
        candidate=manifest.candidates[0],
        final_result=_completion_result(),
    ).written

    manifest_path = root / WORK_MANIFEST_FILENAME
    candidate_path = root / CANDIDATES_SUBDIR / "candidate-1.json"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / CANDIDATES_SUBDIR).stat().st_mode) == 0o700
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(candidate_path.stat().st_mode) == 0o600
    assert list(root.rglob("*.tmp")) == []


def test_canonical_hashes_are_stable_and_hash_tampering_is_rejected(tmp_path: Path) -> None:
    assert canonical_json_hash({"b": 2, "a": [1, "é"]}) == canonical_json_hash(
        {"a": [1, "é"], "b": 2}
    )

    manifest = _manifest()
    root = tmp_path / "ai-cache"
    store = AIRecoveryStore(root)
    assert store.save_passed_preflight(
        attempt_id=manifest.attempt_id,
        config_fingerprint=manifest.config_fingerprint,
        result=_preflight_result(),
    ).written
    path = root / PREFLIGHT_FILENAME
    document = json.loads(path.read_text("utf-8"))
    document["result"]["attempts"] = 2
    path.write_bytes(canonical_json_bytes(document))
    assert store.load_passed_preflight(
        attempt_id=manifest.attempt_id,
        config_fingerprint=manifest.config_fingerprint,
    ) is None


def test_future_and_explicitly_stale_checkpoints_are_misses(tmp_path: Path) -> None:
    manifest = _manifest()
    root = tmp_path / "ai-cache"
    store = AIRecoveryStore(root)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert store.save_passed_preflight(
        attempt_id=manifest.attempt_id,
        config_fingerprint=manifest.config_fingerprint,
        result=_preflight_result(),
        now=future,
    ).written
    assert store.load_passed_preflight(
        attempt_id=manifest.attempt_id,
        config_fingerprint=manifest.config_fingerprint,
    ) is None

    now = datetime.now(timezone.utc)
    assert store.save_candidate_completion(
        attempt_id=manifest.attempt_id,
        run_id=manifest.run_id,
        candidate=manifest.candidates[0],
        final_result=_completion_result(),
        now=now,
    ).written
    assert store.load_candidate_completion(
        attempt_id=manifest.attempt_id,
        run_id=manifest.run_id,
        candidate=manifest.candidates[0],
        not_before=now + timedelta(seconds=1),
    ) is None


def test_sensitive_payload_fields_are_rejected_and_never_persisted(tmp_path: Path) -> None:
    root = tmp_path / "ai-cache"
    store = AIRecoveryStore(root)
    candidate = _candidate()
    secret = "unit-test-api-secret"

    result = store.save_candidate_completion(
        attempt_id="attempt-1",
        run_id="run-1",
        candidate=candidate,
        final_result={
            "analysis_status": "ai_completed",
            "Authorization": f"Bearer {secret}",
            "rendered_prompt": "private prompt",
            "raw_request_body": {"source_code": "private code"},
        },
    )
    assert result.written is False
    assert not (root / CANDIDATES_SUBDIR / "candidate-1.json").exists()

    assert store.save_candidate_completion(
        attempt_id="attempt-1",
        run_id="run-1",
        candidate=candidate,
        final_result=_completion_result(),
    ).written
    serialized = (root / CANDIDATES_SUBDIR / "candidate-1.json").read_text("utf-8")
    for forbidden in (
        secret,
        "Authorization",
        "api_key",
        "rendered_prompt",
        "raw_request_body",
        "source_code",
        "context_slice",
    ):
        assert forbidden not in serialized


def test_partial_completion_listing_is_valid_and_manifest_ordered(tmp_path: Path) -> None:
    candidates = (_candidate(3), _candidate(1), _candidate(2))
    manifest = _manifest(*candidates)
    store = AIRecoveryStore(tmp_path / "ai-cache")
    for candidate in (candidates[0], candidates[2]):
        assert store.save_candidate_completion(
            attempt_id=manifest.attempt_id,
            run_id=manifest.run_id,
            candidate=candidate,
            final_result=_completion_result(candidate.candidate_id),
        ).written

    completed = store.list_candidate_completions(manifest)
    assert [item.candidate_id for item in completed] == ["candidate-3", "candidate-2"]


def test_partial_listing_omits_corrupt_and_identity_mismatched_files(tmp_path: Path) -> None:
    first, second = _candidate(1), _candidate(2)
    manifest = _manifest(first, second)
    root = tmp_path / "ai-cache"
    store = AIRecoveryStore(root)
    for candidate in manifest.candidates:
        assert store.save_candidate_completion(
            attempt_id=manifest.attempt_id,
            run_id=manifest.run_id,
            candidate=candidate,
            final_result=_completion_result(),
        ).written
    (root / CANDIDATES_SUBDIR / "candidate-1.json").write_text("{", encoding="utf-8")
    path = root / CANDIDATES_SUBDIR / "candidate-2.json"
    document = json.loads(path.read_text("utf-8"))
    document["slice_hash"] = _DIGESTS["f"]
    path.write_bytes(canonical_json_bytes(document))

    assert store.list_candidate_completions(manifest) == []


def test_models_are_strict_forbid_extras_and_constrain_path_identifiers() -> None:
    values = _candidate().model_dump(mode="json")
    values["raw_prompt"] = "must not be accepted"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AIWorkCandidateRecord.model_validate(values)
    with pytest.raises(ValidationError):
        _candidate(candidate_id="../escape")
    with pytest.raises(ValidationError):
        AIWorkManifest.model_validate(
            {
                **_manifest().model_dump(mode="python"),
                "state": 1,
            }
        )


def test_write_validation_failures_are_results_not_exceptions(tmp_path: Path) -> None:
    store = AIRecoveryStore(tmp_path / "ai-cache")
    result = store.save_passed_preflight(
        attempt_id="../escape",
        config_fingerprint="not-a-hash",
        result={"status": "passed", "unexpected": True},
    )
    assert result.written is False
    assert result.error


def test_repeated_checkpoint_save_preserves_creation_time(tmp_path: Path) -> None:
    store = AIRecoveryStore(tmp_path / "ai-cache")
    candidate = _candidate()
    first_time = datetime.now(timezone.utc) - timedelta(minutes=2)
    second_time = datetime.now(timezone.utc)
    assert store.save_candidate_completion(
        attempt_id="attempt-1",
        run_id="run-1",
        candidate=candidate,
        final_result=_completion_result("first"),
        now=first_time,
    ).written
    assert store.save_candidate_completion(
        attempt_id="attempt-1",
        run_id="run-1",
        candidate=candidate,
        final_result=_completion_result("second"),
        now=second_time,
    ).written

    loaded = store.load_candidate_completion(
        attempt_id="attempt-1", run_id="run-1", candidate=candidate
    )
    assert loaded is not None
    assert loaded.created_at == first_time
    assert loaded.updated_at == second_time
    assert loaded.final_result["summary"] == "second"
