"""_pipeline_requires_ai 判定矩阵测试（含 L1 高暴露组件升级）。

v2026-08-09：L1 coverage_insufficient（有真实代码上下文，如动态 receiver 注册点）
送 AI——此前 L1 仅 high_risk_uncertain 送 AI（实际为 0），128 个 L1 从不分析；
exposure_only（纯 manifest 事实）不送。L2 行为保持不变。
"""

from __future__ import annotations

from app.analysis.candidate_funnel import _pipeline_requires_ai


def _candidate(**overrides: object) -> dict:
    base: dict = {
        "evidence_level": "L2",
        "funnel_disposition": "coverage_insufficient",
        "deterministic_chain_verified": False,
    }
    base.update(overrides)
    return base


class TestL1Upgrade:
    def test_l1_coverage_insufficient_sends_ai(self) -> None:
        """核心升级：L1 有代码上下文（动态 receiver 注册点）→ 送 AI。"""

        c = _candidate(evidence_level="L1", funnel_disposition="coverage_insufficient")
        assert _pipeline_requires_ai(c) is True

    def test_l1_high_risk_uncertain_sends_ai(self) -> None:
        c = _candidate(evidence_level="L1", funnel_disposition="high_risk_uncertain")
        assert _pipeline_requires_ai(c) is True

    def test_l1_exposure_only_not_sent(self) -> None:
        """纯 manifest 暴露事实无代码上下文，AI 无内容可分析 → 不送。"""

        c = _candidate(evidence_level="L1", funnel_disposition="exposure_only")
        assert _pipeline_requires_ai(c) is False

    def test_l1_deterministically_refuted_not_sent(self) -> None:
        c = _candidate(evidence_level="L1", funnel_disposition="deterministically_refuted")
        assert _pipeline_requires_ai(c) is False


class TestL2Unchanged:
    def test_l2_coverage_insufficient_sends_ai(self) -> None:
        c = _candidate(evidence_level="L2", funnel_disposition="coverage_insufficient")
        assert _pipeline_requires_ai(c) is True

    def test_l2_deterministically_refuted_not_sent(self) -> None:
        c = _candidate(evidence_level="L2", funnel_disposition="deterministically_refuted")
        assert _pipeline_requires_ai(c) is False

    def test_l2_chain_verified_with_uncertainty_sends_ai(self) -> None:
        c = _candidate(
            evidence_level="L2",
            funnel_disposition="deterministically_promoted_l2",
            deterministic_chain_verified=True,
            authorization_status="unknown",
        )
        assert _pipeline_requires_ai(c) is True

    def test_l2_chain_verified_no_uncertainty_not_sent(self) -> None:
        c = _candidate(
            evidence_level="L2",
            funnel_disposition="deterministically_promoted_l2",
            deterministic_chain_verified=True,
            authorization_status="protected",
            guard_status="present",
            impact_status="statically_confirmed",
        )
        assert _pipeline_requires_ai(c) is False

    def test_l2_disposition_missing_defaults_send_ai(self) -> None:
        # disposition 缺失时按非闭链处理 → 送 AI（保守）
        c = _candidate(evidence_level="L2", deterministic_chain_verified=False)
        assert _pipeline_requires_ai(c) is True

    def test_l1_disposition_missing_not_sent(self) -> None:
        # L1 disposition 缺失 → 非 coverage_insufficient → 不送（保持原保守行为）
        c = _candidate(evidence_level="L1", funnel_disposition=None)
        assert _pipeline_requires_ai(c) is False


class TestGuardBlocked:
    def test_l2_guard_blocked_not_sent_to_ai(self) -> None:
        """方案 X'：guard_blocked（debuggable 拦死）→ 确定性事实，无需 AI。"""

        c = _candidate(
            evidence_level="L2",
            funnel_disposition="coverage_insufficient",
            guard_blocked=True,
        )
        assert _pipeline_requires_ai(c) is False

    def test_l1_guard_blocked_not_sent_to_ai(self) -> None:
        """L1 候选 guard_blocked 同样不送 AI（guard 优先于 L1 升级）。"""

        c = _candidate(
            evidence_level="L1",
            funnel_disposition="coverage_insufficient",
            guard_blocked=True,
        )
        assert _pipeline_requires_ai(c) is False

    def test_guard_blocked_overrides_chain_verified(self) -> None:
        """guard_blocked 优先于一切送 AI 条件（包括非闭链）。"""

        c = _candidate(
            evidence_level="L2",
            deterministic_chain_verified=False,
            guard_blocked=True,
        )
        assert _pipeline_requires_ai(c) is False


class TestGroupAggregationGuardBlocked:
    def test_group_ai_required_excludes_guard_blocked_members(self) -> None:
        """方案 X' 防御：同组有 guard_blocked + 需要 AI 的成员，guard_blocked 不被带飞。"""

        from app.analysis.candidate_funnel import CandidateFunnel

        base = {
            "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
            "component": "activity",
            "component_name": "com.example.Foo",
            "evidence_level": "L2",
            "authorization_status": "unprotected",
            "guard_status": "absent",
            "reachability_status": "reachable",
            "impact_status": "potential",
            "dataflow_status": "not_proven",
            "analysis_status": "rule_only",
            "funnel_disposition": "coverage_insufficient",
            "sources": [{"path": "com/example/Foo.java", "line": 10}],
            "sinks": [{"path": "com/example/Foo.java", "line": 20}],
        }
        # 两个候选：identity 相同（同 scope/chain/fact）→ 同组
        cand_a = dict(base)          # 需要 AI
        cand_b = dict(base)
        cand_b["component_name"] = "com.example.Foo2"
        cand_b["sources"] = [{"path": "com/example/Foo2.java", "line": 10}]
        cand_b["guard_blocked"] = True   # guard 阻断
        # 预先设置 ai_required（process 内部会重算，但 guard_blocked 必须在组聚合前已标记）
        result = CandidateFunnel({"min_l1_risk_score": 80}).process([cand_a, cand_b])
        # 验证：guard_blocked 候选不应出现在 representative_indexes（不被带飞送 AI）
        gb_indexes = [i for i, c in enumerate(result.candidates) if c.get("guard_blocked")]
        for i in gb_indexes:
            assert i not in result.representative_indexes, "guard_blocked 候选不得送 AI"
