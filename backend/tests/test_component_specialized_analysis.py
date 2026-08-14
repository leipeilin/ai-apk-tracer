from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.analysis.indexer import build_code_index
from app.analysis.index_store import SQLiteCodeIndexReader
from app.findings.evidence import verify_candidate

RULES_ROOT = Path(__file__).resolve().parents[2] / "rules"
if str(RULES_ROOT) not in sys.path:
    sys.path.insert(0, str(RULES_ROOT))

from shared.dataflow import DataFlowAnalyzer  # noqa: E402
from shared.detector import execute  # noqa: E402
from shared.index_reader import RuleIndexReader  # noqa: E402


def _build_analyzer(
    tmp_path: Path,
    sources: dict[str, str],
    component: str,
    entries: set[str],
) -> tuple[DataFlowAnalyzer, dict, dict]:
    source_root = tmp_path / "sources"
    for relative, content in sources.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    index_root = tmp_path / "index"
    descriptor = build_code_index(source_root, index_root / "code-index.json")
    index = {**descriptor, "allowed_index_root": index_root.resolve().as_posix()}
    reader = RuleIndexReader(index)
    try:
        scope = reader.component_flow_scope(component, entries)
    finally:
        reader.close()
    analyzer = DataFlowAnalyzer(scope["files"], scope["entry_method_ids"], scope["gaps"])
    return analyzer, scope, index


def _build_many_methods_index(tmp_path: Path, method_count: int) -> dict:
    source_root = tmp_path / "sources"
    path = source_root / "com" / "example" / "ManyMethods.java"
    path.parent.mkdir(parents=True)
    methods = "\n".join(
        f"void method{index}() {{ sink({index}); }}"
        for index in range(method_count)
    )
    path.write_text(
        f"package com.example;\npublic class ManyMethods {{\n{methods}\nvoid sink(int value) {{}}\n}}\n",
        "utf-8",
    )
    return build_code_index(source_root, tmp_path / "code-index.json")


def _legacy_methods(reader: SQLiteCodeIndexReader, file_id: int) -> list[dict]:
    methods = []
    for row in reader.db.execute(
        "SELECT * FROM methods WHERE file_id=? ORDER BY start_line",
        (file_id,),
    ):
        method = reader._method(row)
        method["call_sites"] = [
            reader._call_site(call)
            for call in reader.db.execute(
                "SELECT * FROM call_sites WHERE method_id=? ORDER BY ordinal",
                (method["id"],),
            )
        ]
        methods.append(method)
    return methods


def test_load_structure_files_uses_constant_query_count_and_preserves_structure(tmp_path: Path) -> None:
    query_counts = []
    for method_count in (1, 40):
        reader = SQLiteCodeIndexReader(_build_many_methods_index(tmp_path / str(method_count), method_count))
        statements: list[str] = []
        reader.db.set_trace_callback(statements.append)
        try:
            files = reader.load_structure_files()
            reader.db.set_trace_callback(None)
            expected_methods = _legacy_methods(reader, files[0]["_file_id"])
            method_indexes = {row["name"] for row in reader.db.execute("PRAGMA index_list(methods)")}
            call_indexes = {row["name"] for row in reader.db.execute("PRAGMA index_list(call_sites)")}
        finally:
            reader.close()
        selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
        query_counts.append(len(selects))
        assert files[0]["methods"] == expected_methods
        assert [method["name"] for method in files[0]["methods"]] == [
            *(f"method{index}" for index in range(method_count)),
            "sink",
        ]
        assert all(
            [call["ordinal"] for call in method["call_sites"]] == sorted(
                call["ordinal"] for call in method["call_sites"]
            )
            for method in files[0]["methods"]
        )
        assert {
            "idx_methods_qualified_class_name_descriptor",
            "idx_methods_file_start_end",
        }.issubset(method_indexes)
        assert {
            "idx_call_sites_method_ordinal",
            "idx_call_sites_resolved_target_id",
        }.issubset(call_indexes)
    assert query_counts == [4, 4]


