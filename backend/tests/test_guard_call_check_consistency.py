"""调用者身份校验 API 三处一致性参数化测试（结构性防线）。

背景（v2026-08-09）："手动同步点"教训已复发三次——新增调用者校验 API
（如 getNameForUid/getPackageInfo）时，dataflow.GUARD_METHODS、indexer 的
guard_names、context_builder.GUARD_PATTERN 三处独立副本必须同步，否则出现
"规则识别了校验但索引/AI 切片看不到"的静默不一致。

本测试定义调用者身份校验 API 核心集，参数化断言三处全部覆盖：

- rules/shared/dataflow.py `GUARD_METHODS`：guard 判定（fail-closed 证明）
- backend/app/analysis/indexer.py `GUARD_CALLER_CHECK_METHODS`：索引方法摘要 guards 标记
- backend/app/analysis/context_builder.py `GUARD_PATTERN`：切片 guard_candidate 提示

任一处漏新增 API，对应参数化用例立即失败，杜绝手补遗漏。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

RULES_ROOT = Path(__file__).resolve().parents[2] / "rules"
if str(RULES_ROOT) not in sys.path:
    sys.path.insert(0, str(RULES_ROOT))

from shared.dataflow import GUARD_METHODS  # noqa: E402


# 调用者身份校验 API 核心集：三处都必须识别的"调用者是谁"校验原语。
# 注意这是 GUARD_METHODS 的子集（不含 enforceReadPermission/enforceWritePermission
# 等资源权限强制 API——它们只在 dataflow 判定层需要，索引/AI 切片不要求）。
CALLER_CHECK_METHODS = frozenset({
    "checkCallingPermission",
    "enforceCallingPermission",
    "checkCallingOrSelfPermission",
    "enforceCallingOrSelfPermission",
    "checkSignatures",
    "checkUidSignatures",
    "getNameForUid",
    "getPackageInfo",
})


@pytest.mark.parametrize("method", sorted(CALLER_CHECK_METHODS))
def test_dataflow_guard_methods_covers_caller_check(method: str) -> None:
    """dataflow.GUARD_METHODS 必须识别全部调用者身份校验 API（guard 判定）。"""
    assert method in GUARD_METHODS, f"{method} 缺失于 dataflow.GUARD_METHODS"


@pytest.mark.parametrize("method", sorted(CALLER_CHECK_METHODS))
def test_indexer_guard_caller_check_covers(method: str) -> None:
    """indexer.GUARD_CALLER_CHECK_METHODS 必须识别全部调用者身份校验 API（索引摘要标记）。"""
    from app.analysis.indexer import GUARD_CALLER_CHECK_METHODS

    assert method in GUARD_CALLER_CHECK_METHODS, f"{method} 缺失于 indexer.GUARD_CALLER_CHECK_METHODS"


@pytest.mark.parametrize("method", sorted(CALLER_CHECK_METHODS))
def test_context_builder_pattern_matches_caller_check(method: str) -> None:
    """context_builder.GUARD_PATTERN 必须匹配全部调用者身份校验 API（切片 guard 提示）。"""
    from app.analysis.context_builder import GUARD_PATTERN

    assert GUARD_PATTERN.search(f"{method}("), f"{method} 未出现在 context_builder.GUARD_PATTERN"


def test_indexer_constant_is_subset_of_dataflow_guard_methods() -> None:
    """indexer 常量是 dataflow.GUARD_METHODS 的"调用者校验"子集：
    只允许 dataflow 独有的资源权限 API（enforceReadPermission 等），不允许反向缺失。"""
    from app.analysis.indexer import GUARD_CALLER_CHECK_METHODS

    assert GUARD_CALLER_CHECK_METHODS <= GUARD_METHODS, (
        f"indexer 常量含 dataflow.GUARD_METHODS 没有的 API: "
        f"{sorted(GUARD_CALLER_CHECK_METHODS - GUARD_METHODS)}"
    )


def test_pattern_tokens_stay_within_guard_semantics() -> None:
    """GUARD_PATTERN 的纯校验方法名 token（排除 SecurityException/requireNotNull/
    validate/whitelist/allowlist 等通用防御词与 Binder 前缀）应落在
    GUARD_METHODS ∪ 身份来源 ∪ 通用防御词范围内，防止把无关方法名误当 guard。"""
    from app.analysis.context_builder import GUARD_PATTERN

    tokens = set(re.findall(r"(?:Binder\\.)?([A-Za-z]+)", GUARD_PATTERN.pattern))
    allowed = GUARD_METHODS | {"getCallingUid", "getCallingPid"} | {
        "SecurityException", "requireNotNull", "validate", "whitelist", "allowlist",
    }
    unknown = tokens - allowed
    assert not unknown, f"GUARD_PATTERN 含未知 token: {sorted(unknown)}"


def test_unresolved_business_calls_do_not_block_guard_closure() -> None:
    """S8：未解析的业务调用（DI 工厂/getter）不是 caller check wrapper，
    不得产生 GUARD_CALL_TARGET_UNRESOLVED 阻塞确定性闭合（SportXms 实证）。"""

    from shared.dataflow import DataFlowAnalyzer

    method = {
        "id": "com.example.SportXmsApiImpl.finishSport",
        "name": "finishSport",
        "qualified_class": "com.example.SportXmsApiImpl",
        "path": "SportXmsApiImpl.java",
        "start_line": 1,
        "end_line": 20,
        "content": "",
        "flow_ir": [],
        "call_sites": [
            {"ordinal": 1, "method_name": "getSportType", "receiver_type": "com.example.SportXmsFinishData",
             "resolve_status": "ambiguous", "start_line": 3, "end_line": 3, "arguments": []},
            {"ordinal": 2, "method_name": "getInstance", "receiver_type": "com.example.SportManagerExtKt",
             "resolve_status": "ambiguous", "start_line": 4, "end_line": 4, "arguments": []},
            {"ordinal": 3, "method_name": "getInstance", "receiver_type": "com.example.DeviceManagerExtKt",
             "resolve_status": "ambiguous", "start_line": 5, "end_line": 5, "arguments": []},
        ],
    }
    analyzer = DataFlowAnalyzer([{"path": "SportXmsApiImpl.java", "methods": [method]}])
    outcome = analyzer.guard_segment("com.example.SportXmsApiImpl.finishSport", boundary_ordinal=99)
    assert outcome["status"] == "absent"
    assert all(gap.get("code") != "GUARD_CALL_TARGET_UNRESOLVED" for gap in outcome["blocking_gaps"])


def test_unresolved_caller_identity_wrapper_still_conservative() -> None:
    """S8：真正的调用者身份 wrapper（Context receiver / checkCaller 命名）未解析时
    仍保留 GUARD_CALL_TARGET_UNRESOLVED 保守 gap。"""

    from shared.dataflow import DataFlowAnalyzer

    def outcome_for(call_sites):
        method = {
            "id": "com.example.Demo.run",
            "name": "run",
            "qualified_class": "com.example.Demo",
            "path": "Demo.java",
            "start_line": 1,
            "end_line": 20,
            "content": "",
            "flow_ir": [],
            "call_sites": call_sites,
        }
        analyzer = DataFlowAnalyzer([{"path": "Demo.java", "methods": [method]}])
        return analyzer.guard_segment("com.example.Demo.run", boundary_ordinal=99)

    context_wrapper = outcome_for([{
        "ordinal": 1, "method_name": "verifyCaller", "receiver_type": "android.content.Context",
        "resolve_status": "unresolved", "start_line": 2, "end_line": 2, "arguments": [],
    }])
    assert any(gap.get("code") == "GUARD_CALL_TARGET_UNRESOLVED" for gap in context_wrapper["blocking_gaps"])

    named_wrapper = outcome_for([{
        "ordinal": 1, "method_name": "checkCaller", "receiver_type": "com.example.Demo",
        "resolve_status": "ambiguous", "start_line": 2, "end_line": 2, "arguments": [],
    }])
    assert any(gap.get("code") == "GUARD_CALL_TARGET_UNRESOLVED" for gap in named_wrapper["blocking_gaps"])
