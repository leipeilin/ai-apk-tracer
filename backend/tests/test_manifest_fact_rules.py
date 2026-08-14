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


class TestCleartextTrafficAllowed:
    def test_cleartext_true_target_28_produces_candidate(self) -> None:
        cands = _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED", {"uses_cleartext_traffic": True, "target_sdk": 28}
        )
        assert len(cands) == 1
        assert cands[0]["severity_hint"] == "medium"

    def test_cleartext_true_target_27_no_candidate(self) -> None:
        # targetSdk<28 时明文默认允许，显式 true 无额外风险
        assert _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED", {"uses_cleartext_traffic": True, "target_sdk": 27}
        ) == []

    def test_cleartext_false_no_candidate(self) -> None:
        assert _manifest_fact_candidates(
            "CLEARTEXT_TRAFFIC_ALLOWED", {"uses_cleartext_traffic": False, "target_sdk": 30}
        ) == []

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
