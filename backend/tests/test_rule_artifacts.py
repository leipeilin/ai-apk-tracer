"""规则产物 Schema 校验测试（T0.4）。

三个确定性产物 schema（binder_bindings / receiver_registrations / webview_js_bridges）
由规则侧导出（T2.1）、api_surface 读取（T2.2）。字段名对齐规则侧实际产出
（index_reader._binder_transactions / receiver_registration._parse_call /
detector._webview_crypto_match）；service_class、resolve_status、path、bridge_name
等由 T2.1 导出层注入/推导（评审 R-2/R-5）。
"""

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


def _load_schema(name: str) -> dict:
    with (SCHEMAS_DIR / name).open(encoding="utf-8") as fp:
        return json.load(fp)


def _binder_sample() -> dict:
    return {
        "schema_version": "1.0.0",
        "bindings": [
            {
                "service_class": "com/example/SportXmsApi",
                "code": 1,
                "case_token": "TRANSACTION_finishSport",
                "interface_method": "finishSport",
                "descriptor": "com.example.ISportApi",
                "on_transact_method_id": "sources/com/example/ISportApi$Stub.java#onTransact:100",
                "on_transact_descriptor": "com.example.ISportApi",
                "case_line": 42,
                "dispatch_call_site": {"line": 55, "text": "reply.writeNoException(); result = ..."},
                "dispatch_descriptor": "com.example.ISportApi",
                "dispatch_assigned_to": "var_result",
                "path": "sources/com/example/ISportApi$Stub.java",
                "line": 42,
                "implementation_class": "com/example/SportXmsApiImpl",
                "implementation_method_id": "sources/com/example/SportXmsApiImpl.java#finishSport:504",
                "implementation_path": "sources/com/example/SportXmsApiImpl.java",
                "implementation_line": 504,
                "resolve_status": "bound",
                "gaps": [],
            },
            {
                "service_class": "com/example/AmbiguousApi",
                "code": 2,
                "interface_method": "ambiguousCall",
                "path": "sources/com/example/AmbiguousApi.java",
                "resolve_status": "ambiguous",
                "gaps": [{"code": "BINDER_IMPLEMENTATION_AMBIGUOUS", "critical": True, "detail": "2 candidates"}],
            },
            {
                "service_class": "com/example/UnresolvedApi",
                "code": 3,
                "interface_method": "unresolvedCall",
                "path": "sources/com/example/UnresolvedApi.java",
                "resolve_status": "unresolved",
                "gaps": [{"code": "BINDER_IMPLEMENTATION_UNRESOLVED", "critical": False}],
            },
        ],
    }


def _receiver_sample() -> dict:
    return {
        "schema_version": "1.0.0",
        "registrations": [
            {
                "receiver_class": "com.example.SmsReceiver",
                "call": {"name": "registerReceiver", "start_line": 120},
                "method_id": "sources/com/example/MainActivity.java#onCreate:110",
                "method_name": "onCreate",
                "path": "sources/com/example/MainActivity.java",
                "line": 120,
                "actions": ["android.provider.Telephony.SMS_RECEIVED"],
                "unresolved_action_expressions": [],
                "filter_expression": "new IntentFilter(\"android.provider.Telephony.SMS_RECEIVED\")",
                "flag_expression": "0",
                "flag_value": 32,
                "flag_status": "explicit",
                "export_status": "exported",
                "externally_reachable": True,
                "permission_expression": None,
                "permission": None,
                "permission_status": "none",
                "permission_policy": {"status": "none", "rows": []},
                "local_broadcast": False,
                "platform_branch": False,
                "reportable": True,
                "coverage_gaps": [],
            },
            {
                "receiver_class": "com.example.OpaqueReceiver",
                "path": None,
                "line": 5,
                "actions": [],
                "export_status": "unknown",
                "externally_reachable": None,
                "reportable": False,
            },
        ],
    }


def _webview_sample() -> dict:
    return {
        "schema_version": "1.0.0",
        "bridges": [
            {
                "line": 88,
                "text": "addJavascriptInterface(this, \"Android\")",
                "description": "WebView.addJavascriptInterface 注入 JS 桥",
                "sink_kind": "js_bridge",
                "path": "sources/com/example/WebHelper.java",
                "bridge_name": "Android",
            }
        ],
    }


@pytest.fixture(scope="module")
def binder_schema() -> dict:
    return _load_schema("binder_bindings.schema.json")


@pytest.fixture(scope="module")
def receiver_schema() -> dict:
    return _load_schema("receiver_registrations.schema.json")


