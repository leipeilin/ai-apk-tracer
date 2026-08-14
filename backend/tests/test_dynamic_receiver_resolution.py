from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.analysis.indexer import _extract_structure, build_code_index

RULES_ROOT = Path(__file__).resolve().parents[2] / "rules"
if str(RULES_ROOT) not in sys.path:
    sys.path.insert(0, str(RULES_ROOT))

from shared.detector import execute  # noqa: E402


def _index(tmp_path: Path, sources: dict[str, str]) -> dict:
    source_root = tmp_path / "sources"
    for relative, content in sources.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    index_root = tmp_path / "index"
    descriptor = build_code_index(source_root, index_root / "code-index.json")
    return {**descriptor, "allowed_index_root": index_root.resolve().as_posix()}


def _manifest(**updates: object) -> dict:
    value = {
        "analysis_platform_api": 36,
        "target_sdk": 36,
        "components": [],
        "custom_permissions": {},
        "protected_broadcast_actions": [],
        "protected_broadcast_catalog_version": "test-api-36",
    }
    value.update(updates)
    return value


def _execute(tmp_path: Path, sources: dict[str, str], manifest: dict | None = None) -> list[dict]:
    return execute(
        "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION",
        {"manifest": manifest or _manifest(), "index": _index(tmp_path, sources)},
    )["candidates"]


def _same_file_registration(call: str, *, effect: bool = True, action: str = '"com.example.SYNC"') -> str:
    effect_lines = (
        '  String action = intent.getAction();\n  if ("com.example.SYNC".equals(action)) manager.startSport();'
        if effect else "  log(intent);"
    )
    return f"""package com.example;
class DemoReceiver {{
 SportManager manager;
 void register(Context context) {{
  DemoReceiver receiver = new DemoReceiver();
  IntentFilter filter = new IntentFilter({action});
  {call}
 }}
 void onReceive(Context context, Intent intent) {{
{effect_lines}
 }}
}}
"""


@pytest.mark.parametrize(
    ("declaration", "flag", "expected"),
    [
        ("static final int FLAGS = 2;", "FLAGS", True),
        ("static final int FLAGS = 0x2 | Context.RECEIVER_VISIBLE_TO_INSTANT_APPS;", "FLAGS", True),
        ("static final int FLAGS = Context.RECEIVER_EXPORTED | 0x1;", "FLAGS", True),
        ("static final int FLAGS = 4;", "FLAGS", False),
        ("static final int FLAGS = 0x4 | Context.RECEIVER_VISIBLE_TO_INSTANT_APPS;", "FLAGS", False),
    ],
)
def test_numeric_hex_bitwise_and_same_file_flag_constants(
    tmp_path: Path, declaration: str, flag: str, expected: bool
) -> None:
    source = _same_file_registration(f"context.registerReceiver(receiver, filter, {flag});")
    source = source.replace("class DemoReceiver {", f"class DemoReceiver {{\n {declaration}")
    candidates = _execute(tmp_path, {"com/example/DemoReceiver.java": source})
    assert bool(candidates) is expected
    if expected:
        assert candidates[0]["receiver_binding"]["flag_value"] & 0x2
        assert candidates[0]["evidence_level"] == "L2"
        assert candidates[0]["dataflow_status"] == "intraprocedural"


