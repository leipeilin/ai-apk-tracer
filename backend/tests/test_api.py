from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings, SourceAnalysisSettings, StorageSettings
from app.main import create_app


def client_for(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_path=tmp_path / "tracer.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "data"),
        source_analysis=SourceAnalysisSettings(enabled=False),
    )
    return TestClient(create_app(settings))


def apk_payload() -> bytes:
    buffer = io.BytesIO()
    manifest = """<manifest xmlns:android='http://schemas.android.com/apk/res/android'
        package='com.example' android:versionCode='1' android:versionName='1.0'>
      <uses-sdk android:targetSdkVersion='35'/>
      <application android:label='Demo'>
        <activity android:name='.ExportedActivity' android:exported='true'>
          <intent-filter><action android:name='com.example.OPEN'/></intent-filter>
        </activity>
      </application>
    </manifest>"""
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("AndroidManifest.xml", manifest)
        archive.writestr("classes.dex", b"dex\n035\x00")
        archive.writestr("assets/lm-dict.dic", b"dictionary-entry\n" * 200_000)
    return buffer.getvalue()


def test_health_and_ready(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").status_code == 200


def test_upload_requires_authorization(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/runs",
            files={"file": ("sample.apk", apk_payload(), "application/vnd.android.package-archive")},
            data={"authorized": "false", "source_analysis_enabled": "false"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "AUTHORIZATION_CONFIRMATION_REQUIRED"


def test_unknown_api_path_returns_404_json(tmp_path: Path) -> None:
    """M1 审查 §4.2：未知 API 路径不落入 SPA catch-all（404 JSON 而非 200 HTML）。"""
    with client_for(tmp_path) as client:
        response = client.get("/api/definitely-not-an-endpoint")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        # dist 存在时为统一 AppError 结构；无 dist（干净 CI）时 FastAPI 默认 404 JSON
        payload = response.json()
        assert payload.get("error", {}).get("code") == "NOT_FOUND" or "detail" in payload


def test_unexpected_error_keeps_trace_id(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "trace.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "trace-data"),
        source_analysis=SourceAnalysisSettings(enabled=False),
    )
    app = create_app(settings)
    app.state.repository.ping = lambda: (_ for _ in ()).throw(RuntimeError("test failure"))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/ready", headers={"X-Trace-ID": "trace-regression-test"})
        assert response.status_code == 500
        assert response.json()["trace_id"] == "trace-regression-test"
        assert response.headers["X-Trace-ID"] == "trace-regression-test"


def test_upload_creates_run(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/runs",
            files={"file": ("sample.apk", apk_payload(), "application/vnd.android.package-archive")},
            data={"authorized": "true", "source_analysis_enabled": "false"},
        )
        assert response.status_code == 202
        payload = response.json()
        assert payload["filename"] == "sample.apk"
        assert payload["source_analysis_enabled"] is False
        run_response = client.get(f"/api/runs/{payload['id']}")
        assert run_response.status_code == 200
        run = run_response.json()
        assert run["status"] == "completed"
        assert run["package_name"] == "com.example"
        assert run["pipeline_version"] == "2.0.0"
        assert run["schema_version"] == "2.0.0"
        assert run["artifact_schema_versions"]["candidate"] == "2.0.0"
        findings = client.get(f"/api/runs/{payload['id']}/findings").json()["items"]
        assert any(item["evidence_level"] == "L1" for item in findings)


def test_get_run_returns_track_progress(tmp_path: Path) -> None:
    """track-progress-console：getRun 响应含双轨 progress 块（终态对账）。"""
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/runs",
            files={"file": ("sample.apk", apk_payload(), "application/vnd.android.package-archive")},
            data={"authorized": "true", "source_analysis_enabled": "false"},
        )
        run_id = response.json()["id"]
        run = client.get(f"/api/runs/{run_id}").json()
        progress = run["progress"]
        assert progress is not None
        rules = progress["rules"]
        assert rules is not None
        assert rules["total"] >= 1
        # 产物词干排除后（评审 R-2）：每条规则（含失败）恰一个 result 文件
        assert rules["processed"] == rules["total"]
        assert rules["failed"] is None or rules["failed"] <= rules["total"]
        # explorer 默认未启用 → 轨级 null（不伪造 0）
        assert progress["explorer"] is None


def test_upload_manifest_ai_summary_has_requests_used(tmp_path: Path) -> None:
    """真实 pipeline 的 ai_analysis 阶段 summary 含 requests_used（T1.3 batch 预算计数源）。"""
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/runs",
            files={"file": ("sample.apk", apk_payload(), "application/vnd.android.package-archive")},
            data={"authorized": "true", "source_analysis_enabled": "false"},
        )
        run_id = response.json()["id"]
        manifest = client.app.state.storage.read_manifest(run_id)
        ai_stages = [stage for stage in manifest["stages"] if stage["name"] == "ai_analysis"]
        assert ai_stages, "ai_analysis 阶段缺失"
        assert "requests_used" in ai_stages[0]["summary"]


def test_frontend_scoped_finding_id_reads_and_patches_matching_artifacts(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/runs",
            files={"file": ("sample.apk", apk_payload(), "application/vnd.android.package-archive")},
            data={"authorized": "true", "source_analysis_enabled": "false"},
        )
        run_id = response.json()["id"]
        finding = client.get(f"/api/runs/{run_id}/findings").json()["items"][0]
        scoped_id = finding["id"]
        base_id = finding["base_id"]
        run_dir = client.app.state.storage.run_dir(run_id)

        assert scoped_id == f"{run_id}_{base_id}"
        finding_path = run_dir / "findings" / f"{scoped_id}.json"
        evidence_path = run_dir / "reports" / "evidence" / f"{scoped_id}.json"
        assert finding_path.is_file()
        assert evidence_path.is_file()
        assert not (run_dir / "findings" / f"{base_id}.json").exists()

        review = client.patch(
            f"/api/findings/{scoped_id}/review",
            json={"status": "manual_false_positive", "reason": "前端人工复核"},
        )

        assert review.status_code == 200
        assert review.json()["id"] == scoped_id
        assert json.loads(finding_path.read_text("utf-8"))["review_status"] == "manual_false_positive"
        assert json.loads(evidence_path.read_text("utf-8"))["finding"]["id"] == scoped_id


def test_finding_slice_returns_latest_round(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/runs",
            files={"file": ("sample.apk", apk_payload(), "application/vnd.android.package-archive")},
            data={"authorized": "true", "source_analysis_enabled": "false"},
        )
        run_id = response.json()["id"]
        slice_id = "slice_" + "a" * 20
        finding = {
            "id": "finding_slice_test",
            "rule_ids": ["ACTIVITY_INTENT_TO_SENSITIVE_SINK"],
            "title": "slice test",
            "component": "activity",
            "component_name": "com.example.ExportedActivity",
            "severity": "medium",
            "confidence": "medium",
            "evidence_level": "L2",
            "slice_id": slice_id,
        }
        client.app.state.repository.replace_findings(run_id, [finding])
        slice_dir = client.app.state.storage.run_dir(run_id) / "slices" / slice_id
        slice_dir.mkdir(parents=True)
        (slice_dir / "round-000.json").write_text('{"contexts": []}', "utf-8")
        (slice_dir / "round-001.json").write_text('{"contexts": [{"context_id": "method-1"}]}', "utf-8")

        slice_response = client.get("/api/findings/finding_slice_test/slice")
        assert slice_response.status_code == 200
        value = slice_response.json()
        assert value["round_count"] == 2
        assert value["latest_round"] == "round-001.json"
        assert value["slice"]["contexts"][0]["context_id"] == "method-1"


def test_finding_slice_after_prune_falls_back_to_legacy_base_id_evidence(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/runs",
            files={"file": ("sample.apk", apk_payload(), "application/vnd.android.package-archive")},
            data={"authorized": "true", "source_analysis_enabled": "false"},
        )
        run_id = response.json()["id"]
        slice_id = "slice_" + "b" * 20
        finding = {
            "id": "finding_pruned_slice",
            "rule_ids": ["ACTIVITY_INTENT_TO_SENSITIVE_SINK"],
            "title": "pruned slice",
            "component": "activity",
            "component_name": "com.example.ExportedActivity",
            "severity": "medium",
            "confidence": "medium",
            "evidence_level": "L2",
            "slice_id": slice_id,
        }
        client.app.state.repository.replace_findings(run_id, [finding])
        scoped_id = finding["id"]
        run_dir = client.app.state.storage.run_dir(run_id)
        slice_dir = run_dir / "slices" / slice_id
        slice_dir.mkdir(parents=True)
        (slice_dir / "round-000.json").write_text('{"contexts": []}', "utf-8")
        legacy_evidence = run_dir / "reports" / "evidence" / "finding_pruned_slice.json"
        legacy_evidence.write_text(json.dumps({
            "finding": finding,
            "context_slice": {
                "slice_id": slice_id,
                "contexts": [{"context_id": "retained-context"}],
                "request_history": [{"round": 1}],
            },
        }), "utf-8")

        cleanup = client.post(
            f"/api/runs/{run_id}/cleanup",
            json={"mode": "prune_intermediates"},
        )
        assert cleanup.status_code == 200
        assert not slice_dir.exists()

        fallback = client.get(f"/api/findings/{scoped_id}/slice")

        assert fallback.status_code == 200
        value = fallback.json()
        assert value["finding_id"] == scoped_id
        assert value["source"] == "report_evidence"
        assert value["round_count"] == 2
        assert value["slice"]["contexts"][0]["context_id"] == "retained-context"


def test_confirmed_review_requires_reason(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        response = client.patch(
            "/api/findings/missing/review",
            json={"status": "confirmed", "reason": "   "},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"


def test_review_syncs_finding_evidence_and_marks_report_stale(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/runs",
            files={"file": ("sample.apk", apk_payload(), "application/vnd.android.package-archive")},
            data={"authorized": "true", "source_analysis_enabled": "false"},
        )
        run_id = response.json()["id"]
        finding = {
            "id": "finding_review_sync",
            "rule_ids": ["ACTIVITY_INTENT_TO_SENSITIVE_SINK"],
            "title": "review sync",
            "component": "activity",
            "component_name": "com.example.ExportedActivity",
            "severity": "pending",
            "confidence": "medium",
            "evidence_level": "L2",
        }
        client.app.state.repository.replace_findings(run_id, [finding])
        scoped_id = finding["id"]
        run_dir = client.app.state.storage.run_dir(run_id)
        legacy_finding_path = run_dir / "findings" / "finding_review_sync.json"
        legacy_finding_path.write_text(json.dumps(finding, ensure_ascii=False), "utf-8")
        evidence_path = run_dir / "reports" / "evidence" / "finding_review_sync.json"
        evidence_path.write_text(json.dumps({"finding": finding}, ensure_ascii=False), "utf-8")
        report_path = run_dir / "reports" / "finding_review_sync.md"
        report_path.write_text("old report", "utf-8")

        review = client.patch(
            "/api/findings/finding_review_sync/review",
            json={"status": "confirmed", "reason": "人工核对链路"},
        )
        assert review.status_code == 200
        assert review.json()["review_status"] == "confirmed"
        saved_finding = json.loads(legacy_finding_path.read_text("utf-8"))
        saved_evidence = json.loads(evidence_path.read_text("utf-8"))
        assert saved_finding["id"] == scoped_id
        assert not (run_dir / "findings" / f"{scoped_id}.json").exists()
        assert saved_finding["review_status"] == "confirmed"
        assert saved_evidence["finding"]["review_status"] == "confirmed"
        assert (run_dir / "reports" / "finding_review_sync.stale.json").is_file()


def test_review_api_idempotency_and_expected_status_conflicts(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/runs",
            files={"file": ("sample.apk", apk_payload(), "application/vnd.android.package-archive")},
            data={"authorized": "true", "source_analysis_enabled": "false"},
        )
        run_id = response.json()["id"]
        finding = {
            "id": "finding_review_idempotency",
            "rule_ids": ["ACTIVITY_INTENT_TO_SENSITIVE_SINK"],
            "title": "review idempotency",
            "component": "activity",
            "component_name": "com.example.ExportedActivity",
            "severity": "pending",
            "confidence": "medium",
            "evidence_level": "L2",
        }
        client.app.state.repository.replace_findings(run_id, [finding])
        body = {
            "status": "confirmed",
            "reason": "人工核对链路",
            "request_id": "api-review-request-1",
            "expected_status": "pending_ai",
            "basis": "source and sink verified",
            "actor": "local-analyst",
        }

        first = client.patch(f"/api/findings/{finding['id']}/review", json=body)
        repeated = client.patch(f"/api/findings/{finding['id']}/review", json=body)

        assert first.status_code == repeated.status_code == 200
        assert first.json()["review_status"] == repeated.json()["review_status"] == "confirmed"
        with client.app.state.repository.connect() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM review_history WHERE request_id=?",
                (body["request_id"],),
            ).fetchone()[0] == 1

        reused = client.patch(
            f"/api/findings/{finding['id']}/review",
            json={**body, "status": "manual_false_positive", "reason": "different payload"},
        )
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == "REVIEW_REQUEST_ID_CONFLICT"

        stale = client.patch(
            f"/api/findings/{finding['id']}/review",
            json={
                "status": "manual_false_positive",
                "reason": "stale state",
                "request_id": "api-review-request-2",
                "expected_status": "pending_ai",
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "REVIEW_STATUS_CONFLICT"


def test_old_run_manifest_defaults_to_pipeline_v1(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        ingested = client.app.state.storage.ingest(
            io.BytesIO(apk_payload()), "legacy.apk", "legacy-trace", {}
        )
        manifest = ingested["manifest"]
        manifest.pop("schema_version")
        manifest.pop("pipeline_version")
        manifest.pop("artifact_schema_versions")
        client.app.state.storage.write_manifest(ingested["id"], manifest)
        client.app.state.repository.create_run({
            "id": ingested["id"],
            "trace_id": "legacy-trace",
            "status": "completed",
            "stage": "completed",
            "apk_filename": "legacy.apk",
            "apk_sha256": ingested["sha256"],
            "config": {},
            "manifest_path": str(
                client.app.state.storage.run_dir(ingested["id"]) / "manifest.json"
            ),
            "pipeline_version": "1.0.0",
            "schema_version": "1.0.0",
        })

        response = client.get(f"/api/runs/{ingested['id']}")

        assert response.status_code == 200
        value = response.json()
        assert value["pipeline_version"] == "1.0.0"
        assert value["schema_version"] == "1.0.0"
        assert value["manifest"]["pipeline_version"] == "1.0.0"
        assert value["manifest"]["schema_version"] == "1.0.0"