@pytest.fixture(scope="module")
def webview_schema() -> dict:
    return _load_schema("webview_js_bridges.schema.json")


def test_binder_valid_sample_passes(binder_schema: dict) -> None:
    jsonschema.validate(_binder_sample(), binder_schema)


def test_receiver_valid_sample_passes(receiver_schema: dict) -> None:
    jsonschema.validate(_receiver_sample(), receiver_schema)


def test_webview_valid_sample_passes(webview_schema: dict) -> None:
    jsonschema.validate(_webview_sample(), webview_schema)


@pytest.mark.parametrize("schema_name", [
    "binder_bindings.schema.json",
    "receiver_registrations.schema.json",
    "webview_js_bridges.schema.json",
])
def test_schema_files_are_draft2020_objects(schema_name: str) -> None:
    schema = _load_schema(schema_name)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    assert "required" in schema


def test_binder_required_missing(binder_schema: dict) -> None:
    payload = _binder_sample()
    payload["bindings"][0].pop("resolve_status")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, binder_schema)


def test_binder_rejects_bad_resolve_status(binder_schema: dict) -> None:
    payload = _binder_sample()
    payload["bindings"][0]["resolve_status"] = "partial"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, binder_schema)


def test_binder_rejects_code_type_error(binder_schema: dict) -> None:
    payload = _binder_sample()
    payload["bindings"][0]["code"] = "1"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, binder_schema)


def test_binder_dispatch_call_site_type_error(binder_schema: dict) -> None:
    payload = _binder_sample()
    payload["bindings"][0]["dispatch_call_site"] = "call site"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, binder_schema)


def test_binder_unbound_without_implementation_ok(binder_schema: dict) -> None:
    """未绑定（unresolved）时 implementation_* 缺失应通过（可空非 required）。"""
    payload = {
        "schema_version": "1.0.0",
        "bindings": [{"service_class": "com/example/X", "code": 7, "resolve_status": "unresolved"}],
    }
    jsonschema.validate(payload, binder_schema)


def test_binder_extra_field_tolerated(binder_schema: dict) -> None:
    payload = _binder_sample()
    payload["bindings"][0]["extra_debug_field"] = {"internal": True}
    jsonschema.validate(payload, binder_schema)


def test_binder_empty_artifacts_ok(binder_schema: dict) -> None:
    jsonschema.validate({"schema_version": "1.0.0", "bindings": []}, binder_schema)


def test_receiver_required_missing(receiver_schema: dict) -> None:
    payload = _receiver_sample()
    payload["registrations"][0].pop("receiver_class")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, receiver_schema)


def test_receiver_rejects_bad_export_status(receiver_schema: dict) -> None:
    payload = _receiver_sample()
    payload["registrations"][0]["export_status"] = "partial"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, receiver_schema)


def test_receiver_rejects_non_string_action(receiver_schema: dict) -> None:
    payload = _receiver_sample()
    payload["registrations"][0]["actions"] = ["android.action.X", 123]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, receiver_schema)


def test_receiver_path_null_ok(receiver_schema: dict) -> None:
    payload = _receiver_sample()
    payload["registrations"][0]["path"] = None
    jsonschema.validate(payload, receiver_schema)


def test_receiver_class_null_ok(receiver_schema: dict) -> None:
    """T2.1 评审 R-3：opaque 注册点 receiver_class 可空（nullable schema 修订回归）。"""
    payload = _receiver_sample()
    payload["registrations"][0]["receiver_class"] = None
    jsonschema.validate(payload, receiver_schema)


def test_webview_required_missing(webview_schema: dict) -> None:
    payload = _webview_sample()
    payload["bridges"][0].pop("line")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, webview_schema)


def test_webview_line_type_error(webview_schema: dict) -> None:
    payload = _webview_sample()
    payload["bridges"][0]["line"] = "88"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, webview_schema)


def test_webview_bridge_name_null_ok(webview_schema: dict) -> None:
    payload = _webview_sample()
    payload["bridges"][0]["bridge_name"] = None
    jsonschema.validate(payload, webview_schema)


@pytest.mark.parametrize("schema_fixture", ["binder_schema", "receiver_schema", "webview_schema"])
@pytest.mark.parametrize("version", ["2.0.0", "1.0.1"])
def test_schema_version_mismatch(schema_fixture: str, version: str, request: pytest.FixtureRequest) -> None:
    schema = request.getfixturevalue(schema_fixture)
    payload = {"schema_version": version, "bindings": [], "registrations": [], "bridges": []}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


