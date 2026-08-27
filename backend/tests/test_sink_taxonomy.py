"""sink taxonomy 版本化与升级闭环测试（T2.9，验收 A-1~A-18、N-1~N-7）。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from app.analysis.explorer_validation import validate_explorer_candidates
from app.analysis.index_store import SQLiteCodeIndexReader
from app.analysis.indexer import build_code_index
from app.analysis.sink_taxonomy import (
    SinkTaxonomyEntry,
    generate_golden_case,
    load_sink_taxonomy,
    normalize_receiver_type,
    promote_custom_sink,
    revalidate_run_candidates,
    sink_matches_taxonomy,
    sink_method_from_method_id,
)
from app.config import WORKSPACE_ROOT

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
    sink.startService(value);
  }
}
""",
    "com/example/C.java": """package com.example;
public class C {
  public void startService(String value) {
  }
}
""",
}


def _build_index(tmp_path: Path) -> dict:
    source_root = tmp_path / "sources"
    for relative, content in _CHAIN_SOURCE.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    return build_code_index(source_root, tmp_path / "index" / "code-index.json")


def _method_id(reader: SQLiteCodeIndexReader, qualified_class: str, name: str) -> str:
    row = reader.db.execute(
        "SELECT id FROM methods WHERE qualified_class = ? AND name = ?",
        (qualified_class, name),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def _candidate(reader: SQLiteCodeIndexReader) -> dict:
    a = _method_id(reader, "com.example.A", "entry")
    b = _method_id(reader, "com.example.B", "run")
    c = _method_id(reader, "com.example.C", "startService")
    line_row = reader.db.execute(
        "SELECT start_line FROM call_sites WHERE method_id = ? AND resolve_status = 'resolved' LIMIT 1",
        (a,),
    ).fetchone()
    sink_line_row = reader.db.execute(
        "SELECT start_line FROM call_sites WHERE method_id = ? AND resolved_target_id = ? "
        "AND resolve_status = 'resolved' LIMIT 1",
        (b, c),
    ).fetchone()
    assert line_row is not None and sink_line_row is not None
    return {
        "schema_version": "1.0.0", "candidate_id": "expl_" + "e" * 20,
        "source": "explorer_agent", "prompt_version": "explorer/1.0.0", "model": "m",
        "component": {"kind": "activity", "name": "com.example.A", "exported": True, "entry_method": "entry"},
        "api_entry_ref": "act_a",
        "chain_proposal": {
            "source": "A.entry(input)", "sink": "C.startService(value)",
            "hops": [
                {"from_method_id": a, "to_method_id": b, "call_site_line": int(line_row["start_line"]),
                 "resolved_via": "direct_call"},
                {"from_method_id": b, "to_method_id": c, "call_site_line": int(sink_line_row["start_line"]),
                 "resolved_via": "direct_call"},
            ],
            "confidence": "high", "hypothesis": "likely",
            "impact_proposal": "i", "reasoning": "r", "evidence_refs": [],
        },
        "validation": None,
    }


def _entries(*overrides: SinkTaxonomyEntry) -> list[SinkTaxonomyEntry]:
    # 测试链尾 receiver=com.example.C（leaf=C）——条目约束含 C 模拟命中
    base = [SinkTaxonomyEntry(method="startService", taxonomy="connection_session_control",
                              receiver_leaves=frozenset({"C", "Context"}))]
    return [*base, *overrides]


# ---------------------------------------------------------------------------
# A-1~A-5：加载与匹配
# ---------------------------------------------------------------------------


def test_seed_file_loadable() -> None:
    """A-1/A-17：种子文件合法加载（≥30 条 base + 三态约束覆盖）。

    source 合法集 {base, manual}——manual 为升级闭环追加（2026-08-27
    F3 首批 4 条人工确认扩充，taxonomy_version 1.0.4）。
    """

    entries = load_sink_taxonomy(WORKSPACE_ROOT / "rules" / "sink_taxonomy" / "versions.yaml")
    assert len(entries) >= 30
    assert all(entry.source in {"base", "manual"} for entry in entries)
    assert sum(1 for e in entries if e.source == "base") >= 30  # 种子基线不劣化
    assert all(entry.method and entry.taxonomy for entry in entries)
    assert any(entry.receiver_leaves for entry in entries)
    assert any(entry.receiver_prefixes for entry in entries)
    assert any(entry.receiver_exact for entry in entries)
    # 抽样对照（A-17：与 rules 提炼一致）
    start_service = next(e for e in entries if e.method == "startService")
    assert start_service.taxonomy == "connection_session_control"
    assert "Context" in start_service.receiver_leaves
    exec_sql = next(e for e in entries if e.method == "execSQL")
    assert exec_sql.taxonomy == "database_mutation"
    assert any("android.database.sqlite." in p for p in exec_sql.receiver_prefixes)


def test_load_tolerance(tmp_path: Path) -> None:
    """A-2/N-4：缺失/损坏/结构异常 → None（禁用）；畸形条目跳过。"""

    assert load_sink_taxonomy(tmp_path / "absent.yaml") is None
    broken = tmp_path / "broken.yaml"
    broken.write_text("not: {valid: yaml", "utf-8")
    assert load_sink_taxonomy(broken) is None
    bad_entries = tmp_path / "bad_entries.yaml"
    bad_entries.write_text(yaml.safe_dump({"entries": "not-a-list"}), "utf-8")
    assert load_sink_taxonomy(bad_entries) is None
    # 合法文件但空条目 → 空列表（启用且零已知 sink——非禁用）
    empty = tmp_path / "empty.yaml"
    empty.write_text(yaml.safe_dump({"entries": []}), "utf-8")
    assert load_sink_taxonomy(empty) == []
    partial = tmp_path / "partial.yaml"
    partial.write_text(yaml.safe_dump({"entries": [
        {"method": "ok", "taxonomy": "t"},
        {"method": "no-taxonomy"},
        "not-a-mapping",
    ]}), "utf-8")
    entries = load_sink_taxonomy(partial)
    assert len(entries) == 1 and entries[0].method == "ok"


def test_match_three_modes() -> None:
    """A-3：leaf/prefix/exact 三态匹配。"""

    entries = [
        SinkTaxonomyEntry(method="sendBroadcast", taxonomy="t1",
                          receiver_leaves=frozenset({"Context"})),
        SinkTaxonomyEntry(method="execSQL", taxonomy="t2",
                          receiver_prefixes=("android.database.sqlite.",)),
        SinkTaxonomyEntry(method="forName", taxonomy="t3",
                          receiver_exact=frozenset({"java.lang.Class"})),
    ]
    assert sink_matches_taxonomy("sendBroadcast", "Landroid/content/Context;", entries).taxonomy == "t1"
    assert sink_matches_taxonomy("execSQL", "android.database.sqlite.SQLiteDatabase", entries).taxonomy == "t2"
    assert sink_matches_taxonomy("forName", "java.lang.Class", entries).taxonomy == "t3"
    assert sink_matches_taxonomy("unknownMethod", "android.content.Context", entries) is None


def test_receiver_missing_lenient() -> None:
    """A-4（D2）：receiver 缺失 → 宽松命中。"""

    entries = [SinkTaxonomyEntry(method="write", taxonomy="file_mutation",
                                 receiver_leaves=frozenset({"File"}))]
    assert sink_matches_taxonomy("write", None, entries) is not None
    assert sink_matches_taxonomy("write", "", entries) is not None
    assert sink_matches_taxonomy("write", "com.example.C", entries) is None  # 有证据则失配


def test_same_name_receiver_disambiguation() -> None:
    """A-5：同名异义 receiver 消歧（query 双 taxonomy）。"""

    entries = [
        SinkTaxonomyEntry(method="query", taxonomy="data_disclosure",
                          receiver_leaves=frozenset({"ContentResolver"})),
        SinkTaxonomyEntry(method="query", taxonomy="data_disclosure",
                          receiver_prefixes=("android.database.sqlite.",)),
    ]
    assert sink_matches_taxonomy("query", "android.content.ContentResolver", entries) is not None
    assert sink_matches_taxonomy("query", "android.database.sqlite.SQLiteDatabase", entries) is not None
    assert sink_matches_taxonomy("query", "com.example.Other", entries) is None


def test_normalize_receiver_type() -> None:
    """R-2：smali/泛型规范化。"""

    assert normalize_receiver_type("Lcom/foo/Bar;") == "com.foo.Bar"
    assert normalize_receiver_type("Ljava/util/List<Ljava/lang/String;>;") == "java.util.List"
    assert normalize_receiver_type("android.content.Context") == "android.content.Context"
    assert normalize_receiver_type(None) is None
    assert normalize_receiver_type("") is None


def test_method_id_parsing() -> None:
    """R-10：path#Class.method:line 与 path#method:line 双形态。"""

    assert sink_method_from_method_id("src/A.java#C.write:4") == "write"
    assert sink_method_from_method_id("src/A.java#write:4") == "write"
    assert sink_method_from_method_id("no-hash") is None
    assert sink_method_from_method_id(None) is None


# ---------------------------------------------------------------------------
# A-6~A-9：判定接通（真实索引）
# ---------------------------------------------------------------------------


def _validated_reader(tmp_path: Path) -> SQLiteCodeIndexReader:
    return SQLiteCodeIndexReader(_build_index(tmp_path))


def test_hit_no_cap(tmp_path: Path) -> None:
    """A-6：命中不压档（validated 保持）。"""

    reader = _validated_reader(tmp_path)
    try:
        candidate = _candidate(reader)
        counts = validate_explorer_candidates(
            [candidate], reader, str(tmp_path / "analysis.sqlite3"),
            {"debuggable": False}, taxonomy_entries=_entries(),
        )
        assert counts["validated"] == 1
        assert candidate["validation"]["custom_sink_proposal"] is False
    finally:
        reader.close()


def test_miss_caps_to_partial(tmp_path: Path) -> None:
    """A-7：未命中压档（封顶 partial + notes）。"""

    reader = _validated_reader(tmp_path)
    try:
        candidate = _candidate(reader)
        empty_entries = [SinkTaxonomyEntry(method="other", taxonomy="t")]
        counts = validate_explorer_candidates(
            [candidate], reader, str(tmp_path / "analysis.sqlite3"),
            {"debuggable": False}, taxonomy_entries=empty_entries,
        )
        assert counts["partially_validated"] == 1
        validation = candidate["validation"]
        assert validation["custom_sink_proposal"] is True
        assert "custom sink 待人工确认" in validation["notes"]
        assert validation["verified_hop_count"] == 2  # 跳回查全通过（压档仅因 custom）
    finally:
        reader.close()


def test_disabled_compatibility(tmp_path: Path) -> None:
    """A-8：taxonomy_entries=None → T2.6 行为（不标记不压档）。"""

    reader = _validated_reader(tmp_path)
    try:
        candidate = _candidate(reader)
        counts = validate_explorer_candidates(
            [candidate], reader, str(tmp_path / "analysis.sqlite3"),
            {"debuggable": False}, taxonomy_entries=None,
        )
        assert counts["validated"] == 1
        assert candidate["validation"]["custom_sink_proposal"] is False
    finally:
        reader.close()


def test_malformed_anchor_not_flagged(tmp_path: Path) -> None:
    """A-9/N-5：畸形锚点（无 # / 无 receiver 行）判定跳过不加重。"""

    reader = _validated_reader(tmp_path)
    try:
        candidate = _candidate(reader)
        candidate["chain_proposal"]["hops"][-1]["to_method_id"] = "malformed-no-hash"
        validate_explorer_candidates(
            [candidate], reader, str(tmp_path / "analysis.sqlite3"),
            {"debuggable": False}, taxonomy_entries=_entries(),
        )
        assert candidate["validation"]["custom_sink_proposal"] is False
    finally:
        reader.close()


# ---------------------------------------------------------------------------
# A-10~A-14：升级闭环
# ---------------------------------------------------------------------------


def _taxonomy_file(tmp_path: Path) -> Path:
    path = tmp_path / "sink_taxonomy" / "versions.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "schema_version": "1.0", "taxonomy_version": "1.0.0",
        "entries": [{"method": "startService", "taxonomy": "connection_session_control",
                     "receiver_leaves": ["Context"], "source": "base"}],
    }, allow_unicode=True), "utf-8")
    return path


