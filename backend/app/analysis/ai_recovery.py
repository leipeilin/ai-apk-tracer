"""Task-local persistence for resumable AI analysis work."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)


DEFAULT_MAX_RECOVERY_BYTES = 2 * 1024 * 1024
WORK_MANIFEST_FILENAME = "work-manifest.json"
PREFLIGHT_FILENAME = "passed-preflight.json"
CANDIDATES_SUBDIR = "candidate-completions"

PathIdentifier = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
BoundedText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=256),
]
Sha256 = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
]


class _RecoverySafetyError(ValueError):
    pass


class StrictRecoveryModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_default=True,
        revalidate_instances="always",
    )


class AIWorkCandidateRecord(StrictRecoveryModel):
    candidate_id: PathIdentifier
    scope_key: Sha256
    chain_key: Sha256
    deterministic_fact_hash: Sha256
    slice_id: PathIdentifier
    slice_hash: Sha256


class AIWorkManifest(StrictRecoveryModel):
    schema_version: Literal["1"] = "1"
    attempt_id: PathIdentifier
    run_id: PathIdentifier
    analyzer: BoundedText
    provider: BoundedText
    model: BoundedText
    config_fingerprint: Sha256
    candidates: tuple[AIWorkCandidateRecord, ...] = Field(max_length=100_000)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    state: Literal["initialized", "in_progress", "completed", "failed"] = "initialized"

    @model_validator(mode="after")
    def validate_manifest(self) -> "AIWorkManifest":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate_id values must be unique")
        return self


# The persisted manifest is one task-local attempt. This name makes that role
# explicit for callers without introducing a second wire representation.
AIWorkAttempt = AIWorkManifest


class SanitizedPassedPreflightResult(StrictRecoveryModel):
    status: Literal["passed"] = "passed"
    classification: PathIdentifier = "configured"
    recoverable: Literal[False] = False
    circuit_breaking: Literal[False] = False
    http_status: int | None = Field(default=None, ge=100, le=599)
    attempts: int = Field(default=1, ge=0, le=100)
    response_hash: Sha256 | None = None


class PassedPreflightCheckpoint(StrictRecoveryModel):
    schema_version: Literal["1"] = "1"
    attempt_id: PathIdentifier
    config_fingerprint: Sha256
    result: SanitizedPassedPreflightResult
    result_hash: Sha256
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_checkpoint(self) -> "PassedPreflightCheckpoint":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if canonical_json_hash(self.result.model_dump(mode="json")) != self.result_hash:
            raise ValueError("preflight result hash mismatch")
        return self


class RepresentativeCandidateCompletionCheckpoint(StrictRecoveryModel):
    schema_version: Literal["1"] = "1"
    attempt_id: PathIdentifier
    run_id: PathIdentifier
    candidate_id: PathIdentifier
    scope_key: Sha256
    chain_key: Sha256
    deterministic_fact_hash: Sha256
    slice_hash: Sha256
    final_result: dict[str, JsonValue]
    final_result_hash: Sha256
    status: Literal["completed"] = "completed"
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator("final_result")
    @classmethod
    def validate_final_result(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if not value:
            raise ValueError("final_result cannot be empty")
        _reject_sensitive_result(value)
        return value

    @model_validator(mode="after")
    def validate_checkpoint(self) -> "RepresentativeCandidateCompletionCheckpoint":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if canonical_json_hash(self.final_result) != self.final_result_hash:
            raise ValueError("candidate final result hash mismatch")
        return self


CandidateCompletionCheckpoint = RepresentativeCandidateCompletionCheckpoint


@dataclass(frozen=True, slots=True)
class AIRecoveryWriteResult:
    written: bool
    error: str | None = None


RecoveryWriteResult = AIRecoveryWriteResult


def canonical_json_bytes(value: Any) -> bytes:
    document = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return document.encode("utf-8", errors="strict")


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class AIRecoveryStore:
    """Persist only task-local recovery checkpoints under a supplied ai-cache root."""

    def __init__(
        self,
        cache_dir: str | os.PathLike[str],
        *,
        max_entry_bytes: int = DEFAULT_MAX_RECOVERY_BYTES,
    ) -> None:
        if type(max_entry_bytes) is not int or max_entry_bytes <= 0:
            raise ValueError("max_entry_bytes must be a positive integer")
        self.cache_dir = Path(cache_dir)
        self.candidates_dir = self.cache_dir / CANDIDATES_SUBDIR
        self.max_entry_bytes = max_entry_bytes

    def initialize_work_manifest(
        self,
        manifest: AIWorkManifest | Mapping[str, Any] | None = None,
        **values: Any,
    ) -> AIRecoveryWriteResult:
        try:
            if manifest is not None and values:
                raise ValueError("provide a manifest or manifest fields, not both")
            checked = AIWorkManifest.model_validate(manifest if manifest is not None else values)
            return self._save_model(self.cache_dir / WORK_MANIFEST_FILENAME, checked)
        except Exception as exc:
            return _failed_write(exc)

    initialize_manifest = initialize_work_manifest

    def load_work_manifest(
        self,
        expected: AIWorkManifest | Mapping[str, Any] | None = None,
        *,
        attempt_id: str | None = None,
        run_id: str | None = None,
        analyzer: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        config_fingerprint: str | None = None,
        candidates: Sequence[AIWorkCandidateRecord | Mapping[str, Any]] | None = None,
        not_before: datetime | None = None,
    ) -> AIWorkManifest | None:
        try:
            loaded = self._load_model(
                self.cache_dir / WORK_MANIFEST_FILENAME,
                AIWorkManifest,
            )
            if loaded is None:
                return None
            assert isinstance(loaded, AIWorkManifest)
            if expected is not None:
                if any(
                    value is not None
                    for value in (
                        attempt_id,
                        run_id,
                        analyzer,
                        provider,
                        model,
                        config_fingerprint,
                        candidates,
                    )
                ):
                    return None
                checked_expected = AIWorkManifest.model_validate(expected)
                if not _same_manifest_identity(loaded, checked_expected):
                    return None
            else:
                if None in (
                    attempt_id,
                    run_id,
                    analyzer,
                    provider,
                    model,
                    config_fingerprint,
                ) or candidates is None:
                    return None
                expected_candidates = tuple(
                    AIWorkCandidateRecord.model_validate(candidate) for candidate in candidates
                )
                if (
                    loaded.attempt_id != attempt_id
                    or loaded.run_id != run_id
                    or loaded.analyzer != analyzer
                    or loaded.provider != provider
                    or loaded.model != model
                    or loaded.config_fingerprint != config_fingerprint
                    or loaded.candidates != expected_candidates
                ):
                    return None
            if _is_stale(loaded.updated_at, not_before):
                return None
            return loaded
        except Exception:
            return None

    load_manifest = load_work_manifest

    def save_passed_preflight(
        self,
        checkpoint: PassedPreflightCheckpoint | Mapping[str, Any] | None = None,
        *,
        attempt_id: str | None = None,
        config_fingerprint: str | None = None,
        result: SanitizedPassedPreflightResult | Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> AIRecoveryWriteResult:
        try:
            if checkpoint is not None:
                if any(value is not None for value in (attempt_id, config_fingerprint, result, now)):
                    raise ValueError("provide a checkpoint or checkpoint fields, not both")
                checked = PassedPreflightCheckpoint.model_validate(checkpoint)
            else:
                checked_result = SanitizedPassedPreflightResult.model_validate(result)
                timestamp = _checked_now(now)
                existing = self.load_passed_preflight(
                    attempt_id=attempt_id,
                    config_fingerprint=config_fingerprint,
                )
                checked = PassedPreflightCheckpoint(
                    attempt_id=attempt_id,
                    config_fingerprint=config_fingerprint,
                    result=checked_result,
                    result_hash=canonical_json_hash(checked_result.model_dump(mode="json")),
                    created_at=existing.created_at if existing is not None else timestamp,
                    updated_at=timestamp,
                )
            return self._save_model(self.cache_dir / PREFLIGHT_FILENAME, checked)
        except Exception as exc:
            return _failed_write(exc)

    def load_passed_preflight(
        self,
        *,
        attempt_id: str | None,
        config_fingerprint: str | None,
        not_before: datetime | None = None,
    ) -> PassedPreflightCheckpoint | None:
        try:
            expected_attempt = _validate_path_identifier(attempt_id)
            expected_fingerprint = _validate_sha256(config_fingerprint)
            loaded = self._load_model(
                self.cache_dir / PREFLIGHT_FILENAME,
                PassedPreflightCheckpoint,
            )
            if loaded is None:
                return None
            assert isinstance(loaded, PassedPreflightCheckpoint)
            if (
                loaded.attempt_id != expected_attempt
                or loaded.config_fingerprint != expected_fingerprint
                or canonical_json_hash(loaded.result.model_dump(mode="json"))
                != loaded.result_hash
                or _is_stale(loaded.updated_at, not_before)
            ):
                return None
            return loaded
        except Exception:
            return None

    def save_candidate_completion(
        self,
        checkpoint: RepresentativeCandidateCompletionCheckpoint | Mapping[str, Any] | None = None,
        *,
        attempt_id: str | None = None,
        run_id: str | None = None,
        candidate: AIWorkCandidateRecord | Mapping[str, Any] | None = None,
        final_result: Mapping[str, JsonValue] | None = None,
        now: datetime | None = None,
    ) -> AIRecoveryWriteResult:
        try:
            if checkpoint is not None:
                if any(value is not None for value in (attempt_id, run_id, candidate, final_result, now)):
                    raise ValueError("provide a checkpoint or checkpoint fields, not both")
                checked = RepresentativeCandidateCompletionCheckpoint.model_validate(checkpoint)
            else:
                expected_candidate = AIWorkCandidateRecord.model_validate(candidate)
                result_value = dict(final_result) if final_result is not None else None
                if result_value is None:
                    raise ValueError("final_result is required")
                timestamp = _checked_now(now)
                existing = self.load_candidate_completion(
                    attempt_id=attempt_id,
                    run_id=run_id,
                    candidate=expected_candidate,
                )
                checked = RepresentativeCandidateCompletionCheckpoint(
                    attempt_id=attempt_id,
                    run_id=run_id,
                    candidate_id=expected_candidate.candidate_id,
                    scope_key=expected_candidate.scope_key,
                    chain_key=expected_candidate.chain_key,
                    deterministic_fact_hash=expected_candidate.deterministic_fact_hash,
                    slice_hash=expected_candidate.slice_hash,
                    final_result=result_value,
                    final_result_hash=canonical_json_hash(result_value),
                    created_at=existing.created_at if existing is not None else timestamp,
                    updated_at=timestamp,
                )
            destination = self.candidates_dir / f"{checked.candidate_id}.json"
            return self._save_model(destination, checked)
        except Exception as exc:
            return _failed_write(exc)

    def load_candidate_completion(
        self,
        *,
        attempt_id: str | None,
        run_id: str | None,
        candidate: AIWorkCandidateRecord | Mapping[str, Any],
        not_before: datetime | None = None,
    ) -> RepresentativeCandidateCompletionCheckpoint | None:
        try:
            expected_attempt = _validate_path_identifier(attempt_id)
            expected_run = _validate_path_identifier(run_id)
            expected_candidate = AIWorkCandidateRecord.model_validate(candidate)
            path = self.candidates_dir / f"{expected_candidate.candidate_id}.json"
            loaded = self._load_model(path, RepresentativeCandidateCompletionCheckpoint)
            if loaded is None:
                return None
            assert isinstance(loaded, RepresentativeCandidateCompletionCheckpoint)
            if (
                loaded.attempt_id != expected_attempt
                or loaded.run_id != expected_run
                or loaded.candidate_id != expected_candidate.candidate_id
                or loaded.scope_key != expected_candidate.scope_key
                or loaded.chain_key != expected_candidate.chain_key
                or loaded.deterministic_fact_hash != expected_candidate.deterministic_fact_hash
                or loaded.slice_hash != expected_candidate.slice_hash
                or canonical_json_hash(loaded.final_result) != loaded.final_result_hash
                or _is_stale(loaded.updated_at, not_before)
            ):
                return None
            return loaded
        except Exception:
            return None

    def list_candidate_completions(
        self,
        manifest: AIWorkManifest | Mapping[str, Any],
        *,
        not_before: datetime | None = None,
    ) -> list[RepresentativeCandidateCompletionCheckpoint]:
        try:
            expected = AIWorkManifest.model_validate(manifest)
        except Exception:
            return []
        completed: list[RepresentativeCandidateCompletionCheckpoint] = []
        for candidate in expected.candidates:
            checkpoint = self.load_candidate_completion(
                attempt_id=expected.attempt_id,
                run_id=expected.run_id,
                candidate=candidate,
                not_before=not_before,
            )
            if checkpoint is not None:
                completed.append(checkpoint)
        return completed

    list_valid_candidate_completions = list_candidate_completions

    def _save_model(self, destination: Path, model: StrictRecoveryModel) -> AIRecoveryWriteResult:
        temp_path: Path | None = None
        try:
            serialized = canonical_json_bytes(model.model_dump(mode="json"))
            if len(serialized) > self.max_entry_bytes:
                raise _RecoverySafetyError("recovery checkpoint exceeds size limit")

            self._prepare_directories(include_candidates=destination.parent == self.candidates_dir)
            self._require_safe_destination(destination)
            descriptor, temp_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.stem}.",
                suffix=".tmp",
            )
            temp_path = Path(temp_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(serialized)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise

            self._require_safe_destination(destination)
            os.replace(temp_path, destination)
            temp_path = None
            os.chmod(destination, 0o600, follow_symlinks=False)
            self._fsync_directory(destination.parent)
            return AIRecoveryWriteResult(written=True)
        except Exception as exc:
            return _failed_write(exc)
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _load_model(
        self,
        path: Path,
        model_type: type[StrictRecoveryModel],
    ) -> StrictRecoveryModel | None:
        raw = self._read_file(path)
        if raw is None:
            return None
        try:
            document = _parse_strict_json(raw)
            if not isinstance(document, dict):
                return None
            model = model_type.model_validate_json(raw, strict=True)
            canonical = canonical_json_bytes(model.model_dump(mode="json"))
            if canonical != canonical_json_bytes(document):
                return None
            return model
        except Exception:
            return None

    def _read_file(self, path: Path) -> bytes | None:
        if not self._is_safe_directory(self.cache_dir):
            return None
        if path.parent != self.cache_dir and not self._is_safe_directory(path.parent):
            return None
        try:
            path_info = os.lstat(path)
        except OSError:
            return None
        if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
            return None
        if path_info.st_size == 0 or path_info.st_size > self.max_entry_bytes:
            return None

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                return None
            if info.st_size == 0 or info.st_size > self.max_entry_bytes:
                return None
            chunks: list[bytes] = []
            remaining = self.max_entry_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if not raw or len(raw) > self.max_entry_bytes:
                return None
            return raw
        finally:
            os.close(descriptor)

    def _prepare_directories(self, *, include_candidates: bool) -> None:
        self._create_or_require_directory(self.cache_dir)
        if include_candidates:
            self._create_or_require_directory(self.candidates_dir)

    @staticmethod
    def _create_or_require_directory(path: Path) -> None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            path.mkdir(mode=0o700)
            info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _RecoverySafetyError(f"unsafe recovery directory: {path.name}")
        os.chmod(path, 0o700, follow_symlinks=False)

    @staticmethod
    def _is_safe_directory(path: Path) -> bool:
        try:
            info = os.lstat(path)
        except OSError:
            return False
        return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)

    @staticmethod
    def _require_safe_destination(path: Path) -> None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise _RecoverySafetyError("recovery destination is not a safe regular file")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _same_manifest_identity(left: AIWorkManifest, right: AIWorkManifest) -> bool:
    return (
        left.attempt_id == right.attempt_id
        and left.run_id == right.run_id
        and left.analyzer == right.analyzer
        and left.provider == right.provider
        and left.model == right.model
        and left.config_fingerprint == right.config_fingerprint
        and left.candidates == right.candidates
    )


def _checked_now(value: datetime | None) -> datetime:
    timestamp = value if value is not None else datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return timestamp


def _is_stale(updated_at: datetime, not_before: datetime | None) -> bool:
    now = datetime.now(timezone.utc)
    if updated_at > now + timedelta(minutes=5):
        return True
    if not_before is None:
        return False
    checked = _checked_now(not_before)
    return updated_at < checked


def _validate_path_identifier(value: object) -> str:
    class _IdentifierModel(StrictRecoveryModel):
        value: PathIdentifier

    return _IdentifierModel.model_validate({"value": value}).value


def _validate_sha256(value: object) -> str:
    class _ShaModel(StrictRecoveryModel):
        value: Sha256

    return _ShaModel.model_validate({"value": value}).value


def _parse_strict_json(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _RecoverySafetyError) as exc:
        raise _RecoverySafetyError("recovery file is not strict UTF-8 JSON") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _RecoverySafetyError("recovery JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise _RecoverySafetyError(f"invalid JSON constant: {value}")


_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "credentials",
        "credential",
        "headers",
        "messages",
        "rendered_prompt",
        "system_prompt",
        "user_prompt",
        "raw_prompt",
        "raw_request",
        "raw_request_body",
        "request_body",
        "context_slice",
        "complete_context_slice",
        "contexts",
        "raw_code",
        "source_code",
        "code_text",
        "file_content",
    }
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}")


def _reject_sensitive_result(value: JsonValue) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
            if normalized in _SENSITIVE_KEYS or normalized.endswith("_api_key"):
                raise ValueError(f"sensitive field is not persistable: {key}")
            _reject_sensitive_result(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_result(nested)
    elif isinstance(value, str) and _BEARER_RE.search(value):
        raise ValueError("authorization credentials are not persistable")


def _failed_write(exc: Exception) -> AIRecoveryWriteResult:
    return AIRecoveryWriteResult(written=False, error=f"{type(exc).__name__}: {exc}")
