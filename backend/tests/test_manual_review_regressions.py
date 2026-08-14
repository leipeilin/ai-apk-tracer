from __future__ import annotations

import sys
from pathlib import Path

from app.analysis.indexer import build_code_index
from app.analysis.manifest import parse_manifest
from app.analysis.rule_runner import RuleRunner
from app.config import WORKSPACE_ROOT, RuleRuntimeSettings
from app.findings.severity import determine_severity

RULES_ROOT = WORKSPACE_ROOT / "rules"
if str(RULES_ROOT) not in sys.path:
    sys.path.insert(0, str(RULES_ROOT))

from shared.detector import execute  # noqa: E402


def file_record(path: str, content: str, methods: list[dict]) -> dict:
    return {"path": path, "content": content, "methods": methods}


def method(name: str, content: str, start: int = 1) -> dict:
    return {
        "name": name,
        "start_line": start,
        "end_line": start + content.count("\n"),
        "content": content,
    }


def component(kind: str, name: str, **values) -> dict:
    return {
        "kind": kind,
        "name": name,
        "exported": "true",
        "permission": None,
        "permission_protection": None,
        "read_permission": None,
        "write_permission": None,
        "intent_filters": [],
        "path_permissions": [],
        **values,
    }


def payload(rule_component: dict, files: list[dict]) -> dict:
    return {
        "manifest": {"analysis_platform_api": 36, "components": [rule_component]},
        "code_index": {"files": files},
    }


def test_import_intent_and_fixed_sink_do_not_form_activity_flow() -> None:
    content = """import android.content.Intent;
class DemoActivity {
  void open() { webView.loadUrl(\"https://fixed.example\"); }
}
"""
    files = [file_record("DemoActivity.java", content, [method("open", "void open() {\n webView.loadUrl(\"https://fixed.example\");\n}", 3)])]
    result = execute(
        "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        payload(component("activity", "com.example.DemoActivity"), files),
    )
    assert result["candidates"] == []


def test_provider_constant_and_unsupported_mutations_are_not_effects() -> None:
    content = """class EmptyProvider {
 int delete(Uri uri, String s, String[] a) { return 0; }
 Uri insert(Uri uri, ContentValues v) { return null; }
 int update(Uri uri, ContentValues v, String s, String[] a) { throw new UnsupportedOperationException(); }
}
"""
    files = [file_record("EmptyProvider.java", content, [
        method("delete", "int delete(Uri uri, String s, String[] a) {\n return 0;\n}", 2),
        method("insert", "Uri insert(Uri uri, ContentValues v) {\n return null;\n}", 3),
        method("update", "int update(Uri uri, ContentValues v, String s, String[] a) {\n throw new UnsupportedOperationException();\n}", 4),
    ])]
    result = execute(
        "PROVIDER_UNAUTHORIZED_MUTATION",
        payload(component("provider", "com.example.EmptyProvider"), files),
    )
    assert result["candidates"] == []


def test_thread_pool_execute_is_not_network_sink() -> None:
    content = """class DumpReceiver {
 void onReceive(Context c, Intent intent) {
   String action = intent.getAction();
   executor.execute(task);
 }
}
"""
    files = [file_record("DumpReceiver.java", content, [
        method("onReceive", "void onReceive(Context c, Intent intent) {\n String action = intent.getAction();\n executor.execute(task);\n}", 2),
    ])]
    result = execute(
        "RECEIVER_INPUT_TO_SINK",
        payload(component("receiver", "com.example.DumpReceiver"), files),
    )
    assert result["candidates"] == []


def test_local_broadcast_manager_is_not_external_receiver() -> None:
    content = """class InternalEvents {
 void register() { LocalBroadcastManager.getInstance(context).registerReceiver(receiver, filter); }
}
"""
    files = [file_record("InternalEvents.java", content, [
        method("register", "void register() {\n LocalBroadcastManager.getInstance(context).registerReceiver(receiver, filter);\n}", 2),
    ])]
    result = execute(
        "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION",
        {"manifest": {"analysis_platform_api": 36, "components": []}, "code_index": {"files": files}},
    )
    assert result["candidates"] == []


