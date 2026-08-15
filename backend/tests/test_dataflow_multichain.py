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


def test_control_fact_does_not_escape_branch_block(tmp_path: Path) -> None:
    """P0-1：分支条件可控不得蔓延为"整段代码可控"。

    基线 run 20260809T110600Z 中 98.6% 的候选（138/140）源于 control_fact 置位后永不重置：
    入口方法里任意一个"攻击者可控 if"之后，整个调用图内所有 effect 都被挂链——即便 sink
    位于分支块之外、参数全是常量。此处 `web.loadUrl("https://fixed.example.com")` 在块外且
    实参为字面量，改造前会产出 1 条 control_to_sink，改造后应为 0。
    """

    source = """package com.example;
class RouterActivity {
 WebView web;
 void onCreate(Intent intent) {
  String evil = intent.getStringExtra("evil");
  if (evil != null) {
   log(evil);
  }
  web.loadUrl("https://fixed.example.com");
 }
}
"""
    flow = _analyzer(
        tmp_path, {"com/example/RouterActivity.java": source}, {"onCreate"}
    ).analyze_entry({"onCreate"})

    escaped = [
        chain for chain in flow["chains"]
        if chain.get("flow_kind") == "control_to_sink"
    ]
    assert not escaped, (
        "块外的常量 sink 不得因块内条件可控而成链；"
        f"实际逃逸 {len(escaped)} 条：{[c.get('sink', {}).get('text') for c in escaped]}"
    )


def test_control_fact_still_covers_sink_inside_branch_block(tmp_path: Path) -> None:
    """P0-1 召回边界：块内 sink 必须继续成链——降级只针对块外，不得误伤真实攻击面。

    与 test_control_fact_does_not_escape_branch_block 构成对照：同一段代码，
    仅把 sink 从块外移入块内，结论必须相反。
    """

    source = """package com.example;
class RouterActivity {
 WebView web;
 void onCreate(Intent intent) {
  String evil = intent.getStringExtra("evil");
  if (evil != null) {
   web.loadUrl("https://fixed.example.com");
  }
 }
}
"""
    flow = _analyzer(
        tmp_path, {"com/example/RouterActivity.java": source}, {"onCreate"}
    ).analyze_entry({"onCreate"})

    covered = [
        chain for chain in flow["chains"]
        if chain.get("flow_kind") == "control_to_sink"
    ]
    assert covered, "分支块内的敏感操作仍受攻击者控制的条件支配，必须继续成链"


def test_branch_block_end_line_covers_control_structures() -> None:
    """P0-1 作用域边界推断：覆盖花括号块、单语句体、if-else 链、循环、switch、嵌套。

    else / else if 与 if 共享同一支配条件（条件为假时执行），因此整条链合并为一个作用域，
    避免 else 内的 sink 逃逸判定。
    """

    from app.analysis.indexer import _build_flow_ir, _method_texts

    def branch_hints(snippet: str) -> list[dict]:
        raw, masked = _method_texts(snippet)
        return [
            item for item in _build_flow_ir(raw, masked, 1, [])
            if item["op"] == "branch_hint"
        ]

    # 花括号块：块末 = 第 4 行的 "}"，块外 b() 在第 5 行
    hints = branch_hints("void f() {\n if (evil) {\n  a();\n }\n b();\n}")
    assert [h["block_end_line"] for h in hints] == [4]

    # 单语句体：以分号结尾
    hints = branch_hints("void f() {\n if (evil) a();\n b();\n}")
    assert [h["block_end_line"] for h in hints] == [2]

    # if-else：作用域覆盖到 else 块末
    hints = branch_hints("void f() {\n if (evil) {\n  a();\n } else {\n  c();\n }\n b();\n}")
    assert [h["block_end_line"] for h in hints] == [6]

    # else-if 链：两个 branch_hint 都延伸到链末
    hints = branch_hints(
        "void f() {\n if (evil) {\n  a();\n } else if (x) {\n  c();\n } else {\n  d();\n }\n b();\n}"
    )
    assert [h["block_end_line"] for h in hints] == [8, 8]

    # 循环与 switch 同样是独立作用域
    assert [h["block_end_line"] for h in
            branch_hints("void f() {\n while (evil) {\n  a();\n }\n b();\n}")] == [4]
    assert [h["block_end_line"] for h in
            branch_hints("void f() {\n switch (evil) {\n  case 1: a(); break;\n }\n b();\n}")] == [4]

    # 嵌套：内层块末早于外层
    hints = branch_hints(
        "void f() {\n if (evil) {\n  if (inner) {\n   a();\n  }\n  c();\n }\n b();\n}"
    )
    assert [h["block_end_line"] for h in hints] == [7, 5]


