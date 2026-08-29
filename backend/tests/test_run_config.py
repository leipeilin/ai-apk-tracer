"""build_run_config explorer 段三态单测（explorer-run-toggle，方案 §3.5）。"""

from __future__ import annotations

from pathlib import Path

from app.config import ExplorerSettings, Settings
from app.runs.run_config import build_run_config


def _settings(tmp_path: Path) -> Settings:
    # 显式 explorer 覆盖便于断言透传；enabled=True 仅为本测试实例的设置值
    # （运行时默认 True 由 default.yaml/get_settings 提供，pytest 直构为模型默认）
    return Settings(
        database_path=tmp_path / "tracer.sqlite3",
        explorer=ExplorerSettings(enabled=True, max_candidates_per_run=7),
    )


def test_explorer_enabled_explicit_true(tmp_path: Path) -> None:
    config = build_run_config(_settings(tmp_path), explorer_enabled=True)
    assert config["explorer"]["enabled"] is True
    # 其余 explorer 字段为 settings 原值透传（审计，不做任务级覆盖）
    assert config["explorer"]["max_candidates_per_run"] == 7


def test_explorer_enabled_explicit_false(tmp_path: Path) -> None:
    config = build_run_config(_settings(tmp_path), explorer_enabled=False)
    assert config["explorer"]["enabled"] is False
    assert config["explorer"]["max_candidates_per_run"] == 7


def test_explorer_enabled_none_follows_settings(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    config = build_run_config(settings)
    assert config["explorer"]["enabled"] is settings.explorer.enabled
    assert config["explorer"]["auto_promote"] is False
