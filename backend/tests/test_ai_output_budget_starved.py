"""输出预算饿死（output_budget_starved）分类与统计单列测试。

T1 实证（run 20260829T105238Z_fc0d0e01d0e0_b84daab7）：始终思考模型
reasoning_tokens 吃满 max_tokens（finish_reason=length）导致 85 次 L2
空响应被计为 schema_invalid——本文件锁定两层行为：
1. AI facade：空响应 + finish_reason=length 跳过 repair 直接单列分类；
2. orchestrator：ai_analysis stage summary 单列计数（仍计入 failed）。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import httpx

from app.analysis.context_builder import ContextBuilder
from app.analysis.indexer import build_code_index
from app.analysis.orchestrator import ScanOrchestrator
from app.analysis.ai import OpenAICompatibleAnalyzer
from app.config import AISettings, Settings, SourceAnalysisSettings, StorageSettings
from app.runs.storage import RunStorage
from app.shared.repository import SQLiteRepository

API_KEY_ENV = "AI_OUTPUT_BUDGET_TEST_KEY"
BASE_URL = "https://ai-output-budget.invalid/v1"


def _settings(**values) -> AISettings:
    defaults = {
        "enabled": True,
        "base_url": BASE_URL,
        "api_key_env": API_KEY_ENV,
        "model": "test-model",
        "allow_external_code": True,
        "timeout_seconds": 1,
    }
    return AISettings(**{**defaults, **values})


def _analyzer(monkeypatch, handler) -> OpenAICompatibleAnalyzer:
    monkeypatch.setenv(API_KEY_ENV, "unit-test-token")
    return OpenAICompatibleAnalyzer(
        _settings(),
        transport=httpx.MockTransport(handler),
        retry_backoff_seconds=0,
    )


def _empty_response(finish_reason: str | None) -> httpx.Response:
    choice: dict[str, Any] = {"message": {"content": ""}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return httpx.Response(200, json={
        "choices": [choice],
        "usage": {
            "completion_tokens": 8000,
            "completion_tokens_details": {"reasoning_tokens": 7995},
        },
    })


def test_empty_content_with_length_finish_skips_repair_and_starves(monkeypatch) -> None:
    """reasoning 吃满 max_tokens 的空响应：单列 output_budget_starved，不烧 repair。"""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _empty_response("length")

    result = asyncio.run(_analyzer(monkeypatch, handler).review_l2(
        {"rule_id": "STARVED", "evidence_level": "L2"},
        {"slice_id": "starved", "contexts": []},
    ))

    # 同预算 repair 必然再次饿死（T1 实证 83 次全败）——跳过，只发一次请求
    assert request_count == 1
    assert result["status"] == "failed"
    assert result["classification"] == "output_budget_starved"
    assert result["error"]["code"] == "AI_OUTPUT_BUDGET_STARVED"
    assert result["recoverable"] is False
    assert result["circuit_breaking"] is False
    metadata = result["metadata"]
    assert metadata["empty_initial_content"] is True
    assert metadata["finish_reason"] == "length"
    assert metadata["completion_tokens"] == 8000
    assert metadata["reasoning_tokens"] == 7995
    assert metadata["initial_response_hash"] == hashlib.sha256(b"").hexdigest()
    # 未进入解析/repair 路径——不产生协议违规口径的字段
    assert "format_repair_attempted" not in metadata
    assert "initial_validation_errors" not in metadata


def test_empty_content_without_length_finish_keeps_repair_path(monkeypatch) -> None:
    """非 length 的空响应（如提前停止）保持既有 repair 路径——不误分类为饿死。"""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _empty_response("stop")

    result = asyncio.run(_analyzer(monkeypatch, handler).review_l2(
        {"rule_id": "STOPPED", "evidence_level": "L2"},
        {"slice_id": "stopped", "contexts": []},
    ))

    assert request_count == 2  # 初始 + repair（既有行为不变）
    assert result["status"] == "failed"
    assert result["classification"] == "schema_invalid"
    assert result["metadata"]["format_repair_attempted"] is True


def test_starved_content_with_prose_finish_keeps_repair_path(monkeypatch) -> None:
    """非空响应不触发饿死分类——即使 finish_reason=length（截断属既有口径）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{\"summary\": \"截断"}, "finish_reason": "length"}],
        })

    result = asyncio.run(_analyzer(monkeypatch, handler).review_l2(
        {"rule_id": "TRUNCATED", "evidence_level": "L2"},
        {"slice_id": "truncated", "contexts": []},
    ))

    assert result["status"] == "failed"
    assert result["classification"] != "output_budget_starved"


