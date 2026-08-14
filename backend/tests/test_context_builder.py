from __future__ import annotations

import asyncio
from pathlib import Path

from app.analysis.context_budget import ContextBudget, ContextBudgeter
from app.analysis.context_builder import ContextBuilder
from app.analysis.indexer import build_code_index
from app.analysis.index_store import SQLiteCodeIndexReader
from app.analysis.orchestrator import ScanOrchestrator
from app.config import ContextBudgetSettings, Settings, SourceAnalysisSettings, StorageSettings
from app.runs.storage import RunStorage
from app.shared.repository import SQLiteRepository

SOURCE = """package com.example;

import android.app.Activity;
import android.os.Bundle;

public class ExportedActivity extends Activity {
    public void onCreate(Bundle state) {
        String url = getIntent().getStringExtra("url");
        dispatch(url);
    }

    private void dispatch(String url) {
        openUrl(url);
    }

    private void openUrl(String url) {
        webView.loadUrl(url);
    }
}
"""


def build_index(tmp_path: Path) -> dict:
    source_root = tmp_path / "sources"
    path = source_root / "com" / "example" / "ExportedActivity.java"
    path.parent.mkdir(parents=True)
    path.write_text(SOURCE, "utf-8")
    return build_code_index(source_root, tmp_path / "code-index.json")


def candidate() -> dict:
    return {
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "rule_version": "1.0.0",
        "component": "activity",
        "component_name": "com.example.ExportedActivity",
        "title": "Activity external input",
        "description": "candidate",
        "severity_hint": "medium",
        "confidence_tier": "medium",
        "evidence_level": "L2",
        "locations": [{"artifact": "code", "path": "com/example/ExportedActivity.java", "line": 8}],
        "sources": [{"path": "com/example/ExportedActivity.java", "line": 8, "text": "getStringExtra"}],
        "sinks": [],
        "propagation_paths": [],
        "blocking_gaps": [],
        "limitations": [],
    }


def test_index_extracts_class_methods_and_calls(tmp_path: Path) -> None:
    index = build_index(tmp_path)
    reader = SQLiteCodeIndexReader(index)
    try:
        file = reader.load_structure_files()[0]
    finally:
        reader.close()
    assert index["schema_version"] == "2.9.0"
    assert index["type"] == "sqlite"
    assert file["classes"][0]["qualified_name"] == "com.example.ExportedActivity"
    methods = {method["name"]: method for method in file["methods"]}
    assert {"onCreate", "dispatch", "openUrl"}.issubset(methods)
    assert "dispatch" in methods["onCreate"]["calls"]
    assert "openUrl" in methods["dispatch"]["calls"]


def test_context_builder_loads_call_edges_only_for_selected_methods(
    tmp_path: Path, monkeypatch,
) -> None:
    index = build_index(tmp_path)

    def reject_full_call_site_load(*_args, **_kwargs):
        raise AssertionError("ContextBuilder must not load all call_sites")

    monkeypatch.setattr(
        SQLiteCodeIndexReader,
        "load_structure_files",
        reject_full_call_site_load,
    )
    builder = ContextBuilder(index)
    assert all("call_sites" not in method for _, method in builder.methods.values())
    assert not builder._loaded_callees

    initial = builder.build_initial(candidate())
    on_create = next(
        context["context_id"] for context in initial["contexts"]
        if context.get("method_name") == "onCreate"
    )
    assert on_create in builder._loaded_callees
    assert len(builder._loaded_callees) < len(builder.methods)

    expanded, added, _ = builder.extend(initial, [{
        "type": "callees",
        "target": "com.example.ExportedActivity.onCreate",
        "reason": "lazy relation",
    }])
    assert added == 1
    assert any(context.get("method_name") == "dispatch" for context in expanded["contexts"])


