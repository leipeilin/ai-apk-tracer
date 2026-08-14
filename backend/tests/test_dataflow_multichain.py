from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.analysis.indexer import build_code_index

RULES_ROOT = Path(__file__).resolve().parents[2] / "rules"
if str(RULES_ROOT) not in sys.path:
    sys.path.insert(0, str(RULES_ROOT))

from shared.dataflow import DataFlowAnalyzer, classify_operation_taxonomy  # noqa: E402
from shared.detector import execute  # noqa: E402
from shared.index_reader import RuleIndexReader  # noqa: E402


def _analyzer(
    tmp_path: Path,
    sources: dict[str, str],
    entries: set[str],
    **budgets: int,
) -> DataFlowAnalyzer:
    source_root = tmp_path / "sources"
    for relative, content in sources.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    index_root = tmp_path / "index"
    descriptor = build_code_index(source_root, index_root / "code-index.json")
    reader = RuleIndexReader({
        **descriptor,
        "allowed_index_root": index_root.resolve().as_posix(),
    })
    try:
        scope = reader.component_flow_scope("com.example.RouterActivity", entries)
    finally:
        reader.close()
    return DataFlowAnalyzer(
        scope["files"], scope["entry_method_ids"], scope["gaps"], **budgets
    )


def _activity_payload(tmp_path: Path, source: str) -> dict:
    source_root = tmp_path / "sources"
    path = source_root / "com/example/RouterActivity.java"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, "utf-8")
    index_root = tmp_path / "index"
    descriptor = build_code_index(source_root, index_root / "code-index.json")
    return {
        "manifest": {
            "analysis_platform_api": 36,
            "components": [{
                "kind": "activity",
                "name": "com.example.RouterActivity",
                "exported": "true",
                "permission": None,
                "permission_protection": None,
                "intent_filters": [],
            }],
            "custom_permissions": {},
            "authority_conflicts": {},
        },
        "index": {**descriptor, "allowed_index_root": index_root.resolve().as_posix()},
    }


def test_collects_multiple_entries_and_sinks_with_compatibility_first_chain(tmp_path: Path) -> None:
    source = """package com.example;
class RouterActivity {
 WebView web;
 void onNewIntent(Intent first) {
  String a = first.getStringExtra("a");
  web.loadUrl(a);
  web.evaluateJavascript(a, null);
 }
 void onStartCommand(Intent second) {
  web.loadUrl(second.getStringExtra("b"));
 }
}
"""
    flow = _analyzer(
        tmp_path,
        {"com/example/RouterActivity.java": source},
        {"onNewIntent", "onStartCommand"},
    ).analyze_entry({"onNewIntent", "onStartCommand"})

    assert len(flow["chains"]) == 3
    assert flow["source"] == flow["chains"][0]["source"]
    assert flow["sink"] == flow["chains"][0]["sink"]
    assert flow["path"] == flow["chains"][0]["path"]
    assert {chain["entry_method_name"] for chain in flow["chains"]} == {
        "onNewIntent", "onStartCommand"
    }
    assert all(chain["path_model"] == "linear_ir_v1" for chain in flow["chains"])
    assert len({chain["chain_id"] for chain in flow["chains"]}) == 3


def test_detector_emits_one_candidate_per_chain_without_nested_chains(tmp_path: Path) -> None:
    source = """package com.example;
class RouterActivity {
 WebView web;
 void onNewIntent(Intent intent) {
  String value = intent.getStringExtra("url");
  web.loadUrl(value);
  web.evaluateJavascript(value, null);
 }
}
"""

    candidates = execute(
        "ACTIVITY_INTENT_TO_SENSITIVE_SINK", _activity_payload(tmp_path, source)
    )["candidates"]

    assert len(candidates) == 2
    assert len({candidate["chain_id"] for candidate in candidates}) == 2
    assert all("chains" not in candidate for candidate in candidates)
    assert all(len(candidate["sources"]) == len(candidate["sinks"]) == 1 for candidate in candidates)
    assert all(candidate["entry_method_id"] for candidate in candidates)
    assert all(candidate["path_model"] == "linear_ir_v1" for candidate in candidates)


