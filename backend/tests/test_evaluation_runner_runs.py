"""M4-T4.2 run 产物评估测试（探索轨指标 + 三本账 + wall-time + 聚合 + CLI）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.evaluation.golden import GoldenCase
from app.evaluation.runner import (
    evaluate_explorer_against_golden,
    evaluate_runs,
    main,
    summarize_run_costs,
)


def _case(cid: str, expectation: str | None) -> GoldenCase:
    base: dict[str, Any] = {
        "id": cid, "category": "test", "label": "positive",
        "rule": "RULE_X", "component": "service", "entry": "onBind",
        "operation": "op",
        "expected": {
            "candidate": True, "dataflow": "interprocedural",
            "auth": "unprotected", "guard": "absent", "taxonomy": None,
            "verdict": "report",
        },
        "must_not_report": [], "sources": [], "sinks": [], "tags": ["t"],
        "provenance": [{"kind": "test", "reference": "ref"}],
    }
    if expectation:
        base["explorer_expected"] = {
            "expectation": expectation,
            "source_match_keys": [f"{cid}-src"],
            "sink_match_keys": [f"{cid}-snk"],
            "notes": "test",
        }
    return GoldenCase.model_validate(base)


def _proposal(src: str, snk: str) -> dict[str, Any]:
    return {"source": src, "sink": snk, "hops": [
        {"from_method_id": "a/A.java#A.f:1", "to_method_id": "b/B.java#B.g:2"}]}


def _make_run(tmp_path: Path, run_id: str, proposals: list[dict[str, Any]] | None,
              stages: list[dict[str, Any]] | None = None,
              created: str = "2026-08-23T10:00:00+00:00",
              completed: str = "2026-08-23T10:30:00+00:00") -> Path:
    run_dir = tmp_path / run_id
    if proposals is not None:
        (run_dir / "explorer").mkdir(parents=True)
        (run_dir / "explorer" / "candidates.json").write_text(
            json.dumps([{"chain_proposal": p} for p in proposals]), "utf-8")
    manifest = {
        "run_id": run_id, "created_at": created, "completed_at": completed,
        "stages": stages or [],
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    return run_dir


class TestExplorerHitMetrics:
    def test_hit_and_conditional_separated(self, tmp_path: Path) -> None:
        """A-1/A-2：hit 命中分类正确；conditional 不进 hit 分母（独立 rate）。"""
        run_dir = _make_run(tmp_path, "r1", [
            _proposal("case-a-src.onBind", "case-a-snk.fire"),
            _proposal("case-b-src", "case-b-snk"),
        ])
        cases = [
            _case("case-a", "hit"), _case("case-b", "conditional"),
            _case("case-c", "hit"),  # 未命中
            _case("case-d", None),   # 无标注
        ]
        result = evaluate_explorer_against_golden(run_dir, cases)
        assert result["explorer_hits"] == ["case-a"]
        assert result["explorer_hit_rate"] == 0.5
        assert result["explorer_hit_total"] == 2
        assert result["conditional_hits"] == ["case-b"]
        assert result["conditional_hit_rate"] == 1.0
        assert result["proposals_total"] == 2

    def test_missing_candidates_tolerated(self, tmp_path: Path) -> None:
        """A-3/N-1：candidates.json 缺失 → proposals_total=0；无 hit case → rate None。"""
        run_dir = _make_run(tmp_path, "r2", None)
        result = evaluate_explorer_against_golden(run_dir, [_case("x", "hit")])
        assert result["proposals_total"] == 0
        assert result["explorer_hit_rate"] == 0.0
        empty = evaluate_explorer_against_golden(run_dir, [])
        assert empty["explorer_hit_rate"] is None

    def test_hop_channel_matching(self, tmp_path: Path) -> None:
        """三通道：hops method_id 命中（描述性 sink——T4.1 R-3 语义）。"""
        run_dir = _make_run(tmp_path, "r3", [{
            "source": "case-a-src entry", "sink": "未确认的敏感操作",
            "hops": [{"from_method_id": "x#f:1",
                      "to_method_id": "com/example/case-a-snk.java#fire:9"}]}])
        cases = [_case("case-a", "hit")]
        result = evaluate_explorer_against_golden(run_dir, cases)
        assert result["explorer_hits"] == ["case-a"]


class TestRunCosts:
    def test_manifest_extraction(self, tmp_path: Path) -> None:
        """A-4/A-5：三本账五值 + wall_seconds；字段名对齐真实 manifest。"""
        run_dir = _make_run(tmp_path, "r4", [], stages=[
            {"name": "explorer", "summary": {
                "ai_requests_used": 424, "read_requests_used": 20,
                "deep_dive_requests_used": 0}},
            {"name": "ai_analysis", "summary": {
                "requests_used": 486, "explorer_requests_used": 424,
                "ai_stage_requests_used": 62, "verify_requests_used": 29}},
            {"name": "aggregation", "summary": {"finding_count": 151}},
        ])
        costs = summarize_run_costs(run_dir)
        assert costs["explorer_requests"] == 424
        assert costs["deep_dive_requests"] == 0
        assert costs["verify_requests"] == 29
        assert costs["ai_stage_requests"] == 62
        assert costs["total_requests"] == 486
        assert costs["finding_count"] == 151
        assert costs["wall_seconds"] == 1800.0

    def test_missing_stages_tolerated(self, tmp_path: Path) -> None:
        """N-2：缺阶段 → 字段 None；manifest 缺失 → error 标记。"""
        run_dir = _make_run(tmp_path, "r5", None)
        costs = summarize_run_costs(run_dir)
        assert costs["explorer_requests"] is None
        assert costs["wall_seconds"] == 1800.0
        ghost = summarize_run_costs(tmp_path / "no-such")
        assert ghost.get("error") == "manifest_missing"

    def test_bad_time_tolerated(self, tmp_path: Path) -> None:
        run_dir = _make_run(tmp_path, "r6", None,
                            created="not-a-time", completed="also-bad")
        assert summarize_run_costs(run_dir)["wall_seconds"] is None


class TestAggregate:
    def test_weighted_aggregation(self, tmp_path: Path) -> None:
        """A-6：加权命中率（总命中/总 hit case）+ 总三本账 + None-rate 剔除。

        用真实 golden v3 manifest（5 hit case——合成 proposal 键不命中 →
        rate 0.0 如实）；r2 无 hit case 语义由空候选承载（rate 非 None）。
        """
        from app.evaluation.runner import DEFAULT_MANIFEST

        r1 = _make_run(tmp_path, "a1", [
            _proposal("case-a-src", "case-a-snk")], stages=[
            {"name": "explorer", "summary": {"ai_requests_used": 10}}])
        r2 = _make_run(tmp_path, "a2", [], stages=[
            {"name": "explorer", "summary": {"ai_requests_used": 5}}])
        result = evaluate_runs([r1, r2], manifest_path=DEFAULT_MANIFEST)
        assert result["aggregate"]["explorer_hit_rate"] == 0.0  # 加权（0 命中如实）
        assert result["aggregate"]["explorer_hits_total"] == 0
        # run-case 加权语义：每 run 独立评估同一 golden 集
        # （2 run × 6 hit case——M3/M4 审查 4.1 补标 shop V-02 后）
        assert result["aggregate"]["explorer_hit_cases_total"] == 12
        assert result["aggregate"]["costs_total"]["explorer_requests"] == 15
        assert result["aggregate"]["wall_seconds_total"] == 3600.0
        assert result["aggregate"]["unaggregated_runs"] == []


class TestCli:
    def test_runs_mode_json_output(self, tmp_path: Path, capsys) -> None:
        """A-7：--runs 模式 JSON 输出（真实 golden v3 manifest——合成 run 无
        候选 → hit_rate=0 如实输出）。"""
        from unittest.mock import patch

        from app.evaluation.runner import DEFAULT_MANIFEST

        run_dir = _make_run(tmp_path, "cli1", [])
        with patch("app.evaluation.runner.RUNS_ROOT", tmp_path):
            code = main(["--runs", run_dir.name])
        assert code == 0
        output = json.loads(capsys.readouterr().out)
        assert output["runs_total"] == 1
        assert DEFAULT_MANIFEST.is_file()  # 真实数据集被加载

    def test_mutual_exclusion(self, capsys) -> None:
        """N-4：--results 与 --runs 同给/均不给 → 退出码 2。"""
        import pytest

        with pytest.raises(SystemExit) as both:
            main(["--results", "x.json", "--runs", "y"])
        assert both.value.code == 2
        with pytest.raises(SystemExit) as neither:
            main([])
        assert neither.value.code == 2


def _fake_manifest(tmp_path: Path, cases: list[GoldenCase]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({
        "schema_version": "1.0", "dataset_version": "v3",
        "description": "test manifest",
        "ai_responses": [{
            "id": "valid", "file": "ai-responses/valid.json",
            "expectation": "valid", "target_model": "L2ReviewOutput",
            "model_version": "1", "schema_file": "ai_l2_review_output.schema.json",
            "schema_sha256": "0" * 64,
        }],
        "cases": [{"id": c.id, "file": f"cases/{c.id}.json"} for c in cases],
    }), "utf-8")
    # 真实加载走 load_golden_dataset（读 case 文件）——为简化，直接把 case
    # JSON 落盘
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir(exist_ok=True)
    for case in cases:
        (cases_dir / f"{case.id}.json").write_text(case.model_dump_json(), "utf-8")
    return path
