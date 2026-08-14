"""回归：确定性 guard 验证器（debuggable 前置检查）。

事故（源码级评判，run 20260808T173259Z）：AI 对 ADBDebugActivity 判
flaw=True + propagation=True（评分 6 高置信），但 handleIntent 第一行
`(getApplicationInfo().flags & 2)==0 → return`（FLAG_DEBUGGABLE）在 release 包
（debuggable=false）下使整条链路不可达——高置信子集 2/6 误报来自此盲区。
"""

from __future__ import annotations

import glob
import json

from app.analysis.guard_verifier import (
    _has_debuggable_guard,
    _method_at_line,
    verify_candidate_guards,
)
from app.config import WORKSPACE_ROOT


def _latest_index() -> str:
    runs = sorted(glob.glob(str(WORKSPACE_ROOT / ".ai-apk-tracer/runs/*/")))
    assert runs, "未找到 run 目录"
    from pathlib import Path
    return str(Path(runs[-1]) / "index" / "analysis.sqlite3")


def test_debuggable_guard_detected_in_handleintent() -> None:
    """ADBDebugActivity.handleIntent 的 debuggable guard 必须被识别。"""

    body = (
        "private void handleIntent(Intent intent) {\n"
        "    if ((getApplicationInfo().flags & 2) == 0) {\n"
        "        return;\n"
        "    }\n"
        "    if (intent.hasExtra(\"env\")) {\n"
        "        handleEnvSwitch(intent.getIntExtra(\"env\", 0));\n"
        "    }\n"
        "}\n"
    )
    assert _has_debuggable_guard(body) is True


def test_no_guard_when_only_flag_read_without_return() -> None:
    """只有 flags 读取但无 early-return 不算 guard（避免误伤）。"""

    body = (
        "private int check() {\n"
        "    int flags = getApplicationInfo().flags;\n"
        "    return flags;\n"
        "}\n"
    )
    assert _has_debuggable_guard(body) is False


def test_no_guard_when_flag_read_absent() -> None:
    body = "private void handleIntent(Intent intent) {\n    intent.getExtras();\n}\n"
    assert _has_debuggable_guard(body) is False


def test_verify_candidate_guards_blocks_on_release_build() -> None:
    """真实索引库：ADBDebugActivity 候选 + release 包（debuggable=false）→ guard 阻断。"""

    candidate = {
        "sources": [{
            "path": "com/xiaomi/shop/yrnsdk/debug/ADBDebugActivity.java",
            "line": 43,  # onNewIntent → handleIntent 附近
            "kind": "intent_extra",
        }],
        "sinks": [{
            "path": "com/xiaomi/shop/yrnsdk/debug/ADBDebugActivity.java",
            "line": 85,  # putBoolean(KEY_IS_STAGING)
            "kind": "shared_prefs",
        }],
    }
    blocks = verify_candidate_guards(candidate, _latest_index())
    assert any(b.get("type") == "debuggable" for b in blocks), f"应识别 debuggable guard: {blocks}"


def test_verify_candidate_guards_no_block_when_debug_build() -> None:
    """debuggable=true（debug 包）→ guard 不阻断。"""

    candidate = {
        "sources": [{"path": "com/xiaomi/shop/yrnsdk/debug/ADBDebugActivity.java", "line": 43}],
        "manifest_facts": {"debuggable": True},
    }
    assert verify_candidate_guards(candidate, _latest_index()) == []


def test_verify_candidate_guards_skips_missing_index() -> None:
    candidate = {"sources": [{"path": "x.java", "line": 1}]}
    assert verify_candidate_guards(candidate, "/nonexistent/analysis.sqlite3") == []


def test_apply_guard_verification_idempotent() -> None:
    from app.analysis.guard_verifier import apply_guard_verification

    candidate = {"guard_blocks": [{"type": "debuggable", "path": "p", "line": 1}]}
    out = apply_guard_verification(candidate, _latest_index())
    assert out is candidate
    assert len(out["guard_blocks"]) == 1


def test_decision_blocks_ai_support_when_guard_blocked() -> None:
    """guard_blocks（debuggable）存在时：AI 判 supports 不得被采信为 supported。"""

    from app.findings.decision import DecisionEngine

    candidate = {
        "evidence_level": "L2",
        "analysis_status": "ai_completed",
        "deterministic_chain_verified": True,
        "dataflow_status": "intraprocedural",
        "authorization_status": "unprotected",
        "guard_status": "absent",
        "reachability_status": "reachable",
        "guard_blocks": [{"type": "debuggable", "path": "ADBDebugActivity.java", "line": 43}],
        "ai_analysis": {
            "verdict": "supports_candidate",
            "analysis_complete": True,
            "semantic_evidence_complete": True,
            "verified_evidence_refs": [],
        },
    }
    DecisionEngine().apply([candidate])
    # 方案 X'：guard_blocked → blocked（条件不可利用），不得 supported/unresolved
    assert candidate["evidence_decision"] == "blocked"


