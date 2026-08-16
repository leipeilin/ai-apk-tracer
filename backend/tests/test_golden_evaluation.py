from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.evaluation.golden as golden_module
from app.analysis.ai_models import AI_OUTPUT_MODEL_VERSIONS, L2ReviewOutput
from app.evaluation.golden import CaseLabel, GoldenCase, GoldenManifest, load_golden_dataset
from app.evaluation.metrics import ActualResult, calculate_metrics
from app.evaluation.runner import evaluate_results


REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_ROOT = REPO_ROOT / "evaluation" / "golden" / "v1"
MANIFEST = GOLDEN_ROOT / "manifest.json"


def test_golden_dataset_schema_and_required_regression_patterns() -> None:
    dataset = load_golden_dataset(MANIFEST)
    assert len(dataset.cases) == 29
    assert len(dataset.by_id()) == len(dataset.cases)
    assert {case.label for case in dataset.cases} >= {
        CaseLabel.POSITIVE,
        CaseLabel.NEGATIVE,
    }
    required_ids = {
        "import-is-not-source",
        "empty-provider-mutation",
        "executor-execute-not-network",
        "local-broadcast-not-external",
        "local-binder-not-remote",
        "remote-aidl-unguarded",
        "strong-permission-protected",
        "router-validation-overwritten",
        "fragment-external-class-name",
        "started-service-stopself-event",
        "dynamic-receiver-flag-2",
        "dynamic-receiver-flag-4",
        "file-provider-safe-prefix",
        "file-provider-unsafe-prefix",
        "map-put-not-persistent-write",
        "unregistered-activity",
        "debuggable-default-false",
        # S9（2026-08-16）：动态终审正负样本（manual-verification-report）。
        "provider-query-helper-delegation",
        "sport-binder-unguarded-effect",
        "ble-broadcast-sdk-dead-code",
        "nfc-service-no-sensitive-capability",
        "connect-new-phone-protected-broadcast",
        "widget-provider-authority-conflict",
        "sp-control-flow-cooccurrence-refuted",
        "ownsystem-unselected-implementation",
        "extra-splashinfo-plugin-injection",
        "extra-close-url-unregistered-dos",
        "account-broadcast-external-sender",
        "keepalive-proxy-data-status-injection",
    }
    assert set(dataset.by_id()) == required_ids
    assert all(case.provenance for case in dataset.cases)
    assert all(not ref.path.startswith("apk/") for case in dataset.cases for ref in case.sources + case.sinks)

    invalid = dataset.cases[0].model_dump(mode="json")
    invalid["extra"] = True
    with pytest.raises(ValidationError):
        GoldenCase.model_validate(invalid)


def test_manifest_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    manifest["cases"].append(dict(manifest["cases"][0]))
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), "utf-8")
    with pytest.raises(ValidationError, match="duplicate manifest case id"):
        load_golden_dataset(path)


def test_manifest_rejects_missing_referenced_file(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    manifest["cases"] = [{"id": "missing-case", "file": "cases/missing.json"}]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), "utf-8")
    with pytest.raises(FileNotFoundError, match="missing.json"):
        load_golden_dataset(path)


def test_metrics_binary_classification_and_must_not_report() -> None:
    dataset = load_golden_dataset(MANIFEST)
    selected_ids = [
        "remote-aidl-unguarded",
        "router-validation-overwritten",
        "import-is-not-source",
        "local-binder-not-remote",
    ]
    cases = [dataset.by_id()[case_id] for case_id in selected_ids]
    actual = {
        "remote-aidl-unguarded": ActualResult(
            candidate=True,
            dataflow="interprocedural",
            auth="unprotected",
            guard="absent",
            taxonomy="location_sensor_collection",
            verdict="report",
        ),
        "router-validation-overwritten": ActualResult(candidate=False),
        "import-is-not-source": ActualResult(
            candidate=True,
            reports=["import declaration as external Intent source"],
        ),
        "local-binder-not-remote": ActualResult(candidate=False),
    }
    metrics = calculate_metrics(cases, actual)
    assert metrics["candidate"] == {
        "tp": 1,
        "fp": 1,
        "tn": 1,
        "fn": 1,
        "excluded_conditional_unknown": 0,
        "precision": 0.5,
        "recall": 0.5,
        "f1": 0.5,
    }
    assert metrics["known_positive_recall"] == 0.5
    assert metrics["known_negative_leakage"] == 0.5
    assert metrics["classification"]["by_field"]["taxonomy"]["accuracy"] == 0.5
    assert metrics["must_not_report"]["violation_count"] == 1
    assert set(metrics["by_category"]) == {"binder", "dataflow", "source_detection"}


