from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

import app.analysis.prompt_registry as prompt_registry_module
from app.analysis.prompt_registry import PromptRegistry, PromptRegistryError
from app.config import WORKSPACE_ROOT


def _isolated_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[PromptRegistry, Path, Path]:
    prompts_root = tmp_path / "prompts"
    schemas_root = tmp_path / "schemas"
    shutil.copytree(WORKSPACE_ROOT / "prompts", prompts_root)
    shutil.copytree(WORKSPACE_ROOT / "schemas", schemas_root)
    monkeypatch.setattr(prompt_registry_module, "PROMPTS_ROOT", prompts_root)
    monkeypatch.setattr(prompt_registry_module, "SCHEMAS_ROOT", schemas_root)
    return PromptRegistry(), prompts_root, schemas_root


def _update_user_hash(prompts_root: Path, prompt_id: str, version: str) -> None:
    registry_path = prompts_root / "registry.yaml"
    document = yaml.safe_load(registry_path.read_text("utf-8"))
    definition = next(
        item for item in document["prompts"]
        if item["id"] == prompt_id and item["version"] == version
    )
    user_path = prompts_root / definition["user_file"]
    definition["template_sha256"]["user"] = hashlib.sha256(user_path.read_bytes()).hexdigest()
    registry_path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), "utf-8")


