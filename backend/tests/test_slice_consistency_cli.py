"""check-finding-slice-consistency.py 的扫描分类逻辑测试。

v2026-08-14：CLI 的 SLICE_UNAVAILABLE 判定修正——L1/rule_only finding 天然无
slice（_should_build_slice 对 L1 且 ai_eligible≠True 返回 False），不算异常；
只有"本该进 AI 的 finding"（L2 且 analysis_status != rule_only）缺 slice 才计
slice_missing。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))

import importlib.util  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "check_finding_slice_consistency",
    SCRIPTS_ROOT / "check-finding-slice-consistency.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)
scan_run = _MOD.scan_run


def _write_finding(run_dir: Path, fid: str, finding: dict, evidence: dict) -> None:
    (run_dir / "findings").mkdir(parents=True, exist_ok=True)
    (run_dir / "reports" / "evidence").mkdir(parents=True, exist_ok=True)
    (run_dir / "findings" / f"{fid}.json").write_text(json.dumps(finding, ensure_ascii=False), "utf-8")
    (run_dir / "reports" / "evidence" / f"{fid}.json").write_text(json.dumps(evidence, ensure_ascii=False), "utf-8")


def test_l1_rule_only_without_slice_is_consistent(tmp_path: Path) -> None:
    """v2026-08-14：L1 + rule_only finding 无 slice 是设计预期，不算 slice_missing。"""
    run_dir = tmp_path / "run1"
    _write_finding(run_dir, "f1", {
        "rule_id": "ACTIVITY_EXPORTED_NO_PERMISSION",
        "component_name": "com.example.Act",
        "evidence_level": "L1",
        "analysis_status": "rule_only",
        "sinks": [],
    }, {"context_slice": None})

    result = scan_run(run_dir)
    assert result["findings"] == 1
    assert result["mismatch"] == 0
    assert result["slice_missing"] == 0
    assert result["consistent"] == 1


def test_l2_ai_expected_without_slice_is_slice_missing(tmp_path: Path) -> None:
    """v2026-08-14：L2 且已 AI 分析但缺 slice → 真异常，计 slice_missing。"""
    run_dir = tmp_path / "run2"
    _write_finding(run_dir, "f2", {
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "component_name": "com.example.Act",
        "evidence_level": "L2",
        "analysis_status": "ai_completed",
        "sinks": [{"path": "com/example/Sink.java", "line": 100, "kind": "sensitive_sink"}],
    }, {"context_slice": None})

    result = scan_run(run_dir)
    assert result["findings"] == 1
    assert result["slice_missing"] == 1
    assert result["consistent"] == 0
    assert result["details"][0]["category"] == "SLICE_UNAVAILABLE"


def test_l2_rule_only_without_slice_is_consistent(tmp_path: Path) -> None:
    """v2026-08-14：L2 但 rule_only（funnel 拦截未进 AI）无 slice 也属正常。"""
    run_dir = tmp_path / "run3"
    _write_finding(run_dir, "f3", {
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "component_name": "com.example.Act",
        "evidence_level": "L2",
        "analysis_status": "rule_only",
        "sinks": [],
    }, {"context_slice": None})

    result = scan_run(run_dir)
    assert result["findings"] == 1
    assert result["slice_missing"] == 0
    assert result["consistent"] == 1


def test_sink_mismatch_still_reported(tmp_path: Path) -> None:
    """v2026-08-14：sinks 不一致仍报 mismatch（修正不影响主异常检测）。"""
    run_dir = tmp_path / "run4"
    _write_finding(run_dir, "f4", {
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "component_name": "com.example.Act",
        "evidence_level": "L2",
        "analysis_status": "ai_completed",
        "sinks": [{"path": "com/example/Pref.java", "line": 221, "kind": "sensitive_sink"}],
    }, {"context_slice": {
        "slice_id": "slice_x",
        "candidate": {"sinks": [{"path": "com/example/Pref.java", "line": 124, "kind": "sensitive_sink"}]},
    }})

    result = scan_run(run_dir)
    assert result["findings"] == 1
    assert result["mismatch"] == 1
    assert result["slice_missing"] == 0
    assert result["details"][0]["category"] == "FINDING_SLICE_SINK_MISMATCH"
