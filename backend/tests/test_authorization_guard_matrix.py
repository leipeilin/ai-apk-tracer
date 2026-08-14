from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.analysis.indexer import build_code_index
from app.analysis.manifest import parse_manifest
from app.findings.evidence import verify_candidate
from app.findings.severity import determine_severity

RULES_ROOT = Path(__file__).resolve().parents[2] / "rules"
if str(RULES_ROOT) not in sys.path:
    sys.path.insert(0, str(RULES_ROOT))

from shared.authorization import evaluate_authorization, parse_protection_level  # noqa: E402
from shared.dataflow import DataFlowAnalyzer  # noqa: E402
from shared.index_reader import RuleIndexReader  # noqa: E402


def _component(kind: str = "provider", name: str = "com.example.Provider", **values):
    return {
        "kind": kind,
        "name": name,
        "manifest_tag": kind,
        "exported": "true",
        "permission": None,
        "permission_protection": None,
        "read_permission": None,
        "read_permission_protection": None,
        "write_permission": None,
        "write_permission_protection": None,
        "grant_uri_permissions": False,
        "grant_uri_patterns": [],
        "path_permissions": [],
        "authority_tokens": ["com.example.provider"] if kind == "provider" else [],
        **values,
    }


def _manifest(*components, **values):
    return {
        "analysis_platform_api": 36,
        "target_sdk": 36,
        "components": list(components),
        "custom_permissions": {},
        "authority_conflicts": {},
        **values,
    }


def test_application_permission_inheritance_and_alias_target(tmp_path: Path) -> None:
    manifest_path = tmp_path / "AndroidManifest.xml"
    manifest_path.write_text(
        """<manifest xmlns:android='http://schemas.android.com/apk/res/android' package='com.example'>
        <permission android:name='com.example.SIG' android:protectionLevel='signature'/>
        <application android:permission='com.example.SIG'>
          <activity android:name='.Target' android:exported='true'/>
          <activity-alias android:name='.Alias' android:targetActivity='.Target' android:exported='true'/>
        </application></manifest>""",
        "utf-8",
    )
    parsed = parse_manifest(manifest_path)
    target, alias = parsed["components"]
    assert evaluate_authorization(parsed, target, "component_entry")["status"] == "strongly_protected"
    alias_result = evaluate_authorization(parsed, alias, "component_entry")
    assert alias_result["status"] == "strongly_protected"
    assert alias["target_permission"] == "com.example.SIG"
    assert any(item["source"] == "target_activity" for item in alias_result["rows"][0]["provenance"])


def test_provider_generic_permission_protects_read_and_write() -> None:
    provider = _component(permission="com.example.SIG", permission_protection="signature")
    manifest = _manifest(provider)
    assert evaluate_authorization(manifest, provider, "query")["status"] == "strongly_protected"
    assert evaluate_authorization(manifest, provider, "update")["status"] == "strongly_protected"


def test_provider_read_signature_write_normal_and_open_file_modes() -> None:
    provider = _component(
        read_permission="com.example.READ",
        read_permission_protection="signature",
        write_permission="com.example.WRITE",
        write_permission_protection="normal",
    )
    manifest = _manifest(provider)
    assert evaluate_authorization(manifest, provider, "query")["status"] == "strongly_protected"
    assert evaluate_authorization(manifest, provider, "delete")["status"] == "conditional"
    assert evaluate_authorization(manifest, provider, "openFile", mode="r")["status"] == "strongly_protected"
    assert evaluate_authorization(manifest, provider, "openFile", mode="w")["status"] == "conditional"
    assert evaluate_authorization(manifest, provider, "openFile", mode="c")["status"] == "conditional"
    assert evaluate_authorization(manifest, provider, "openFile")["status"] == "conditional"


def test_path_permission_can_weaken_or_strengthen_and_unknown_uri_keeps_regions() -> None:
    strong = _component(
        permission="com.example.SIG",
        permission_protection="signature",
        path_permissions=[{"pathPrefix": "/public", "readPermission": "com.example.NORMAL", "read_permission_protection": "normal"}],
    )
    strong_manifest = _manifest(strong)
    assert evaluate_authorization(strong_manifest, strong, "query", path="/private/x")["status"] == "strongly_protected"
    assert evaluate_authorization(strong_manifest, strong, "query", path="/public/x")["status"] == "conditional"
    unknown_path = evaluate_authorization(strong_manifest, strong, "query")
    assert len(unknown_path["rows"]) == 2
    assert unknown_path["status"] == "conditional"

    weak = _component(
        path_permissions=[{"path": "/admin", "permission": "com.example.SIG", "read_permission_protection": "signature"}],
    )
    weak_manifest = _manifest(weak)
    assert evaluate_authorization(weak_manifest, weak, "query", path="/admin")["status"] == "strongly_protected"
    assert evaluate_authorization(weak_manifest, weak, "query")["status"] == "unprotected"


