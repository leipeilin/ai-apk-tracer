"""校验证据位置，并按确定性条件控制候选的证据晋级。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.analysis.index_store import SQLiteCodeIndexReader

_DATAFLOW_RANK = {
    "not_applicable": 0,
    "not_proven": 0,
    "intraprocedural": 1,
    "interprocedural": 2,
    "verified": 3,
}


def verify_candidate(
    candidate: dict[str, Any],
    code_index: dict[str, Any],
    index_reader: SQLiteCodeIndexReader | None = None,
) -> dict[str, Any]:
    """回查位置、Source 与 Sink，仅在候选自身证据闭合时允许晋级 L3。"""

    indexed = {entry["path"]: entry for entry in code_index.get("files", [])}
    methods = _method_index(indexed, index_reader)
    expected_scope = candidate.get("scope_id") or candidate.get("scope_key")
    result = dict(candidate)
    result["blocking_gaps"] = list(candidate.get("blocking_gaps", []))

    valid_locations, invalid_locations = _verify_items(
        candidate.get("locations", []), indexed, index_reader, allow_manifest=True,
        expected_scope=expected_scope, methods=methods,
    )
    valid_sources, invalid_sources = _verify_items(
        candidate.get("sources", []), indexed, index_reader,
        expected_scope=expected_scope, methods=methods,
    )
    valid_sinks, invalid_sinks = _verify_items(
        candidate.get("sinks", []), indexed, index_reader,
        expected_scope=expected_scope, methods=methods,
    )
    valid_paths, invalid_paths = _verify_propagation_paths(
        candidate.get("propagation_paths", []), methods, expected_scope
    )
    result.update({
        "locations": valid_locations,
        "invalid_locations": invalid_locations,
        "sources": valid_sources,
        "invalid_sources": invalid_sources,
        "sinks": valid_sinks,
        "invalid_sinks": invalid_sinks,
        "propagation_paths": valid_paths,
        "invalid_propagation_paths": invalid_paths,
    })

    _record_invalid_gap(result, "EVIDENCE_LOCATION_NOT_FOUND", invalid_locations)
    _record_invalid_gap(result, "EVIDENCE_SOURCE_NOT_FOUND", invalid_sources)
    _record_invalid_gap(result, "EVIDENCE_SINK_NOT_FOUND", invalid_sinks)
    _record_invalid_gap(result, "EVIDENCE_PROPAGATION_TARGET_NOT_FOUND", invalid_paths)

    analysis = candidate.get("ai_analysis")
    if isinstance(analysis, Mapping):
        # 校验器专属 code（v3.0.5）：幂等合并时先剔除旧值再追加本次结果，防止
        # checkpoint 恢复/重试携带的残留 gap 拦截。AI 自标 gap 与其它来源 gap 原样保留。
        _VALIDATOR_GAP_CODES = {
            "AI_EVIDENCE_REF_INVALID",
            "AI_EVIDENCE_REF_REQUIRED",
            "AI_EVIDENCE_REQUIREMENTS_UNRESOLVED",
            "AI_EVIDENCE_SEMANTIC_INCOMPLETE",
        }
        contexts = _ai_evidence_contexts(candidate, indexed, methods, index_reader)
        ai_validation = validate_ai_evidence_references(result, contexts)
        result.update({
            key: value for key, value in ai_validation.items()
            if key != "ai_evidence_blocking_gaps"
        })
        result["ai_analysis"] = {
            **analysis,
            **{
                key: value for key, value in ai_validation.items()
                if key != "ai_evidence_blocking_gaps"
            },
        }
        # 幂等（v3.0.5）：ai_analysis.blocking_gaps 中校验器专属 code 旧值一并清除——
        # _applicable_critical_gap 同时读 ai_analysis.blocking_gaps 与顶层 ai_blocking_gaps，
        # 只清顶层仍会被残留污染（checkpoint 恢复/重试场景实测 35 候选仍 0 采信）。
        analysis_gaps = analysis.get("blocking_gaps", [])
        if isinstance(analysis_gaps, Sequence) and not isinstance(analysis_gaps, (str, bytes)):
            result["ai_analysis"] = {
                **result["ai_analysis"],
                "blocking_gaps": [
                    gap for gap in analysis_gaps
                    if not isinstance(gap, Mapping)
                    or gap.get("code") not in _VALIDATOR_GAP_CODES
                ],
            }
        ai_gaps = list(candidate.get("ai_blocking_gaps", []))
        ai_gaps = [
            gap for gap in ai_gaps
            if not isinstance(gap, Mapping) or gap.get("code") not in _VALIDATOR_GAP_CODES
        ]
        existing_codes = {
            gap.get("code") for gap in ai_gaps if isinstance(gap, Mapping)
        }
        for gap in ai_validation["ai_evidence_blocking_gaps"]:
            if gap.get("code") not in existing_codes:
                ai_gaps.append(gap)
                existing_codes.add(gap.get("code"))
        result["ai_blocking_gaps"] = ai_gaps

    original_level = candidate.get("evidence_level")
    if result.get("evidence_level") not in {"L1", "L2"}:
        result["evidence_level"] = "L2"
    requires_chain_evidence = result.get("evidence_level") == "L2"
    missing_required = [
        field for field, values in (
            ("locations", valid_locations),
            ("sources", valid_sources),
            ("sinks", valid_sinks),
        )
        if not values and (requires_chain_evidence or field == "locations")
    ]
    if missing_required:
        result["blocking_gaps"].append({
            "code": "EVIDENCE_REQUIRED_MISSING",
            "critical": True,
            "fields": missing_required,
        })
    evidence_complete = not missing_required
    critical_gap = any(
        not isinstance(gap, dict) or gap.get("critical", True)
        for gap in [
            *result.get("blocking_gaps", []),
            *result.get("coverage_gaps", []),
            *result.get("ai_blocking_gaps", []),
        ]
    )
    promotable_guard = result.get("guard_status") in {"absent", "present_bypassable"}
    authorization_gradeable = result.get("authorization_status") not in {"unknown", "protected", "strongly_protected"}
    dataflow_verified = _DATAFLOW_RANK.get(str(result.get("dataflow_status")), 0) >= _DATAFLOW_RANK["intraprocedural"]
    result["fact_integrity_status"] = (
        "invalid" if invalid_locations or invalid_sources or invalid_sinks or invalid_paths
        else "verified" if evidence_complete
        else "incomplete"
    )
    result["semantic_status"] = (
        "not_applicable" if result.get("evidence_level") == "L1"
        else "closed" if evidence_complete and result.get("deterministic_chain_verified") is True and dataflow_verified
        else "not_proven"
    )
    result["exploitability_status"] = (
        "dynamically_confirmed" if result.get("dynamic_validation_status") == "passed"
        else "statically_gradeable" if (
            result["semantic_status"] == "closed"
            and not critical_gap
            and promotable_guard
            and authorization_gradeable
        )
        else "pending"
    )
    promotion_requested = result.get("promotion_requested") is True or original_level == "L3"
    if (
        promotion_requested
        and result.get("evidence_level") == "L2"
        and result.get("deterministic_chain_verified") is True
        and dataflow_verified
        and valid_sources
        and valid_sinks
        and valid_locations
        and not critical_gap
        and promotable_guard
        and authorization_gradeable
    ):
        result["evidence_level"] = "L3"
        result.setdefault("promotion_reason", []).append("候选自身的确定性 Source→Sink 证据经回查确认")
    return result


def summarize_evidence_integrity(
    candidates: list[dict[str, Any]],
    findings: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """拆分事实完整性、闭合链和可定级数量，避免用单一 verified 误导。"""

    def count_items(field: str) -> tuple[int, int]:
        total = sum(len(candidate.get(field, [])) + len(candidate.get(f"invalid_{field}", [])) for candidate in candidates)
        verified = sum(len(candidate.get(field, [])) for candidate in candidates)
        return total, verified

    locations_total, locations_verified = count_items("locations")
    sources_total, sources_verified = count_items("sources")
    sinks_total, sinks_verified = count_items("sinks")
    closed = sum(candidate.get("semantic_status") == "closed" for candidate in candidates)
    gradeable_candidates = sum(candidate.get("exploitability_status") == "statically_gradeable" for candidate in candidates)
    gradeable_findings = sum(
        finding.get("exploitability_status") in {"statically_gradeable", "dynamically_confirmed"}
        and finding.get("severity") not in {"pending", "informational"}
        for finding in (findings or [])
    )
    pending_findings = sum(
        finding.get("severity") == "pending" or finding.get("exploitability_status") == "pending"
        for finding in (findings or [])
    )
    return {
        "candidates_checked": len(candidates),
        "locations_total": locations_total,
        "locations_verified": locations_verified,
        "sources_total": sources_total,
        "sources_verified": sources_verified,
        "sinks_total": sinks_total,
        "sinks_verified": sinks_verified,
        "deterministic_chains_closed": closed,
        "gradeable_candidates": gradeable_candidates,
        "gradeable_findings": gradeable_findings,
        "findings_pending_review": pending_findings,
    }


def _verify_items(
    items: list[Any],
    indexed: dict[str, dict[str, Any]],
    index_reader: SQLiteCodeIndexReader | None,
    *,
    allow_manifest: bool = False,
    expected_scope: Any = None,
    methods: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    methods = methods or {}
    for raw in items:
        if not isinstance(raw, dict):
            invalid.append({"value": raw, "verification": "missing", "reason": "EVIDENCE_NOT_OBJECT"})
            continue
        if not _scope_matches(raw, expected_scope):
            invalid.append({**raw, "verification": "missing", "reason": "SCOPE_ID_MISMATCH"})
            continue
        path = raw.get("path")
        line = _positive_line(raw.get("line"))
        if allow_manifest and raw.get("artifact") == "manifest":
            if path == "AndroidManifest.xml" and (raw.get("line") is None or line is not None):
                valid.append({**raw, "verification": "fact"})
            else:
                invalid.append({**raw, "verification": "missing", "reason": "MANIFEST_LOCATION_INVALID"})
            continue
        entry = indexed.get(path) if isinstance(path, str) and path else None
        if entry is None and index_reader and isinstance(path, str) and path:
            entry = index_reader.get_file_metadata(path)
        if not entry or line is None or line > int(entry["line_count"]):
            invalid.append({**raw, "verification": "missing", "reason": "LOCATION_NOT_INDEXED"})
            continue

        semantic_fields = any(raw.get(field) is not None for field in (
            "content_sha256", "quoted_text", "symbol_key", "method_id"
        ))
        if semantic_fields:
            content = _entry_content(entry, path, index_reader)
            failure = _semantic_failure(raw, entry, content, line, methods)
            if failure:
                invalid.append({**raw, "verification": "missing", "reason": failure})
                continue
        valid.append({**raw, "line": line, "verification": "fact"})
    return valid, invalid


def _semantic_failure(
    item: Mapping[str, Any],
    entry: Mapping[str, Any],
    content: str | None,
    line: int,
    methods: Mapping[str, dict[str, Any]],
) -> str | None:
    method_id = item.get("method_id")
    symbol_key = item.get("symbol_key")
    method = methods.get(str(method_id)) if method_id else None
    if method_id and method is None:
        return "METHOD_ID_NOT_FOUND"
    if symbol_key:
        symbol_matches = [value for value in methods.values() if value.get("symbol_key") == symbol_key]
        if not symbol_matches:
            return "SYMBOL_KEY_NOT_FOUND"
        if method is not None and method.get("symbol_key") != symbol_key:
            return "METHOD_SYMBOL_MISMATCH"
        if method is None:
            containing = [value for value in symbol_matches if _method_contains(value, entry, line)]
            if not containing:
                return "SYMBOL_LOCATION_MISMATCH"
            method = containing[0]
    if method is not None and not _method_contains(method, entry, line):
        return "METHOD_LOCATION_MISMATCH"
    if content is None:
        return "INDEX_CONTENT_UNAVAILABLE"

    lines = content.splitlines()
    quoted = item.get("quoted_text")
    if quoted is not None:
        quote = str(quoted)
        start = max(0, line - 2)
        end = min(len(lines), line + 1)
        if quote not in "\n".join(lines[start:end]):
            return "QUOTED_TEXT_MISMATCH"
    expected_hash = item.get("content_sha256")
    if expected_hash is not None:
        hashes = {
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
            str(entry.get("sha256") or ""),
        }
        if lines:
            hashes.add(hashlib.sha256(lines[line - 1].encode("utf-8")).hexdigest())
        if quoted is not None:
            hashes.add(hashlib.sha256(str(quoted).encode("utf-8")).hexdigest())
        if method is not None:
            start_line = int(method.get("start_line") or line)
            end_line = int(method.get("end_line") or line)
            snippet = "\n".join(lines[start_line - 1:end_line])
            hashes.add(hashlib.sha256(snippet.encode("utf-8")).hexdigest())
        if str(expected_hash) not in hashes:
            return "CONTENT_SHA256_MISMATCH"
    return None


def _verify_propagation_paths(
    items: list[Any],
    methods: Mapping[str, dict[str, Any]],
    expected_scope: Any,
) -> tuple[list[Any], list[dict[str, Any]]]:
    valid: list[Any] = []
    invalid: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            # Legacy path descriptions were not method-addressable and retain old behavior.
            valid.append(raw)
            continue
        if not _scope_matches(raw, expected_scope):
            invalid.append({**raw, "verification": "missing", "reason": "SCOPE_ID_MISMATCH"})
            continue
        identifiers = [raw.get("method_id"), raw.get("resolved_target_id")]
        missing = [str(value) for value in identifiers if value and str(value) not in methods]
        if missing:
            invalid.append({
                **raw,
                "verification": "missing",
                "reason": "PROPAGATION_METHOD_NOT_FOUND",
                "missing_method_ids": missing,
            })
            continue
        valid.append({**raw, "verification": "fact"} if any(identifiers) else raw)
    return valid, invalid


def _method_index(
    indexed: Mapping[str, dict[str, Any]],
    index_reader: SQLiteCodeIndexReader | None,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path, entry in indexed.items():
        for method in entry.get("methods", []):
            result[str(method["id"])] = {**method, "path": path}
    if index_reader is not None:
        result.update(index_reader.load_method_index())
    return result


def _entry_content(
    entry: Mapping[str, Any],
    path: Any,
    index_reader: SQLiteCodeIndexReader | None,
) -> str | None:
    if "content" in entry:
        return str(entry["content"])
    if index_reader is not None and isinstance(path, str):
        try:
            return index_reader.get_content(path)
        except KeyError:
            return None
    return None


def _method_contains(method: Mapping[str, Any], entry: Mapping[str, Any], line: int) -> bool:
    return (
        str(method.get("path") or entry.get("path") or "") == str(entry.get("path") or "")
        and int(method.get("start_line") or 0) <= line <= int(method.get("end_line") or 0)
    )


def _scope_matches(item: Mapping[str, Any], expected_scope: Any) -> bool:
    actual = item.get("scope_id") or item.get("scope_key")
    return actual is None or expected_scope is None or str(actual) == str(expected_scope)


def validate_ai_evidence_references(
    candidate: Mapping[str, Any],
    contexts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Purely validate AI references and the evidence domains needed by its conclusion."""

    analysis_value = candidate.get("ai_analysis")
    analysis = analysis_value if isinstance(analysis_value, Mapping) else candidate
    raw_refs = analysis.get("evidence_refs", [])
    context_by_id = {
        str(context["context_id"]): context
        for context in contexts
        if isinstance(context, Mapping) and context.get("context_id")
    }
    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    covered_roles: set[str] = set()
    covered_domains: set[str] = set()

    if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, (str, bytes)):
        raw_refs = []
        invalid.append({"value": analysis.get("evidence_refs"), "reason": "EVIDENCE_REFS_NOT_LIST"})

    for value in raw_refs:
        if not isinstance(value, Mapping):
            invalid.append({"value": value, "reason": "EVIDENCE_REF_NOT_OBJECT"})
            continue
        reference = dict(value)
        context_id = reference.get("context_id")
        context = context_by_id.get(str(context_id)) if context_id else None
        if context is None:
            invalid.append({**reference, "reason": "CONTEXT_ID_NOT_FOUND"})
            continue
        context_path = context.get("path")
        supplied_path = reference.get("path")
        if supplied_path is not None and supplied_path != context_path:
            invalid.append({**reference, "reason": "PATH_MISMATCH", "expected_path": context_path})
            continue

        line = _strict_positive_line(reference.get("line"))
        end_line = _strict_positive_line(reference.get("end_line"))
        concrete = _is_concrete_context(context)
        if reference.get("line") is not None and line is None:
            invalid.append({**reference, "reason": "LINE_INVALID"})
            continue
        if reference.get("end_line") is not None and end_line is None:
            invalid.append({**reference, "reason": "END_LINE_INVALID"})
            continue
        if concrete and line is None:
            invalid.append({**reference, "reason": "LINE_REQUIRED_FOR_CONCRETE_EVIDENCE"})
            continue
        if end_line is not None and line is None:
            invalid.append({**reference, "reason": "END_LINE_WITHOUT_LINE"})
            continue
        if line is not None and end_line is not None and end_line < line:
            invalid.append({**reference, "reason": "END_LINE_BEFORE_LINE"})
            continue
        if line is not None and not _reference_contained(context, line, end_line or line):
            invalid.append({**reference, "reason": "LINE_OUTSIDE_CONTEXT"})
            continue

        roles, domains = _reference_semantics(candidate, context, line, end_line or line)
        covered_roles.update(roles)
        covered_domains.update(domains)
        valid.append({
            **reference,
            "path": context_path,
            **({"line": line} if line is not None else {}),
            **({"end_line": end_line} if end_line is not None else {}),
            "evidence_roles": sorted(roles),
            "evidence_domains": sorted(domains),
            "verification": "fact",
        })

    required_roles, required_domains, requirements_known = _required_ai_evidence(candidate, analysis)
    missing_roles = sorted(required_roles - covered_roles)
    missing_domains = sorted(required_domains - covered_domains)
    missing_refs: list[dict[str, str]] = []
    if not valid:
        missing_refs.append({"type": "reference", "value": "any"})
    missing_refs.extend({"type": "role", "value": role} for role in missing_roles)
    missing_refs.extend({"type": "domain", "value": domain} for domain in missing_domains)
    gaps: list[dict[str, Any]] = []
    if invalid:
        gaps.append({"code": "AI_EVIDENCE_REF_INVALID", "critical": True, "count": len(invalid)})
    if not valid:
        gaps.append({"code": "AI_EVIDENCE_REF_REQUIRED", "critical": True})
    if not requirements_known:
        gaps.append({"code": "AI_EVIDENCE_REQUIREMENTS_UNRESOLVED", "critical": True})
    if missing_roles or missing_domains:
        gaps.append({
            "code": "AI_EVIDENCE_SEMANTIC_INCOMPLETE",
            "critical": True,
            "missing_roles": missing_roles,
            "missing_domains": missing_domains,
        })
    return {
        "verified_evidence_refs": valid,
        "invalid_evidence_refs": invalid,
        "evidence_refs_valid": bool(valid) and not invalid,
        "evidence_roles": sorted(covered_roles),
        "evidence_domains": sorted(covered_domains),
        "required_evidence_roles": sorted(required_roles),
        "required_evidence_domains": sorted(required_domains),
        "missing_evidence_refs": missing_refs,
        "missing_evidence_roles": missing_roles,
        "missing_evidence_domains": missing_domains,
        "semantic_evidence_complete": bool(valid) and requirements_known and not missing_roles and not missing_domains,
        "ai_evidence_blocking_gaps": gaps,
    }


