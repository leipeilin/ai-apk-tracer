"""M3-1 报告草稿 + PoC 骨架 + 修复建议测试（方案 §5 验收清单）。

覆盖：拒绝路径（非 confirmed / L1 / 可执行 PoC 违例）、正向四点自查
（字段完整 / AI 与确定性分离 + provenance / 零可执行产物 / 引用回查）、
真实 V-01/V-02 finding 端到端（含引用真实存在性断言）。
async 用 asyncio.run 同步包装（项目惯例——无 pytest-asyncio 配置）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.config import ReportSettings
from app.reporting.generator import (
    generate_report_document,
    project_draft_from_l2_review,
    save_report_document,
)
from app.reporting.models import PoCSkeleton, ReportDraft
from app.reporting.poc import build_poc_skeleton
from app.reporting.repair import build_repair_draft
from app.shared.errors import ConflictError, ValidationError

_WORKSPACE = Path(__file__).resolve().parents[2]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _confirmed_finding(**overrides: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": "finding_test_001",
        "run_id": "run_test",
        "review_status": "confirmed",
        "evidence_level": "L2",
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "component": "activity",
        "component_name": "com.example.RouterActivity",
        "candidate_source": None,
        "title": "外部 Intent 到达敏感 sink",
        "description": "exported activity 的 extras 流入敏感操作",
        "severity": "pending",
        "entry_method_id": "sources/com/example/RouterActivity.java#RouterActivity.onCreate:33",
        "entry_points": ["RouterActivity.onCreate"],
        "sources": [{"path": "com/example/RouterActivity.java", "line": 38, "text": "getIntent extras"}],
        "sinks": [{"path": "com/example/RouterActivity.java", "line": 70, "text": "startActivity"}],
        "locations": [],
        "ai_analysis": {
            "candidate_verdict": "supports_candidate",
            "confidence_tier": "medium",
            "flaw_holds": True,
            "analysis_complete": True,
            "harm": "敏感数据外泄",
        },
    }
    finding.update(overrides)
    return finding


class TestRejectionPaths:
    def test_non_confirmed_rejected(self) -> None:
        for status in ("pending_manual", "pending_ai", "refuted"):
            with pytest.raises(ConflictError) as exc_info:
                run(generate_report_document(_confirmed_finding(review_status=status)))
            assert exc_info.value.code == "REPORT_DRAFT_REQUIRES_CONFIRMED"

    def test_l1_rejected(self) -> None:
        with pytest.raises(ConflictError) as exc_info:
            run(generate_report_document(_confirmed_finding(evidence_level="L1")))
        assert exc_info.value.code == "L1_REPORT_FORBIDDEN"

    def test_executable_poc_config_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            run(generate_report_document(
                _confirmed_finding(), settings=ReportSettings(allow_executable_poc=True)))
        assert exc_info.value.code == "EXECUTABLE_POC_FORBIDDEN"

    def test_require_confirmed_disabled_allows_any(self) -> None:
        document = run(generate_report_document(
            _confirmed_finding(review_status="pending_manual"),
            settings=ReportSettings(require_confirmed_finding=False)))
        assert document.finding_id == "finding_test_001"


class TestPositiveContract:
    def test_document_fields_complete(self) -> None:
        document = run(generate_report_document(_confirmed_finding()))
        assert document.finding_id == "finding_test_001"
        assert document.run_id == "run_test"
        assert document.evidence_source == "rule_candidate"
        assert document.explorer_caveat is None  # 规则候选不注入 caveat
        assert document.generated_at is not None

    def test_ai_and_deterministic_separated(self) -> None:
        document = run(generate_report_document(_confirmed_finding()))
        # 结构分离：ai_draft 与 deterministic 键分离 + provenance 诚实标注
        assert set(document.ai_draft) >= {
            "summary", "narrative", "exploit_scenario", "confidence_tier", "provenance"}
        assert document.ai_draft["provenance"] == "projected_from_l2_review"
        assert document.deterministic["rule_id"] == "ACTIVITY_INTENT_TO_SENSITIVE_SINK"
        assert document.deterministic["severity"] == "pending"
        assert document.ai_draft["confidence_tier"] == "medium"

    def test_explorer_source_caveat_injected(self) -> None:
        document = run(generate_report_document(
            _confirmed_finding(candidate_source="explorer")))
        assert document.evidence_source == "explorer_candidate"
        assert document.explorer_caveat and "explorer_validated=0" in document.explorer_caveat

    def test_zero_executable_artifacts(self) -> None:
        document = run(generate_report_document(_confirmed_finding()))
        assert document.poc_skeleton.executable_files_created == []
        assert document.poc_skeleton.command_skeleton  # 骨架文本存在
        assert all("<" in cmd or cmd.startswith("#") for cmd in document.poc_skeleton.command_skeleton)

    def test_provider_injection_point(self) -> None:
        """provider 抽象（M3-2 衔接点）：自定义 provider 替换投影实现。"""
        custom = ReportDraft(
            summary="s", vulnerability_narrative="n", exploit_scenario="e",
            evidence_refs=[], confidence_tier="high", analysis_complete=True)

        async def fake_provider(finding: dict[str, Any]) -> ReportDraft:
            return custom

        document = run(generate_report_document(_confirmed_finding(), provider=fake_provider))
        assert document.ai_draft["summary"] == "s"
        assert document.ai_draft["confidence_tier"] == "high"

    def test_save_creates_file_with_no_executables(self, tmp_path: Path) -> None:
        document = run(generate_report_document(_confirmed_finding()))
        path = save_report_document(document, tmp_path)
        assert path.is_file()
        assert path.parent == tmp_path / "reports" / "drafts"
        # 落盘目录无任何可执行后缀文件
        for suffix in (".py", ".sh", ".apk", ".jar", ".dex"):
            assert not list(path.parent.glob(f"*{suffix}"))
        loaded = json.loads(path.read_text("utf-8"))
        assert loaded["finding_id"] == "finding_test_001"


class TestPocAndRepair:
    def test_poc_kind_by_rule(self) -> None:
        assert build_poc_skeleton(
            _confirmed_finding(rule_id="SERVICE_BINDER_CALLER_CHECK_MISSING", component="service")
        ).kind == "binder_transaction"
        assert build_poc_skeleton(
            _confirmed_finding(rule_id="PROVIDER_LOOSE_URI_MATCH", component="provider")
        ).kind == "provider_query"
        assert build_poc_skeleton(
            _confirmed_finding(rule_id="DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", component="receiver")
        ).kind == "broadcast"

    def test_poc_binder_notes_adb_limitation(self) -> None:
        skeleton = build_poc_skeleton(
            _confirmed_finding(rule_id="SERVICE_BINDER_CALLER_CHECK_MISSING", component="service"))
        assert any("ADB" in note for note in skeleton.notes)
        assert skeleton.executable_files_created == []

    def test_repair_deterministic_mapping(self) -> None:
        repair = build_repair_draft(_confirmed_finding())
        assert repair.deterministic_recommendations
        assert all(isinstance(item, str) for item in repair.deterministic_recommendations)


def test_projected_draft_reference_alignment() -> None:
    """投影草稿的 evidence_refs 与 finding 的 sources/sinks 对齐（可回查）。"""
    draft = run(project_draft_from_l2_review(_confirmed_finding()))
    assert {p.path for p in draft.evidence_refs} == {"com/example/RouterActivity.java"}
    assert all(p.line and p.line >= 1 for p in draft.evidence_refs)


# ---------------------------------------------------------------------------
# 真实 V-01/V-02 finding 端到端（指引 §6.2 最小闭环）
# ---------------------------------------------------------------------------

_HEALTH_RUN = "20260815T125744Z_2a80fc5a8735_ef5915ff"
_REAL_FINDINGS = {
    "V-01": "20260815T125744Z_2a80fc5a8735_ef5915ff_finding_1ed37af9596f8761bda5.json",
    "V-02": "20260815T125744Z_2a80fc5a8735_ef5915ff_finding_5312960eaa38fec5d8bd.json",
}


def _load_real_finding(key: str) -> dict[str, Any]:
    path = (
        _WORKSPACE / ".ai-apk-tracer" / "runs" / _HEALTH_RUN
        / "findings" / _REAL_FINDINGS[key]
    )
    if not path.is_file():  # CI 无 run 产物时跳过
        pytest.skip(f"真实 finding 产物缺失: {path}")
    return json.loads(path.read_text("utf-8"))


class TestRealFindingsEndToEnd:
    def test_v01_binder(self) -> None:
        finding = _load_real_finding("V-01")
        assert finding["review_status"] == "confirmed"
        document = run(generate_report_document(finding))
        assert document.evidence_source == "rule_candidate"
        assert document.poc_skeleton.kind == "binder_transaction"
        assert document.poc_skeleton.executable_files_created == []
        assert document.ai_draft["provenance"] == "projected_from_l2_review"
        assert document.ai_draft["summary"]

    def test_v02_intent(self) -> None:
        finding = _load_real_finding("V-02")
        assert finding["review_status"] == "confirmed"
        document = run(generate_report_document(finding))
        assert document.poc_skeleton.kind == "intent"
        assert document.poc_skeleton.executable_files_created == []
        assert document.repair.deterministic_recommendations

    def test_real_findings_evidence_path_exists(self) -> None:
        """四点自查②（引用回查——评审 R-1 强化）：V-01/V-02 的全部
        sources/sinks path 在反编译源码树逐条存在（弱断言 checked>0 已废弃）。"""
        decompile_root = (
            _WORKSPACE / ".ai-apk-tracer" / "runs" / _HEALTH_RUN / "decompile" / "sources"
        )
        if not decompile_root.is_dir():
            pytest.skip("反编译产物缺失")
        total = 0
        for key in ("V-01", "V-02"):
            finding = _load_real_finding(key)
            document = run(generate_report_document(finding))
            for bucket in ("sources", "sinks"):
                for item in document.deterministic.get(bucket) or []:
                    if isinstance(item, dict) and item.get("path"):
                        total += 1
                        assert (decompile_root / str(item["path"])).exists(), (
                            f"{key} 的 {bucket} 引用不可回查: {item['path']}")
        assert total > 0


# ---------------------------------------------------------------------------
# 事后评审闭合（2026-08-23 review R-1/R-2/R-5/R-6/R-7——见 m3-report-poc-review.md）
# ---------------------------------------------------------------------------


class TestReviewClosure:
    def test_informational_severity_rejected(self) -> None:
        """R-2：L1 拒绝双条件——severity=informational 同样拦截（对齐 report.py 先例）。"""
        with pytest.raises(ConflictError) as exc_info:
            run(generate_report_document(
                _confirmed_finding(severity="informational")))
        assert exc_info.value.code == "L1_REPORT_FORBIDDEN"

    def test_missing_finding_id_rejected(self) -> None:
        """R-5：缺 id 拒绝（防 unknown 兜底多 finding 覆盖）。"""
        finding = _confirmed_finding()
        finding.pop("id")
        with pytest.raises(ValidationError) as exc_info:
            run(generate_report_document(finding))
        assert exc_info.value.code == "FINDING_ID_MISSING"

    def test_invalid_finding_id_characters_rejected(self) -> None:
        """R-5：finding_id 路径注入防护（字符白名单）。"""
        with pytest.raises(ValidationError) as exc_info:
            run(generate_report_document(_confirmed_finding(id="../escape")))
        assert exc_info.value.code == "FINDING_ID_INVALID"

    def test_symlink_save_rejected(self, tmp_path: Path) -> None:
        """R-5：预置 symlink 写穿防护。"""
        document = run(generate_report_document(_confirmed_finding()))
        drafts_dir = tmp_path / "reports" / "drafts"
        drafts_dir.mkdir(parents=True)
        target = tmp_path / "outside.json"
        target.write_text("x", "utf-8")
        (drafts_dir / f"{document.finding_id}.json").symlink_to(target)
        with pytest.raises(ValidationError) as exc_info:
            save_report_document(document, tmp_path)
        assert exc_info.value.code == "REPORT_DRAFT_PATH_UNSAFE"

    def test_executable_files_schema_enforced(self) -> None:
        """R-6：executable_files_created 恒空由 schema 强制（M3-2 provider 无法绕过）。"""
        with pytest.raises(Exception, match="executable_files_created"):
            PoCSkeleton(
                component_kind="service", kind="binder_transaction",
                steps=[], command_skeleton=[], notes=[],
                executable_files_created=["evil.py"])


class TestReportDraftApi:
    """R-7：API 层集成——三拒绝路径 HTTP 映射 + 正向 200 落盘。"""

    def _client(self, tmp_path: Path, finding: dict[str, Any]):
        from fastapi.testclient import TestClient

        from app.config import Settings
        from app.main import create_app

        class _Repo:
            def get_finding(self, finding_id: str) -> dict[str, Any]:
                if finding.get("id") != finding_id:
                    from app.shared.errors import NotFoundError
                    raise NotFoundError("finding 不存在", "FINDING_NOT_FOUND")
                return finding

        class _Storage:
            def run_dir(self, run_id: str) -> Path:
                return tmp_path / "runs" / run_id

        settings = Settings(
            database_path=tmp_path / "tracer.sqlite3",
        )
        app = create_app(settings)
        app.state.repository = _Repo()
        app.state.storage = _Storage()
        return TestClient(app)

    def test_api_rejection_paths(self, tmp_path: Path) -> None:
        cases = [
            (_confirmed_finding(review_status="pending_manual"), 409),
            (_confirmed_finding(severity="informational"), 409),
            (_confirmed_finding(), 200),
        ]
        for finding, expected in cases:
            with self._client(tmp_path, finding) as client:
                response = client.post(
                    f"/api/findings/{finding['id']}/report-draft")
                assert response.status_code == expected, (
                    finding.get("review_status"), finding.get("severity"), response.text[:200])
                if expected == 200:
                    data = response.json()
                    assert data["finding_id"] == finding["id"]
                    assert data["poc_skeleton"]["executable_files_created"] == []

    def test_api_not_found(self, tmp_path: Path) -> None:
        with self._client(tmp_path, _confirmed_finding()) as client:
            response = client.post("/api/findings/no-such/report-draft")
            assert response.status_code == 404

    def test_api_persists_draft(self, tmp_path: Path) -> None:
        finding = _confirmed_finding()
        with self._client(tmp_path, finding) as client:
            assert client.post(
                f"/api/findings/{finding['id']}/report-draft").status_code == 200
        saved = tmp_path / "runs" / "run_test" / "reports" / "drafts" / f"{finding['id']}.json"
        assert saved.is_file()
        assert json.loads(saved.read_text("utf-8"))["finding_id"] == finding["id"]