def test_control_scope_unresolved_gap_when_block_end_unknown(tmp_path: Path) -> None:
    """作用域无法推断时退回旧行为（持续到方法末尾），但必须显式标注 CONTROL_SCOPE_UNRESOLVED。

    "未知"不得被当作"无限制"——缺失边界的链需带 critical gap，使其无法被判为高可信。
    这里复用真实索引产物再抹掉 block_end_line，模拟旧索引 / 括号未闭合场景：
    链应当仍然产出（保守），且必须携带该 gap。
    """

    source = """package com.example;
class RouterActivity {
 WebView web;
 void onCreate(Intent intent) {
  String evil = intent.getStringExtra("evil");
  if (evil != null) {
   log(evil);
  }
  web.loadUrl("https://fixed.example.com");
 }
}
"""
    analyzer = _analyzer(
        tmp_path, {"com/example/RouterActivity.java": source}, {"onCreate"}
    )
    stripped = 0
    for file in analyzer.files:
        for method in file.get("methods", []):
            for item in method.get("flow_ir", []):
                if item.get("op") == "branch_hint" and "block_end_line" in item:
                    del item["block_end_line"]
                    stripped += 1
    assert stripped, "测试前提：索引应已产出 block_end_line 供抹除"

    flow = analyzer.analyze_entry({"onCreate"})

    assert flow["chains"], "边界未知时应退回旧行为继续挂链（保守），而不是静默丢弃"
    emitted = {
        gap.get("code")
        for chain in flow["chains"]
        for gap in chain.get("blocking_gaps", [])
    }
    emitted |= {gap.get("code") for gap in flow.get("coverage_gaps", [])}
    assert "CONTROL_SCOPE_UNRESOLVED" in emitted, (
        f"block_end_line 缺失时必须产出 CONTROL_SCOPE_UNRESOLVED，实际 gap：{emitted}"
    )


def test_route_injection_detects_cross_method_plugin_routing(tmp_path: Path) -> None:
    """P2-6：复刻 v04 真机验证成立的插件路由注入形态。

    该漏洞此前完全漏检——ACTIVITY_INTENT_TO_SENSITIVE_SINK 只追"值流到达已知敏感 API"，
    应用自定义路由 wrapper 不在 effect 表内；且 classify_operation_taxonomy 对
    resolved_target 非空的调用直接返回 is_effect=False，插件 Activity 不在 manifest
    索引中、resolve 失败即丢弃。

    真实形态是跨方法的：onCreate 读 extra_splashinfo → handleSplashInfo 组装 Intent
    并 putExtras 全量透传 → startActivity。
    """

    source = """package com.example;
class RouterActivity extends Activity {
 void onCreate(Bundle b) {
  String info = getIntent().getStringExtra("extra_splashinfo");
  handleSplashInfo(info);
 }
 private void handleSplashInfo(String str) {
  Bundle bundle = new Bundle();
  Intent intent = new Intent();
  intent.putExtras(bundle);
  intent.setAction("com.example.ACTION_ROOT");
  startActivity(intent);
 }
}
"""
    candidates = execute(
        "ACTIVITY_EXTERNAL_ROUTE_INJECTION", _activity_payload(tmp_path, source)
    )["candidates"]

    assert len(candidates) == 1, "跨方法路由注入必须被检出（v04 实证成立的真实攻击面）"
    candidate = candidates[0]
    assert candidate["route_injection_kind"] == "bulk_extras_forwarding"
    assert candidate["flow_kind"] == "external_route_control"
    assert candidate["entry_method_name"] == "onCreate"
    gap_codes = {gap["code"] for gap in candidate["blocking_gaps"]}
    assert "ROUTE_TARGET_RESOLUTION_UNVERIFIED" in gap_codes, (
        "路由目标由运行期值决定，必须如实产 gap 而非静默丢弃候选"
    )
    assert "BULK_EXTRAS_FORWARDING" in gap_codes


