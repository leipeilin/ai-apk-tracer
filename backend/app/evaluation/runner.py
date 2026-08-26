"""Offline golden evaluation runner and JSON-only CLI."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .golden import GoldenDataset, load_golden_dataset
from .metrics import ActualResult, calculate_metrics

DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[3] / "evaluation" / "golden" / "v1" / "manifest.json"
)

RUNS_ROOT = Path(__file__).resolve().parents[3] / ".ai-apk-tracer" / "runs"


def _hop_ids_text(chain_proposal: Mapping[str, Any]) -> str:
    """候选全部 hop 的 from/to method_id 拼接（explorer_hit 同口径——R-1）。"""
    return " ".join(
        f"{hop.get('from_method_id', '')} {hop.get('to_method_id', '')}"
        for hop in chain_proposal.get("hops") or [] if isinstance(hop, Mapping)
    )


def evaluate_explorer_against_golden(
    run_dir: Path, cases: list
) -> dict[str, Any]:
    """探索候选 ↔ golden 标注命中率（M4-T4.2——T4.1 explorer_hit 消费）。

    conditional 命中经 matches 直调（explorer_hit 对 conditional 恒 False
    ——评审 R-1）；candidates.json 缺失 → proposals_total=0 容错（A-3）。
    """

    candidates_path = run_dir / "explorer" / "candidates.json"
    proposals: list[Mapping[str, Any]] = []
    if candidates_path.is_file():
        try:
            loaded = json.loads(candidates_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            loaded = []
        proposals = [
            item["chain_proposal"] for item in loaded
            if isinstance(item, dict) and isinstance(item.get("chain_proposal"), Mapping)
        ]

    hit_cases = [
        case for case in cases
        if case.explorer_expected and case.explorer_expected.expectation == "hit"
    ]
    conditional_cases = [
        case for case in cases
        if case.explorer_expected and case.explorer_expected.expectation == "conditional"
    ]

    def _matched(case) -> bool:
        for proposal in proposals:
            if case.explorer_expected.matches(
                str(proposal.get("source") or ""),
                str(proposal.get("sink") or ""),
                _hop_ids_text(proposal),
            ):
                return True
        return False

    hit_ids = sorted(case.id for case in hit_cases if _matched(case))
    conditional_ids = sorted(case.id for case in conditional_cases if _matched(case))
    return {
        "run_id": run_dir.name,
        "proposals_total": len(proposals),
        "explorer_hit_total": len(hit_cases),
        "explorer_hits": hit_ids,
        "explorer_hit_rate": (
            len(hit_ids) / len(hit_cases) if hit_cases else None
        ),
        "conditional_total": len(conditional_cases),
        "conditional_hits": conditional_ids,
        "conditional_hit_rate": (
            len(conditional_ids) / len(conditional_cases) if conditional_cases else None
        ),
    }


def summarize_run_costs(run_dir: Path) -> dict[str, Any]:
    """三本账 + wall-time（manifest stages 提取——字段缺失容错 N-2）。"""

    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return {"run_id": run_dir.name, "error": "manifest_missing"}
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"run_id": run_dir.name, "error": "manifest_invalid"}

    stages = {str(s.get("name")): (s.get("summary") or {}) for s in manifest.get("stages") or []}
    explorer = stages.get("explorer") or {}
    ai = stages.get("ai_analysis") or {}
    aggregation = stages.get("aggregation") or {}
    wall_seconds = None
    try:
        from datetime import datetime

        created = datetime.fromisoformat(str(manifest.get("created_at")))
        completed = datetime.fromisoformat(str(manifest.get("completed_at")))
        wall_seconds = round((completed - created).total_seconds(), 1)
    except (TypeError, ValueError):
        pass
    return {
        "run_id": run_dir.name,
        "explorer_requests": explorer.get("ai_requests_used"),
        "deep_dive_requests": explorer.get("deep_dive_requests_used"),
        "read_requests": explorer.get("read_requests_used"),
        "verify_requests": ai.get("verify_requests_used"),
        "ai_stage_requests": ai.get("ai_stage_requests_used"),
        "total_requests": ai.get("requests_used"),
        "finding_count": aggregation.get("finding_count"),
        "wall_seconds": wall_seconds,
    }


def evaluate_runs(
    run_dirs: list[Path], manifest_path: str | Path = DEFAULT_MANIFEST
) -> dict[str, Any]:
    """多 run 聚合（M4-T4.2——加权命中率 + 总三本账，评审 R-4）。

    rate=None 的 run（无 hit case）剔除聚合并列入 unaggregated_runs。
    """

    dataset = load_golden_dataset(manifest_path)
    per_run = []
    total_hits = 0
    total_hit_cases = 0
    total_conditional = 0
    total_conditional_cases = 0
    unaggregated: list[str] = []
    costs_total: dict[str, int] = {}
    wall_total = 0.0
    for run_dir in run_dirs:
        explorer_metrics = evaluate_explorer_against_golden(run_dir, dataset.cases)
        costs = summarize_run_costs(run_dir)
        per_run.append({"explorer": explorer_metrics, "costs": costs})
        if explorer_metrics["explorer_hit_rate"] is None:
            unaggregated.append(run_dir.name)
        else:
            total_hits += len(explorer_metrics["explorer_hits"])
            total_hit_cases += explorer_metrics["explorer_hit_total"]
        total_conditional += len(explorer_metrics["conditional_hits"])
        total_conditional_cases += explorer_metrics["conditional_total"]
        for key in ("explorer_requests", "deep_dive_requests", "read_requests",
                    "verify_requests", "ai_stage_requests", "total_requests"):
            value = costs.get(key)
            if isinstance(value, int):
                costs_total[key] = costs_total.get(key, 0) + value
        if isinstance(costs.get("wall_seconds"), (int, float)):
            wall_total += float(costs["wall_seconds"])
    return {
        "dataset_version": dataset.manifest.dataset_version,
        "runs_total": len(run_dirs),
        "per_run": per_run,
        "aggregate": {
            "explorer_hit_rate": (
                total_hits / total_hit_cases if total_hit_cases else None
            ),
            "explorer_hits_total": total_hits,
            "explorer_hit_cases_total": total_hit_cases,
            "conditional_hit_rate": (
                total_conditional / total_conditional_cases
                if total_conditional_cases else None
            ),
            "costs_total": costs_total,
            "wall_seconds_total": round(wall_total, 1),
            "unaggregated_runs": unaggregated,
        },
    }


def evaluate_results(
    dataset: GoldenDataset,
    raw_results: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate actual results and report omissions without scoring them.

    Partial submissions are useful for iteration, but a complete published run
    must require ``missing_actual_count == 0``.
    """

    known_ids = set(dataset.by_id())
    unknown_ids = sorted(set(raw_results) - known_ids)
    if unknown_ids:
        raise ValueError(f"actual results contain unknown case ids: {', '.join(unknown_ids)}")

    actual = {
        case_id: ActualResult.model_validate(result)
        for case_id, result in raw_results.items()
    }
    missing_actual_ids = sorted(known_ids - set(actual))
    return {
        "schema_version": dataset.manifest.schema_version,
        "dataset_version": dataset.manifest.dataset_version,
        "case_count": len(dataset.cases),
        "submitted_result_count": len(actual),
        "missing_actual_count": len(missing_actual_ids),
        "missing_actual_ids": missing_actual_ids,
        "metrics": calculate_metrics(dataset.cases, actual),
    }