def test_evidence_verification_loads_full_method_index_once_for_100_candidates(
    tmp_path: Path,
) -> None:
    descriptor = _build_many_methods_index(tmp_path, 10)
    reader = SQLiteCodeIndexReader(descriptor)
    statements: list[str] = []
    reader.db.set_trace_callback(statements.append)
    try:
        for index in range(100):
            candidate = {
                "rule_id": f"RULE_{index}",
                "evidence_level": "L2",
                "locations": [{"path": "com/example/ManyMethods.java", "line": 3}],
                "sources": [{"path": "com/example/ManyMethods.java", "line": 3}],
                "sinks": [{"path": "com/example/ManyMethods.java", "line": 3}],
                "blocking_gaps": [],
                "coverage_gaps": [],
            }
            verify_candidate(candidate, descriptor, reader)
    finally:
        reader.close()

    normalized = [" ".join(statement.upper().split()) for statement in statements]
    full_method_scans = [
        statement for statement in normalized
        if "FROM METHODS M JOIN FILES F ON F.ID=M.FILE_ID" in statement
    ]
    assert len(full_method_scans) == 1


def test_component_files_batches_methods_classes_and_call_sites(tmp_path: Path) -> None:
    methods = "\n".join(f"void method{index}() {{ sink({index}); }}" for index in range(40))
    source_root = tmp_path / "sources"
    source_path = source_root / "com/example/ManyMethods.java"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        f"package com.example;\npublic class ManyMethods {{\n{methods}\nvoid sink(int value) {{}}\n}}\n",
        "utf-8",
    )
    index_root = tmp_path / "index"
    descriptor = build_code_index(source_root, index_root / "code-index.json")
    reader = RuleIndexReader({
        **descriptor,
        "allowed_index_root": index_root.resolve().as_posix(),
    })
    statements: list[str] = []
    reader.db.set_trace_callback(statements.append)
    try:
        files = reader.component_files("com.example.ManyMethods")
    finally:
        reader.close()
    selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 4
    assert len(files) == 1
    assert len(files[0]["methods"]) == 41
    assert all(
        [call["ordinal"] for call in method["call_sites"]] == sorted(
            call["ordinal"] for call in method["call_sites"]
        )
        for method in files[0]["methods"]
    )


def _activity(body: str, parameters: str = "Intent intent") -> str:
    return f"""package com.example;
class RouterActivity {{
 WebView web;
 void onCreate({parameters}) {{
{body}
 }}
}}
"""


def _service(body: str) -> str:
    return f"""package com.example;
class CommandService {{
 Context context;
 SportManager manager;
 int onStartCommand(Intent intent, int flags, int startId) {{
  Intent command = new Intent();
{body}
  return 1;
 }}
}}
"""


def _receiver(flag: str = "Context.RECEIVER_EXPORTED", local: bool = False, overloaded: bool = False) -> str:
    registration = (
        "  LocalBroadcastManager.getInstance(context).registerReceiver(receiver, filter);"
        if local
        else f"  registerReceiver(receiver, filter, {flag});"
    )
    overload = "\n void onReceive(Object context, Intent intent) { log(intent); }" if overloaded else ""
    return f"""package com.example;
class DemoReceiver {{
 SportManager manager;
 void register() {{
  DemoReceiver receiver = new DemoReceiver();
  IntentFilter filter = new IntentFilter("com.example.SYNC");
{registration}
 }}
 void onReceive(Context context, Intent intent) {{
  String action = intent.getAction();
  if ("com.example.SYNC".equals(action)) {{ manager.startSport(); }}
 }}{overload}
}}
"""


def _manifest_component(kind: str, name: str) -> dict:
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
    }


def _payload(index: dict, component: dict | None = None) -> dict:
    return {
        "manifest": {
            "analysis_platform_api": 36,
            "target_sdk": 36,
            "components": [component] if component else [],
            "custom_permissions": {},
            "authority_conflicts": {},
        },
        "index": index,
    }


