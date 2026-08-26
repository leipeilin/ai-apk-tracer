"""四组件攻击面导出测试（T2.3）。

设计：docs/analysis/explorer-track/2026-08-22-t2-3-implementation-plan.md（含评审 R-1~R-7
修订）。夹具策略（评审 R-4）：手写 rule-results 产物 + **真实
build_api_entry_table 生成** entry_table——消手写漂移（entry_method 复用
name(params)->return 实际格式、exported 映射与 T2.2 同源）。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import jsonschema
from fastapi.testclient import TestClient

from app.analysis.api_surface import build_api_entry_table
from app.analysis.attack_surface import build_attack_surfaces
from app.config import (
    ApiSurfaceSettings,
    Settings,
    SourceAnalysisSettings,
    StorageSettings,
)
from app.main import create_app

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
_SETTINGS = ApiSurfaceSettings(enabled=True)


def _manifest() -> dict:
    return {
        "package": "com.example",
        "components": [
            {
                "kind": "activity", "name": "com.example.SplashActivity",
                "exported": "conditional", "exported_reason": "intent_filter_default",
                "permission": None, "permission_protection": None,
                "intent_filters": [{"actions": ["android.intent.action.VIEW"]}],
            },
            {
                "kind": "service", "name": "com.example.JobService",
                "exported": "true", "exported_reason": "manifest_true",
                "permission": "com.example.PERM", "permission_protection": "dangerous",
                "intent_filters": None,
            },
            {
                "kind": "provider", "name": "com.example.FileProvider",
                "exported": "false", "exported_reason": "manifest_false",
                "permission": None, "permission_protection": None,
                "intent_filters": None, "authorities": ["com.example.files"],
                "read_permission": "com.example.READ", "write_permission": None,
            },
            {
                "kind": "receiver", "name": "com.example.BootReceiver",
                "exported": "false", "exported_reason": "manifest_false",
                "permission": None, "permission_protection": None,
                "intent_filters": [{"actions": ["android.intent.action.BOOT_COMPLETED"]}],
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


def _prepare(run_dir: Path, registrations: list[dict]) -> dict[str, dict]:
    """手写 rule-results + 真实生成器产出 entry_table → 四攻击面文件。"""
    _write_artifact(run_dir, "receiver_registrations", "registrations", registrations)
    entry_table = build_api_entry_table(run_dir, _manifest(), _SETTINGS, reader=None)
    entry_path = run_dir / "api-surface" / "api_entry_table.json"
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(json.dumps(entry_table, ensure_ascii=False), "utf-8")
    return build_attack_surfaces(run_dir, _manifest(), _candidates())


def _candidates() -> list[dict]:
    return [
        {"rule_id": "ACTIVITY_EXPORTED_NO_PERMISSION", "component_name": "com.example.SplashActivity"},
        {"rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK", "component_name": "com.example.SplashActivity"},
        {"rule_id": "RECEIVER_EXPORTED_NO_PERMISSION", "component_name": "com.example.BootReceiver"},
        {"rule_id": "WEBVIEW_FILE_ACCESS_ENABLED", "component_name": "dynamic:com/example/WebHelper.java"},
        {"rule_id": "ACTIVITY_SENSITIVE_NAME_HINT", "component_name": "com.example.SplashActivity"},  # auxiliary 含入（R-7）
    ]


def _validate(payload: dict) -> None:
    schema = json.loads((SCHEMAS_DIR / "attack_surface.schema.json").read_text("utf-8"))
    jsonschema.validate(payload, schema)


# ---------------------------------------------------------------------------
# A-1/A-2/A-5：组件字段与保守导出 + 能力聚合
# ---------------------------------------------------------------------------


def test_activity_surface_fields(tmp_path: Path) -> None:
    surfaces = _prepare(tmp_path, [])
    _validate(surfaces["activity"])
    splash = next(c for c in surfaces["activity"]["components"] if c["name"] == "com.example.SplashActivity")
    # D2 保守高估：conditional → True + reason 透传
    assert splash["exported"] is True
    assert splash["exported_reason"] == "intent_filter_default"
    assert splash["intent_filters"] == [{"actions": ["android.intent.action.VIEW"]}]
    # A-2 能力聚合：组件级 3 条命中（含 auxiliary）；全局规则不入（D3）
    assert splash["sensitive_capabilities"] == [
        "ACTIVITY_EXPORTED_NO_PERMISSION",
        "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "ACTIVITY_SENSITIVE_NAME_HINT",
    ]
    # refs/entry_methods 来自真实生成器（manifest 条目）
    assert splash["api_entry_refs"] == ["act_com_example_SplashActivity"]
    assert splash["entry_methods"] == []
    assert splash["source"] == "manifest"


def test_service_and_provider_fields(tmp_path: Path) -> None:
    surfaces = _prepare(tmp_path, [])
    service = next(c for c in surfaces["service"]["components"] if c["name"] == "com.example.JobService")
    assert service["exported"] is True
    assert service["permission"] == "com.example.PERM"
    assert service["permission_protection"] == "dangerous"
    # R-6：provider 读写权限透传 + authorities
    provider = next(c for c in surfaces["provider"]["components"] if c["name"] == "com.example.FileProvider")
    assert provider["authorities"] == ["com.example.files"]
    assert provider["read_permission"] == "com.example.READ"
    assert provider["write_permission"] is None
    assert provider["exported"] is False
    _validate(surfaces["service"])
    _validate(surfaces["provider"])


# ---------------------------------------------------------------------------
# A-3/A-4 + R-1：receiver 合并与动态分支
# ---------------------------------------------------------------------------


def test_receiver_merge_manifest_and_dynamic(tmp_path: Path) -> None:
    registrations = [
        {"receiver_class": "com.example.BootReceiver", "path": "com/example/BootReceiver.java",
         "line": 12, "method_name": "register",
         "actions": ["com.example.SYNC"], "export_status": "exported",
         "externally_reachable": True, "reportable": True},
    ]
    surfaces = _prepare(tmp_path, registrations)
    _validate(surfaces["receiver"])
    boot = next(c for c in surfaces["receiver"]["components"] if c["name"] == "com.example.BootReceiver")
    assert boot["source"] == "manifest+dynamic"
    # exported OR 合并：静态 false + 动态 reachable=True → True
    assert boot["exported"] is True
    # R-5：reason 组合标注（动态来源可回溯）
    assert boot["exported_reason"].startswith("static:manifest_false;dynamic:")
    # actions 并集（静态 BOOT_COMPLETED + 动态 SYNC）
    assert set(boot["actions"] or []) == {"android.intent.action.BOOT_COMPLETED", "com.example.SYNC"}
    # refs 并集（静态 rcv + 动态 dynrcv）
    assert "rcv_com_example_BootReceiver" in boot["api_entry_refs"]
    assert "dynrcv_com_example_BootReceiver_register" in boot["api_entry_refs"]
    assert boot["dynamic_registrations"] == [{"export_status": "exported", "externally_reachable": True}]
    assert boot["sensitive_capabilities"] == ["RECEIVER_EXPORTED_NO_PERMISSION"]


def test_receiver_merge_with_dynamic_unknown(tmp_path: Path) -> None:
    """R-1：静态 false + 动态 None（未知）→ True（保守统一）。"""
    registrations = [
        {"receiver_class": "com.example.BootReceiver", "path": "p", "line": 1,
         "actions": [], "export_status": "unknown", "externally_reachable": None},
    ]
    surfaces = _prepare(tmp_path, registrations)
    boot = next(c for c in surfaces["receiver"]["components"] if c["name"] == "com.example.BootReceiver")
    assert boot["exported"] is True  # 动态未知 → 保守 True


def test_dynamic_only_receiver(tmp_path: Path) -> None:
    """A-4：纯动态 receiver 三分支（True/False/None→True，R-1）。"""
    registrations = [
        {"receiver_class": "com.example.SmsReceiver", "path": "com/example/SmsReceiver.java",
         "line": 5, "method_name": "init", "actions": ["android.provider.Telephony.SMS_RECEIVED"],
         "export_status": "exported", "externally_reachable": True},
        {"receiver_class": "com.example.LocalReceiver", "path": "com/example/LocalReceiver.java",
         "line": 6, "method_name": "init", "actions": [], "export_status": "not_exported",
         "externally_reachable": False},
        {"receiver_class": "com.example.OpaqueReceiver", "path": "com/example/Opaque.java",
         "line": 7, "method_name": "init", "actions": [], "export_status": "unknown",
         "externally_reachable": None},
    ]
    surfaces = _prepare(tmp_path, registrations)
    _validate(surfaces["receiver"])
    by_name = {c["name"]: c for c in surfaces["receiver"]["components"]}
    assert by_name["com.example.BootReceiver"]["source"] == "manifest"  # 静态无动态
    assert by_name["com.example.SmsReceiver"]["source"] == "dynamic"
    assert by_name["com.example.SmsReceiver"]["exported"] is True
    assert by_name["com.example.LocalReceiver"]["exported"] is False
    assert by_name["com.example.OpaqueReceiver"]["exported"] is True  # None → 保守 True（R-1）
    assert by_name["com.example.SmsReceiver"]["api_entry_refs"] == ["dynrcv_com_example_SmsReceiver_init"]
    assert by_name["com.example.OpaqueReceiver"]["sensitive_capabilities"] == []


# ---------------------------------------------------------------------------
# A-6/A-7：空类型文件与容错
# ---------------------------------------------------------------------------


def test_empty_kind_file(tmp_path: Path) -> None:
    manifest = {"package": "com.example", "components": [
        {"kind": "activity", "name": "com.example.Only", "exported": "true",
         "exported_reason": None, "permission": None, "permission_protection": None,
         "intent_filters": None},
    ]}
    run_dir = tmp_path
    entry_table = build_api_entry_table(run_dir, manifest, _SETTINGS, reader=None)
    (run_dir / "api-surface").mkdir(parents=True, exist_ok=True)
    (run_dir / "api-surface" / "api_entry_table.json").write_text(json.dumps(entry_table), "utf-8")
    surfaces = build_attack_surfaces(run_dir, manifest, [])
    assert set(surfaces.keys()) == {"activity", "service", "provider", "receiver"}
    assert surfaces["provider"]["components"] == []  # D5：恒生成
    _validate(surfaces["provider"])


def test_missing_entry_table_tolerated(tmp_path: Path) -> None:
    """A-7：api_entry_table 缺失 → refs/entry_methods 空（阶段不挂）。"""
    surfaces = build_attack_surfaces(tmp_path, _manifest(), _candidates())
    splash = next(c for c in surfaces["activity"]["components"] if c["name"] == "com.example.SplashActivity")
    assert splash["api_entry_refs"] == []
    assert splash["entry_methods"] == []
    assert splash["sensitive_capabilities"]  # 能力聚合不依赖 entry_table
    _validate(surfaces["activity"])


def test_corrupted_entry_table(tmp_path: Path) -> None:
    """N-3：entry_table 损坏 JSON 容错。"""
    entry_path = tmp_path / "api-surface" / "api_entry_table.json"
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text("{ not json", "utf-8")
    surfaces = build_attack_surfaces(tmp_path, _manifest(), [])
    _validate(surfaces["activity"])


# ---------------------------------------------------------------------------
# A-8：集成
# ---------------------------------------------------------------------------


def _apk_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", "<manifest/>")
        archive.writestr("classes.dex", b"dex\n035\x00")
    return buffer.getvalue()


def test_orchestrator_attack_surface_stage(tmp_path: Path) -> None:
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
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "completed"

        storage = client.app.state.storage
        run_manifest = storage.read_manifest(run_id)
        surface_artifacts = [
            item for item in run_manifest.get("artifacts", [])
            if item["type"] == "attack_surface"
        ]
        assert {item["component_kind"] for item in surface_artifacts} == {"activity", "service", "provider", "receiver"}
        for kind in ("activity", "service", "provider", "receiver"):
            payload = json.loads(
                (storage.run_dir(run_id) / "attack_surface" / f"{kind}.json").read_text("utf-8")
            )
            assert payload["schema_version"] == "1.0.0"
        stages = [stage for stage in run_manifest.get("stages", []) if stage["name"] == "attack_surface"]
        assert stages and stages[0]["status"] == "completed"
