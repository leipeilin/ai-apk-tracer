"""Normalized coverage domains and fail-closed candidate applicability."""

from __future__ import annotations

import posixpath
from copy import deepcopy
from typing import Any, Literal, Mapping, NotRequired, TypedDict

CoverageScope = Literal["run", "rule", "component", "path"]


class CoverageDomain(TypedDict):
    """JSON-serializable selectors describing exactly where a coverage fact applies."""

    scope: CoverageScope
    critical: bool
    provenance: Any
    rule_id: NotRequired[str]
    component_name: NotRequired[str]
    path: NotRequired[str]
    operation: NotRequired[str]


def normalize_coverage_domain(domain: Mapping[str, Any] | None) -> CoverageDomain | None:
    """Return a canonical domain, or ``None`` when it cannot be matched safely."""

    if not isinstance(domain, Mapping):
        return None
    scope = domain.get("scope")
    if scope not in {"run", "rule", "component", "path"}:
        return None

    selectors: dict[str, str] = {}
    for field in ("rule_id", "component_name", "path", "operation"):
        if field not in domain:
            continue
        value = domain[field]
        if not isinstance(value, str) or not value.strip():
            return None
        normalized = _normalize_path(value) if field == "path" else value.strip()
        if not normalized:
            return None
        selectors[field] = normalized

    if scope == "run" and any(field in selectors for field in ("rule_id", "component_name", "path")):
        return None
    if scope == "rule" and ("rule_id" not in selectors or any(
        field in selectors for field in ("component_name", "path")
    )):
        return None
    if scope == "component" and ("component_name" not in selectors or "path" in selectors):
        return None
    if scope == "path" and "path" not in selectors:
        return None

    critical = domain.get("critical", True)
    if not isinstance(critical, bool):
        return None
    return {
        "scope": scope,
        **selectors,
        "critical": critical,
        "provenance": deepcopy(domain.get("provenance", [])),
    }


def coverage_domain_from_facts(
    *,
    scope: CoverageScope | None = None,
    rule_id: Any = None,
    component_name: Any = None,
    path: Any = None,
    operation: Any = None,
    critical: Any = True,
    provenance: Any = None,
) -> CoverageDomain | None:
    """Build the narrowest domain supported by supplied facts without widening invalid facts."""

    if scope is None:
        if path is not None:
            scope = "path"
        elif component_name is not None:
            scope = "component"
        elif rule_id is not None:
            scope = "rule"
        else:
            return None
    raw: dict[str, Any] = {
        "scope": scope,
        "critical": critical,
        "provenance": [] if provenance is None else provenance,
    }
    if rule_id is not None:
        raw["rule_id"] = rule_id
    if component_name is not None:
        raw["component_name"] = component_name
    if path is not None:
        raw["path"] = path
    if operation is not None:
        raw["operation"] = operation
    return normalize_coverage_domain(raw)


def candidate_applies_to_domain(
    candidate: Mapping[str, Any], domain: Mapping[str, Any] | None
) -> bool:
    """Return whether a candidate is inside a valid domain; malformed domains match nothing."""

    normalized = normalize_coverage_domain(domain)
    if normalized is None or not isinstance(candidate, Mapping):
        return False

    rule_id = normalized.get("rule_id")
    if rule_id is not None and rule_id not in _candidate_rule_ids(candidate):
        return False
    component_name = normalized.get("component_name")
    if component_name is not None and component_name != _candidate_component_name(candidate):
        return False
    path = normalized.get("path")
    if path is not None and path not in _candidate_paths(candidate):
        return False
    operation = normalized.get("operation")
    if operation is not None and operation.casefold() not in _candidate_operations(candidate):
        return False
    return True


def coverage_domain_applies_to_candidate(
    domain: Mapping[str, Any] | None, candidate: Mapping[str, Any]
) -> bool:
    """Domain-first spelling for callers that propagate coverage facts."""

    return candidate_applies_to_domain(candidate, domain)


def _normalize_path(value: str) -> str:
    path = value.strip().replace("\\", "/")
    if "://" in path:
        return path
    normalized = posixpath.normpath(path)
    return "" if normalized == "." else normalized


def _candidate_rule_ids(candidate: Mapping[str, Any]) -> set[str]:
    values = candidate.get("rule_ids")
    rule_ids = {
        value.strip() for value in values or []
        if isinstance(value, str) and value.strip()
    } if isinstance(values, (list, tuple, set)) else set()
    value = candidate.get("rule_id")
    if isinstance(value, str) and value.strip():
        rule_ids.add(value.strip())
    return rule_ids


def _candidate_component_name(candidate: Mapping[str, Any]) -> str | None:
    value = candidate.get("component_name")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _candidate_paths(candidate: Mapping[str, Any]) -> set[str]:
    paths: set[str] = set()
    root_path = candidate.get("path")
    if isinstance(root_path, str) and root_path.strip():
        paths.add(_normalize_path(root_path))
    for field in ("locations", "sources", "sinks", "propagation_paths"):
        values = candidate.get(field)
        if not isinstance(values, (list, tuple)):
            continue
        for value in values:
            if not isinstance(value, Mapping):
                continue
            path = value.get("path")
            if isinstance(path, str) and path.strip():
                paths.add(_normalize_path(path))
    return paths


def _candidate_operations(candidate: Mapping[str, Any]) -> set[str]:
    operations: set[str] = set()
    for field in ("operation", "authorization_operation", "operation_taxonomy", "sink_kind"):
        value = candidate.get(field)
        if isinstance(value, str) and value.strip():
            operations.add(value.strip().casefold())
    for field in ("locations", "sources", "sinks", "propagation_paths"):
        values = candidate.get(field)
        if not isinstance(values, (list, tuple)):
            continue
        for item in values:
            if not isinstance(item, Mapping):
                continue
            for key in ("operation", "authorization_operation", "taxonomy", "kind", "method_name"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    operations.add(value.strip().casefold())
    return operations