def test_router_put_extras_overwrite_is_the_only_validation_bypass(tmp_path: Path) -> None:
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {
            "com/example/RouterActivity.java": _activity(
                '  String target = intent.getStringExtra("target");\n'
                "  if (!isAllowedHttps(target)) return;\n"
                "  Intent routed = new Intent();\n"
                '  routed.putExtra("target", target);\n'
                "  routed.putExtras(extras);\n"
                '  web.loadUrl(routed.getStringExtra("target"));',
                "Intent intent, Bundle extras",
            )
        },
        "com.example.RouterActivity",
        {"onCreate"},
    )
    flow = analyzer.analyze_entry({"onCreate"})
    assert flow["sink"]["kind"] == "webview"
    assert flow["final_reaching_state"] == "maybe_untrusted"
    assert [(item["finding_type"], item["key"], item["overwrite_operation"]) for item in flow["router_validation_bypasses"]] == [
        ("ROUTER_VALIDATION_BYPASS", "target", "putExtras")
    ]


def test_router_without_slot_overwrite_remains_negative(tmp_path: Path) -> None:
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {
            "com/example/RouterActivity.java": _activity(
                '  String target = intent.getStringExtra("target");\n'
                "  if (!isAllowedHttps(target)) return;\n"
                "  Intent routed = new Intent();\n"
                '  routed.putExtra("target", target);\n'
                '  routed.putExtra("other", intent.getStringExtra("override"));\n'
                '  web.loadUrl(routed.getStringExtra("target"));'
            )
        },
        "com.example.RouterActivity",
        {"onCreate"},
    )
    flow = analyzer.analyze_entry({"onCreate"})
    assert flow["sink"] is None
    assert flow["slot_overwrites"] == []
    assert flow["router_validation_bypasses"] == []


def test_external_fragment_class_for_name_is_verified(tmp_path: Path) -> None:
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/RouterActivity.java": _activity(
            '  String className = intent.getStringExtra("className");\n  Class<?> type = java.lang.Class.forName(className);'
        )},
        "com.example.RouterActivity",
        {"onCreate"},
    )
    flow = analyzer.analyze_entry({"onCreate"})
    fragment = analyzer.fragment_reflection_analysis(flow)
    assert fragment["status"] == "verified"
    assert fragment["source"]["kind"] in {"source", "entry_parameter"}
    assert fragment["class_name_sink"]["kind"] == "fragment_reflection"


def test_external_fragment_instantiate_is_verified(tmp_path: Path) -> None:
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/RouterActivity.java": _activity(
            '  String className = intent.getStringExtra("className");\n  Fragment.instantiate(this, className);'
        )},
        "com.example.RouterActivity",
        {"onCreate"},
    )
    fragment = analyzer.fragment_reflection_analysis(analyzer.analyze_entry({"onCreate"}))
    assert fragment["status"] == "verified"
    assert fragment["class_name_sink"]["taxonomy"] == "ui_navigation"


def test_fixed_fragment_class_is_suppressed(tmp_path: Path) -> None:
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/RouterActivity.java": _activity('  Fragment.instantiate(this, "com.example.SafeFragment");')},
        "com.example.RouterActivity",
        {"onCreate"},
    )
    flow = analyzer.analyze_entry({"onCreate"})
    fragment = analyzer.fragment_reflection_analysis(flow)
    assert flow["sink"] is None
    assert fragment["status"] == "suppressed"
    assert fragment["allowlist"] == "fail_closed_or_fixed_mapping"


def test_fail_closed_fragment_allowlist_is_suppressed(tmp_path: Path) -> None:
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/RouterActivity.java": _activity(
            '  String className = intent.getStringExtra("className");\n'
            "  if (!ALLOWED.contains(className)) return;\n"
            "  Fragment.instantiate(this, className);"
        )},
        "com.example.RouterActivity",
        {"onCreate"},
    )
    flow = analyzer.analyze_entry({"onCreate"})
    fragment = analyzer.fragment_reflection_analysis(flow)
    assert flow["sink"] is None
    assert fragment["status"] == "suppressed"


def test_started_service_action_controls_effect_branch(tmp_path: Path) -> None:
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/CommandService.java": _service(
            '  String action = intent.getAction();\n  if ("START".equals(action)) { context.startService(command); }'
        )},
        "com.example.CommandService",
        {"onStartCommand"},
    )
    machine = analyzer.started_service_state_machine(analyzer.analyze_entry({"onStartCommand"}))
    assert machine["status"] == "verified"
    assert len(machine["transitions"]) == 1
    assert machine["transitions"][0]["event"] == "START"
    assert machine["transitions"][0]["effect_taxonomy"] == "connection_session_control"


