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


def test_skipped_exported_component_is_surfaced_in_coverage_gaps() -> None:
    """S7：索引跳过的导出组件必须出现在 run 汇总与组件级 coverage gap。"""

    from app.analysis.coverage import finalize_run_coverage

    gaps = finalize_run_coverage(
        candidates=[],
        jadx_gaps=[],
        rule_failures=[],
        code_index={
            "stats": {"skipped_file_count": 1},
            "skipped_files": [{
                "path": "com/example/export/SecretActivity.java",
                "reason": "FILE_SIZE_LIMIT",
            }],
        },
        manifest_components=[
            {"kind": "activity", "name": "com.example.export.SecretActivity", "exported": "true"},
            {"kind": "activity", "name": "com.example.export.InternalActivity", "exported": "false"},
            {"kind": "service", "name": "com.other.UnrelatedService", "exported": "true"},
        ],
    )
    codes = {gap.get("code") for gap in gaps}
    assert "JADX_SKIPPED_EXPORTED_COMPONENTS" in codes
    assert "RULE_COMPONENT_PARTIAL" in codes

    run_gap = next(gap for gap in gaps if gap["code"] == "JADX_SKIPPED_EXPORTED_COMPONENTS")
    assert run_gap["skipped_exported_component_count"] == 1
    assert run_gap["components"][0]["component_name"] == "com.example.export.SecretActivity"
    assert run_gap["affects_positive_proof"] is True

    component_gap = next(gap for gap in gaps if gap["code"] == "RULE_COMPONENT_PARTIAL")
    assert component_gap["domain"]["component"] == "com.example.export.SecretActivity"
    assert component_gap["skipped_paths"] == ["com/example/export/SecretActivity.java"]


def test_missing_exported_component_class_is_surfaced_in_coverage_gaps() -> None:
    """S7：JADX 静默未产出的导出组件类（非跳过）必须出现在 coverage gap。"""

    from app.analysis.coverage import _missing_exported_components, finalize_run_coverage

    components = [
        {"kind": "service", "name": "com.example.SportXmsService", "exported": "true"},
        {"kind": "activity", "name": "com.example.InternalActivity", "exported": "false"},
        {"kind": "provider", "name": "com.example.DeviceProvider", "exported": "true"},
        {"kind": "receiver", "name": "dynamic:com/example/DynamicRec.java", "exported": "true"},
    ]
    indexed = {"com.example.InternalActivity"}
    missing = _missing_exported_components(components, indexed)
    assert {item["component_name"] for item in missing} == {
        "com.example.SportXmsService", "com.example.DeviceProvider",
    }

    gaps = finalize_run_coverage(
        candidates=[],
        jadx_gaps=[],
        rule_failures=[],
        code_index={"database_path": "/nonexistent/index.sqlite3"},
        manifest_components=components,
    )
    run_gap = next(gap for gap in gaps if gap["code"] == "COMPONENT_CLASS_NOT_INDEXED" and gap.get("scope") == "run")
    assert run_gap["missing_exported_component_count"] == 2
    assert run_gap["affects_positive_proof"] is True
    component_gaps = [gap for gap in gaps if gap["code"] == "COMPONENT_CLASS_NOT_INDEXED" and gap.get("scope") == "component"]
    assert {gap["domain"]["component"] for gap in component_gaps} == {
        "com.example.SportXmsService", "com.example.DeviceProvider",
    }
