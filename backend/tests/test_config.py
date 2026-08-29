from __future__ import annotations

import json

from app.config import WORKSPACE_ROOT, AISettings, Settings


def test_settings_source_priority_and_nested_environment_override(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "AI_APK_TRACER_PORT=8100\n"
        "AI_APK_TRACER_AI__MODEL=dotenv-model\n"
        "AI_APK_TRACER_AI__CANDIDATE_CONCURRENCY=7\n",
        "utf-8",
    )
    monkeypatch.setenv("AI_APK_TRACER_PORT", "8200")
    monkeypatch.setenv("AI_APK_TRACER_AI__MODEL", "env-model")

    settings = Settings(
        _env_file=tmp_path / ".env",
        port=8001,
        ai={
            "enabled": False,
            "model": "yaml-model",
            "candidate_concurrency": 3,
        },
    )

    assert settings.port == 8200
    assert settings.ai.model == "env-model"
    assert settings.ai.candidate_concurrency == 7
    assert settings.ai.enabled is False
    assert settings.log_level == "INFO"


def test_legacy_timeout_only_falls_back_when_read_timeout_is_absent() -> None:
    assert AISettings(timeout_seconds=17).read_timeout_seconds == 17.0
    assert AISettings(timeout_seconds=17, read_timeout_seconds=23).read_timeout_seconds == 23.0


def test_handwritten_config_schema_describes_recent_fields_without_old_consts() -> None:
    document = json.loads((WORKSPACE_ROOT / "schemas/config.schema.json").read_text("utf-8"))
    properties = document["properties"]

    assert "平台 API" in properties["analysis_platform_api"]["description"]
    assert "优先于 timeout_seconds" in properties["ai"]["properties"]["read_timeout_seconds"]["description"]
    assert "逻辑 AI 调用" in properties["context_budget"]["properties"]["max_requests_per_run"]["description"]
    assert "const" not in properties["source_analysis"]["properties"]["decompiler"]
    assert "const" not in properties["rule_runtime"]["properties"]["wall_timeout_seconds"]


# ---------------------------------------------------------------------------
# T0.7：探索轨/核验/资产/批量/报告配置段
# ---------------------------------------------------------------------------

def test_explorer_and_related_sections_defaults() -> None:
    settings = Settings()
    assert settings.explorer.enabled is False
    assert settings.explorer.max_candidates_per_run == 50
    assert settings.explorer.auto_promote is False
    assert settings.explorer.max_rounds_per_entry == 4
    assert settings.explorer.max_requests_per_entry == 20
    assert settings.explorer.max_requests_per_candidate == 4
    assert settings.explorer.deep_dive_prompt_version == "explorer-deep-dive/1.0.0"
    assert settings.explorer.call_tree.max_depth == 8
    assert settings.explorer.call_tree.max_nodes == 500
    assert settings.verify.enabled is False
    assert settings.verify.max_rounds_per_candidate == 4
    assert settings.verify.max_requests_per_candidate == 12
    assert settings.verify.fallback_to_single_turn_l2 is True
    assert settings.api_surface.enabled is False
    assert settings.assets.enabled is False
    assert settings.batch.max_concurrent_runs == 2
    assert settings.batch.max_ai_calls == 0
    assert settings.batch.max_wall_seconds == 0
    assert settings.report.allow_executable_poc is False
    assert settings.report.require_confirmed_finding is True


def test_nested_env_override_explorer(monkeypatch) -> None:
    monkeypatch.setenv("AI_APK_TRACER_EXPLORER__MAX_ROUNDS_PER_ENTRY", "6")
    monkeypatch.setenv("AI_APK_TRACER_VERIFY__ENABLED", "true")
    monkeypatch.setenv("AI_APK_TRACER_BATCH__MAX_AI_CALLS", "25")
    settings = Settings()
    assert settings.explorer.max_rounds_per_entry == 6
    assert settings.verify.enabled is True
    assert settings.batch.max_ai_calls == 25


def test_default_yaml_loads_with_new_sections() -> None:
    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    assert settings.explorer.enabled is False
    assert settings.explorer.prompt_version == "explorer/1.0.0"
    assert settings.verify.fallback_to_single_turn_l2 is True
    assert settings.api_surface.enabled is False
    assert settings.assets.enabled is False
    assert settings.batch.max_ai_calls == 0
    assert settings.report.require_confirmed_finding is True
    # 基线：现有段以 default.yaml 为准（既有 config.py/yaml 漂移不归因 T0.7；评审 R-7）
    assert settings.context_budget.max_output_tokens == 8000
    get_settings.cache_clear()


