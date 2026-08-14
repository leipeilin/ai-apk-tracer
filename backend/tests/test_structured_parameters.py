from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from app.analysis.indexer import _extract_structure, build_code_index, parse_structured_parameters
from app.analysis.index_store import SCHEMA_VERSION, SQLiteCodeIndexReader

RULES_ROOT = Path(__file__).resolve().parents[2] / "rules"
if str(RULES_ROOT) not in sys.path:
    sys.path.insert(0, str(RULES_ROOT))

from shared.index_reader import RuleIndexReader  # noqa: E402


def test_java_obfuscated_parameters_are_balanced_and_typed() -> None:
    parameters = parse_structured_parameters(
        '@Named(values = {"a,b", "c"}) final Map<String, List<Foo>> a, String... b',
        language="java",
        method_name="a",
        package="x",
        imports=["java.util.Map"],
    )

    assert [(item["position"], item["name"], item["normalized_type"]) for item in parameters] == [
        (0, "a", "Map"),
        (1, "b", "String[]"),
    ]
    assert parameters[0]["qualified_type"] == "java.util.Map"
    assert parameters[0]["descriptor"] == "Ljava/util/Map;"
    assert parameters[1]["descriptor"] == "[Ljava/lang/String;"
    assert all(item["source_kind"] is None for item in parameters)


def test_realistic_multiline_kotlin_signature_and_receiver() -> None:
    source = '''package com.example
import android.content.Intent
import java.util.Map
class Router {
 override fun onNewIntent(
   @Named("incoming") intent: Intent?,
   mapping: Map<String, List<Int>> = mapOf("a,b" to listOf(1, 2))
 ): Unit {
   intent?.handle(mapping)
 }
 fun Intent.handle(mapping: Map<String, List<Int>>) = this.getAction()
}
'''

    methods = {method["name"]: method for method in _extract_structure("Router.kt", source, ".kt")["methods"]}
    entry = methods["onNewIntent"]
    extension = methods["handle"]

    assert entry["parameters"].endswith('mapOf("a,b" to listOf(1, 2))')
    assert [(item["name"], item["declared_type"], item["normalized_type"]) for item in entry["structured_parameters"]] == [
        ("intent", "Intent?", "Intent"),
        ("mapping", "Map<String, List<Int>>", "Map"),
    ]
    assert entry["structured_parameters"][0]["source_kind"] == "intent"
    assert extension["structured_parameters"][0]["name"] == "mapping"
    assert next(call for call in extension["call_sites"] if call["method_name"] == "getAction")["receiver_type"] == "android.content.Intent"


def test_entrypoint_and_provider_parameter_roles_are_signature_scoped() -> None:
    receiver = parse_structured_parameters(
        "Context context, Intent intent, String label",
        language="java",
        method_name="onReceive",
        imports=["android.content.Context", "android.content.Intent"],
    )
    query = parse_structured_parameters(
        "Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder",
        language="java",
        method_name="query",
        imports=["android.net.Uri"],
    )
    ordinary = parse_structured_parameters(
        "String value", language="java", method_name="helper"
    )

    assert [item["source_kind"] for item in receiver] == [None, "intent", None]
    assert [item["source_kind"] for item in query] == [
        "provider_uri",
        "provider_projection",
        "provider_selection",
        "provider_selection_args",
        "provider_sort_order",
    ]
    assert ordinary[0]["source_kind"] is None
    assert query[0]["source_basis"] == "android-provider-signature:query[0]:Uri"


@pytest.mark.parametrize(
    ("signature", "expected_registers"),
    [
        (".method public onStartCommand(Landroid/content/Intent;IJD)V", [("p1", 1), ("p2", 1), ("p3", 2), ("p5", 2)]),
        (".method public static onStartCommand(Landroid/content/Intent;IJD)V", [("p0", 1), ("p1", 1), ("p2", 2), ("p4", 2)]),
    ],
)
def test_smali_explicit_parameters_use_correct_p_registers(
    signature: str, expected_registers: list[tuple[str, int]]
) -> None:
    source = f''' .class public Lcom/example/Service;
.super Ljava/lang/Object;
{signature}
    return-void
.end method
'''

    method = _extract_structure("Service.smali", source, ".smali")["methods"][0]

    assert [(item["register"], item["register_width"]) for item in method["structured_parameters"]] == expected_registers
    assert method["structured_parameters"][0]["source_kind"] == "intent"
    assert method["source_language"] == "smali"
    assert method["smali_descriptor_only"] is True
    assert method["limitations"][0]["code"] == "SMALI_REGISTER_FLOW_UNPROVEN"
    assert method["summary"]["limitations"][0]["code"] == "SMALI_REGISTER_FLOW_UNPROVEN"


def _build_roundtrip_index(tmp_path: Path) -> dict:
    source_root = tmp_path / "sources"
    source_path = source_root / "com/example/Receiver.java"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        '''package com.example;
import android.content.Context;
import android.content.Intent;
class Receiver {
 void onReceive(Context context, Intent intent) { intent.getAction(); }
}
''',
        "utf-8",
    )
    return build_code_index(source_root, tmp_path / "index" / "code-index.json")


def test_sqlite_structured_parameters_round_trip_for_all_read_paths(tmp_path: Path) -> None:
    descriptor = _build_roundtrip_index(tmp_path)
    reader = SQLiteCodeIndexReader(descriptor)
    try:
        stored = reader.db.execute("SELECT parameters_text, parameters_json FROM methods").fetchone()
        lightweight = reader.load_lightweight_structure_files()[0]["methods"][0]
        full = reader.load_structure_files()[0]["methods"][0]
        batched = reader.get_methods_for_files([1])[1][0]
    finally:
        reader.close()

    rule_reader = RuleIndexReader({
        **descriptor,
        "allowed_index_root": (tmp_path / "index").resolve().as_posix(),
    })
    try:
        rule_method = rule_reader.component_files("com.example.Receiver")[0]["methods"][0]
    finally:
        rule_reader.close()

    assert descriptor["schema_version"] == SCHEMA_VERSION == "2.9.0"
    assert stored["parameters_text"] == "Context context, Intent intent"
    assert isinstance(stored["parameters_json"], bytes)
    for method in (lightweight, full, batched, rule_method):
        assert method["parameters"] == "Context context, Intent intent"
        assert method["structured_parameters"][1]["source_kind"] == "intent"


def test_old_descriptor_and_meta_require_index_rebuild(tmp_path: Path) -> None:
    descriptor = _build_roundtrip_index(tmp_path)
    with pytest.raises(ValueError, match="INDEX_SCHEMA_REBUILD_REQUIRED"):
        SQLiteCodeIndexReader({**descriptor, "schema_version": "2.8.0"})

    with sqlite3.connect(descriptor["database_path"]) as db:
        db.execute("UPDATE meta SET value='2.8.0' WHERE key='schema_version'")
    with pytest.raises(ValueError, match="INDEX_SCHEMA_REBUILD_REQUIRED"):
        SQLiteCodeIndexReader(descriptor)
    with pytest.raises(ValueError, match="INDEX_SCHEMA_REBUILD_REQUIRED"):
        RuleIndexReader({
            **descriptor,
            "allowed_index_root": (tmp_path / "index").resolve().as_posix(),
        })
