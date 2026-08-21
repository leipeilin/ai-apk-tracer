"""资产/批量 API 端点测试（T1.4）。

设计：docs/analysis/2026-08-22-t1-4-implementation-plan.md
（含评审 R-1~R-6 修订：data_root 显式 tmp 隔离 / _public_batch 剔除
assets_json / 恶意文件名负例）。API 层只验协议；编排逻辑由
test_batch.py 覆盖（run_batch 以 no-op 替身防真实 decompile 重执行）。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import AssetsSettings, Settings, SourceAnalysisSettings, StorageSettings
from app.main import create_app

APK_CONTENT_TYPE = "application/vnd.android.package-archive"


def client_for(tmp_path: Path, *, enabled: bool = True) -> TestClient:
    """构造测试客户端（assets.data_root 显式指向 tmp，评审 R-2：防工作区污染）。"""
    settings = Settings(
        database_path=tmp_path / "tracer.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "data"),
        source_analysis=SourceAnalysisSettings(enabled=False),
        assets=AssetsSettings(enabled=enabled, data_root=tmp_path / "assets"),
    )
    return TestClient(create_app(settings))


def apk_payload(seed: str = "demo") -> bytes:
    """构造最小合法 APK ZIP 字节流。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", f"<manifest package='{seed}'/>".encode())
        archive.writestr("classes.dex", b"dex\n035\x00" + seed.encode())
    return buffer.getvalue()


def import_asset(client: TestClient, seed: str = "demo", **overrides) -> dict:
    data = {"package_name": f"com.example.{seed}", "authorized": "true"}
    data.update(overrides)
    response = client.post(
        "/api/assets/import",
        files={"file": (f"{seed}.apk", apk_payload(seed), APK_CONTENT_TYPE)},
        data=data,
    )
    assert response.status_code == 201, response.text
    return response.json()


def stub_run_batch(app) -> list[str]:
    """将 run_batch 替换为记录调用的 no-op（API 层验协议，编排归 test_batch.py）。"""
    scheduled: list[str] = []

    async def fake_run_batch(batch_id: str) -> None:
        scheduled.append(batch_id)

    app.state.batch_orchestrator.run_batch = fake_run_batch
    return scheduled


# ----------------------------------------------------------------------
# 门禁（A-1 / N-1）
# ----------------------------------------------------------------------


def test_assets_endpoints_disabled_by_default(tmp_path: Path) -> None:
    """默认 enabled=False：四端点全部 503 ASSETS_DISABLED（安全默认回归）。"""
    with client_for(tmp_path, enabled=False) as client:
        assert client.get("/api/assets").status_code == 503
        assert client.get("/api/assets").json()["error"]["code"] == "ASSETS_DISABLED"
        # N-1：门禁先于授权判定（authorized=true 仍拒绝）
        response = client.post(
            "/api/assets/import",
            files={"file": ("a.apk", apk_payload(), APK_CONTENT_TYPE)},
            data={"package_name": "com.example.x", "authorized": "true"},
        )
        assert response.status_code == 503
        assert client.post("/api/batches", json={"authorized": True, "asset_ids": ["a"]}).status_code == 503
        assert client.get("/api/batches/any").status_code == 503


# ----------------------------------------------------------------------
# 导入（A-2~A-5 / N-2）
# ----------------------------------------------------------------------