def test_started_service_extra_controls_effect_branch(tmp_path: Path) -> None:
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/CommandService.java": _service(
            '  String mode = intent.getStringExtra("mode");\n  if ("START".equals(mode)) { manager.startSport(); }'
        )},
        "com.example.CommandService",
        {"onStartCommand"},
    )
    machine = analyzer.started_service_state_machine(analyzer.analyze_entry({"onStartCommand"}))
    transition = machine["transitions"][0]
    assert machine["status"] == "verified"
    assert transition["key"] == "mode"
    assert transition["effect_taxonomy"] == "location_sensor_collection"


def test_started_service_reachable_effect_without_event_branch_does_not_close(tmp_path: Path) -> None:
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/CommandService.java": _service(
            "  String action = intent.getAction();\n  context.startService(command);"
        )},
        "com.example.CommandService",
        {"onStartCommand"},
    )
    machine = analyzer.started_service_state_machine(analyzer.analyze_entry({"onStartCommand"}))
    assert machine["status"] == "not_proven"
    assert machine["transitions"] == []
    assert any(gap["code"] == "SERVICE_EVENT_EFFECT_BINDING_UNKNOWN" and gap["critical"] for gap in machine["coverage_gaps"])


def test_started_service_ambiguous_branch_target_is_not_selected_by_name(tmp_path: Path) -> None:
    source = """package com.example;
class CommandService {
 SportManager manager;
 int onStartCommand(Intent intent, int flags, int startId) {
  String action = intent.getAction();
  if ("START".equals(action)) { apply(intent.getStringExtra("mode")); }
  return 1;
 }
 void apply(String value) { manager.startSport(); }
 void apply(int value) { log(value); }
}
"""
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/CommandService.java": source},
        "com.example.CommandService",
        {"onStartCommand"},
    )
    machine = analyzer.started_service_state_machine(analyzer.analyze_entry({"onStartCommand"}))
    assert machine["transitions"] == []
    assert any(gap["code"] == "SERVICE_EVENT_EFFECT_BRANCH_TARGET_AMBIGUOUS" and gap["critical"] for gap in machine["coverage_gaps"])


def test_dynamic_receiver_unique_registration_to_effect_binding(tmp_path: Path) -> None:
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/DemoReceiver.java": _receiver()},
        "com.example.DemoReceiver",
        {"register", "onReceive"},
    )
    binding = analyzer.dynamic_receiver_bindings()[0]
    assert binding["registration"]["externally_reachable"] is True
    assert "DemoReceiver.onReceive" in binding["on_receive"]
    assert binding["binding_complete"] is True
    assert binding["transitions"][0]["effect_taxonomy"] == "location_sensor_collection"


def test_dynamic_receiver_not_exported_is_negative(tmp_path: Path) -> None:
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/DemoReceiver.java": _receiver("Context.RECEIVER_NOT_EXPORTED")},
        "com.example.DemoReceiver",
        {"register", "onReceive"},
    )
    binding = analyzer.dynamic_receiver_bindings()[0]
    assert binding["registration"]["externally_reachable"] is False
    assert binding["binding_complete"] is False


def test_dynamic_receiver_local_broadcast_is_negative(tmp_path: Path) -> None:
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/DemoReceiver.java": _receiver(local=True)},
        "com.example.DemoReceiver",
        {"register", "onReceive"},
    )
    binding = analyzer.dynamic_receiver_bindings()[0]
    assert binding["registration"]["local_broadcast"] is True
    assert binding["registration"]["externally_reachable"] is False
    assert binding["binding_complete"] is False


def test_dynamic_receiver_ignores_non_lifecycle_on_receive_overload(tmp_path: Path) -> None:
    source = _receiver(overloaded=True).replace(
        "SportManager manager;", "android.hardware.SensorManager manager;"
    ).replace(
        "manager.startSport();", "manager.registerListener(listener, sensor, 3);"
    )
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/DemoReceiver.java": source},
        "com.example.DemoReceiver",
        {"register", "onReceive"},
    )
    binding = analyzer.dynamic_receiver_bindings()[0]
    assert "DemoReceiver.onReceive" in binding["on_receive"]
    assert binding["binding_complete"] is True
    assert binding["transitions"][0]["effect_taxonomy"] == "location_sensor_collection"
    assert not any(
        gap["code"] == "RECEIVER_TARGET_AMBIGUOUS"
        for gap in binding["coverage_gaps"]
    )


