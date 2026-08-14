from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.analysis.indexer import build_code_index

RULES_ROOT = Path(__file__).resolve().parents[2] / "rules"
if str(RULES_ROOT) not in sys.path:
    sys.path.insert(0, str(RULES_ROOT))

from shared.dataflow import DataFlowAnalyzer  # noqa: E402
from shared.index_reader import RuleIndexReader  # noqa: E402


def _analyze(
    tmp_path: Path,
    sources: dict[str, str],
    component: str = "com.example.RouterActivity",
    entries: set[str] | None = None,
    **analyzer_options: int,
) -> dict:
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
        scope = reader.component_flow_scope(component, entries or {"onCreate"})
    finally:
        reader.close()
    analyzer = DataFlowAnalyzer(
        scope["files"],
        entry_method_ids=scope["entry_method_ids"],
        scope_gaps=scope["gaps"],
        **analyzer_options,
    )
    return analyzer.analyze_entry(entries or {"onCreate"})


def _activity(body: str, parameters: str = "Intent intent") -> str:
    return f'''package com.example;
class RouterActivity {{ WebView web;
 void onCreate({parameters}) {{
{body}
 }}
}}
'''


def test_raw_key_and_outer_assignment_round_trip(tmp_path: Path) -> None:
    flow = _analyze(tmp_path, {
        "com/example/RouterActivity.java": _activity(
            '  String url = intent.getStringExtra("url");\n  web.loadUrl(url);'
        ),
    })
    assert flow["sink"]["kind"] == "webview"
    assert flow["final_reaching_state"] == "untrusted"
    assert any(item["value"] == "intent" and item["state"] == "untrusted" for item in flow["reaching_definitions"])
    assert any(item["value"] == "url" and item["state"] == "maybe_untrusted" for item in flow["reaching_definitions"])


def test_last_local_definition_kills_tainted_value(tmp_path: Path) -> None:
    flow = _analyze(tmp_path, {
        "com/example/RouterActivity.java": _activity(
            '  String url = intent.getStringExtra("url");\n  url = "https://safe.example";\n  web.loadUrl(url);'
        ),
    })
    url_definitions = [item for item in flow["reaching_definitions"] if item["value"] == "url"]
    assert flow["sink"] is None
    assert url_definitions[-1]["state"] == "trusted"
    assert url_definitions[-1]["killed_version"] == url_definitions[-2]["version"]


def test_validation_applies_only_to_matching_value_version(tmp_path: Path) -> None:
    flow = _analyze(tmp_path, {
        "com/example/RouterActivity.java": _activity(
            "  String url = intent.getStringExtra(\"url\");\n"
            "  boolean accepted = isAllowedHttps(url);\n"
            "  if (!accepted) return;\n"
            "  web.loadUrl(url);"
        ),
    })
    source_definition = next(
        item for item in flow["reaching_definitions"]
        if item["value"] == "url" and item["state"] == "maybe_untrusted"
    )
    assert flow["sink"] is None
    assert flow["validation_transitions"] == [{
        "value": "url",
        "version": source_definition["version"],
        "from": "maybe_untrusted",
        "to": "validated",
        "path": "com/example/RouterActivity.java",
        "line": 6,
    }]


def test_assignment_after_validation_kills_validated_version(tmp_path: Path) -> None:
    flow = _analyze(tmp_path, {
        "com/example/RouterActivity.java": _activity(
            "  String url = intent.getStringExtra(\"url\");\n"
            "  if (!isAllowedHttps(url)) return;\n"
            "  url = intent.getStringExtra(\"override\");\n"
            "  web.loadUrl(url);"
        ),
    })
    url_definitions = [item for item in flow["reaching_definitions"] if item["value"] == "url"]
    assert flow["sink"] is not None
    assert flow["final_reaching_state"] == "untrusted"
    assert flow["validation_transitions"][0]["version"] == url_definitions[0]["version"]
    assert url_definitions[-1]["killed_version"] == url_definitions[0]["version"]


def test_put_extra_same_key_overwrites_validated_slot(tmp_path: Path) -> None:
    flow = _analyze(tmp_path, {
        "com/example/RouterActivity.java": _activity(
            "  String url = intent.getStringExtra(\"url\");\n"
            "  if (!isAllowedHttps(url)) return;\n"
            "  Intent routed = new Intent();\n"
            "  routed.putExtra(\"url\", url);\n"
            "  routed.putExtra(\"url\", intent.getStringExtra(\"override\"));\n"
            "  web.loadUrl(routed.getStringExtra(\"url\"));"
        ),
    })
    assert flow["sink"] is not None
    assert flow["final_reaching_state"] == "untrusted"
    assert [(item["code"], item["key"], item["operation"]) for item in flow["slot_overwrites"]] == [
        ("VALIDATED_SLOT_OVERWRITTEN", "url", "putExtra")
    ]