def test_route_injection_ignores_launch_without_external_input(tmp_path: Path) -> None:
    """无外部输入的固定跳转不得成候选——避免制造新噪声。"""

    source = """package com.example;
class RouterActivity extends Activity {
 void onCreate(Bundle b) {
  Intent intent = new Intent();
  intent.setClassName("com.example", "com.example.Other");
  startActivity(intent);
 }
}
"""
    candidates = execute(
        "ACTIVITY_EXTERNAL_ROUTE_INJECTION", _activity_payload(tmp_path, source)
    )["candidates"]
    assert candidates == []


def test_route_injection_ignores_input_without_route_decision(tmp_path: Path) -> None:
    """读了外部输入但既不决定目标也不透传 extras → 非路由注入面。"""

    source = """package com.example;
class RouterActivity extends Activity {
 WebView web;
 void onCreate(Bundle b) {
  String value = getIntent().getStringExtra("url");
  web.loadUrl(value);
 }
}
"""
    candidates = execute(
        "ACTIVITY_EXTERNAL_ROUTE_INJECTION", _activity_payload(tmp_path, source)
    )["candidates"]
    assert candidates == []


def test_route_injection_requires_exported_component(tmp_path: Path) -> None:
    """非导出组件无外部攻击面，不出候选。"""

    source = """package com.example;
class RouterActivity extends Activity {
 void onCreate(Bundle b) {
  String pid = getIntent().getStringExtra("pid");
  Intent intent = new Intent();
  intent.putExtras(new Bundle());
  startActivity(intent);
 }
}
"""
    payload = _activity_payload(tmp_path, source)
    payload["manifest"]["components"][0]["exported"] = "false"
    assert execute("ACTIVITY_EXTERNAL_ROUTE_INJECTION", payload)["candidates"] == []


def test_route_injection_detects_dynamic_key_bundle_assembly(tmp_path: Path) -> None:
    """v04 真实形态：以攻击者 JSON 的键名逐条 putString，而非 putExtras 整体搬运。

    最初只匹配 putExtras/replaceExtras，在真实 APK 上漏掉了 v04 漏洞——
    MainActivity.handleSplashInfo 用 `for (key : json.keys()) bundle.putString(key, ...)`
    组装 Bundle，键名同样完全由攻击者决定。非字面量键名必须视为全量注入。
    """

    source = """package com.example;
class RouterActivity extends Activity {
 void onCreate(Bundle b) {
  String info = getIntent().getStringExtra("extra_splashinfo");
  handleSplashInfo(info);
 }
 private void handleSplashInfo(String str) {
  JSONObject json = new JSONObject(str);
  Bundle bundle = new Bundle();
  Iterator<String> keys = json.keys();
  while (keys.hasNext()) {
   String next = keys.next();
   bundle.putString(next, String.valueOf(json.get(next)));
  }
  Fasade.startNewPluginActivity(this, json.getString("pid"), bundle);
 }
}
"""
    candidates = execute(
        "ACTIVITY_EXTERNAL_ROUTE_INJECTION", _activity_payload(tmp_path, source)
    )["candidates"]

    assert len(candidates) == 1, "动态键名组装 + 自定义路由 wrapper 必须被检出"
    candidate = candidates[0]
    assert candidate["route_injection_kind"] == "bulk_extras_forwarding"
    assert "startNewPluginActivity" in candidate["sinks"][0]["text"], (
        "应用自定义路由 wrapper 必须能作为 sink——只匹配平台 API 会漏掉 v04 这类真实漏洞"
    )