def test_detector_guard_isolated_per_entry_chain(tmp_path: Path) -> None:
    source = """package com.example;
class RouterActivity {
 WebView web;
 void onCreate(Intent intent) {
  enforceCallingPermission("sig", "denied");
  web.loadUrl(intent.getStringExtra("guarded"));
 }
 void onNewIntent(Intent intent) {
  web.loadUrl(intent.getStringExtra("open"));
 }
}
"""

    candidates = execute(
        "ACTIVITY_INTENT_TO_SENSITIVE_SINK", _activity_payload(tmp_path, source)
    )["candidates"]

    assert len(candidates) == 1
    assert candidates[0]["entry_method_name"] == "onNewIntent"
    assert candidates[0]["guard_status"] == "absent"


def test_callee_multiple_sinks_do_not_hide_caller_following_sink(tmp_path: Path) -> None:
    source = """package com.example;
class RouterActivity {
 WebView web;
 void onNewIntent(Intent input) {
  String value = input.getStringExtra("url");
  publish(value);
  web.loadUrl(value);
 }
 void publish(String value) {
  web.loadUrl(value);
  web.evaluateJavascript(value, null);
 }
}
"""
    flow = _analyzer(
        tmp_path,
        {"com/example/RouterActivity.java": source},
        {"onNewIntent"},
    ).analyze_entry({"onNewIntent"})

    assert len(flow["chains"]) == 3
    assert [chain["sink"]["operation_name"] for chain in flow["chains"]].count("loadUrl") == 2
    assert any(chain["sink"]["operation_name"] == "evaluateJavascript" for chain in flow["chains"])
    assert any(chain["dataflow_status"] == "interprocedural" for chain in flow["chains"])


def test_structured_kotlin_source_uses_role_not_obfuscated_parameter_name(tmp_path: Path) -> None:
    source = """package com.example
class RouterActivity {
 lateinit var web: WebView
 fun onNewIntent(a: Intent) {
  web.loadUrl(a)
 }
}
"""
    flow = _analyzer(
        tmp_path,
        {"com/example/RouterActivity.kt": source},
        {"onNewIntent"},
    ).analyze_entry({"onNewIntent"})

    assert len(flow["chains"]) == 1
    assert flow["source"]["source_kind"] == "intent"
    assert flow["source"]["source_basis"].startswith("android-entrypoint-signature:onNewIntent")
    assert flow["source"]["text"] == "a"


def test_unknown_slot_read_preserves_structured_receiver_source(tmp_path: Path) -> None:
    source = """package com.example;
import android.content.Intent;
import android.webkit.WebView;
class RouterActivity {
 android.webkit.WebView web;
 void onNewIntent(Intent incoming) {
  web.loadUrl(incoming.getStringExtra("url"));
 }
}
"""
    flow = _analyzer(
        tmp_path,
        {"com/example/RouterActivity.java": source},
        {"onNewIntent"},
    ).analyze_entry({"onNewIntent"})

    assert flow["source"]["source_kind"] == "intent"
    assert flow["source"]["parameter_position"] == 0
    assert flow["source"]["parameter_type"] == "android.content.Intent"
    assert any(
        node.get("kind") == "source" and "getStringExtra" in str(node.get("text") or "")
        for node in flow["path"]
    )


def test_third_party_source_named_method_does_not_create_untrusted_source(tmp_path: Path) -> None:
    source = """package com.example;
class RouterActivity {
 WebView web;
 ThirdPartyConfig config;
 void onCreate() {
  web.loadUrl(config.getPath());
 }
}
class ThirdPartyConfig { String getPath() { return "safe"; } }
"""
    flow = _analyzer(
        tmp_path,
        {"com/example/RouterActivity.java": source},
        {"onCreate"},
    ).analyze_entry({"onCreate"})

    assert flow["chains"] == []


