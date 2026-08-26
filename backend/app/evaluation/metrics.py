"""Pure metric calculations for offline golden-case evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field, StrictBool

from .golden import (
    AuthorizationStatus,
    CaseLabel,
    DataflowStatus,
    GoldenCase,
    GuardStatus,
    StrictModel,
    Verdict,
)


class ActualResult(StrictModel):
    candidate: StrictBool
    dataflow: DataflowStatus | None = None
    auth: AuthorizationStatus | None = None
    guard: GuardStatus | None = None
    taxonomy: str | None = None
    verdict: Verdict | None = None
    reports: list[str] = Field(default_factory=list)


_GRADEABLE_UNKNOWN = {"unknown", "not_applicable"}
_CLASSIFICATION_FIELDS = ("dataflow", "auth", "guard", "taxonomy", "verdict")


def calculate_metrics(
    cases: Sequence[GoldenCase],
    actual: Mapping[str, ActualResult],
    *,
    include_categories: bool = True,
) -> dict[str, Any]:
    """Calculate binary, classification, and forbidden-report metrics.

    Conditional and unknown labels are excluded from TP/FP/TN/FN. Cases without
    an actual result are excluded from every metric denominator and reported by
    ID. ``must_not_report`` violations require an exact report-string match;
    provenance references are audit metadata only and never affect scoring.
    """

    metrics = _calculate_slice(cases, actual)
    if include_categories:
        categories = sorted({case.category for case in cases})
        metrics["by_category"] = {
            category: _calculate_slice(
                [case for case in cases if case.category == category], actual
            )
            for category in categories
        }
    return metrics


def _calculate_slice(
    cases: Sequence[GoldenCase], actual: Mapping[str, ActualResult]
) -> dict[str, Any]:
    tp = fp = tn = fn = 0
    label_counts = {label.value: 0 for label in CaseLabel}
    field_counts = {
        field: {"correct": 0, "total": 0, "accuracy": 0.0}
        for field in _CLASSIFICATION_FIELDS
    }
    violations: list[dict[str, str]] = []
    missing_actual_ids: list[str] = []

    for case in cases:
        label_counts[case.label.value] += 1
        result = actual.get(case.id)
        if result is None:
            missing_actual_ids.append(case.id)
            continue
        if case.label is CaseLabel.POSITIVE:
            if result.candidate:
                tp += 1
            else:
                fn += 1
        elif case.label is CaseLabel.NEGATIVE:
            if result.candidate:
                fp += 1
            else:
                tn += 1

        for field in _CLASSIFICATION_FIELDS:
            expected = getattr(case.expected, field)
            expected_value = expected.value if hasattr(expected, "value") else expected
            if expected_value is None or expected_value in _GRADEABLE_UNKNOWN:
                continue
            observed = getattr(result, field)
            observed_value = observed.value if hasattr(observed, "value") else observed
            field_counts[field]["total"] += 1
            if observed_value == expected_value:
                field_counts[field]["correct"] += 1

        prohibited = set(case.must_not_report)
        for report in result.reports:
            if report in prohibited:
                violations.append({"case_id": case.id, "report": report})

    for counts in field_counts.values():
        counts["accuracy"] = _ratio(counts["correct"], counts["total"])
    classification_correct = sum(item["correct"] for item in field_counts.values())
    classification_total = sum(item["total"] for item in field_counts.values())
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)

    missing_actual_ids.sort()
    return {
        "case_count": len(cases),
        "missing_actual_count": len(missing_actual_ids),
        "missing_actual_ids": missing_actual_ids,
        "label_counts": label_counts,
        "candidate": {
            "tp": tp,
            "fp": fp,
            "tn": tn,
            "fn": fn,
            "excluded_conditional_unknown": (
                label_counts[CaseLabel.CONDITIONAL.value]
                + label_counts[CaseLabel.UNKNOWN.value]
            ),
            "precision": precision,
            "recall": recall,
            "f1": _ratio(2 * precision * recall, precision + recall),
        },
        "known_positive_recall": recall,
        "known_negative_leakage": _ratio(fp, fp + tn),
        "classification_accuracy": _ratio(classification_correct, classification_total),
        "classification": {
            "correct": classification_correct,
            "total": classification_total,
            "accuracy": _ratio(classification_correct, classification_total),
            "by_field": field_counts,
        },
        "must_not_report": {
            "violation_count": len(violations),
            "violations": violations,
        },
    }


def _ratio(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 6)
