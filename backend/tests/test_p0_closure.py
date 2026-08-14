from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

from app.analysis.index_store import SQLiteCodeIndexReader
from app.analysis.indexer import build_code_index
from app.analysis.orchestrator import _finalize_run_coverage
from app.findings.aggregate import aggregate_candidates
from app.findings.evidence import summarize_evidence_integrity, verify_candidate

RULES_ROOT = Path(__file__).resolve().parents[2] / "rules"
if str(RULES_ROOT) not in sys.path:
    sys.path.insert(0, str(RULES_ROOT))

from shared.dataflow import classify_call_operation  # noqa: E402
from shared.detector import execute  # noqa: E402
from shared.index_reader import RuleIndexReader  # noqa: E402


def _component(kind: str, name: str, **values):
    return {
        "kind": kind,
        "name": name,
        "exported": "true",
        "permission": None,
        "permission_protection": None,
        "read_permission": None,
        "read_permission_protection": None,
        "write_permission": None,
        "write_permission_protection": None,
        "grant_uri_permissions": False,
        "path_permissions": [],
        "provider_paths": [],
        "intent_filters": [],
        **values,
    }


def _indexed_payload(tmp_path: Path, sources: dict[str, str], components: list[dict]):
    source_root = tmp_path / "sources"
    source_root.mkdir(parents=True)
    for relative, content in sources.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    descriptor = build_code_index(source_root, tmp_path / "index" / "code-index.json")
    return descriptor, {
        "manifest": {"analysis_platform_api": 36, "components": components, "authority_conflicts": {}},
        "index": {**descriptor, "allowed_index_root": (tmp_path / "index").resolve().as_posix()},
    }


def test_call_sites_exclude_comments_strings_and_declarations(tmp_path: Path) -> None:
    descriptor, _ = _indexed_payload(tmp_path, {
        "Demo.java": '''class Demo {
 void run(Intent intent) {
  String fake = "web.loadUrl(intent.getStringExtra())";
  // sendBroadcast(intent);
  String url = intent.getStringExtra("url");
  web.loadUrl(url);
 }
}'''
    }, [])
    reader = SQLiteCodeIndexReader(descriptor)
    try:
        methods = reader.load_structure_files()[0]["methods"]
        names = [call["method_name"] for call in methods[0]["call_sites"]]
        assert names == ["getStringExtra", "loadUrl"]
        assert methods[0]["summary"]["parameter_to_sink"] == []
    finally:
        reader.close()


def test_binder_requires_remote_dispatch_and_maps_transaction(tmp_path: Path) -> None:
    service = _component("service", "com.example.RemoteService")
    descriptor, payload = _indexed_payload(tmp_path, {
        "com/example/RemoteService.java": '''package com.example;
class RemoteService {
 Object onBind(Intent i) { return new SportStub(); }
}
''',
        "com/example/SportStub.java": '''package com.example;
class SportStub extends ISportApi.Stub {
 SportManager manager;
 static final int TRANSACTION_startSport = 1;
 boolean onTransact(int code, Parcel data, Parcel reply, int flags) {
  switch (code) { case 1: data.readInt(); startSport(); reply.writeNoException(); return true; }
  return false;
 }
 void startSport() { manager.startSport(); }
}
''',
    }, [service])
    result = execute("SERVICE_BINDER_CALLER_CHECK_MISSING", payload)
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    transaction = next(item for item in candidate["binder_transactions"] if item["code"] == 1)
    assert transaction["interface_method"] == "startSport"
    assert transaction["parcel_reads"] == ["readInt"]
    assert candidate["deterministic_chain_verified"] is True

    local_root = tmp_path / "local"
    _, local_payload = _indexed_payload(local_root, {
        "LocalService.java": '''class LocalService {
 Object onBind(Intent i) { return new LocalBinder(); }
 class LocalBinder extends Binder {
  void startSport() { manager.startSport(); }
 }
}'''
    }, [_component("service", "LocalService")])
    assert execute("SERVICE_BINDER_CALLER_CHECK_MISSING", local_payload)["candidates"] == []