def test_top_level_not_object(binder_schema: dict) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate([], binder_schema)


# ---------------------------------------------------------------------------
# api_entry_table / attack_surface（T0.5）：探索轨 Agent1 的确定性输入
# ---------------------------------------------------------------------------

def _api_entry_table_sample() -> dict:
    return {
        "schema_version": "1.0.0",
        "package": "com.example",
        "api_entries": [
            {
                "entry_id": "act_com_example_SplashActivity_onCreate",
                "kind": "activity",
                "component_name": "com.example.SplashActivity",
                "source": "manifest",
                "exported": True,
                "permissions": [],
                "entry_method": "onCreate(Landroid/os/Bundle;)V",
                "intent_filters": [{"actions": ["android.intent.action.VIEW"], "categories": ["android.intent.category.DEFAULT"], "data": [{"scheme": "https"}]}],
                "reliability": "not_applicable",
            },
            {
                "entry_id": "svc_com_example_JobService_onStartCommand",
                "kind": "service",
                "component_name": "com.example.JobService",
                "source": "manifest",
                "exported": False,
                "entry_method": "onStartCommand(Landroid/content/Intent;II)I",
            },
            {
                "entry_id": "prv_com_example_FileProvider_getType",
                "kind": "provider",
                "component_name": "com.example.FileProvider",
                "source": "manifest",
                "exported": True,
                "authorities": ["com.example.files"],
            },
            {
                "entry_id": "rcv_com_example_BootReceiver_onReceive",
                "kind": "receiver",
                "component_name": "com.example.BootReceiver",
                "source": "manifest",
                "exported": True,
                "actions": ["android.intent.action.BOOT_COMPLETED"],
            },
            {
                "entry_id": "binder_com_example_ISportXms_finishSport",
                "kind": "binder",
                "component_name": "com.example.SportXmsService",
                "source": "rule_artifact:binder_bindings",
                "exported": True,
                "interface_method": "finishSport",
                "transaction_code": 4,
                "implementation_method_id": "com/example/SportXmsApiImpl.java#finishSport:504",
                "reliability": "bound",
            },
            {
                "entry_id": "binder_com_example_AmbiguousApi_call",
                "kind": "binder",
                "component_name": "com.example.AmbiguousService",
                "source": "rule_artifact:binder_bindings",
                "interface_method": "call",
                "transaction_code": 2,
                "reliability": "ambiguous",
            },
            {
                "entry_id": "dynrcv_com_example_SmsReceiver_onReceive",
                "kind": "receiver",
                "component_name": "com.example.SmsReceiver",
                "source": "rule_artifact:receiver_registrations",
                "actions": ["android.provider.Telephony.SMS_RECEIVED"],
                "export_status": "exported",
                "externally_reachable": True,
            },
            {
                "entry_id": "webview_com_example_WebHelper_addJavascriptInterface",
                "kind": "webview_bridge",
                "component_name": "com.example.WebHelper",
                "source": "rule_artifact:webview_js_bridges",
                "bridge_path": "sources/com/example/WebHelper.java",
                "bridge_line": 88,
                "bridge_name": "Android",
            },
        ],
    }


def _attack_surface_activity_sample() -> dict:
    return {
        "schema_version": "1.0.0",
        "package": "com.example",
        "components": [
            {
                "kind": "activity",
                "name": "com.example.SplashActivity",
                "exported": True,
                "exported_reason": "intent_filter_default",
                "permission": None,
                "permission_protection": None,
                "entry_methods": ["onCreate(Landroid/os/Bundle;)V", "onNewIntent(Landroid/content/Intent;)V"],
                "intent_filters": [{"actions": ["android.intent.action.VIEW"], "categories": ["android.intent.category.DEFAULT"], "data": [{"scheme": "https"}]}],
                "sensitive_capabilities": ["WEBVIEW_FILE_ACCESS_ENABLED"],
                "api_entry_refs": ["act_com_example_SplashActivity_onCreate"],
                "source": "manifest",
            }
        ],
    }


def _attack_surface_receiver_sample() -> dict:
    return {
        "schema_version": "1.0.0",
        "package": "com.example",
        "components": [
            {
                "kind": "receiver",
                "name": "com.example.SmsReceiver",
                "exported": True,
                "entry_methods": ["onReceive(Landroid/content/Context;Landroid/content/Intent;)V"],
                "actions": ["android.provider.Telephony.SMS_RECEIVED"],
                "sensitive_capabilities": ["EXPORTED_RECEIVER"],
                "api_entry_refs": ["rcv_com_example_SmsReceiver_onReceive", "dynrcv_com_example_SmsReceiver_onReceive"],
                "source": "manifest+dynamic",
            }
        ],
    }