def test_manifest_records_provider_permission_strength_and_authority_conflicts(tmp_path: Path) -> None:
    manifest = tmp_path / "AndroidManifest.xml"
    manifest.write_text("""<manifest xmlns:android='http://schemas.android.com/apk/res/android' package='com.example'>
      <permission android:name='com.example.SIGNATURE' android:protectionLevel='signature'/>
      <application>
        <provider android:name='.One' android:exported='true' android:authorities='com.example.files' android:readPermission='com.example.SIGNATURE'/>
        <provider android:name='.Two' android:exported='true' android:authorities='com.example.files'/>
      </application>
    </manifest>""", "utf-8")
    parsed = parse_manifest(manifest)
    first = parsed["components"][0]
    assert first["read_permission_protection"] == "signature"
    assert parsed["authority_conflicts"]["com.example.files"] == ["com.example.One", "com.example.Two"]


def test_ai_failure_and_unproven_flow_cannot_be_high() -> None:
    severity, reasons = determine_severity({
        "evidence_level": "L2",
        "severity_hint": "high",
        "analysis_status": "ai_failed",
        "dataflow_status": "not_proven",
        "authorization_status": "unknown",
        "impact_status": "potential",
        "blocking_gaps": [{"code": "AI_ANALYSIS_FAILED", "critical": True}],
    })
    assert severity == "pending"
    assert reasons


def test_remote_binder_and_unsafe_file_provider_specialists(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    source_root = run_dir / "decompile" / "sources" / "com" / "example"
    source_root.mkdir(parents=True)
    for relative in ("index", "rule-work", "rule-results"):
        (run_dir / relative).mkdir(parents=True)
    (source_root / "RemoteService.java").write_text(
        """package com.example;
public class RemoteService {
 public Object onBind(Intent intent) { return new SportApiStub(); }
}
""",
        "utf-8",
    )
    (source_root / "SportApiStub.java").write_text(
        """package com.example;
public class SportApiStub extends ISportApi.Stub {
 SportManager manager;
 static final int TRANSACTION_startSport = 1;
 public boolean onTransact(int code, Parcel data, Parcel reply, int flags) {
  switch (code) { case 1: data.readInt(); startSport(); reply.writeNoException(); return true; }
  return false;
 }
 public void startSport() { manager.startSport(); }
}
""",
        "utf-8",
    )
    (source_root / "UnsafeFileProvider.java").write_text(
        """package com.example;
public class UnsafeFileProvider {
 private File resolve(Uri uri) {
   String path = uri.getPath();
   File canonical = new File(root, path).getCanonicalFile();
   if (!canonical.getPath().startsWith(root.getPath())) throw new SecurityException();
   return canonical;
 }
 public ParcelFileDescriptor openFile(Uri uri, String mode) {
   File file = resolve(uri);
   return ParcelFileDescriptor.open(file, parseMode(mode));
 }
 public int delete(Uri uri, String s, String[] a) { return resolve(uri).delete() ? 1 : 0; }
}
""",
        "utf-8",
    )
    descriptor = build_code_index(run_dir / "decompile" / "sources", run_dir / "index" / "code-index.json")
    remote = component("service", "com.example.RemoteService")
    provider = component(
        "provider",
        "com.example.UnsafeFileProvider",
        authorities="com.example.files",
    )
    duplicate = component(
        "provider",
        "com.example.OtherProvider",
        authorities="com.example.files",
        permission="com.example.SIGNATURE",
        permission_protection="signature",
    )
    manifest = {"analysis_platform_api": 36, "components": [remote, provider, duplicate]}
    candidates, failures = RuleRunner(RULES_ROOT, RuleRuntimeSettings()).run_all(run_dir, {
        "manifest": manifest,
        "index": {**descriptor, "allowed_index_root": (run_dir / "index").resolve().as_posix()},
        "config": {"analysis_platform_api": 36},
    })
    assert failures == []
    binder = next(item for item in candidates if item["rule_id"] == "SERVICE_BINDER_CALLER_CHECK_MISSING")
    assert binder["binder_remote_interface"] is True
    assert binder["dataflow_status"] == "interprocedural"
    file_provider = next(item for item in candidates if item["rule_id"] == "PROVIDER_URI_TO_FILE")
    assert file_provider["path_boundary_status"] == "unsafe_prefix"
    assert file_provider["authority_resolution_status"] == "ambiguous"
