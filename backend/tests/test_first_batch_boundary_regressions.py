from __future__ import annotations

import copy
import sys
from pathlib import Path

from app.analysis.candidate_funnel import CandidateFunnel, build_candidate_identity, exact_candidate_key
from app.analysis.indexer import build_code_index, parse_structured_parameters
from app.analysis.index_store import SQLiteCodeIndexReader
from app.findings.evidence import verify_candidate
from app.findings.aggregate import aggregate_candidates

RULES_ROOT = Path(__file__).resolve().parents[2] / "rules"
if str(RULES_ROOT) not in sys.path:
    sys.path.insert(0, str(RULES_ROOT))

from shared.dataflow import DataFlowAnalyzer, classify_operation_taxonomy  # noqa: E402
from shared.detector import execute  # noqa: E402
from shared.index_reader import RuleIndexReader  # noqa: E402


def _component(kind: str, name: str) -> dict:
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
        "grant_uri_patterns": [],
        "path_permissions": [],
        "provider_paths": [],
        "authority_tokens": ["com.example.provider"] if kind == "provider" else [],
    }


def _payload(tmp_path: Path, sources: dict[str, str], components: list[dict]) -> dict:
    source_root = tmp_path / "sources"
    for relative, content in sources.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    index_root = tmp_path / "index"
    descriptor = build_code_index(source_root, index_root / "code-index.json")
    return {
        "manifest": {
            "analysis_platform_api": 36,
            "target_sdk": 36,
            "components": components,
            "authority_conflicts": {},
            "custom_permissions": {},
            "protected_broadcast_actions": [],
            "protected_broadcast_catalog_version": "test-api-36",
        },
        "index": {**descriptor, "allowed_index_root": index_root.resolve().as_posix()},
    }


def test_braceless_multiline_conditional_enforce_is_partial_with_critical_gap() -> None:
    content = """void run(boolean allowed) {
 if (
   allowed
 )
   enforceCallingPermission(\"sig\", \"denied\");
 sensitive();
}"""
    method = {
        "id": "Demo.run", "name": "run", "qualified_class": "com.example.Demo",
        "path": "Demo.java", "start_line": 1, "end_line": 7, "content": content,
        "flow_ir": [],
        "call_sites": [
            {"ordinal": 1, "method_name": "enforceCallingPermission", "start_line": 5,
             "arguments": ['\"sig\"', '\"denied\"'], "receiver_type": "android.content.Context"},
            {"ordinal": 2, "method_name": "sensitive", "start_line": 6, "arguments": []},
        ],
    }
    analyzer = DataFlowAnalyzer([{"path": "Demo.java", "methods": [method]}])
    outcome = analyzer.guard_segment("Demo.run", boundary_ordinal=2)
    assert outcome["status"] == "present_partial"
    assert any(gap["code"] == "GUARD_DOMINANCE_UNKNOWN" and gap["critical"] for gap in outcome["blocking_gaps"])


def test_application_fqcns_with_framework_leaf_names_are_not_verified_operations() -> None:
    webview = classify_operation_taxonomy({
        "method_name": "loadUrl", "method_descriptor": "(String)->void",
        "receiver_type": "com.example.WebView", "receiver_text": "webView", "arguments": ['url'],
    }, "open", "com.example.Screen")
    service = classify_operation_taxonomy({
        "method_name": "startService", "method_descriptor": "(Intent)->void",
        "receiver_type": "com.example.Service", "receiver_text": "service", "arguments": ["intent"],
    }, "open", "com.example.Screen")
    assert webview["verified"] is False and webview["is_effect"] is False
    assert service["verified"] is False and service["is_effect"] is False


def test_custom_register_receiver_is_skipped_but_wrapper_bottoms_out_at_context(tmp_path: Path) -> None:
    custom = _payload(tmp_path / "custom", {
        "com/example/Demo.java": """package com.example;
class Demo { void run(CustomContext c, DemoReceiver r, IntentFilter f) { c.registerReceiver(r, f, 2); } }
class CustomContext { void registerReceiver(DemoReceiver r, IntentFilter f, int flags) {} }
class DemoReceiver { void onReceive(Context c, Intent i) {} }
""",
    }, [])
    assert execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", custom)["candidates"] == []

    wrapped = _payload(tmp_path / "wrapped", {
        "com/example/Demo.java": """package com.example;
import android.content.Context;
class Demo {
 Context context;
 void registerReceiver(DemoReceiver r, IntentFilter f, int flags) { context.registerReceiver(r, f, flags); }
 void run() { DemoReceiver r = new DemoReceiver(); IntentFilter f = new IntentFilter(\"com.example.X\"); registerReceiver(r, f, 2); }
}
class DemoReceiver { void onReceive(Context c, Intent i) {} }
""",
    }, [])
    candidates = execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", wrapped)["candidates"]
    assert len(candidates) == 1
    registration = candidates[0]["receiver_binding"]["registration"]
    assert registration["api_family"] == "platform_context"
    assert registration["line"] == 5


