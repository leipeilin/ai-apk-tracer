"""run_all 逐条落盘回归（track-progress-console，评审 R-5）。

验证：run_all 结束后成功与失败规则的 rule-results/{rule_id}.json 一一对应
落盘且失败 status 归一——增量 _persist_result 前置后 post-loop 聚合行为不变。
既有聚合顺序/并发语义/串行并行一致性回归见 test_rule_index_protocol.py。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.analysis.rule_runner import RuleRunner
from app.config import RuleRuntimeSettings


def _write_test_rule(rules_root: Path, rule_id: str, behavior: str) -> None:
    """与 test_rule_index_protocol 同构的最小规则（success / error 两态）。"""

    rule_dir = rules_root / "test" / rule_id
    rule_dir.mkdir(parents=True)
    (rule_dir / "rule.yaml").write_text(
        f"id: {rule_id}\nbuiltin: true\n",
        "utf-8",
    )
    (rule_dir / "detect.py").write_text(
        f'''import json
import sys

rule_id = {rule_id!r}
behavior = {behavior!r}
if behavior == "error":
    sys.stderr.write("expected failure")
    raise SystemExit(3)
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


def test_run_all_persists_every_rule_result_including_failures(tmp_path: Path) -> None:
    rules_root = tmp_path / "rules"
    _write_test_rule(rules_root, "a-rule", "success")
    _write_test_rule(rules_root, "b-error", "error")
    for max_concurrency in (1, 2):  # 串行与进程池两条路径
        run_dir = tmp_path / f"run-{max_concurrency}"
        runner = RuleRunner(
            rules_root,
            RuleRuntimeSettings(max_concurrency=max_concurrency, wall_timeout_seconds=5, cpu_timeout_seconds=5),
        )
        candidates, failures = runner.run_all(run_dir, {})
        result_ids = sorted(path.stem for path in (run_dir / "rule-results").glob("*.json"))
        assert result_ids == ["a-rule", "b-error"]
        assert [failure["rule_id"] for failure in failures] == ["b-error"]
        completed = json.loads((run_dir / "rule-results" / "a-rule.json").read_text("utf-8"))
        failed = json.loads((run_dir / "rule-results" / "b-error.json").read_text("utf-8"))
        assert completed["status"] == "completed"
        assert failed["status"] != "completed"
        assert [candidate["rule_id"] for candidate in candidates] == ["a-rule"]