def test_chain_ids_are_stable_when_input_mapping_order_changes(tmp_path: Path) -> None:
    activity = """package com.example;
class RouterActivity {
 WebView web;
 void onNewIntent(Intent value) { helper(value.getStringExtra("url")); }
 void helper(String value) { Helper.publish(web, value); }
}
"""
    helper = """package com.example;
class Helper {
 static void publish(WebView web, String value) { web.loadUrl(value); }
}
"""
    entries = {"onNewIntent"}
    first = _analyzer(
        tmp_path / "first",
        {
            "com/example/RouterActivity.java": activity,
            "com/example/Helper.java": helper,
        },
        entries,
    ).analyze_entry(entries)
    second = _analyzer(
        tmp_path / "second",
        {
            "com/example/Helper.java": helper,
            "com/example/RouterActivity.java": activity,
        },
        entries,
    ).analyze_entry(entries)

    assert [chain["chain_id"] for chain in first["chains"]] == [
        chain["chain_id"] for chain in second["chains"]
    ]


def test_chain_budget_reports_critical_gap_instead_of_silent_truncation(tmp_path: Path) -> None:
    source = """package com.example;
class RouterActivity {
 WebView web;
 void onNewIntent(Intent input) {
  String value = input.getStringExtra("url");
  web.loadUrl(value);
  web.evaluateJavascript(value, null);
 }
}
"""
    flow = _analyzer(
        tmp_path,
        {"com/example/RouterActivity.java": source},
        {"onNewIntent"},
        max_chains=1,
    ).analyze_entry({"onNewIntent"})

    assert len(flow["chains"]) == 1
    assert flow["dataflow_status"] == "not_proven"
    assert any(gap["code"] == "DATAFLOW_CHAIN_BUDGET_EXCEEDED" for gap in flow["coverage_gaps"])
    assert any(gap["code"] == "DATAFLOW_CHAIN_BUDGET_EXCEEDED" for gap in flow["chains"][0]["blocking_gaps"])


def test_linear_branch_chain_is_collected_but_not_deterministically_verified(tmp_path: Path) -> None:
    source = """package com.example;
class RouterActivity {
 WebView web;
 void onNewIntent(Intent input) {
  String value = input.getStringExtra("url");
  if (enabled()) { web.loadUrl(value); }
 }
}
"""
    flow = _analyzer(
        tmp_path,
        {"com/example/RouterActivity.java": source},
        {"onNewIntent"},
    ).analyze_entry({"onNewIntent"})

    assert len(flow["chains"]) == 1
    assert flow["chains"][0]["dataflow_status"] == "not_proven"
    assert any(
        gap["code"] == "LINEAR_IR_PATH_SENSITIVITY_LIMITATION"
        for gap in flow["chains"][0]["blocking_gaps"]
    )


@pytest.mark.parametrize(
    ("receiver_type", "method_name"),
    [
        ("java.lang.Object", "notify"),
        ("com.example.Custom", "commit"),
        ("com.example.Custom", "emit"),
        ("com.example.Custom", "connect"),
        ("com.example.Custom", "delete"),
        ("com.example.Custom", "write"),
        ("com.example.Custom", "startSport"),
        ("com.example.Pojo", "setValue"),
    ],
)
def test_taxonomy_generic_method_names_are_not_sensitive(
    receiver_type: str, method_name: str
) -> None:
    operation = classify_operation_taxonomy({
        "method_name": method_name,
        "receiver_type": receiver_type,
        "receiver_text": "target",
        "method_descriptor": "(?)->?",
        "arguments": ["value"],
    })

    assert operation == {
        "is_effect": False,
        "taxonomy": "unknown_effect",
        "kind": "not_sensitive",
        "verified": False,
    }