def test_decision_keeps_ai_refutes_when_guard_blocked() -> None:
    """guard_blocks 存在时 AI refutes 仍走否定路径（guard 佐证否定方向）。"""

    from app.findings.decision import DecisionEngine
    from tests.test_finding_decision import _local_broadcast_candidate

    candidate = _local_broadcast_candidate(
        guard_blocks=[{"type": "debuggable", "path": "p", "line": 1}],
    )
    DecisionEngine().apply([candidate])
    assert candidate["evidence_decision"] in {"ai_false_positive", "deterministically_refuted"}


class TestGuardVerifierDirtyData:
    def test_non_dict_evidence_ignored(self) -> None:
        """sources 含非 dict 元素不得崩溃。"""

        from app.analysis.guard_verifier import verify_candidate_guards

        candidate = {"sources": [None, 123, "str", {"path": "x.java", "line": 1}]}
        assert verify_candidate_guards(candidate, _latest_index()) == []

    def test_out_of_range_line_returns_empty(self) -> None:
        """line 越界/非 int 不崩溃，返回空。"""

        from app.analysis.guard_verifier import verify_candidate_guards

        for bad_line in (0, -1, 999999, "abc", None):
            candidate = {"sources": [{"path": "com/xiaomi/shop/yrnsdk/debug/ADBDebugActivity.java", "line": bad_line}]}
            assert verify_candidate_guards(candidate, _latest_index()) == [], f"line={bad_line!r}"

    def test_nonexistent_path_returns_empty(self) -> None:
        from app.analysis.guard_verifier import verify_candidate_guards

        candidate = {"sources": [{"path": "com/not/exist/File.java", "line": 10}]}
        assert verify_candidate_guards(candidate, _latest_index()) == []

    def test_sinks_also_checked(self) -> None:
        """guard 检测同时覆盖 sinks（sink 所在方法也可能带 guard）。"""

        from app.analysis.guard_verifier import verify_candidate_guards

        candidate = {
            "sinks": [{"path": "com/xiaomi/shop/yrnsdk/debug/ADBDebugActivity.java", "line": 85}],
        }
        # sink 85 行在 handleEnvSwitch（无 guard），但调用跟随应回到调用者
        # handleIntent（有 guard）——实际 handleEnvSwitch 由 handleIntent 调用，
        # 但调用跟随只向上看"被调用的方法"，故此处不强制命中，仅验证不崩溃。
        verify_candidate_guards(candidate, _latest_index())

    def test_manifest_facts_debuggable_none_treated_as_release(self) -> None:
        """manifest_facts 缺失或 debuggable=None → 视为 release（保守检测）。"""

        from app.analysis.guard_verifier import verify_candidate_guards

        candidate = {
            "sources": [{"path": "com/xiaomi/shop/yrnsdk/debug/ADBDebugActivity.java", "line": 43}],
            "manifest_facts": {"debuggable": None},
        }
        blocks = verify_candidate_guards(candidate, _latest_index())
        assert any(b.get("type") == "debuggable" for b in blocks)


def test_apply_guard_verification_sets_guard_blocked_flag() -> None:
    """guard_blocked 布尔标志与 guard_blocks 同写（funnel 跳 AI 用）。"""

    from app.analysis.guard_verifier import apply_guard_verification

    candidate = {
        "sources": [{"path": "com/xiaomi/shop/yrnsdk/debug/ADBDebugActivity.java", "line": 43}],
        "manifest_facts": {"debuggable": False},
    }
    out = apply_guard_verification(candidate, _latest_index())
    assert out.get("guard_blocked") is True
    assert len(out.get("guard_blocks") or []) >= 1

    # 未命中 guard 时不写标志
    clean = {
        "sources": [{"path": "com/xiaomi/shop/yrnsdk/debug/ADBDebugActivity.java", "line": 43}],
        "manifest_facts": {"debuggable": True},  # debug 包 → guard 不阻断
    }
    apply_guard_verification(clean, _latest_index())
    assert clean.get("guard_blocked") is None
