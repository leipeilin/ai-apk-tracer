from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.analysis.indexer import build_code_index
from app.analysis.rule_runner import RuleRunner
from app.config import WORKSPACE_ROOT, RuleRuntimeSettings


SOURCE = """package com.example;
public class ExportedActivity {
    public void onCreate() {
        String url = getIntent().getStringExtra("url");
        webView.loadUrl(url);
        context.registerReceiver(receiver, filter);
        Intent broadcast = new Intent();
        broadcast.putExtra("token", url);
        sendBroadcast(broadcast);
    }
}
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_test_rule(rules_root: Path, rule_id: str, behavior: str) -> None:
    rule_dir = rules_root / "test" / rule_id
    rule_dir.mkdir(parents=True)
    (rule_dir / "rule.yaml").write_text(
        f"id: {rule_id}\nbuiltin: true\n",
        "utf-8",
    )
    (rule_dir / "detect.py").write_text(
        f'''import json
import sys
import time
from pathlib import Path

rule_id = {rule_id!r}
behavior = {behavior!r}
Path("started").write_text(str(time.time()), "utf-8")
if behavior == "timeout":
    time.sleep(2)
if behavior == "error":
    sys.stderr.write("expected failure")
    raise SystemExit(3)
time.sleep(0.15)
Path("ended").write_text(str(time.time()), "utf-8")
print(json.dumps({{
    "protocol_version": "1.0.0",
    "rule_id": rule_id,
    "status": "completed",
    "candidates": [{{
        "rule_id": rule_id,
        "rule_version": "1.0.0",
        "component": "activity",
        "evidence_level": "L1",
        "locations": [],
    }}],
}}))
''',
        "utf-8",
    )


def _prepare_rule_run(path: Path) -> None:
    (path / "rule-results").mkdir(parents=True)


def _peak_success_concurrency(run_dir: Path, rule_ids: list[str]) -> int:
    events = []
    for rule_id in rule_ids:
        workdir = run_dir / "rule-work" / rule_id
        events.append((float((workdir / "started").read_text("utf-8")), 1))
        events.append((float((workdir / "ended").read_text("utf-8")), -1))
    active = peak = 0
    for _, delta in sorted(events, key=lambda event: (event[0], event[1])):
        active += delta
        peak = max(peak, active)
    return peak


def test_rule_runner_bounded_concurrency_matches_serial_fingerprint(tmp_path: Path) -> None:
    rules_root = tmp_path / "rules"
    successful = ["a-rule", "b-rule", "c-rule", "d-rule"]
    for rule_id in successful:
        _write_test_rule(rules_root, rule_id, "success")
    _write_test_rule(rules_root, "e-error", "error")
    _write_test_rule(rules_root, "f-timeout", "timeout")
    serial_dir = tmp_path / "serial"
    parallel_dir = tmp_path / "parallel"
    _prepare_rule_run(serial_dir)
    _prepare_rule_run(parallel_dir)
    common = {
        "wall_timeout_seconds": 1,
        "cpu_timeout_seconds": 3,
    }

    serial = RuleRunner(rules_root, RuleRuntimeSettings(max_concurrency=1, **common)).run_all(serial_dir, {})
    parallel = RuleRunner(rules_root, RuleRuntimeSettings(max_concurrency=2, **common)).run_all(parallel_dir, {})

    serial_candidates, serial_failures = serial
    parallel_candidates, parallel_failures = parallel
    fingerprint = lambda values: hashlib.sha256(
        json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert fingerprint(serial_candidates) == fingerprint(parallel_candidates)
    assert [candidate["rule_id"] for candidate in parallel_candidates] == successful
    assert [(item["rule_id"], item["error"]["code"]) for item in serial_failures] == [
        ("e-error", "RULE_NONZERO_EXIT"),
        ("f-timeout", "RULE_TIMEOUT"),
    ]
    assert [(item["rule_id"], item["error"]["code"]) for item in parallel_failures] == [
        ("e-error", "RULE_NONZERO_EXIT"),
        ("f-timeout", "RULE_TIMEOUT"),
    ]
    assert _peak_success_concurrency(parallel_dir, successful) == 2


def test_rule_runner_can_repeat_same_run_without_stale_workdir(tmp_path: Path) -> None:
    rules_root = tmp_path / "rules"
    _write_test_rule(rules_root, "repeatable-rule", "success")
    run_dir = tmp_path / "run"
    _prepare_rule_run(run_dir)
    runner = RuleRunner(
        rules_root,
        RuleRuntimeSettings(max_concurrency=1),
    )

    first = runner.run_all(run_dir, {})
    workdir = run_dir / "rule-work" / "repeatable-rule"
    (workdir / "stale").write_text("stale", "utf-8")
    result_path = run_dir / "rule-results" / "repeatable-rule.json"
    result_path.write_text("incomplete", "utf-8")

    second = runner.run_all(run_dir, {})

    assert second == first
    assert not (workdir / "stale").exists()
    assert json.loads(result_path.read_text("utf-8"))["status"] == "completed"
    assert not list(result_path.parent.glob(".repeatable-rule.json.*.tmp"))


def test_rules_use_lightweight_readonly_shared_index(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    for relative in ("decompile/sources/com/example", "index", "rule-work", "rule-results"):
        (run_dir / relative).mkdir(parents=True, exist_ok=True)
    (run_dir / "decompile" / "sources" / "com" / "example" / "ExportedActivity.java").write_text(SOURCE, "utf-8")
    descriptor = build_code_index(
        run_dir / "decompile" / "sources",
        run_dir / "index" / "code-index.json",
    )
    database_path = Path(descriptor["database_path"])
    database_hash = sha256(database_path)
    manifest = {
        "analysis_platform_api": 36,
        "components": [{
            "kind": "activity",
            "name": "com.example.ExportedActivity",
            "exported": "true",
            "permission": None,
            "intent_filters": [],
        }],
    }
    payload = {
        "manifest": manifest,
        "index": {**descriptor, "allowed_index_root": (run_dir / "index").resolve().as_posix()},
        "config": {"analysis_platform_api": 36},
    }
    runner = RuleRunner(WORKSPACE_ROOT / "rules", RuleRuntimeSettings())

    candidates, failures = runner.run_all(run_dir, payload)

    assert failures == []
    assert any(candidate["rule_id"] == "ACTIVITY_EXPORTED_NO_PERMISSION" for candidate in candidates)
    assert any(candidate["rule_id"] == "ACTIVITY_INTENT_TO_SENSITIVE_SINK" for candidate in candidates)
    assert any(candidate["rule_id"] == "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION" for candidate in candidates)
    assert any(candidate["rule_id"] == "IMPLICIT_BROADCAST_SENSITIVE_DATA" for candidate in candidates)
    assert sha256(database_path) == database_hash
    input_paths = list((run_dir / "rule-work").glob("*/input.json"))
    assert len(input_paths) == 29
    assert max(path.stat().st_size for path in input_paths) < 64 * 1024
    sample = json.loads(input_paths[0].read_text("utf-8"))
    assert "code_index" not in sample
    assert sample["index"]["type"] == "sqlite"