def test_index_reader_queries_callers_and_callees_by_method_targets(tmp_path: Path) -> None:
    index = build_index(tmp_path)
    reader = SQLiteCodeIndexReader(index)
    try:
        methods = {
            method["name"]: method
            for method in reader.load_lightweight_structure_files()[0]["methods"]
        }
        relations = reader.get_call_relations_for_methods([
            methods["onCreate"]["id"],
            methods["dispatch"]["id"],
        ])
    finally:
        reader.close()

    assert relations["callees"][methods["onCreate"]["id"]] == [methods["dispatch"]["id"]]
    assert relations["callers"][methods["dispatch"]["id"]] == [methods["onCreate"]["id"]]


def test_initial_slice_maps_anchor_to_containing_method(tmp_path: Path) -> None:
    builder = ContextBuilder(build_index(tmp_path))
    document = builder.build_initial(candidate())
    methods = {context.get("method_name") for context in document["contexts"]}
    assert "onCreate" in methods
    assert all("content" in context and "content_sha256" in context for context in document["contexts"])
    assert "files" not in document


def test_callee_and_class_expansion_are_deterministic(tmp_path: Path) -> None:
    builder = ContextBuilder(build_index(tmp_path))
    initial = builder.build_initial(candidate())
    expanded, added, results = builder.extend(initial, [{
        "type": "callees",
        "target": "com.example.ExportedActivity.onCreate",
        "reason": "追踪外部输入分发",
    }])
    assert added == 1
    assert results[0]["status"] == "fulfilled"
    assert any(context.get("method_name") == "dispatch" for context in expanded["contexts"])

    expanded_again, added_again, duplicate_results = builder.extend(expanded, [{
        "type": "callees",
        "target": "com.example.ExportedActivity.onCreate",
        "reason": "追踪外部输入分发",
    }])
    assert added_again == 0
    assert duplicate_results[0]["status"] == "duplicate_request"
    assert len(expanded_again["contexts"]) == len(expanded["contexts"])


class FakeExpandingAI:
    def __init__(self):
        self.calls = 0
        self.inputs = []

    async def analyze(self, candidate_payload, context_slice, previous_analysis=None):
        self.inputs.append(context_slice)
        self.calls += 1
        if self.calls == 1:
            return {
                "status": "completed",
                "analysis": {
                    "summary": "需要继续跟踪 dispatch",
                    "guard_status": "unknown",
                    "blocking_gaps": [],
                    "promotion_recommended": False,
                    "confidence_tier": "medium",
                    "analysis_complete": False,
                    "evidence_refs": [],
                    "context_requests": [{
                        "type": "callees",
                        "target": "com.example.ExportedActivity.onCreate",
                        "reason": "获取直接被调用方法",
                    }],
                },
            }
        dispatch = next(context for context in context_slice["contexts"] if context.get("method_name") == "dispatch")
        return {
            "status": "completed",
            "analysis": {
                "summary": "已确认外部输入进入 dispatch；仍需人工确认最终影响",
                "guard_status": "absent",
                "blocking_gaps": [],
                "promotion_recommended": True,
                "confidence_tier": "high",
                "analysis_complete": True,
                "evidence_refs": [{"context_id": dispatch["context_id"], "line": dispatch["start_line"]}],
                "context_requests": [],
            },
        }


def test_orchestrator_performs_multi_round_expansion_without_full_index(tmp_path: Path) -> None:
    index = build_index(tmp_path)
    builder = ContextBuilder(index)
    initial = builder.build_initial(candidate())
    settings = Settings(
        database_path=tmp_path / "db.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "data"),
        source_analysis=SourceAnalysisSettings(enabled=True),
    )
    repository = SQLiteRepository(settings.resolved_database_path())
    repository.initialize()
    orchestrator = ScanOrchestrator(settings, repository, RunStorage(settings.resolved_data_root(), settings.storage))
    fake_ai = FakeExpandingAI()
    orchestrator.ai = fake_ai
    run_dir = tmp_path / "run"
    (run_dir / "slices").mkdir(parents=True)
    value = candidate()

    result = asyncio.run(orchestrator._analyze_with_expansion(value, initial, builder, run_dir))

    assert result["status"] == "completed"
    assert fake_ai.calls == 2
    assert value["promotion_requested"] is True
    assert len(value["ai_analysis_trace"]) == 2
    assert all("files" not in model_input for model_input in fake_ai.inputs)
    assert (run_dir / "slices" / initial["slice_id"] / "round-001.json").is_file()