def test_route_injection_ignores_constant_key_extras(tmp_path: Path) -> None:
    """常量键名的 putExtra 不构成"全量注入"——键集合固定，攻击者无法任意扩展。"""

    source = """package com.example;
class RouterActivity extends Activity {
 void onCreate(Bundle b) {
  String value = getIntent().getStringExtra("id");
  Intent intent = new Intent();
  intent.putExtra("fixed_key", value);
  startActivity(intent);
 }
}
"""
    candidates = execute(
        "ACTIVITY_EXTERNAL_ROUTE_INJECTION", _activity_payload(tmp_path, source)
    )["candidates"]
    assert candidates == [], "键名为字面量时不应判为 bulk_extras_forwarding"


def test_route_injection_target_fixed_class_literal(tmp_path: Path) -> None:
    """setClass(this, Foo.class)：类字面量目标 → resolved_target_fixed=True。

    P1-5 打通（2026-08-15）：fixed_local_target 反证依赖候选事实
    resolved_target_fixed。v04 §1.6 实证的 WbShareResultActivity 正是此形态
    （setClass(this, WbShareTransActivity.class)），目标固定本包、非任意启动。
    """

    source = """package com.example;
class RouterActivity extends Activity {
 void onCreate(Bundle b) {
  String action = getIntent().getAction();
  Intent intent = new Intent(action);
  if (Constants.ACTIVITY_REQ_SDK.equals(action)) {
   intent.setClass(this, WbShareTransActivity.class);
  } else {
   intent.setClass(this, WbShareToStoryActivity.class);
  }
  startActivity(intent);
 }
}
class WbShareTransActivity extends Activity {}
class WbShareToStoryActivity extends Activity {}
"""
    candidates = execute(
        "ACTIVITY_EXTERNAL_ROUTE_INJECTION", _activity_payload(tmp_path, source)
    )["candidates"]
    assert len(candidates) == 1, "外部 action 参与目标决策必须被检出"
    candidate = candidates[0]
    assert candidate["route_injection_kind"] == "target_selection"
    assert candidate.get("resolved_target_fixed") is True, (
        "类字面量目标（Foo.class）必须判为固定——v04 §1.6 实证形态"
    )