def test_put_extra_other_key_does_not_overwrite_validated_slot(tmp_path: Path) -> None:
    flow = _analyze(tmp_path, {
        "com/example/RouterActivity.java": _activity(
            "  String url = intent.getStringExtra(\"url\");\n"
            "  if (!isAllowedHttps(url)) return;\n"
            "  Intent routed = new Intent();\n"
            "  routed.putExtra(\"url\", url);\n"
            "  routed.putExtra(\"other\", intent.getStringExtra(\"override\"));\n"
            "  web.loadUrl(routed.getStringExtra(\"url\"));"
        ),
    })
    assert flow["sink"] is None
    assert flow["slot_overwrites"] == []


def test_put_extras_wildcard_overwrites_validated_slot(tmp_path: Path) -> None:
    flow = _analyze(tmp_path, {
        "com/example/RouterActivity.java": _activity(
            "  String url = intent.getStringExtra(\"url\");\n"
            "  if (!isAllowedHttps(url)) return;\n"
            "  Intent routed = new Intent();\n"
            "  routed.putExtra(\"url\", url);\n"
            "  routed.putExtras(extras);\n"
            "  web.loadUrl(routed.getStringExtra(\"url\"));",
            "Intent intent, Bundle extras",
        ),
    })
    assert flow["sink"] is not None
    assert flow["final_reaching_state"] == "maybe_untrusted"
    assert any(item["key"] == "url" and item["operation"] == "putExtras" for item in flow["slot_overwrites"])


def test_replace_extras_kills_existing_slots(tmp_path: Path) -> None:
    flow = _analyze(tmp_path, {
        "com/example/RouterActivity.java": _activity(
            "  String url = intent.getStringExtra(\"url\");\n"
            "  if (!isAllowedHttps(url)) return;\n"
            "  Intent routed = new Intent();\n"
            "  routed.putExtra(\"url\", url);\n"
            "  routed.replaceExtras(extras);\n"
            "  web.loadUrl(routed.getStringExtra(\"url\"));",
            "Intent intent, Bundle extras",
        ),
    })
    assert flow["sink"] is not None
    assert any(item["key"] == "url" and item["operation"] == "replaceExtras" for item in flow["slot_overwrites"])


def test_cross_method_return_is_assigned_to_caller(tmp_path: Path) -> None:
    source = '''package com.example;
class RouterActivity { WebView web;
 void onCreate(Intent intent) {
  String url = extract(intent);
  web.loadUrl(url);
 }
 String extract(Intent input) {
  return input.getStringExtra("url");
 }
}
'''
    flow = _analyze(tmp_path, {"com/example/RouterActivity.java": source})
    assert flow["sink"] is not None
    assert flow["dataflow_status"] == "interprocedural"
    assert any(item["value"] == "intent" and item["state"] == "untrusted" for item in flow["reaching_definitions"])
    assert any(item["value"] == "url" and item["state"] == "maybe_untrusted" for item in flow["reaching_definitions"])


def test_cross_file_callee_slot_mutation_is_shared_with_caller(tmp_path: Path) -> None:
    activity = '''package com.example;
class RouterActivity { WebView web;
 void onCreate(Intent intent, Bundle extras) {
  String url = intent.getStringExtra("url");
  if (!isAllowedHttps(url)) return;
  Intent routed = new Intent();
  routed.putExtra("url", url);
  Helper.overwrite(routed, extras);
  web.loadUrl(routed.getStringExtra("url"));
 }
}
'''
    helper = '''package com.example;
class Helper { WebView web;
 static void overwrite(Intent target, Bundle incoming) {
  target.putExtras(incoming);
 }
}
'''
    flow = _analyze(tmp_path, {
        "com/example/RouterActivity.java": activity,
        "com/example/Helper.java": helper,
    })
    assert flow["sink"] is not None
    assert flow["final_reaching_state"] == "maybe_untrusted"
    assert any("Helper.overwrite" in item["method_id"] for item in flow["slot_overwrites"])


def test_ambiguous_call_adds_critical_gap_and_does_not_close(tmp_path: Path) -> None:
    source = '''package com.example;
class RouterActivity { WebView web;
 void onCreate(Intent intent) {
  String routed = route(intent.getStringExtra("url"));
  web.loadUrl(routed);
 }
 String route(String value) { return value; }
 String route(Bundle value) { return "safe"; }
}
'''
    flow = _analyze(tmp_path, {"com/example/RouterActivity.java": source})
    assert flow["sink"] is not None
    assert flow["dataflow_status"] == "not_proven"
    assert any(gap["code"] == "SYMBOL_TARGET_AMBIGUOUS" and gap["critical"] for gap in flow["coverage_gaps"])