class FakeRepeatingAI:
    def __init__(self):
        self.calls = 0
        self.finalize_calls = 0

    async def finalize(self, candidate_payload, context_slice, previous_analysis):
        self.finalize_calls += 1
        self.calls += 1
        return {
            "status": "completed",
            "analysis": {
                "summary": "现有证据不足以完成归并",
                "verdict": "unresolved",
                "review_recommendation": "pending_ai",
                "blocking_gaps": [],
                "uncertainties": [],
                "analysis_complete": False,
                "evidence_refs": [],
            },
        }

    async def analyze(self, candidate_payload, context_slice, previous_analysis=None):
        self.calls += 1
        return {
            "status": "completed",
            "analysis": {
                "summary": "仍请求相同调用关系",
                "guard_status": "unknown",
                "blocking_gaps": [],
                "promotion_recommended": True,
                "confidence_tier": "medium",
                "analysis_complete": False,
                "evidence_refs": [],
                "context_requests": [{
                    "type": "callees",
                    "target": "com.example.ExportedActivity.onCreate",
                    "reason": "重复请求测试",
                }],
            },
        }


def test_repeated_context_request_stops_naturally(tmp_path: Path) -> None:
    index = build_index(tmp_path)
    builder = ContextBuilder(index)
    initial = builder.build_initial(candidate())
    settings = Settings(
        database_path=tmp_path / "repeat.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "repeat-data"),
        source_analysis=SourceAnalysisSettings(enabled=True),
    )
    repository = SQLiteRepository(settings.resolved_database_path())
    repository.initialize()
    orchestrator = ScanOrchestrator(settings, repository, RunStorage(settings.resolved_data_root(), settings.storage))
    fake_ai = FakeRepeatingAI()
    orchestrator.ai = fake_ai
    run_dir = tmp_path / "repeat-run"
    (run_dir / "slices").mkdir(parents=True)
    value = candidate()

    result = asyncio.run(orchestrator._analyze_with_expansion(value, initial, builder, run_dir))

    assert result["status"] == "incomplete"
    assert fake_ai.calls == 3  # initial + 1 expansion + 1 finalization round
    assert fake_ai.finalize_calls == 1
    assert value.get("promotion_requested") is not True
    assert any(gap.get("code") == "CONTEXT_EXPANSION_STALLED" for gap in value["ai_blocking_gaps"] if isinstance(gap, dict))


def test_context_budget_omits_optional_contexts_deterministically() -> None:
    contexts = [{
        "context_id": f"ctx-{index}",
        "kind": "code_window",
        "path": f"Demo{index}.java",
        "start_line": 1,
        "end_line": 1,
        "reason": "optional",
        "content_sha256": "0" * 64,
        "content": "     1 | value",
    } for index in range(3)]
    document = {
        "schema_version": "1.0.0",
        "builder_version": "test",
        "slice_id": "slice_" + "0" * 20,
        "candidate": {},
        "contexts": contexts,
        "edges": [],
        "guards": [],
        "request_history": [],
        "unresolved": [],
        "limitations": [],
    }
    budgeter = ContextBudgeter(ContextBudgetSettings(
        max_input_tokens=10_000,
        max_contexts_per_slice=2,
    ))

    trimmed = budgeter.trim(document)

    assert trimmed["budget"]["status"] == "trimmed"
    assert [context["context_id"] for context in trimmed["contexts"]] == ["ctx-0", "ctx-1"]
    assert trimmed["omitted_contexts"] == [{
        "context_id": "ctx-2",
        "priority": 3,
        "reason": "context_count_budget",
    }]


