"""探索轨定向验证 harness 测试（M2 收尾-1，指引 §4.1）。

覆盖：入口取样（指定子集/异构均衡/上限）、D-3 违规判定纯函数、
参数错误路径。dry-run/真实探针以真实 run 产物手动冒烟（见验收记录）。
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe_explorer_entry.py"
_spec = importlib.util.spec_from_file_location("probe_explorer_entry", _SCRIPT)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)  # type: ignore[union-attr]


def _args(entries: str | None = None, max_entries: int = 8, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(entries=entries, max_entries=max_entries, dry_run=dry_run)


def _entry(entry_id: str, kind: str) -> dict[str, Any]:
    return {"entry_id": entry_id, "kind": kind, "method_id": f"{entry_id}#m:1"}


class TestSelectEntries:
    def test_explicit_subset_wins(self) -> None:
        entries = [_entry("a", "activity"), _entry("b", "service"), _entry("c", "receiver")]
        selected = probe._select_entries(entries, _args(entries="a,c"))
        assert [e["entry_id"] for e in selected] == ["a", "c"]

    def test_explicit_subset_missing_fails(self) -> None:
        entries = [_entry("a", "activity")]
        with pytest.raises(SystemExit):
            probe._select_entries(entries, _args(entries="a,ghost"))

    def test_heterogeneous_sampling_balanced(self) -> None:
        entries = (
            [_entry(f"act{i}", "activity") for i in range(5)]
            + [_entry(f"svc{i}", "service") for i in range(3)]
            + [_entry(f"rcv{i}", "receiver") for i in range(2)]
            + [_entry("prv0", "provider")]
        )
        selected = probe._select_entries(entries, _args())
        kinds = [e["kind"] for e in selected]
        # 异构优先（各 kind 前 2），剩余额度补位至 max_entries=8：
        # 2+2+2+1=7 后从 activity 桶补 1 → activity=3
        assert len(selected) == 8
        assert kinds.count("activity") == 3
        assert kinds.count("service") == 2
        assert kinds.count("receiver") == 2
        assert kinds.count("provider") == 1

    def test_max_entries_cap(self) -> None:
        entries = [_entry(f"act{i}", "activity") for i in range(10)]
        selected = probe._select_entries(entries, _args(max_entries=3))
        assert len(selected) == 3

    def test_other_kind_fills_remaining(self) -> None:
        entries = [_entry("binder1", "binder"), _entry("web1", "webview_bridge")]
        selected = probe._select_entries(entries, _args())
        assert len(selected) == 2  # 非四类 kind 走补位不被丢弃


class TestClassifyD3Violation:
    def test_violation_when_contextless_completed_with_chains(self) -> None:
        assert probe._classify_d3_violation(True, "completed", 2) is True

    def test_no_violation_when_context_present(self) -> None:
        assert probe._classify_d3_violation(False, "completed", 2) is False

    def test_no_violation_when_contextless_but_failed(self) -> None:
        assert probe._classify_d3_violation(True, "schema_invalid", 0) is False
        assert probe._classify_d3_violation(True, "skipped", 3) is False

    def test_no_violation_when_completed_but_no_chains(self) -> None:
        assert probe._classify_d3_violation(True, "completed", 0) is False


def test_main_missing_run_dir_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert probe.main(["--run-id", "no-such-run", "--dry-run"]) == 2
    assert "不存在" in capsys.readouterr().err