def _required_ai_evidence(
    candidate: Mapping[str, Any], analysis: Mapping[str, Any]
) -> tuple[set[str], set[str], bool]:
    track = analysis.get("analysis_track") or candidate.get("analysis_track")
    level = candidate.get("evidence_level")
    if track == "finalization":
        track = (
            analysis.get("source_analysis_track")
            or candidate.get("source_analysis_track")
            or ("l1_triage" if level == "L1" else "l2_review" if level in {"L2", "L3"} else None)
        )
    verdict = (
        analysis.get("candidate_verdict")
        or analysis.get("verdict")
        or analysis.get("triage_disposition")
        or candidate.get("candidate_verdict")
    )
    if track == "l1_triage" and level == "L1":
        if verdict == "potential_chain":
            return {"location", "source", "sink"}, {"exposure", "dataflow", "impact"}, True
        if verdict in {"exposure_only", "insufficient"}:
            return {"location"}, {"exposure"}, True
        return {"location", "source", "sink"}, {"exposure", "dataflow", "impact"}, False
    if track in {"l2_review", "verify"} and level in {"L2", "L3"}:
        # verify（核验 agent，T2.12）按 l2_review 语义取证据需求——L2 agent 化
        # 演进的产物裁决口径与单轮 L2 一致（评审 R-2：不入分支会恒发
        # AI_EVIDENCE_REQUIREMENTS_UNRESOLVED critical gap，核验轨失效）。
        if verdict == "supports_candidate":
            return {"source", "sink"}, {"authorization", "dataflow", "impact"}, True
        if verdict == "refutes_candidate":
            return _refutation_requirements(candidate)
        if verdict == "unresolved":
            return {"source", "sink"}, {"dataflow"}, True
        return {"source", "sink"}, {"authorization", "dataflow", "impact"}, False
    return set(), set(), False