def test_uri_grant_is_directional_or_alternative() -> None:
    provider = _component(
        read_permission="com.example.SIG",
        read_permission_protection="signature",
        write_permission="com.example.SIG",
        write_permission_protection="signature",
        grant_uri_permissions=True,
    )
    manifest = _manifest(provider)
    read = evaluate_authorization(manifest, provider, "query")
    write = evaluate_authorization(manifest, provider, "update")
    assert read["status"] == write["status"] == "conditional"
    assert read["has_uri_grant_alternative"] is True
    assert read["rows"][0]["alternatives"][-1]["direction"] == "read"
    assert write["rows"][0]["alternatives"][-1]["direction"] == "write"


@pytest.mark.parametrize(
    ("value", "status", "base"),
    [
        ("signature|privileged", "strongly_protected", "signature"),
        ("0x12", "strongly_protected", "signature"),
        ("1", "conditional", "dangerous"),
        ("signature|futureFlag", "unknown", "signature"),
        ("17", "conditional", "dangerous"),
        ("0x40000001", "unknown", "dangerous"),
    ],
)
def test_protection_level_fail_closed(value, status, base) -> None:
    parsed = parse_protection_level(value)
    assert parsed["status"] == status
    assert parsed["base"] == base


def test_unknown_platform_permission_and_authority_conflict_are_gaps() -> None:
    provider = _component(
        permission="android.permission.NOT_IN_MINIMAL_CATALOG",
        permission_protection="platform_or_unknown",
    )
    manifest = _manifest(
        provider,
        authority_conflicts={"com.example.provider": ["com.example.Provider", "com.example.Other"]},
    )
    result = evaluate_authorization(manifest, provider, "query")
    assert result["status"] == "unknown"
    assert result["rows"][0]["authority_resolution"] == "ambiguous"
    assert {gap["code"] for gap in result["blocking_gaps"]} >= {
        "AUTHORIZATION_PERMISSION_UNKNOWN", "DUPLICATE_PROVIDER_AUTHORITY",
    }


def _guard_flow(tmp_path: Path, body: str, entry: str = "onCreate", extra_methods: str = "") -> dict:
    source_root = tmp_path / "sources"
    source_root.mkdir(parents=True)
    source = f"""package com.example;
class GuardedActivity {{ WebView web;
 void {entry}(Intent intent) {{
{body}
 }}
{extra_methods}
}}
"""
    path = source_root / "com/example/GuardedActivity.java"
    path.parent.mkdir(parents=True)
    path.write_text(source, "utf-8")
    index_root = tmp_path / "index"
    descriptor = build_code_index(source_root, index_root / "code-index.json")
    reader = RuleIndexReader({**descriptor, "allowed_index_root": index_root.resolve().as_posix()})
    try:
        scope = reader.component_flow_scope("com.example.GuardedActivity", {entry})
    finally:
        reader.close()
    analyzer = DataFlowAnalyzer(scope["files"], scope["entry_method_ids"], scope["gaps"])
    return analyzer.analyze_entry({entry})


def _sink_body(prefix: str = "", suffix: str = "") -> str:
    return f"""  {prefix}
  String url = intent.getStringExtra(\"url\");
  web.loadUrl(url);
  {suffix}"""


def test_guard_enforce_and_check_fail_closed_are_effective(tmp_path: Path) -> None:
    enforce = _guard_flow(tmp_path / "enforce", _sink_body('enforceCallingPermission("com.example.SIG", "denied");'))
    assert enforce["guard_coverage"]["status"] == "present_effective"

    checked = _guard_flow(
        tmp_path / "check",
        _sink_body(
            'int allowed = checkCallingPermission("com.example.SIG");\n  if (allowed != PackageManager.PERMISSION_GRANTED) return;'
        ),
    )
    assert checked["guard_coverage"]["status"] == "present_effective"