def test_candidate_request_budget_stops_before_unbounded_finalization(tmp_path: Path) -> None:
    index = build_index(tmp_path)
    builder = ContextBuilder(index)
    initial = builder.build_initial(candidate())
    settings = Settings(
        database_path=tmp_path / "budget.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "budget-data"),
        source_analysis=SourceAnalysisSettings(enabled=True),
        context_budget=ContextBudgetSettings(
            max_requests_per_candidate=2,
            max_expansions_per_candidate=2,
        ),
    )
    repository = SQLiteRepository(settings.resolved_database_path())
    repository.initialize()
    orchestrator = ScanOrchestrator(settings, repository, RunStorage(settings.resolved_data_root(), settings.storage))
    fake_ai = FakeRepeatingAI()
    orchestrator.ai = fake_ai
    run_dir = tmp_path / "budget-run"
    (run_dir / "slices").mkdir(parents=True)
    value = candidate()

    result = asyncio.run(orchestrator._analyze_with_expansion(value, initial, builder, run_dir))

    assert result["status"] == "incomplete"
    assert fake_ai.calls == 2
    assert any(gap.get("code") == "CONTEXT_BUDGET_EXHAUSTED" for gap in value["ai_blocking_gaps"])


def test_manifest_context_injected_into_initial_slice(tmp_path: Path) -> None:
    """Manifest component facts should be injected as a traceable context."""

    index = build_index(tmp_path)
    builder = ContextBuilder(index)
    cand = {**candidate(), "component": "activity", "component_name": "com.example.ExportedActivity"}
    initial = builder.build_initial(cand)
    manifest_ctxs = [ctx for ctx in initial["contexts"] if ctx.get("kind") == "manifest_component"]
    assert len(manifest_ctxs) == 1
    assert manifest_ctxs[0]["context_id"] == "manifest:activity:com.example.ExportedActivity"
    assert manifest_ctxs[0]["path"] == "AndroidManifest.xml"


def test_canonical_method_target_in_context(tmp_path: Path) -> None:
    """Method contexts should include canonical_method_target for stable model requests."""

    index = build_index(tmp_path)
    builder = ContextBuilder(index)
    initial = builder.build_initial(candidate())
    method_ctxs = [ctx for ctx in initial["contexts"] if ctx.get("kind") == "method"]
    assert len(method_ctxs) > 0
    assert all("canonical_method_target" in ctx for ctx in method_ctxs)


def test_resolve_methods_handles_fqcn_hash_format(tmp_path: Path) -> None:
    """_resolve_methods should handle FQCN#method and FQCN#Class.method:line formats."""

    index = build_index(tmp_path)
    builder = ContextBuilder(index)
    # Try FQCN#method format
    results = builder._resolve_methods("com.example.ExportedActivity#onCreate")
    assert len(results) >= 1
    # Try FQCN.method format
    results2 = builder._resolve_methods("com.example.ExportedActivity.onCreate")
    assert len(results2) >= 1


def test_extend_returns_already_present_for_existing_context(tmp_path: Path) -> None:
    """Requests for contexts already in the slice should return already_present, not not_found."""

    index = build_index(tmp_path)
    builder = ContextBuilder(index)
    initial = builder.build_initial(candidate())
    # Request the same component that's already indexed
    expanded, added, results = builder.extend(initial, [{
        "type": "component",
        "target": "com.example.ExportedActivity",
        "reason": "already there",
    }])
    # Should be already_present or fulfilled (if class summary wasn't added before)
    assert all(r["status"] != "unresolved" for r in results)


