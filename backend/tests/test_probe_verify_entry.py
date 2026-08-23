"""核验轨定向验证 harness 测试（M2 收尾-2，指引 §4.2）。

覆盖：归因分类映射、L2 候选取样（过滤/指定子集/rule 分散/上限）、
参数错误路径。dry-run/真实探针以真实 run 产物手动冒烟（见验收记录）。
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe_verify_entry.py"
_spec = importlib.util.spec_from_file_location("probe_verify_entry", _SCRIPT)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)  # type: ignore[union-attr]


def _args(candidates: str | None = None, max_candidates: int = 5, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(candidates=candidates, max_candidates=max_candidates, dry_run=dry_run)


def _candidate(cid: str, level: str = "L2", rule: str = "R_A") -> dict[str, Any]:
    return {"candidate_id": cid, "evidence_level": level, "rule_id": rule}


class TestAttribution:
    def test_schema_group(self) -> None:
        assert probe._attribution("schema_invalid") == "ai_output_contract"
        assert probe._attribution("response_invalid") == "ai_output_contract"

    def test_network_group(self) -> None:
        assert probe._attribution("transient_failure") == "network"
        assert probe._attribution("rate_limited") == "network"

    def test_fatal_group(self) -> None:
        assert probe._attribution("auth_failed") == "fatal"
        assert probe._attribution("model_not_found") == "fatal"
        assert probe._attribution("circuit_open") == "fatal"

    def test_unknown(self) -> None:
        assert probe._attribution(None) == "other"
        assert probe._attribution("weird") == "other"


class TestSelectL2Candidates:
    def test_l2_filter(self) -> None:
        pool = [
            _candidate("a", "L1"), _candidate("b", "L2", rule="R_B"),
            _candidate("c", "L2", rule="R_C"),
        ]
        selected = probe._select_l2_candidates(pool, _args())
        assert [c["candidate_id"] for c in selected] == ["b", "c"]

    def test_explicit_subset(self) -> None:
        pool = [_candidate("a"), _candidate("b"), _candidate("c")]
        selected = probe._select_l2_candidates(pool, _args(candidates="a,c"))
        assert [c["candidate_id"] for c in selected] == ["a", "c"]

    def test_explicit_subset_missing_fails(self) -> None:
        pool = [_candidate("a"), _candidate("x", "L1")]
        with pytest.raises(SystemExit):
            probe._select_l2_candidates(pool, _args(candidates="a,ghost"))

    def test_rule_diversified_sampling(self) -> None:
        pool = (
            [_candidate(f"a{i}", rule="R_A") for i in range(3)]
            + [_candidate("b0", rule="R_B")]
            + [_candidate("c0", rule="R_C")]
        )
        selected = probe._select_l2_candidates(pool, _args(max_candidates=3))
        rules = [c["rule_id"] for c in selected]
        assert rules == ["R_A", "R_B", "R_C"]  # 每规则只取首个

    def test_max_cap(self) -> None:
        pool = [_candidate(f"c{i}", rule=f"R{i}") for i in range(10)]
        assert len(probe._select_l2_candidates(pool, _args(max_candidates=4))) == 4


def test_main_missing_run_dir_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert probe.main(["--run-id", "no-such-run", "--dry-run"]) == 2
    assert "不存在" in capsys.readouterr().err