@pytest.mark.parametrize(
    ("declaration", "call", "taxonomy"),
    [
        ("WebView web;", 'web.loadUrl("https://example.test");', "data_disclosure"),
        ("android.content.SharedPreferences.Editor editor;", 'editor.putString("key", "value");', "persistent_state_write"),
        ("BluetoothGatt gatt;", "gatt.writeCharacteristic(value);", "device_protocol_output"),
        ("LiveData events;", "events.postValue(value);", "callback_event_injection"),
        ("LocationManager location;", "location.requestLocationUpdates(provider, 0, 0, listener);", "location_sensor_collection"),
        ("BluetoothGatt gatt;", "gatt.connect();", "connection_session_control"),
        ("Context context; Intent next;", "context.startActivity(next);", "ui_navigation"),
        ("java.io.File file;", "file.delete();", "file_mutation"),
        ("SQLiteDatabase database;", 'database.execSQL("DELETE FROM item");', "database_mutation"),
    ],
)
def test_operation_taxonomy_covers_specialized_effects(
    tmp_path: Path,
    declaration: str,
    call: str,
    taxonomy: str,
) -> None:
    source = f"""package com.example;
class TaxonomyActivity {{
 void onCreate(Intent intent) {{
  {declaration}
  Object value = new Object();
  {call}
 }}
}}
"""
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/TaxonomyActivity.java": source},
        "com.example.TaxonomyActivity",
        {"onCreate"},
    )
    summary = next(iter(analyzer.summaries.values()))
    assert any(effect["taxonomy"] == taxonomy and effect["verified"] for effect in summary["side_effects"])


def test_detector_router_bypass_requires_actual_slot_overwrite(tmp_path: Path) -> None:
    positive, _, positive_index = _build_analyzer(
        tmp_path / "positive",
        {"com/example/RouterActivity.java": _activity(
            '  String target = intent.getStringExtra("target");\n'
            "  if (!isAllowedHttps(target)) return;\n"
            "  Intent routed = new Intent();\n"
            '  routed.putExtra("target", target);\n'
            "  routed.putExtras(extras);\n"
            '  web.loadUrl(routed.getStringExtra("target"));',
            "Intent intent, Bundle extras",
        )},
        "com.example.RouterActivity",
        {"onCreate"},
    )
    assert positive is not None
    component = _manifest_component("activity", "com.example.RouterActivity")
    candidate = execute("ACTIVITY_INTENT_TO_SENSITIVE_SINK", _payload(positive_index, component))["candidates"][0]
    assert candidate["router_validation_bypass"][0]["overwrite_operation"] == "putExtras"
    assert candidate["sinks"][0]["kind"] == "webview"
    assert candidate["deterministic_chain_verified"] is True

    _, _, negative_index = _build_analyzer(
        tmp_path / "negative",
        {"com/example/RouterActivity.java": _activity(
            '  String target = intent.getStringExtra("target");\n'
            "  if (!isAllowedHttps(target)) return;\n"
            "  Intent routed = new Intent();\n"
            '  routed.putExtra("target", target);\n'
            '  web.loadUrl(routed.getStringExtra("target"));'
        )},
        "com.example.RouterActivity",
        {"onCreate"},
    )
    assert execute("ACTIVITY_INTENT_TO_SENSITIVE_SINK", _payload(negative_index, component))["candidates"] == []


