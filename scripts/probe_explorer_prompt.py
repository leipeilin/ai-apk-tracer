#!/usr/bin/env python
"""explorer prompt 真实 AI 冒烟探针（EXPLORER-PROMPT-FIX，验收 A-6/A-7）。

走正式 registry 路径的 OpenAICompatibleAnalyzer.explore_entry()（禁止绕过
registry 的裸 HTTP）；入口数据取自既有扫描 run 的 api_entry_table.json。
默认同一入口连续 3 次调用；--entries 支持按异构 kind 选择 2-3 个入口。

退出码：0 = 全部通过（status=completed 且 ExplorerObservation 解析通过）；
非 0 = 任一失败（含缺 key——不发网络调用）。

用法：
  backend/.venv/bin/python scripts/probe_explorer_prompt.py            # 默认 3 次同入口
  backend/.venv/bin/python scripts/probe_explorer_prompt.py --entries 3 --heterogeneous
  backend/.venv/bin/python scripts/probe_explorer_prompt.py --run-dir <run_dir>

密钥：脚本自行 load_dotenv() 后经 os.environ 校验 AI_APK_TRACER_OPENAI_API_KEY
（get_settings() 的 dotenv 来源不注入 os.environ，而 ai.py 经 os.environ 读 key
——评审 R-7）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:  # pragma: no cover - python-dotenv 为传递依赖，缺失时退化为环境变量
    pass

from app.analysis.ai_models import ExplorerInput
from app.config import get_settings

DEFAULT_RUN_DIR = (
    Path(__file__).resolve().parents[1] / ".ai-apk-tracer" / "runs"
    / "20260822T124055Z_2a80fc5a8735_34aedd85"
)


def _load_entries(run_dir: Path) -> list[dict]:
    """经 CallTreeService.get_entry_points() 取入口（与探索驱动同源——磁盘
    api_entry_table.json 的 entry_method 由该层解析 lifecycle 后增强为
    method_id；直接读磁盘会得到 method_id=null 的原始形状）。"""

    index_path = run_dir / "index" / "code-index.json"
    if not index_path.is_file():
        raise SystemExit(f"run 索引不存在：{index_path}")
    try:
        code_index = json.loads(index_path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"run 索引读取失败：{exc}") from exc

    from app.analysis.call_tree import CallTreeService
    from app.analysis.index_store import SQLiteCodeIndexReader
    from app.config import CallTreeSettings

    reader = SQLiteCodeIndexReader(code_index)
    try:
        call_tree = CallTreeService(run_dir, reader, CallTreeSettings())
        entries = list(call_tree.get_entry_points())
    finally:
        reader.close()
    entries = [e for e in entries if isinstance(e, dict) and e.get("method_id")]
    if not entries:
        raise SystemExit("入口解析后没有含 method_id 的入口（无法构造 ExplorerInput）")
    return entries


def _pick_entries(entries: list[dict], count: int, heterogeneous: bool) -> list[dict]:
    if not heterogeneous:
        return [entries[0]] * count
    # 异构：按 kind 去重取前 count 个不同类型入口
    picked: list[dict] = []
    seen_kinds: set[str] = set()
    for entry in entries:
        kind = str(entry.get("kind") or "other")
        if kind in seen_kinds:
            continue
        seen_kinds.add(kind)
        picked.append(entry)
        if len(picked) >= count:
            return picked
    # 不足 count 个 kind 时用首个入口补齐
    while len(picked) < count:
        picked.append(entries[0])
    return picked


async def _probe_once(analyzer, entry: dict) -> dict:
    model_input = ExplorerInput.model_validate({
        "round_index": 1,
        "rounds_budget": 1,
        "requests_budget": 0,
        "entry_json": json.dumps(entry, ensure_ascii=False),
        "attack_surface_json": None,
        "prior_observations": None,
        "code_context": None,
    })
    return await analyzer.explore_entry(model_input)


async def _probe_all(analyzer, entries: list[dict]) -> list[dict | BaseException]:
    """单事件循环内跑全部调用（httpx AsyncClient 绑定首个 loop——跨
    asyncio.run 复用会 Event loop is closed；探针勘误 2026-08-23）。"""

    results: list[dict | BaseException] = []
    for entry in entries:
        try:
            results.append(await _probe_once(analyzer, entry))
        except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
            # 网络层/传输层意外异常——如实记录，不静默
            results.append(exc)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="explorer prompt 真实 AI 冒烟探针")
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR,
                        help="既有 run 目录（含 api-surface/api_entry_table.json）")
    parser.add_argument("--entries", type=int, default=3,
                        help="调用次数（同入口）或异构入口数（配合 --heterogeneous）")
    parser.add_argument("--heterogeneous", action="store_true",
                        help="按异构 kind 选择入口（activity/provider/receiver 等）")
    args = parser.parse_args()

    # 评审 R-7：显式校验 key（缺失时不发网络调用直接报错）
    settings = get_settings()
    key_env = settings.ai.api_key_env
    if not os.environ.get(key_env):
        print(f"错误：缺少 API key 环境变量 {key_env}（请在 .env 或 shell 中配置）", file=sys.stderr)
        return 2

    entries = _load_entries(args.run_dir)
    targets = _pick_entries(entries, args.entries, args.heterogeneous)
    print(f"[probe] 入口源：{args.run_dir}")
    print(f"[probe] 计划调用 {len(targets)} 次"
          + ("（异构 kind：" + ", ".join(str(e.get('kind')) for e in targets) + "）"
             if args.heterogeneous else f"（同一入口 {targets[0].get('entry_id')}）"))

    from app.analysis.ai_runtime import AIRuntime

    analyzer = AIRuntime(settings.ai).create_analyzer(
        cache_dir=Path(tempfile.mkdtemp(prefix="explorer-probe-cache-")),
        max_output_tokens=settings.context_budget.max_output_tokens,
        budget_policy=settings.context_budget.model_dump(mode="json"),
    )

    failures = 0
    results = asyncio.run(_probe_all(analyzer, targets))
    for index, (entry, result) in enumerate(zip(targets, results), start=1):
        label = str(entry.get("entry_id") or entry.get("method_id"))
        if isinstance(result, BaseException):
            print(f"[{index}/{len(targets)}] {label} → EXCEPTION: {result}")
            failures += 1
            continue
        status = result.get("status")
        metadata = result.get("metadata") or {}
        analysis = result.get("analysis")
        proposals = 0
        parsed = False
        if isinstance(analysis, dict):
            proposals = len(analysis.get("chain_proposals") or [])
            parsed = bool(analysis.get("component_summary")) and bool(analysis.get("loop"))
        classification = result.get("classification") or metadata.get("classification") or (
            "parsed" if parsed else "unparsed")
        ok = status == "completed" and parsed
        print(f"[{index}/{len(targets)}] {label} → status={status} "
              f"classification={classification} chain_proposals={proposals} "
              f"model={metadata.get('model') or 'unknown'}"
              + ("" if ok else "  ✗"))
        if not ok:
            failures += 1
            errors = metadata.get("initial_validation_errors")
            if errors:
                print(f"        validation_errors={errors}")

    if failures:
        print(f"[probe] 失败 {failures}/{len(targets)} 次——冒烟未通过（不得以多数通过放行）")
        return 1
    print(f"[probe] {len(targets)}/{len(targets)} 全部通过（status=completed 且 ExplorerObservation 解析通过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