def test_context_and_context_compat_permission_overloads_use_real_permission_strength(tmp_path: Path) -> None:
    context_source = _same_file_registration(
        "context.registerReceiver(receiver, filter, RECEIVE_PERMISSION, null, FLAGS);"
    ).replace(
        "class DemoReceiver {",
        'class DemoReceiver {\n static final int FLAGS = 2;\n static final String RECEIVE_PERMISSION = "com.example.NORMAL";',
    )
    normal = _execute(
        tmp_path / "context",
        {"com/example/DemoReceiver.java": context_source},
        _manifest(custom_permissions={"com.example.NORMAL": "normal"}),
    )
    assert len(normal) == 1
    assert normal[0]["permission"] == "com.example.NORMAL"
    assert normal[0]["authorization_status"] == "conditional"
    assert normal[0]["evidence_level"] == "L2"

    compat_source = _same_file_registration(
        "ContextCompat.registerReceiver(context, receiver, filter, RECEIVE_PERMISSION, null, FLAGS);"
    ).replace(
        "class DemoReceiver {",
        'class DemoReceiver {\n static final int FLAGS = 0x2;\n static final String RECEIVE_PERMISSION = "com.example.SIGNATURE";',
    )
    signature = _execute(
        tmp_path / "compat",
        {"com/example/DemoReceiver.java": compat_source},
        _manifest(custom_permissions={"com.example.SIGNATURE": "signature|privileged"}),
    )
    assert signature == []


def test_unknown_flag_and_permission_are_l1_with_critical_gaps(tmp_path: Path) -> None:
    source = _same_file_registration(
        "ContextCompat.registerReceiver(context, receiver, filter, runtimePermission, null, runtimeFlags);"
    )
    candidate = _execute(tmp_path, {"com/example/DemoReceiver.java": source})[0]
    assert candidate["evidence_level"] == "L1"
    assert candidate["deterministic_chain_verified"] is False
    gap_codes = {gap["code"] for gap in candidate["blocking_gaps"]}
    assert "RECEIVER_FLAG_UNKNOWN" in gap_codes
    assert any("PERMISSION" in code for code in gap_codes)
    assert all(gap["critical"] for gap in candidate["blocking_gaps"])


def test_manifest_protected_action_suppresses_dynamic_receiver(tmp_path: Path) -> None:
    action = "android.intent.action.BOOT_COMPLETED"
    source = _same_file_registration(
        "context.registerReceiver(receiver, filter, 2);",
        action="PROTECTED_ACTION",
    ).replace(
        "class DemoReceiver {",
        f'class DemoReceiver {{\n static final String PROTECTED_ACTION = "{action}";',
    )
    assert _execute(
        tmp_path,
        {"com/example/DemoReceiver.java": source},
        _manifest(protected_broadcast_actions=[action]),
    ) == []


def test_local_broadcast_is_decided_per_registration_call(tmp_path: Path) -> None:
    source = """package com.example;
class DemoReceiver {
 SportManager manager;
 void register(Context context) {
  DemoReceiver receiver = new DemoReceiver();
  IntentFilter localFilter = new IntentFilter("com.example.LOCAL");
  IntentFilter externalFilter = new IntentFilter("com.example.EXTERNAL");
  LocalBroadcastManager.getInstance(context).registerReceiver(receiver, localFilter);
  context.registerReceiver(receiver, externalFilter, 2);
 }
 void onReceive(Context context, Intent intent) { manager.startSport(); }
}
"""
    candidates = _execute(tmp_path, {"com/example/DemoReceiver.java": source})
    assert len(candidates) == 1
    assert candidates[0]["receiver_binding"]["registration"]["local_broadcast"] is False
    assert candidates[0]["locations"][0]["line"] == 9


def test_cross_file_receiver_uses_exact_on_receive_and_upgrades_complete_chain_to_l2(tmp_path: Path) -> None:
    registrar = """package com.example;
class Registrar {
 void register(Context context) {
  ResolvedReceiver receiver = new ResolvedReceiver();
  IntentFilter filter = new IntentFilter("com.example.SYNC");
  ContextCompat.registerReceiver(context, receiver, filter, 2);
 }
}
"""
    receiver = """package com.example;
class ResolvedReceiver extends BroadcastReceiver {
 android.hardware.SensorManager manager;
 void onReceive(Object context, Intent intent) { log(intent); }
 void onReceive(Context context, Intent intent) {
  String action = intent.getAction();
  if ("com.example.SYNC".equals(action)) apply();
 }
 void apply() { manager.registerListener(listener, sensor, 3); }
}
"""
    candidate = _execute(
        tmp_path,
        {
            "com/example/Registrar.java": registrar,
            "com/example/ResolvedReceiver.java": receiver,
        },
    )[0]
    binding = candidate["receiver_binding"]
    assert candidate["evidence_level"] == "L2"
    assert candidate["dataflow_status"] == "interprocedural"
    assert candidate["deterministic_chain_verified"] is True
    assert "ResolvedReceiver.onReceive" in binding["on_receive"]
    assert binding["transitions"][0]["effect_taxonomy"] == "location_sensor_collection"
    assert not any(gap["code"] == "RECEIVER_TARGET_AMBIGUOUS" for gap in binding["coverage_gaps"])