def run(manifest_path: str | Path, results_path: str | Path) -> dict[str, Any]:
    dataset = load_golden_dataset(manifest_path)
    raw_results = _load_results(results_path)
    return evaluate_results(dataset, raw_results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline golden-set evaluation")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--results",
        help="case_id to result JSON file, or - for stdin（离线规则轨模式）",
    )
    mode.add_argument(
        "--runs",
        help="逗号分隔 run 目录名（run 产物评估模式——探索轨指标 + 三本账 + wall-time，M4-T4.2）",
    )
    args = parser.parse_args(argv)
    try:
        if args.runs:
            run_dirs = [
                RUNS_ROOT / name.strip() for name in args.runs.split(",") if name.strip()
            ]
            for run_dir in run_dirs:
                if not run_dir.is_dir():
                    parser.error(f"run 目录不存在: {run_dir}")
            report = evaluate_runs(run_dirs, args.manifest)
        else:
            report = run(args.manifest, args.results)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def _load_results(path: str | Path) -> dict[str, Any]:
    if str(path) == "-":
        value = json.load(sys.stdin, object_pairs_hook=_reject_duplicate_keys)
    else:
        result_path = Path(path)
        if not result_path.is_file():
            raise FileNotFoundError(f"actual results file not found: {result_path}")
        with result_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(value, dict):
        raise TypeError("actual results JSON root must be an object")
    if not all(isinstance(key, str) and isinstance(item, dict) for key, item in value.items()):
        raise TypeError("actual results must map string case ids to result objects")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


if __name__ == "__main__":
    raise SystemExit(main())