def test_detector_fragment_external_class_positive_fixed_class_negative(tmp_path: Path) -> None:
    component = _manifest_component("activity", "com.example.RouterActivity")
    _, _, positive_index = _build_analyzer(
        tmp_path / "positive",
        {"com/example/RouterActivity.java": _activity(
            '  String className = intent.getStringExtra("className");\n  Fragment.instantiate(this, className);'
        )},
        "com.example.RouterActivity",
        {"onCreate"},
    )
    candidate = execute("ACTIVITY_INTENT_TO_SENSITIVE_SINK", _payload(positive_index, component))["candidates"][0]
    assert candidate["fragment_reflection"]["status"] == "verified"
    assert candidate["operation_taxonomy"] == "ui_navigation"
    assert candidate["impact_status"] == "statically_confirmed"

    _, _, fixed_index = _build_analyzer(
        tmp_path / "fixed",
        {"com/example/RouterActivity.java": _activity('  Fragment.instantiate(this, "com.example.SafeFragment");')},
        "com.example.RouterActivity",
        {"onCreate"},
    )
    assert execute("ACTIVITY_INTENT_TO_SENSITIVE_SINK", _payload(fixed_index, component))["candidates"] == []


def test_detector_dynamic_receiver_only_confirms_unique_exported_binding(tmp_path: Path) -> None:
    _, _, exported_index = _build_analyzer(
        tmp_path / "exported",
        {"com/example/DemoReceiver.java": _receiver()},
        "com.example.DemoReceiver",
        {"register", "onReceive"},
    )
    exported = execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", _payload(exported_index))["candidates"]
    assert len(exported) == 1
    assert exported[0]["receiver_binding"]["binding_complete"] is True
    assert exported[0]["impact_status"] == "statically_confirmed"

    _, _, private_index = _build_analyzer(
        tmp_path / "private",
        {"com/example/DemoReceiver.java": _receiver("Context.RECEIVER_NOT_EXPORTED")},
        "com.example.DemoReceiver",
        {"register", "onReceive"},
    )
    assert execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", _payload(private_index))["candidates"] == []


def test_dynamic_receiver_numeric_export_flags_are_structured(tmp_path: Path) -> None:
    def source(flag: str) -> str:
        return f"""package com.example;
class NumericReceiver {{
 SportManager manager;
 void register(Context context) {{
  NumericReceiver receiver = new NumericReceiver();
  IntentFilter filter = new IntentFilter("com.example.SYNC");
  context.registerReceiver(receiver, filter, {flag});
 }}
 void onReceive(Context context, Intent intent) {{ manager.startSport(); }}
}}
"""

    _, _, exported_index = _build_analyzer(
        tmp_path / "exported",
        {"com/example/NumericReceiver.java": source("2")},
        "com.example.NumericReceiver",
        {"register", "onReceive"},
    )
    candidates = execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", _payload(exported_index))["candidates"]
    assert len(candidates) == 1
    registration = candidates[0]["receiver_binding"]["registration"]
    assert registration["flags_value"] == 2
    assert registration["export_status"] == "exported"

    _, _, private_index = _build_analyzer(
        tmp_path / "private",
        {"com/example/NumericReceiver.java": source("0x4")},
        "com.example.NumericReceiver",
        {"register", "onReceive"},
    )
    assert execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", _payload(private_index))["candidates"] == []


def test_dynamic_receiver_context_compat_argument_roles(tmp_path: Path) -> None:
    source = """package com.example;
class CompatReceiver {
 SportManager manager;
 void register(Context context) {
  CompatReceiver receiver = new CompatReceiver();
  IntentFilter filter = new IntentFilter("com.example.SYNC");
  ContextCompat.registerReceiver(context, receiver, filter, 2);
 }
 void onReceive(Context context, Intent intent) { manager.startSport(); }
}
"""
    _, _, index = _build_analyzer(
        tmp_path,
        {"com/example/CompatReceiver.java": source},
        "com.example.CompatReceiver",
        {"register", "onReceive"},
    )
    candidate = execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", _payload(index))["candidates"][0]
    registration = candidate["receiver_binding"]["registration"]
    assert registration["api_family"] == "context_compat"
    assert registration["overload"] == "context_compat_flags"
    assert registration["receiver_expression"] == "receiver"
    assert registration["filter_expression"] == "filter"
    assert registration["flags_expression"] == "2"