def test_unsupported_request_is_not_coerced_to_method(tmp_path: Path) -> None:
    builder = ContextBuilder(build_index(tmp_path))
    initial = builder.build_initial(candidate())

    expanded, added, results = builder.extend(initial, [{
        "type": "whole_repository",
        "target": "everything",
        "reason_code": "UNBOUNDED_REQUEST",
        "reason": "request all code",
    }])

    assert added == 0
    assert results[0]["status"] == "unsupported"
    assert results[0]["type"] == "whole_repository"
    assert expanded["request_history"][0]["reason_code"] == "UNBOUNDED_REQUEST"


def test_bare_method_request_is_ambiguous_and_adds_nothing(tmp_path: Path) -> None:
    source_root = tmp_path / "ambiguous-sources"
    for name in ("First", "Second"):
        path = source_root / "com" / "example" / f"{name}.java"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"package com.example;\nclass {name} {{\n  void dispatch() {{\n  }}\n}}\n",
            "utf-8",
        )
    index = build_code_index(source_root, tmp_path / "ambiguous-index.json")
    builder = ContextBuilder(index)
    document = {
        "schema_version": "1.0.0", "builder_version": builder.version,
        "slice_id": "slice_" + "1" * 20, "candidate": {}, "contexts": [],
        "edges": [], "guards": [], "request_history": [], "unresolved": [],
        "limitations": [], "omitted_contexts": [],
    }

    expanded, added, results = builder.extend(document, [{"type": "method", "target": "dispatch"}])

    assert added == 0
    assert results[0]["status"] == "ambiguous"
    assert len(results[0]["alternatives"]) == 2
    assert expanded["contexts"] == []


def test_class_request_is_bounded_instead_of_expanding_the_whole_class(tmp_path: Path) -> None:
    builder = ContextBuilder(
        build_index(tmp_path),
        ContextBudgetSettings(max_methods_per_class_request=1),
    )
    document = {
        "schema_version": "1.0.0", "builder_version": builder.version,
        "slice_id": "slice_" + "3" * 20, "candidate": {}, "contexts": [],
        "edges": [], "guards": [], "request_history": [], "unresolved": [],
        "limitations": [], "omitted_contexts": [],
    }

    expanded, added, results = builder.extend(document, [{
        "type": "class", "target": "com.example.ExportedActivity",
    }])

    assert added == 1
    assert results[0]["status"] == "fulfilled_limited"
    assert len(expanded["contexts"]) == 1
    assert len(results[0]["omissions"]) == 2


def test_context_budget_trims_around_core_anchor_and_fails_if_anchors_are_too_far_apart() -> None:
    content = "\n".join(f"{line:>6} | line {line}" for line in range(1, 101))
    context = {
        "context_id": "method", "kind": "method", "path": "Demo.java",
        "start_line": 1, "end_line": 100, "method_name": "entry",
        "reason": "taint_source", "content_sha256": "0" * 64, "content": content,
    }
    base = {
        "schema_version": "1.0.0", "builder_version": "test",
        "slice_id": "slice_" + "2" * 20, "contexts": [context], "edges": [],
        "guards": [], "request_history": [], "unresolved": [], "limitations": [],
    }
    budgeter = ContextBudgeter(ContextBudgetSettings(
        max_input_tokens=10_000, max_contexts_per_slice=4, max_lines_per_context=20,
    ))

    trimmed = budgeter.trim({**base, "candidate": {"sources": [{"path": "Demo.java", "line": 50}]}})
    unsafe = budgeter.trim({
        **base,
        "candidate": {
            "sources": [{"path": "Demo.java", "line": 10}],
            "sinks": [{"path": "Demo.java", "line": 90}],
        },
    })

    assert trimmed["budget"]["status"] == "trimmed"
    assert trimmed["contexts"][0]["start_line"] <= 50 <= trimmed["contexts"][0]["end_line"]
    assert unsafe["budget"]["status"] == "cannot_trim_safely"


