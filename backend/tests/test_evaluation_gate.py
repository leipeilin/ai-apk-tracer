"""M4-T4.4 优化门槛测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evaluation.gate import compare_against_baseline, main


def _current(**overrides: object) -> dict:
    doc = {
        "aggregate": {
            "explorer_hit_rate": 0.4, "conditional_hit_rate": 0.5,
            "costs_total": {"explorer_requests": 100},
        },
        "metrics": {"candidate": {"precision": 0.9, "recall": 0.8, "f1": 0.85}},
    }
    doc.update(overrides)  # type: ignore[arg-type]
    return doc


class TestCompare:
    def test_equal_allows(self) -> None:
        result = compare_against_baseline(_current(), _current())
        assert result["gate"] == "ALLOW"
        assert len(result["comparisons"]) == 5
        assert not result["skipped"]

    def test_improvement_allows(self) -> None:
        improved = _current()
        improved["aggregate"]["explorer_hit_rate"] = 0.6
        assert compare_against_baseline(improved, _current())["gate"] == "ALLOW"

    def test_degradation_blocks_with_deficit(self) -> None:
        degraded = _current()
        degraded["metrics"]["candidate"]["f1"] = 0.7
        result = compare_against_baseline(degraded, _current())
        assert result["gate"] == "BLOCK"
        f1 = next(c for c in result["comparisons"] if c["metric"].endswith(".f1"))
        assert f1["deficit"] == pytest.approx(0.15)
        assert f1["verdict"] == "BLOCK"

    def test_tolerance_boundary(self) -> None:
        """=tol 边界：恰好等于容差 → ALLOW（严格小于才 BLOCK）；浮点尾差容忍。"""
        degraded = _current()
        degraded["metrics"]["candidate"]["f1"] = 0.83
        result = compare_against_baseline(
            degraded, _current(), {"f1": 0.02})
        assert result["gate"] == "ALLOW"
        result_strict = compare_against_baseline(
            degraded, _current(), {"f1": 0.01})
        assert result_strict["gate"] == "BLOCK"

    def test_baseline_missing_skips(self) -> None:
        """baseline 缺指标 → SKIP（新指标先刷基线——守卫回归不守卫演进）。"""
        baseline = {"metrics": {"candidate": {"precision": 0.9}}}
        result = compare_against_baseline(_current(), baseline)
        assert result["gate"] == "ALLOW"
        assert len(result["skipped"]) == 4  # 其余 4 指标 baseline 缺

    def test_current_missing_blocks(self) -> None:
        degraded = _current()
        degraded["aggregate"].pop("explorer_hit_rate")
        result = compare_against_baseline(degraded, _current())
        assert result["gate"] == "BLOCK"
        assert any(c["reason"] == "current 缺指标" for c in result["comparisons"])

    def test_null_metric_blocks_current_side(self) -> None:
        degraded = _current()
        degraded["aggregate"]["conditional_hit_rate"] = None
        result = compare_against_baseline(degraded, _current())
        assert result["gate"] == "BLOCK"

    def test_structurally_disjoint_reports_blocked(self) -> None:
        """R-4：结构混用（evaluate_runs vs evaluate_results）→ BLOCK。"""
        result = compare_against_baseline(
            {"something": "else"}, {"another": "thing"})
        assert result["gate"] == "BLOCK"
        assert "结构" in result["reason"]


class TestRealOutputFixture:
    def test_evaluate_results_shape(self) -> None:
        """R-2：真实 evaluate_results 输出形态（metrics.candidate 嵌套 +
        by_category 每类重复）——白名单点路径不误入 by_category。"""
        shape = {
            "metrics": {
                "candidate": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
                "by_category": {
                    "test": {"precision": 0.5, "recall": 0.5, "f1": 0.5}},
            },
            "aggregate": {"explorer_hit_rate": 0.2},
        }
        result = compare_against_baseline(shape, shape)
        assert result["gate"] == "ALLOW"
        # by_category 的 0.5 劣化值不参与判定（白名单未含）
        degraded = json.loads(json.dumps(shape))
        degraded["metrics"]["by_category"]["test"]["precision"] = 0.1
        assert compare_against_baseline(degraded, shape)["gate"] == "ALLOW"


class TestCli:
    def test_cli_allow_and_block(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        current = tmp_path / "current.json"
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps(_current()), "utf-8")
        current.write_text(json.dumps(_current()), "utf-8")
        assert main(["--current", str(current), "--baseline", str(baseline)]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["gate"] == "ALLOW"

        degraded = _current()
        degraded["aggregate"]["explorer_hit_rate"] = 0.1
        current.write_text(json.dumps(degraded), "utf-8")
        assert main(["--current", str(current), "--baseline", str(baseline)]) == 1
        output = json.loads(capsys.readouterr().out)
        assert output["gate"] == "BLOCK"

    def test_cli_tolerance_flag(self, tmp_path: Path) -> None:
        degraded = _current()
        degraded["metrics"]["candidate"]["f1"] = 0.84
        current = tmp_path / "c.json"
        baseline = tmp_path / "b.json"
        current.write_text(json.dumps(degraded), "utf-8")
        baseline.write_text(json.dumps(_current()), "utf-8")
        assert main([
            "--current", str(current), "--baseline", str(baseline),
            "--tolerance", "f1=0.02"]) == 0


class TestWorkflowDoc:
    def test_doc_contains_sections_and_commands(self) -> None:
        """A-6（评审 R-5/R-3）：流程文档三节 + 命令实录。"""
        doc = (
            Path(__file__).resolve().parents[2] / "docs" / "evaluation-workflow.md"
        ).read_text("utf-8")
        for section in ("## 1. 基线快照", "## 2. 门槛判定", "## 3. 默认开启检查点", "## 4. BLOCK 处置"):
            assert section in doc
        assert "--runs" in doc and "--results" in doc  # R-3：规则轨快照命令
        assert "backend.app.evaluation.gate" in doc
        assert "不劣于基线" in doc
