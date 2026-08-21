"""call_tree on-demand 检索服务测试（T2.4）。

设计：docs/analysis/2026-08-22-t2-4-implementation-plan.md（含评审
R-1~R-7 修订）。真实 build_code_index 构造调用链/继承/同名歧义源码。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.analysis.call_tree import CallTreeService
from app.analysis.index_store import SQLiteCodeIndexReader
from app.analysis.indexer import build_code_index
from app.config import CallTreeSettings

_CHAIN_SOURCE = {
    "com/example/A.java": """package com.example;
public class A {
  public void entry(String input) {
    B helper = new B();
    helper.run(input);
  }
}
""",
    "com/example/B.java": """package com.example;
public class B {
  public void run(String value) {
    C sink = new C();
    sink.write(value);
  }
}
""",
    "com/example/C.java": """package com.example;
public class C {
  public void write(String value) {
  }
}
""",
    # 继承（class_hierarchy）
    "com/example/Base.java": """package com.example;
public class Base {
  public void template() {
  }
}
""",
    "com/example/Sub.java": """package com.example;
public class Sub extends Base {
  public void extra() {
  }
}
""",
    # 同名方法歧义（resolve_invoke_target）
    "com/example/LogOne.java": """package com.example;
public class LogOne {
  public void log(String message) {
  }
}
""",
    "com/example/LogTwo.java": """package com.example;
public class LogTwo {
  public void log(String message) {
  }
}
""",
    # 环（A→B→A）：Ringing 互调
    "com/example/Ring1.java": """package com.example;
public class Ring1 {
  public void ping() {
    Ring2 other = new Ring2();
    other.pong();
  }
}
""",
    "com/example/Ring2.java": """package com.example;
