from __future__ import annotations

import copy

import pytest

from app.analysis.candidate_funnel import (
    CandidateFunnel,
    CandidateReason,
    CandidateRoute,
    build_candidate_identity,
    build_candidate_routing_plan,
    candidate_precheck,
    deduplicate_exact_candidates,
    exact_candidate_key,
)


def _candidate(**values) -> dict:
    candidate = {
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "rule_version": "1.0.0",
        "evidence_level": "L1",
        "component": "activity",
        "component_name": "com.example.ExportedActivity",
        "entry_points": ["com.example.ExportedActivity#onCreate"],
        "locations": [
            {"artifact": "manifest", "path": "AndroidManifest.xml", "line": None},
            {"artifact": "code", "path": "Example.java", "line": 10},
        ],
        "sources": [
            {"path": "Example.java", "line": 10, "kind": "intent"},
            {"path": "Example.java", "line": 11, "kind": "extra"},
        ],
        "sinks": [{"path": "Example.java", "line": 20, "kind": "webview"}],
        "propagation_paths": [{"from": "intent", "to": "webview", "status": "fact"}],
        "authorization_status": "unprotected",
        "authorization_matrix": [{"operation": "read", "allowed": True}],
        "guard_status": "absent",
        "dataflow_status": "intraprocedural",
        "reachability_status": "reachable",
        "deterministic_chain_verified": True,
        "operation_modes": ["r", "rw"],
    }
    candidate.update(values)
    return candidate


def test_exact_duplicates_group_under_first_seen_representative() -> None:
    first = _candidate()
    duplicate = copy.deepcopy(first)

    groups = deduplicate_exact_candidates([first, duplicate])

    assert len(groups) == 1
    assert groups[0].representative_index == 0
    assert groups[0].duplicate_indexes == (1,)
    assert groups[0].original_indexes == (0, 1)


def test_near_duplicates_do_not_collapse_and_ordered_lists_remain_ordered() -> None:
    original = _candidate()
    changed_location = _candidate(
        locations=[
            {"artifact": "manifest", "path": "AndroidManifest.xml", "line": None},
            {"artifact": "code", "path": "Example.java", "line": 12},
        ]
    )
    changed_rule = _candidate(rule_id="ACTIVITY_EXPORTED_NO_PERMISSION")
    reordered_sources = _candidate(sources=list(reversed(original["sources"])))

    groups = deduplicate_exact_candidates(
        [original, changed_location, changed_rule, reordered_sources]
    )

    assert [group.representative_index for group in groups] == [0, 1, 2, 3]
    assert len({group.candidate_key for group in groups}) == 4


def test_clearly_set_like_fields_are_canonicalized_without_reordering_inputs() -> None:
    first = _candidate(operation_modes=["rw", "r"])
    second = _candidate(operation_modes=["r", "rw"])
    before = copy.deepcopy(first)

    assert exact_candidate_key(first) == exact_candidate_key(second)
    assert first == before


def test_routing_plan_preserves_first_seen_order_and_duplicate_provenance() -> None:
    first = _candidate(component_name="First")
    second = _candidate(component_name="Second", evidence_level="L2")
    candidates = [
        first,
        second,
        copy.deepcopy(first),
        copy.deepcopy(second),
        copy.deepcopy(first),
    ]

    plan = build_candidate_routing_plan(candidates)

    assert plan.representative_indexes == (0, 1)
    assert plan.entries[0].duplicate_indexes == (2, 4)
    assert plan.entries[0].original_indexes == (0, 2, 4)
    assert plan.entries[1].duplicate_indexes == (3,)
    assert [
        (item.candidate_index, item.representative_index)
        for item in plan.duplicate_provenance
    ] == [(2, 0), (3, 1), (4, 0)]
    assert [entry.representative_index for entry in plan.for_route("l1_triage")] == [0]
    assert [entry.representative_index for entry in plan.for_route(CandidateRoute.L2_REVIEW)] == [1]