def test_dynamic_receiver_permission_policy_suppresses_strong_and_keeps_unresolved(tmp_path: Path) -> None:
    strong = """package com.example;
class PermissionReceiver {
 SportManager manager;
 void register(Context context) {
  PermissionReceiver receiver = new PermissionReceiver();
  IntentFilter filter = new IntentFilter("com.example.SYNC");
  context.registerReceiver(receiver, filter, Manifest.permission.BIND_VPN_SERVICE, null, 2);
 }
 void onReceive(Context context, Intent intent) { manager.startSport(); }
}
"""
    _, _, strong_index = _build_analyzer(
        tmp_path / "strong",
        {"com/example/PermissionReceiver.java": strong},
        "com.example.PermissionReceiver",
        {"register", "onReceive"},
    )
    assert execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", _payload(strong_index))["candidates"] == []

    unresolved = strong.replace("Manifest.permission.BIND_VPN_SERVICE", "permissionProvider.value()")
    _, _, unresolved_index = _build_analyzer(
        tmp_path / "unresolved",
        {"com/example/PermissionReceiver.java": unresolved},
        "com.example.PermissionReceiver",
        {"register", "onReceive"},
    )
    candidate = execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", _payload(unresolved_index))["candidates"][0]
    assert candidate["authorization_status"] == "unknown"
    assert any(
        gap["code"] == "DYNAMIC_RECEIVER_PERMISSION_UNRESOLVED" and gap["critical"]
        for gap in candidate["blocking_gaps"]
    )


def test_dynamic_receiver_protected_only_suppressed_but_mixed_actions_reported(tmp_path: Path) -> None:
    protected = """package com.example;
class ActionReceiver {
 SportManager manager;
 void register(Context context) {
  ActionReceiver receiver = new ActionReceiver();
  IntentFilter filter = new IntentFilter("android.intent.action.BOOT_COMPLETED");
  context.registerReceiver(receiver, filter, 2);
 }
 void onReceive(Context context, Intent intent) { manager.startSport(); }
}
"""
    _, _, protected_index = _build_analyzer(
        tmp_path / "protected",
        {"com/example/ActionReceiver.java": protected},
        "com.example.ActionReceiver",
        {"register", "onReceive"},
    )
    protected_payload = _payload(protected_index)
    protected_payload["manifest"].update({
        "protected_broadcast_catalog_version": "test",
        "protected_broadcast_actions": ["android.intent.action.BOOT_COMPLETED"],
    })
    assert execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", protected_payload)["candidates"] == []

    mixed = protected.replace(
        "  context.registerReceiver(receiver, filter, 2);",
        '  filter.addAction("com.example.CUSTOM");\n  context.registerReceiver(receiver, filter, 2);',
    )
    _, _, mixed_index = _build_analyzer(
        tmp_path / "mixed",
        {"com/example/ActionReceiver.java": mixed},
        "com.example.ActionReceiver",
        {"register", "onReceive"},
    )
    mixed_payload = _payload(mixed_index)
    mixed_payload["manifest"].update({
        "protected_broadcast_catalog_version": "test",
        "protected_broadcast_actions": ["android.intent.action.BOOT_COMPLETED"],
    })
    candidate = execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", mixed_payload)["candidates"][0]
    assert candidate["receiver_binding"]["actions"] == [
        "android.intent.action.BOOT_COMPLETED", "com.example.CUSTOM",
    ]
    assert candidate["receiver_binding"]["protected_actions_only"] is False


def test_dynamic_receiver_collective_scope_binds_cross_file_helper(tmp_path: Path) -> None:
    sources = {
        "com/example/Registrar.java": """package com.example;
class Registrar {
 void register(Context context) {
  DemoReceiver receiver = new DemoReceiver();
  IntentFilter filter = new IntentFilter("com.example.SYNC");
  context.registerReceiver(receiver, filter, 2);
 }
}
""",
        "com/example/DemoReceiver.java": """package com.example;
class DemoReceiver {
 void onReceive(Context context, Intent intent) {
  Helper helper = new Helper();
  if ("com.example.SYNC".equals(intent.getAction())) { helper.apply(); }
 }
}
""",
        "com/example/Helper.java": """package com.example;
class Helper {
 SportManager manager;
 void apply() { manager.startSport(); }
}
""",
    }
    _, _, index = _build_analyzer(
        tmp_path, sources, "com.example.Registrar", {"register"}
    )
    candidate = execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", _payload(index))["candidates"][0]
    binding = candidate["receiver_binding"]
    assert binding["receiver_class"] == "DemoReceiver"
    assert binding["receiver_qualified_class"] == "com.example.DemoReceiver"
    assert binding["effect_binding_proven"] is True
    assert candidate["dataflow_status"] == "interprocedural"
    assert candidate["impact_status"] == "statically_confirmed"