def test_promote_append_and_idempotent(tmp_path: Path) -> None:
    """A-10/A-11/N-1：追加 + 版本递增 + 幂等；文件不存在冷启动。"""

    path = tmp_path / "new" / "versions.yaml"  # N-1 冷启动
    first = promote_custom_sink(
        path, method="writeConfig", taxonomy="persistent_state_write",
        receiver_leaves=["SportConfig"], operator="analyst",
    )
    assert first["status"] == "appended"
    assert first["taxonomy_version"] == "1.0.1"
    entries = load_sink_taxonomy(path)
    manual = entries[-1]
    assert manual.source == "manual" and manual.meta["confirmed_by"] == "analyst"

    again = promote_custom_sink(
        path, method="writeConfig", taxonomy="persistent_state_write",
        receiver_leaves=["SportConfig"], operator="analyst",
    )
    assert again["status"] == "skipped"
    assert len(load_sink_taxonomy(path)) == len(entries)  # 幂等：条目数不变


def test_promote_upgrade_base(tmp_path: Path) -> None:
    """A-12：base 条目同约束 promote → 升级 manual。"""

    path = _taxonomy_file(tmp_path)
    result = promote_custom_sink(
        path, method="startService", taxonomy="connection_session_control",
        receiver_leaves=["Context"], operator="analyst",
    )
    assert result["status"] == "upgraded"
    entries = load_sink_taxonomy(path)
    upgraded = next(e for e in entries if e.method == "startService")
    assert upgraded.source == "manual"
    assert upgraded.meta["confirmed_by"] == "analyst"