@pytest.mark.parametrize(
    ("candidate", "eligible", "route", "reason"),
    [
        (
            _candidate(auxiliary=True),
            False,
            CandidateRoute.NONE,
            CandidateReason.AUXILIARY,
        ),
        (
            _candidate(evidence_level="L2", auxiliary=True),
            False,
            CandidateRoute.NONE,
            CandidateReason.AUXILIARY,
        ),
        (
            _candidate(evidence_level="L2", component_name=None),
            True,
            CandidateRoute.L2_REVIEW,
            CandidateReason.L2_ELIGIBLE,
        ),
        (
            _candidate(reachability_status="unreachable"),
            False,
            CandidateRoute.NONE,
            CandidateReason.L1_UNREACHABLE,
        ),
        (
            _candidate(authorization_status="protected"),
            False,
            CandidateRoute.NONE,
            CandidateReason.L1_PROTECTED,
        ),
        (
            _candidate(authorization_status="strongly_protected"),
            False,
            CandidateRoute.NONE,
            CandidateReason.L1_PROTECTED,
        ),
        (
            _candidate(component_name=""),
            False,
            CandidateRoute.NONE,
            CandidateReason.L1_COMPONENT_NAME_MISSING,
        ),
        (
            _candidate(reachability_status="conditional"),
            True,
            CandidateRoute.L1_TRIAGE,
            CandidateReason.L1_ELIGIBLE,
        ),
        (
            _candidate(evidence_level="L3"),
            False,
            CandidateRoute.NONE,
            CandidateReason.EVIDENCE_LEVEL_UNKNOWN,
        ),
    ],
)
def test_precheck_routes_with_explicit_reason_codes(
    candidate: dict,
    eligible: bool,
    route: CandidateRoute,
    reason: CandidateReason,
) -> None:
    decision = candidate_precheck(candidate)

    assert decision.eligible is eligible
    assert decision.route == route
    assert decision.reason_code == reason


def test_stable_key_ignores_runtime_ai_review_severity_slice_error_and_timestamp_fields() -> None:
    deterministic = _candidate()
    runtime_enriched = {
        **copy.deepcopy(deterministic),
        "analysis_status": "ai_completed",
        "review_status": "pending_manual",
        "manual_review_status": "approved",
        "severity_hint": "critical",
        "severity_reason": ["AI changed this"],
        "risk_severity": "critical",
        "slice_id": "slice-random",
        "context_slice_id": "slice-random-2",
        "slice_refs": ["context-random"],
        "ai_analysis": {"verdict": "confirmed"},
        "ai_analysis_trace": [{"round": 2, "result": {"random": True}}],
        "runtime": {"python": "3.12.9"},
        "duration_ms": 987,
        "created_at": "2026-08-04T00:00:00Z",
        "finished_at": "2026-08-04T00:00:01Z",
        "transient_error": "connection reset",
        "error": {"request_id": "random-request"},
    }

    assert exact_candidate_key(deterministic) == exact_candidate_key(runtime_enriched)


def test_deterministic_rule_facts_remain_part_of_the_key() -> None:
    original = _candidate()
    variants = [
        _candidate(authorization_status="unknown"),
        _candidate(guard_status="present_partial"),
        _candidate(dataflow_status="not_proven"),
        _candidate(reachability_status="conditional"),
        _candidate(review_priority=90),
        _candidate(propagation_paths=[{"from": "intent", "to": "file", "status": "fact"}]),
        _candidate(custom_rule_fact={"verified": False}),
    ]

    assert all(exact_candidate_key(original) != exact_candidate_key(item) for item in variants)


def test_all_public_operations_leave_inputs_unchanged() -> None:
    candidates = [
        _candidate(),
        _candidate(analysis_status="rule_only", slice_id="slice-runtime"),
        _candidate(component_name="Other"),
    ]
    before = copy.deepcopy(candidates)

    candidate_precheck(candidates[0])
    exact_candidate_key(candidates[0])
    deduplicate_exact_candidates(candidates)
    build_candidate_routing_plan(candidates)

    assert candidates == before