def test_binder_reply_requires_dispatch_result_argument(tmp_path: Path) -> None:
    service = _component("service", "com.example.RemoteService")
    payload = _payload(tmp_path, {
        "com/example/RemoteService.java": """package com.example;
class RemoteService {
 Object onBind(Intent i) { return new Impl(); }
}
class Impl extends Api.Stub {
 String getToken() { return \"secret\"; }
}
""",
        "com/example/Api.java": """package com.example;
class Api { static class Stub extends Binder {
 static final int TRANSACTION_getToken=1;
 boolean onTransact(int code, Parcel data, Parcel reply, int flags) {
  switch(code) {
  case TRANSACTION_getToken:
   String ignored = getToken();
   reply.writeString(\"constant\");
   return true;
  }
  return false;
 }
 String getToken() { return null; }
}}
""",
    }, [service])
    reader = RuleIndexReader(payload["index"])
    try:
        transaction = reader.binder_components([service["name"]])[service["name"]]["transactions"][0]
    finally:
        reader.close()
    assert transaction["dispatch_assigned_to"] == "ignored"
    assert transaction["reply_write_call_sites"][0]["arguments"] == ['\"constant\"']
    assert execute("SERVICE_BINDER_CALLER_CHECK_MISSING", payload)["candidates"] == []


def test_provider_scope_excludes_invalid_same_name_overload(tmp_path: Path) -> None:
    provider = _component("provider", "com.example.DataProvider")
    payload = _payload(tmp_path, {
        "com/example/DataProvider.java": """package com.example;
class DataProvider extends ContentProvider {
 Cursor query(String key) { return null; }
 Cursor query(Uri uri, String[] projection, String selection, String[] args, String order) { return null; }
}
""",
    }, [provider])
    reader = RuleIndexReader(payload["index"])
    try:
        scopes = reader.provider_entry_scopes(provider["name"])
    finally:
        reader.close()
    assert [(scope["entry_name"], scope["entry_descriptor"]) for scope in scopes] == [
        ("query", "(Uri,String[],String,String[],String)->Cursor")
    ]


def test_provider_query_sensitivity_is_bound_to_selected_columns_not_method_text(tmp_path: Path) -> None:
    provider = _component("provider", "com.example.DataProvider")
    payload = _payload(tmp_path, {
        "com/example/DataProvider.java": """package com.example;
import android.database.sqlite.SQLiteDatabase;
class DataProvider { SQLiteDatabase db;
 Cursor query(Uri uri, String[] projection, String selection, String[] args, String order) {
  String tokenForLogging = \"token\";
  return db.rawQuery(\"SELECT display_name FROM users\", null);
 }
}
""",
    }, [provider])
    assert execute("PROVIDER_UNAUTHORIZED_QUERY", payload)["candidates"] == []

    unknown = _payload(tmp_path / "unknown", {
        "com/example/DataProvider.java": """package com.example;
import android.database.sqlite.SQLiteDatabase;
class DataProvider { SQLiteDatabase db;
 Cursor query(Uri uri, String[] projection, String selection, String[] args, String order) {
  return db.query(\"users\", projection, selection, args, null, null, order);
 }
}
""",
    }, [provider])
    candidate = execute("PROVIDER_UNAUTHORIZED_QUERY", unknown)["candidates"][0]
    assert candidate["sinks"][0]["sensitive_result"] is False
    assert any(gap["code"] == "PROVIDER_QUERY_SENSITIVITY_UNPROVEN" for gap in candidate["blocking_gaps"])


