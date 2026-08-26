#!/usr/bin/env python
"""核验（verify）轨定向验证 harness（M2 收尾-2，指引 §4.2/§5/§9）。

从已跑完的真实 run 取 L2 核验候选（evidence_level="L2"——与
ScanOrchestrator._verify_path_for 同分流口径）与探索候选映射，按
_verify_candidate 同口径装配 VerifyAgent，调用正式
OpenAICompatibleAnalyzer.verify_entry——定位 verify 全量 fallback 根因
（真实 run：health 52/52、shop 29/29 全 fallback）。

与 orchestrator 的两点刻意偏差（探针需求，不改 verify 代码路径）：
1. ai_call 注入捕获层——orchestrator 在 verify 失败回退时只保留
   terminated_by，丢弃 analyzer 返回的 classification/message；探针在
   注入层还原归因（schema_invalid/response_invalid → AI 输出契约问题；
   transient_failure/rate_limited → 网络；auth_failed/model_not_found/
   circuit_open → fatal）；
2. 轮审计落盘指向 <run_dir>/probe-verify/<时间戳>/（VerifyAgent 的
   run_dir 仅用于 verify/observations.json 落盘）——不追加真实 run 的
   observations，防污染 M2 验收统计口径。

每候选记录：status / terminated_by / fallback 语义 / AI 轮归因（分类
+ message 样例）/ 聚合层证据回查（adapt_verify_result +
evidence_contexts_for + validate_ai_evidence_references——与
evidence_integrity_validation 阶段同函数）。

门槛判定（指引 §4.2）：N 个候选中 completed ≥ 1 且 schema_invalid 归因
清楚（失败时给出分类分布与首条 message 样例）→ PASS；否则 FAIL。

用法：
  backend/.venv/bin/python scripts/probe_verify_entry.py --run-id <id> --max-candidates 3
  backend/.venv/bin/python scripts/probe_verify_entry.py --run-id <id> --dry-run
  backend/.venv/bin/python scripts/probe_verify_entry.py --run-id <id> \\
      --candidates candidate_xxx,candidate_yyy

输出：结构化 JSON（stdout）+ 人读摘要（stderr）。
退出码：0 = dry-run 或 PASS；1 = FAIL；2 = 输入/环境错误。

设计：docs/analysis/milestones/2026-08-23-m2m3-forward-guidance.md §4.2/§9。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = WORKSPACE_ROOT / "backend"
RUNS_ROOT = WORKSPACE_ROOT / ".ai-apk-tracer" / "runs"
sys.path.insert(0, str(BACKEND_ROOT))

# 坑 1（同 probe_explorer_entry）：dotenv 不注入 os.environ，ai.py 直接读后者
try:
    from dotenv import load_dotenv

    load_dotenv(WORKSPACE_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from app.analysis.ai_models import VerifyInput
from app.analysis.call_tree import CallTreeService
from app.analysis.index_store import SQLiteCodeIndexReader
from app.analysis.verify_agent import (
    VerifyAgent,
    adapt_verify_result,
    evidence_contexts_for,
)
from app.config import get_settings
from app.findings.evidence import validate_ai_evidence_references

LOGGER = logging.getLogger("probe_verify_entry")

# analyzer classification → 归因大类（与 ai.py 失败分类全集对齐）
_SCHEMA_CLASSIFICATIONS = {"schema_invalid", "response_invalid"}
_NETWORK_CLASSIFICATIONS = {"transient_failure", "rate_limited"}
_FATAL_CLASSIFICATIONS = {"auth_failed", "model_not_found", "circuit_open"}


def _attribution(classification: str | None) -> str:
    if classification in _SCHEMA_CLASSIFICATIONS:
        return "ai_output_contract"
    if classification in _NETWORK_CLASSIFICATIONS:
        return "network"
    if classification in _FATAL_CLASSIFICATIONS:
        return "fatal"
    return "other"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"[probe-verify] 无法读取 {path}: {exc}") from exc


def _load_explorer_map(run_dir: Path) -> dict[str, dict[str, Any]]:
    """与 orchestrator._load_explorer_candidates 同口径（容错空映射）。"""

    path = run_dir / "explorer" / "candidates.json"
    if not path.is_file():
        return {}
    loaded = _load_json(path)
    if not isinstance(loaded, list):
        return {}
    return {
        str(entry.get("candidate_id")): entry
        for entry in loaded if isinstance(entry, dict) and entry.get("candidate_id")
    }


def _select_l2_candidates(
    candidates: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    """L2 过滤（= _verify_path_for 分流口径）+ 取样（--candidates 优先，
    否则按 rule_id 分散取前 N——异构归因更有代表性）。"""

    l2 = [c for c in candidates if c.get("evidence_level") == "L2"]
    if args.candidates:
        wanted = {c.strip() for c in args.candidates.split(",") if c.strip()}
        selected = [c for c in l2 if c.get("candidate_id") in wanted]
        missing = wanted - {c.get("candidate_id") for c in selected}
        if missing:
            raise SystemExit(f"[probe-verify] 指定候选不存在或非 L2: {sorted(missing)}")
        return selected[: args.max_candidates]
    seen_rules: set[str] = set()
    selected: list[dict[str, Any]] = []
    for candidate in l2:
        rule = str(candidate.get("rule_id") or candidate.get("chain_key") or "unknown")
        if rule in seen_rules:
            continue
        seen_rules.add(rule)
        selected.append(candidate)
        if len(selected) >= args.max_candidates:
            break
    return selected


async def _run_probe(args: argparse.Namespace) -> int:
    settings = get_settings()
    run_dir = RUNS_ROOT / args.run_id
    if not run_dir.is_dir():
        print(f"[probe-verify] run 目录不存在: {run_dir}", file=sys.stderr)
        return 2
    pool = _load_json(run_dir / "slices" / "candidates.json")
    if not isinstance(pool, list) or not pool:
        print(f"[probe-verify] 主链候选池缺失: {run_dir / 'slices' / 'candidates.json'}", file=sys.stderr)
        return 2
    code_index = _load_json(run_dir / "index" / "code-index.json")
    database_path = str((code_index or {}).get("database_path") or "")
    if not database_path or not Path(database_path).is_file():
        print(f"[probe-verify] code index 不可用: {database_path!r}", file=sys.stderr)
        return 2

    selected = _select_l2_candidates(pool, args)
    explorer_map = _load_explorer_map(run_dir)
    probe_dir = run_dir / "probe-verify" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    l2_total = sum(1 for c in pool if c.get("evidence_level") == "L2")

    plan = {
        "run_id": args.run_id,
        "run_dir": str(run_dir),
        "probe_dir": str(probe_dir),
        "candidate_pool": len(pool),
        "l2_total": l2_total,
        "selected_candidates": [
            {"candidate_id": c.get("candidate_id"),
             "rule_id": c.get("rule_id"),
             "explorer_candidate_id": c.get("explorer_candidate_id")}
            for c in selected
        ],
        "verify_settings": {
            "max_rounds_per_candidate": settings.verify.max_rounds_per_candidate,
            "max_requests_per_candidate": settings.verify.max_requests_per_candidate,
            "fallback_to_single_turn_l2": settings.verify.fallback_to_single_turn_l2,
        },
        "dry_run": args.dry_run,
    }
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    from app.analysis.ai import OpenAICompatibleAnalyzer

    analyzer = OpenAICompatibleAnalyzer(settings.ai)
    reader = SQLiteCodeIndexReader(code_index)
    ai_rounds: list[dict[str, Any]] = []
    per_candidate: list[dict[str, Any]] = []

    async def probed_verify_call(model_input: VerifyInput) -> dict[str, Any]:
        result = await analyzer.verify_entry(model_input)
        ai_rounds.append({
            "candidate_id": model_input.candidate_id,
            "status": result.get("status"),
            "classification": result.get("classification"),
            "attribution": _attribution(result.get("classification")),
            "recoverable": result.get("recoverable"),
            "circuit_breaking": result.get("circuit_breaking"),
            "message": str(result.get("message") or "")[:300] or None,
        })
        return result

    try:
        call_tree = CallTreeService(run_dir, reader, settings.explorer.call_tree)
        started = time.monotonic()
        for candidate in selected:
            candidate_id = str(candidate.get("candidate_id"))
            explorer_candidate = explorer_map.get(
                str(candidate.get("explorer_candidate_id") or ""))
            record: dict[str, Any] = {
                "candidate_id": candidate_id,
                "rule_id": candidate.get("rule_id"),
                "has_explorer_candidate": explorer_candidate is not None,
            }
            try:
                agent = VerifyAgent(
                    probed_verify_call, call_tree, settings.verify, probe_dir, reader
                )
                result = await agent.verify(candidate, explorer_candidate)
                record.update({
                    "status": result.get("status"),
                    "terminated_by": result.get("terminated_by"),
                    "rounds": len(result.get("rounds") or []),
                    "ai_requests_used": agent.ai_requests_used,
                    "read_requests_used": agent.read_requests_used,
                    "claims": len(result.get("claims") or []),
                    "undecided_claim_indices": result.get("undecided_claim_indices"),
                    "fallback_semantics": result.get("status") != "completed",
                })
                if result.get("status") == "completed":
                    # 聚合层证据回查（与 evidence_integrity_validation 同函数）
                    analysis = adapt_verify_result(result)
                    contexts = evidence_contexts_for(analysis)
                    evidence_check = validate_ai_evidence_references(
                        {**candidate, "ai_analysis": analysis}, contexts
                    )
                    record["evidence_check"] = evidence_check
                    record["evidence_contexts"] = len(contexts)
            except Exception as exc:  # noqa: BLE001——探针逐候选隔离异常
                LOGGER.exception("候选核验探针异常")
                record.update({"status": "probe_error", "error": str(exc)[:300]})
            per_candidate.append(record)
        elapsed = time.monotonic() - started
    finally:
        reader.close()
        if hasattr(analyzer, "aclose"):
            await analyzer.aclose()

    completed = sum(1 for r in per_candidate if r.get("status") == "completed")
    fallback = sum(1 for r in per_candidate if r.get("fallback_semantics"))
    classifications = Counter(
        r.get("classification") for r in ai_rounds if r.get("classification")
    )
    attributions = Counter(r.get("attribution") for r in ai_rounds)
    first_messages = [
        r.get("message") for r in ai_rounds if r.get("classification") in _SCHEMA_CLASSIFICATIONS
    ][:2]
    attribution_clear = bool(classifications) or completed == len(per_candidate)
    passed = completed >= 1 and attribution_clear

    summary = {
        **plan,
        "elapsed_seconds": round(elapsed, 1),
        "ai_rounds": ai_rounds,
        "per_candidate": per_candidate,
        "counts": {
            "completed": completed,
            "fallback": fallback,
            "probe_error": sum(1 for r in per_candidate if r.get("status") == "probe_error"),
        },
        "classification_distribution": dict(classifications),
        "attribution_distribution": dict(attributions),
        "schema_message_samples": first_messages,
        "verdict": "PASS" if passed else "FAIL",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(
        f"[probe-verify] {summary['verdict']}: candidates={len(selected)} "
        f"completed={completed} fallback={fallback} "
        f"classifications={dict(classifications)} elapsed={elapsed:.0f}s",
        file=sys.stderr,
    )
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核验轨定向验证 harness（复用真实 run 产物）")
    parser.add_argument("--run-id", required=True, help="runs/ 下的 run 目录名")
    parser.add_argument("--candidates", default=None, help="逗号分隔候选 ID 子集（可选）")
    parser.add_argument("--max-candidates", type=int, default=5, help="候选上限（默认 5）")
    parser.add_argument("--dry-run", action="store_true", help="只构造取样计划，不调 AI")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if not os.environ.get("AI_APK_TRACER_OPENAI_API_KEY", ""):
        print(
            "[probe-verify] 警告: 未检测到 AI API key（AI_APK_TRACER_OPENAI_API_KEY），"
            "非 dry-run 模式将失败",
            file=sys.stderr,
        )
    return asyncio.run(_run_probe(args))


if __name__ == "__main__":
    raise SystemExit(main())
