"""M4-T4.3 报告质量检查测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.evaluation.report_quality import check_report_document
from app.reporting.generator import generate_report_document

_WORKSPACE = Path(__file__).resolve().parents[2]


def _document(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "ai_draft": {
            "summary": "s", "narrative": "n", "exploit_scenario": "e",
            "confidence_tier": "medium", "provenance": "ai_report_protocol",
            "prompt_version": "1.0.0", "model": "m", "analysis_complete": True,
        },
        "deterministic": {
            "rule_id": "R", "severity": "pending",
            "sources": [{"path": "a/A.java", "line": 1, "text": "src"}],
            "sinks": [{"path": "a/A.java", "line": 2, "text": "snk"}],
        },
        "poc_skeleton": {
            "component_kind": "service", "kind": "binder_transaction",
            "steps": ["x"], "command_skeleton": ["# bind <PACKAGE>"],
            "notes": ["本骨架仅为验证步骤说明，不包含任何可执行文件；命令中的占位符需替换"],
            "executable_files_created": [],
        },
    }
    doc.update(overrides)
    return doc


class TestSeparation:
    def test_pass(self) -> None:
        result = check_report_document(_document())
        assert result["verdict"] == "PASS"
        assert all(c["verdict"] == "PASS" for c in result["checks"].values())

    def test_illegal_provenance_fails(self) -> None:
        doc = _document()
        doc["ai_draft"]["provenance"] = "human_written"
        result = check_report_document(doc)
        assert result["verdict"] == "FAIL"
        assert any("provenance 非法" in v for v in result["checks"]["ai_deterministic_separation"]["violations"])

    def test_missing_ai_draft_fails(self) -> None:
        result = check_report_document(_document(ai_draft=None))
        assert result["verdict"] == "FAIL"


class TestReferences:
    def test_bad_path_and_line_warn(self) -> None:
        doc = _document()
        doc["deterministic"]["sources"].append({"path": "", "line": -3})
        doc["deterministic"]["sinks"].append({"path": "b.java", "line": "x"})
        result = check_report_document(doc)
        assert result["verdict"] == "WARN"
        refs = result["checks"]["evidence_reference_integrity"]["violations"]
        assert any("path 空" in v for v in refs)
        assert any("line 非法" in v for v in refs)

    def test_null_line_tolerated(self) -> None:
        doc = _document()
        doc["deterministic"]["sources"].append({"path": "c.java", "line": None})
        assert check_report_document(doc)["verdict"] == "PASS"


class TestPocConsistency:
    def test_executable_files_fails(self) -> None:
        doc = _document()
        doc["poc_skeleton"]["executable_files_created"] = ["evil.py"]
        result = check_report_document(doc)
        assert result["verdict"] == "FAIL"
        assert any("executable_files_created 非空" in v for v in result["checks"]["poc_skeleton_consistency"]["violations"])

    def test_concrete_command_warns(self) -> None:
        """R-7 分级：命令无占位符 → WARN（非硬红线）。"""
        doc = _document()
        doc["poc_skeleton"]["command_skeleton"] = ["adb shell am start -n real.app/Real"]
        result = check_report_document(doc)
        assert result["verdict"] == "WARN"
        assert result["checks"]["poc_skeleton_consistency"]["verdict"] == "WARN"

    def test_kind_enum(self) -> None:
        doc = _document()
        doc["poc_skeleton"]["kind"] = "weird"
        result = check_report_document(doc)
        assert any("poc kind 非法" in v for v in result["checks"]["poc_skeleton_consistency"]["violations"])

    def test_notes_keyword_required_warns(self) -> None:
        """R-7 分级：notes 缺声明 → WARN（非硬红线）。"""
        doc = _document()
        doc["poc_skeleton"]["notes"] = ["随便一句话"]
        result = check_report_document(doc)
        assert result["verdict"] == "WARN"
        assert any("notes 缺少" in v for v in result["checks"]["poc_skeleton_consistency"]["violations"])


class TestRealDocument:
    def test_v01_projection_document_passes(self) -> None:
        """A-6：真实 V-01（投影模式）→ 全 PASS（评审 R-7：本地产物 skip 兜底）。"""
        finding_path = (
            _WORKSPACE / ".ai-apk-tracer" / "runs" / "20260815T125744Z_2a80fc5a8735_ef5915ff"
            / "findings" / "20260815T125744Z_2a80fc5a8735_ef5915ff_finding_1ed37af9596f8761bda5.json"
        )
        if not finding_path.is_file():
            pytest.skip(f"真实 finding 产物缺失: {finding_path}")
        finding = json.loads(finding_path.read_text("utf-8"))
        document = asyncio.run(generate_report_document(finding))
        result = check_report_document(document.model_dump(mode="json"))
        assert result["verdict"] == "PASS", result["checks"]

    def test_synthetic_confirmed_document_passes(self) -> None:
        """A-6 兜底：合成 confirmed finding（投影模式）→ 全 PASS（CI 无产物时覆盖）。"""
        finding = {
            "id": "finding_synthetic_001", "run_id": "run_x",
            "review_status": "confirmed", "evidence_level": "L2",
            "rule_id": "SERVICE_BINDER_CALLER_CHECK_MISSING",
            "component": "service", "component_name": "com.example.Svc",
            "severity": "pending",
            "sources": [{"path": "a/A.java", "line": 1, "text": "t"}],
            "sinks": [{"path": "a/A.java", "line": 2, "text": "t"}],
            "ai_analysis": {"candidate_verdict": "supports_candidate",
                            "confidence_tier": "medium", "flaw_holds": True,
                            "analysis_complete": True, "harm": "h"},
        }
        document = asyncio.run(generate_report_document(finding))
        result = check_report_document(document.model_dump(mode="json"))
        assert result["verdict"] == "PASS", result["checks"]


def test_locations_bucket_checked() -> None:
    """M3/M4 审查 4.5：引用回查覆盖 locations 桶（与投影三桶对齐）。"""
    doc = _document()
    doc["deterministic"]["locations"] = [{"path": "loc/L.java", "line": 5}]
    assert check_report_document(doc)["verdict"] == "PASS"
    bad = _document()
    bad["deterministic"]["locations"] = [{"path": "", "line": 5}]
    result = check_report_document(bad)
    assert result["verdict"] == "WARN"
    assert any("locations" in v and "path 空" in v
               for v in result["checks"]["evidence_reference_integrity"]["violations"])


class TestComponentDomainR3:
    """2026-08-26 审查 R-3：webview/crypto/manifest 组件域合法（真实 finding 域）；
    P5 核验 R-1 扩 intent/log（新规则域）。"""

    def test_webview_and_crypto_components_pass(self) -> None:
        for component in ("webview", "crypto", "manifest", "intent", "log"):
            doc = _document()
            doc["poc_skeleton"]["component_kind"] = component
            result = check_report_document(doc)
            assert result["verdict"] == "PASS", (component, result["checks"]["poc_skeleton_consistency"])

    def test_component_kind_shared_constant(self) -> None:
        from app.evaluation.report_quality import _LEGAL_COMPONENT_KINDS
        from app.reporting.poc import FINDING_COMPONENT_KINDS

        assert _LEGAL_COMPONENT_KINDS is FINDING_COMPONENT_KINDS  # 共享单一常量
        for component in ("webview", "crypto", "manifest", "intent", "log"):
            assert component in FINDING_COMPONENT_KINDS