@pytest.mark.parametrize(
    ("receiver_type", "method_name", "arity", "taxonomy", "kind"),
    [
        ("android.content.Context", "sendBroadcast", 1, "callback_event_injection", "broadcast"),
        ("android.location.LocationManager", "requestLocationUpdates", 4, "location_sensor_collection", "location"),
        ("android.hardware.SensorManager", "registerListener", 3, "location_sensor_collection", "sensor"),
        ("android.bluetooth.BluetoothGatt", "writeCharacteristic", 1, "device_protocol_output", "device_protocol_output"),
        ("android.database.sqlite.SQLiteDatabase", "insert", 3, "database_mutation", "database_mutation"),
        ("android.content.ContentResolver", "insert", 2, "database_mutation", "content_mutation"),
        ("java.io.FileOutputStream", "write", 1, "file_mutation", "file_mutation"),
        ("android.content.SharedPreferences.Editor", "putString", 2, "persistent_state_write", "persistent_state_write"),
        ("okhttp3.Call", "execute", 0, "data_disclosure", "data_disclosure"),
        ("android.webkit.WebView", "loadUrl", 1, "data_disclosure", "webview"),
    ],
)
def test_taxonomy_verifies_known_receiver_families_and_arities(
    receiver_type: str, method_name: str, arity: int, taxonomy: str, kind: str
) -> None:
    operation = classify_operation_taxonomy({
        "method_name": method_name,
        "receiver_type": receiver_type,
        "receiver_text": "target",
        "method_descriptor": f"({','.join('?' for _ in range(arity))})->?",
        "arguments": ["value"] * arity,
    })

    assert operation == {
        "is_effect": True,
        "taxonomy": taxonomy,
        "kind": kind,
        "verified": True,
    }


def test_taxonomy_parses_jvm_descriptor_and_rejects_wrong_arity() -> None:
    valid = classify_operation_taxonomy({
        "method_name": "loadUrl",
        "receiver_type": "android.webkit.WebView",
        "receiver_text": "target",
        "method_descriptor": "(Ljava/lang/String;)V",
    })
    invalid = classify_operation_taxonomy({
        "method_name": "loadUrl",
        "receiver_type": "android.webkit.WebView",
        "receiver_text": "target",
        "method_descriptor": "(Ljava/lang/String;Ljava/util/Map;I)V",
    })

    assert valid["verified"] is True
    assert valid["kind"] == "webview"
    assert invalid == {
        "is_effect": False,
        "taxonomy": "unknown_effect",
        "kind": "not_sensitive",
        "verified": False,
    }


def test_taxonomy_missing_descriptor_is_unverified_signature_candidate() -> None:
    operation = classify_operation_taxonomy({
        "method_name": "delete",
        "receiver_type": "java.io.File",
        "receiver_text": "target",
    })

    assert operation["is_effect"] is True
    assert operation["verified"] is False
    assert operation["kind"] == "file_delete"
    assert operation["gap"]["code"] == "OPERATION_SIGNATURE_GAP"
    assert operation["gap"]["critical"] is True


@pytest.mark.parametrize(
    "call",
    [
        {
            "method_name": "loadUrl",
            "receiver_type": "java.lang.Object",
            "receiver_text": "webview",
            "method_descriptor": "(?)->?",
        },
        {
            "method_name": "delete",
            "receiver_type": "com.example.DatabaseRepository",
            "receiver_text": "database",
            "method_descriptor": "()->?",
        },
        {
            "method_name": "delete",
            "receiver_type": "com.example.UserDao",
            "receiver_text": "dao",
            "method_descriptor": "(?)->?",
        },
    ],
)
def test_taxonomy_rejects_receiver_name_and_substring_spoofing(call: dict) -> None:
    assert classify_operation_taxonomy(call)["kind"] == "not_sensitive"


def test_taxonomy_rejects_package_qualified_leaf_spoofing() -> None:
    operation = classify_operation_taxonomy({
        "method_name": "loadUrl",
        "receiver_type": "com.attacker.WebView",
        "receiver_text": "web",
        "method_descriptor": "(?)->?",
        "arguments": ["value"],
    })

    assert operation["kind"] == "not_sensitive"
    assert operation["is_effect"] is False


