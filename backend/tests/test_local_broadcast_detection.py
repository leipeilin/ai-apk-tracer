"""LocalBroadcast/EventBus 进程内分发检测的完整测试覆盖。

背景（2026-08-09 复审）：单词边界正则曾三处不一致——candidate_funnel.py 与
detector.py 均无 \\b，EventBusUtils 包装类被误判为进程内分发，危害链为
basis 误报 → coverage 白名单保留 → ai_false_positive 隐藏真漏洞（最危险错误）。
上一轮仅手工验证，本文件将以下行为全部自动化锁死：

- A. 规则层 _implicit_broadcast_flow 的 effect_verified 降级行为（核心盲区）
- B. candidate_funnel._sink_is_local_broadcast 函数级脏数据
- C. decision._has_sdk_semantic_refutation 函数级脏数据
- D. 端到端负向链路：EventBusUtils + AI refutes 不得产生 ai_false_positive
- E. 规则层与 backend 层正则对同一批样本输出一致（防未来再分裂）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.analysis.candidate_funnel import (
    LOCAL_BROADCAST_RECEIVER_RE,
    deterministic_refutation_basis,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rules"))

from shared.detector import _implicit_broadcast_flow  # noqa: E402

# 规则层内联正则（必须与 LOCAL_BROADCAST_RECEIVER_RE 手动同步——跨层无法共享模块）
RULE_LAYER_RE = __import__("re").compile(r"\bLocalBroadcastManager\b|\bEventBus\b")

# 行为矩阵：receiver 文本 → 是否应识别为进程内分发
_BEHAVIOR_MATRIX: list[tuple[str, bool]] = [
    ("LocalBroadcastManager.getInstance(getAppContext())", True),
    ("EventBus.getDefault()", True),
    ("androidx.localbroadcastmanager.content.LocalBroadcastManager.getInstance(ctx)", True),
    ("EventBusUtils.dispatch(event)", False),            # 包装类，本次修复核心
    ("EventBusManager.post(event)", False),              # 类名含 EventBus 子串
    ("context.sendBroadcast(intent)", False),
    ("this.mContext", False),
    ("mLocalBroadcastManager.send(...)", False),         # 小写变量名，保守方向
    ("", False),
    ("none", False),                                     # str(None)
]


def _make_file(receiver_text: str) -> dict:
    """构造能触发 _implicit_broadcast_flow 的 file dict（敏感字段 + sendBroadcast）。"""

    return {
        "path": "com/example/Sample.java",
        "methods": [{
            "name": "sendLoginBroadcast",
            "start_line": 1,
            "content": 'intent.putExtra("password", pwd)\ncontext.sendBroadcast(intent)\n',
            "call_sites": [
                {"method_name": "putExtra", "receiver_text": "intent", "start_line": 1},
                {
                    "method_name": "sendBroadcast",
                    "receiver_text": receiver_text,
                    "arguments": ["intent"],
                    "start_line": 2,
                },
            ],
        }],
    }


# ---------- A. 规则层 effect_verified 降级行为 ----------

class TestRuleLayerEffectVerification:
    """_implicit_broadcast_flow：进程内分发降级 effect_verified=False。"""

    @pytest.mark.parametrize(
        ("receiver", "is_intra"),
        _BEHAVIOR_MATRIX,
        ids=[f"{r[:32]}:{'intra' if e else 'external'}" for r, e in _BEHAVIOR_MATRIX],
    )
    def test_effect_verified_matches_behavior_matrix(self, receiver: str, is_intra: bool) -> None:
        flow = _implicit_broadcast_flow(_make_file(receiver))
        assert flow is not None, f"receiver={receiver!r} 应产生 dataflow"
        assert flow["sink"]["effect_verified"] is (not is_intra), (
            f"receiver={receiver!r}：进程内分发应 effect_verified=False，"
            f"跨进程应 True（当前 {flow['sink']['effect_verified']}）"
        )
        # sink 必须携带 receiver_text（decision 层反证依据）
        assert flow["sink"]["receiver_text"] == receiver

    def test_rule_flow_none_receiver_returns_none(self) -> None:
        # receiver_text 缺失（send.get 返回 None）→ str(None) 不匹配 → 不降级
        file = _make_file("ignored")
        file["methods"][0]["call_sites"][1]["receiver_text"] = None
        flow = _implicit_broadcast_flow(file)
        assert flow is not None
        assert flow["sink"]["effect_verified"] is True
        assert flow["sink"]["receiver_text"] is None


# ---------- B. candidate_funnel._sink_is_local_broadcast 函数级脏数据 ----------

class TestSinkIsLocalBroadcastDirtyData:
    def test_empty_and_missing_sinks(self) -> None:
        # 空/缺失 sinks → 不产生反证背书，且不崩溃
        assert "local_broadcast_intra_process" not in deterministic_refutation_basis({"sinks": []})
        assert "local_broadcast_intra_process" not in deterministic_refutation_basis({})
        assert LOCAL_BROADCAST_RECEIVER_RE.search("") is None

    @pytest.mark.parametrize("bad_sink", [None, 123, "str", {"no_receiver": 1}, {"receiver_text": None}])
    def test_dirty_sink_elements(self, bad_sink: object) -> None:
        # 不应抛异常（search 必须容忍非字符串 receiver）
        candidate = {"sinks": [bad_sink]}
        deterministic_refutation_basis(candidate)

    def test_basis_no_local_broadcast_for_wrapper_class(self) -> None:
        """EventBusUtils 包装类不得进入 basis（决策层采信的前提是 basis 干净）。"""

        candidate = {
            "sinks": [{"receiver_text": "EventBusUtils.dispatch(event)"}],
            "evidence_level": "L2",
            "authorization_status": "unprotected",
            "guard_status": "absent",
            "reachability_status": "reachable",
        }
        assert "local_broadcast_intra_process" not in deterministic_refutation_basis(candidate)


# ---------- C. decision._has_sdk_semantic_refutation 函数级脏数据 ----------

class TestHasSdkSemanticRefutationDirtyData:
    def test_dirty_sink_elements_no_crash(self) -> None:
        from app.findings.decision import _has_sdk_semantic_refutation

        for bad in (
            {"sinks": [None]},
            {"sinks": [123]},
            {"sinks": [{"receiver_text": None}]},
            {"sinks": [{"receiver_text": 123}]},
            {"sinks": []},
            {"sinks": "not-a-list"},
            {},
        ):
            assert _has_sdk_semantic_refutation(bad) is False, f"脏数据 {bad!r} 应为 False"


# ---------- D. 端到端负向链路：EventBusUtils + AI refutes ----------

class TestEndToEndNegativePath:
    def _refutes_candidate(self, receiver_text: str) -> dict:
        from tests.test_finding_decision import _local_broadcast_candidate

        # 复用已有 helper：默认 LocalBroadcast 候选 → 覆盖为包装类 + AI refutes
        return _local_broadcast_candidate(sinks=[{"receiver_text": receiver_text}])

    def test_wrapper_class_refutes_not_marked_false_positive(self) -> None:
        """EventBusUtils + AI refutes：basis 为空 → 不得 ai_false_positive（防隐藏真漏洞）。"""

        from app.findings.decision import DecisionEngine

        candidate = self._refutes_candidate("EventBusUtils.dispatch(event)")
        DecisionEngine().apply([candidate])
        assert candidate["evidence_decision"] != "ai_false_positive", (
            "EventBusUtils 包装类不是进程内分发，AI refutes 不得被 SDK 反证背书为确认误报"
        )

    def test_true_local_broadcast_still_refuted(self) -> None:
        """对照：真 LocalBroadcast + AI refutes → ai_false_positive 仍落地（修复未破坏正路径）。"""

        from app.findings.decision import DecisionEngine

        candidate = self._refutes_candidate("LocalBroadcastManager.getInstance(getAppContext())")
        DecisionEngine().apply([candidate])
        assert candidate["evidence_decision"] == "ai_false_positive"


# ---------- E. 规则层与 backend 层正则一致性 ----------

class TestRegexConsistencyAcrossLayers:
    @pytest.mark.parametrize(
        ("receiver", "is_intra"),
        _BEHAVIOR_MATRIX,
        ids=[f"{r[:32]}:{'intra' if e else 'external'}" for r, e in _BEHAVIOR_MATRIX],
    )
    def test_rule_layer_matches_backend_layer(self, receiver: str, is_intra: bool) -> None:
        backend_hit = bool(LOCAL_BROADCAST_RECEIVER_RE.search(receiver))
        rule_hit = bool(RULE_LAYER_RE.search(receiver))
        assert backend_hit == rule_hit == is_intra, (
            f"receiver={receiver!r}：backend={backend_hit} rule={rule_hit} 期望={is_intra} —— "
            "规则层与 backend 层正则不一致，须手动同步"
        )
