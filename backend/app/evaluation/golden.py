"""Strict schemas and loaders for versioned offline golden datasets."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ..analysis.ai_models import AI_OUTPUT_MODEL_VERSIONS, L2ReviewOutput


_SCHEMAS_ROOT = Path(__file__).resolve().parents[3] / "schemas"
_L2_SCHEMA_FILE = "ai_l2_review_output.schema.json"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class CaseLabel(str, Enum):
    """Ground-truth label; only positive and negative enter binary metrics."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


class DataflowStatus(str, Enum):
    INTRAPROCEDURAL = "intraprocedural"
    INTERPROCEDURAL = "interprocedural"
    NOT_PROVEN = "not_proven"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class AuthorizationStatus(str, Enum):
    UNPROTECTED = "unprotected"
    STRONGLY_PROTECTED = "strongly_protected"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class GuardStatus(str, Enum):
    ABSENT = "absent"
    PRESENT_EFFECTIVE = "present_effective"
    PRESENT_BYPASSABLE = "present_bypassable"
    PRESENT_PARTIAL = "present_partial"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class Verdict(str, Enum):
    REPORT = "report"
    SUPPRESS = "suppress"
    REVIEW = "review"
    UNKNOWN = "unknown"


class EvidenceRef(StrictModel):
    path: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)


class ProvenanceRef(StrictModel):
    """Audit-only source reference; provenance is never treated as scoring evidence."""

    kind: str = Field(min_length=1)
    reference: str = Field(min_length=1)


class ExpectedOutcome(StrictModel):
    candidate: StrictBool | None
    dataflow: DataflowStatus
    auth: AuthorizationStatus
    guard: GuardStatus
    taxonomy: str | None
    verdict: Verdict


class GoldenCase(StrictModel):
    """One labeled case; ``must_not_report`` entries use exact string matching."""

    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    category: str = Field(min_length=1)
    label: CaseLabel
    rule: str = Field(min_length=1)
    component: str = Field(min_length=1)
    entry: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    expected: ExpectedOutcome
    must_not_report: list[str]
    sources: list[EvidenceRef]
    sinks: list[EvidenceRef]
    tags: list[str]
    provenance: list[ProvenanceRef] = Field(min_length=1)

    @model_validator(mode="after")
    def label_matches_binary_expectation(self) -> "GoldenCase":
        if self.label is CaseLabel.POSITIVE and self.expected.candidate is not True:
            raise ValueError("positive case must expect candidate=true")
        if self.label is CaseLabel.NEGATIVE and self.expected.candidate is not False:
            raise ValueError("negative case must expect candidate=false")
        return self


class CaseManifestEntry(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    file: str = Field(min_length=1)


class AIResponseManifestEntry(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    file: str = Field(min_length=1)
    expectation: str = Field(pattern=r"^(valid|invalid|repairable)$")
    target_model: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    schema_file: str = Field(min_length=1)
    schema_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def targets_current_l2_protocol(self) -> "AIResponseManifestEntry":
        expected_model = L2ReviewOutput.__name__
        expected_version = AI_OUTPUT_MODEL_VERSIONS[expected_model]
        if self.target_model != expected_model:
            raise ValueError(f"AI response target_model must be {expected_model!r}")
        if self.model_version != expected_version:
            raise ValueError(
                f"AI response model_version must be {expected_version!r} for {expected_model}"
            )
        if self.schema_file != _L2_SCHEMA_FILE:
            raise ValueError(f"AI response schema_file must be {_L2_SCHEMA_FILE!r}")
        return self


class GoldenManifest(StrictModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    dataset_version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    cases: list[CaseManifestEntry] = Field(min_length=1)
    ai_responses: list[AIResponseManifestEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def ids_and_files_are_unique(self) -> "GoldenManifest":
        _require_unique((entry.id for entry in self.cases), "manifest case id")
        _require_unique((entry.file for entry in self.cases), "manifest case file")
        _require_unique((entry.id for entry in self.ai_responses), "AI response id")
        _require_unique((entry.file for entry in self.ai_responses), "AI response file")
        return self


class GoldenDataset(StrictModel):
    manifest: GoldenManifest
    cases: tuple[GoldenCase, ...]
    root: Path

    def by_id(self) -> dict[str, GoldenCase]:
        return {case.id: case for case in self.cases}


def load_golden_dataset(manifest_path: str | Path) -> GoldenDataset:
    """Load a manifest, validate every referenced file, and reject duplicate case IDs."""

    path = Path(manifest_path).resolve()
    manifest = GoldenManifest.model_validate(_load_json_object(path))
    root = path.parent
    cases: list[GoldenCase] = []
    seen_case_ids: set[str] = set()

    for entry in manifest.cases:
        case_path = _resolve_dataset_file(root, entry.file)
        case = GoldenCase.model_validate(_load_json_object(case_path))
        if case.id != entry.id:
            raise ValueError(
                f"case id mismatch for {entry.file}: manifest={entry.id!r}, file={case.id!r}"
            )
        if case.id in seen_case_ids:
            raise ValueError(f"duplicate case id: {case.id}")
        seen_case_ids.add(case.id)
        cases.append(case)

    for entry in manifest.ai_responses:
        _validate_ai_response_identity(entry)
        _load_json_object(_resolve_dataset_file(root, entry.file))

    return GoldenDataset(manifest=manifest, cases=tuple(cases), root=root)


def _validate_ai_response_identity(entry: AIResponseManifestEntry) -> None:
    schema_path = _SCHEMAS_ROOT / entry.schema_file
    if not schema_path.is_file():
        raise FileNotFoundError(f"AI response schema file not found: {schema_path}")
    raw_schema = schema_path.read_bytes()
    actual_hash = hashlib.sha256(raw_schema).hexdigest()
    if actual_hash != entry.schema_sha256:
        raise ValueError(
            f"AI response schema hash mismatch for {entry.file}: "
            f"manifest={entry.schema_sha256!r}, actual={actual_hash!r}"
        )
    try:
        schema = json.loads(raw_schema, object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise ValueError(f"AI response schema is not valid UTF-8: {schema_path}") from exc
    if schema != L2ReviewOutput.model_json_schema(mode="validation"):
        raise ValueError(
            f"AI response schema does not match current {L2ReviewOutput.__name__}: {schema_path}"
        )


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"golden dataset file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _resolve_dataset_file(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or candidate.suffix != ".json":
        raise ValueError(f"dataset file must be a relative JSON path: {relative}")
    resolved = (root / candidate).resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"dataset file escapes golden root: {relative}")
    return resolved


def _require_unique(values: Any, description: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {description}: {value}")
        seen.add(value)
