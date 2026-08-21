"""回归：确定性 guard 验证器（debuggable 前置检查）。

事故（源码级评判，run 20260808T173259Z）：AI 对 ADBDebugActivity 判
flaw=True + propagation=True（评分 6 高置信），但 handleIntent 第一行
`(getApplicationInfo().flags & 2)==0 → return`（FLAG_DEBUGGABLE）在 release 包
（debuggable=false）下使整条链路不可达——高置信子集 2/6 误报来自此盲区。

M1 审查 §4.1 修复：原 `_latest_index()` glob 真实 runs 目录取"最新 run"的
analysis.sqlite3——全新环境（CI/新 clone）必失败，且测试结果随环境中的 run
变化（T1.5 端到端污染曾致 2 项失败）。现改为 fixture 构造最小索引库
（files 表 + guard 样本），彻底消除环境依赖。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.analysis.guard_verifier import (
    _has_debuggable_guard,
    verify_candidate_guards,
)

GUARD_SOURCE_PATH = "com/xiaomi/shop/yrnsdk/debug/ADBDebugActivity.java"

# 行号布局：L11 落在 onNewIntent（自身无 guard，调用 handleIntent——覆盖
# 一层调用跟随）；L43 落在 handleIntent 的 guard 块内（直接命中）。
_FIXTURE_LINES = [
    "package com.xiaomi.shop.yrnsdk.debug;",                          # 1
    "",                                                                # 2
    "import android.content.Intent;",                                  # 3
    "import android.content.SharedPreferences;",                        # 4
    "",                                                                # 5
    "public class ADBDebugActivity {",                                 # 6
    "",                                                                # 7
    "    private static final String KEY_IS_STAGING = \"is_staging\";",  # 8
    "",                                                                # 9
    "    protected void onNewIntent(Intent intent) {",                 # 10
    "        super.onNewIntent(intent);",                              # 11
    "        handleIntent(intent);",                                   # 12
    "    }",                                                           # 13
    "",                                                                # 14
    "    // 以下填充保持与历史事故样本近似的行号布局。",                    # 15
    "    private static final int MODE_PRIVATE_FALLBACK = 0;",         # 16
    "",                                                                # 17
    "    private boolean checkEnvReady() {",                           # 18
    "        return getApplicationInfo().targetSdkVersion > 0;",       # 19
    "    }",                                                           # 20
    "",                                                                # 21
    "    private void logIntent(Intent intent) {",                     # 22
    "        if (intent == null) {",                                   # 23
    "            return;",                                             # 24
    "        }",                                                       # 25
    "    }",                                                           # 26
    "",                                                                # 27
    "    private int normalizeEnv(int raw) {",                         # 28
    "        return raw < 0 ? 0 : raw;",                               # 29
    "    }",                                                           # 30
    "",                                                                # 31
    "    private void recordStagingFlag(boolean value) {",             # 32
    "        // 占位方法：无 FLAG_DEBUGGABLE 读取",                      # 33
    "    }",                                                           # 34
    "",                                                                # 35
    "    private boolean shouldProceed() {",                           # 36
    "        return checkEnvReady();",                                 # 37
    "    }",                                                           # 38
    "",                                                                # 39
    "    private void handleIntent(Intent intent) {",                  # 40
    "        if ((getApplicationInfo().flags & 2) == 0) {",            # 41
    "            return;",                                             # 42
    "        }",                                                       # 43
    "        if (intent.hasExtra(\"env\")) {",                         # 44
    "            handleEnvSwitch(intent.getIntExtra(\"env\", 0));",    # 45
    "        }",                                                       # 46
    "    }",                                                           # 47
    "",                                                                # 48
    "    private void handleEnvSwitch(int env) {",                     # 49
    "        SharedPreferences prefs = getSharedPreferences(\"s\", 0);",  # 50
    "        prefs.edit().putBoolean(KEY_IS_STAGING, true).apply();",  # 51
    "        recordStagingFlag(env > 0);",                             # 52
    "    }",                                                           # 53
    "}",                                                               # 54
]

# 关键行号常量（与 _FIXTURE_LINES 布局对应）
LINE_IN_ON_NEW_INTENT = 11  # onNewIntent body（无 guard，调用 handleIntent → 跟随命中）
LINE_IN_HANDLE_INTENT = 43  # handleIntent guard 块内（直接命中）


@pytest.fixture()
def guard_index(tmp_path: Path) -> str:
    """构造含 debuggable guard 样本的最小 analysis.sqlite3。

    guard_verifier 只读 files(path, content) 两列——按最小表结构构造，
    不依赖任何真实 run 产物。
    """

    db_path = tmp_path / "analysis.sqlite3"
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE files (path TEXT PRIMARY KEY, content TEXT)")
        con.execute(
            "INSERT INTO files (path, content) VALUES (?, ?)",
            (GUARD_SOURCE_PATH, "\n".join(_FIXTURE_LINES)),
        )
        con.commit()
    finally:
        con.close()
    return str(db_path)


def test_debuggable_guard_detected_in_handleintent() -> None:
    """handleIntent 方法体的 debuggable guard 必须被识别。"""

    body = "\n".join(_FIXTURE_LINES[39:47])  # handleIntent 完整方法体
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


def test_verify_candidate_guards_blocks_on_release_build(guard_index: str) -> None:
    """release 包（manifest_facts 缺失 → 视为 release）：guard 直接命中。"""

    candidate: dict[str, Any] = {
        "sources": [{
            "path": GUARD_SOURCE_PATH,
            "line": LINE_IN_HANDLE_INTENT,
            "kind": "intent_extra",
        }],
        "sinks": [{
            "path": GUARD_SOURCE_PATH,
            "line": 51,  # putBoolean(KEY_IS_STAGING)（handleEnvSwitch 内）
            "kind": "shared_prefs",
        }],
    }
    blocks = verify_candidate_guards(candidate, guard_index)
    assert any(b.get("type") == "debuggable" for b in blocks), f"应识别 debuggable guard: {blocks}"


def test_verify_candidate_guards_follows_one_level_call(guard_index: str) -> None:
    """一层调用跟随：入口方法（onNewIntent）无 guard，但其调用的
    handleIntent 有 guard → 命中且 method 记为被调用方法。"""

    candidate: dict[str, Any] = {
        "sources": [{"path": GUARD_SOURCE_PATH, "line": LINE_IN_ON_NEW_INTENT}],
    }
    blocks = verify_candidate_guards(candidate, guard_index)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "debuggable"
    assert blocks[0]["method"] == "handleIntent"


def test_verify_candidate_guards_no_block_when_debug_build(guard_index: str) -> None:
    """debuggable=true（debug 包）→ guard 不阻断。"""

    candidate: dict[str, Any] = {
        "sources": [{"path": GUARD_SOURCE_PATH, "line": LINE_IN_HANDLE_INTENT}],
        "manifest_facts": {"debuggable": True},
    }
    assert verify_candidate_guards(candidate, guard_index) == []


def test_verify_candidate_guards_skips_missing_index() -> None:
    candidate: dict[str, Any] = {"sources": [{"path": "x.java", "line": 1}]}
    assert verify_candidate_guards(candidate, "/nonexistent/analysis.sqlite3") == []


def test_apply_guard_verification_idempotent(guard_index: str) -> None:
    from app.analysis.guard_verifier import apply_guard_verification

    candidate: dict[str, Any] = {"guard_blocks": [{"type": "debuggable", "path": "p", "line": 1}]}
    out = apply_guard_verification(candidate, guard_index)
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
    def test_non_dict_evidence_ignored(self, guard_index: str) -> None:
        """sources 含非 dict 元素不得崩溃。"""

        candidate: dict[str, Any] = {"sources": [None, 123, "str", {"path": "x.java", "line": 1}]}
        assert verify_candidate_guards(candidate, guard_index) == []

    def test_out_of_range_line_returns_empty(self, guard_index: str) -> None:
        """line 越界/非 int 不崩溃，返回空。"""

        for bad_line in (0, -1, 999999, "abc", None):
            candidate: dict[str, Any] = {
                "sources": [{"path": GUARD_SOURCE_PATH, "line": bad_line}]
            }
            assert verify_candidate_guards(candidate, guard_index) == [], f"line={bad_line!r}"

    def test_nonexistent_path_returns_empty(self, guard_index: str) -> None:
        candidate: dict[str, Any] = {"sources": [{"path": "com/not/exist/File.java", "line": 10}]}
        assert verify_candidate_guards(candidate, guard_index) == []

    def test_sinks_also_checked(self, guard_index: str) -> None:
        """guard 检测同时覆盖 sinks（sink 所在方法也可能带 guard）。"""

        candidate: dict[str, Any] = {
            "sinks": [{"path": GUARD_SOURCE_PATH, "line": 51}],
        }
        # 51 行在 handleEnvSwitch（无 guard）；调用跟随只向上看"被调用的方法"，
        # recordStagingFlag 亦无 guard → 不强制命中，仅验证不崩溃。
        verify_candidate_guards(candidate, guard_index)

    def test_manifest_facts_debuggable_none_treated_as_release(self, guard_index: str) -> None:
        """manifest_facts 缺失或 debuggable=None → 视为 release（保守检测）。"""

        candidate: dict[str, Any] = {
            "sources": [{"path": GUARD_SOURCE_PATH, "line": LINE_IN_HANDLE_INTENT}],
            "manifest_facts": {"debuggable": None},
        }
        blocks = verify_candidate_guards(candidate, guard_index)
        assert any(b.get("type") == "debuggable" for b in blocks)


def test_apply_guard_verification_sets_guard_blocked_flag(guard_index: str) -> None:
    """guard_blocked 布尔标志与 guard_blocks 同写（funnel 跳 AI 用）。"""

    from app.analysis.guard_verifier import apply_guard_verification

    candidate: dict[str, Any] = {
        "sources": [{"path": GUARD_SOURCE_PATH, "line": LINE_IN_HANDLE_INTENT}],
        "manifest_facts": {"debuggable": False},
    }
    out = apply_guard_verification(candidate, guard_index)
    assert out.get("guard_blocked") is True
    assert len(out.get("guard_blocks") or []) >= 1

    # 未命中 guard 时不写标志
    clean: dict[str, Any] = {
        "sources": [{"path": GUARD_SOURCE_PATH, "line": LINE_IN_HANDLE_INTENT}],
        "manifest_facts": {"debuggable": True},  # debug 包 → guard 不阻断
    }
    apply_guard_verification(clean, guard_index)
    assert clean.get("guard_blocked") is None