def test_deterministic_refutation_basis_local_broadcast_sink() -> None:
    """回归：LocalBroadcastManager/EventBus 进程内分发须提供确定性反证背书。

    3.0.1 run（20260808T155920Z）：AI 正确识别 ShopApp 的 LocalBroadcast 判定为
    refutes，但 decision 层 false_positive_basis=[] 不采信（采信率 0/8）。
    0.3.1：规则层在 sink 记录 receiver_text，decision 层据此给 local_broadcast_intra_process。
    """

    from app.analysis.candidate_funnel import deterministic_refutation_basis

    candidate = {
        "sinks": [{
            "path": "com/xiaomi/shop2/ShopApp.java",
            "line": 450,
            "kind": "implicit_broadcast",
            "receiver_text": "LocalBroadcastManager.getInstance(getAppContext())",
        }],
        "evidence_level": "L2",
        "authorization_status": "unprotected",
        "guard_status": "absent",
        "reachability_status": "reachable",
    }
    basis = deterministic_refutation_basis(candidate)
    assert "local_broadcast_intra_process" in basis

    # 非 LocalBroadcast 的普通 sendBroadcast 不产生该背书
    candidate["sinks"] = [{"receiver_text": "context.sendBroadcast(intent)"}]
    assert "local_broadcast_intra_process" not in deterministic_refutation_basis(candidate)

    # EventBusUtils 等包装类不得误匹配（单词边界，2026-08-09 复审）
    candidate["sinks"] = [{"receiver_text": "EventBusUtils.dispatch(event)"}]
    assert "local_broadcast_intra_process" not in deterministic_refutation_basis(candidate), \
        "EventBusUtils 不是 EventBus，不得误判为进程内分发（否则真漏洞被 ai_false_positive 隐藏）"

    # 无 receiver 信息时不产生（旧候选兼容）
    candidate["sinks"] = [{"kind": "implicit_broadcast"}]
    assert "local_broadcast_intra_process" not in deterministic_refutation_basis(candidate)


def test_control_to_sink_scope_resolved_refutes_in_process_terminus() -> None:
    """S10：control_to_sink（值流未达 sink 实参）在作用域可解析时确定性反证。"""

    from app.analysis.candidate_funnel import (
        deterministic_precheck,
        deterministic_refutation_basis,
    )

    candidate = _candidate(
        evidence_level="L2",
        flow_kind="control_to_sink",
        deterministic_chain_verified=False,
        blocking_gaps=[],
    )
    basis = deterministic_refutation_basis(candidate)
    assert "in_process_terminus" in basis
    assert deterministic_precheck(candidate) == "deterministically_refuted"

    # 作用域未知的 control_to_sink 不直接反证（维持 scope_unresolved 降级路径）。
    candidate["blocking_gaps"] = [{"code": "CONTROL_SCOPE_UNRESOLVED", "critical": True}]
    assert "in_process_terminus" not in deterministic_refutation_basis(candidate)

    # source_to_sink（值流已证明）不得反证。
    candidate["blocking_gaps"] = []
    candidate["flow_kind"] = "source_to_sink"
    assert "in_process_terminus" not in deterministic_refutation_basis(candidate)


def test_binder_surface_candidates_aggregate_by_service() -> None:
    """S11：同一导出 Service 的多 Binder transaction 候选按服务级攻击面聚合。"""

    from app.analysis.candidate_funnel import CandidateFunnel, build_candidate_identity
    from app.findings.aggregate import aggregate_candidates

    def binder_candidate(method_id: str, transaction_code: int, taxonomy: str) -> dict:
        return _candidate(
            evidence_level="L2",
            component="service",
            component_name="com.example.SportXmsService",
            entry_method_id=method_id,
            flow_kind="binder_dispatch",
            binder_remote_interface=True,
            binder_transaction={"code": transaction_code},
            operation_taxonomy=taxonomy,
            guard_status="absent",
            authorization_status="unprotected",
            deterministic_chain_verified=False,
        )

    finish = binder_candidate("SportXmsApiImpl.java#finishSport:1", 4, "sport_state")
    device = binder_candidate("SportXmsApiImpl.java#getDeviceInfo:2", 9, "data_disclosure")
    identity_finish = build_candidate_identity(finish)
    identity_device = build_candidate_identity(device)
    assert (identity_finish.scope_key, identity_finish.chain_key, identity_finish.deterministic_fact_hash) == (
        identity_device.scope_key, identity_device.chain_key, identity_device.deterministic_fact_hash,
    )

    funnel = CandidateFunnel()
    result = funnel.process([finish, device])
    assert result.summary["identity_group_count"] == 1
    assert result.summary["deduplicated_count"] == 1

    findings = aggregate_candidates([
        {**finish, **identity_finish.as_dict()},
        {**device, **identity_device.as_dict()},
    ])
    assert len(findings) == 1
    assert findings[0]["rule_ids"] == ["ACTIVITY_INTENT_TO_SENSITIVE_SINK"]