def test_revalidate_promotion_lifecycle(tmp_path: Path) -> None:
    """A-13：完整闭环——压档 → promote → 重校验升档（副本不落盘）。"""

    taxonomy_path = tmp_path / "sink_taxonomy" / "versions.yaml"
    taxonomy_path.parent.mkdir(parents=True)
    taxonomy_path.write_text(yaml.safe_dump({
        "schema_version": "1.0", "taxonomy_version": "1.0.0", "entries": [],
    }), "utf-8")

    code_index = _build_index(tmp_path)
    reader = SQLiteCodeIndexReader(code_index)
    try:
        candidate = _candidate(reader)
    finally:
        reader.close()
    run_dir = tmp_path / "run"
    (run_dir / "explorer").mkdir(parents=True)
    (run_dir / "index").mkdir()
    (run_dir / "index" / "code-index.json").write_text(json.dumps(code_index), "utf-8")
    (run_dir / "manifest.json").write_text(json.dumps({"debuggable": False}), "utf-8")
    (run_dir / "explorer" / "candidates.json").write_text(
        json.dumps([candidate], ensure_ascii=False), "utf-8")

    # 闭环前：空 taxonomy → custom 压档
    report_before = revalidate_run_candidates(run_dir, taxonomy_path)
    change = next(c for c in report_before["status_changes"]
                  if c["candidate_id"] == candidate["candidate_id"])
    assert change["before"] is None and change["after"] == "partially_validated"
    assert change["custom_after"] is True

    # promote（链尾方法）
    promote_custom_sink(
        taxonomy_path, method="startService", taxonomy="connection_session_control",
        operator="analyst", provenance={"run_id": "run", "candidate_id": candidate["candidate_id"]},
    )
    report_after = revalidate_run_candidates(run_dir, taxonomy_path)
    promoted = next(c for c in report_after["status_changes"]
                    if c["candidate_id"] == candidate["candidate_id"])
    # D4 副本语义：before 取原始文件（validation=None 未被首报告改写）；
    # 升档体现在 after（custom 解除 → 全跳通过 → validated）
    assert promoted["before"] is None and promoted["after"] == "validated"
    assert promoted["custom_before"] is None and promoted["custom_after"] is False

    # 副本不落盘（D4）：candidates.json 原文不变（validation 仍为 None）
    raw = json.loads((run_dir / "explorer" / "candidates.json").read_text("utf-8"))
    assert raw[0]["validation"] is None