def test_route_injection_target_not_fixed_variable(tmp_path: Path) -> None:
    """setClassName(this, str)：变量目标 → resolved_target_fixed=False。"""

    source = """package com.example;
class RouterActivity extends Activity {
 void onCreate(Bundle b) {
  String target = getIntent().getStringExtra("target");
  Intent intent = new Intent();
  intent.setClassName(this, target);
  startActivity(intent);
 }
}
"""
    candidates = execute(
        "ACTIVITY_EXTERNAL_ROUTE_INJECTION", _activity_payload(tmp_path, source)
    )["candidates"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["route_injection_kind"] == "target_selection"
    assert candidate.get("resolved_target_fixed") is False, (
        "变量目标不得判为固定——误判 fixed 会被采信为 ai_false_positive（假阴性）"
    )


def test_route_injection_bulk_has_no_target_field(tmp_path: Path) -> None:
    """bulk_extras_forwarding 无目标决策 → 不输出 resolved_target_fixed。"""

    source = """package com.example;
class RouterActivity extends Activity {
 void onCreate(Bundle b) {
  String info = getIntent().getStringExtra("extra_splashinfo");
  Bundle bundle = new Bundle();
  bundle.putString("pid", info);
  Intent intent = new Intent();
  intent.putExtras(bundle);
  startActivity(intent);
 }
}
"""
    candidates = execute(
        "ACTIVITY_EXTERNAL_ROUTE_INJECTION", _activity_payload(tmp_path, source)
    )["candidates"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["route_injection_kind"] == "bulk_extras_forwarding"
    assert "resolved_target_fixed" not in candidate, (
        "无目标决策时不得输出该字段——避免把'无目标决策'误读为'目标不固定'"
    )


def test_route_injection_target_not_fixed_external_input_with_literal_key(tmp_path: Path) -> None:
    """混合形态负例①：setAction(intent.getStringExtra("action_name"))。

    2026-08-15 修订前用 search 匹配参数区任意字符串字面量，"action_name" 只是
    getStringExtra 的 key、不是目标值，却被误判 fixed=True → AI 可凭 fixed_local_target
    反证 → ai_false_positive → 真漏洞压成漏报（方向更坏）。
    """

    source = """package com.example;
class RouterActivity extends Activity {
 void onCreate(Bundle b) {
  Intent intent = new Intent();
  intent.setAction(intent.getStringExtra("action_name"));
  startActivity(intent);
 }
}
"""
    candidates = execute(
        "ACTIVITY_EXTERNAL_ROUTE_INJECTION", _activity_payload(tmp_path, source)
    )["candidates"]
    assert len(candidates) == 1, "外部输入参与 action 决策必须被检出"
    candidate = candidates[0]
    assert candidate["route_injection_kind"] == "target_selection"
    assert candidate.get("resolved_target_fixed") is False, (
        "getStringExtra 的 key 不是目标值：action 完全由外部输入决定，不得判为固定"
    )


def test_route_injection_target_not_fixed_external_cls_with_literal_in_call(tmp_path: Path) -> None:
    """混合形态负例②：setClassName(this, getIntent().getStringExtra("cls"))。

    类名来自外部输入，"cls" 只是 extra key。修订前误判 fixed=True。
    """

    source = """package com.example;
class RouterActivity extends Activity {
 void onCreate(Bundle b) {
  Intent intent = new Intent();
  intent.setClassName(this, getIntent().getStringExtra("cls"));
  startActivity(intent);
 }
}
"""
    candidates = execute(
        "ACTIVITY_EXTERNAL_ROUTE_INJECTION", _activity_payload(tmp_path, source)
    )["candidates"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["route_injection_kind"] == "target_selection"
    assert candidate.get("resolved_target_fixed") is False, (
        "类名来自外部输入，任一参数外部可控即不得判为固定"
    )


def test_route_injection_target_not_fixed_external_pkg_fixed_cls(tmp_path: Path) -> None:
    """混合形态负例③：setClassName(getIntent().getStringExtra("pkg"), "com.example.Target")。

    类名虽为字面量，但包名来自外部输入——setClassName(pkg, cls) 中两者共同决定目标
    组件，pkg 可控即目标不固定。修订前因 args 中存在任意字符串字面量误判 True。
    """

    source = """package com.example;
class RouterActivity extends Activity {
 void onCreate(Bundle b) {
  Intent intent = new Intent();
  intent.setClassName(getIntent().getStringExtra("pkg"), "com.example.Target");
  startActivity(intent);
 }
}
"""
    candidates = execute(
        "ACTIVITY_EXTERNAL_ROUTE_INJECTION", _activity_payload(tmp_path, source)
    )["candidates"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["route_injection_kind"] == "target_selection"
    assert candidate.get("resolved_target_fixed") is False, (
        "包名外部可控时目标不固定——setClassName 的两个分量必须都是字面量"
    )


def test_route_injection_target_not_fixed_concatenated_action(tmp_path: Path) -> None:
    """混合形态负例④：setAction("prefix_" + evil)——拼接表达式。

    字符串前缀 + 外部变量的拼接结果是外部可控的，不得判为固定。
    """

    source = """package com.example;
class RouterActivity extends Activity {
 void onCreate(Bundle b) {
  String evil = getIntent().getStringExtra("action");
  Intent intent = new Intent();
  intent.setAction("com.example.prefix_" + evil);
  startActivity(intent);
 }
}
"""
    candidates = execute(
        "ACTIVITY_EXTERNAL_ROUTE_INJECTION", _activity_payload(tmp_path, source)
    )["candidates"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["route_injection_kind"] == "target_selection"
    assert candidate.get("resolved_target_fixed") is False, (
        "拼接表达式结果外部可控，不得判为固定"
    )


def test_resolved_target_fixed_reaches_ai_slice_and_decision(tmp_path: Path) -> None:
    """端到端流转：规则字段 → 切片摘要与 deterministic_facts → decision 采信。

    2026-08-15 修订前该字段只在决策层可读（candidate 顶层），AI 输入切片
    （_candidate_summary 白名单 + deterministic_facts）均无——3.0.7 提示词要求
    refutation_basis 每一项必须在 candidate.deterministic_facts 中找到对应事实，
    AI 看不到字段就无从输出 basis，交叉验证永不触发（safe 但无效）。
    本用例验证：切片双通道下发 + 决策层 _refutation_basis_confirmed 采信。
    """

    from app.analysis.context_builder import ContextBuilder
    from app.findings.decision import decide_candidate
    from test_context_builder import build_index, candidate as _ctx_candidate

    builder = ContextBuilder(build_index(tmp_path))
    payload = _ctx_candidate()
    payload.update({
        "rule_id": "ACTIVITY_EXTERNAL_ROUTE_INJECTION",
        "flow_kind": "external_route_control",
        "route_injection_kind": "target_selection",
        "resolved_target_fixed": True,
        "sinks": [{
            "path": "com/example/ExportedActivity.java", "line": 12,
            "effect_verified": True, "resolve_status": "resolved",
        }],
        "blocking_gaps": [{"code": "ROUTE_TARGET_RESOLUTION_UNVERIFIED", "critical": True}],
    })
    document = builder.build_initial(payload)
    summary = document["candidate"]

    # ① 顶层摘要必须携带该字段（_candidate_summary 白名单）
    assert summary.get("resolved_target_fixed") is True, "切片候选摘要必须下发 resolved_target_fixed"
    # ② deterministic_facts 也必须携带（3.0.7 提示词要求 basis 事实在此可查）
    assert summary["deterministic_facts"].get("resolved_target_fixed") is True, (
        "deterministic_facts 必须包含 resolved_target_fixed，否则 AI 无从输出 fixed_local_target"
    )

    # ③ 生产决策路径采信：DecisionEngine.decide（orchestrator 实际入口）读 AI 的
    # refutation_basis，经 _cross_validated_refutation_basis 与 deterministic_facts
    # 交叉验证后采信为 ai_false_positive。修订前该机制只接在 decide_candidate
    # （测试入口），生产路径从不调用，且 ROUTE_TARGET_RESOLUTION_UNVERIFIED 不在
    # 证据不足白名单——AI 否定在生产上被双重拦截（safe 但无效）。
    from app.findings.decision import DecisionEngine

    decision_input = dict(summary)
    decision_input.update({
        "review_status": "pending_ai",
        "analysis_status": "ai_completed",
        "evidence_level": "L2",
        "verified_evidence_refs": [{"context_id": "ctx-1", "line": 12, "claim": "verified"}],
        "invalid_evidence_refs": [],
        "locations": [{"artifact": "code", "path": "com/example/ExportedActivity.java", "line": 12}],
        "ai_analysis": {
            "verdict": "refutes_candidate",
            "refutation_basis": ["fixed_local_target"],
            "verified_evidence_refs": [{"context_id": "ctx-1", "line": 12, "claim": "verified"}],
            "evidence_refs_valid": True,
            "semantic_evidence_complete": True,
            "flaw_holds": False,
            "exploitability": {"entry_reachable": False, "exfiltration_channel": "absent"},
            "harm": {"impact_type": "none"},
            "reachability_class": "remote",
            "impact_vector": {"confidentiality": "none", "integrity": "none", "availability": "none"},
            "summary": "目标固定本包，非任意启动",
            "confidence_tier": "high",
            "guard_status": "unknown",
            "analysis_complete": True,
        },
    })
    decision = DecisionEngine().decide(decision_input)
    assert decision.get("evidence_decision") == "ai_false_positive", (
        f"resolved_target_fixed=True 时 fixed_local_target 应被生产路径采信为 "
        f"ai_false_positive，实际 {decision.get('evidence_decision')}"
    )