def _ambiguous_method_index() -> dict:
    def file(path: str, fqcn: str, callee: str) -> dict:
        class_name = fqcn.rsplit(".", 1)[-1]
        method_id = f"{path}#{class_name}.run:2"
        callee_id = f"{path}#{class_name}.{callee}:5"
        return {
            "path": path,
            "content": f"class {class_name} {{\n void run() {{ {callee}(); }}\n }}\n void {callee}() {{}}\n",
            "line_count": 5,
            "classes": [{
                "id": f"{path}#{class_name}:1",
                "name": class_name,
                "qualified_name": fqcn,
                "start_line": 1,
                "end_line": 5,
            }],
            "methods": [{
                "id": method_id,
                "name": "run",
                "class_name": class_name,
                "qualified_class": fqcn,
                "signature": "void run()",
                "descriptor": "()->void",
                "symbol_key": f"{fqcn}#run()->void",
                "start_line": 2,
                "end_line": 2,
                "calls": [callee],
                "call_sites": [{
                    "resolved_target_id": callee_id,
                    "resolve_status": "resolved",
                }],
            }, {
                "id": callee_id,
                "name": callee,
                "class_name": class_name,
                "qualified_class": fqcn,
                "signature": f"void {callee}()",
                "descriptor": "()->void",
                "symbol_key": f"{fqcn}#{callee}()->void",
                "start_line": 5,
                "end_line": 5,
                "calls": [],
                "call_sites": [],
            }],
        }

    return {
        "schema_version": "test",
        "files": [
            file("one/Same.java", "one.Same", "first"),
            file("two/Same.java", "two.Same", "second"),
        ],
    }


def test_canonical_target_exactly_matches_index_symbol_key(tmp_path: Path) -> None:
    index = build_index(tmp_path)
    reader = SQLiteCodeIndexReader(index)
    try:
        indexed_methods = {
            method["id"]: method for method in reader.load_structure_files()[0]["methods"]
        }
    finally:
        reader.close()

    document = ContextBuilder(index).build_initial(candidate())
    method_contexts = [context for context in document["contexts"] if context["kind"] == "method"]

    assert method_contexts
    for context in method_contexts:
        assert context["canonical_method_target"] == indexed_methods[context["index_method_id"]]["symbol_key"]
        assert context["symbol_key"] == context["canonical_method_target"]


def test_request_line_is_preserved_in_normalization_and_dedup(tmp_path: Path) -> None:
    builder = ContextBuilder(build_index(tmp_path))
    initial = builder.build_initial(candidate())
    path = "com/example/ExportedActivity.java"

    expanded, _, results = builder.extend(initial, [{
        "type": "method", "target": "unknown", "path": path, "line": 8, "reason": "first",
    }, {
        "type": "method", "target": "unknown", "path": path, "line": 13, "reason": "second",
    }])

    assert [result["line"] for result in results] == [8, 13]
    assert all(result["status"] != "duplicate_request" for result in results)
    assert [item["line"] for item in expanded["request_history"][-2:]] == [8, 13]


def test_ambiguous_method_has_explicit_alternatives_and_does_not_union_expand() -> None:
    builder = ContextBuilder(_ambiguous_method_index())
    initial = builder.build_initial({"rule_id": "test", "limitations": []})

    expanded, added, results = builder.extend(initial, [{
        "type": "callees", "target": "run", "reason": "must not union",
    }])

    assert added == 0
    assert expanded["contexts"] == []
    assert results[0]["status"] == "ambiguous"
    assert {item["canonical_method_target"] for item in results[0]["alternatives"]} == {
        "one.Same#run()->void", "two.Same#run()->void",
    }


def test_resolution_statuses_are_distinct(tmp_path: Path) -> None:
    builder = ContextBuilder(build_index(tmp_path))
    initial = builder.build_initial(candidate())
    method_contexts = {context["method_name"]: context for context in initial["contexts"] if context["kind"] == "method"}

    _, _, results = builder.extend(initial, [{
        "type": "method",
        "target": method_contexts["onCreate"]["canonical_method_target"],
        "reason": "already present",
    }, {
        "type": "callees",
        "target": "com.example.ExportedActivity#openUrl(String)->void",
        "reason": "known method with no callees",
    }, {
        "type": "method",
        "target": "missing",
        "path": "missing/File.java",
        "reason": "file absent",
    }, {
        "type": "method",
        "target": "missing",
        "path": "com/example/ExportedActivity.java",
        "reason": "symbol absent",
    }])

    assert [result["status"] for result in results] == [
        "already_present", "empty_relation", "not_indexed", "not_found",
    ]