def test_config_schema_describes_new_sections() -> None:
    document = json.loads((WORKSPACE_ROOT / "schemas/config.schema.json").read_text("utf-8"))
    properties = document["properties"]
    required = set(document["required"])
    for section in ("explorer", "verify", "api_surface", "assets", "batch", "report"):
        assert section in properties, f"schema 缺段 {section}"
        assert section in required, f"schema 顶层 required 缺 {section}（评审 R-5）"
    # Path 类型与关键字段（评审 R-3/R-5）
    assert properties["assets"]["properties"]["data_root"]["type"] == "string"
    assert properties["explorer"]["properties"]["max_rounds_per_entry"]["default"] == 4
    assert properties["verify"]["properties"]["fallback_to_single_turn_l2"]["default"] is True
    assert properties["batch"]["properties"]["max_ai_calls"]["default"] == 0
    assert properties["report"]["properties"]["allow_executable_poc"]["default"] is False


def test_config_schema_describes_thinking_and_budget_fields() -> None:
    document = json.loads((WORKSPACE_ROOT / "schemas/config.schema.json").read_text("utf-8"))
    ai = document["properties"]["ai"]["properties"]
    funnel = document["properties"]["funnel"]["properties"]
    explorer = document["properties"]["explorer"]["properties"]

    # ai 段：思维控制/输出上限/preflight 协议/请求总兜底（与 config.py AISettings 对齐）
    assert ai["disable_thinking"]["default"] is True
    assert ai["thinking_param"]["default"] == "thinking"
    assert ai["thinking_level"]["type"] == ["string", "null"]
    assert ai["max_output_tokens"]["type"] == ["integer", "null"]
    assert ai["max_output_tokens"]["maximum"] == 384000
    assert ai["preflight_strict_protocol"]["default"] is False
    assert ai["request_timeout_seconds"]["type"] == ["number", "null"]
    assert ai["read_timeout_seconds"]["default"] == 240.0
    # funnel 段：L1 预算开关；l2_ai_undecidable_route 在 config.py 与
    # candidate_funnel.py 均有定义与消费，schema 不得移除（漂移审计曾误判为死字段）
    assert funnel["demote_unproven_flow"]["default"] is False
    assert funnel["l1_priority_clean"]["default"] is False
    assert funnel["l1_skip_ai"]["default"] is True
    assert funnel["l2_ai_undecidable_route"]["type"] == "boolean"
    # explorer 段：入口并行度、可空候选预算与 sink taxonomy 路径
    assert explorer["entry_concurrency"]["default"] == 4
    assert explorer["entry_concurrency"]["maximum"] == 16
    assert explorer["max_candidates_per_run"]["type"] == ["integer", "null"]
    assert explorer["max_candidates_per_run"]["default"] == 50
    assert explorer["custom_sink_taxonomy_path"]["type"] == ["string", "null"]
    # context_budget.max_requests_per_run 支持整型或 null（None = 无上限）
    max_requests_per_run = document["properties"]["context_budget"]["properties"]["max_requests_per_run"]
    assert max_requests_per_run["type"] == ["integer", "null"]
    assert max_requests_per_run["default"] == 140


def test_batch_zero_semantics() -> None:
    from app.config import BatchSettings

    settings = BatchSettings(max_ai_calls=0, max_wall_seconds=0)
    assert settings.max_ai_calls == 0
    assert settings.max_wall_seconds == 0


def test_resolved_assets_data_root(tmp_path, monkeypatch) -> None:
    from app.config import Settings as S

    monkeypatch.chdir(tmp_path)
    settings = S(assets={"data_root": ".ai-apk-tracer/assets"})
    resolved = settings.resolved_assets_data_root()
    assert resolved.is_absolute()
    assert str(resolved).endswith(".ai-apk-tracer/assets")


def test_prompt_version_declared_matches_registry() -> None:
    import yaml

    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    registry = yaml.safe_load((WORKSPACE_ROOT / "prompts/registry.yaml").read_text("utf-8"))
    registered = {(entry["id"], entry["version"]) for entry in registry["prompts"]}
    # explorer-deep-dive 已注册（T0.3），默认值必须匹配
    deep_dive_id, deep_dive_version = settings.explorer.deep_dive_prompt_version.split("/")
    assert (deep_dive_id, deep_dive_version) in registered
    # explorer 已注册（T2.5a 协议层交付），默认值必须匹配
    explorer_id, explorer_version = settings.explorer.prompt_version.split("/")
    assert (explorer_id, explorer_version) in registered
    # verify 已在 T0.9 注册（先声明后注册闭合）
    verify_id, verify_version = settings.verify.prompt_version.split("/")
    assert (verify_id, verify_version) in registered
    get_settings.cache_clear()


def test_unknown_section_ignored() -> None:
    settings = Settings(explorer_bogus={"x": 1})
    assert settings.explorer.enabled is False


def test_batch_invalid_max_ai_calls_rejected() -> None:
    import pytest
    from pydantic import ValidationError

    from app.config import BatchSettings

    with pytest.raises(ValidationError):
        BatchSettings(max_ai_calls=-1)
    # 正向：缺省默认 0（评审 R-6）
    assert BatchSettings().max_ai_calls == 0


def test_explorer_call_tree_invalid_rejected() -> None:
    import pytest
    from pydantic import ValidationError

    from app.config import ExplorerSettings

    with pytest.raises(ValidationError):
        ExplorerSettings(call_tree={"max_depth": 0})
