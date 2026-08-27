"""本地存储配置族（§12）_manifest_fact_candidates 全边界测试。

新增规则（v2026-08-09）：DEBUGGABLE_IN_PRODUCTION / ALLOW_BACKUP_ENABLED /
CLEARTEXT_TRAFFIC_ALLOWED——纯 manifest 事实，确定性生成 L1 候选。
真实数据：小米商城命中 CLEARTEXT 1 条（此前"证据明确但系统未发现"漏报）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.config import WORKSPACE_ROOT

sys.path.insert(0, str(WORKSPACE_ROOT / "rules"))

from shared.detector import _manifest_fact_candidates  # noqa: E402


class TestDebugGableInProduction:
    def test_debuggable_true_produces_high_candidate(self) -> None:
        cands = _manifest_fact_candidates(
            "DEBUGGABLE_IN_PRODUCTION", {"debuggable": True, "target_sdk": 30}
        )
        assert len(cands) == 1
        assert cands[0]["severity_hint"] == "high"
        assert cands[0]["evidence_level"] == "L1"
        assert cands[0]["analysis_status"] == "rule_only"

    def test_debuggable_false_no_candidate(self) -> None:
        assert _manifest_fact_candidates(
            "DEBUGGABLE_IN_PRODUCTION", {"debuggable": False}
        ) == []

    def test_debuggable_missing_is_release_no_candidate(self) -> None:
        # manifest 未声明 debuggable = 默认 false（release），不产生高危候选
        assert _manifest_fact_candidates("DEBUGGABLE_IN_PRODUCTION", {}) == []

    def test_debuggable_non_bool_no_candidate(self) -> None:
        assert _manifest_fact_candidates(
            "DEBUGGABLE_IN_PRODUCTION", {"debuggable": "true"}
        ) == []


class TestAllowBackupEnabled:
    def test_allow_backup_true_target_23_produces_candidate(self) -> None:
        cands = _manifest_fact_candidates(
            "ALLOW_BACKUP_ENABLED", {"allow_backup": True, "target_sdk": 23}
        )
        assert len(cands) == 1
        assert cands[0]["severity_hint"] == "medium"

    def test_allow_backup_true_target_22_no_candidate(self) -> None:
        # targetSdk<23 时备份行为由旧规则决定，不判定为中危
        assert _manifest_fact_candidates(
            "ALLOW_BACKUP_ENABLED", {"allow_backup": True, "target_sdk": 22}
        ) == []

    def test_allow_backup_false_no_candidate(self) -> None:
        assert _manifest_fact_candidates(
            "ALLOW_BACKUP_ENABLED", {"allow_backup": False, "target_sdk": 30}
        ) == []

    def test_allow_backup_true_target_missing_no_candidate(self) -> None:
        assert _manifest_fact_candidates("ALLOW_BACKUP_ENABLED", {"allow_backup": True}) == []

    def test_allow_backup_undeclared_target_23_reports_low_stock(self) -> None:
        # P3（评审 E4）：未声明 allowBackup 时默认 true——沉默风险按 low 存量报
        cands = _manifest_fact_candidates("ALLOW_BACKUP_ENABLED", {"target_sdk": 30})
        assert len(cands) == 1
        assert cands[0]["severity_hint"] == "low"
        assert "未声明" in cands[0]["description"]

    def test_allow_backup_undeclared_target_22_no_candidate(self) -> None:
        # targetSdk<23 不在 Auto Backup 门槛内，未声明不报
        assert _manifest_fact_candidates("ALLOW_BACKUP_ENABLED", {"target_sdk": 22}) == []

    def test_allow_backup_with_exemption_mechanism_downgrades_low(self) -> None:
        # P3（评审 E4）：dataExtractionRules/fullBackupContent 存在时降级 low（需规则内容复核）
        for exemption in ("data_extraction_rules", "full_backup_content"):
            cands = _manifest_fact_candidates(
                "ALLOW_BACKUP_ENABLED",
                {"allow_backup": True, "target_sdk": 31, exemption: "@xml/rules"},
            )
            assert len(cands) == 1
            assert cands[0]["severity_hint"] == "low"
            assert "豁免机制" in cands[0]["description"]


class TestCleartextTrafficAllowed:
    def test_cleartext_true_target_28_produces_candidate(self) -> None:
        cands = _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED", {"uses_cleartext_traffic": True, "target_sdk": 28}
        )
        assert len(cands) == 1
        assert cands[0]["severity_hint"] == "medium"

    def test_cleartext_true_target_27_reports_low_stock(self) -> None:
        # P3（评审 E3）：targetSdk<28 平台默认允许明文——显式 true 同样落在存量风险面，按 low 报
        cands = _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED", {"uses_cleartext_traffic": True, "target_sdk": 27}
        )
        assert len(cands) == 1
        assert cands[0]["severity_hint"] == "low"
        assert "默认允许明文" in cands[0]["description"]

    def test_cleartext_false_no_candidate(self) -> None:
        assert _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED", {"uses_cleartext_traffic": False, "target_sdk": 30}
        ) == []

    def test_cleartext_undeclared_target_27_reports_low_stock(self) -> None:
        # 未声明（None）+ targetSdk<28 → 默认放行存量
        cands = _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED", {"target_sdk": 26}
        )
        assert len(cands) == 1
        assert cands[0]["severity_hint"] == "low"

    def test_cleartext_false_target_27_no_candidate(self) -> None:
        assert _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED", {"uses_cleartext_traffic": False, "target_sdk": 27}
        ) == []

    def test_cleartext_with_nsc_downgrades_low(self) -> None:
        # P3（评审 E3）：networkSecurityConfig 存在时 manifest 标志被官方语义覆盖忽略——降级 low
        cands = _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED",
            {
                "uses_cleartext_traffic": True, "target_sdk": 30,
                "network_security_config": "@xml/network_security_config",
            },
        )
        assert len(cands) == 1
        assert cands[0]["severity_hint"] == "low"
        assert "networkSecurityConfig" in cands[0]["description"]

    def test_cleartext_true_target_invalid_no_candidate(self) -> None:
        # 非法 target_sdk（非数字）不得抛异常，按 0 处理 → 不命中
        assert _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED", {"uses_cleartext_traffic": True, "target_sdk": "abc"}
        ) == []
        assert _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED", {"uses_cleartext_traffic": True, "target_sdk": None}
        ) == []


class TestUnknownRuleId:
    def test_unknown_rule_returns_empty(self) -> None:
        assert _manifest_fact_candidates("NOT_A_REAL_RULE", {}) == []


class TestManifestParsingNewFields:
    """P3（评审 E3/E4）：manifest.py 解析 networkSecurityConfig/dataExtractionRules/fullBackupContent 入口属性。"""

    _NS = 'xmlns:android="http://schemas.android.com/apk/res/android"'

    def test_parse_extracts_new_application_attributes(self, tmp_path: Path) -> None:
        from app.analysis.manifest import parse_manifest

        path = tmp_path / "AndroidManifest.xml"
        path.write_text(
            f'<manifest package="com.example" {self._NS}>'
            '<uses-sdk android:targetSdkVersion="31"/>'
            '<application android:allowBackup="true" android:usesCleartextTraffic="true"'
            ' android:networkSecurityConfig="@xml/nsc"'
            ' android:dataExtractionRules="@xml/rules"'
            ' android:fullBackupContent="@xml/backup"/>'
            "</manifest>",
            "utf-8",
        )
        data = parse_manifest(path)
        assert data["network_security_config"] == "@xml/nsc"
        assert data["data_extraction_rules"] == "@xml/rules"
        assert data["full_backup_content"] == "@xml/backup"

    def test_parse_defaults_new_attributes_none(self, tmp_path: Path) -> None:
        from app.analysis.manifest import parse_manifest

        path = tmp_path / "AndroidManifest.xml"
        path.write_text(
            f'<manifest package="com.example" {self._NS}><application/></manifest>', "utf-8"
        )
        data = parse_manifest(path)
        assert data["network_security_config"] is None
        assert data["data_extraction_rules"] is None
        assert data["full_backup_content"] is None


class TestP3VerificationEdgeCases:
    """P3 核验 R-1/R-2：NSC 双向覆盖与 unknown（资源引用）形态的行为锚定。"""

    def test_cleartext_explicit_false_with_nsc_target_27_reports_stock(self) -> None:
        # R-1：显式 false + NSC → 标志被忽略，NSC 对 <28 默认允许明文 → 报存量
        cands = _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED",
            {"uses_cleartext_traffic": False, "target_sdk": 27,
             "network_security_config": "@xml/nsc"},
        )
        assert len(cands) == 1
        assert cands[0]["severity_hint"] == "low"

    def test_cleartext_explicit_false_without_nsc_target_27_no_candidate(self) -> None:
        cands = _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED",
            {"uses_cleartext_traffic": False, "target_sdk": 27},
        )
        assert cands == []

    def test_cleartext_unknown_target_27_reports_low(self) -> None:
        # R-2：资源引用（unknown）+ <28 → 保守报存量
        cands = _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED",
            {"uses_cleartext_traffic": "unknown", "target_sdk": 27},
        )
        assert len(cands) == 1
        assert cands[0]["severity_hint"] == "low"

    def test_cleartext_unknown_target_30_no_candidate(self) -> None:
        # R-2：unknown + >=28 → 引用真值未解析，不按显式 true 报
        cands = _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED",
            {"uses_cleartext_traffic": "unknown", "target_sdk": 30},
        )
        assert cands == []

    def test_allow_backup_unknown_target_30_reports_low_stock(self) -> None:
        # R-2：unknown 与 None 同报存量（allowBackup 默认 true，运行时行为一致）
        cands = _manifest_fact_candidates(
            "ALLOW_BACKUP_ENABLED",
            {"allow_backup": "unknown", "target_sdk": 30},
        )
        assert len(cands) == 1
        assert cands[0]["severity_hint"] == "low"
        assert "未声明（或资源引用未解析）" in cands[0]["description"]

    def test_allow_backup_unknown_with_exemption_still_low(self) -> None:
        cands = _manifest_fact_candidates(
            "ALLOW_BACKUP_ENABLED",
            {"allow_backup": "unknown", "target_sdk": 30, "data_extraction_rules": "@xml/r"},
        )
        assert len(cands) == 1
        assert cands[0]["severity_hint"] == "low"

    def test_platform_assumptions_carry_new_facts(self) -> None:
        # R-5：三个新事实随候选下发给 AI 复核（_fact 输出 "key=value" 字符串）
        from shared.detector import _platform_assumptions

        facts = _platform_assumptions({
            "network_security_config": "@xml/nsc",
            "data_extraction_rules": "@xml/rules",
            "full_backup_content": "@xml/backup",
        })
        assert "network_security_config=@xml/nsc" in facts
        assert "data_extraction_rules=@xml/rules" in facts
        assert "full_backup_content=@xml/backup" in facts
        # 缺失时显式标 unknown（与既有事实的口径一致）
        assert "network_security_config=unknown" in _platform_assumptions({})
