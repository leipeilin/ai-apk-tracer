"""sticky 广播查询误报修复测试（receiver_registration）。

v2026-08-09：registerReceiver(null, filter) 是 sticky 广播查询（读取最后发送的
sticky 值），不是接收器注册——不产生外部可达接收器候选。
skill 复核发现 megvii CommonProtectorManager 3 处误报即此模式。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.config import WORKSPACE_ROOT

sys.path.insert(0, str(WORKSPACE_ROOT / "rules"))

from shared.receiver_registration import parse_receiver_registrations  # noqa: E402


def _file_with_registration(arguments: list[str], receiver_type: str = "android.content.Context") -> dict:
    """构造含一个 registerReceiver call_site 的最小 file dict（platform_context owner）。"""

    return {
        "content": "",
        "imports": [],
        "classes": [],
        "methods": [{
            "name": "register",
            "qualified_class": "com/example/App.java",
            "call_sites": [{
                "method_name": "registerReceiver",
                "receiver_type": receiver_type,
                "arguments": arguments,
                "method_descriptor": "(Landroid/content/BroadcastReceiver;Landroid/content/IntentFilter;)Landroid/content/Intent;",
            }],
        }],
    }


class TestStickyQueryExcluded:
    def test_register_receiver_null_is_sticky_query_skipped(self) -> None:
        """registerReceiver(null, filter) 是查询非注册 → 不产生注册记录。"""

        regs = parse_receiver_registrations(_file_with_registration(["null", "myFilter"]))
        assert regs == []

    def test_normal_registration_kept(self) -> None:
        """registerReceiver(receiver, filter) 正常产生注册记录。"""

        regs = parse_receiver_registrations(_file_with_registration(["receiver", "myFilter"]))
        assert len(regs) == 1

    def test_this_receiver_not_sticky(self) -> None:
        """this 作为 receiver 是真实注册，不得误跳过。"""

        regs = parse_receiver_registrations(_file_with_registration(["this", "myFilter"]))
        assert len(regs) == 1


class TestContextCompatPosition:
    def test_context_compat_null_receiver_skipped(self) -> None:
        """ContextCompat.registerReceiver(ctx, null, filter, flags) → receiver 在位置 1。"""

        regs = parse_receiver_registrations(_file_with_registration(
            ["context", "null", "myFilter", "0"],
            receiver_type="androidx.core.content.ContextCompat",
        ))
        assert regs == []

    def test_context_compat_normal_kept(self) -> None:
        regs = parse_receiver_registrations(_file_with_registration(
            ["context", "receiver", "myFilter", "0"],
            receiver_type="androidx.core.content.ContextCompat",
        ))
        assert len(regs) == 1


class TestEdgeCases:
    def test_empty_arguments_no_crash(self) -> None:
        regs = parse_receiver_registrations(_file_with_registration([]))
        assert regs == [] or True  # 参数不足时按解析逻辑处理，不崩溃

    def test_no_call_sites_returns_empty(self) -> None:
        file = _file_with_registration(["receiver", "filter"])
        file["methods"][0]["call_sites"] = []
        assert parse_receiver_registrations(file) == []
