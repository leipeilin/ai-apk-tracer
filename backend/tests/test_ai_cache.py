from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.analysis.ai_cache import (
    AICacheStore,
    build_cache_descriptor,
    build_cache_key,
    canonical_json_bytes,
    canonical_json_hash,
    is_valid_cache_key,
)
from app.analysis.ai_models import (
    AICacheDescriptor,
    AICacheEntry,
    L1TriageOutput,
    SchemaSerialization,
)


_DIGESTS = {letter: letter * 64 for letter in "abcdef"}


def _descriptor(**overrides: object) -> AICacheDescriptor:
    values: dict[str, object] = {
        "provider_kind": "openai-compatible",
        "base_url": "https://provider.invalid/v1",
        "model": "unit-test-model",
        "analyzer_version": "2.1.0",
        "prompt_id": "l1-triage",
        "prompt_version": "1.0.0",
        "system_template_hash": _DIGESTS["a"],
        "user_template_hash": _DIGESTS["b"],
        "input_schema_hash": _DIGESTS["c"],
        "output_schema_hash": SchemaSerialization.sha256_for(L1TriageOutput),
        "model_input_hash": _DIGESTS["d"],
        "input_slice_hash": _DIGESTS["e"],
        "request_hash": _DIGESTS["f"],
        "output_model_name": "L1TriageOutput",
        "output_model_version": "1",
    }
    values.update(overrides)
    return build_cache_descriptor(**values)  # type: ignore[arg-type]


def _output(summary: str = "发现需要确定性验证的潜在线索。") -> dict[str, object]:
    return {
        "summary": summary,
        "triage_disposition": "potential_chain",
        "evidence_refs": [{
            "context_id": "ctx-1",
            "path": "Demo.java",
            "line": 1,
            "end_line": 1,
            "claim": "缓存测试证据",
        }],
        "analysis_complete": True,
    }


def _entry_path(cache_root: Path, descriptor: AICacheDescriptor) -> Path:
    return cache_root / "entries" / f"{build_cache_key(descriptor)}.json"


def _write_document(cache_root: Path, key: str, document: object) -> Path:
    entries = cache_root / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    path = entries / f"{key}.json"
    path.write_bytes(canonical_json_bytes(document))
    return path


def test_cache_key_is_deterministic_and_all_identity_inputs_invalidate() -> None:
    descriptor = _descriptor()
    assert build_cache_key(descriptor) == build_cache_key(_descriptor())

    variants = [
        _descriptor(provider_kind="other-compatible"),
        _descriptor(base_url="https://other.invalid/v1"),
        _descriptor(model="other-model"),
        _descriptor(analyzer_version="2.1.1"),
        _descriptor(prompt_id="l2-review"),
        _descriptor(prompt_version="1.0.1"),
        _descriptor(system_template_hash=_DIGESTS["b"]),
        _descriptor(user_template_hash=_DIGESTS["c"]),
        _descriptor(input_schema_hash=_DIGESTS["d"]),
        _descriptor(output_schema_hash=_DIGESTS["e"]),
        _descriptor(model_input_hash=_DIGESTS["e"]),
        _descriptor(input_slice_hash=None),
        _descriptor(request_hash=_DIGESTS["a"]),
        _descriptor(output_model_name="L2ReviewOutput"),
        _descriptor(output_model_version="2"),
        _descriptor(protocol_version="strict-json-v2"),
        _descriptor(analysis_track="l1_triage"),
        _descriptor(scope_hash=_DIGESTS["a"]),
        _descriptor(fact_hash=_DIGESTS["b"]),
        _descriptor(context_hash=_DIGESTS["c"]),
        _descriptor(prompt_hash=_DIGESTS["d"]),
        _descriptor(schema_hash=_DIGESTS["e"]),
        _descriptor(temperature=0.0),
        _descriptor(max_output_tokens=3000),
        _descriptor(budget_policy_hash=_DIGESTS["f"]),
    ]

    original_key = build_cache_key(descriptor)
    assert len({build_cache_key(item) for item in variants} | {original_key}) == len(variants) + 1


@pytest.mark.parametrize(
    "key",
    ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "../" + "a" * 64, 123],
)
def test_cache_key_validation_requires_lowercase_sha256(key: object) -> None:
    assert is_valid_cache_key(key) is False
    assert is_valid_cache_key("a" * 64) is True