def test_dynamic_receiver_local_detection_is_per_call(tmp_path: Path) -> None:
    source = """package com.example;
class MixedReceiver {
 SportManager manager;
 void register(Context context) {
  MixedReceiver receiver = new MixedReceiver();
  IntentFilter localFilter = new IntentFilter("com.example.LOCAL");
  IntentFilter platformFilter = new IntentFilter("com.example.PLATFORM");
  LocalBroadcastManager.getInstance(context).registerReceiver(receiver, localFilter);
  context.registerReceiver(receiver, platformFilter, 2);
 }
 void onReceive(Context context, Intent intent) { manager.startSport(); }
}
"""
    _, _, index = _build_analyzer(
        tmp_path,
        {"com/example/MixedReceiver.java": source},
        "com.example.MixedReceiver",
        {"register", "onReceive"},
    )
    candidates = execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", _payload(index))["candidates"]
    assert len(candidates) == 1
    registration = candidates[0]["receiver_binding"]["registration"]
    assert registration["local_broadcast"] is False
    assert registration["api_family"] == "platform_context"
    assert registration["line"] == 9


def test_started_service_inline_extra_condition_binds_sensor_effect(tmp_path: Path) -> None:
    source = '''package com.example;
class SportService {
 SensorClient sensor;
 int onStartCommand(Intent intent, int flags, int startId) {
  if (2 == intent.getIntExtra("EXTRA_KEY", 0)) { startSensors(); }
  return 2;
 }
 void startSensors() { sensor.startAccSensor(3); }
}
'''
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/SportService.java": source},
        "com.example.SportService",
        {"onStartCommand"},
    )
    state = analyzer.started_service_state_machine()
    assert state["status"] == "verified"
    assert state["transitions"][0]["event"] == "2"
    assert state["transitions"][0]["effect_taxonomy"] == "location_sensor_collection"


def test_obfuscated_fragment_factory_wrapper_remains_type_aware_sink(tmp_path: Path) -> None:
    source = '''package com.example;
class CommonBaseActivity {
 androidx.fragment.app.FragmentFactory factory;
 void onCreate(Intent intent) {
  String className = intent.getStringExtra("className");
  getFragmentByFragmentFactory(className);
 }
 Object getFragmentByFragmentFactory(String className) {
  return factory.mo6498a(getClassLoader(), className);
 }
}
'''
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/CommonBaseActivity.java": source},
        "com.example.CommonBaseActivity",
        {"onCreate"},
    )
    flow = analyzer.analyze_entry({"onCreate"})
    assert flow["sink"]["kind"] == "fragment_reflection"
    assert flow["sink"]["effect_verified"] is True
    assert flow["dataflow_status"] == "interprocedural"


def test_fragment_factory_shape_without_owner_is_unverified_candidate(tmp_path: Path) -> None:
    source = '''package com.example;
class CommonBaseActivity {
 Object factory;
 void onCreate(Intent intent) {
  String className = intent.getStringExtra("className");
  getFragmentByFragmentFactory(className);
 }
 Object getFragmentByFragmentFactory(String className) {
  return factory.mo6498a(getClassLoader(), className);
 }
}
'''
    analyzer, _, _ = _build_analyzer(
        tmp_path,
        {"com/example/CommonBaseActivity.java": source},
        "com.example.CommonBaseActivity",
        {"onCreate"},
    )
    flow = analyzer.analyze_entry({"onCreate"})
    assert flow["sink"]["kind"] == "fragment_reflection"
    assert flow["sink"]["effect_verified"] is False
    assert any(
        gap["code"] == "FRAGMENT_FACTORY_PROVENANCE_GAP" and gap["critical"]
        for gap in flow["blocking_gaps"]
    )
