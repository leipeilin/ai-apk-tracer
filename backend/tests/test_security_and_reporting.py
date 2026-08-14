from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.analysis.apk_validation import validate_apk_zip
from app.findings.report import build_report_payload, render_markdown
from app.findings.severity import determine_severity
from app.shared.errors import ConflictError, ValidationError


def make_apk(path: Path, entries: dict[str, bytes] | None = None) -> Path:
    content = entries or {
        "AndroidManifest.xml": b"<manifest package='com.example.app' />",
        "classes.dex": b"dex\n035\x00",
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in content.items():
            archive.writestr(name, value)
    return path


def test_apk_zip_rejects_path_traversal(tmp_path: Path) -> None:
    apk = make_apk(tmp_path / "bad.apk", {"AndroidManifest.xml": b"ok", "../escape": b"bad"})
    with pytest.raises(ValidationError) as exc:
        validate_apk_zip(apk)
    assert exc.value.code == "ZIP_PATH_TRAVERSAL"


def test_apk_zip_allows_highly_compressible_dictionary(tmp_path: Path) -> None:
    apk = make_apk(
        tmp_path / "dictionary.apk",
        {
            "AndroidManifest.xml": b"<manifest package='com.example.dictionary' />",
            "assets/lm-dict.dic": b"dictionary-entry\n" * 200_000,
        },
    )
    validate_apk_zip(apk)


def test_l1_is_always_informational() -> None:
    severity, reasons = determine_severity({"evidence_level": "L1", "severity_hint": "critical"})
    assert severity == "informational"
    assert reasons


def test_critical_gap_forces_pending() -> None:
    severity, _ = determine_severity({
        "evidence_level": "L2",
        "severity_hint": "high",
        "blocking_gaps": [{"code": "SERVER_AUTH_UNKNOWN", "critical": True}],
    })
    assert severity == "pending"


def test_static_report_has_strict_sample_headings() -> None:
    finding = {
        "id": "finding_demo",
        "run_id": "run_demo",
        "title": "导出 Activity 代理调用高权限能力",
        "description": "静态分析发现外部 Intent 可到达敏感设置修改操作。",
        "severity": "high",
        "confidence": "high",
        "evidence_level": "L3",
        "review_status": "confirmed",
        "component": "activity",
        "component_name": "com.example.AdminActivity",
        "permission": None,
        "locations": [{"path": "sources/AdminActivity.java", "line": 42, "verification": "fact"}],
        "sources": [{"text": "Intent.getStringExtra", "evidence_id": "source:42", "status": "fact"}],
        "sinks": [{"text": "Settings.Secure.putString", "kind": "persistent_state_write", "path": "sources/AdminActivity.java", "line": 76, "evidence_id": "sink:76", "status": "fact"}],
        "propagation_paths": [{"text": "onCreate -> applySetting", "evidence_id": "path:1", "status": "fact"}],
        "attacker_prerequisites": ["普通第三方应用"],
        "impact_scope": ["系统安全设置"],
        "pipeline_version": "2.0.0",
        "schema_version": "2.0.0",
        "funnel_disposition": "deterministically_promoted_l2",
        "analysis_status": "ai_completed",
        "analysis_track": "l2_review",
        "evidence_decision": "supported",
        "ai_stop_reason": "analysis_complete",
        "ai_analysis": {
            "summary": "AI observation only",
            "triage_disposition": "potential_chain",
        },
        "ai_analysis_trace": [{
            "result": {"metadata": {"prompt_version": "2.0.0", "cache_hit": True}}
        }],
        "external_status": "not_exported",
        "app": {"package": "com.example", "version_code": "1", "version_name": "1.0"},
    }
    run = {
        "id": "run_demo",
        "manifest": {
            "schema_version": "2.0.0",
            "pipeline_version": "2.0.0",
            "artifact_schema_versions": {"report_payload": "2.0.0"},
            "analysis_incomplete": True,
            "stages": [
                {"name": "decompiling", "status": "partial", "summary": {"error_count": 12}},
                {"name": "rule_prescan", "status": "completed", "summary": {"rule_failures": [], "rule_total_count": 29}},
                {"name": "code_slicing", "status": "completed", "summary": {"index_stats": {"skipped_file_count": 2}}},
                {"name": "ai_analysis", "status": "partial", "summary": {"analyzed": 4, "failed": 1}},
            ],
        },
    }
    markdown = render_markdown(build_report_payload(finding, run))
    headings = [line for line in markdown.splitlines() if line.startswith("## ")]
    assert headings == ["## 版本", "## 测试环境：", "## 漏洞描述", "## 漏洞链路：", "## POC", "## 修复方案"]
    assert "未执行动态验证" in markdown
    assert "扫描完整性：不完整" in markdown
    assert "JADX：partial（错误 12）" in markdown
    assert markdown.startswith("# 导出 Activity 代理调用高权限能力")
    assert "规则失败：0/29" in markdown
    assert "AI：partial（成功 3，失败 1）" in markdown
    assert "AI 跳过原因：无" in markdown
    assert "POC结果：尚未执行动态影响验证" in markdown
    assert "adb shell am start -W -n 'com.example/com.example.AdminActivity'" in markdown
    assert "攻击者可获得的能力" in markdown
    assert "可能造成的具体后果" in markdown
    assert "对应 Sink 证据" in markdown
    assert "将外部可控数据写入应用持久化状态" in markdown
    assert "后续业务读取该状态时" in markdown
    assert "persistent_state_write @ sources/AdminActivity.java:76" in markdown
    assert "Pipeline 版本：2.0.0" in markdown
    assert "Prompt 版本：2.0.0" in markdown
    assert "确定性事实：reachability_status=unknown" in markdown
    assert "AI observation：analysis_track" in markdown
    assert "triage_disposition=potential_chain" in markdown
    assert "stop_reason=analysis_complete" in markdown
    assert "cache_hit=True" in markdown
    assert "覆盖/停止原因：analysis_complete" in markdown
    assert "外发状态：not_exported" in markdown
    assert "用户无需确认" not in markdown


@pytest.mark.parametrize(
    ("component", "component_name", "expected_command", "expected_note"),
    [
        (
            "service",
            "com.example.SyncService",
            "adb shell am startservice -n 'com.example/com.example.SyncService'",
            "started-service 路径可达",
        ),
        (
            "receiver",
            "com.example.EventReceiver",
            "adb shell am broadcast -n 'com.example/com.example.EventReceiver' -a '<ACTION>'",
            "动态 Receiver 还需先满足其注册生命周期",
        ),
        (
            "provider",
            "com.example.DataProvider",
            "adb shell content query --uri 'content://<AUTHORITY>/<PATH>' --user 0",
            "只先执行只读 query",
        ),
    ],
)
def test_report_poc_contains_component_specific_adb_guidance(
    component: str, component_name: str, expected_command: str, expected_note: str
) -> None:
    finding = {
        "id": f"finding_{component}",
        "title": "组件安全候选",
        "evidence_level": "L2",
        "severity": "medium",
        "component": component,
        "component_name": component_name,
        "review_status": "pending_manual",
        "sources": [{"text": "intent.getStringExtra(...)"}],
        "sinks": [{"kind": "callback_event_injection", "path": "Demo.java", "line": 10}],
        "app": {"package": "com.example"},
    }
    markdown = render_markdown(build_report_payload(finding, {"id": "run", "manifest": {}}))
    assert expected_command in markdown
    assert expected_note in markdown
    assert "命令执行后漏洞必然成立" in markdown


def test_binder_report_requires_normal_uid_test_apk() -> None:
    finding = {
        "id": "finding_binder",
        "title": "Binder 无鉴权候选",
        "rule_id": "SERVICE_BINDER_CALLER_CHECK_MISSING",
        "evidence_level": "L2",
        "severity": "high",
        "component": "service",
        "component_name": "com.example.RemoteService",
        "review_status": "pending_manual",
        "sinks": [{"kind": "binder_sensitive_api", "path": "RemoteService.java", "line": 42}],
        "app": {"package": "com.example"},
    }
    markdown = render_markdown(build_report_payload(finding, {"id": "run", "manifest": {}}))
    assert "adb shell dumpsys activity services 'com.example'" in markdown
    assert "无特殊权限、不同签名的最小测试 APK" in markdown
    assert "ADB 不能直接完成普通第三方 UID" in markdown


def test_critical_gap_report_is_marked_pending() -> None:
    finding = {
        "id": "gap",
        "title": "Provider 文件访问候选",
        "evidence_level": "L2",
        "severity": "pending",
        "blocking_gaps": [{"code": "DUPLICATE_PROVIDER_AUTHORITY", "critical": True, "message": "authority 冲突"}],
        "app": {},
    }
    payload = build_report_payload(finding, {"id": "run", "manifest": {}})
    assert payload["title"] == "待确认：Provider 文件访问候选"
    assert render_markdown(payload).startswith("# 待确认：Provider 文件访问候选")


def test_l1_report_is_forbidden() -> None:
    with pytest.raises(ConflictError):
        build_report_payload(
            {"id": "l1", "evidence_level": "L1", "severity": "informational"},
            {"id": "run"},
        )
