"""Coverage-domain modeling and claim-aware gap propagation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Mapping, TypedDict

ClaimImpact = Literal["positive_proof", "negative_proof", "both"]


class CoverageDomain(TypedDict, total=False):
    scope: str
    file: str
    class_name: str
    method: str
    component: str
    rule: str
    operation: str


class CoverageGap(TypedDict, total=False):
    code: str
    message: str
    scope: str
    domain: CoverageDomain
    claim_impact: ClaimImpact
    affects_positive_proof: bool
    affects_negative_proof: bool
    critical: bool


def normalize_coverage_gap(
    gap: Mapping[str, Any] | Any,
    *,
    scope: str,
    default_impact: ClaimImpact = "both",
) -> CoverageGap:
    """Return one stable, claim-aware gap while retaining producer details.

    ``critical`` is deliberately meaningful only for positive proof: a gap that affects negative
    proof alone prevents a confident clean/refuted conclusion but must not downgrade an otherwise
    closed positive chain. Missing legacy impact metadata falls back conservatively to ``default_impact``.
    """

    if not isinstance(gap, Mapping):
        gap = {"code": "COVERAGE_GAP", "message": str(gap)}
    result: dict[str, Any] = dict(gap)
    domain = dict(result.get("domain") or {})
    coverage_domain = result.get("coverage_domain")
    if isinstance(coverage_domain, Mapping):
        for source, target in (
            ("rule_id", "rule"),
            ("component_name", "component"),
            ("path", "file"),
            ("operation", "operation"),
        ):
            if coverage_domain.get(source) and not domain.get(target):
                domain[target] = str(coverage_domain[source])
    aliases = {
        "scope": ("scope_id", "scope_key"),
        "file": ("file", "path"),
        "class_name": ("class_name", "qualified_class"),
        "method": ("method", "method_id", "caller_method_id"),
        "component": ("component_name", "component"),
        "rule": ("rule_id",),
        "operation": ("operation", "authorization_operation"),
    }
    for canonical, fields in aliases.items():
        if domain.get(canonical):
            continue
        value = next((result.get(field) for field in fields if result.get(field)), None)
        if value is not None:
            domain[canonical] = str(value)
    impact = result.get("claim_impact")
    if impact not in {"positive_proof", "negative_proof", "both"}:
        positive = result.get("affects_positive_proof")
        negative = result.get("affects_negative_proof")
        if positive is True and negative is not True:
            impact = "positive_proof"
        elif negative is True and positive is not True:
            impact = "negative_proof"
        else:
            impact = default_impact
    affects_positive = impact in {"positive_proof", "both"}
    affects_negative = impact in {"negative_proof", "both"}
    result.update({
        "scope": scope,
        "domain": domain,
        "claim_impact": impact,
        "affects_positive_proof": affects_positive,
        "affects_negative_proof": affects_negative,
        "critical": bool(result.get("critical", affects_positive)) and affects_positive,
    })
    result.setdefault("code", "COVERAGE_GAP")
    return result  # type: ignore[return-value]


def _skipped_exported_components(
    skipped_files: list[dict[str, Any]],
    manifest_components: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """S7：映射索引跳过文件 → 导出 manifest 组件（按源码路径前缀）。"""

    skipped_paths = {
        str(item.get("path") or "")
        for item in skipped_files
        if str(item.get("path") or "")
    }
    if not skipped_paths:
        return []
    result: list[dict[str, Any]] = []
    for component in manifest_components:
        if component.get("exported") != "true":
            continue
        name = str(component.get("name") or "")
        source_key = name.split("$", 1)[0].replace(".", "/")
        if not source_key:
            continue
        matched = sorted(
            path for path in skipped_paths
            if path.startswith(f"{source_key}/") or path == f"{source_key}.java"
        )
        if matched:
            result.append({"component_name": name, "skipped_paths": matched})
    return result


def finalize_run_coverage(
    candidates: list[dict[str, Any]],
    jadx_gaps: list[Any],
    rule_failures: list[Any],
    code_index: dict[str, Any],
    rule_component_gaps: list[dict[str, Any]] | None = None,
    manifest_components: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build run gaps and propagate only gaps whose domain maps to a candidate.

    Domainless run gaps participate in global negative-proof completeness, but are not copied onto
    every candidate as positive-proof blockers. AI failure on a deterministically closed chain is
    likewise negative-proof-only; unresolved chains remain blocked for both claim directions.
    """

    artifact_gaps: list[dict[str, Any]] = []
    for raw in jadx_gaps:
        source = raw if isinstance(raw, Mapping) else {"code": "JADX_COVERAGE_GAP", "message": str(raw)}
        has_domain = _has_mappable_domain(source)
        artifact_gaps.append(normalize_coverage_gap(
            source,
            scope="run",
            default_impact="both" if has_domain else "negative_proof",
        ))

    skipped_files = list(code_index.get("skipped_files") or [])
    skipped_file_count = int(code_index.get("stats", {}).get("skipped_file_count") or len(skipped_files))
    skipped_gap: dict[str, Any] | None = None
    if skipped_file_count > 0:
        skipped_gap = normalize_coverage_gap({
            "code": "INDEX_FILES_SKIPPED",
            "skipped_file_count": skipped_file_count,
            "skipped_files": skipped_files,
            "message": f"代码索引跳过 {skipped_file_count} 个文件",
            "claim_impact": "both",
        }, scope="run")
        artifact_gaps.append(skipped_gap)
    # S7（2026-08-16）：把"因 JADX 失败/索引跳过未被完整分析的导出组件"显式
    # 暴露到 run 汇总与组件级 coverage gap，杜绝静默漏检（无法区分"无漏洞"与
    # "没看到"）。
    skipped_exported = _skipped_exported_components(skipped_files, manifest_components or [])
    if skipped_exported:
        artifact_gaps.append(normalize_coverage_gap({
            "code": "JADX_SKIPPED_EXPORTED_COMPONENTS",
            "skipped_exported_component_count": len(skipped_exported),
            "components": skipped_exported,
            "message": f"{len(skipped_exported)} 个导出组件因 JADX 失败/索引跳过未被完整分析",
            "claim_impact": "both",
        }, scope="run"))

    rule_gaps = [
        normalize_coverage_gap({
            "code": "RULE_PRESCAN_PARTIAL",
            "rule_id": failure.get("rule_id") if isinstance(failure, Mapping) else None,
            "coverage_domain": failure.get("coverage_domain") if isinstance(failure, Mapping) else None,
            "message": (
                f"规则 {failure.get('rule_id')} 执行失败" if isinstance(failure, Mapping)
                else "规则执行失败"
            ),
            "claim_impact": "both",
        }, scope="rule")
        for failure in rule_failures
    ]
    component_gaps = [
        normalize_coverage_gap(gap, scope="component", default_impact="both")
        for gap in (rule_component_gaps or [])
    ]
    component_gaps.extend(
        normalize_coverage_gap({
            "code": "RULE_COMPONENT_PARTIAL",
            "component_name": item["component_name"],
            "skipped_paths": item["skipped_paths"],
            "message": f"导出组件 {item['component_name']} 因 JADX 失败/索引跳过未被完整分析",
            "claim_impact": "both",
        }, scope="component", default_impact="both")
        for item in skipped_exported
    )

    statuses = [
        candidate.get("analysis_status") for candidate in candidates
        if candidate.get("evidence_level") == "L2"
    ]
    ai_run_gaps: list[dict[str, Any]] = []
    for status, code, label in (
        ("ai_failed", "AI_ANALYSIS_FAILED", "失败"),
        ("ai_skipped", "AI_ANALYSIS_SKIPPED", "跳过"),
        ("ai_incomplete", "AI_ANALYSIS_INCOMPLETE", "未完成"),
    ):
        count = statuses.count(status)
        if count:
            ai_run_gaps.append(normalize_coverage_gap({
                "code": code,
                "count": count,
                "message": f"{count} 个 L2 候选的 AI 分析{label}",
                "claim_impact": "negative_proof",
            }, scope="ai", default_impact="negative_proof"))

    run_gaps = _merge_dict_list([*artifact_gaps, *rule_gaps, *component_gaps, *ai_run_gaps])
    global_negative_gaps = [
        gap for gap in run_gaps
        if gap.get("affects_negative_proof") is True and not gap.get("domain")
    ]
    for candidate in candidates:
        candidate_gaps = [
            normalize_coverage_gap(gap, scope="candidate")
            if isinstance(gap, Mapping) else normalize_coverage_gap(gap, scope="candidate")
            for gap in candidate.get("coverage_gaps", [])
        ]
        for gap in artifact_gaps:
            if gap.get("code") == "INDEX_FILES_SKIPPED":
                if skipped_gap and _candidate_depends_on_skipped_files(candidate, skipped_files):
                    candidate_gaps.append(normalize_coverage_gap(
                        {**skipped_gap, "domain": _candidate_domain(candidate)}, scope="candidate"
                    ))
            elif gap.get("domain") and gap_applies_to_candidate(gap, candidate):
                candidate_gaps.append(normalize_coverage_gap(gap, scope="candidate"))
        candidate_gaps.extend(
            normalize_coverage_gap(gap, scope="candidate")
            for gap in [*rule_gaps, *component_gaps]
            if gap_applies_to_candidate(gap, candidate)
        )
        status = candidate.get("analysis_status")
        if status in {"ai_failed", "ai_skipped", "ai_incomplete"}:
            closed = candidate.get("deterministic_chain_verified") is True
            candidate_gaps.append(normalize_coverage_gap({
                "code": {
                    "ai_failed": "AI_ANALYSIS_FAILED",
                    "ai_skipped": "AI_ANALYSIS_SKIPPED",
                    "ai_incomplete": "AI_ANALYSIS_INCOMPLETE",
                }[status],
                "message": candidate.get("ai_skip_reason") or candidate.get("ai_stop_reason") or f"当前候选 {status}",
                "domain": _candidate_domain(candidate),
                "claim_impact": "negative_proof" if closed else "both",
            }, scope="candidate"))

        candidate["coverage_gaps"] = _merge_dict_list(candidate_gaps)
        candidate["positive_proof_coverage_complete"] = not any(
            gap.get("affects_positive_proof") is True for gap in candidate["coverage_gaps"]
        )
        candidate["negative_proof_coverage_complete"] = not global_negative_gaps and not any(
            gap.get("affects_negative_proof") is True for gap in candidate["coverage_gaps"]
        )
        candidate["analysis_incomplete"] = bool(candidate["coverage_gaps"])
    return run_gaps


