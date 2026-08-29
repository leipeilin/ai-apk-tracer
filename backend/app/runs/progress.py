"""Run 级双轨进度计算（track-progress-console，方案 §3.3 模块 A）。

只读聚合 run_dir 产物与 manifest，供 GET /api/runs/{id} 的 progress 块使用。
全部字段多级降级为 null（不伪造 0）；单个产物畸形按字段降级，不抛异常
（对齐 explorer_candidates 端点的保守哲学）。口径详见
docs/analysis/console-ui/2026-08-29-track-progress-console-implementation-plan.md §3.4。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.analysis.rule_runner import RULE_ARTIFACT_KEYS

LOGGER = logging.getLogger(__name__)


def _last_stage(manifest: dict[str, Any] | None, stage_name: str) -> dict[str, Any] | None:
    """manifest.stages 中同名 stage 的最后一条；缺失或结构不符返回 None。"""

    if not isinstance(manifest, dict):
        return None
    stages = manifest.get("stages")
    if not isinstance(stages, list):
        return None
    result = None
    for stage in stages:
        if isinstance(stage, dict) and stage.get("name") == stage_name:
            result = stage
    return result


def _stage_summary(manifest: dict[str, Any] | None, stage_name: str) -> dict[str, Any] | None:
    stage = _last_stage(manifest, stage_name)
    summary = stage.get("summary") if isinstance(stage, dict) else None
    return summary if isinstance(summary, dict) else None


def _manifest_int(source: dict[str, Any] | None, key: str) -> int | None:
    if isinstance(source, dict) and isinstance(source.get(key), int) and not isinstance(source.get(key), bool):
        return int(source[key])
    return None


def _rule_progress(run_dir: Path, manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    """规则轨：total=规则任务总量；processed=已处理数（成功+失败）；failed=失败数。

    total 降级链：rule_prescan summary.rule_total_count（终态）→ manifest 顶层
    rule_total_count（orchestrator 运行中提前写入）→ null。processed 数
    rule-results/*.json 并排除 RULE_ARTIFACT_KEYS 三类规则产物词干（评审 R-2
    ——产物与规则 result 同目录，落盘路径不可挪）。
    """

    summary = _stage_summary(manifest, "rule_prescan")
    total = _manifest_int(summary, "rule_total_count")
    if total is None:
        total = _manifest_int(manifest if isinstance(manifest, dict) else None, "rule_total_count")

    results_dir = run_dir / "rule-results"
    dir_exists = results_dir.is_dir()
    processed = 0
    if dir_exists:
        processed = sum(1 for path in results_dir.glob("*.json") if path.stem not in RULE_ARTIFACT_KEYS)

    failed = None
    if summary is not None and isinstance(summary.get("rule_failures"), list):
        failed = len(summary["rule_failures"])

    if total is None and not dir_exists:
        return None
    return {"total": total, "processed": processed, "failed": failed}


def _count_valid_records(path: Path) -> int | None:
    """逐行 JSON 校验计数（崩溃安全 jsonl 的记录数）；任一非空行畸形 → None
    （保守不显示可疑数字，评审 N-2）。"""

    try:
        count = 0
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                json.loads(stripped)
                count += 1
        return count
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _observations_entry_count(path: Path) -> int | None:
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    entries = payload.get("entries") if isinstance(payload, dict) else None
    return len(entries) if isinstance(entries, list) else None


def _explorer_progress(run_dir: Path, manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    """探索轨：total=攻击面总量（有效入口数）；explored=已探索；unexplored=未探索。

    total 降级链：explorer summary.entry_count（终态，orchestrator.py:1258）→
    manifest 顶层 explorer_total_count（explore_all 前提前写入）→ null。原始
    api_entry_table.json 条目无 method_id 键（评审 R-1），JSON 静态计数不可行。
    explored 降级链：summary.entries_explored（终态精确）→
    observations-partial.jsonl 校验行数（运行中近似，唯一偏差源为 worker 异常
    条目——终态计入而 partial 缺行，运行中略小）→ observations.json entries 数
    （历史终态 run）→ null。
    """

    summary = _stage_summary(manifest, "explorer")
    manifest_dict = manifest if isinstance(manifest, dict) else None
    stage_exists = _last_stage(manifest, "explorer") is not None
    explorer_dir = run_dir / "explorer"
    has_products = any(
        (explorer_dir / name).is_file()
        for name in ("candidates.json", "observations.json", "observations-partial.jsonl")
    )
    # 运行早期窗口（代码审查 C-1）：stage summary 未落、首个入口未完成（无任何
    # 产物文件）时，explore_all 前写入的顶层 explorer_total_count 是唯一"探索轨
    # 在跑"信号——缺失它会把运行中的探索轨误判为"未启用"。
    has_top_level_total = _manifest_int(manifest_dict, "explorer_total_count") is not None
    if not stage_exists and not has_products and not has_top_level_total:
        return None

    total = _manifest_int(summary, "entry_count")
    if total is None:
        total = _manifest_int(manifest_dict, "explorer_total_count")

    explored = _manifest_int(summary, "entries_explored")
    if explored is None:
        partial_path = explorer_dir / "observations-partial.jsonl"
        if partial_path.is_file():
            explored = _count_valid_records(partial_path)
    if explored is None:
        observations_path = explorer_dir / "observations.json"
        if observations_path.is_file():
            explored = _observations_entry_count(observations_path)

    unexplored = _manifest_int(summary, "entries_unexplored")
    if unexplored is None and total is not None and explored is not None:
        unexplored = max(total - explored, 0)

    return {"total": total, "explored": explored, "unexplored": unexplored}


def build_run_progress(run_dir: Path, manifest: dict[str, Any] | None) -> dict[str, Any]:
    """双轨进度聚合；轨级无任何信号时该轨为 null（探索轨未启用是常态）。"""

    return {
        "rules": _rule_progress(run_dir, manifest),
        "explorer": _explorer_progress(run_dir, manifest),
    }
