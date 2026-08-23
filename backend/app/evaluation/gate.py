"""M4-T4.4 优化门槛：golden 指标不劣于基线才可默认开启。

白名单点路径（评审 R-1——precision/recall/f1 实际位于 metrics.candidate
两层嵌套，by_category 每类别重复一套，自动发现必误判）：
- ``aggregate.explorer_hit_rate`` / ``aggregate.conditional_hit_rate``
  （evaluate_runs 输出）
- ``metrics.candidate.precision`` / ``.recall`` / ``.f1``
  （evaluate_results 输出）

语义（评审认可：守卫回归不守卫演进）：
- baseline 缺指标 → SKIP（新指标须先刷新基线再启用门槛——文档硬性步骤，R-4）
- current 缺指标 → BLOCK（基线有而当前无 = 劣化）
- current < baseline - tolerance → BLOCK；否则 ALLOW
- 两报告结构混用（键集全不交）→ BLOCK（结构校验，R-4）

CLI：``python -m backend.app.evaluation.gate --current x.json --baseline y.json
[--tolerance f1=0.02,...]``——退出码 0=ALLOW / 1=BLOCK（项目惯例）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# 白名单指标点路径（R-1）
_METRIC_PATHS: tuple[str, ...] = (
    "aggregate.explorer_hit_rate",
    "aggregate.conditional_hit_rate",
    "metrics.candidate.precision",
    "metrics.candidate.recall",
    "metrics.candidate.f1",
)


def _resolve(document: Mapping[str, Any], path: str) -> Any:
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def compare_against_baseline(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
    tolerances: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """白名单指标对比（评审 R-1~R-6 修订后的最终语义）。"""

    tolerance_map = {k: float(v) for k, v in (tolerances or {}).items()}
    comparisons: list[dict[str, Any]] = []
    skipped: list[str] = []
    seen_any = False
    for path in _METRIC_PATHS:
        base_value = _resolve(baseline, path)
        if base_value is None:
            skipped.append(path)
            continue
        seen_any = True
        current_value = _resolve(current, path)
        if current_value is None:
            comparisons.append({
                "metric": path, "baseline": base_value, "current": None,
                "deficit": None, "verdict": "BLOCK", "reason": "current 缺指标",
            })
            continue
        tolerance = tolerance_map.get(path.split(".")[-1], 0.0)
        deficit = round(float(base_value) - float(current_value), 10)
        blocked = deficit > tolerance
        comparisons.append({
            "metric": path, "baseline": base_value, "current": current_value,
            "deficit": deficit, "verdict": "BLOCK" if blocked else "ALLOW",
            "reason": None if not blocked else f"劣化超容差（tol={tolerance}）",
        })
    # 结构校验（R-4：键集全不交 = 报告结构混用）
    if not seen_any and skipped == list(_METRIC_PATHS):
        return {
            "gate": "BLOCK",
            "reason": "baseline 与白名单指标全不匹配——检查报告结构（evaluate_runs vs evaluate_results）",
            "comparisons": [], "skipped": skipped,
        }
    gate = "BLOCK" if any(c["verdict"] == "BLOCK" for c in comparisons) else "ALLOW"
    return {"gate": gate, "comparisons": comparisons, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="优化门槛：golden 指标不劣于基线才可默认开启")
    parser.add_argument("--current", required=True, type=Path, help="当前评估报告 JSON")
    parser.add_argument("--baseline", required=True, type=Path, help="基线快照 JSON")
    parser.add_argument(
        "--tolerance", default=None,
        help="容差覆写（如 f1=0.02,recall=0.01——按指标短名）",
    )
    args = parser.parse_args(argv)
    try:
        current = json.loads(args.current.read_text("utf-8"))
        baseline = json.loads(args.baseline.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"报告读取失败: {exc}")
    tolerances: dict[str, float] = {}
    if args.tolerance:
        for chunk in args.tolerance.split(","):
            name, _, value = chunk.partition("=")
            try:
                tolerances[name.strip()] = float(value)
            except ValueError:
                parser.error(f"非法容差: {chunk!r}（期望 形如 f1=0.02）")  # M3/M4 审查 4.6
    result = compare_against_baseline(current, baseline, tolerances)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["gate"] == "ALLOW" else 1


if __name__ == "__main__":
    sys.exit(main())