def test_provider_query_helper_delegation_with_column_vocabulary(tmp_path: Path) -> None:
    """S3：query→私有 helper 委托 + 常量投影列名词表（DeviceProvider V-03 型）。"""

    provider = _component("provider", "com.example.DeviceProvider")
    payload = _payload(tmp_path, {
        "com/example/DeviceProvider.java": """package com.example;
class DeviceProvider extends ContentProvider {
 private static final String[] DEFAULT_DEVICE_PROJECTION = {"device_name", "device_type", "device_icon"};
 Cursor query(Uri uri, String[] projection, String selection, String[] args, String order) {
  if (matcher.match(uri) == 0) { return queryDeviceStatus(); }
  return null;
 }
 private Cursor queryDeviceStatus() {
  MatrixCursor cursor = new MatrixCursor(DEFAULT_DEVICE_PROJECTION);
  cursor.newRow().add(\"device_name\", getName());
  cursor.newRow().add(DeviceContractKt.COLUMN_DEVICE_BATTERY, getBattery());
  return cursor;
 }
}
class DeviceContractKt {
 static final String COLUMN_DEVICE_BATTERY = \"device_battery\";
}
""",
    }, [provider])
    result = execute("PROVIDER_UNAUTHORIZED_QUERY", payload)
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["evidence_level"] == "L2"
    assert candidate["deterministic_chain_verified"] is True
    assert candidate["sinks"][0]["sensitive_data_evidence"] in {"device_name", "device_battery"}
    assert candidate["sinks"][0]["effect_verified"] is True
    # S5：sink 的 method_id 必须与 line 所在方法一致，证据回查不得剥离 sink。
    reader = SQLiteCodeIndexReader(payload["index"])
    try:
        verified = verify_candidate(candidate, {"files": []}, reader)
    finally:
        reader.close()
    assert verified["invalid_sinks"] == [], verified["invalid_sinks"]
    assert any(item.get("kind") == "sensitive_query_result" for item in verified["sinks"])


def test_provider_query_helper_delegation_respects_permission(tmp_path: Path) -> None:
    """S3：受权限保护的 Provider（CarIconProvider 型）不得因列名词表误升级。"""

    provider = _component("provider", "com.example.CarIconProvider")
    provider.update({
        "permission": "miui.permission.USE_INTERNAL_GENERAL_API",
        "permission_protection": "signature",
    })
    payload = _payload(tmp_path, {
        "com/example/CarIconProvider.java": """package com.example;
class CarIconProvider extends ContentProvider {
 Cursor query(Uri uri, String[] projection, String selection, String[] args, String order) {
  MatrixCursor cursor = new MatrixCursor(new String[]{\"device_name\", \"device_battery\"});
  cursor.newRow().add(\"device_name\", getName());
  return cursor;
 }
}
""",
    }, [provider])
    assert execute("PROVIDER_UNAUTHORIZED_QUERY", payload)["candidates"] == []


def test_kotlin_default_comparison_does_not_consume_following_parameter() -> None:
    parameters = parse_structured_parameters(
        "x: Int = if (a < b) 1 else 0, y: String",
        language="kotlin", method_name="demo", package="com.example",
    )
    assert [(item["name"], item["normalized_type"]) for item in parameters] == [
        ("x", "Int"), ("y", "String")
    ]


def test_semantic_set_order_is_canonical_but_propagation_order_is_preserved() -> None:
    base = {
        "rule_id": "RULE_A", "rule_ids": ["RULE_B", "RULE_A"], "rule_version": "1",
        "evidence_level": "L2", "component": "provider", "component_name": "com.example.P",
        "entry_points": ["P#query2", "P#query1"], "operation_modes": ["rw", "r"],
        "actions": ["b", "a"], "permissions": ["p2", "p1"],
        "authorization_matrix": [{"path_region": "/b"}, {"path_region": "/a"}],
        "authorization_status": "unprotected", "guard_status": "absent",
        "deterministic_chain_verified": True, "dataflow_status": "intraprocedural",
        "impact_status": "statically_confirmed", "sources": [{"kind": "source", "line": 1}],
        "sinks": [{"kind": "sink", "line": 2, "effect_verified": True}],
        "propagation_paths": [{"ordinal": 1}, {"ordinal": 2}], "locations": [],
    }
    reordered = copy.deepcopy(base)
    for field in ("rule_ids", "entry_points", "operation_modes", "actions", "permissions", "authorization_matrix"):
        reordered[field] = list(reversed(reordered[field]))

    assert exact_candidate_key(base) == exact_candidate_key(reordered)
    assert build_candidate_identity(base) == build_candidate_identity(reordered)
    first_id = CandidateFunnel().process([copy.deepcopy(base)]).candidates[0]["candidate_id"]
    second_id = CandidateFunnel().process([copy.deepcopy(reordered)]).candidates[0]["candidate_id"]
    assert first_id == second_id
    assert aggregate_candidates([copy.deepcopy(base)])[0]["id"] == aggregate_candidates([copy.deepcopy(reordered)])[0]["id"]

    reversed_path = copy.deepcopy(base)
    reversed_path["propagation_paths"].reverse()
    assert exact_candidate_key(base) != exact_candidate_key(reversed_path)
    assert build_candidate_identity(base).chain_key != build_candidate_identity(reversed_path).chain_key