def test_complete_binding_emits_one_candidate_per_confirmed_effect(tmp_path: Path) -> None:
    source = """package com.example;
class DemoReceiver {
 SportManager manager; android.hardware.SensorManager sensors;
 void register(Context context) {
  DemoReceiver receiver = new DemoReceiver();
  IntentFilter filter = new IntentFilter("com.example.SYNC");
  context.registerReceiver(receiver, filter, 2);
 }
 void onReceive(Context context, Intent intent) {
  manager.startSport();
  sensors.registerListener(listener, sensor, 3);
 }
}
"""

    candidates = _execute(tmp_path, {"com/example/DemoReceiver.java": source})

    assert len(candidates) == 2
    assert len({candidate["chain_id"] for candidate in candidates}) == 2
    assert all(candidate["evidence_level"] == "L2" for candidate in candidates)
    assert all("chains" not in candidate for candidate in candidates)
    assert all(len(candidate["sinks"]) == 1 for candidate in candidates)


def test_exposure_without_resolved_effect_remains_l1(tmp_path: Path) -> None:
    source = _same_file_registration(
        "context.registerReceiver(receiver, filter, 2);",
        effect=False,
    )
    candidate = _execute(tmp_path, {"com/example/DemoReceiver.java": source})[0]
    assert candidate["evidence_level"] == "L1"
    assert candidate["impact_status"] == "potential"


def test_protected_action_with_unresolved_action_is_not_suppressed(tmp_path: Path) -> None:
    protected = "android.intent.action.BOOT_COMPLETED"
    source = _same_file_registration(
        'filter.addAction(runtimeAction);\n  context.registerReceiver(receiver, filter, 2);',
        action="PROTECTED_ACTION",
    ).replace(
        "class DemoReceiver {",
        f'class DemoReceiver {{\n static final String PROTECTED_ACTION = "{protected}";',
    )
    candidate = _execute(
        tmp_path,
        {"com/example/DemoReceiver.java": source},
        _manifest(protected_broadcast_actions=[protected]),
    )[0]
    registration = candidate["receiver_binding"]["registration"]
    assert registration["actions"] == [protected]
    assert registration["unresolved_action_expressions"] == ["runtimeAction"]
    assert registration["protected_actions_only"] is False
    assert any(
        gap["code"] == "RECEIVER_ACTION_UNRESOLVED"
        and gap["expression"] == "runtimeAction"
        and gap["critical"]
        for gap in candidate["blocking_gaps"]
    )


def test_add_action_after_registration_does_not_change_authorization(tmp_path: Path) -> None:
    protected = "android.intent.action.BOOT_COMPLETED"
    source = _same_file_registration(
        'context.registerReceiver(receiver, filter, 2);\n  filter.addAction("com.example.LATE");',
        action="PROTECTED_ACTION",
    ).replace(
        "class DemoReceiver {",
        f'class DemoReceiver {{\n static final String PROTECTED_ACTION = "{protected}";',
    )
    assert _execute(
        tmp_path,
        {"com/example/DemoReceiver.java": source},
        _manifest(protected_broadcast_actions=[protected]),
    ) == []