def _refutation_requirements(candidate: Mapping[str, Any]) -> tuple[set[str], set[str], bool]:
    if candidate.get("authorization_status") in {"protected", "strongly_protected"} or candidate.get(
        "guard_status"
    ) == "present_effective" or candidate.get("guard_coverage_status") in {
        "effective", "fail_closed", "present_effective", "verified_effective"
    }:
        return set(), {"authorization"}, True
    if candidate.get("invalid_source_verified") is True or candidate.get("source_status") in {
        "deterministically_invalid", "invalid_verified", "verified_invalid"
    }:
        return {"source"}, {"dataflow"}, True
    if candidate.get("invalid_sink_verified") is True or candidate.get("sink_status") in {
        "deterministically_invalid", "invalid_verified", "verified_invalid"
    }:
        return {"sink"}, {"impact"}, True
    if candidate.get("disconnected_verified") is True or any(
        candidate.get(field) in {"disconnected_verified", "verified_disconnected"}
        for field in ("connectivity_status", "dataflow_status", "deterministic_path_status", "source_sink_status")
    ):
        return {"source", "sink"}, {"dataflow"}, True
    return {"source", "sink"}, {"authorization", "dataflow"}, True


def _reference_semantics(
    candidate: Mapping[str, Any],
    context: Mapping[str, Any],
    line: int | None,
    end_line: int | None,
) -> tuple[set[str], set[str]]:
    roles = _metadata_tags(context, "role")
    domains = _metadata_tags(context, "domain")
    kind = str(context.get("kind") or "").lower()
    reason = str(context.get("reason") or "").lower()
    if kind == "manifest_component":
        roles.add("location")
        domains.update({"exposure", "authorization"})
    if kind == "guard_candidate" or any(word in reason for word in ("guard", "authoriz", "permission")):
        roles.add("guard")
        domains.add("authorization")
    if context.get("method_name") in set(candidate.get("entry_points") or []):
        roles.add("location")
        domains.add("exposure")

    path = context.get("path")
    for field, role, field_domains in (
        ("locations", "location", {"exposure"}),
        ("sources", "source", {"dataflow"}),
        ("sinks", "sink", {"dataflow", "impact"}),
    ):
        for anchor in candidate.get(field, []) or []:
            if not isinstance(anchor, Mapping) or anchor.get("path") != path:
                continue
            anchor_line = _strict_positive_line(anchor.get("line"))
            anchor_end = _strict_positive_line(anchor.get("end_line")) or anchor_line
            if anchor_line is None:
                if line is None:
                    roles.add(role)
                    domains.update(field_domains)
            elif (
                line is not None and end_line is not None and anchor_end is not None
                and line <= anchor_end and anchor_line <= end_line
            ):
                roles.add(role)
                domains.update(field_domains)
    for role in roles:
        if role == "location":
            domains.add("exposure")
        elif role == "source":
            domains.add("dataflow")
        elif role == "sink":
            domains.update({"dataflow", "impact"})
        elif role == "guard":
            domains.add("authorization")
    return roles, domains


