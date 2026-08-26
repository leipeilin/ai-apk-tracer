"""API 入口表生成测试（T2.2）。

设计：docs/analysis/explorer-track/2026-08-22-t2-2-implementation-plan.md（含评审 R-1~R-9 修订）。
构造模式复用 T2.1（tmp 源码 → build_code_index 真实 index）；规则产物手写
rule-results/*.json（T2.1 落盘结构）。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import jsonschema
from fastapi.testclient import TestClient

from app.analysis.api_surface import build_api_entry_table
from app.analysis.index_store import SQLiteCodeIndexReader
from app.analysis.indexer import build_code_index
from app.config import (
    ApiSurfaceSettings,
    Settings,
    SourceAnalysisSettings,
    StorageSettings,
)
from app.main import create_app

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"

_SPLASH_SOURCE = """package com.example;
import android.os.Bundle;
public class SplashActivity {
  protected void onCreate(Bundle savedInstanceState) {
  }
  protected void onNewIntent(android.content.Intent intent) {
  }
  void helper() {
  }
}
"""

# 同简名异包类（评审 R-3：不得给 com.example.SplashActivity 供方法）
_OTHER_SPLASH_SOURCE = """package com.other;
import android.os.Bundle;
public class SplashActivity {
  protected void onCreate(Bundle savedInstanceState) {
  }
}
"""

_RECEIVER_SOURCE = """package com.example;
public class BootReceiver {
  public void onReceive(android.content.Context context, android.content.Intent intent) {
  }
}
"""


def _index_reader(tmp_path: Path, sources: dict[str, str]) -> SQLiteCodeIndexReader | None:
    source_root = tmp_path / "sources"
    for relative, content in sources.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    descriptor = build_code_index(source_root, tmp_path / "index" / "code-index.json")
    return SQLiteCodeIndexReader(descriptor)


def _manifest() -> dict:
    return {
        "package": "com.example",
        "components": [
            {
                "kind": "activity", "name": "com.example.SplashActivity",
                "exported": "conditional", "exported_reason": "intent_filter_default",
                "permission": None, "intent_filters": [{"actions": ["android.intent.action.VIEW"]}],
                "authorities": None,
            },
            {
                "kind": "service", "name": "com.example.JobService",
                "exported": "true", "exported_reason": "manifest_true",
                "permission": "com.example.PERM", "intent_filters": None, "authorities": None,
            },
            {
                "kind": "provider", "name": "com.example.FileProvider",
                "exported": "false", "exported_reason": "manifest_false",
                "permission": None, "intent_filters": None,
                "authorities": ["com.example.files"],
            },
        ],
    }


def _write_artifact(run_dir: Path, name: str, entry_key: str, records: list[dict]) -> None:
    path = run_dir / "rule-results" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": "1.0.0", entry_key: records}, ensure_ascii=False),
        "utf-8",
    )


def _build(tmp_path: Path, manifest: dict, reader=None, **settings_overrides) -> dict:
    settings = ApiSurfaceSettings(**settings_overrides)
    return build_api_entry_table(tmp_path, manifest, settings, reader)


def _validate_schema(table: dict) -> None:
    schema = json.loads((SCHEMAS_DIR / "api_entry_table.schema.json").read_text("utf-8"))
    jsonschema.validate(table, schema)


# ---------------------------------------------------------------------------
# manifest 入口（A-1/A-2 + R-2 四值域 + R-3 防误匹配）
# ---------------------------------------------------------------------------


def test_manifest_entries_with_lifecycle_methods(tmp_path: Path) -> None:
    reader = _index_reader(tmp_path, {
        "com/example/SplashActivity.java": _SPLASH_SOURCE,
        "com/other/SplashActivity.java": _OTHER_SPLASH_SOURCE,
    })
    table = _build(tmp_path, _manifest(), reader)

    _validate_schema(table)
    assert table["package"] == "com.example"
    splash_entries = [e for e in table["api_entries"] if e["component_name"] == "com.example.SplashActivity"]
    assert len(splash_entries) == 2  # onCreate + onNewIntent（每方法一条）
    methods = {e["entry_method"] for e in splash_entries}
    # 评审 R-1：entry_method 为 index 实际格式 name(params)->return（非 JVM 形态）
    assert all(")->" in m for m in methods)
    assert any(m.startswith("onCreate(") for m in methods)
    assert any(m.startswith("onNewIntent(") for m in methods)
    # helper（非 lifecycle）不出现在入口方法
    assert not any("helper" in (e.get("entry_method") or "") for e in table["api_entries"])
    # 评审 R-3：同简名异包类不误匹配——entry_method 只来自 com.example 类
    # （参数为声明处简单名形态：onCreate(Bundle)->void）
    on_create = next(e for e in splash_entries if e["entry_method"].startswith("onCreate"))
    assert on_create["entry_method"] == "onCreate(Bundle)->void"
    # 评审 R-2：exported 四值域映射 + exported_reason 透传
    assert on_create["exported"] is None  # conditional → None
    assert on_create["exported_reason"] == "intent_filter_default"
    assert on_create["permissions"] == []
    assert on_create["intent_filters"] == [{"actions": ["android.intent.action.VIEW"]}]
    assert on_create["reliability"] == "not_applicable"
    # service：exported true → True + permission → permissions
    service_entry = next(e for e in table["api_entries"] if e["component_name"] == "com.example.JobService")
    assert service_entry["exported"] is True
    assert service_entry["permissions"] == ["com.example.PERM"]
    assert service_entry["entry_method"] is None  # 类不在 index——不伪造
    # provider：authorities 透传
    provider_entry = next(e for e in table["api_entries"] if e["component_name"] == "com.example.FileProvider")
    assert provider_entry["authorities"] == ["com.example.files"]
    assert provider_entry["exported"] is False


def test_manifest_entry_without_reader(tmp_path: Path) -> None:
    """source 关闭（reader=None）：entry_method=null 降级（不伪造）。"""
    table = _build(tmp_path, _manifest(), None)
    _validate_schema(table)
    splash = [e for e in table["api_entries"] if e["component_name"] == "com.example.SplashActivity"]
    assert len(splash) == 1  # 组件级单条
    assert splash[0]["entry_method"] is None


# ---------------------------------------------------------------------------
# 规则产物入口（A-3~A-5 + R-5 空数组 + R-7 路径语义）
# ---------------------------------------------------------------------------


def test_binder_entries_from_artifact(tmp_path: Path) -> None:
    _write_artifact(tmp_path, "binder_bindings", "bindings", [
        {"service_class": "com.example.JobService", "code": 1, "interface_method": "startJob",
         "implementation_method_id": "com/example/JobImpl.java#startJob:9",
         "resolve_status": "bound"},
        {"service_class": "com.example.Ambiguous", "code": 2, "interface_method": "call",
         "resolve_status": "ambiguous"},
        {"service_class": "com.example.NoImpl", "code": 7, "resolve_status": "unresolved"},
    ])
    table = _build(tmp_path, _manifest(), None)

    binder_entries = [e for e in table["api_entries"] if e["kind"] == "binder"]
    assert len(binder_entries) == 3
    by_class = {e["component_name"]: e for e in binder_entries}
    # exported 按 service_class 匹配 manifest 组件（JobService exported=true）
    assert by_class["com.example.JobService"]["exported"] is True
    assert by_class["com.example.Ambiguous"]["exported"] is None  # 匹配不到——不伪造
    assert by_class["com.example.JobService"]["reliability"] == "bound"
    assert by_class["com.example.Ambiguous"]["reliability"] == "ambiguous"
    assert by_class["com.example.NoImpl"]["reliability"] == "unresolved"
    assert by_class["com.example.JobService"]["transaction_code"] == 1
    assert by_class["com.example.JobService"]["source"] == "rule_artifact:binder_bindings"
    _validate_schema(table)

    # include_binder=false 时不生成
    table_off = _build(tmp_path, _manifest(), None, include_binder=False)
    assert not [e for e in table_off["api_entries"] if e["kind"] == "binder"]


def test_dynrcv_entries_from_artifact(tmp_path: Path) -> None:
    _write_artifact(tmp_path, "receiver_registrations", "registrations", [
        {"receiver_class": "com.example.BootReceiver", "path": "com/example/BootReceiver.java",
         "line": 12, "method_name": "register", "actions": ["android.intent.action.BOOT_COMPLETED"],
         "export_status": "exported", "externally_reachable": True, "reportable": True},
        {"receiver_class": None, "path": "com/example/Opaque.java", "line": 5,
         "method_name": "init", "actions": [], "export_status": "legacy_unspecified",
         "externally_reachable": None, "reportable": False},
    ])
    reader = _index_reader(tmp_path, {"com/example/BootReceiver.java": _RECEIVER_SOURCE})
    table = _build(tmp_path, _manifest(), reader)

    dynrcv = [e for e in table["api_entries"] if e["source"] == "rule_artifact:receiver_registrations"]
    assert len(dynrcv) == 2
    assert all(e["kind"] == "receiver" for e in dynrcv)  # R-9：kind 显式
    boot = next(e for e in dynrcv if e["component_name"] == "com.example.BootReceiver")
    # D2：receiver_class 走 lifecycle 解析（onReceive）
    assert boot["entry_method"] is not None and boot["entry_method"].startswith("onReceive(")
    assert boot["actions"] == ["android.intent.action.BOOT_COMPLETED"]
    assert boot["export_status"] == "exported"
    assert boot["externally_reachable"] is True
    # receiver_class=None → component_name 兜底 path 类名；legacy → unknown（转换层职责）
    opaque = next(e for e in dynrcv if e["entry_method"] is None)
    assert opaque["component_name"] == "com.example.Opaque"
    assert opaque["export_status"] == "unknown"
    _validate_schema(table)


def test_webview_entries_from_artifact(tmp_path: Path) -> None:
    _write_artifact(tmp_path, "webview_js_bridges", "bridges", [
        {"path": "com/example/WebHelper.java", "line": 8,
         "text": 'addJavascriptInterface(new JsBridge(), "Android")',
         "sink_kind": "js_bridge", "bridge_name": "Android",
         "description": "JS 桥注入"},
        {"path": "sources/com/example/Other.java", "line": 9,
         "text": 'addJavascriptInterface(b, "B2")', "sink_kind": "js_bridge",
         "bridge_name": "B2", "description": "d"},
    ])
    table = _build(tmp_path, _manifest(), None)

    webview = [e for e in table["api_entries"] if e["kind"] == "webview_bridge"]
    assert len(webview) == 2
    by_name = {e["bridge_name"]: e for e in webview}
    # R-7：条件式剥离 sources/ 前缀；component_name=注册调用类 FQCN
    assert by_name["Android"]["component_name"] == "com.example.WebHelper"
    assert by_name["B2"]["component_name"] == "com.example.Other"
    assert by_name["Android"]["bridge_line"] == 8
    assert by_name["Android"]["bridge_path"] == "com/example/WebHelper.java"
    assert by_name["Android"]["reliability"] == "not_applicable"
    _validate_schema(table)

    table_off = _build(tmp_path, _manifest(), None, include_webview_jsbridge=False)
    assert not [e for e in table_off["api_entries"] if e["kind"] == "webview_bridge"]


# ---------------------------------------------------------------------------
# 容错与 entry_id（A-6/A-7/N-3 + R-5/R-6）
# ---------------------------------------------------------------------------


def test_missing_and_empty_artifacts_tolerated(tmp_path: Path) -> None:
    """三文件全缺 + 空数组文件两形态均容错（R-5：T2.1 空键也写盘）。"""
    table = _build(tmp_path, _manifest(), None)  # 无 rule-results 目录
    assert not [e for e in table["api_entries"] if e["source"].startswith("rule_artifact:")]

    _write_artifact(tmp_path, "binder_bindings", "bindings", [])
    table_empty = _build(tmp_path, _manifest(), None)
    assert not [e for e in table_empty["api_entries"] if e["kind"] == "binder"]
    _validate_schema(table_empty)


def test_corrupted_artifact_envelope(tmp_path: Path) -> None:
    """N-3：信封结构错误（entry_key 不符）容错空记录。"""
    path = tmp_path / "rule-results" / "binder_bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "1.0.0", "wrong_key": [{"x": 1}]}), "utf-8")
    broken = tmp_path / "rule-results" / "receiver_registrations.json"
    broken.write_text("{ not json", "utf-8")

    table = _build(tmp_path, _manifest(), None)
    assert not [e for e in table["api_entries"] if e["source"].startswith("rule_artifact:")]
    _validate_schema(table)


def test_entry_id_pattern_and_dedup(tmp_path: Path) -> None:
    """N-4/N-5：内部类 $ 转换 + `__2` 双下划线去重（防与合法方法名 _2 撞车）。"""
    manifest = {
        "package": "com.example",
        "components": [
            {"kind": "activity", "name": "com.example.MainActivity$Inner",
             "exported": "true", "exported_reason": None, "permission": None,
             "intent_filters": None, "authorities": None},
        ],
    }
    reader = _index_reader(tmp_path, {
        "com/example/MainActivity$Inner.java": (
            "package com.example;\npublic class MainActivity$Inner {\n"
            "  protected void onCreate(android.os.Bundle b) {\n  }\n"
            "  protected void onCreate_2(android.os.Bundle b) {\n  }\n"
            "  protected void onCreate(android.os.Intent i) {\n  }\n"  # 同名重载（第二个 onCreate）
        ),
    })
    table = _build(tmp_path, manifest, reader)

    ids = [e["entry_id"] for e in table["api_entries"]]
    assert all(
        e["entry_id"].startswith(("act_", "svc_", "rcv_", "prv_"))
        for e in table["api_entries"]
    )
    # 内部类 $ → _（pattern 合法）
    assert any("MainActivity_Inner" in entry_id for entry_id in ids)
    # onCreate 重载去重：`__2` 双下划线后缀（防与合法方法名 `_2` 撞车——
    # 白名单固定名下真实撞车不可发生，此为防御性验证）
    on_create_ids = [entry_id for entry_id in ids if "onCreate" in entry_id]
    assert len(on_create_ids) == 2  # onCreate / onCreate__2（重载各一条）
    assert any(entry_id.endswith("onCreate__2") for entry_id in ids)
    # onCreate_2 非白名单方法——不产生 entry（lifecycle 白名单语义）
    assert not any(entry_id.endswith("onCreate_2") for entry_id in ids)
    _validate_schema(table)


def test_empty_components(tmp_path: Path) -> None:
    """N-1：空组件清单 → 空表合法。"""
    table = _build(tmp_path, {"package": "com.example", "components": []}, None)
    assert table["api_entries"] == []
    _validate_schema(table)


# ---------------------------------------------------------------------------
# 集成（A-8：api_surface 阶段 + manifest-only 断言）
# ---------------------------------------------------------------------------


def _apk_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", "<manifest/>")
        archive.writestr("classes.dex", b"dex\n035\x00")
    return buffer.getvalue()


def test_orchestrator_api_surface_stage(tmp_path: Path) -> None:
    """api_surface.enabled=True + source=false：manifest-only 产物生成 + 阶段记录。"""
    settings = Settings(
        database_path=tmp_path / "tracer.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "data"),
        source_analysis=SourceAnalysisSettings(enabled=False),
        api_surface=ApiSurfaceSettings(enabled=True),
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/runs",
            files={"file": ("sample.apk", _apk_bytes(), "application/vnd.android.package-archive")},
            data={"authorized": "true", "source_analysis_enabled": "false"},
        )
        run_id = response.json()["id"]
        run = client.get(f"/api/runs/{run_id}").json()

        assert run["status"] == "completed"
        storage = client.app.state.storage
        manifest = storage.read_manifest(run_id)
        artifacts = [item for item in manifest.get("artifacts", []) if item["type"] == "api_entry_table"]
        assert artifacts and artifacts[0]["entry_count"] >= 0
        # A-8 补充（R-9）：manifest-only 下规则产物入口为空
        table = json.loads(
            (storage.run_dir(run_id) / "api-surface" / "api_entry_table.json").read_text("utf-8")
        )
        assert not [e for e in table["api_entries"] if e["source"].startswith("rule_artifact:")]
        stages = [stage for stage in manifest.get("stages", []) if stage["name"] == "api_surface"]
        assert stages and stages[0]["status"] == "completed"
