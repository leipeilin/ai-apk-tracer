"""回归：manifest 上下文不得携带 0 行号——模型照抄导致 EvidenceReference line>=1 校验失败。

事故（run 20260808T045452Z）：81/147 候选 schema_invalid，全部为
`evidence_refs.N.line/end_line: greater_than_equal`。根因是
`_build_manifest_context` 硬编码 start_line=0/end_line=0，AndroidManifest.xml
是 XML 无代码行号，模型忠实照抄 0 → 违反 minimum=1。
"""

from __future__ import annotations

from app.analysis.context_builder import _build_manifest_context


def _candidate() -> dict[str, object]:
    return {
        "component_name": "com.example.FooActivity",
        "component": "activity",
        "reachability_status": "reachable",
        "authorization_status": "unprotected",
        "guard_status": "absent",
        "entry_points": ["onCreate", "onNewIntent"],
    }


def test_manifest_context_has_null_line_numbers() -> None:
    ctx = _build_manifest_context(_candidate())

    assert ctx is not None
    assert ctx["kind"] == "manifest_component"
    assert ctx["path"] == "AndroidManifest.xml"
    assert ctx["start_line"] is None, "manifest 无代码行号，必须为 None 而非 0"
    assert ctx["end_line"] is None, "manifest 无代码行号，必须为 None 而非 0"


def test_manifest_context_still_carries_facts() -> None:
    ctx = _build_manifest_context(_candidate())

    assert ctx["component_name"] == "com.example.FooActivity"
    assert ctx["exported"] is True
    assert ctx["path"] == "AndroidManifest.xml"


def test_manifest_context_requires_component() -> None:
    assert _build_manifest_context({"component": "activity"}) is None
    assert _build_manifest_context({"component_name": "com.example.Foo"}) is None