# ---------------------------------------------------------------------------
# orchestrator：ai_analysis stage summary 单列计数
# ---------------------------------------------------------------------------


class _FakeStarvedAI:
    """preflight 通过 + analyze 恒返回 output_budget_starved 失败。"""

    def __init__(self, classification: str) -> None:
        self._classification = classification
        self.analyze_calls = 0

    async def preflight(self) -> dict[str, Any]:
        return {
            "status": "passed",
            "classification": "configured",
            "recoverable": False,
            "circuit_breaking": False,
            "http_status": None,
            "message": "AI preflight 通过",
            "metadata": {"attempts": 0},
        }

    async def analyze(
        self, candidate: dict[str, Any], slice_document: dict[str, Any],
        previous_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.analyze_calls += 1
        return {
            "status": "failed",
            "classification": self._classification,
            "recoverable": False,
            "circuit_breaking": False,
            "http_status": 200,
            "message": "测试失败注入",
            "error": {
                "code": f"AI_{self._classification.upper()}",
                "classification": self._classification,
                "recoverable": False,
                "circuit_breaking": False,
                "http_status": 200,
                "message": "测试失败注入",
            },
            "metadata": {"attempts": 1},
        }


def _orchestrator(tmp_path: Path, ai: _FakeStarvedAI) -> tuple[ScanOrchestrator, RunStorage, str, Path]:
    settings = Settings(
        database_path=tmp_path / "tracer.sqlite3",
        storage=StorageSettings(data_root=tmp_path / "data"),
        source_analysis=SourceAnalysisSettings(enabled=False),
    )
    repository = SQLiteRepository(settings.resolved_database_path())
    repository.initialize()
    storage = RunStorage(settings.resolved_data_root(), settings.storage)
    run_id = "20260830T000000Z_aaaaaaaaaaaa_bbbbbbbb"
    run_dir = storage.runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text("{}", "utf-8")
    orchestrator = ScanOrchestrator(settings, repository, storage)
    orchestrator.ai = ai
    return orchestrator, storage, run_id, run_dir


def _candidate() -> dict[str, Any]:
    return {
        "candidate_id": "cand_starved_0001",
        "rule_id": "SERVICE_BINDER_CALLER_CHECK_MISSING",
        "evidence_level": "L2",
        "component": "service",
        "component_name": "com.example.A",
        "locations": [{"artifact": "code", "path": "com/example/A.java", "line": 3}],
    }


def _build_code_index(tmp_path: Path) -> dict[str, Any]:
    source_root = tmp_path / "sources"
    path = source_root / "com" / "example" / "A.java"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "package com.example;\n"
        "public class A {\n"
        "  public void entry(String input) {\n"
        "  }\n"
        "}\n",
        "utf-8",
    )
    return build_code_index(source_root, tmp_path / "index" / "code-index.json")


def test_stage_summary_counts_output_budget_starved_separately(tmp_path: Path) -> None:
    """饿死失败进 summary 单列（仍计入 failed），schema_invalid 不混入该计数。"""
    orchestrator, storage, run_id, run_dir = _orchestrator(
        tmp_path, _FakeStarvedAI("output_budget_starved")
    )
    builder = ContextBuilder(_build_code_index(tmp_path))
    candidates = [_candidate()]
    slices = {0: {"slice_id": "s1", "contexts": []}}

    asyncio.run(orchestrator._run_ai_stage(
        run_id, candidates, [0], slices, builder, False, run_dir, ai_enabled=True,
    ))

    manifest = storage.read_manifest(run_id)
    stage = next(s for s in manifest["stages"] if s["name"] == "ai_analysis")
    summary = stage["summary"]
    assert summary["output_budget_starved"] == 1
    assert summary["failed"] == 1
    assert summary["completed"] == 0


def test_stage_summary_does_not_count_other_failures_as_starved(tmp_path: Path) -> None:
    orchestrator, storage, run_id, run_dir = _orchestrator(
        tmp_path, _FakeStarvedAI("schema_invalid")
    )
    builder = ContextBuilder(_build_code_index(tmp_path))
    candidates = [_candidate()]
    slices = {0: {"slice_id": "s1", "contexts": []}}

    asyncio.run(orchestrator._run_ai_stage(
        run_id, candidates, [0], slices, builder, False, run_dir, ai_enabled=True,
    ))

    manifest = storage.read_manifest(run_id)
    stage = next(s for s in manifest["stages"] if s["name"] == "ai_analysis")
    summary = stage["summary"]
    assert summary["output_budget_starved"] == 0
    assert summary["failed"] == 1