def test_binder_nested_stub_owner_disambiguates_same_named_inner_classes(tmp_path: Path) -> None:
    service = _component("service", "com.example.RemoteService")
    descriptor, payload = _indexed_payload(tmp_path, {
        "com/example/RemoteService.java": '''package com.example;
class RemoteService {
 private BinderImpl binder = new BinderImpl();
 Object onBind(Intent intent) { return this.binder; }
}
class BinderImpl extends ISecond.a { }
''',
        "com/example/IFirst.java": '''package com.example;
interface IFirst {
 abstract class a extends Binder {
  boolean onTransact(int code, Parcel data, Parcel reply, int flags) { return false; }
 }
}
''',
        "com/example/ISecond.java": '''package com.example;
interface ISecond {
 abstract class a extends Binder {
  static final int TRANSACTION_startSport = 1;
  boolean onTransact(int code, Parcel data, Parcel reply, int flags) {
   switch (code) { case 1: startSport(); reply.writeNoException(); return true; }
   return false;
  }
  void startSport() { manager.startSport(); }
 }
}
''',
    }, [service])
    structure_reader = SQLiteCodeIndexReader(descriptor)
    try:
        qualified_classes = {
            item["qualified_name"]
            for file in structure_reader.load_structure_files()
            for item in file["classes"]
        }
    finally:
        structure_reader.close()
    assert "com.example.IFirst.a" in qualified_classes
    assert "com.example.ISecond.a" in qualified_classes

    reader = RuleIndexReader(payload["index"])
    try:
        facts = reader.binder_components([service["name"]])[service["name"]]
    finally:
        reader.close()
    assert not any(gap["code"] == "BINDER_RETURN_TYPE_AMBIGUOUS" for gap in facts["gaps"])
    assert any(item["class"] == "com.example.ISecond.a" for item in facts["inheritance_chain"])
    assert {Path(file["path"]).name for file in facts["files"]} == {
        "RemoteService.java", "ISecond.java"
    }
    assert any(transaction["interface_method"] == "startSport" for transaction in facts["transactions"])


def test_safe_file_boundary_is_not_verified_path_traversal_and_authority_tokens_conflict(tmp_path: Path) -> None:
    provider = _component("provider", "com.example.SafeProvider", authorities="a;b")
    other = _component("provider", "com.example.OtherProvider", authorities="b")
    descriptor, payload = _indexed_payload(tmp_path, {
        "com/example/SafeProvider.java": '''package com.example;
class SafeProvider {
 File resolve(Uri uri) {
  String p = uri.getPath(); File f = new File(root, p).getCanonicalFile();
  if (!(f.equals(root) || f.getPath().startsWith(root.getPath() + File.separator))) throw new SecurityException();
  return f;
 }
 ParcelFileDescriptor openFile(Uri uri, String mode) { return ParcelFileDescriptor.open(resolve(uri), MODE_READ_ONLY); }
}'''
    }, [provider, other])
    payload["manifest"]["authority_conflicts"] = {"b": [provider["name"], other["name"]]}
    result = execute("PROVIDER_URI_TO_FILE", payload)
    candidate = result["candidates"][0]
    assert candidate["path_boundary_status"] == "safe_boundary"
    assert candidate["deterministic_chain_verified"] is False
    assert candidate["operation_modes"] == ["r"]
    assert candidate["duplicate_authorities"] == [other["name"]]
    assert any(gap["code"] == "DUPLICATE_PROVIDER_AUTHORITY" for gap in candidate["blocking_gaps"])


def test_invalid_source_or_sink_blocks_l3_and_mixed_ai_status_is_visible() -> None:
    candidate = {
        "rule_id": "TEST",
        "component": "activity",
        "component_name": "Demo",
        "evidence_level": "L2",
        "promotion_requested": True,
        "deterministic_chain_verified": True,
        "dataflow_status": "intraprocedural",
        "guard_status": "absent",
        "locations": [{"path": "Demo.java", "line": 1}],
        "sources": [{"path": "Demo.java", "line": 99}],
        "sinks": [{"path": "Demo.java", "line": 2, "kind": "webview"}],
        "blocking_gaps": [],
        "coverage_gaps": [],
    }
    verified = verify_candidate(candidate, {"files": [{"path": "Demo.java", "line_count": 3}]})
    assert verified["evidence_level"] == "L2"
    assert any(gap["code"] == "EVIDENCE_SOURCE_NOT_FOUND" for gap in verified["blocking_gaps"])

    base = {
        "rule_id": "TEST",
        "component": "activity",
        "component_name": "Demo",
        "evidence_level": "L2",
        "severity_hint": "high",
        "dataflow_status": "not_proven",
        "authorization_status": "unknown",
        "impact_status": "potential",
        "locations": [],
        "sources": [],
        "sinks": [],
        "propagation_paths": [],
        "blocking_gaps": [],
        "coverage_gaps": [],
        "review_priority": 1,
    }
    findings = aggregate_candidates([
        {**base, "analysis_status": "ai_completed"},
        {**base, "rule_id": "TEST_2", "analysis_status": "ai_failed"},
    ])
    assert findings[0]["analysis_status"] == "ai_partial"


