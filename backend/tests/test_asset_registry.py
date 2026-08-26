"""资产注册表测试（T1.2）。

设计：docs/analysis/explorer-track/2026-08-22-t1-2-implementation-plan.md（含评审 R-1~R-8 修订）；
关键断言：重复注册冲突保留副本（R-1）、limits 三参数同源（R-2）、
link_run 前置 run 存在（R-4）、删除复用 safe_remove_tree（R-4）。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.assets.registry import AssetRegistry
from app.config import StorageSettings
from app.runs.storage import RunStorage
from app.shared.errors import ConflictError, NotFoundError, ValidationError
from app.shared.repository import SQLiteRepository


def _apk_bytes() -> bytes:
    """构造最小合法 APK ZIP 字节流（通过 validate_apk_zip 结构校验）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00manifest")
        zf.writestr("classes.dex", b"dex\n035\x00placeholder")
    return buffer.getvalue()


def _make_registry(tmp_path: Path, storage_settings: StorageSettings | None = None) -> tuple[AssetRegistry, SQLiteRepository]:
    repository = SQLiteRepository(tmp_path / "tracer.sqlite3")
    repository.initialize()
    storage = RunStorage(tmp_path / "data", storage_settings or StorageSettings())
    registry = AssetRegistry(repository, storage, tmp_path / "data" / "assets")
    return registry, repository


def _register_asset(registry: AssetRegistry, package_name: str = "com.example.demo") -> dict:
    return registry.register(io.BytesIO(_apk_bytes()), "demo.apk", package_name)


def test_register_persists_asset_and_copy(tmp_path: Path) -> None:
    registry, _ = _make_registry(tmp_path)
    asset = _register_asset(registry)

    assert asset["package_name"] == "com.example.demo"
    assert asset["apk_filename"] == "demo.apk"
    assert asset["source"] == "local_upload"
    assert asset["status"] == "ready"
    assert asset["last_run_id"] is None
    assert asset["apk_sha256"] and len(asset["apk_sha256"]) == 64
    # 副本内容寻址落位且内容一致
    copy_path = Path(asset["apk_path"])
    assert copy_path.read_bytes() == _apk_bytes()
    assert copy_path.parent.name == asset["apk_sha256"]
    assert copy_path.parent.parent.name == asset["apk_sha256"][:2]


def test_register_duplicate_sha256_conflict(tmp_path: Path) -> None:
    registry, _ = _make_registry(tmp_path)
    first = _register_asset(registry)

    with pytest.raises(ConflictError) as error:
        _register_asset(registry, package_name="com.example.another")
    assert error.value.details["asset_id"] == first["id"]
    assert error.value.details["apk_sha256"] == first["apk_sha256"]
    # 评审 R-1 断言补强：冲突不清副本——既有副本仍存在且内容一致
    assert Path(first["apk_path"]).read_bytes() == _apk_bytes()
    assert len(registry.list_assets()) == 1


def test_register_rejects_oversize(tmp_path: Path) -> None:
    registry, _ = _make_registry(tmp_path, StorageSettings(max_apk_size_mb=0))
    with pytest.raises(ValidationError) as error:
        registry.register(io.BytesIO(_apk_bytes()), "big.apk", "com.example.big")
    assert error.value.code == "APK_TOO_LARGE"
    # 临时文件无残留（assets_root 下仅无 .incoming-*）
    assert not list((tmp_path / "data" / "assets").glob(".incoming-*"))


def test_register_rejects_non_zip(tmp_path: Path) -> None:
    registry, _ = _make_registry(tmp_path)
    with pytest.raises(ValidationError):
        registry.register(io.BytesIO(b"this is not a zip archive"), "plain.apk", "com.example.x")
    assert not list((tmp_path / "data" / "assets").glob(".incoming-*"))


