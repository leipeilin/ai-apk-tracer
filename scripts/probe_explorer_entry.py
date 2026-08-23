#!/usr/bin/env python
"""探索轨定向验证 harness（M2 收尾-1，指引 §4.1）。

复用已跑完的真实 run 产物（api_entry_table / code index / manifest facts），
不重新反编译、不重建索引、不重跑规则——对入口子集调用与
ScanOrchestrator._run_explorer_stage 同构的正式探索链路：

    CallTreeService(run_dir, SQLiteCodeIndexReader, explorer.call_tree)
      → ExplorerOrchestrator（轮循环 / read_requests 分发 / code_context 注入）
      → OpenAICompatibleAnalyzer.explore_entry（正式 registry prompt 路径）
      → validate_explorer_candidates（三档校验 + custom sink taxonomy）

产出（分钟级，替代整包全量 40-60 分钟）：
- 每入口每轮 status / read_requests / chain_proposals / code_context 状态；
- 三档计数（validated / partially_validated / unverified）；
- D-3 行为级机器断言：code_context 为 null 的轮次模型输出的
  chain_proposals 必须为空（prompts/explorer/1.0.0/system.md 硬约束 10
  "禁止无据产链"），违规即整体 FAIL；
- 门槛判定：validated + partially_validated ≥ 5 且零 D-3 违规 → PASS
  （入口数 < 10 时按比例折算报告）。

与 orchestrator 的已知差异（不影响行为口径）：
- 探索输出写独立 probe_dir（时间戳子目录——observations 追加语义，
  不污染真实 run 的 explorer/ 产物）；
- 不执行 deep_dive（不改链不升档，对三档计数无影响——T2.8 语义）；
- 无 run 级请求预算锁（探针入口数即天然预算）。

用法：
  backend/.venv/bin/python scripts/probe_explorer_entry.py \\
      --run-id 20260822T210017Z_1c55d3fb9f95_dc24a077 --max-entries 6
  backend/.venv/bin/python scripts/probe_explorer_entry.py --run-id <id> --dry-run
  backend/.venv/bin/python scripts/probe_explorer_entry.py --run-id <id> \\
      --entries <entry_id_1>,<entry_id_2>

输出：结构化 JSON（stdout）+ 人读摘要（stderr）。
退出码：0 = dry-run 或 PASS；1 = FAIL；2 = 输入/环境错误。

设计：docs/analysis/2026-08-23-m2m3-forward-guidance.md §4.1/§5/§9。
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

# 坑 1（子 agent 调研）：.env 存在但 get_settings 的 dotenv 不注入 os.environ，
# 而 ai.py 经 os.environ 读 key —— 脚本必须显式加载
try:
    from dotenv import load_dotenv

    load_dotenv(WORKSPACE_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from app.analysis.ai_models import ExplorerInput
from app.analysis.call_tree import CallTreeService
from app.analysis.explorer import ExplorerOrchestrator
from app.analysis.explorer_validation import validate_explorer_candidates
from app.analysis.index_store import SQLiteCodeIndexReader
from app.analysis.sink_taxonomy import load_sink_taxonomy
from app.config import get_settings

LOGGER = logging.getLogger("probe_explorer_entry")

# 异构取样：每 kind 最多取数（activity/service/receiver/provider 各 2-3，指引 §4.1）
_PER_KIND_SAMPLE = 2
_KIND_ORDER = ("activity", "service", "receiver", "provider")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"[probe-explorer] 无法读取 {path}: {exc}") from exc


def _select_entries(entries: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    """入口取样：--entries 指定优先；否则异构取样（各 kind 均衡）。"""

    if args.entries:
        wanted = {e.strip() for e in args.entries.split(",") if e.strip()}
        selected = [e for e in entries if e.get("entry_id") in wanted]
        missing = wanted - {e.get("entry_id") for e in selected}
        if missing:
            raise SystemExit(f"[probe-explorer] 指定入口不存在: {sorted(missing)}")
        return selected[: args.max_entries]

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_kind.setdefault(str(entry.get("kind") or "other"), []).append(entry)
    selected: list[dict[str, Any]] = []
    for kind in _KIND_ORDER:
        selected.extend(by_kind.get(kind, [])[:_PER_KIND_SAMPLE])
    for kind_bucket in by_kind.values():
        for entry in kind_bucket:
            if len(selected) >= args.max_entries:
                break
            if entry not in selected:
                selected.append(entry)
    return selected[: args.max_entries]


def _classify_d3_violation(context_is_none: bool, status: Any, chain_count: int) -> bool:
    """D-3 断言（纯函数可测）：无 code_context 且模型完成但产链即违规。"""

    return bool(context_is_none and status == "completed" and chain_count > 0)


async def _run_probe(args: argparse.Namespace) -> int:
    settings = get_settings()
    run_dir = RUNS_ROOT / args.run_id
    if not run_dir.is_dir():
        print(f"[probe-explorer] run 目录不存在: {run_dir}", file=sys.stderr)
        return 2

    # ---- 与 orchestrator._run_explorer_stage 同构的装配（读者从 code-index 恢复）
    code_index = _load_json(run_dir / "index" / "code-index.json")
    database_path = str((code_index or {}).get("database_path") or "")
    if not database_path or not Path(database_path).is_file():
        print(f"[probe-explorer] code index 不可用: {database_path!r}", file=sys.stderr)
        return 2
    index_manifest = _load_json(run_dir / "index" / "manifest.json")
    manifest_facts = {
        "debuggable": index_manifest.get("debuggable"),
        "target_sdk": index_manifest.get("target_sdk"),
    }
    taxonomy_path = WORKSPACE_ROOT / "rules" / "sink_taxonomy" / "versions.yaml"
    taxonomy_entries = load_sink_taxonomy(taxonomy_path)

    reader = SQLiteCodeIndexReader(code_index)
    try:
        call_tree = CallTreeService(run_dir, reader, settings.explorer.call_tree)
        entries = [e for e in call_tree.get_entry_points() if e.get("method_id")]
        if not entries:
            print("[probe-explorer] 无有效入口（method_id 全空）", file=sys.stderr)
            return 2
        selected = _select_entries(entries, args)
        kind_dist = Counter(str(e.get("kind") or "other") for e in selected)

        probe_dir = run_dir / "probe-explorer" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        plan = {
            "run_id": args.run_id,
            "run_dir": str(run_dir),
            "probe_dir": str(probe_dir),
            "selected_entries": [
                {"entry_id": e.get("entry_id"), "kind": e.get("kind"),
                 "method_id": e.get("method_id")}
                for e in selected
            ],
            "kind_distribution": dict(kind_dist),
            "manifest_facts": manifest_facts,
            "explorer_settings": {
                "max_rounds_per_entry": settings.explorer.max_rounds_per_entry,
                "max_requests_per_entry": settings.explorer.max_requests_per_entry,
                "max_candidates_per_run": settings.explorer.max_candidates_per_run,
                "allow_external_code": settings.explorer.allow_external_code,
            },
            "dry_run": args.dry_run,
        }
        if args.dry_run:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0

        # ---- 正式 AI 调用（registry prompt 路径）+ D-3 断言捕获层
        from app.analysis.ai import OpenAICompatibleAnalyzer

        analyzer = OpenAICompatibleAnalyzer(settings.ai)
        round_probes: list[dict[str, Any]] = []

        async def probed_ai_call(model_input: ExplorerInput) -> dict[str, Any]:
            context_is_none = model_input.code_context is None
            result = await analyzer.explore_entry(model_input)
            analysis = result.get("analysis") if isinstance(result, dict) else None
            chain_count = len((analysis or {}).get("chain_proposals") or [])
            round_probes.append({
                "entry_id": json.loads(model_input.entry_json).get("entry_id"),
                "round_index": model_input.round_index,
                "code_context_is_none": context_is_none,
                "status": result.get("status"),
                "chain_proposals_count": chain_count,
                "d3_violation": _classify_d3_violation(
                    context_is_none, result.get("status"), chain_count),
            })
            return result

        started = time.monotonic()
        orchestrator = ExplorerOrchestrator(
            probed_ai_call, call_tree, settings.explorer, probe_dir
        )
        candidates = await orchestrator.explore_all(selected)
        validation_counts = validate_explorer_candidates(
            candidates,
            reader,
            str(run_dir / "index" / "analysis.sqlite3"),
            manifest_facts,
            taxonomy_entries=taxonomy_entries,
        )
        elapsed = time.monotonic() - started

        # 轮次审计回读（probe_dir 隔离落盘——不污染真实 run 产物）
        observations_path = probe_dir / "explorer" / "observations.json"
        observations = _load_json(observations_path) if observations_path.is_file() else {}
        per_entry = []
        for record in observations.get("entries", []):
            rounds = record.get("rounds") or []
            per_entry.append({
                "entry_id": record.get("entry_id"),
                "terminated_by": record.get("terminated_by"),
                "rounds": len(rounds),
                "rounds_completed": sum(1 for r in rounds if r.get("status") == "completed"),
                "read_requests": sum(len(r.get("requests_executed") or []) for r in rounds),
                "candidate_count": record.get("candidate_count"),
            })

        d3_violations = [p for p in round_probes if p["d3_violation"]]
        validated = int(validation_counts.get("validated") or 0)
        partial = int(validation_counts.get("partially_validated") or 0)
        unverified = int(validation_counts.get("unverified") or 0)
        threshold = 5 if len(selected) >= 10 else max(1, round(len(selected) * 0.5))
        passed = not d3_violations and (validated + partial) >= threshold

        summary = {
            **plan,
            "elapsed_seconds": round(elapsed, 1),
            "ai_requests_used": orchestrator.ai_requests_used,
            "read_requests_used": orchestrator.read_requests_used,
            "round_probes": round_probes,
            "per_entry": per_entry,
            "validation_counts": {
                "validated": validated,
                "partially_validated": partial,
                "unverified": unverified,
            },
            "d3_violations": d3_violations,
            "threshold": {"required_validated_plus_partial": threshold,
                          "entries_probed": len(selected)},
            "verdict": "PASS" if passed else "FAIL",
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(
            f"[probe-explorer] {summary['verdict']}: entries={len(selected)} "
            f"rounds={len(round_probes)} ai={orchestrator.ai_requests_used} "
            f"validated={validated} partial={partial} unverified={unverified} "
            f"d3_violations={len(d3_violations)} elapsed={elapsed:.0f}s",
            file=sys.stderr,
        )
        if hasattr(analyzer, "aclose"):
            await analyzer.aclose()
        return 0 if passed else 1
    finally:
        reader.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="探索轨定向验证 harness（复用真实 run 产物）")
    parser.add_argument("--run-id", required=True, help="runs/ 下的 run 目录名")
    parser.add_argument("--entries", default=None, help="逗号分隔入口 ID 子集（可选）")
    parser.add_argument("--max-entries", type=int, default=8, help="总入口上限（默认 8）")
    parser.add_argument("--dry-run", action="store_true", help="只构造取样计划，不调 AI")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if not os.environ.get("AI_APK_TRACER_OPENAI_API_KEY", ""):
        print(
            "[probe-explorer] 警告: 未检测到 AI API key（AI_APK_TRACER_OPENAI_API_KEY），"
            "非 dry-run 模式将失败",
            file=sys.stderr,
        )
    return asyncio.run(_run_probe(args))


if __name__ == "__main__":
    raise SystemExit(main())