def test_l2_ai_undecidable_predicate() -> None:
    """S4：纯运行期目标/符号歧义且无 AI 可补证据的 L2 候选判定为不可路由。"""

    from app.analysis.candidate_funnel import _pipeline_l2_ai_undecidable

    base = _candidate(
        evidence_level="L2",
        deterministic_chain_verified=False,
        sources=[],
        sinks=[],
        propagation_paths=[],
        blocking_gaps=[{"code": "ROUTE_TARGET_RESOLUTION_UNVERIFIED", "critical": True}],
    )
    assert _pipeline_l2_ai_undecidable(base) is True

    # 有 source/sink 可补证据（af9745 型、WebView 型）→ 仍送 AI。
    with_evidence = dict(base)
    with_evidence["sources"] = [{"path": "a.java", "line": 1, "kind": "intent_extra"}]
    assert _pipeline_l2_ai_undecidable(with_evidence) is False

    # 有值流事实可交叉验证 → 不判定为不可路由。
    with_facts = dict(base)
    with_facts["call_site_exists"] = True
    assert _pipeline_l2_ai_undecidable(with_facts) is False

    # 非不可判定 gap → 不判定。
    other_gap = dict(base)
    other_gap["blocking_gaps"] = [{"code": "DATAFLOW_NOT_PROVEN", "critical": True}]
    assert _pipeline_l2_ai_undecidable(other_gap) is False


def test_l2_ai_undecidable_route_gated_by_setting() -> None:
    """S4：路由开关默认关闭；开启后也不误路由带证据候选（v2 修正安全边界）。"""

    from app.analysis.candidate_funnel import CandidateFunnel

    # 带 ROUTE_TARGET_RESOLUTION_UNVERIFIED 但有 source/sink 证据（af9745 型）：
    # 即使开关开启仍必须送 AI，避免把"切片不足"误判为"不可判定"。
    candidate = _candidate(
        evidence_level="L2",
        deterministic_chain_verified=False,
        blocking_gaps=[{"code": "ROUTE_TARGET_RESOLUTION_UNVERIFIED", "critical": True}],
    )

    default = CandidateFunnel().process([dict(candidate)]).candidates[0]
    assert default["ai_required"] is True
    assert default.get("ai_routing") is None

    routed = CandidateFunnel({"l2_ai_undecidable_route": True}).process([dict(candidate)]).candidates[0]
    assert routed["ai_required"] is True, (
        "带证据候选不得因运行期目标 gap 被路由掉（v2 修正：WebView/af9745 型仍送 AI）"
    )


def _flow_candidate(chain_id: str, *, sink_line: int = 219, gap_line: int = 100,
                    trace_size: int = 1) -> dict:
    """构造两条语义等价、仅表层细节不同的数据流候选。"""

    return {
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "rule_version": "2.0.0",
        "evidence_level": "L2",
        "component": "activity",
        "component_name": "com.example.MainActivity",
        "entry_points": ["MainActivity#onCreate"],
        "entry_method_id": "com/example/MainActivity.java#MainActivity.onCreate:10",
        "authorization_status": "unprotected",
        "authorization_operation": "component_entry",
        "guard_status": "absent",
        "dataflow_status": "not_proven",
        "flow_kind": "control_to_sink",
        "path_model": "linear_ir_v2",
        "operation_taxonomy": "persistent_state_write",
        "deterministic_chain_verified": False,
        "chain_id": chain_id,
        "sources": [{
            "path": "com/example/MainActivity.java", "line": 12,
            "kind": "intent_extra", "ordinal": 3, "text": "getIntent().getStringExtra(...)",
        }],
        "sinks": [{
            "path": "com/example/PreferenceUtil.java", "line": sink_line,
            "kind": "persistent_state_write", "taxonomy": "persistent_state_write",
            "method_id": "com/example/PreferenceUtil.java#PreferenceUtil.removePref:210",
            "ordinal": 4, "resolve_status": "pending", "text": "editorEdit.apply(...)",
        }],
        "propagation_paths": [{
            "method_id": "com/example/MainActivity.java#MainActivity.onCreate:10",
            "line": 12 + trace_size, "ordinal": trace_size, "text": f"call#{trace_size}",
        }],
        "blocking_gaps": [{
            "code": "LINEAR_IR_PATH_SENSITIVITY_LIMITATION", "critical": True,
            "line": gap_line, "method": f"m{gap_line}",
        }],
        "guard_coverage": {"status": "absent", "checked_line": gap_line},
        # 组件级 trace：同组件所有链共享下发，随链路数量波动
        "method_summaries": {"total": trace_size, "methods": [f"m{i}" for i in range(trace_size)]},
        "reaching_definitions": [{"slot": f"s{i}"} for i in range(trace_size)],
        "locations": [],
    }