def test_register_rejects_bad_inputs(tmp_path: Path) -> None:
    registry, _ = _make_registry(tmp_path)
    payload = _apk_bytes()

    # N-1：空 package_name
    with pytest.raises(ValidationError) as error:
        registry.register(io.BytesIO(payload), "a.apk", "")
    assert error.value.code == "PACKAGE_NAME_REQUIRED"
    with pytest.raises(ValidationError):
        registry.register(io.BytesIO(payload), "a.apk", "   ")

    # N-2：路径穿越 / 非法文件名
    with pytest.raises(ValidationError):
        registry.register(io.BytesIO(payload), "../../evil.apk", "com.example.x")
    with pytest.raises(ValidationError):
        registry.register(io.BytesIO(payload), "..", "com.example.x")

    # 非 .apk 扩展名（复用 storage 先例错误码）
    with pytest.raises(ValidationError) as error:
        registry.register(io.BytesIO(payload), "a.txt", "com.example.x")
    assert error.value.code == "INVALID_APK_EXTENSION"

    # N-5：0 字节流（zip 魔数缺失）
    with pytest.raises(ValidationError):
        registry.register(io.BytesIO(b""), "empty.apk", "com.example.x")


def test_get_not_found(tmp_path: Path) -> None:
    registry, _ = _make_registry(tmp_path)
    with pytest.raises(NotFoundError):
        registry.get("missing_asset")


def test_list_assets_filters_by_status(tmp_path: Path) -> None:
    registry, _ = _make_registry(tmp_path)
    first = _register_asset(registry, "com.example.first")
    second = registry.register(io.BytesIO(_apk_bytes() + b"\x00"), "second.apk", "com.example.second")
    registry.update_status(first["id"], "error")

    all_assets = registry.list_assets()
    assert [a["id"] for a in all_assets] == [second["id"], first["id"]]  # created_at DESC
    ready_only = registry.list_assets(status="ready")
    assert [a["id"] for a in ready_only] == [second["id"]]
    error_only = registry.list_assets(status="error")
    assert [a["id"] for a in error_only] == [first["id"]]


def test_update_status_whitelist(tmp_path: Path) -> None:
    registry, _ = _make_registry(tmp_path)
    asset = _register_asset(registry)

    updated = registry.update_status(asset["id"], "scanning")
    assert updated["status"] == "scanning"

    with pytest.raises(ValidationError) as error:
        registry.update_status(asset["id"], "archived")
    assert error.value.code == "INVALID_ASSET_STATUS"

    with pytest.raises(NotFoundError):
        registry.update_status("missing_asset", "ready")


def test_link_run_updates_last_run_id(tmp_path: Path) -> None:
    registry, repository = _make_registry(tmp_path)
    asset = _register_asset(registry)
    repository.create_run(
        {
            "id": "run_link",
            "trace_id": "t",
            "status": "completed",
            "stage": "completed",
            "apk_filename": "demo.apk",
            "apk_sha256": asset["apk_sha256"],
            "config": {},
            "manifest_path": "/tmp/m.json",
        }
    )

    linked = registry.link_run(asset["id"], "run_link")
    assert linked["last_run_id"] == "run_link"

    # run 不存在 → NotFoundError（评审 R-4，FK 裸异常不逃逸）
    with pytest.raises(NotFoundError):
        registry.link_run(asset["id"], "missing_run")

    # 删除 run → last_run_id 置 NULL（T1.1 FK ON DELETE SET NULL 联动）
    repository.delete_run_record("run_link")
    assert registry.get(asset["id"])["last_run_id"] is None


def test_delete_removes_record_and_copy(tmp_path: Path) -> None:
    registry, _ = _make_registry(tmp_path)
    asset = _register_asset(registry)
    copy_dir = Path(asset["apk_path"]).parent

    registry.delete(asset["id"])

    with pytest.raises(NotFoundError):
        registry.get(asset["id"])
    assert not copy_dir.exists()
    with pytest.raises(NotFoundError):
        registry.delete(asset["id"])


def test_sql_injection_safety(tmp_path: Path) -> None:
    """参数绑定实证（机制保证的防退化断言，评审 R-8 性质说明）。"""
    registry, _ = _make_registry(tmp_path)
    injection_ids = [
        "a'; DROP TABLE assets;--",
        'x" OR 1=1--',
        "missing\" UNION SELECT * FROM runs--",
    ]
    for bad_id in injection_ids:
        with pytest.raises(NotFoundError):
            registry.get(bad_id)
    # 注入未破坏表结构
    _register_asset(registry)
    assert len(registry.list_assets()) == 1