def gap_applies_to_candidate(gap: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    domain = gap.get("domain")
    if not isinstance(domain, Mapping) or not domain:
        return False
    candidate_domain = _candidate_domain(candidate)
    for key, expected in domain.items():
        if expected in {None, ""}:
            continue
        if key == "file":
            if str(expected) not in _candidate_files(candidate):
                return False
        elif key == "method":
            if str(expected) not in _candidate_methods(candidate):
                return False
        elif key == "rule":
            rule_ids = {str(candidate.get("rule_id") or ""), *(str(item) for item in candidate.get("rule_ids", []) or [])}
            if str(expected) not in rule_ids:
                return False
        elif key == "class_name":
            if str(expected) != str(candidate.get("component_name") or "") and str(expected) not in _candidate_classes(candidate):
                return False
        elif str(candidate_domain.get(key) or "") != str(expected):
            return False
    return True


def coverage_allows(candidate: Mapping[str, Any], claim: Literal["positive_proof", "negative_proof"]) -> bool:
    """Fail closed only for gaps that affect the requested claim direction.

    Explicit finalized coverage flags take precedence. Legacy gaps without claim metadata remain
    conservative when marked critical, preserving compatibility without treating negative-only gaps
    as positive-proof blockers.
    """

    explicit = candidate.get(f"{claim}_coverage_complete")
    if explicit is not None:
        return explicit is True
    impact_field = "affects_positive_proof" if claim == "positive_proof" else "affects_negative_proof"
    for gap in candidate.get("coverage_gaps", []) or []:
        if not isinstance(gap, Mapping):
            return False
        if gap.get(impact_field) is True:
            return False
        impact = gap.get("claim_impact")
        if impact in {claim, "both"}:
            return False
        if impact is None and gap.get("critical", True):
            return False
    return True


def _has_mappable_domain(gap: Mapping[str, Any]) -> bool:
    if isinstance(gap.get("domain"), Mapping) and gap.get("domain"):
        return True
    return any(gap.get(field) for field in (
        "scope_id", "scope_key", "file", "path", "class_name", "qualified_class",
        "method", "method_id", "caller_method_id", "component_name", "component",
        "rule_id", "operation", "authorization_operation",
    ))


def _candidate_domain(candidate: Mapping[str, Any]) -> CoverageDomain:
    result: CoverageDomain = {}
    values = {
        "scope": candidate.get("scope_id") or candidate.get("scope_key"),
        "component": candidate.get("component_name") or candidate.get("component"),
        "rule": candidate.get("rule_id"),
        "operation": candidate.get("authorization_operation"),
    }
    for key, value in values.items():
        if value:
            result[key] = str(value)  # type: ignore[literal-required]
    return result


def _candidate_files(candidate: Mapping[str, Any]) -> set[str]:
    paths: set[str] = set()
    for field in ("locations", "sources", "sinks", "propagation_paths"):
        for item in candidate.get(field, []) or []:
            if isinstance(item, Mapping) and item.get("path"):
                paths.add(str(item["path"]))
    component = str(candidate.get("component_name") or "").split("$", 1)[0]
    if component:
        source = component.replace(".", "/")
        paths.update({f"{source}.java", f"{source}.kt"})
    return paths


def _candidate_methods(candidate: Mapping[str, Any]) -> set[str]:
    methods: set[str] = set()
    for field in ("locations", "sources", "sinks", "propagation_paths"):
        for item in candidate.get(field, []) or []:
            if not isinstance(item, Mapping):
                continue
            for key in ("method", "method_id", "resolved_target_id", "symbol_key"):
                if item.get(key):
                    methods.add(str(item[key]))
    methods.update(str(item) for item in candidate.get("entry_points", []) or [])
    return methods


def _candidate_classes(candidate: Mapping[str, Any]) -> set[str]:
    values = {str(candidate.get("component_name") or "")}
    for field in ("locations", "sources", "sinks", "propagation_paths"):
        for item in candidate.get(field, []) or []:
            if isinstance(item, Mapping):
                values.update(str(item.get(key) or "") for key in ("class_name", "qualified_class"))
    return {value for value in values if value}


def _candidate_depends_on_skipped_files(candidate: Mapping[str, Any], skipped_files: list[dict[str, Any]]) -> bool:
    skipped_paths = {str(item.get("path") or "") for item in skipped_files}
    if _candidate_files(candidate).intersection(skipped_paths):
        return True
    skipped_source_keys = {
        str(Path(path).with_suffix("")).replace("\\", "/").split("$", 1)[0]
        for path in skipped_paths if path
    }
    component_name = str(candidate.get("component_name") or "")
    component_source_key = component_name.split("$", 1)[0].replace(".", "/")
    if component_source_key and component_source_key in skipped_source_keys:
        return True
    component_package = component_name.rsplit(".", 1)[0] if "." in component_name else ""
    for return_type in candidate.get("binder_return_types", []) or []:
        normalized = str(return_type).replace("$", ".")
        if normalized and normalized.split(".", 1)[0][:1].isupper() and component_package:
            normalized = f"{component_package}.{normalized}"
        type_key = normalized.replace(".", "/")
        possible = {type_key, type_key.rsplit("/", 1)[0] if "/" in type_key else type_key}
        if possible.intersection(skipped_source_keys):
            return True
    return False


def _merge_dict_list(items: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for item in items:
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, dict) else str(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result
