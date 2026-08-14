from __future__ import annotations

from app.analysis.coverage_domain import (
    candidate_applies_to_domain,
    coverage_domain_applies_to_candidate,
    coverage_domain_from_facts,
    normalize_coverage_domain,
)
from app.analysis.rule_runner import RuleRunner


def test_normalized_domain_preserves_criticality_and_provenance() -> None:
    provenance = {"source": "indexer", "facts": ["component", "path"]}

    domain = normalize_coverage_domain({
        "scope": "path",
        "rule_id": "RULE_A",
        "component_name": "com.example.Provider",
        "path": r"com\example\Provider.java",
        "operation": "query",
        "critical": False,
        "provenance": provenance,
    })

    assert domain == {
        "scope": "path",
        "rule_id": "RULE_A",
        "component_name": "com.example.Provider",
        "path": "com/example/Provider.java",
        "operation": "query",
        "critical": False,
        "provenance": provenance,
    }
    assert domain["provenance"] is not provenance


def test_candidate_applicability_honors_every_domain_scope() -> None:
    candidate = {
        "rule_id": "RULE_A",
        "rule_ids": ["RULE_ALIAS"],
        "component_name": "com.example.Provider",
        "locations": [{"path": "com/example/Provider.java"}],
        "authorization_operation": "query",
    }

    assert candidate_applies_to_domain(candidate, coverage_domain_from_facts(scope="run"))
    assert candidate_applies_to_domain(candidate, coverage_domain_from_facts(rule_id="RULE_A"))
    assert candidate_applies_to_domain(candidate, coverage_domain_from_facts(rule_id="RULE_ALIAS"))
    assert candidate_applies_to_domain(candidate, coverage_domain_from_facts(
        rule_id="RULE_A", component_name="com.example.Provider"
    ))
    path_domain = coverage_domain_from_facts(
        rule_id="RULE_A",
        component_name="com.example.Provider",
        path=r"com\example\Provider.java",
        operation="query",
    )
    assert coverage_domain_applies_to_candidate(path_domain, candidate)

    assert not candidate_applies_to_domain(candidate, coverage_domain_from_facts(rule_id="RULE_B"))
    assert not candidate_applies_to_domain(candidate, coverage_domain_from_facts(
        rule_id="RULE_A", component_name="com.example.Other"
    ))
    assert not candidate_applies_to_domain(candidate, coverage_domain_from_facts(
        path="com/example/Other.java"
    ))
    assert not candidate_applies_to_domain(candidate, coverage_domain_from_facts(
        scope="run", operation="delete"
    ))


def test_missing_or_invalid_domains_match_nothing_instead_of_becoming_run_wide() -> None:
    candidate = {"rule_id": "RULE_A", "component_name": "com.example.Provider"}

    assert coverage_domain_from_facts() is None
    assert normalize_coverage_domain({"scope": "component", "critical": True}) is None
    assert normalize_coverage_domain({"scope": "path", "path": ""}) is None
    assert normalize_coverage_domain({"scope": "run", "rule_id": "RULE_A"}) is None
    assert normalize_coverage_domain({"scope": "rule", "rule_id": "RULE_A", "critical": "yes"}) is None
    assert not candidate_applies_to_domain(candidate, None)
    assert not candidate_applies_to_domain(candidate, {"scope": "component"})


def test_rule_runner_retains_narrow_domains_on_failures_and_component_diagnostics() -> None:
    failure = RuleRunner._failure("RULE_A", "RULE_TIMEOUT", "timeout", 25)
    assert failure["coverage_domain"] == {
        "scope": "rule",
        "rule_id": "RULE_A",
        "critical": True,
        "provenance": [{"source": "rule_runner", "fact": "rule_id"}],
    }

    output = {
        "component_diagnostics": [{
            "component_name": "com.example.Provider",
            "status": "completed",
            "coverage_domain": {
                "scope": "run",
                "critical": False,
                "provenance": {"source": "binder_index"},
            },
            "gaps": [{
                "code": "METHOD_NOT_INDEXED",
                "path": r"com\example\Provider.java",
                "operation": "query",
                "critical": False,
                "provenance": {"source": "method_index"},
            }],
        }, {
            "status": "error",
            "gaps": [{"code": "COMPONENT_UNKNOWN", "critical": True}],
        }],
    }

    RuleRunner._normalize_component_diagnostics("RULE_A", output)

    diagnostic = output["component_diagnostics"][0]
    assert diagnostic["coverage_domain"] == {
        "scope": "component",
        "rule_id": "RULE_A",
        "component_name": "com.example.Provider",
        "critical": False,
        "provenance": {"source": "binder_index"},
    }
    assert diagnostic["gaps"][0]["coverage_domain"] == {
        "scope": "path",
        "rule_id": "RULE_A",
        "component_name": "com.example.Provider",
        "path": "com/example/Provider.java",
        "operation": "query",
        "critical": False,
        "provenance": {"source": "method_index"},
    }
    malformed = output["component_diagnostics"][1]
    assert malformed["coverage_domain"] is None
    assert malformed["gaps"][0]["coverage_domain"] is None
    assert not candidate_applies_to_domain(
        {"rule_id": "RULE_A", "component_name": "com.example.Other"},
        diagnostic["coverage_domain"],
    )