def test_golden_case_shape(tmp_path: Path) -> None:
    """A-14（评审 R-1）：golden 用例经 GoldenCase 模型校验通过。"""

    from app.evaluation.golden import GoldenCase

    reader = _validated_reader(tmp_path)
    try:
        candidate = _candidate(reader)
    finally:
        reader.close()
    entry = SinkTaxonomyEntry(
        method="startService", taxonomy="connection_session_control",
        receiver_leaves=frozenset({"Context"}), source="manual",
        meta={"run_id": "r1", "candidate_id": "expl_x", "taxonomy_version": "1.0.1"},
    )
    case = generate_golden_case(
        candidate, entry, case_id="explorer-custom-sink-startservice", operator="analyst",
    )
    validated = GoldenCase.model_validate(case)  # 严格模型校验（非仅 JSON 可序列化）
    assert validated.label == "positive"
    assert validated.rule == "EXPLORER_AGENT"
    assert validated.expected.taxonomy == "connection_session_control"
    assert validated.provenance[0].kind == "explorer-promotion"
    assert "r1/expl_x@v1.0.1" == validated.provenance[0].reference


# ---------------------------------------------------------------------------
# A-15/A-18/N-2/N-3/N-6/N-7：接线与边界
# ---------------------------------------------------------------------------