@pytest.mark.parametrize(
    ("mutation", "expect_sink", "expected_state"),
    [
        ("routed.putExtras(extras);", True, "maybe_untrusted"),
        ("", False, None),
        ('routed.putExtra("other", extras.getString("url"));', False, None),
    ],
    ids=["putExtras-overwrite", "no-overwrite", "other-key"],
)
def test_router_validation_then_slot_mutation(
    tmp_path: Path,
    mutation: str,
    expect_sink: bool,
    expected_state: str | None,
) -> None:
    flow = _analyze(tmp_path, {
        "com/example/RouterActivity.java": _activity(
            "  String url = intent.getStringExtra(\"url\");\n"
            "  if (!isAllowedHttps(url)) return;\n"
            "  Intent routed = new Intent();\n"
            "  routed.putExtra(\"url\", url);\n"
            f"  {mutation}\n"
            "  web.loadUrl(routed.getStringExtra(\"url\"));",
            "Intent intent, Bundle extras",
        ),
    })
    assert (flow["sink"] is not None) is expect_sink
    assert flow["final_reaching_state"] == expected_state
    assert flow["summary_fixpoint"]["status"] == "converged"
    assert flow["summary_fixpoint"]["iterations"] <= flow["summary_fixpoint"]["limit"]


def test_single_entry_enumerates_two_sinks(tmp_path: Path) -> None:
    flow = _analyze(tmp_path, {
        "com/example/RouterActivity.java": _activity(
            '  String url = intent.getStringExtra("url");\n'
            "  web.loadUrl(url);\n"
            "  web.evaluateJavascript(url, null);"
        ),
    })

    assert [chain["sink"]["method_name"] for chain in flow["chains"]] == [
        "loadUrl", "evaluateJavascript",
    ]
    assert len({chain["chain_id"] for chain in flow["chains"]}) == 2


def test_two_entries_are_enumerated_independently(tmp_path: Path) -> None:
    source = '''package com.example;
class RouterActivity { WebView web;
 void onCreate(Intent intent) {
  web.loadUrl(intent.getStringExtra("create"));
 }
 void onNewIntent(Intent intent) {
  web.loadUrl(intent.getStringExtra("new"));
 }
}
'''
    flow = _analyze(
        tmp_path,
        {"com/example/RouterActivity.java": source},
        entries={"onCreate", "onNewIntent"},
    )

    assert [chain["entry_method_name"] for chain in flow["chains"]] == [
        "onCreate", "onNewIntent",
    ]
    assert len({chain["chain_id"] for chain in flow["chains"]}) == 2


@pytest.mark.parametrize(
    "sink_expression",
    [
        "web.evaluateJavascript(left, right);",
        "web.loadUrl(left + right);",
        "web.loadUrl(left.concat(right));",
    ],
    ids=["independent-sink-arguments", "merged-expression", "merged-transform"],
)
def test_one_sink_keeps_two_independent_source_lineages(
    tmp_path: Path,
    sink_expression: str,
) -> None:
    flow = _analyze(tmp_path, {
        "com/example/RouterActivity.java": _activity(
            '  String left = intent.getStringExtra("left");\n'
            '  String right = intent.getStringExtra("right");\n'
            f"  {sink_expression}"
        ),
    })

    assert len(flow["chains"]) == 2
    assert {chain["source"]["parameter_position"] for chain in flow["chains"]} == {0}
    assert {
        node["ordinal"]
        for chain in flow["chains"]
        for node in chain["path"]
        if node.get("kind") == "source"
    } == {1, 2}
    assert len({chain["chain_id"] for chain in flow["chains"]}) == 2


def test_same_helper_from_different_callsites_has_stable_distinct_chain_ids(tmp_path: Path) -> None:
    source = '''package com.example;
class RouterActivity { WebView web;
 void onCreate(Intent intent) {
  String value = intent.getStringExtra("url");
  forward(value);
  forward(value);
 }
 void forward(String value) {
  web.loadUrl(value);
 }
}
'''
    first = _analyze(tmp_path / "first", {"com/example/RouterActivity.java": source})
    second = _analyze(tmp_path / "second", {"com/example/RouterActivity.java": source})

    first_ids = [chain["chain_id"] for chain in first["chains"]]
    assert len(first_ids) == 2
    assert len(set(first_ids)) == 2
    assert first_ids == [chain["chain_id"] for chain in second["chains"]]