def test_class_for_name_requires_class_owner_and_matching_signature() -> None:
    class_call = classify_operation_taxonomy({
        "method_name": "forName",
        "receiver_type": "java.lang.Class",
        "receiver_text": "Class",
        "method_descriptor": "(Ljava/lang/String;)Ljava/lang/Class;",
    })
    custom_call = classify_operation_taxonomy({
        "method_name": "forName",
        "receiver_type": "com.example.CustomLoader",
        "receiver_text": "loader",
        "method_descriptor": "(?)->?",
    })

    assert class_call["kind"] == "fragment_reflection"
    assert class_call["verified"] is True
    assert custom_call["kind"] == "not_sensitive"


def test_resolved_target_precedes_external_family_classification() -> None:
    operation = classify_operation_taxonomy({
        "method_name": "delete",
        "receiver_type": "android.database.sqlite.SQLiteDatabase",
        "receiver_text": "database",
        "method_descriptor": "(?,?,?)->?",
        "resolved_target_id": "com.example.DatabaseFacade#delete",
    })

    assert operation["kind"] == "resolved_wrapper"
    assert operation["is_effect"] is False


def test_resolved_database_facade_delete_does_not_close_chain(tmp_path: Path) -> None:
    source = '''package com.example;
class RouterActivity {
 DatabaseFacade database;
 void onNewIntent(Intent intent) {
  database.delete(intent.getStringExtra("value"));
 }
}
class DatabaseFacade {
 void delete(String value) {}
}
'''
    flow = _analyzer(
        tmp_path,
        {"com/example/RouterActivity.java": source},
        {"onNewIntent"},
    ).analyze_entry({"onNewIntent"})

    assert flow["chains"] == []


def test_resolved_webview_adapter_closes_only_at_real_webview_call(tmp_path: Path) -> None:
    source = '''package com.example;
import android.webkit.WebView;
class RouterActivity {
 WebViewAdapter webView;
 void onNewIntent(Intent intent) {
  webView.loadUrl(intent.getStringExtra("url"));
 }
}
class WebViewAdapter {
 WebView delegate;
 void loadUrl(String value) { delegate.loadUrl(value); }
}
'''
    flow = _analyzer(
        tmp_path,
        {"com/example/RouterActivity.java": source},
        {"onNewIntent"},
    ).analyze_entry({"onNewIntent"})

    assert len(flow["chains"]) == 1
    assert flow["chains"][0]["sink"]["receiver_type"] == "android.webkit.WebView"


def test_ambiguous_custom_load_url_reports_gap_without_sink(tmp_path: Path) -> None:
    source = '''package com.example;
class RouterActivity {
 CustomRenderer renderer;
 void onNewIntent(Intent intent) {
  renderer.loadUrl(intent.getStringExtra("url"));
 }
}
class CustomRenderer {
 void loadUrl(String value) {}
 void loadUrl(Object value) {}
}
'''
    flow = _analyzer(
        tmp_path,
        {"com/example/RouterActivity.java": source},
        {"onNewIntent"},
    ).analyze_entry({"onNewIntent"})

    assert flow["chains"] == []
    assert any(
        gap["code"] == "SYMBOL_TARGET_AMBIGUOUS" and gap["critical"]
        for gap in flow["coverage_gaps"]
    )


def test_getnameforuid_null_check_is_fail_closed_guard() -> None:
    """v2026-08-09（Cluster E 根因修复）：`String name = getNameForUid(uid);
    if (name == null) { return false; }` 是 Android 最常见的调用者包名校验
    之一，此前不在 GUARD_METHODS 导致 Binder/Provider 规则把存在精确包名
    校验的服务误报为 caller check missing。判空 fail-closed 必须识别为有效 Guard。"""

    from shared.dataflow import GUARD_METHODS

    assert "getNameForUid" in GUARD_METHODS

    analyzer = DataFlowAnalyzer([], [])
    method = {
        "id": "m1", "name": "check", "content": """boolean check(int uid) {
    String name = pm.getNameForUid(uid);
    if (name == null) { return false; }
    return "com.example.market".equals(name);
}""",
        "start_line": 1, "end_line": 5,
        "flow_ir": [
            {"op": "call", "method_name": "getNameForUid", "assigned_to": "name",
             "ordinal": 1, "start_line": 2},
            {"op": "branch_hint", "condition": "name == null", "fail_closed": True, "line": 3},
            {"op": "return", "line": 4},
        ],
        "call_sites": [
            {"method_name": "getNameForUid", "ordinal": 1, "start_line": 2, "assigned_to": "name"},
        ],
    }
    effective, _ = analyzer._check_guard_fail_closed(method, method["call_sites"][0], 10)
    assert effective is True