def test_deep_dive_excludes_custom(tmp_path: Path) -> None:
    """A-18（评审 R-3）：custom 压档候选不进深挖（省预算）。"""

    from app.analysis.call_tree import CallTreeService
    from app.analysis.explorer import ExplorerOrchestrator
    from app.config import ExplorerSettings

    code_index = _build_index(tmp_path)
    reader = SQLiteCodeIndexReader(code_index)
    call_tree = CallTreeService(tmp_path, reader, ExplorerSettings().call_tree)

    class FakeDive:
        calls = 0

        async def __call__(self, model_input):  # pragma: no cover - 不应被调
            FakeDive.calls += 1
            return {"status": "completed", "analysis": {}, "metadata": {}}

    fake = FakeDive()
    orchestrator = ExplorerOrchestrator(
        fake, call_tree, ExplorerSettings(), tmp_path, deep_dive_call=fake)
    try:
        candidate = _candidate(reader)
        validate_explorer_candidates(
            [candidate], reader, str(tmp_path / "a.sqlite3"),
            {"debuggable": False},
            taxonomy_entries=[SinkTaxonomyEntry(method="none", taxonomy="t")],
        )
        assert candidate["validation"]["status"] == "partially_validated"
        assert candidate["validation"]["custom_sink_proposal"] is True
        counts = __import__("asyncio").run(orchestrator.deep_dive_partials([candidate], reader))
        assert counts["partial_total"] == 0  # custom 压档被排除
        assert fake.calls == 0
    finally:
        reader.close()


def test_revalidate_tolerances(tmp_path: Path) -> None:
    """N-2/N-3/N-7：candidates 缺失/索引缺失/无 validation 历史产物容错。"""

    empty_dir = tmp_path / "empty-run"
    empty_dir.mkdir()
    assert revalidate_run_candidates(empty_dir, _taxonomy_file(tmp_path)) == {
        "total": 0, "status_changes": [], "counts": {},
    }

    no_index = tmp_path / "no-index"
    (no_index / "explorer").mkdir(parents=True)
    (no_index / "explorer" / "candidates.json").write_text(
        json.dumps([{"candidate_id": "x", "chain_proposal": {"hops": []}}]), "utf-8")
    report = revalidate_run_candidates(no_index, _taxonomy_file(tmp_path))
    assert report["degraded"] == "index_missing"


def test_bare_method_entry_matches_any_receiver() -> None:
    """N-6：无 receiver 约束条目（manual 裸方法名）任意 receiver 命中。"""

    entry = SinkTaxonomyEntry(method="customSink", taxonomy="t")
    assert sink_matches_taxonomy("customSink", "com.anything.Receiver", [entry]) is not None


def test_versions_yaml_synced_with_dataflow() -> None:
    """双源同步 CI 接入（P1 核验 R-4）：versions.yaml base 条目与
    rules/shared/dataflow.py::classify_operation_taxonomy 一致性校验。

    冲突（CONFLICT，退出码 1）即失败——防止两源 taxonomy 漂移
    （验收用例：write 设备流曾归 file_mutation 而 dataflow 归
    device_protocol_output）。详见 scripts/check_sink_taxonomy_sync.py。
    """

    import subprocess
    import sys

    script = WORKSPACE_ROOT / "scripts" / "check_sink_taxonomy_sync.py"
    taxonomy_yaml = WORKSPACE_ROOT / "rules" / "sink_taxonomy" / "versions.yaml"
    assert script.exists() and taxonomy_yaml.exists()
    result = subprocess.run(
        [sys.executable, str(script), "--yaml", str(taxonomy_yaml)],
        capture_output=True,
        text=True,
        cwd=str(WORKSPACE_ROOT),
        timeout=120,
    )
    assert result.returncode == 0, (
        f"sink taxonomy 双源同步校验失败（exit={result.returncode}）：\n"
        f"{result.stdout}\n{result.stderr}"
    )