def test_missing_actual_is_reported_and_excluded_from_all_denominators() -> None:
    dataset = load_golden_dataset(MANIFEST)
    cases = [
        dataset.by_id()["remote-aidl-unguarded"],
        dataset.by_id()["import-is-not-source"],
    ]
    metrics = calculate_metrics(
        cases,
        {"remote-aidl-unguarded": ActualResult(candidate=True)},
    )
    assert metrics["missing_actual_count"] == 1
    assert metrics["missing_actual_ids"] == ["import-is-not-source"]
    assert metrics["candidate"]["tp"] == 1
    assert metrics["candidate"]["tn"] == 0
    assert metrics["classification"]["total"] == 5
    assert metrics["by_category"]["source_detection"]["classification"]["total"] == 0


def test_conditional_and_unknown_labels_are_excluded_from_binary_counts() -> None:
    dataset = load_golden_dataset(MANIFEST)
    template = dataset.by_id()["remote-aidl-unguarded"].model_dump(mode="json")
    cases = []
    for label in ("conditional", "unknown"):
        value = dict(template)
        value["id"] = f"synthetic-{label}"
        value["label"] = label
        value["expected"] = {**template["expected"], "candidate": None, "verdict": "review"}
        cases.append(GoldenCase.model_validate(value))
    metrics = calculate_metrics(
        cases,
        {
            "synthetic-conditional": ActualResult(candidate=True),
            "synthetic-unknown": ActualResult(candidate=False),
        },
    )
    assert metrics["candidate"]["tp"] == 0
    assert metrics["candidate"]["fp"] == 0
    assert metrics["candidate"]["tn"] == 0
    assert metrics["candidate"]["fn"] == 0
    assert metrics["candidate"]["excluded_conditional_unknown"] == 2


def test_ai_protocol_samples_target_current_l2_protocol() -> None:
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    entries = {entry["id"]: entry for entry in manifest["ai_responses"]}
    samples = {
        sample_id: json.loads((GOLDEN_ROOT / entry["file"]).read_text("utf-8"))
        for sample_id, entry in entries.items()
    }
    assert set(samples) == {
        "valid",
        "missing-analysis-complete",
        "extra-field",
        "invalid-enum",
        "repairable-json",
    }

    schema_path = REPO_ROOT / "schemas" / "ai_l2_review_output.schema.json"
    schema_hash = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    for entry in entries.values():
        assert entry["target_model"] == L2ReviewOutput.__name__
        assert entry["model_version"] == AI_OUTPUT_MODEL_VERSIONS[L2ReviewOutput.__name__]
        assert entry["schema_file"] == schema_path.name
        assert entry["schema_sha256"] == schema_hash

    validated = L2ReviewOutput.model_validate(samples["valid"])
    assert validated.analysis_complete is True

    invalid_expectations = {
        "missing-analysis-complete": ("missing", "analysis_complete"),
        "extra-field": ("extra_forbidden", "unexpected"),
        "invalid-enum": ("literal_error", "guard_status"),
    }
    for sample_id, (error_type, field) in invalid_expectations.items():
        with pytest.raises(ValidationError) as exc_info:
            L2ReviewOutput.model_validate(samples[sample_id])
        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["type"] == error_type
        assert errors[0]["loc"] == (field,)

    fenced = samples["repairable-json"]["raw_response"]
    assert fenced.startswith("```json\n") and fenced.endswith("\n```")
    repaired = json.loads(fenced.removeprefix("```json\n").removesuffix("\n```"))
    L2ReviewOutput.model_validate(repaired)


def test_manifest_rejects_ai_response_identity_mismatch() -> None:
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    manifest["ai_responses"][0]["target_model"] = "L1TriageOutput"
    with pytest.raises(ValidationError, match="target_model"):
        GoldenManifest.model_validate(manifest)


def test_loader_rejects_ai_response_schema_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_root = tmp_path / "schemas"
    schema_root.mkdir()
    schema_name = "ai_l2_review_output.schema.json"
    original = (REPO_ROOT / "schemas" / schema_name).read_bytes()
    (schema_root / schema_name).write_bytes(original + b" ")
    monkeypatch.setattr(golden_module, "_SCHEMAS_ROOT", schema_root)
    with pytest.raises(ValueError, match="schema hash mismatch"):
        load_golden_dataset(MANIFEST)


def test_runner_rejects_unknown_result_ids() -> None:
    dataset = load_golden_dataset(MANIFEST)
    with pytest.raises(ValueError, match="unknown case ids"):
        evaluate_results(dataset, {"not-in-manifest": {"candidate": False}})


def test_cli_prints_json_without_writing_output(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    results.write_text(
        json.dumps({"remote-aidl-unguarded": {"candidate": True}}),
        "utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.evaluation.runner",
            "--manifest",
            str(MANIFEST),
            "--results",
            str(results),
        ],
        cwd=REPO_ROOT / "backend",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output["dataset_version"] == "v2"
    assert output["submitted_result_count"] == 1
    assert output["missing_actual_count"] == 28
    assert output["missing_actual_ids"] == output["metrics"]["missing_actual_ids"]
    assert "remote-aidl-unguarded" not in output["missing_actual_ids"]
    assert output["metrics"]["candidate"]["tp"] == 1
    assert list(tmp_path.iterdir()) == [results]