def test_identity_merges_semantically_identical_chains() -> None:
    """P0-3：语义相同的链必须合并——chain_id 与行号级噪声不得制造伪差异。"""

    from app.analysis.candidate_funnel import build_candidate_identity

    # 两条候选：入口/source/sink/taxonomy 全同，仅 chain_id、gap 行号、trace 规模不同
    first = _flow_candidate("dfc_aaaa", gap_line=100, trace_size=1)
    second = _flow_candidate("dfc_bbbb", gap_line=205, trace_size=7)

    assert build_candidate_identity(first) == build_candidate_identity(second), (
        "chain_id / gap 行号 / 组件级 trace 规模属于表层差异，"
        "不得阻止语义相同的链合并（否则精确去重 0 生效，重复链耗尽 AI 预算）"
    )


def test_identity_still_separates_different_sinks_and_gap_semantics() -> None:
    """P0-3 安全边界：真实语义差异必须继续区分，不得过度合并。"""

    from app.analysis.candidate_funnel import build_candidate_identity

    base = _flow_candidate("dfc_aaaa")

    other_sink = _flow_candidate("dfc_aaaa", sink_line=221)
    assert build_candidate_identity(base).chain_key != build_candidate_identity(other_sink).chain_key, \
        "不同 sink 行是不同的漏洞终点，必须分组"

    other_gap = copy.deepcopy(base)
    other_gap["blocking_gaps"] = [{"code": "DATAFLOW_NOT_PROVEN", "critical": True}]
    assert (
        build_candidate_identity(base).deterministic_fact_hash
        != build_candidate_identity(other_gap).deterministic_fact_hash
    ), "gap code 不同代表判定依据不同，必须分组"

    other_guard = copy.deepcopy(base)
    other_guard["guard_coverage"] = {"status": "present_effective"}
    assert (
        build_candidate_identity(base).deterministic_fact_hash
        != build_candidate_identity(other_guard).deterministic_fact_hash
    ), "guard status 不同直接影响裁决，必须分组"

    other_component = _flow_candidate("dfc_aaaa")
    other_component["component_name"] = "com.example.OtherActivity"
    assert build_candidate_identity(base).scope_key != build_candidate_identity(other_component).scope_key, \
        "不同组件是不同的攻击面，必须分组"


def test_identity_preserves_propagation_order_without_method_id() -> None:
    """路径节点缺少 method_id 时仍须保序，不能被投影成同一个 None。"""

    from app.analysis.candidate_funnel import build_candidate_identity

    base = _flow_candidate("dfc_aaaa")
    base["propagation_paths"] = [{"ordinal": 1}, {"ordinal": 2}]
    reversed_path = copy.deepcopy(base)
    reversed_path["propagation_paths"].reverse()

    assert build_candidate_identity(base).chain_key != build_candidate_identity(reversed_path).chain_key, \
        "调用顺序是链语义的一部分，无标识节点必须回退到节点内容而非丢弃"


# ---------------------------------------------------------------------------
# T2.7（2026-08-22）：探索候选 candidate_source 三分流与 identity 分源
# ---------------------------------------------------------------------------


def _explorer_candidate(**values) -> dict:
    """归一化形状的探索候选（explorer_normalization 产物最小形状）。"""

    candidate = {
        "rule_id": "EXPLORER_AGENT",
        "rule_version": "explorer/1.0.0",
        "evidence_level": "L2",
        "component": "activity",
        "component_name": "com.example.ExportedActivity",
        "entry_points": ["com.example.ExportedActivity"],
        "entry_method_id": "onCreate",
        "locations": [{"artifact": "code", "path": "Example.java", "line": 10}],
        "sources": [{"path": "Example.java", "line": 10, "kind": "source_expression", "status": "fact"}],
        "sinks": [{"path": "Example.java", "line": 20, "kind": "sink_call", "status": "fact"}],
        "blocking_gaps": [],
        "severity_hint": "medium",
        "confidence_tier": "high",
        "authorization_status": "unknown",
        "guard_status": "unknown",
        "dataflow_status": "not_proven",
        "reachability_status": "reachable",
        "deterministic_chain_verified": False,
        "candidate_source": "explorer",
        "explorer_candidate_id": "expl_aaaaaaaaaaaaaaaaaaaa",
        "explorer_validation_status": "validated",
    }
    candidate.update(values)
    return candidate