def _metadata_tags(context: Mapping[str, Any], name: str) -> set[str]:
    values: list[Any] = []
    for field in (name, f"evidence_{name}", f"{name}s", f"evidence_{name}s"):
        value = context.get(field)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            values.extend(value)
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _is_concrete_context(context: Mapping[str, Any]) -> bool:
    if context.get("concrete") is not None:
        return context.get("concrete") is True
    roles = _metadata_tags(context, "role")
    domains = _metadata_tags(context, "domain")
    return (
        context.get("kind") in {"method", "code_window", "source", "sink", "guard_candidate"}
        or bool(roles & {"source", "sink", "guard"})
        or bool(domains & {"dataflow", "impact"})
    )


def _reference_contained(context: Mapping[str, Any], line: int, end_line: int) -> bool:
    start = _nonnegative_int(context.get("start_line"))
    end = _nonnegative_int(context.get("end_line"))
    return start is not None and end is not None and start <= line <= end_line <= end


def _strict_positive_line(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _ai_evidence_contexts(
    candidate: Mapping[str, Any],
    indexed: Mapping[str, dict[str, Any]],
    methods: Mapping[str, dict[str, Any]],
    index_reader: SQLiteCodeIndexReader | None,
) -> list[dict[str, Any]]:
    """恢复 AI 引用上下文，显式保存的切片优先，索引重建仅作保守回退。

    回退只接受可由 slice_refs 编码或已验证引用 path 唯一重建的 manifest、method、window、
    symbol/summary 上下文；缺失或歧义项直接省略，使后续引用校验失败，而不是伪造范围。

    索引回查（v3.0.5）：AI 引用了 slice_refs 未登记、但索引 methods 中真实存在的方法
    （context_id 与 methods.id 同格式 ``path#Class.method:line``）时，按 context_id 直查
    索引恢复 method 上下文，使其可回查。索引中不存在的 context_id 仍省略，由后续引用
    校验判 CONTEXT_ID_NOT_FOUND（防幻觉底线不变，只是把"可回查"范围从切片扩展到索引）。
    """

    for field in ("ai_evidence_contexts", "slice_contexts", "contexts"):
        supplied = candidate.get(field)
        # 非空显式上下文优先；空列表视为未提供，继续走 slice_refs + 索引回查，
        # 否则空列表会吞掉索引回查恢复的上下文（v3.0.5 防御）。
        if isinstance(supplied, Sequence) and not isinstance(supplied, (str, bytes)) and supplied:
            return [dict(item) for item in supplied if isinstance(item, Mapping)]

    analysis_value = candidate.get("ai_analysis")
    analysis = analysis_value if isinstance(analysis_value, Mapping) else {}
    actual_paths = {
        str(ref.get("context_id")): ref.get("path")
        for ref in analysis.get("verified_evidence_refs", []) or []
        if isinstance(ref, Mapping) and ref.get("context_id") and isinstance(ref.get("path"), str)
    }
    contexts: list[dict[str, Any]] = []
    for value in candidate.get("slice_refs", []) or []:
        context_id = str(value)
        if context_id.startswith("manifest:"):
            contexts.append({
                "context_id": context_id,
                "kind": "manifest_component",
                "path": "AndroidManifest.xml",
                "start_line": 0,
                "end_line": 0,
            })
            continue
        method = methods.get(context_id)
        if method:
            contexts.append({
                "context_id": context_id,
                "kind": "method",
                "path": method.get("path"),
                "start_line": int(method.get("start_line") or 0),
                "end_line": int(method.get("end_line") or 0),
                "method_name": method.get("name"),
                "symbol_key": method.get("symbol_key"),
            })
            continue
        window = re.fullmatch(r"(.+)#window:(\d+)-(\d+)", context_id)
        if window:
            contexts.append({
                "context_id": context_id,
                "kind": "code_window",
                "path": window.group(1),
                "start_line": int(window.group(2)),
                "end_line": int(window.group(3)),
            })
            continue
        path = actual_paths.get(context_id)
        if not isinstance(path, str):
            continue
        entry = indexed.get(path)
        if entry is None and index_reader is not None:
            entry = index_reader.get_file_metadata(path)
        if not entry:
            continue
        if context_id.endswith("#symbols"):
            contexts.append({
                "context_id": context_id,
                "kind": "file_symbols",
                "path": path,
                "start_line": 1,
                "end_line": int(entry.get("line_count") or 0),
            })
            continue
        if context_id.endswith("#summary"):
            class_id = context_id.removesuffix("#summary")
            class_info = next(
                (item for item in entry.get("classes", []) if str(item.get("id")) == class_id), None
            )
            if class_info:
                contexts.append({
                    "context_id": context_id,
                    "kind": "class_summary",
                    "path": path,
                    "start_line": int(class_info.get("start_line") or 0),
                    "end_line": int(class_info.get("end_line") or 0),
                })
    _index_resolve_ai_refs(analysis, methods, contexts)
    return contexts


def _index_resolve_ai_refs(
    analysis: Mapping[str, Any],
    methods: Mapping[str, dict[str, Any]],
    contexts: list[dict[str, Any]],
) -> None:
    """索引回查（v3.0.5）：AI 引用 slice_refs 未登记但索引中真实存在的方法时，
    按 context_id（与 methods.id 同格式）直查索引恢复 method 上下文。

    仅当 AI 原始 evidence_refs 中的 context_id 能在全量方法索引精确命中时才恢复；
    索引中不存在的 context_id 保持省略，由后续引用校验判 CONTEXT_ID_NOT_FOUND。
    命中后行号仍须落在该方法行号范围内才算有效引用（防幻觉底线不变）。
    """
    raw_refs = analysis.get("evidence_refs", []) or []
    if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, (str, bytes)):
        return
    known = {str(context.get("context_id")) for context in contexts}
    for ref in raw_refs:
        if not isinstance(ref, Mapping):
            continue
        context_id = ref.get("context_id")
        if not isinstance(context_id, str) or context_id in known:
            continue
        method = methods.get(context_id)
        if not method:
            continue
        contexts.append({
            "context_id": context_id,
            "kind": "method",
            "path": method.get("path"),
            "start_line": int(method.get("start_line") or 0),
            "end_line": int(method.get("end_line") or 0),
            "method_name": method.get("name"),
            "symbol_key": method.get("symbol_key"),
        })
        known.add(context_id)


def _positive_line(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        line = int(value)
    except (TypeError, ValueError):
        return None
    return line if line >= 1 else None


def _record_invalid_gap(result: dict[str, Any], code: str, invalid: list[dict[str, Any]]) -> None:
    if not invalid:
        return
    result["blocking_gaps"].append({"code": code, "critical": True, "count": len(invalid)})
