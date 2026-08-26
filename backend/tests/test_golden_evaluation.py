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
from app.evaluation.golden import (
    CaseLabel,
    GoldenCase,
    GoldenManifest,
    load_golden_dataset,
)
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
    assert output["dataset_version"] == "v3"  # M4-T4.1：探索轨标注层升级（评审 R-2）
    assert output["submitted_result_count"] == 1
    assert output["missing_actual_count"] == 28
    assert output["missing_actual_ids"] == output["metrics"]["missing_actual_ids"]
    assert "remote-aidl-unguarded" not in output["missing_actual_ids"]
    assert output["metrics"]["candidate"]["tp"] == 1
    assert list(tmp_path.iterdir()) == [results]


# ---------------------------------------------------------------------------
# M4-T4.1：探索轨命中标注（ExplorerExpectation + explorer_hit）
# ---------------------------------------------------------------------------

_HIT_CASES = {
    "remote-aidl-unguarded", "provider-query-helper-delegation",
    "sport-binder-unguarded-effect", "router-validation-overwritten",
    "fragment-external-class-name", "extra-close-url-unregistered-dos",
}
_CONDITIONAL_CASES = {
    "account-broadcast-external-sender",
    "keepalive-proxy-data-status-injection",
    "extra-splashinfo-plugin-injection",
}


def _load_all_cases() -> dict:
    root = Path(__file__).resolve().parents[2] / "evaluation" / "golden" / "v1" / "cases"
    manifest = json.loads(
        (root.parent / "manifest.json").read_text("utf-8"))
    cases = {}
    for entry in manifest["cases"]:
        cases[entry["id"]] = json.loads((root.parent / entry["file"]).read_text("utf-8"))
    return cases


def test_explorer_annotations_present_and_typed() -> None:
    """A-6：9 case 有标注（6 hit + 3 conditional——M3/M4 实施审查 4.1 修正后
    口径：含 shop V-02 extra-close-url-unregistered-dos）、其余 None。"""
    cases = _load_all_cases()
    annotated = {cid for cid, c in cases.items() if c.get("explorer_expected")}
    assert annotated == _HIT_CASES | _CONDITIONAL_CASES
    for cid in _HIT_CASES:
        assert cases[cid]["explorer_expected"]["expectation"] == "hit"
        assert cases[cid]["explorer_expected"]["source_match_keys"]
        assert cases[cid]["explorer_expected"]["sink_match_keys"]
        assert cases[cid]["explorer_expected"]["notes"]
    for cid in _CONDITIONAL_CASES:
        assert cases[cid]["explorer_expected"]["expectation"] == "conditional"


def test_explorer_hit_matching_channels() -> None:
    """A-3/A-5/A-8 三通道命中（source 文本 + sink 文本或 hops method_id）。"""
    from app.evaluation.golden import GoldenCase, explorer_hit

    case = GoldenCase.model_validate({
        **_load_all_cases()["sport-binder-unguarded-effect"]})
    # sink 文本通道
    assert explorer_hit(case, {
        "source": "SportXmsService.onBind", "sink": "finishSport 停止运动",
        "hops": []})
    # hops method_id 通道（描述性 sink——评审 R-3）
    assert explorer_hit(case, {
        "source": "SportXmsService 绑定入口",
        "sink": "未确认的敏感操作",
        "hops": [{"from_method_id": "a/A.java#A.f:1",
                  "to_method_id": "com/xiaomi/fitness/sport_xms/SportXmsApiImpl.java#finishSport:703"}]})
    # 单边命中（source 对 sink 错）→ False；大小写不敏感 → True
    assert not explorer_hit(case, {
        "source": "SportXmsService.onBind", "sink": "unrelated", "hops": []})
    assert explorer_hit(case, {
        "source": "sportxmsservice.onbind", "sink": "FINISHSPORT", "hops": []})