def test_load_render_and_cache_exact_prompt_version() -> None:
    registry = PromptRegistry()

    first = registry.load("l2-review", "2.0.1")
    second = registry.load("l2-review", "2.0.1")
    canonical_input = json.dumps(
        {"semantic_bundle": {"candidate": {}, "contexts": []}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    rendered = registry.render(
        "l2-review",
        "2.0.1",
        {"l2_review_input_json": canonical_input},
    )

    assert first is second
    assert canonical_input in rendered["user"]
    assert rendered["template_sha256"]["system"] == hashlib.sha256(
        first.system_template.encode("utf-8")
    ).hexdigest()
    assert rendered["rendered_sha256"]["user"] == hashlib.sha256(
        rendered["user"].encode("utf-8")
    ).hexdigest()
    assert set(rendered["schema_sha256"]) == {"input", "output"}


def test_rejects_template_hash_tampering(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, prompts_root, _ = _isolated_registry(monkeypatch, tmp_path)
    system_path = prompts_root / "l1-triage/2.0.4/system.md"
    system_path.write_text(system_path.read_text("utf-8") + "篡改", "utf-8")

    registry = PromptRegistry()
    with pytest.raises(PromptRegistryError, match="SHA-256 不匹配"):
        registry.load("l1-triage", "2.0.4")


def test_rejects_symlinked_template(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, prompts_root, _ = _isolated_registry(monkeypatch, tmp_path)
    user_path = prompts_root / "preflight/1.0.0/user.md"
    external = tmp_path / "outside.md"
    external.write_text("{preflight_input_json}", "utf-8")
    user_path.unlink()
    user_path.symlink_to(external)

    registry = PromptRegistry()
    with pytest.raises(PromptRegistryError, match="symlink"):
        registry.load("preflight", "1.0.0")


@pytest.mark.parametrize(
    "replacement",
    [
        "不可信输入：{unknown_json}\n",
        "不可信输入，但没有规范 JSON placeholder。\n",
    ],
)
def test_rejects_unknown_or_missing_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    replacement: str,
) -> None:
    _, prompts_root, _ = _isolated_registry(monkeypatch, tmp_path)
    user_path = prompts_root / "repair/1.0.1/user.md"
    user_path.write_text(replacement, "utf-8")
    _update_user_hash(prompts_root, "repair", "1.0.1")

    registry = PromptRegistry()
    with pytest.raises(PromptRegistryError, match="placeholder"):
        registry.load("repair", "1.0.1")


def test_render_rejects_missing_and_unknown_variables() -> None:
    registry = PromptRegistry()

    with pytest.raises(PromptRegistryError, match="缺失变量"):
        registry.render("finalization", "1.0.1", {})
    with pytest.raises(PromptRegistryError, match="未知变量"):
        registry.render(
            "finalization",
            "1.0.1",
            {"finalization_input_json": "{}", "extra_json": "{}"},
        )


def test_rejects_non_utf8_template_even_with_matching_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, prompts_root, _ = _isolated_registry(monkeypatch, tmp_path)
    user_path = prompts_root / "finalization/1.0.1/user.md"
    user_path.write_bytes(b"\xff\xfe")
    _update_user_hash(prompts_root, "finalization", "1.0.1")

    registry = PromptRegistry()
    with pytest.raises(PromptRegistryError, match="UTF-8"):
        registry.load("finalization", "1.0.1")


def test_rejects_unknown_prompt_or_version() -> None:
    registry = PromptRegistry()

    with pytest.raises(PromptRegistryError, match="未知 Prompt"):
        registry.load("l2-review", "9.9.9")


def test_registry_uses_exact_patch_versions_and_raw_byte_hashes() -> None:
    registry = PromptRegistry()
    assert ("l1-triage", "2.0.4") in registry.prompt_keys
    assert ("l2-review", "2.0.1") in registry.prompt_keys
    assert ("repair", "1.0.1") in registry.prompt_keys
    assert ("finalization", "1.0.1") in registry.prompt_keys
    assert ("l2-review", "2.0.0") not in registry.prompt_keys

    document = yaml.safe_load((WORKSPACE_ROOT / "prompts/registry.yaml").read_bytes())
    for definition in document["prompts"]:
        system_raw = (WORKSPACE_ROOT / "prompts" / definition["system_file"]).read_bytes()
        user_raw = (WORKSPACE_ROOT / "prompts" / definition["user_file"]).read_bytes()
        input_raw = (WORKSPACE_ROOT / "schemas" / definition["input_schema_file"]).read_bytes()
        output_raw = (WORKSPACE_ROOT / "schemas" / definition["output_schema_file"]).read_bytes()
        assert definition["template_sha256"] == {
            "system": hashlib.sha256(system_raw).hexdigest(),
            "user": hashlib.sha256(user_raw).hexdigest(),
        }
        assert definition["schema_sha256"] == {
            "input": hashlib.sha256(input_raw).hexdigest(),
            "output": hashlib.sha256(output_raw).hexdigest(),
        }


def test_patch_prompts_state_strict_stage_boundaries() -> None:
    registry = PromptRegistry()
    l1 = registry.load("l1-triage", "2.0.4").system_template
    l2 = registry.load("l2-review", "2.0.1").system_template
    repair = registry.load("repair", "1.0.1").system_template
    finalization = registry.load("finalization", "1.0.1").system_template

    assert "context_requests 非空，analysis_complete 必须为 false" in l1
    assert "不得输出 verdict" in l1
    assert "supports_candidate 或 refutes_candidate 必须提供非空 evidence_refs" in l2
    assert "analysis_complete 与 verdict 相互独立" in l2
    assert "不得重新进行安全分析" in repair
    assert "不得把 false 改成 true" in repair
    assert "输入若只有 l1_triage" in finalization
    assert "不得比其结论更强" in finalization
    assert "不得创建新的 context_id" in finalization


@pytest.mark.parametrize(
    ("prompt_id", "version", "schema_file"),
    [
        ("preflight", "1.0.1", "ai_preflight_output.schema.json"),
        ("l1-triage", "2.0.4", "ai_l1_triage_output.schema.json"),
        ("l2-review", "3.0.4", "ai_l2_review_output.schema.json"),
        ("finalization", "1.0.3", "ai_finalization_output.schema.json"),
    ],
    )
def test_prompt_declares_every_required_output_field(prompt_id, version, schema_file) -> None:
    """回归：提示词漏声明必填字段会导致该阶段 100% schema_invalid。

    已发生两次——preflight 1.0.0 漏 ok（全量熔断）、l2-review 2.0.1 漏 summary 与
    confidence_tier（7 个候选全 ai_failed）。repair 阶段禁止猜测缺失的必填裁决，
    漏字段无法被兜底，因此每个阶段的 prompt 都必须显式声明目标 schema 的全部
    required 字段。
    """

    schema = json.loads((WORKSPACE_ROOT / "schemas" / schema_file).read_text(encoding="utf-8"))
    system = PromptRegistry().load(prompt_id, version).system_template

    missing = [field for field in schema["required"] if field not in system]
    assert not missing, f"{prompt_id} {version} 提示词未声明必填字段 {missing}"


@pytest.mark.parametrize(
    ("prompt_id", "version"),
    [
        ("preflight", "1.0.1"),
        ("l2-review", "3.0.4"),
        ("finalization", "1.0.3"),
    ],
    )
def test_prompt_states_no_required_field_may_be_omitted(prompt_id, version) -> None:
    """必填字段清单必须带显式的\"不得省略\"约束，仅列出字段名不足以约束模型。"""

    system = PromptRegistry().load(prompt_id, version).system_template
    assert "一个都不得省略" in system


def test_l2_review_prompt_declares_nested_evidence_reference_constraints() -> None:
    """回归：l2-review 2.0.2 只声明顶层必填，未声明嵌套结构约束，导致 132 个候选
    schema_invalid（evidence_refs 缺 claim / 行号为 0 / 协议外字段 text）。

    嵌套结构（EvidenceReference / BlockingGap）的 required 字段、行号 >= 1、
    additionalProperties=false 必须显式写入提示词，否则模型自行发挥。
    """

    schema = json.loads(
        (WORKSPACE_ROOT / "schemas" / "ai_l2_review_output.schema.json").read_text(encoding="utf-8")
    )
    system = PromptRegistry().load("l2-review", "2.0.3").system_template

    er_required = schema["$defs"]["EvidenceReference"]["required"]
    for field in er_required:
        assert field in system, f"提示词未声明 EvidenceReference 必填字段 {field}"
    assert "claim" in system, "提示词必须声明 evidence_refs 元素的 claim 字段"
    assert "不得为 0 或负数" in system, "提示词必须声明行号 >= 1（模型会把行号写成 0）"
    assert "不得添加" in system, "提示词必须声明禁止协议外字段"

    bg_required = schema["$defs"]["BlockingGap"]["required"]
    for field in bg_required:
        assert field in system, f"提示词未声明 BlockingGap 必填字段 {field}"


def test_l2_review_300_declares_four_factor_judgment_fields() -> None:
    """vuln-judgment-prompt §2-§3：四要素判定标准与结构化字段必须显式声明。

    基线（run 20260808T050946Z）：AI 对 138/147 候选全输出 unresolved，裁决率 0%。
    3.0.0 引入四要素判定（flaw_holds/exploitability/harm/reachability_class/impact_vector）
    打破"全 unresolved"——提示词必须声明这些字段及"缺一不可"的判定逻辑。
    """

    system = PromptRegistry().load("l2-review", "3.0.0").system_template

    for field in ("flaw_holds", "exploitability", "harm", "reachability_class", "impact_vector"):
        assert field in system, f"3.0.0 提示词未声明字段 {field}"
    assert "四要素" in system, "必须声明四要素判定标准"
    assert "漏洞 = 缺陷成立 + 可利用 + 产生危害" in system or "漏洞 = 缺陷成立" in system
    assert "不得输出 CVSS 数值分数" in system, "AI 不得输出 CVSS 分数"
    assert "不得输出 severity_class" in system, "AI 不得输出 severity_class"


def test_l2_review_300_declares_red_line_exclusions() -> None:
    """vuln-judgment-prompt §4：23 条反向排除红线 + verdict 映射必须存在。"""

    system = PromptRegistry().load("l2-review", "3.0.0").system_template

    assert "反向排除" in system or "红线" in system
    assert "refutes_candidate 只能用于确定性否定" in system
    assert "EXFILTRATION_CHANNEL_UNVERIFIED" in system, "红线 23 必须生成 blocking_gap code"
    assert "确定性否定类" in system and "证据缺失" in system, "verdict 映射两类必须声明"


def test_l2_review_300_declares_context_request_structure() -> None:
    """回归：2.0.4 未声明 context_requests 元素结构，2 个候选 failed
    （context_requests.N.type/target: missing）。3.0.0 必须声明 type/target/reason 必填。
    """

    system = PromptRegistry().load("l2-review", "3.0.0").system_template

    assert "context_requests 每个元素必须包含" in system
    assert "type" in system and "target" in system and "reason" in system


def test_l1_triage_202_declares_all_required_output_fields() -> None:
    """回归：L1 送 AI（v2026-08-09）首次真实调用 l1-triage，20 个候选 schema_invalid。

    run 20260808T184016Z：failed 20 全是 DYNAMIC_RECEIVER L1 候选，错误
    suggested_sources.N.context_id/symbol missing + summary missing——2.0.1 提示词
    从未声明顶层必填与嵌套结构（L1 此前从不送 AI，该提示词从未被真实执行验证）。
    2.0.2 必须声明：顶层三字段 + ProposedEvidence/EvidenceReference/BlockingGap/
    ProposedPath/Uncertainty/ContextRequest 的全部 required + 行号 >= 1 + 禁额外字段。
    """

    import json as _json

    schema = _json.loads(
        (WORKSPACE_ROOT / "schemas" / "ai_l1_triage_output.schema.json").read_text(encoding="utf-8")
    )
    system = PromptRegistry().load("l1-triage", "2.0.4").system_template

    # 顶层 required
    for field in schema["required"]:
        assert field in system, f"l1-triage 2.0.2 未声明顶层必填字段 {field}"
    assert "一个都不得省略" in system, "必须声明不得省略约束"

    # 嵌套结构 required（从 $defs 逐一断言）
    for def_name in ("ProposedEvidence", "EvidenceReference", "BlockingGap", "ProposedPath",
                     "Uncertainty", "ContextRequest"):
        required = schema["$defs"][def_name]["required"]
        for field in required:
            assert field in system, f"l1-triage 2.0.2 未声明 {def_name} 必填字段 {field}"
    assert "不得为 0 或负数" in system, "必须声明行号 >= 1"
    assert "不得添加" in system, "必须声明禁止协议外字段"


def test_l1_triage_203_declares_enum_and_type_constraints() -> None:
    """回归：2.0.2 修 suggested_sources 后，20 个 L1 候选仍 failed——
    uncertainties.impact（枚举 low/medium/high 未声明，17 个）+
    suggested_sinks.line int_type（字符串 "10" 形式，3 个）。
    2.0.4 必须声明：impact 枚举值 + resolvable boolean + line JSON 整数禁字符串。
    """

    system = PromptRegistry().load("l1-triage", "2.0.4").system_template
    assert "low/medium/high" in system, "必须声明 uncertainties.impact 枚举值"
    assert "resolvable" in system and "boolean" in system, "必须声明 resolvable 类型"
    assert "JSON 整数" in system and "禁止字符串" in system, "必须声明 line 为 JSON number"


def _extract_all_enums(node: object, out: list[tuple[str, list[str]]]) -> None:
    """递归提取 schema 中所有 enum 约束（路径 -> 值列表）。"""
    if isinstance(node, dict):
        if "enum" in node and isinstance(node["enum"], list):
            out.append((node.get("description") or "", node["enum"]))
        for v in node.values():
            _extract_all_enums(v, out)
    elif isinstance(node, list):
        for v in node:
            _extract_all_enums(v, out)


@pytest.mark.parametrize(
    ("prompt_id", "version", "schema_file"),
    [
        ("l1-triage", "2.0.4", "ai_l1_triage_output.schema.json"),
        ("l2-review", "3.0.4", "ai_l2_review_output.schema.json"),
        ("finalization", "1.0.3", "ai_finalization_output.schema.json"),
    ],
)
def test_prompt_declares_every_schema_enum_value(prompt_id, version, schema_file) -> None:
    """回归：提示词未声明 schema 枚举值会导致 literal_error。

    已发生一次——run 20260808T192140Z 的 17 个 L1 候选因 l1-triage 未声明
    uncertainties.impact 枚举（low/medium/high）全部 failed；l2-review 3.0.2 的
    guard_status 枚举（present_effective/present_bypassable/present_partial）也
    长期未声明（当前数据全是 unknown 属侥幸）。本测试从 schema 提取全部 enum，
    逐值断言提示词已声明——新增枚举字段漏声明时直接拦截。
    """

    schema = json.loads((WORKSPACE_ROOT / "schemas" / schema_file).read_text(encoding="utf-8"))
    system = PromptRegistry().load(prompt_id, version).system_template

    enums: list[tuple[str, list[str]]] = []
    _extract_all_enums(schema, enums)
    assert enums, f"{schema_file} 未提取到任何 enum"
    for desc, values in enums:
        missing = [v for v in values if v not in system]
        assert not missing, (
            f"{prompt_id} {version} 提示词未声明枚举值 {missing}"
            f"（{desc[:60]}）"
        )


def test_l1_triage_204_declares_source_ref_int_and_expansion_temperance() -> None:
    """回归：run 20260808T194354Z 暴露两个新缺口——

    1. 3 个候选 failed：suggested_paths.0.source_ref/sink_ref 输出字符串 "0"
       （schema 要求 JSON 整数）——2.0.4 必须声明零基索引 JSON 整数禁字符串。
    2. 14 个 L1 候选 incomplete（context_expansion_stalled）：L1 上下文薄，
       AI 重复请求同一 target 空转——2.0.4 必须声明扩片节制（与 l2-review 3.0.1 对齐）。
    """

    system = PromptRegistry().load("l1-triage", "2.0.4").system_template
    assert "source_ref" in system and "sink_ref" in system
    assert "JSON 整数" in system and "禁止字符串形式" in system
    assert "零基索引" in system
    assert "扩片节制" in system and "空转扩片" in system and "每轮最多 3 个" in system


def test_l2_review_304_unlocks_confidence_and_mechanism_verdict() -> None:
    """联合裁决 v1：3.0.4 解锁 confidence + 机制内裁决。

    背景（run 194354Z）：138/139 confidence=low（提示词"存在关键 blocking_gaps 时
    不得给 high"）→ AI 强判定（6 个四要素全真）也被压成 unresolved；红线 23 规定
    exfil unverified 时只能 unresolved。3.0.4 改为：confidence 表示"对判定的信心"，
    exfil 缺口只降级不禁止 high；机制未排除时四要素强判可 supports（不再强制
    unresolved）。
    """

    system = PromptRegistry().load("l2-review", "3.0.4").system_template
    assert "不是证据完备度" in system, "confidence 语义必须重定义"
    assert "不禁止 high" in system, "exfil 缺口不得禁止 high"
    assert "机制内裁决" in system, "必须声明机制内裁决语义"
    assert "不再强制 unresolved" in system, "exfil unverified 不得强制 unresolved"
    assert "不得输出 refutes_candidate" in system, "红线 23 否定禁令必须保留"