def test_empty_l2_evidence_is_incomplete_and_not_semantically_closed() -> None:
    candidate = {
        "rule_id": "TEST",
        "component": "activity",
        "component_name": "Demo",
        "evidence_level": "L2",
        "deterministic_chain_verified": True,
        "dataflow_status": "intraprocedural",
        "guard_status": "absent",
        "authorization_status": "unprotected",
        "locations": [],
        "sources": [],
        "sinks": [],
        "blocking_gaps": [],
        "coverage_gaps": [],
    }

    verified = verify_candidate(candidate, {"files": []})

    assert verified["fact_integrity_status"] == "incomplete"
    assert verified["semantic_status"] == "not_proven"
    assert verified["exploitability_status"] == "pending"
    assert any(gap["code"] == "EVIDENCE_REQUIRED_MISSING" for gap in verified["blocking_gaps"])


def test_aggregate_propagates_review_status_from_ai_completed(tmp_path: Path) -> None:
    """聚合只消费 review state；未满足双重反驳条件时保持人工复核。"""

    l2_base = {
        "rule_id": "TEST",
        "component": "activity",
        "component_name": "Demo",
        "evidence_level": "L2",
        "severity_hint": "high",
        "dataflow_status": "intraprocedural",
        "authorization_status": "unknown",
        "impact_status": "potential",
        "locations": [{"path": "Demo.java", "line": 1}],
        "sources": [{"path": "Demo.java", "line": 99}],
        "sinks": [{"path": "Demo.java", "line": 2, "kind": "webview"}],
        "propagation_paths": [],
        "blocking_gaps": [],
        "coverage_gaps": [],
        "review_priority": 1,
    }
    # 场景 1：L2 + ai_completed + promotion_recommended=True → pending_manual
    findings = aggregate_candidates([
        {**l2_base, "analysis_status": "ai_completed",
         "ai_analysis": {"promotion_recommended": True, "analysis_complete": True}},
    ])
    assert findings[0]["review_status"] == "pending_manual"

    # 场景 2：promotion_recommended=False 不是独立误报依据 → pending_manual
    findings = aggregate_candidates([
        {**l2_base, "analysis_status": "ai_completed",
         "ai_analysis": {"promotion_recommended": False, "analysis_complete": True}},
    ])
    assert findings[0]["review_status"] == "pending_manual"

    # 场景 3：L2 + ai_incomplete → pending_ai（待 AI 重试）
    findings = aggregate_candidates([
        {**l2_base, "analysis_status": "ai_incomplete"},
    ])
    assert findings[0]["review_status"] == "pending_ai"

    # 场景 4：普通 L1 暴露项默认进入人工复核
    l1_base = {**l2_base, "evidence_level": "L1"}
    findings = aggregate_candidates([
        {**l1_base, "analysis_status": "ai_completed",
         "ai_analysis": {"promotion_recommended": True, "analysis_complete": True}},
    ])
    assert findings[0]["review_status"] == "pending_manual"


