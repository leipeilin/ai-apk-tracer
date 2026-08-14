"""回归：组件级数据流 trace 不得按候选复制，否则触发 RULE_OUTPUT_LIMIT 丢失整族候选。

实测事故（run 20260806T151747Z）：ACTIVITY_INTENT_TO_SENSITIVE_SINK 输出 90.5 MB
超过 10 MiB 上限，规则被 kill，Activity 族候选全部丢失（242 -> 149）。根因是
detector 把 `reaching_definitions` 等组件级 trace 无过滤 update 到每个链路候选上，
单组件 78 条链 x 921KB 直接放大 78 倍。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import WORKSPACE_ROOT

sys.path.insert(0, str(WORKSPACE_ROOT / "rules"))

from shared.detector import (  # noqa: E402
    _TRACE_RECORD_CAP,
    _cap_records,
    _summarize_method_summaries,
    _summarize_reaching_definitions,
)


def _reaching_definitions(count: int) -> list[dict[str, object]]:
    return [
        {
        "value": "v%d" % (index % 40),
      "version": "ver%d" % index,
    "killed_version": "ver%d" % (index - 1) if index else None,
         "state": "tainted" if index % 3 else "trusted",
        "path": "com/example/Very/Long/Package/Name/Activity%d.java" % index,
         "line": index,
}
  for index in range(count)
    ]


def test_reaching_definitions_summary_is_bounded_regardless_of_input_size() -> None:
    """摘要体积必须与输入条目数解耦——事故中单组件累计 53 万条明细。"""

    small = _summarize_reaching_definitions(_reaching_definitions(10))
    huge = _summarize_reaching_definitions(_reaching_definitions(60000))

    small_bytes = len(json.dumps(small, ensure_ascii=False))
    huge_bytes = len(json.dumps(huge, ensure_ascii=False))

    assert huge["total"] == 60000, "必须保留真实总数用于覆盖判断"
    assert len(huge["samples"]) <= 20, "样本必须封顶"
    assert huge_bytes < 8192, f"摘要体积失控：{huge_bytes} bytes"
    assert huge_bytes < small_bytes * 3, "摘要体积不得随输入线性增长"


def test_reaching_definitions_summary_keeps_decision_semantics() -> None:
    """压缩不得丢掉判定所需语义：总数、去重值数、kill 次数与 state 分布。"""

    summary = _summarize_reaching_definitions(_reaching_definitions(100))

    assert summary["total"] == 100
    assert summary["values"] == 40, "去重后的值数量用于判断赋值覆盖面"
    assert summary["killed"] == 99, "strong update / kill 次数必须保留"
    assert set(summary["states"]) == {"tainted", "trusted"}
    assert sum(summary["states"].values()) == 100


def test_cap_records_marks_truncation_explicitly() -> None:
    """截断必须显式标注，不能静默丢数据——静默截断比报错更危险。"""

    records = [{"index": index} for index in range(_TRACE_RECORD_CAP + 500)]
    capped = _cap_records(records)

    assert len(capped) == _TRACE_RECORD_CAP + 1
    marker = capped[-1]
    assert marker["trace_truncated"] is True
    assert marker["total_records"] == _TRACE_RECORD_CAP + 500
    assert marker["retained_records"] == _TRACE_RECORD_CAP

    short = [{"index": index} for index in range(5)]
    assert _cap_records(short) == short, "未超限时不得插入截断标记"


def test_method_summaries_summary_drops_bodies_but_keeps_scale() -> None:
    """方法摘要体不外发，只保留规模与键名供覆盖核对。"""

    summaries = {
     "com/example/Foo;->bar%d()V" % index: {"body": "x" * 4000}
        for index in range(_TRACE_RECORD_CAP + 50)
    }
    summary = _summarize_method_summaries(summaries)

    assert summary["total"] == _TRACE_RECORD_CAP + 50
    assert summary["methods_truncated"] is True
    assert len(summary["methods"]) == _TRACE_RECORD_CAP
    assert "x" * 100 not in json.dumps(summary), "摘要体必须被剔除"


def test_summarizers_tolerate_malformed_input() -> None:
    """规则运行在子进程且输入来自反编译产物，摘要器不得因脏数据抛异常。"""

    assert _cap_records(None) == []
    assert _cap_records("not-a-list") == []
    assert _summarize_method_summaries(None)["total"] == 0
    assert _summarize_reaching_definitions(None)["total"] == 0
    mixed = _summarize_reaching_definitions([{"state": "tainted"}, "junk", None, 42])
    assert mixed["total"] == 4
    assert mixed["states"] == {"tainted": 1}
