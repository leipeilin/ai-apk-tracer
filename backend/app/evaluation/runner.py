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
    parser.add_argument(
        "--results",
        required=True,
        help="case_id to result JSON file, or - for stdin",
    )
    args = parser.parse_args(argv)
    try:
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
        raise ValueError("actual results JSON root must be an object")
    if not all(isinstance(key, str) and isinstance(item, dict) for key, item in value.items()):
        raise ValueError("actual results must map string case ids to result objects")
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