def test_valid_hit_is_revalidated_with_current_output_model(tmp_path: Path) -> None:
    descriptor = _descriptor()
    store = AICacheStore(tmp_path / "ai-cache")

    result = store.save(descriptor, _output())
    loaded = store.load(descriptor)

    assert result.written is True
    assert isinstance(loaded, L1TriageOutput)
    assert loaded.analysis_complete is True
    assert loaded.suggested_sources == []


def test_cache_store_rejects_completed_output_without_evidence_refs_on_save_and_load(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor()
    store = AICacheStore(tmp_path / "ai-cache")
    empty_refs = {**_output(), "evidence_refs": []}

    assert store.save(descriptor, empty_refs).written is False

    accepted = L1TriageOutput.model_validate(empty_refs).model_dump(mode="json")
    now = datetime.now(timezone.utc)
    entry = AICacheEntry(
        schema_version="1",
        descriptor=descriptor,
        accepted_output=accepted,
        accepted_output_hash=canonical_json_hash(accepted),
        created_at=now,
        updated_at=now,
    )
    _write_document(
        tmp_path / "ai-cache",
        build_cache_key(descriptor),
        entry.model_dump(mode="json"),
    )

    assert store.load(descriptor) is None


def test_output_model_version_mismatch_is_a_miss(tmp_path: Path) -> None:
    cache_root = tmp_path / "ai-cache"
    descriptor = _descriptor()
    store = AICacheStore(cache_root)
    assert store.save(descriptor, _output()).written
    document = json.loads(_entry_path(cache_root, descriptor).read_text("utf-8"))
    document["descriptor"]["output_model_version"] = "999"
    stale_descriptor = AICacheDescriptor.model_validate(document["descriptor"])
    _write_document(cache_root, build_cache_key(stale_descriptor), document)

    assert store.load(stale_descriptor) is None


def test_corrupt_invalid_utf8_and_duplicate_json_are_misses(tmp_path: Path) -> None:
    descriptor = _descriptor()
    key = build_cache_key(descriptor)
    cache_root = tmp_path / "ai-cache"
    entries = cache_root / "entries"
    entries.mkdir(parents=True)
    path = entries / f"{key}.json"
    store = AICacheStore(cache_root)

    for corrupt in (b"{", b"\xff", b'{"schema_version":"1","schema_version":"1"}'):
        path.write_bytes(corrupt)
        assert store.load(descriptor) is None


def test_oversize_entry_is_a_miss(tmp_path: Path) -> None:
    descriptor = _descriptor()
    cache_root = tmp_path / "ai-cache"
    path = _write_document(cache_root, build_cache_key(descriptor), {"padding": "x" * 256})
    assert path.stat().st_size > 64

    assert AICacheStore(cache_root, max_entry_bytes=64).load(descriptor) is None


def test_symlink_root_and_entry_are_refused_without_raising(tmp_path: Path) -> None:
    descriptor = _descriptor()
    real_root = tmp_path / "real-cache"
    real_root.mkdir()
    linked_root = tmp_path / "ai-cache"
    linked_root.symlink_to(real_root, target_is_directory=True)
    linked_store = AICacheStore(linked_root)

    assert linked_store.load(descriptor) is None
    assert linked_store.save(descriptor, _output()).written is False

    cache_root = tmp_path / "safe-cache"
    entries = cache_root / "entries"
    entries.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (entries / f"{build_cache_key(descriptor)}.json").symlink_to(outside)
    assert AICacheStore(cache_root).load(descriptor) is None


def test_output_hash_mismatch_is_a_miss(tmp_path: Path) -> None:
    cache_root = tmp_path / "ai-cache"
    descriptor = _descriptor()
    store = AICacheStore(cache_root)
    assert store.save(descriptor, _output()).written
    path = _entry_path(cache_root, descriptor)
    document = json.loads(path.read_text("utf-8"))
    document["accepted_output"]["summary"] = "内容已被替换。"
    path.write_bytes(canonical_json_bytes(document))

    assert store.load(descriptor) is None


def test_descriptor_and_file_key_mismatch_is_a_miss(tmp_path: Path) -> None:
    cache_root = tmp_path / "ai-cache"
    descriptor = _descriptor()
    other_descriptor = _descriptor(model="other-model")
    store = AICacheStore(cache_root)
    assert store.save(descriptor, _output()).written
    original = json.loads(_entry_path(cache_root, descriptor).read_text("utf-8"))
    _write_document(cache_root, build_cache_key(other_descriptor), original)

    assert store.load(other_descriptor) is None
    assert store.load(descriptor, key=build_cache_key(other_descriptor)) is None


def test_entry_and_current_output_schema_mismatches_are_misses(tmp_path: Path) -> None:
    cache_root = tmp_path / "ai-cache"
    descriptor = _descriptor()
    store = AICacheStore(cache_root)
    assert store.save(descriptor, _output()).written
    path = _entry_path(cache_root, descriptor)
    document = json.loads(path.read_text("utf-8"))
    document["schema_version"] = "2"
    path.write_bytes(canonical_json_bytes(document))
    assert store.load(descriptor) is None

    stale_descriptor = _descriptor(output_schema_hash=_DIGESTS["a"])
    accepted = L1TriageOutput.model_validate(_output()).model_dump(mode="json")
    now = datetime.now(timezone.utc)
    stale_entry = AICacheEntry(
        schema_version="1",
        descriptor=stale_descriptor,
        accepted_output=accepted,
        accepted_output_hash=canonical_json_hash(accepted),
        created_at=now,
        updated_at=now,
    )
    _write_document(
        cache_root,
        build_cache_key(stale_descriptor),
        stale_entry.model_dump(mode="json"),
    )
    assert store.load(stale_descriptor) is None


def test_revalidated_output_rejects_self_consistent_extra_fields(tmp_path: Path) -> None:
    cache_root = tmp_path / "ai-cache"
    descriptor = _descriptor()
    accepted = L1TriageOutput.model_validate(_output()).model_dump(mode="json")
    accepted["unexpected"] = "not accepted by the current strict model"
    now = datetime.now(timezone.utc)
    entry = AICacheEntry(
        schema_version="1",
        descriptor=descriptor,
        accepted_output=accepted,
        accepted_output_hash=canonical_json_hash(accepted),
        created_at=now,
        updated_at=now,
    )
    _write_document(cache_root, build_cache_key(descriptor), entry.model_dump(mode="json"))

    assert AICacheStore(cache_root).load(descriptor) is None


def test_atomic_replacement_uses_private_mode_and_leaves_no_temp_files(tmp_path: Path) -> None:
    cache_root = tmp_path / "ai-cache"
    descriptor = _descriptor()
    store = AICacheStore(cache_root)
    first = store.save(descriptor, _output("第一次接受的输出。"))
    second = store.save(descriptor, _output("第二次接受的输出。"))
    path = _entry_path(cache_root, descriptor)

    assert first.written is True
    assert second.written is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load(descriptor).summary == "第二次接受的输出。"  # type: ignore[union-attr]
    assert [item for item in path.parent.iterdir() if item.suffix == ".tmp"] == []


def test_symlink_destination_write_failure_is_reported_and_target_unchanged(tmp_path: Path) -> None:
    descriptor = _descriptor()
    cache_root = tmp_path / "ai-cache"
    entries = cache_root / "entries"
    entries.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("unchanged", encoding="utf-8")
    destination = entries / f"{build_cache_key(descriptor)}.json"
    destination.symlink_to(outside)

    result = AICacheStore(cache_root).save(descriptor, _output())

    assert result.written is False
    assert result.error
    assert outside.read_text("utf-8") == "unchanged"


def test_persisted_entry_contains_only_hash_metadata_and_strict_output(tmp_path: Path) -> None:
    raw_url = "https://secret-host.invalid/v1?token=do-not-store"
    descriptor = _descriptor(base_url=raw_url)
    cache_root = tmp_path / "ai-cache"
    result = AICacheStore(cache_root).save(descriptor, _output())
    document = json.loads(_entry_path(cache_root, descriptor).read_text("utf-8"))
    serialized = json.dumps(document, ensure_ascii=False, sort_keys=True)

    assert result.written is True
    assert set(document) == {
        "schema_version",
        "descriptor",
        "accepted_output",
        "accepted_output_hash",
        "created_at",
        "updated_at",
    }
    assert raw_url not in serialized
    assert "do-not-store" not in serialized
    for forbidden in ("api_key", "headers", "messages", "rendered_prompt", "raw_request", "raw_response", "context_slice"):
        assert forbidden not in serialized


def test_cache_models_forbid_extra_fields_and_partial_output_identity() -> None:
    descriptor = _descriptor().model_dump(mode="json")
    descriptor["base_url"] = "https://must-not-be-stored.invalid"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AICacheDescriptor.model_validate(descriptor)

    descriptor.pop("base_url")
    descriptor["output_model_version"] = None
    with pytest.raises(ValidationError, match="simultaneously|同时"):
        AICacheDescriptor.model_validate(descriptor)
