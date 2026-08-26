"""规则产物导出测试（T2.1）。

设计：docs/analysis/explorer-track/2026-08-22-t2-1-implementation-plan.md（含评审 R-1~R-7 修订）。
分层：
- detector 层：build_code_index 构造真实 index 路径（复用 test_dynamic_receiver_resolution
  先例——生产 payload 只含 manifest/index/config，legacy files 生产无数据，评审 R-5）；
- rule_runner 层：_export_rule_artifacts 单测（per-record 剔除粒度，评审 R-3）；
- orchestrator 层：_register_rule_artifacts 注册方法（评审 R-6）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from app.analysis.indexer import build_code_index
from app.analysis.rule_runner import RuleRunner
from app.config import RuleRuntimeSettings

RULES_ROOT = Path(__file__).resolve().parents[2] / "rules"
SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
if str(RULES_ROOT) not in sys.path:
    sys.path.insert(0, str(RULES_ROOT))

from shared.detector import (  # noqa: E402
    _binder_bindings_artifact,
    _bound_artifact_records,
    _webview_bridge_artifact_records,
    execute,
)


def _index(tmp_path: Path, sources: dict[str, str]) -> dict:
    """tmp 源码 → 真实 code index（复用 test_dynamic_receiver_resolution 先例）。"""
    source_root = tmp_path / "sources"
    for relative, content in sources.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    index_root = tmp_path / "index"
    descriptor = build_code_index(source_root, index_root / "code-index.json")
    return {**descriptor, "allowed_index_root": index_root.resolve().as_posix()}


def _manifest(**updates: object) -> dict:
    value = {
        "analysis_platform_api": 36,
        "target_sdk": 36,
        "components": [],
        "custom_permissions": {},
        "protected_broadcast_actions": [],
        "protected_broadcast_catalog_version": "test-api-36",
    }
    value.update(updates)
    return value


_RECEIVER_SOURCE = """package com.example;
class DemoReceiver {
  void register(android.content.Context context) {
    DemoReceiver receiver = new DemoReceiver();
    android.content.IntentFilter filter = new android.content.IntentFilter("com.example.SYNC");
    context.registerReceiver(receiver, filter);
  }
  void onReceive(android.content.Context context, android.content.Intent intent) {
    String action = intent.getAction();
    if ("com.example.SYNC".equals(action)) log(intent);
  }
  void log(android.content.Intent intent) {
  }
}
"""

_WEBVIEW_SOURCE = """package com.example;
class WebHelper {
  void setup(android.webkit.WebView view) {
    view.addJavascriptInterface(new JsBridge(), "Bridge1");
    view.addJavascriptInterface(new SafeBridge(), "Bridge2");
  }
}
"""


# ---------------------------------------------------------------------------
# detector 层（index 路径真实执行）
# ---------------------------------------------------------------------------


def test_detector_receiver_artifact_index_path(tmp_path: Path) -> None:
    """评审 R-5：生产 index 路径真实执行——全量导出（含非 reportable）+ schema 通过。"""
    result = execute(
        "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION",
        {"manifest": _manifest(), "index": _index(tmp_path, {"DemoReceiver.java": _RECEIVER_SOURCE})},
    )
    records = result.get("artifacts", {}).get("receiver_registrations")
    assert records, "receiver_registrations 产物应非空"
    schema = json.loads((SCHEMAS_DIR / "receiver_registrations.schema.json").read_text("utf-8"))
    jsonschema.validate({"schema_version": "1.0.0", "registrations": records}, schema)
    # 记录结构关键断言（T0.4 字段名对齐规则侧实际产出）
    first = records[0]
    assert first["path"].endswith("DemoReceiver.java")
    assert first["line"] > 0
    assert "receiver_class" in first and "reportable" in first


def test_detector_receiver_artifact_legacy_path() -> None:
    """legacy 补充用例：手工 file dict（无 index）。"""
    file = {
        "path": "DemoReceiver.java",
        "content": _RECEIVER_SOURCE,
        "methods": [],
    }
    result = execute(
        "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION",
        {"manifest": _manifest(), "code_index": {"files": [file]}},
    )
    # legacy 无 call_sites 结构：registrations 为空数组也必须通过 schema（空产物合法）
    records = result.get("artifacts", {}).get("receiver_registrations", [])
    schema = json.loads((SCHEMAS_DIR / "receiver_registrations.schema.json").read_text("utf-8"))
    jsonschema.validate({"schema_version": "1.0.0", "registrations": records}, schema)


def test_detector_webview_artifact_multi_bridge() -> None:
    """评审 R-7：同文件多桥全枚举（finditer）+ bridge_name 正确 + schema 通过。"""
    result = execute(
        "WEBVIEW_JS_BRIDGE_EXPOSED",
        {"manifest": _manifest(), "code_index": {"files": [
            {"path": "WebHelper.java", "content": _WEBVIEW_SOURCE},
        ]}},
    )
    bridges = result.get("artifacts", {}).get("webview_js_bridges")
    assert bridges and len(bridges) == 2, f"应枚举两个桥: {bridges}"
    names = {item["bridge_name"] for item in bridges}
    assert names == {"Bridge1", "Bridge2"}
    assert all(item["sink_kind"] == "js_bridge" for item in bridges)
    assert all(item["path"] == "WebHelper.java" for item in bridges)
    schema = json.loads((SCHEMAS_DIR / "webview_js_bridges.schema.json").read_text("utf-8"))
    jsonschema.validate({"schema_version": "1.0.0", "bridges": bridges}, schema)


def test_webview_bridge_artifact_skips_commented_calls() -> None:
    """注释行内的 addJavascriptInterface 不进产物（与候选口径一致）。"""
    code = 'package x;\nclass A {\n  void m() {\n    // view.addJavascriptInterface(b, "Nope");\n  }\n}\n'
    records = _webview_bridge_artifact_records(code, {"path": "A.java"})
    assert records == []


# ---------------------------------------------------------------------------
# Binder 组装 helper（评审 R-1/R-2：真实形态 mock + 推导）
# ---------------------------------------------------------------------------


def _mock_binder_batch() -> dict:
    """真实形态 binder_batch：service_class 是 class 记录 dict（评审 R-1）、
    transaction 无 resolve_status 字段（评审 R-2）。"""
    return {
        "com.example.SportService": {
            "service_class": {"qualified_name": "com.example.SportXmsApi", "kind": "class"},
            "transactions": [
                {  # bound：实现已唯一绑定
                    "code": 1, "interface_method": "finishSport",
                    "path": "com/example/ISportApi.java", "line": 42,
                    "implementation_method_id": "com/example/SportImpl.java#finishSport:504",
                    "gaps": [{"code": "BINDER_DISPATCH_TARGET_AMBIGUOUS", "critical": False}],
                },
                {  # ambiguous：实现歧义 gap
                    "code": 2, "interface_method": "ambiguousCall",
                    "path": "com/example/A.java", "line": 7,
                    "gaps": [{"code": "BINDER_IMPLEMENTATION_AMBIGUOUS", "critical": True}],
                },
                {  # unresolved：仅 dispatch 未解析
                    "code": 3, "interface_method": "unresolvedCall",
                    "path": "com/example/B.java", "line": 9,
                    "gaps": [{"code": "BINDER_DISPATCH_TARGET_UNRESOLVED", "critical": True}],
                },
            ],
            "gaps": [],
        },
        "com.example.FallbackService": {  # service_class 缺失 → manifest 名兜底
            "service_class": None,
            "transactions": [{"code": 4, "interface_method": "m", "path": "x.java", "line": 1, "gaps": []}],
            "gaps": [],
        },
    }


def test_binder_bindings_artifact_helper() -> None:
    records = _binder_bindings_artifact(_mock_binder_batch())
    assert len(records) == 4
    by_method = {item["interface_method"]: item for item in records}
    # R-1：qualified_name 注入（class dict 取字符串）；缺失时 manifest 名兜底
    assert by_method["finishSport"]["service_class"] == "com.example.SportXmsApi"
    assert by_method["m"]["service_class"] == "com.example.FallbackService"
    # R-2：resolve_status 推导（implementation 绑定优先于 dispatch 歧义）
    assert by_method["finishSport"]["resolve_status"] == "bound"
    assert by_method["ambiguousCall"]["resolve_status"] == "ambiguous"
    assert by_method["unresolvedCall"]["resolve_status"] == "unresolved"
    schema = json.loads((SCHEMAS_DIR / "binder_bindings.schema.json").read_text("utf-8"))
    jsonschema.validate({"schema_version": "1.0.0", "bindings": records}, schema)


# ---------------------------------------------------------------------------
# 体积预算截断（评审 R-4：ensure_ascii=False 口径）
# ---------------------------------------------------------------------------


def test_artifact_budget_truncation() -> None:
    """超 2 MiB 截断 + gap 保真实总数（CJK 口径估算）。"""
    big_record = {"text": "桥" * 1024, "detail": "x" * 1024}  # 每条约 4 KB（UTF-8 中文 3 字节）
    records = [dict(big_record) for _ in range(1500)]  # 总量 ~6 MiB
    bounded, gaps = _bound_artifact_records("webview_js_bridges", records)
    assert len(bounded) < len(records)
    assert len(bounded) > 0
    assert len(json.dumps(bounded, ensure_ascii=False)) <= 2 * 1024 * 1024
    assert gaps and gaps[0]["code"] == "RULE_ARTIFACT_TRUNCATED"
    assert gaps[0]["total"] == 1500 and gaps[0]["kept"] == len(bounded)
    # 未超限时零 gap
    bounded_all, no_gaps = _bound_artifact_records("webview_js_bridges", records[:10])
    assert len(bounded_all) == 10 and no_gaps == []


# ---------------------------------------------------------------------------
# rule_runner 汇总侧（评审 R-3：per-record 剔除粒度）
# ---------------------------------------------------------------------------


def _make_rule_runner(tmp_path: Path) -> RuleRunner:
    return RuleRunner(RULES_ROOT, RuleRuntimeSettings())


def test_rule_runner_exports_artifacts(tmp_path: Path) -> None:
    runner = _make_rule_runner(tmp_path)
    result = {
        "status": "completed",
        "artifacts": {
            "webview_js_bridges": [
                {"line": 5, "text": 'addJavascriptInterface(b, "A")', "bridge_name": "A",
                 "path": "x.java", "sink_kind": "js_bridge", "description": "d"},
            ],
        },
        "artifact_gaps": [],
    }
    runner._export_rule_artifacts(tmp_path, result)

    assert len(runner.last_artifacts) == 1
    entry = runner.last_artifacts[0]
    assert entry["type"] == "webview_js_bridges"
    assert entry["record_count"] == 1 and entry["truncated"] is False
    payload = json.loads((tmp_path / "rule-results" / "webview_js_bridges.json").read_text("utf-8"))
    assert payload["schema_version"] == "1.0.0"
    assert payload["bridges"][0]["bridge_name"] == "A"


def test_rule_runner_per_record_invalid(tmp_path: Path) -> None:
    """评审 R-3：单条坏记录剔除 + gap——不毒化整产物。"""
    runner = _make_rule_runner(tmp_path)
    result = {
        "status": "completed",
        "artifacts": {
            "webview_js_bridges": [
                {"line": 5, "text": "ok"},  # 合法（description/sink_kind/path/bridge_name 可空）
                {"line": "bad-line"},  # line 类型错（schema: integer）→ 剔除
                {"text": "missing-line"},  # line 缺失（required）→ 剔除
            ],
        },
        "artifact_gaps": [],
    }
    runner._export_rule_artifacts(tmp_path, result)

    payload = json.loads((tmp_path / "rule-results" / "webview_js_bridges.json").read_text("utf-8"))
    assert len(payload["bridges"]) == 1  # 坏记录剔除、好记录保留
    gap_codes = [gap["code"] for gap in runner.last_coverage_gaps]
    assert gap_codes.count("RULE_ARTIFACT_RECORD_INVALID") == 2
    assert all(gap["critical"] is False for gap in runner.last_coverage_gaps)
    assert runner.last_artifacts[0]["record_count"] == 1


def test_rule_runner_skips_unknown_keys(tmp_path: Path) -> None:
    """未知 artifacts 键跳过（协议白名单由 _validate_output 把关，导出侧容错）。"""
    runner = _make_rule_runner(tmp_path)
    runner._export_rule_artifacts(tmp_path, {"status": "completed", "artifacts": {"unknown": []}})
    assert runner.last_artifacts == []
    assert not (tmp_path / "rule-results" / "unknown.json").exists()


def test_rule_runner_truncated_flag(tmp_path: Path) -> None:
    runner = _make_rule_runner(tmp_path)
    result = {
        "status": "completed",
        "artifacts": {"webview_js_bridges": [{"line": 1, "text": "t"}]},
        "artifact_gaps": [{"code": "RULE_ARTIFACT_TRUNCATED", "artifact": "webview_js_bridges", "kept": 1, "total": 9}],
    }
    runner._export_rule_artifacts(tmp_path, result)
    assert runner.last_artifacts[0]["truncated"] is True


# ---------------------------------------------------------------------------
# orchestrator 注册方法（评审 R-6）
# ---------------------------------------------------------------------------


def test_register_rule_artifacts(tmp_path: Path) -> None:
    from app.analysis.orchestrator import ScanOrchestrator

    manifests: dict[str, dict] = {"run-x": {"artifacts": [{"type": "decompile"}]}}
    storage = SimpleNamespace(
        read_manifest=lambda run_id: manifests[run_id],
        write_manifest=lambda run_id, manifest: manifests.__setitem__(run_id, manifest),
    )
    orchestrator = object.__new__(ScanOrchestrator)  # 跳过重构造，仅注入依赖
    orchestrator.storage = storage
    orchestrator.rule_runner = SimpleNamespace(
        last_artifacts=[{"type": "binder_bindings", "path": "rule-results/binder_bindings.json", "record_count": 3}]
    )

    orchestrator._register_rule_artifacts("run-x")

    artifacts = manifests["run-x"]["artifacts"]
    assert [item["type"] for item in artifacts] == ["decompile", "binder_bindings"]

    # 空产物零操作（不写 manifest）
    orchestrator.rule_runner.last_artifacts = []
    orchestrator._register_rule_artifacts("run-x")
    assert len(manifests["run-x"]["artifacts"]) == 2