def test_file_and_entry_input_order_do_not_change_chain_order(tmp_path: Path) -> None:
    activity = '''package com.example;
class RouterActivity { WebView web;
 void onCreate(Intent intent) { Helper.forward(intent.getStringExtra("create")); }
 void onNewIntent(Intent intent) { Helper.forward(intent.getStringExtra("new")); }
}
'''
    helper = '''package com.example;
class Helper { WebView web;
 static void forward(String value) { web.loadUrl(value); }
}
'''
    first = _analyze(tmp_path / "first", {
        "com/example/RouterActivity.java": activity,
        "com/example/Helper.java": helper,
    }, entries={"onCreate", "onNewIntent"})
    second = _analyze(tmp_path / "second", {
        "com/example/Helper.java": helper,
        "com/example/RouterActivity.java": activity,
    }, entries={"onNewIntent", "onCreate"})

    assert [chain["chain_id"] for chain in first["chains"]] == [
        chain["chain_id"] for chain in second["chains"]
    ]


def _budget_source() -> str:
    return '''package com.example;
class RouterActivity { WebView web;
 void onCreate(Intent intent) {
  String value = intent.getStringExtra("url");
  first(value);
  web.evaluateJavascript(value, null);
 }
 void first(String value) { second(value); }
 void second(String value) { web.loadUrl(value); }
}
'''


@pytest.mark.parametrize(
    ("options", "gap_code"),
    [
        ({"max_chains": 1}, "DATAFLOW_CHAIN_BUDGET_EXCEEDED"),
        ({"max_ir_steps": 1}, "DATAFLOW_IR_STEP_BUDGET_EXCEEDED"),
        ({"max_call_depth": 1}, "DATAFLOW_CALL_DEPTH_EXCEEDED"),
        ({"max_methods": 1}, "DATAFLOW_METHOD_BUDGET_EXCEEDED"),
    ],
)
def test_dataflow_budgets_stop_deterministically_and_report_usage(
    tmp_path: Path,
    options: dict[str, int],
    gap_code: str,
) -> None:
    flow = _analyze(
        tmp_path,
        {"com/example/RouterActivity.java": _budget_source()},
        **options,
    )

    gaps = [gap for gap in flow["coverage_gaps"] if gap["code"] == gap_code]
    assert len(gaps) == 1
    assert gaps[0]["critical"] is True
    assert gaps[0]["usage"] >= gaps[0]["limit"]
    assert flow["dataflow_status"] == "not_proven"
    assert all(chain["dataflow_status"] == "not_proven" for chain in flow["chains"])


def test_branch_gap_only_blocks_following_fallthrough_chain(tmp_path: Path) -> None:
    flow = _analyze(tmp_path, {
        "com/example/RouterActivity.java": _activity(
            '  String url = intent.getStringExtra("url");\n'
            "  web.loadUrl(url);\n"
            "  if (flag) return;\n"
            "  web.evaluateJavascript(url, null);"
        ),
    })

    assert len(flow["chains"]) == 2
    first_codes = {gap["code"] for gap in flow["chains"][0]["blocking_gaps"]}
    second_codes = {gap["code"] for gap in flow["chains"][1]["blocking_gaps"]}
    assert "LINEAR_IR_PATH_SENSITIVITY_LIMITATION" not in first_codes
    assert "LINEAR_IR_PATH_SENSITIVITY_LIMITATION" in second_codes


def test_structured_source_roles_disable_name_fallback_and_helpers_do_not_reseed(tmp_path: Path) -> None:
    ordinary_entry = _analyze(tmp_path / "ordinary", {
        "com/example/RouterActivity.java": _activity(
            "  web.loadUrl(intent);",
            parameters="String intent",
        ),
    })
    helper = _analyze(tmp_path / "helper", {
        "com/example/RouterActivity.java": '''package com.example;
class RouterActivity { WebView web;
 void onCreate() { helper("https://safe.example"); }
 void helper(String intent) { web.loadUrl(intent); }
}
''',
    })
    structured_source = _analyze(tmp_path / "structured", {
        "com/example/RouterActivity.java": _activity("  web.loadUrl(intent);")
    })

    assert ordinary_entry["chains"] == []
    assert helper["chains"] == []
    assert structured_source["source"]["source_kind"] == "intent"


def test_legacy_parameter_name_fallback_when_structured_data_is_absent() -> None:
    method = {
        "id": "Legacy.java#onCreate:1",
        "name": "onCreate",
        "path": "Legacy.java",
        "start_line": 1,
        "parameters": "Intent intent",
        "flow_ir": [{"op": "call", "ordinal": 1, "line": 2}],
        "call_sites": [{
            "ordinal": 1,
            "method_name": "loadUrl",
            "receiver_text": "web",
            "receiver_type": "android.webkit.WebView",
            "arguments": ["intent"],
            "start_line": 2,
            "resolve_status": "unresolved",
        }],
    }
    flow = DataFlowAnalyzer([
        {"path": "Legacy.java", "methods": [method]},
    ], entry_method_ids=[method["id"]]).analyze_entry({"onCreate"})

    assert flow["source"]["source_kind"] == "legacy_parameter_name"
    assert len(flow["chains"]) == 1