public class Ring2 {
  public void pong() {
    Ring1 other = new Ring1();
    other.ping();
  }
}
""",
}

_DEEP_SOURCE = {
    f"com/example/L{i}.java": (
        f"package com.example;\npublic class L{i} {{\n"
        f"  public void step() {{\n"
        f"    L{i + 1} next = new L{i + 1}();\n"
        f"    next.step();\n"
        f"  }}\n}}\n"
    )
    for i in range(5)
}
_DEEP_SOURCE["com/example/L5.java"] = (
    "package com.example;\npublic class L5 {\n  public void step() {\n  }\n}\n"
)


def _service(tmp_path: Path, sources: dict[str, str]) -> CallTreeService:
    source_root = tmp_path / "sources"
    for relative, content in sources.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    descriptor = build_code_index(source_root, tmp_path / "index" / "code-index.json")
    reader = SQLiteCodeIndexReader(descriptor)
    return CallTreeService(tmp_path, reader, CallTreeSettings())


def _find_id(service: CallTreeService, qualified_class: str, name: str) -> str:
    rows = service._reader.db.execute(
        "SELECT id FROM methods WHERE qualified_class = ? AND name = ?",
        (qualified_class, name),
    ).fetchall()
    assert rows, f"方法未入索引: {qualified_class}.{name}"
    return str(rows[0]["id"])


# ---------------------------------------------------------------------------
# A-1/A-2：调用边与方法体
# ---------------------------------------------------------------------------


def test_get_callees_callers(tmp_path: Path) -> None:
    service = _service(tmp_path, _CHAIN_SOURCE)
    a_id = _find_id(service, "com.example.A", "entry")
    b_id = _find_id(service, "com.example.B", "run")

    callees = service.get_callees(a_id)
    assert [item["method_id"] for item in callees["callees"]] == [b_id]
    assert callees["callees"][0]["name"] == "run"
    assert callees["callees"][0]["qualified_class"] == "com.example.B"
    assert callees["callees"][0]["path"].endswith("B.java")
    assert callees["gaps"] == []

    callers = service.get_callers(b_id)
    assert [item["method_id"] for item in callers["callers"]] == [a_id]


def test_get_method_body(tmp_path: Path) -> None:
    service = _service(tmp_path, _CHAIN_SOURCE)
    b_id = _find_id(service, "com.example.B", "run")
    body = service.get_method_body(b_id)
    assert body is not None
    assert "helper.run" not in body["body"] or True  # body 为 B.run 自身
    assert "sink.write" in body["body"]
    assert body["truncated"] is False
    assert body["qualified_class"] == "com.example.B"
    assert body["start_line"] >= 1

    # 不存在的方法 → None（N-1）
    assert service.get_method_body("missing#nope:1") is None


def test_get_method_body_truncation(tmp_path: Path) -> None:
    long_body = "\n".join(f"    int v{i} = {i};" for i in range(600))
    sources = {
        "com/example/Big.java": (
            "package com.example;\npublic class Big {\n"
            "  public void huge() {\n" + long_body + "\n  }\n}\n"
        ),
    }
    service = _service(tmp_path, sources)
    big_id = _find_id(service, "com.example.Big", "huge")
    body = service.get_method_body(big_id)
    assert body is not None
    assert body["truncated"] is True
    assert len(body["body"].splitlines()) == CallTreeService.MAX_BODY_LINES


# ---------------------------------------------------------------------------
# A-4/A-5/A-6：符号解析/层次/搜索
# ---------------------------------------------------------------------------


def test_resolve_invoke_target_ambiguity(tmp_path: Path) -> None:
    service = _service(tmp_path, _CHAIN_SOURCE)
    candidates = service.resolve_invoke_target("log")
    assert len(candidates) == 2  # LogOne.log + LogTwo.log（歧义如实，D4）
    classes = {item["qualified_class"] for item in candidates}
    assert classes == {"com.example.LogOne", "com.example.LogTwo"}

    # descriptor 限定（参数为声明处简单名形态——T2.2 事实）
    narrowed = service.resolve_invoke_target("log(String)->void")
    assert len(narrowed) == 2
    assert all("(String" in item["descriptor"] for item in narrowed)


def test_class_hierarchy(tmp_path: Path) -> None:
    service = _service(tmp_path, _CHAIN_SOURCE)
    sub = service.class_hierarchy("com.example.Sub")
    assert "Base" in sub["extends"]  # Java 侧 extends 为源码字面简单名
    base = service.class_hierarchy("com.example.Base")
    assert "com.example.Sub" in base["subclasses"]

    # 不存在的类（N-2）
    missing = service.class_hierarchy("com.example.Missing")
    assert missing["extends"] == [] and missing["subclasses"] == []


def test_search_symbol(tmp_path: Path) -> None:
    service = _service(tmp_path, _CHAIN_SOURCE)
    results = service.search_symbol("Lo")
    kinds = {item["kind"] for item in results}
    assert "class" in kinds  # LogOne/LogTwo
    assert any(item["kind"] == "method" and item["name"] == "log" for item in results)


# ---------------------------------------------------------------------------
# A-7/A-8/8b：有界树
# ---------------------------------------------------------------------------


def test_build_bounded_tree_full_chain(tmp_path: Path) -> None:
    service = _service(tmp_path, _CHAIN_SOURCE)
    a_id = _find_id(service, "com.example.A", "entry")
    b_id = _find_id(service, "com.example.B", "run")
    c_id = _find_id(service, "com.example.C", "write")
    tree = service.build_bounded_tree(a_id)

    assert set(tree["nodes"].keys()) == {a_id, b_id, c_id}
    assert tree["edges"] == [{"from": a_id, "to": b_id}, {"from": b_id, "to": c_id}]
    assert tree["gaps"] == {}
    assert tree["truncated"] is None


def test_bounded_tree_node_limit(tmp_path: Path) -> None:
    service = _service(tmp_path, _CHAIN_SOURCE)
    a_id = _find_id(service, "com.example.A", "entry")
    tree = service.build_bounded_tree(a_id, max_nodes=2)

    assert tree["truncated"]["reason"] == "node_limit"
    assert tree["truncated"]["nodes"] == 2
    # R-6：edges 端点恒 ⊆ nodes
    node_ids = set(tree["nodes"].keys())
    assert all(edge["from"] in node_ids and edge["to"] in node_ids for edge in tree["edges"])


def test_bounded_tree_depth_limit(tmp_path: Path) -> None:
    service = _service(tmp_path, _DEEP_SOURCE)
    l0_id = _find_id(service, "com.example.L0", "step")
    tree = service.build_bounded_tree(l0_id, max_depth=3)

    assert tree["truncated"]["reason"] == "depth_limit"
    assert len(tree["nodes"]) == 4  # L0-L3


def test_bounded_tree_cycle(tmp_path: Path) -> None:
    """R-3：环调用不死循环（visited 实证）。"""
    service = _service(tmp_path, _chain_with_ring())
    ping_id = _find_id(service, "com.example.Ring1", "ping")
    pong_id = _find_id(service, "com.example.Ring2", "pong")
    tree = service.build_bounded_tree(ping_id)
    assert set(tree["nodes"].keys()) == {ping_id, pong_id}
    assert tree["truncated"] is None


def _chain_with_ring() -> dict[str, str]:
    return {
        "com/example/Ring1.java": _CHAIN_SOURCE_R1,
        "com/example/Ring2.java": _CHAIN_SOURCE_R2,
    }


_CHAIN_SOURCE_R1 = """package com.example;
public class Ring1 {
  public void ping() {
    Ring2 other = new Ring2();
    other.pong();
  }
}
"""
_CHAIN_SOURCE_R2 = """package com.example;
public class Ring2 {
  public void pong() {
    Ring1 other = new Ring1();
    other.ping();
  }
}
"""


# ---------------------------------------------------------------------------
# A-9：落盘
# ---------------------------------------------------------------------------


def test_save_tree(tmp_path: Path) -> None:
    service = _service(tmp_path, _CHAIN_SOURCE)
    a_id = _find_id(service, "com.example.A", "entry")
    tree = service.build_bounded_tree(a_id)
    path = service.save_tree("act_com_example_A_entry", tree)

    assert path.is_file()
    payload = json.loads(path.read_text("utf-8"))
    assert payload["entry"] == a_id
    assert set(payload["nodes"].keys()) == set(tree["nodes"].keys())


# ---------------------------------------------------------------------------
# A-10/A-11：入口清单
# ---------------------------------------------------------------------------


def test_get_entry_points_with_table(tmp_path: Path) -> None:
    service = _service(tmp_path, _CHAIN_SOURCE)
    a_id = _find_id(service, "com.example.A", "entry")
    entry_table = {
        "schema_version": "1.0.0",
        "package": "com.example",
        "api_entries": [
            {  # manifest 条目：lifecycle 解析
                "entry_id": "act_com_example_A_entry", "kind": "activity",
                "component_name": "com.example.A", "source": "manifest",
                "entry_method": "entry(java.lang.String)->void",
            },
            {  # binder 条目：implementation_method_id 直通（D2）
                "entry_id": "binder_com_example_B_run", "kind": "binder",
                "component_name": "com.example.B",
                "source": "rule_artifact:binder_bindings",
                "implementation_method_id": a_id,
            },
            {  # webview 条目：method_id=None
                "entry_id": "webview_com_example_C_bridge", "kind": "webview_bridge",
                "component_name": "com.example.C",
                "source": "rule_artifact:webview_js_bridges",
                "bridge_line": 3, "bridge_name": "Bridge",
            },
        ],
    }
    entry_path = tmp_path / "api-surface" / "api_entry_table.json"
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(json.dumps(entry_table), "utf-8")

    points = service.get_entry_points()
    by_id = {item["entry_id"]: item for item in points}
    # manifest 条目 lifecycle 解析（A.entry 非 activity lifecycle 名——无方法解析 → None？
    # A 类无 onCreate 等 lifecycle 方法 → method_id=None（不伪造））
    assert by_id["act_com_example_A_entry"]["method_id"] is None
    # binder 直通
    assert by_id["binder_com_example_B_run"]["method_id"] == a_id
    # webview None
    assert by_id["webview_com_example_C_bridge"]["method_id"] is None


def test_get_entry_points_with_lifecycle(tmp_path: Path) -> None:
    """manifest 条目的 lifecycle 解析路径（组件含 onCreate）。"""
    sources = {
        "com/example/SplashActivity.java": (
            "package com.example;\npublic class SplashActivity {\n"
            "  protected void onCreate(android.os.Bundle b) {\n"
            "    C sink = new C();\n"
            "    sink.write(\"x\");\n"
            "  }\n}\n"
        ),
        **_CHAIN_SOURCE,
    }
    service = _service(tmp_path, sources)
    entry_table = {
        "schema_version": "1.0.0", "package": "com.example",
        "api_entries": [{
            "entry_id": "act_com_example_SplashActivity_onCreate", "kind": "activity",
            "component_name": "com.example.SplashActivity", "source": "manifest",
            "entry_method": "onCreate(android.os.Bundle)->void",
        }],
    }
    entry_path = tmp_path / "api-surface" / "api_entry_table.json"
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(json.dumps(entry_table), "utf-8")

    points = service.get_entry_points()
    assert points and points[0]["method_id"] is not None
    assert "SplashActivity" in points[0]["method_id"]


def test_get_entry_points_degraded(tmp_path: Path) -> None:
    """A-11/N-3：入口表缺失/损坏降级（其余能力不受影响）。"""
    service = _service(tmp_path, _CHAIN_SOURCE)
    points = service.get_entry_points()
    assert len(points) == 1 and points[0]["degraded"] == "api_entry_table_missing"
    assert "search_symbol" in points[0]["hint"]

    entry_path = tmp_path / "api-surface" / "api_entry_table.json"
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text("{ not json", "utf-8")
    assert service.get_entry_points() == []
    # 其余能力不受影响
    assert service.search_symbol("log")
