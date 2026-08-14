from __future__ import annotations

import json

from app.config import AISettings, Settings, WORKSPACE_ROOT


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