def test_manifest_component_matching_is_exact(tmp_path: Path) -> None:
    builder = ContextBuilder(build_index(tmp_path))
    initial = builder.build_initial(candidate())

    _, _, results = builder.extend(initial, [{
        "type": "component", "target": "com.example.Exported", "reason": "partial",
    }, {
        "type": "component", "target": "com.example.ExportedActivity", "reason": "exact",
    }])

    assert [result["status"] for result in results] == ["not_found", "already_present"]


def test_context_budget_records_context_addition_and_byte_omissions(tmp_path: Path) -> None:
    index = build_index(tmp_path)

    context_limited = ContextBuilder(index, budget=ContextBudget(max_contexts=1, max_additions=8, max_bytes=100_000))
    context_document = context_limited.build_initial(candidate())
    assert len(context_document["contexts"]) == 1
    assert any(item["reason"] == "context_limit" for item in context_document["omitted_contexts"])

    addition_limited = ContextBuilder(index, budget=ContextBudget(max_contexts=10, max_additions=1, max_bytes=100_000))
    addition_initial = ContextBuilder(index).build_initial(candidate())
    expanded, added, results = addition_limited.extend(addition_initial, [{
        "type": "class", "target": "com.example.ExportedActivity", "reason": "bounded class",
    }])
    assert added == 1
    assert results[0]["status"] == "fulfilled_limited"
    assert any(item["reason"] == "addition_limit" for item in expanded["omitted_contexts"])
    assert expanded["context_budget"]["usage"]["additions"] == 1

    byte_limited = ContextBuilder(index, budget=ContextBudget(max_contexts=10, max_additions=8, max_bytes=1))
    byte_document = byte_limited.build_initial(candidate())
    assert byte_document["contexts"] == []
    assert byte_document["context_budget"]["usage"]["bytes"] == 0
    assert byte_document["omitted_contexts"]
    assert all(item["reason"] == "byte_limit" for item in byte_document["omitted_contexts"])


def test_slice_id_distinguishes_different_sinks() -> None:
    """v2026-08-14（CONTEXT_SLICE_MISMATCH 回归）：同 rule+component+locations、
    不同 sink 的候选必须生成不同 slice_id——此前 _slice_id 不含 sinks 导致
    42+ 候选共用同一 slice（AI 跨链污染、finding.sinks 与 slice 不一致）。"""
    from app.analysis.context_builder import ContextBuilder

    base = {
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "component_name": "com.example.MainActivity",
        "locations": [{"path": "com/example/MainActivity.java", "line": 100}],
    }
    a = {**base, "sinks": [{"path": "com/example/PreferenceUtil.java", "line": 124, "kind": "sensitive_sink"}]}
    b = {**base, "sinks": [{"path": "com/example/PreferenceUtil.java", "line": 221, "kind": "sensitive_sink"}]}
    assert ContextBuilder._slice_id(a) != ContextBuilder._slice_id(b)


def test_slice_id_stable_for_same_chain() -> None:
    """同一链（同 sources/sinks/paths）重复生成 slice_id 必须相同，防抖动。"""
    from app.analysis.context_builder import ContextBuilder

    candidate = {
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "component_name": "com.example.MainActivity",
        "locations": [{"path": "com/example/MainActivity.java", "line": 100}],
        "sources": [{"path": "com/example/Launch.java", "line": 79, "kind": "taint_source"}],
        "sinks": [{"path": "com/example/PreferenceUtil.java", "line": 124, "kind": "sensitive_sink"}],
    }
    assert ContextBuilder._slice_id(candidate) == ContextBuilder._slice_id(dict(candidate))