def test_fqcn_resolution_keeps_same_named_services_separate(tmp_path: Path) -> None:
    descriptor, payload = _indexed_payload(tmp_path, {
        "com/a/SportService.java": '''package com.a;
class SportService {
 void onStartCommand(Intent intent) { dispatch(intent); }
 void dispatch(Intent intent) { startForeground(1, notification); }
}''',
        "com/b/SportService.java": '''package com.b;
class SportService {
 void onStartCommand(Intent intent) { dispatch(intent); }
 void dispatch(Intent intent) { stopSelf(); }
}''',
    }, [
        _component("service", "com.a.SportService"),
        _component("service", "com.b.SportService"),
    ])
    reader = SQLiteCodeIndexReader(descriptor)
    try:
        files = reader.load_structure_files()
    finally:
        reader.close()
    dispatch_targets = {}
    for file in files:
        for method in file["methods"]:
            if method["name"] != "onStartCommand":
                continue
            call = next(item for item in method["call_sites"] if item["method_name"] == "dispatch")
            dispatch_targets[method["qualified_class"]] = call
    assert dispatch_targets["com.a.SportService"]["resolve_status"] == "resolved"
    assert dispatch_targets["com.b.SportService"]["resolve_status"] == "resolved"
    assert "com/a/SportService.java" in dispatch_targets["com.a.SportService"]["resolved_target_id"]
    assert "com/b/SportService.java" in dispatch_targets["com.b.SportService"]["resolved_target_id"]
    rule_reader = RuleIndexReader(payload["index"])
    try:
        assert [file["path"] for file in rule_reader.component_files("com.a.SportService")] == [
            "com/a/SportService.java"
        ]
        assert [file["path"] for file in rule_reader.component_files("com.b.SportService")] == [
            "com/b/SportService.java"
        ]
    finally:
        rule_reader.close()


def test_overloaded_unknown_call_is_ambiguous_not_arbitrarily_resolved(tmp_path: Path) -> None:
    descriptor, _ = _indexed_payload(tmp_path, {
        "Demo.java": '''class Demo {
 void run() { route(getValue()); }
 void route(String value) { }
 void route(int value) { }
}'''
    }, [])
    reader = SQLiteCodeIndexReader(descriptor)
    try:
        method = next(
            method for file in reader.load_structure_files() for method in file["methods"]
            if method["name"] == "run"
        )
    finally:
        reader.close()
    call = next(item for item in method["call_sites"] if item["method_name"] == "route")
    assert call["resolve_status"] == "ambiguous"
    assert call["resolved_target_id"] is None


def test_receiver_type_aware_delete_classification() -> None:
    file_delete = classify_call_operation({
        "method_name": "delete", "receiver_type": "java.io.File", "receiver_text": "file"
    }, "remove")
    database_delete = classify_call_operation({
        "method_name": "delete", "receiver_type": "android.database.sqlite.SQLiteDatabase", "receiver_text": "db"
    }, "remove")
    content_delete = classify_call_operation({
        "method_name": "delete", "receiver_type": "android.content.ContentResolver", "receiver_text": "resolver"
    }, "remove")
    provider_entry = classify_call_operation({
        "method_name": "delete", "receiver_type": "", "receiver_text": "this"
    }, "delete")
    unknown = classify_call_operation({
        "method_name": "delete", "receiver_type": "", "receiver_text": "repo"
    }, "remove")
    assert file_delete["kind"] == "file_delete"
    assert database_delete["kind"] == "database_mutation"
    assert content_delete["kind"] == "content_mutation"
    assert provider_entry["is_sink"] is False
    assert unknown["kind"] == "not_sensitive"
    assert unknown["is_sink"] is False
    assert unknown["verified"] is False


def test_binder_batch_load_is_narrow_and_reports_component_diagnostics(tmp_path: Path) -> None:
    sources = {
        "com/example/RemoteService.java": '''package com.example;
class RemoteService {
 Object onBind(Intent i) { return new RemoteStub(); }
}
''',
        "com/example/RemoteStub.java": '''package com.example;
class RemoteStub extends IRemote.Stub {
 static final int TRANSACTION_getDeviceState = 1;
 boolean onTransact(int code, Parcel data, Parcel reply, int flags) {
  switch(code) { case 1: getDeviceState(); return true; } return false;
 }
 void getDeviceState() { manager.getDeviceState(); }
}
''',
    }
    for index in range(80):
        sources[f"com/example/noise/Noise{index}.java"] = (
            f"package com.example.noise; class Noise{index} {{ void work() {{ helper(); }} void helper() {{ }} }}"
        )
    descriptor, payload = _indexed_payload(
        tmp_path,
        sources,
        [_component("service", "com.example.RemoteService")],
    )
    started = time.monotonic()
    reader = RuleIndexReader(payload["index"])
    try:
        facts = reader.binder_components(["com.example.RemoteService"])["com.example.RemoteService"]
    finally:
        reader.close()
    assert time.monotonic() - started < 3
    assert {Path(file["path"]).name for file in facts["files"]} == {"RemoteService.java", "RemoteStub.java"}
    result = execute("SERVICE_BINDER_CALLER_CHECK_MISSING", payload)
    assert result["status"] == "completed"
    assert result["component_diagnostics"][0]["status"] == "completed"


