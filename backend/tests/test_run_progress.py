"""build_run_progress 单测（track-progress-console，方案 §3.5）。

覆盖：全信号精确 / 运行中降级（manifest 顶层计数 + partial jsonl）/
历史 run（observations.json）/ 探索轨未启用 / 畸形产物降级 /
rule-results 缺目录与产物词干排除（评审 R-2）。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.runs.progress import build_run_progress


def _stage(name: str, summary: dict) -> dict:
    return {"name": name, "status": "completed", "summary": summary}


def _entry_record(index: int) -> dict:
    return {"entry_id": f"e{index}", "terminated_by": "loop_done", "rounds": [], "candidate_count": 0}


def test_full_signals_precise(tmp_path: Path) -> None:
    results = tmp_path / "rule-results"
    results.mkdir()
    for rule_id in ("R_A", "R_B", "R_C"):
        (results / f"{rule_id}.json").write_text("{}", "utf-8")
    manifest = {
        "stages": [
            _stage("rule_prescan", {"rule_total_count": 3, "rule_failures": [{"rule_id": "R_C"}]}),
            _stage("explorer", {"entry_count": 73, "entries_explored": 70, "entries_unexplored": 3}),
        ]
    }
    progress = build_run_progress(tmp_path, manifest)
    assert progress["rules"] == {"total": 3, "processed": 3, "failed": 1}
    assert progress["explorer"] == {"total": 73, "explored": 70, "unexplored": 3}


def test_running_falls_back_to_manifest_counts_and_partial_jsonl(tmp_path: Path) -> None:
    """运行中：无 stage summary，顶层计数键（评审 R-1）+ partial jsonl 行数。"""

    explorer_dir = tmp_path / "explorer"
    explorer_dir.mkdir()
    records = [_entry_record(index) for index in range(5)]
    (explorer_dir / "observations-partial.jsonl").write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n\n",
        "utf-8",
    )
    manifest = {"rule_total_count": 33, "explorer_total_count": 8, "stages": []}
    progress = build_run_progress(tmp_path, manifest)
    assert progress["rules"] == {"total": 33, "processed": 0, "failed": None}
    assert progress["explorer"] == {"total": 8, "explored": 5, "unexplored": 3}


def test_historical_run_uses_observations(tmp_path: Path) -> None:
    explorer_dir = tmp_path / "explorer"
    explorer_dir.mkdir()
    (explorer_dir / "observations.json").write_text(
        json.dumps({"entries": [{"entry_id": index} for index in range(4)]}),
        "utf-8",
    )
    progress = build_run_progress(tmp_path, {"stages": []})
    assert progress["rules"] is None
    assert progress["explorer"] == {"total": None, "explored": 4, "unexplored": None}


def test_explorer_disabled_returns_none(tmp_path: Path) -> None:
    progress = build_run_progress(tmp_path, {"rule_total_count": 5, "stages": []})
    assert progress["rules"] == {"total": 5, "processed": 0, "failed": None}
    assert progress["explorer"] is None


def test_explorer_running_early_window_uses_top_level_total(tmp_path: Path) -> None:
    """运行早期窗口（代码审查 C-1）：stage 未落、首条 partial 未写，仅顶层
    explorer_total_count 可用——探索轨不得被误判为未启用。"""

    progress = build_run_progress(tmp_path, {"explorer_total_count": 12, "stages": []})
    assert progress["explorer"] == {"total": 12, "explored": None, "unexplored": None}


def test_malformed_products_degrade_per_field(tmp_path: Path) -> None:
    explorer_dir = tmp_path / "explorer"
    explorer_dir.mkdir()
    (explorer_dir / "observations.json").write_text("{invalid", "utf-8")
    (explorer_dir / "observations-partial.jsonl").write_text("{oops\n", "utf-8")
    manifest = {"stages": [_stage("explorer", {"entry_count": 9})]}
    progress = build_run_progress(tmp_path, manifest)
    assert progress["explorer"] == {"total": 9, "explored": None, "unexplored": None}


def test_rule_results_excludes_artifact_stems(tmp_path: Path) -> None:
    """评审 R-2：rule-results 目录内规则产物（RULE_ARTIFACT_KEYS）不计入 processed。"""

    results = tmp_path / "rule-results"
    results.mkdir()
    for name in ("R_A", "R_B", "binder_bindings", "receiver_registrations", "webview_js_bridges"):
        (results / f"{name}.json").write_text("{}", "utf-8")
    progress = build_run_progress(tmp_path, {"rule_total_count": 2, "stages": []})
    assert progress["rules"] == {"total": 2, "processed": 2, "failed": None}


def test_rule_results_missing_dir_counts_zero_when_total_known(tmp_path: Path) -> None:
    progress = build_run_progress(tmp_path, {"rule_total_count": 7, "stages": []})
    assert progress["rules"] == {"total": 7, "processed": 0, "failed": None}


def test_manifest_none_degrades(tmp_path: Path) -> None:
    (tmp_path / "rule-results").mkdir()
    (tmp_path / "rule-results" / "R_A.json").write_text("{}", "utf-8")
    progress = build_run_progress(tmp_path, None)
    assert progress["rules"] == {"total": None, "processed": 1, "failed": None}
    assert progress["explorer"] is None