def test_explorer_three_way_disposition_routing() -> None:
    """A-10/A-14/N-2：promoted 进 L2 路由送 AI；partial/unverified 不送 AI。"""

    candidates = [
        _explorer_candidate(explorer_candidate_id="expl_promoted00000000000"),
        _explorer_candidate(
            explorer_candidate_id="expl_partial000000000000",
            explorer_validation_status="partially_validated",
            confidence_tier="medium",
        ),
        _explorer_candidate(
            explorer_candidate_id="expl_unverified0000000000",
            explorer_validation_status="unverified",
            confidence_tier="low",
        ),
        _explorer_candidate(
            explorer_candidate_id="expl_weird0000000000000",
            explorer_validation_status="weird",  # N-2：未知档位保守 unverified
        ),
    ]
    result = CandidateFunnel().process(candidates)

    dispositions = [c["funnel_disposition"] for c in result.candidates]
    assert dispositions == [
        "explorer_promoted",
        "explorer_partial",
        "explorer_unverified",
        "explorer_unverified",
    ]
    # promoted 与规则 L2 候选同等路由（deterministic_chain_verified≠True → 送 AI）
    assert result.candidates[0]["ai_required"] is True
    assert result.candidates[0]["ai_eligible"] is True
    assert 0 in result.representative_indexes
    # partial / unverified 不送 AI（不占 AI 预算）
    for index in (1, 2, 3):
        assert result.candidates[index]["ai_required"] is False
        assert result.candidates[index]["ai_eligible"] is False
        assert index not in result.representative_indexes
    # A-14：summary 三档计数
    assert result.summary["explorer_promoted"] == 1
    assert result.summary["explorer_partial"] == 1
    assert result.summary["explorer_unverified"] == 2


def test_explorer_promoted_guard_blocked_skips_ai() -> None:
    """A-7（funnel 侧）：guard_blocked 优先短路——确定性阻断不送 AI。"""

    candidates = [_explorer_candidate(
        explorer_candidate_id="expl_guard0000000000000",
        guard_blocked=True,
        guard_blocks=[{"type": "debuggable", "path": "Example.java", "line": 10, "method": "onCreate"}],
    )]
    result = CandidateFunnel().process(candidates)
    assert result.candidates[0]["funnel_disposition"] == "explorer_promoted"
    assert result.candidates[0]["ai_required"] is False
    assert result.representative_indexes == []


def test_identity_includes_candidate_source_no_cross_source_merge() -> None:
    """A-11：candidate_source 进 facts 投影——探索候选与规则候选不跨源合并。"""

    rule = _flow_candidate("dfc_aaaa")
    explorer = _flow_candidate("dfc_aaaa")
    explorer["candidate_source"] = "explorer"

    rule_identity = build_candidate_identity(rule)
    explorer_identity = build_candidate_identity(explorer)
    assert rule_identity.deterministic_fact_hash != explorer_identity.deterministic_fact_hash, (
        "candidate_source 必须参与 facts 投影（方案 §2.6 identity 含 source），"
        "否则探索候选与规则候选错误共享 AI 复用结果"
    )

    result = CandidateFunnel().process([copy.deepcopy(rule), copy.deepcopy(explorer)])
    assert result.summary["identity_group_count"] == 2


def test_unknown_candidate_source_falls_back_to_rule_path() -> None:
    """N-3：candidate_source 未知值走规则候选默认路径（deterministic_precheck）。"""

    candidate = _explorer_candidate(
        explorer_candidate_id="expl_unknownsrc000000000",
        candidate_source="unknown",
        explorer_validation_status=None,
    )
    # 规则路径：L2 + deterministic_chain_verified≠True → coverage_insufficient
    result = CandidateFunnel().process([candidate])
    assert result.candidates[0]["funnel_disposition"] == "coverage_insufficient"


def test_related_candidate_ids_and_explorer_id_excluded_from_identity() -> None:
    """A-12（identity 安全）：关联回填字段不参与身份——写回后 recompute 一致。"""

    base = _flow_candidate("dfc_aaaa")
    identity_before = build_candidate_identity(base)

    written_back = copy.deepcopy(base)
    written_back["related_candidate_ids"] = ["candidate_xxxxxxxxxxxxxxxxxx"]
    written_back["explorer_candidate_id"] = "expl_bbbbbbbbbbbbbbbbbbbb"

    identity_after = build_candidate_identity(written_back)
    assert identity_after == identity_before, (
        "related_candidate_ids / explorer_candidate_id 是 funnel 后写回的关联字段，"
        "参与身份会导致 recompute 校验（_pipeline_identity_compatible）误判不一致，"
        "阻断 AI 结果向组内成员传播"
    )