def test_import_asset_roundtrip(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        asset = import_asset(client)

        # 脱敏：无 apk_path（T1.2 评审遗留落地）
        assert "apk_path" not in asset
        assert asset["package_name"] == "com.example.demo"
        assert asset["status"] == "ready"
        assert asset["source"] == "local_upload"
        assert len(asset["apk_sha256"]) == 64

        items = client.get("/api/assets").json()["items"]
        assert [item["id"] for item in items] == [asset["id"]]
        assert all("apk_path" not in item for item in items)


def test_import_requires_authorization(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/assets/import",
            files={"file": ("a.apk", apk_payload(), APK_CONTENT_TYPE)},
            data={"package_name": "com.example.x", "authorized": "false"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "AUTHORIZATION_CONFIRMATION_REQUIRED"
        # 未落库
        assert client.get("/api/assets").json()["items"] == []


def test_import_duplicate_conflict(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        first = import_asset(client, "demo")
        response = client.post(
            "/api/assets/import",
            files={"file": ("copy.apk", apk_payload("demo"), APK_CONTENT_TYPE)},
            data={"package_name": "com.example.another", "authorized": "true"},
        )
        assert response.status_code == 409
        payload = response.json()["error"]
        assert payload["code"] == "ASSET_ALREADY_REGISTERED"
        assert payload["details"]["asset_id"] == first["id"]


def test_import_rejects_invalid_inputs(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        base = {"authorized": "true"}

        # 空 package_name
        response = client.post(
            "/api/assets/import",
            files={"file": ("a.apk", apk_payload(), APK_CONTENT_TYPE)},
            data={"package_name": "  ", **base},
        )
        assert response.status_code == 422

        # 非 .apk 扩展名
        response = client.post(
            "/api/assets/import",
            files={"file": ("a.txt", apk_payload(), APK_CONTENT_TYPE)},
            data={"package_name": "com.example.x", **base},
        )
        assert response.json()["error"]["code"] == "INVALID_APK_EXTENSION"

        # 路径穿越文件名（评审 R-3：公开上传端点安全负例）
        response = client.post(
            "/api/assets/import",
            files={"file": ("../../evil.apk", apk_payload(), APK_CONTENT_TYPE)},
            data={"package_name": "com.example.x", **base},
        )
        assert response.json()["error"]["code"] == "INVALID_APK_FILENAME"

        # 非 ZIP 内容
        response = client.post(
            "/api/assets/import",
            files={"file": ("b.apk", b"not a zip", APK_CONTENT_TYPE)},
            data={"package_name": "com.example.x", **base},
        )
        assert response.status_code == 422

        assert client.get("/api/assets").json()["items"] == []


def test_import_requires_file_field(tmp_path: Path) -> None:
    """N-2：缺 file 字段 → 请求模型校验 422。"""
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/assets/import",
            data={"package_name": "com.example.x", "authorized": "true"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"


# ----------------------------------------------------------------------
# 批量（A-6~A-10 / N-3~N-4）
# ----------------------------------------------------------------------


def test_create_batch_returns_pending(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        scheduled = stub_run_batch(client.app)
        asset = import_asset(client)

        response = client.post(
            "/api/batches", json={"authorized": True, "asset_ids": [asset["id"]]}
        )
        assert response.status_code == 202, response.text
        batch = response.json()
        assert batch["status"] == "pending"
        assert batch["assets"] == [
            {
                "asset_id": asset["id"],
                "package_name": asset["package_name"],
                "apk_sha256": asset["apk_sha256"],
            }
        ]
        # 评审 R-1：响应无 assets_json 原始列
        assert "assets_json" not in batch
        assert batch["ai_skipped_count"] == 0
        # run_batch 已被调度（background task 执行 no-op 替身）
        assert scheduled == [batch["id"]]


def test_create_batch_requires_authorization(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        stub_run_batch(client.app)
        response = client.post(
            "/api/batches", json={"authorized": False, "asset_ids": ["any"]}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "AUTHORIZATION_CONFIRMATION_REQUIRED"


def test_create_batch_missing_asset(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/batches", json={"authorized": True, "asset_ids": ["missing_asset"]}
        )
        assert response.status_code == 404


def test_get_batch_summary(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        stub_run_batch(client.app)
        asset = import_asset(client)
        created = client.post(
            "/api/batches", json={"authorized": True, "asset_ids": [asset["id"]]}
        ).json()

        fetched = client.get(f"/api/batches/{created['id']}").json()
        assert fetched["id"] == created["id"]
        assert fetched["total_runs"] == 0
        assert fetched["completed_runs"] == 0 and fetched["failed_runs"] == 0
        assert fetched["ai_skipped"] == 0
        assert "assets_json" not in fetched  # 评审 R-1

        assert client.get("/api/batches/missing").status_code == 404
        # N-4：注入样例安全返回 404（参数绑定，机制保证）
        assert client.get("/api/batches/x'; DROP TABLE batches;--").status_code == 404


def test_batch_request_model_validation(tmp_path: Path) -> None:
    """N-3/A-10：asset_ids 空列表 / 缺字段 → REQUEST_VALIDATION_ERROR。"""
    with client_for(tmp_path) as client:
        stub_run_batch(client.app)
        response = client.post("/api/batches", json={"authorized": True, "asset_ids": []})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"

        response = client.post("/api/batches", json={"asset_ids": ["a"]})
        assert response.status_code == 422