def test_explorer_hit_conditional_and_unannotated_excluded() -> None:
    """A-4：conditional/无标注/miss 不进二元命中。"""
    from app.evaluation.golden import GoldenCase, explorer_hit

    cases = _load_all_cases()
    conditional = GoldenCase.model_validate({
        **cases["account-broadcast-external-sender"]})
    assert conditional.explorer_expected is not None
    assert conditional.explorer_expected.expectation == "conditional"
    assert not explorer_hit(conditional, {
        "source": "AccountChangedBroadcastHelper", "sink": "sendAccountUpdateBroadcast",
        "hops": []})
    # 无标注 case
    for data in cases.values():
        if not data.get("explorer_expected"):
            plain = GoldenCase.model_validate({**data})
            assert plain.explorer_expected is None
            assert not explorer_hit(plain, {"source": "x", "sink": "y", "hops": []})
            break


def test_explorer_match_keys_no_cross_case_collision() -> None:
    """评审 R-4：hit case 的合成候选两两交叉命中为空（键区分度）。

    conditional case 不进二元命中（explorer_hit 恒 False）——只验证
    其标注键不使 hit case 的标准候选产生额外命中路径外的语义（键
    层面交叉由 hit 集合承载）。
    """
    from app.evaluation.golden import GoldenCase, explorer_hit

    cases = _load_all_cases()
    annotated = {}
    for cid, data in cases.items():
        if data.get("explorer_expected"):
            annotated[cid] = GoldenCase.model_validate({**data})
    hit_cases = {
        cid: case for cid, case in annotated.items()
        if case.explorer_expected.expectation == "hit"
    }
    assert len(hit_cases) == 6
    for owner_cid, case in hit_cases.items():
        # 以 owner 自己的键构造"标准候选"，检查其他 hit case 不误命中
        keys = case.explorer_expected
        candidate = {
            "source": " ".join(keys.source_match_keys),
            "sink": " ".join(keys.sink_match_keys),
            "hops": [],
        }
        for other_cid, other in hit_cases.items():
            hit = explorer_hit(other, candidate)
            if other_cid == owner_cid:
                assert hit, f"{owner_cid} 应命中自身标准候选"
            else:
                assert not hit, f"{other_cid} 不应命中 {owner_cid} 的标准候选（键冲突）"
    # conditional 标注键非空（数据完整性）
    for cid, case in annotated.items():
        if case.explorer_expected.expectation == "conditional":
            assert case.explorer_expected.source_match_keys
            assert case.explorer_expected.sink_match_keys


# ---------------------------------------------------------------------------
# 2026-08-26 审查 R-1：词边界匹配 + 真实假阳回归
# ---------------------------------------------------------------------------


def test_explorer_match_word_boundary() -> None:
    """R-1：词边界——"startActivity" 不匹配 "startActivityForResult"。"""
    from app.evaluation.golden import GoldenCase

    case = GoldenCase.model_validate({
        **_load_all_cases()["extra-close-url-unregistered-dos"],
    })
    keys = case.explorer_expected
    assert keys.matches("MainActivity extras", "startActivityForResult", "") is False
    assert keys.matches("MainActivity extras", "startActivity:33", "") is True
    assert keys.matches("MainActivity extras", "调用 startActivity", "") is True


def test_explorer_hit_qq_sdk_false_positive_regression() -> None:
    """R-1 真实假阳回归：QQ SDK AuthActivity 候选（source='Intent extras' +
    sink='WebView.loadUrl'）不得命中 router-validation-overwritten
    （原宽松子串命中的假阳——2026-08-23 shop 基线 0.167 即此假阳）。"""
    from app.evaluation.golden import GoldenCase, explorer_hit

    case = GoldenCase.model_validate({
        **_load_all_cases()["router-validation-overwritten"]})
    qq_candidate = {
        "source": "Intent extras",
        "sink": "WebView.loadUrl",
        "hops": [{"from_method_id": "com/tencent/tauth/AuthActivity.java#onCreate:63",
                  "to_method_id": "android/webkit/WebView.java#loadUrl:184"}],
    }
    assert not explorer_hit(case, qq_candidate)
    # 真实 RouterActivity 候选仍命中（类名键 + 词边界）
    real_candidate = {
        "source": "RouterActivity Intent extras",
        "sink": "loadUrl(url)",
        "hops": [],
    }
    assert explorer_hit(case, real_candidate)