def test_slice_id_ignores_unrelated_extension_fields() -> None:
    """AI 附加的无关字段（evidence_refs/verify_status 等）不得改变 slice_id——
    投影只取 path/line/kind/method_name 四键，防止同一链因扩展字段抖动碎片化。"""
    from app.analysis.context_builder import ContextBuilder

    base = {
        "rule_id": "ACTIVITY_INTENT_TO_SENSITIVE_SINK",
        "component_name": "com.example.MainActivity",
        "locations": [{"path": "com/example/MainActivity.java", "line": 100}],
        "sinks": [{"path": "com/example/PreferenceUtil.java", "line": 124, "kind": "sensitive_sink"}],
    }
    plain = ContextBuilder._slice_id(base)
    extended = ContextBuilder._slice_id({
        **base,
        "sinks": [{
            "path": "com/example/PreferenceUtil.java", "line": 124, "kind": "sensitive_sink",
            "evidence_refs": [{"context_id": "x", "line": 124}], "verify_status": "pending",
        }],
    })
    assert plain == extended


def test_finding_slice_sink_match_passes() -> None:
    """v2026-08-14 自检：finding.sinks 与 slice.candidate.sinks 一致 → 返回 []。"""
    from app.analysis.context_builder import finding_slice_sink_mismatch

    finding = {"sinks": [{"path": "com/example/Pref.java", "line": 124, "kind": "sensitive_sink"}]}
    context_slice = {
        "slice_id": "slice_x",
        "candidate": {"sinks": [{"path": "com/example/Pref.java", "line": 124, "kind": "sensitive_sink"}]},
    }
    assert finding_slice_sink_mismatch(finding, context_slice) == []


def test_finding_slice_sink_mismatch_reported() -> None:
    """v2026-08-14 自检：finding.sinks(221) vs slice.sinks(124) → mismatch 详情。"""
    from app.analysis.context_builder import finding_slice_sink_mismatch

    finding = {"sinks": [{"path": "com/example/PreferenceUtil.java", "line": 221, "kind": "sensitive_sink"}]}
    context_slice = {
        "slice_id": "slice_bb21709c",
        "candidate": {"sinks": [{"path": "com/example/PreferenceUtil.java", "line": 124, "kind": "sensitive_sink"}]},
    }
    issues = finding_slice_sink_mismatch(finding, context_slice)
    assert len(issues) == 1
    assert issues[0]["code"] == "FINDING_SLICE_SINK_MISMATCH"
    assert issues[0]["critical"] is True
    assert issues[0]["finding_sinks"][0]["line"] == 221
    assert issues[0]["slice_sinks"][0]["line"] == 124
    assert issues[0]["slice_id"] == "slice_bb21709c"


def test_slice_missing_reported_noncritical() -> None:
    """v2026-08-14 自检：context_slice=None → SLICE_UNAVAILABLE non-critical。"""
    from app.analysis.context_builder import finding_slice_sink_mismatch

    issues = finding_slice_sink_mismatch({"sinks": []}, None)
    assert issues == [{"code": "SLICE_UNAVAILABLE", "critical": False}]


def test_finding_slice_sink_order_insensitive() -> None:
    """v2026-08-14 自检：多 sink 顺序差异不误报（投影后排序比较）。"""
    from app.analysis.context_builder import finding_slice_sink_mismatch

    finding = {"sinks": [
        {"path": "a.java", "line": 1, "kind": "x"}, {"path": "b.java", "line": 2, "kind": "y"},
    ]}
    context_slice = {"slice_id": "s", "candidate": {"sinks": [
        {"path": "b.java", "line": 2, "kind": "y"}, {"path": "a.java", "line": 1, "kind": "x"},
    ]}}
    assert finding_slice_sink_mismatch(finding, context_slice) == []