@pytest.fixture(scope="module")
def api_entry_schema() -> dict:
    return _load_schema("api_entry_table.schema.json")


@pytest.fixture(scope="module")
def attack_surface_schema() -> dict:
    return _load_schema("attack_surface.schema.json")


def test_api_entry_table_sample_passes(api_entry_schema: dict) -> None:
    jsonschema.validate(_api_entry_table_sample(), api_entry_schema)


def test_api_entry_manifest_entries_pass(api_entry_schema: dict) -> None:
    """act/svc/prv/rcv 四类 manifest 入口（评审 R-4 补全覆盖）。"""
    sample = _api_entry_table_sample()
    for entry in sample["api_entries"][:4]:
        jsonschema.validate({"schema_version": "1.0.0", "package": "com.example", "api_entries": [entry]}, api_entry_schema)


def test_attack_surface_activity_passes(attack_surface_schema: dict) -> None:
    jsonschema.validate(_attack_surface_activity_sample(), attack_surface_schema)


def test_attack_surface_receiver_passes(attack_surface_schema: dict) -> None:
    jsonschema.validate(_attack_surface_receiver_sample(), attack_surface_schema)


@pytest.mark.parametrize("field", ["entry_id", "kind", "component_name", "source"])
def test_api_entry_required_missing(field: str, api_entry_schema: dict) -> None:
    payload = _api_entry_table_sample()
    payload["api_entries"][0].pop(field)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, api_entry_schema)


def test_api_entry_required_package(api_entry_schema: dict) -> None:
    payload = _api_entry_table_sample()
    payload.pop("package")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, api_entry_schema)


@pytest.mark.parametrize("field,value", [
    ("kind", "widget"),
    ("source", "bogus"),
    ("reliability", "partial"),
    ("reliability", "unknown"),
])
def test_api_entry_rejects_wrong_enums(field: str, value: str, api_entry_schema: dict) -> None:
    payload = _api_entry_table_sample()
    payload["api_entries"][4][field] = value
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, api_entry_schema)


@pytest.mark.parametrize("entry_id", [
    "xxx_com_example_SplashActivity_onCreate",
    "act_非法",
    "rcv",
    "wv_com_example_x",
    "act_",
])
def test_api_entry_rejects_bad_entry_id_pattern(entry_id: str, api_entry_schema: dict) -> None:
    payload = _api_entry_table_sample()
    payload["api_entries"][0]["entry_id"] = entry_id
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, api_entry_schema)


def test_api_entry_rejects_type_error(api_entry_schema: dict) -> None:
    payload = _api_entry_table_sample()
    payload["api_entries"][4]["transaction_code"] = "1"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, api_entry_schema)
    payload = _api_entry_table_sample()
    payload["api_entries"][7]["bridge_line"] = "88"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, api_entry_schema)


def test_api_entry_empty_ok(api_entry_schema: dict) -> None:
    jsonschema.validate({"schema_version": "1.0.0", "package": "com.example", "api_entries": []}, api_entry_schema)


def test_attack_surface_required_missing(attack_surface_schema: dict) -> None:
    payload = _attack_surface_activity_sample()
    payload["components"][0].pop("exported")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, attack_surface_schema)


def test_attack_surface_rejects_wrong_enums(attack_surface_schema: dict) -> None:
    payload = _attack_surface_activity_sample()
    payload["components"][0]["kind"] = "widget"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, attack_surface_schema)
    payload = _attack_surface_receiver_sample()
    payload["components"][0]["source"] = "bogus"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, attack_surface_schema)


def test_attack_surface_entry_methods_empty_ok(attack_surface_schema: dict) -> None:
    """code-index 未解析入口方法时允许空数组（确定性原则，不伪造）。"""
    payload = _attack_surface_activity_sample()
    payload["components"][0]["entry_methods"] = []
    jsonschema.validate(payload, attack_surface_schema)


def test_attack_surface_top_level_not_object(attack_surface_schema: dict) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate([], attack_surface_schema)


@pytest.mark.parametrize("schema_fixture", ["api_entry_schema", "attack_surface_schema"])
def test_api_attack_schema_version_mismatch(schema_fixture: str, request: pytest.FixtureRequest) -> None:
    schema = request.getfixturevalue(schema_fixture)
    payload = {"schema_version": "2.0.0", "package": "com.example", "api_entries": [], "components": []}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)