def test_manifest_component_uses_larger_index_limit_and_exposes_skipped_list(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    package_root = source_root / "com" / "example"
    package_root.mkdir(parents=True)
    padding = "// padding\n" * 60_000
    component_path = package_root / "Receivers.kt"
    component_path.write_text(
        "package com.example; class LargeReceiver { void onReceive(Context c, Intent i) { i.getAction(); } }\n" + padding,
        "utf-8",
    )
    generated_path = package_root / "GeneratedModels.java"
    generated_path.write_text("package com.example; class GeneratedModels { }\n" + padding, "utf-8")
    descriptor = build_code_index(
        source_root,
        tmp_path / "index" / "code-index.json",
        max_file_size_kb=512,
        component_max_file_size_kb=2048,
        priority_component_fqcns={"com.example.LargeReceiver"},
    )
    reader = SQLiteCodeIndexReader(descriptor)
    try:
        indexed_paths = {file["path"] for file in reader.load_structure_files()}
    finally:
        reader.close()
    assert "com/example/Receivers.kt" in indexed_paths
    assert "com/example/GeneratedModels.java" not in indexed_paths
    assert descriptor["stats"]["skipped_file_count"] == 1
    assert descriptor["skipped_files"] == [{
        "path": "com/example/GeneratedModels.java",
        "size_bytes": generated_path.stat().st_size,
        "reason": "FILE_SIZE_LIMIT",
        "component_related": False,
    }]


def test_skipped_file_gap_only_blocks_related_candidate() -> None:
    related = {
        "rule_id": "RECEIVER_EXPORTED_NO_PERMISSION",
        "component_name": "com.example.LargeReceiver",
        "evidence_level": "L1",
        "analysis_status": "rule_only",
        "coverage_gaps": [],
    }
    unrelated = {
        "rule_id": "SERVICE_BINDER_CALLER_CHECK_MISSING",
        "component_name": "com.example.RemoteService",
        "evidence_level": "L2",
        "analysis_status": "ai_completed",
        "deterministic_chain_verified": True,
        "coverage_gaps": [],
    }
    code_index = {
        "stats": {"skipped_file_count": 1},
        "skipped_files": [{
            "path": "com/example/LargeReceiver.java",
            "size_bytes": 900_000,
            "reason": "FILE_SIZE_LIMIT",
            "component_related": True,
        }],
    }
    run_gaps = _finalize_run_coverage([related, unrelated], [], [], code_index, [])
    assert any(gap["code"] == "INDEX_FILES_SKIPPED" for gap in run_gaps)
    assert any(gap["code"] == "INDEX_FILES_SKIPPED" for gap in related["coverage_gaps"])
    assert unrelated["coverage_gaps"] == []
    assert unrelated["analysis_incomplete"] is False


def test_rule_failure_only_blocks_its_coverage_domain() -> None:
    candidates = [{
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "component_name": "com.example.Activity",
        "evidence_level": "L2",
        "analysis_status": "ai_completed",
        "deterministic_chain_verified": True,
        "coverage_gaps": [],
    }]
    run_gaps = _finalize_run_coverage(
        candidates,
        [],
        [{"rule_id": "SERVICE_BINDER_CALLER_CHECK_MISSING", "status": "failed"}],
        {"stats": {"skipped_file_count": 0}},
        [],
    )
    assert any(gap["code"] == "RULE_PRESCAN_PARTIAL" for gap in run_gaps)
    assert candidates[0]["coverage_gaps"] == []
    assert candidates[0]["analysis_incomplete"] is False


def test_evidence_integrity_summary_uses_explicit_counts() -> None:
    verified = [{
        "fact_integrity_status": "verified",
        "semantic_status": "closed",
        "exploitability_status": "statically_gradeable",
        "locations": [{"verification": "fact"}],
        "invalid_locations": [],
        "sources": [{"verification": "fact"}],
        "invalid_sources": [],
        "sinks": [{"verification": "fact"}],
        "invalid_sinks": [],
    }]
    findings = [{"exploitability_status": "statically_gradeable", "severity": "high"}]
    summary = summarize_evidence_integrity(verified, findings)
    assert summary == {
        "candidates_checked": 1,
        "locations_total": 1,
        "locations_verified": 1,
        "sources_total": 1,
        "sources_verified": 1,
        "sinks_total": 1,
        "sinks_verified": 1,
        "deterministic_chains_closed": 1,
        "gradeable_candidates": 1,
        "gradeable_findings": 1,
        "findings_pending_review": 0,
    }


def test_global_jadx_partial_blocks_negative_not_positive_proof() -> None:
    candidate = {
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "component_name": "com.example.Activity",
        "evidence_level": "L2",
        "analysis_status": "ai_completed",
        "deterministic_chain_verified": True,
        "coverage_gaps": [],
    }

    run_gaps = _finalize_run_coverage(
        [candidate],
        [{"code": "JADX_PARTIAL_DECOMPILATION", "message": "global partial"}],
        [],
        {"stats": {"skipped_file_count": 0}},
        [],
    )

    assert run_gaps[0]["claim_impact"] == "negative_proof"
    assert candidate["coverage_gaps"] == []
    assert candidate["positive_proof_coverage_complete"] is True
    assert candidate["negative_proof_coverage_complete"] is False
    assert candidate["analysis_incomplete"] is False


def test_evidence_semantics_reject_hash_quote_symbol_and_scope_mismatches() -> None:
    content = "class Demo {\n  void entry() { dangerous(); }\n}"
    method = {
        "id": "Demo.java#method:2:entry", "name": "entry", "class_name": "Demo",
        "qualified_class": "Demo", "symbol_key": "Demo#entry()", "start_line": 2,
        "end_line": 2,
    }
    code_index = {"files": [{
        "path": "Demo.java", "line_count": 3, "content": content,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "classes": [], "methods": [method],
    }]}
    base = {
        "scope_key": "scope-a", "evidence_level": "L2", "locations": [{"path": "Demo.java", "line": 2}],
        "sources": [{"path": "Demo.java", "line": 2}], "sinks": [{"path": "Demo.java", "line": 2}],
        "propagation_paths": [], "blocking_gaps": [], "coverage_gaps": [],
        "deterministic_chain_verified": True, "dataflow_status": "intraprocedural",
        "guard_status": "absent", "authorization_status": "unprotected",
    }
    mutations = [
        ({"content_sha256": "0" * 64}, "CONTENT_SHA256_MISMATCH"),
        ({"quoted_text": "safe();"}, "QUOTED_TEXT_MISMATCH"),
        ({"symbol_key": "Demo#missing()"}, "SYMBOL_KEY_NOT_FOUND"),
        ({"scope_key": "scope-b"}, "SCOPE_ID_MISMATCH"),
    ]

    for mutation, reason in mutations:
        result = verify_candidate({
            **base,
            "sources": [{"path": "Demo.java", "line": 2, **mutation}],
        }, code_index)
        assert result["invalid_sources"][0]["reason"] == reason

    legacy = verify_candidate(base, code_index)
    assert legacy["invalid_sources"] == []
    assert legacy["invalid_sinks"] == []


def test_propagation_method_ids_are_checked_without_claiming_cfg_validation() -> None:
    content = "class Demo {\n  void entry() { }\n}"
    code_index = {"files": [{
        "path": "Demo.java", "line_count": 3, "content": content, "classes": [],
        "methods": [{
            "id": "entry-method", "name": "entry", "class_name": "Demo",
            "qualified_class": "Demo", "symbol_key": "Demo#entry()",
            "start_line": 2, "end_line": 2,
        }],
    }]}
    candidate = {
        "evidence_level": "L2", "locations": [{"path": "Demo.java", "line": 2}],
        "sources": [{"path": "Demo.java", "line": 2}], "sinks": [{"path": "Demo.java", "line": 2}],
        "propagation_paths": [{"method_id": "entry-method", "resolved_target_id": "missing-method"}],
        "blocking_gaps": [], "coverage_gaps": [],
    }

    result = verify_candidate(candidate, code_index)

    assert result["propagation_paths"] == []
    assert result["invalid_propagation_paths"][0]["reason"] == "PROPAGATION_METHOD_NOT_FOUND"