def test_guard_ignored_uid_only_after_sink_and_catch_continue(tmp_path: Path) -> None:
    ignored = _guard_flow(
        tmp_path / "ignored",
        _sink_body('checkCallingPermission("com.example.SIG");'),
    )
    assert ignored["guard_coverage"]["status"] == "present_partial"

    uid_only = _guard_flow(tmp_path / "uid", _sink_body("int uid = Binder.getCallingUid();"))
    assert uid_only["guard_coverage"]["status"] == "absent"
    assert uid_only["guard_coverage"]["identity_sources"]

    after = _guard_flow(
        tmp_path / "after",
        _sink_body("", 'enforceCallingPermission("com.example.SIG", "denied");'),
    )
    assert after["guard_coverage"]["status"] == "absent"

    caught = _guard_flow(
        tmp_path / "caught",
        _sink_body('try { enforceCallingPermission("com.example.SIG", "denied"); } catch (SecurityException ignored) { log(); }'),
    )
    assert caught["guard_coverage"]["status"] == "present_bypassable"


def test_guard_is_entry_specific(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir(parents=True)
    path = source_root / "com/example/GuardedActivity.java"
    path.parent.mkdir(parents=True)
    path.write_text(
        """package com.example;
class GuardedActivity { WebView web;
 void onCreate(Intent intent) { enforceCallingPermission("p", "d"); String u=intent.getStringExtra("u"); web.loadUrl(u); }
 void onNewIntent(Intent intent) { String u=intent.getStringExtra("u"); web.loadUrl(u); }
}
""",
        "utf-8",
    )
    index_root = tmp_path / "index"
    descriptor = build_code_index(source_root, index_root / "code-index.json")
    reader = RuleIndexReader({**descriptor, "allowed_index_root": index_root.resolve().as_posix()})
    try:
        create_scope = reader.component_flow_scope("com.example.GuardedActivity", {"onCreate"})
        new_scope = reader.component_flow_scope("com.example.GuardedActivity", {"onNewIntent"})
    finally:
        reader.close()
    create = DataFlowAnalyzer(create_scope["files"], create_scope["entry_method_ids"], create_scope["gaps"]).analyze_entry({"onCreate"})
    new = DataFlowAnalyzer(new_scope["files"], new_scope["entry_method_ids"], new_scope["gaps"]).analyze_entry({"onNewIntent"})
    assert create["guard_coverage"]["status"] == "present_effective"
    assert new["guard_coverage"]["status"] == "absent"


def test_guard_wrapper_requires_unique_resolved_target(tmp_path: Path) -> None:
    unique = _guard_flow(
        tmp_path / "unique",
        _sink_body("authorize();"),
        extra_methods=' void authorize() { enforceCallingPermission("p", "d"); }',
    )
    assert unique["guard_coverage"]["status"] == "present_effective"
    assert unique["guard_coverage"]["guards"][0]["kind"] == "wrapped_guard"

    ambiguous = _guard_flow(
        tmp_path / "ambiguous",
        _sink_body("authorize(value);"),
        extra_methods=' void authorize(String value) { enforceCallingPermission("p", "d"); }\n void authorize(int value) { }',
    )
    assert ambiguous["guard_coverage"]["status"] == "unknown"


def test_severity_and_l3_require_known_authorization_and_gradeable_guard() -> None:
    base = {
        "rule_id": "TEST",
        "component": "activity",
        "component_name": "Demo",
        "evidence_level": "L2",
        "promotion_requested": True,
        "deterministic_chain_verified": True,
        "dataflow_status": "intraprocedural",
        "analysis_status": "rule_only",
        "severity_hint": "high",
        "impact_status": "statically_confirmed",
        "locations": [{"path": "Demo.java", "line": 1}],
        "sources": [{"path": "Demo.java", "line": 1}],
        "sinks": [{"path": "Demo.java", "line": 2}],
        "blocking_gaps": [],
        "coverage_gaps": [],
    }
    unknown_auth = {**base, "authorization_status": "unknown", "guard_status": "absent"}
    assert determine_severity(unknown_auth)[0] == "pending"
    assert verify_candidate(unknown_auth, {"files": [{"path": "Demo.java", "line_count": 2}]})["evidence_level"] == "L2"

    partial_guard = {**base, "authorization_status": "unprotected", "guard_status": "present_partial"}
    assert determine_severity(partial_guard)[0] == "pending"
    assert verify_candidate(partial_guard, {"files": [{"path": "Demo.java", "line_count": 2}]})["evidence_level"] == "L2"

    effective = {**base, "authorization_status": "unprotected", "guard_status": "present_effective"}
    assert determine_severity(effective)[0] == "informational"