def test_receiver_protocol_gate_detects_binary_payload() -> None:
    """v2026-08-14（动态验证提炼）：getByteArrayExtra("mipush_payload") + 解析
    → binary_payload 协议门。推送类接收器（mipush）业务回调需服务端二进制载荷，
    普通应用无法构造合法消息——外部输入"到达 Sink"在业务语义上不成立。"""

    from shared.detector import _receiver_protocol_gate

    class _MockAnalyzer:
        def __init__(self, methods: dict) -> None:
            self.methods_by_id = methods

    analyzer = _MockAnalyzer({
        "m1": {"content": """
            byte[] payload = intent.getByteArrayExtra("mipush_payload");
            if (payload == null) { return; }
            C7971ji.m21500a(parser, payload);
            """, "path": "C7082t.java"},
        "m2": {"content": "void onReceive(...) {}", "path": "R.java"},
    })
    chain = {
        "source": {"method_id": "m2"},
        "sink": {"method_id": "m1"},
        "path": [],
    }
    gated, gates = _receiver_protocol_gate(analyzer, chain)
    assert gated is True
    assert "binary_payload" in gates


def test_receiver_protocol_gate_detects_serialized_command() -> None:
    """v2026-08-14：getSerializableExtra + instanceof → serialized_command 协议门。"""

    from shared.detector import _receiver_protocol_gate

    class _MockAnalyzer:
        def __init__(self, methods: dict) -> None:
            self.methods_by_id = methods

    analyzer = _MockAnalyzer({
        "m1": {"content": """
            Object cmd = intent.getSerializableExtra("key_command");
            if (cmd instanceof MiPushCommandMessage) { handle((MiPushCommandMessage) cmd); }
            """, "path": "MsgService.java"},
    })
    chain = {"source": {"method_id": "m1"}, "sink": {"method_id": "m1"}, "path": []}
    gated, gates = _receiver_protocol_gate(analyzer, chain)
    assert gated is True
    assert "serialized_command" in gates


def test_receiver_protocol_gate_direct_input_no_gate() -> None:
    """v2026-08-14（保守侧）：无协议门（直接 extra 读值 → startService）不误报。"""

    from shared.detector import _receiver_protocol_gate

    class _MockAnalyzer:
        def __init__(self, methods: dict) -> None:
            self.methods_by_id = methods

    analyzer = _MockAnalyzer({
        "m1": {"content": """
            String action = intent.getStringExtra("action");
            Intent i = new Intent(action);
            i.setComponent(new ComponentName(ctx, TargetService.class));
            context.startService(i);
            """, "path": "DirectReceiver.java"},
    })
    chain = {"source": {"method_id": "m1"}, "sink": {"method_id": "m1"}, "path": []}
    gated, gates = _receiver_protocol_gate(analyzer, chain)
    assert gated is False
    assert gates == []


def test_decision_whitelist_includes_input_protocol_uncontrolled() -> None:
    """v2026-08-14：INPUT_PROTOCOL_UNCONTROLLED 必须进证据不足白名单——
    协议门是静态可识别的"外部输入可控性未证明"（静态限制，非确定性冲突），
    降级不否决：severity → pending、AI 判定方向不被采信但候选保留。"""

    from app.findings.decision import _EVIDENCE_INSUFFICIENCY_GAPS

    assert "INPUT_PROTOCOL_UNCONTROLLED" in _EVIDENCE_INSUFFICIENCY_GAPS