def test_exact_fqcn_wins_over_same_simple_name_in_other_package(tmp_path: Path) -> None:
    registrar = """package com.registrar;
import com.alpha.SharedReceiver;
class Registrar {
 void register(Context context) {
  SharedReceiver receiver = new SharedReceiver();
  IntentFilter filter = new IntentFilter("com.example.SYNC");
  context.registerReceiver(receiver, filter, 2);
 }
}
"""
    alpha = """package com.alpha;
class SharedReceiver {
 SportManager manager;
 void onReceive(Context context, Intent intent) { manager.startSport(); }
}
"""
    beta = """package com.beta;
class SharedReceiver {
 void onReceive(Context context, Intent intent) { log(intent); }
}
"""
    candidate = _execute(tmp_path, {
        "com/registrar/Registrar.java": registrar,
        "com/alpha/SharedReceiver.java": alpha,
        "com/beta/SharedReceiver.java": beta,
    })[0]
    binding = candidate["receiver_binding"]
    assert binding["receiver_qualified_class"] == "com.alpha.SharedReceiver"
    assert "com/alpha/SharedReceiver.java" in binding["on_receive"]
    assert not any(gap["code"] == "RECEIVER_TARGET_AMBIGUOUS" for gap in binding["coverage_gaps"])


def test_simple_name_fallback_with_multiple_classes_is_ambiguous(tmp_path: Path) -> None:
    registrar = """package com.registrar;
class Registrar {
 void register(Context context) {
  SharedReceiver receiver = new SharedReceiver();
  IntentFilter filter = new IntentFilter("com.example.SYNC");
  context.registerReceiver(receiver, filter, 2);
 }
}
"""
    receiver = """package {package};
class SharedReceiver {{
 SportManager manager;
 void onReceive(Context context, Intent intent) {{ manager.startSport(); }}
}}
"""
    candidate = _execute(tmp_path, {
        "com/registrar/Registrar.java": registrar,
        "com/alpha/SharedReceiver.java": receiver.format(package="com.alpha"),
        "com/beta/SharedReceiver.java": receiver.format(package="com.beta"),
    })[0]
    binding = candidate["receiver_binding"]
    assert binding["on_receive"] is None
    assert any(
        gap["code"] == "RECEIVER_TARGET_AMBIGUOUS"
        and gap["candidate_count"] == 2
        and gap["critical"]
        for gap in binding["coverage_gaps"]
    )


@pytest.mark.parametrize(
    ("flags", "gap_code"),
    [("2 | 4", "RECEIVER_FLAG_CONFLICT"), ("1", "RECEIVER_FLAG_EXPORT_STATE_UNKNOWN")],
)
def test_invalid_export_flag_states_remain_critical(
    tmp_path: Path, flags: str, gap_code: str
) -> None:
    source = _same_file_registration(f"context.registerReceiver(receiver, filter, {flags});")
    candidate = _execute(tmp_path, {"com/example/DemoReceiver.java": source})[0]
    assert candidate["evidence_level"] == "L1"
    assert any(
        gap["code"] == gap_code and gap["critical"]
        for gap in candidate["blocking_gaps"]
    )


def test_legacy_dynamic_receiver_respects_manifest_authorization() -> None:
    protected = "android.intent.action.BOOT_COMPLETED"
    protected_source = _same_file_registration(
        "context.registerReceiver(receiver, filter, 2);",
        action=f'"{protected}"',
    )
    permission_source = _same_file_registration(
        'context.registerReceiver(receiver, filter, "com.example.SIGNATURE", null, 2);'
    )

    def legacy_file(path: str, source: str) -> dict:
        return {
            "path": path,
            "content": source,
            **_extract_structure(path, source, ".java"),
        }

    manifest = _manifest(
        protected_broadcast_actions=[protected],
        custom_permissions={"com.example.SIGNATURE": "signature"},
    )
    result = execute(
        "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION",
        {
            "manifest": manifest,
            "code_index": {"files": [
                legacy_file("ProtectedReceiver.java", protected_source),
                legacy_file("PermissionReceiver.java", permission_source),
            ]},
        },
    )
    assert result["candidates"] == []
