#!/usr/bin/env python
"""custom sink 升级闭环 CLI（T2.9，方案 §2.5）。

用法 A（从 run 的探索候选确认——推荐）：
  backend/.venv/bin/python scripts/promote_custom_sink.py \
    --run-dir <run_dir> --candidate-id <expl_id> --taxonomy <taxonomy> \
    [--severity high] --operator <name> [--golden-out <dir>]

  流程：定位候选 → 提取 sink 锚点（回查 run 索引 receiver，默认 exact
  FQCN 忠实记录）→ promote（taxonomy 版本化扩展）→ revalidate（升档
  对比报告）→ 可选 golden 用例生成（--golden-out 目录）。

用法 B（直接确认方法名）：
  backend/.venv/bin/python scripts/promote_custom_sink.py \
    --method <name> [--receiver-leaf <leaf> ... | --receiver-prefix <p> ... |
    --receiver-exact <fqcn> ...] --taxonomy <t> --operator <name> \
    [--taxonomy-path <file>]

单人操作约定：并发 promote 的版本冲突在 git 层解决（文件头声明）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analysis.sink_taxonomy import (
    SinkTaxonomyEntry,
    generate_golden_case,
    promote_custom_sink,
    revalidate_run_candidates,
    sink_method_from_method_id,
)

DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "rules" / "sink_taxonomy" / "versions.yaml"


def _resolve_taxonomy_path(args: argparse.Namespace) -> Path:
    return Path(args.taxonomy_path) if args.taxonomy_path else DEFAULT_TAXONOMY_PATH


def _sink_anchor_from_run(run_dir: Path, candidate_id: str) -> dict:
    """从 run 候选提取 sink 锚点：method + receiver（默认 exact——评审 R-6）。"""

    from app.analysis.index_store import SQLiteCodeIndexReader

    candidates_path = run_dir / "explorer" / "candidates.json"
    raw = json.loads(candidates_path.read_text("utf-8"))
    candidate = next(
        (item for item in raw
         if isinstance(item, dict) and str(item.get("candidate_id")) == candidate_id),
        None,
    )
    if candidate is None:
        raise SystemExit(f"候选不存在：{candidate_id}")
    hops = (candidate.get("chain_proposal") or {}).get("hops") or []
    if not hops:
        raise SystemExit("候选无 hops（无法提取 sink 锚点）")
    last_hop = hops[-1]
    if not last_hop.get("to_method_id") or not last_hop.get("from_method_id"):
        raise SystemExit("链尾跳 from/to_method_id 缺失（锚点不可靠）")
    if str(last_hop.get("to_method_id") or "") == str(last_hop.get("from_method_id") or ""):
        # P1 核验（2026-08-27）：链尾自环（from==to）时 to_method_id 是所在方法本身
        # 而非 sink 调用——提取出的方法名与真实 sink 无关（如 saveCallback 候选
        # 提取出 "loading"）。拒绝并要求用法 B 显式指定。
        raise SystemExit(
            "链尾跳为自环（from==to）——sink 锚点不可靠；"
            "请改用 --method 显式指定方法名及 receiver 约束（用法 B）"
        )
    method = sink_method_from_method_id(last_hop.get("to_method_id"))
    if not method:
        raise SystemExit("链尾 to_method_id 畸形（无法提取方法名）")
    index_path = run_dir / "index" / "code-index.json"
    code_index = json.loads(index_path.read_text("utf-8"))
    reader = SQLiteCodeIndexReader(code_index)
    receiver_exact: list[str] = []
    try:
        row = reader.db.execute(
            "SELECT receiver_type FROM call_sites WHERE method_id = ? AND start_line = ? LIMIT 1",
            (last_hop.get("from_method_id"), last_hop.get("call_site_line")),
        ).fetchone()
        if row is not None and row["receiver_type"]:
            from app.analysis.sink_taxonomy import normalize_receiver_type

            normalized = normalize_receiver_type(str(row["receiver_type"]))
            if normalized:
                receiver_exact = [normalized]
    finally:
        reader.close()
    return {"method": method, "receiver_exact": receiver_exact, "candidate": candidate}


def main() -> int:
    parser = argparse.ArgumentParser(description="custom sink 升级闭环（T2.9）")
    parser.add_argument("--taxonomy-path", help="sink taxonomy 文件路径（默认 rules/sink_taxonomy/versions.yaml）")
    parser.add_argument("--method", help="用法 B：直接确认的方法名")
    parser.add_argument("--receiver-leaf", action="append", default=None,
                        help="用法 B：receiver 裸类名约束（可多次）")
    parser.add_argument("--receiver-prefix", action="append", default=None,
                        help="用法 B：receiver 包前缀约束（可多次）")
    parser.add_argument("--receiver-exact", action="append", default=None,
                        help="用法 B：receiver FQCN 约束（可多次）")
    parser.add_argument("--run-dir", help="用法 A：run 目录")
    parser.add_argument("--candidate-id", help="用法 A：探索候选 ID")
    parser.add_argument("--taxonomy", required=True, help="人工标注的 taxonomy 值")
    parser.add_argument("--severity", default=None, help="severity 提示（如 high）")
    parser.add_argument("--operator", required=True, help="确认人标识")
    parser.add_argument("--golden-out", default=None, help="golden 用例输出目录")
    args = parser.parse_args()

    taxonomy_path = _resolve_taxonomy_path(args)
    if args.run_dir and args.candidate_id:
        anchor = _sink_anchor_from_run(Path(args.run_dir), args.candidate_id)
        method = anchor["method"]
        receiver_exact = args.receiver_exact or anchor["receiver_exact"]
        receiver_leaves, receiver_prefixes = args.receiver_leaf, args.receiver_prefix
        if not (receiver_exact or receiver_leaves or receiver_prefixes):
            # P1 核验（2026-08-27）：run 索引反查 receiver 失败时旧行为静默生成
            # 无约束条目（任意 receiver 命中，消费端 N-6 语义）——过宽匹配风险。
            raise SystemExit(
                "sink 锚点无任何 receiver 约束（run 索引反查失败且未显式提供）——"
                "拒绝生成任意 receiver 命中的无约束条目；"
                "请用 --receiver-exact/--receiver-leaf/--receiver-prefix 指定，"
                "或确认 receiver 与 sink 无关后改用 --method（用法 B）"
            )
        provenance = {"run_id": Path(args.run_dir).name, "candidate_id": args.candidate_id}
    elif args.method:
        method = args.method
        receiver_exact = args.receiver_exact
        receiver_leaves, receiver_prefixes = args.receiver_leaf, args.receiver_prefix
        provenance = None
    else:
        parser.error("需要 --run-dir + --candidate-id（用法 A）或 --method（用法 B）")

    result = promote_custom_sink(
        taxonomy_path,
        method=method, taxonomy=args.taxonomy,
        receiver_leaves=receiver_leaves, receiver_prefixes=receiver_prefixes,
        receiver_exact=receiver_exact, severity=args.severity,
        operator=args.operator, provenance=provenance,
    )
    print(f"[promote] status={result['status']} taxonomy_version={result['taxonomy_version']}")
    if result["status"] == "skipped":
        print("[promote] 同约束 manual 条目已存在（幂等跳过）")
        return 0

    if args.run_dir:
        report = revalidate_run_candidates(Path(args.run_dir), taxonomy_path)
        print(f"[revalidate] total={report['total']} changes={len(report['status_changes'])}")
        for change in report["status_changes"]:
            print(
                f"  {change['candidate_id']}: {change['before']} → {change['after']} "
                f"(custom: {change['custom_before']} → {change['custom_after']})"
            )
        if report.get("degraded"):
            print(f"[revalidate] 降级：{report['degraded']}")

    if args.golden_out and args.run_dir:
        entry = SinkTaxonomyEntry(
            method=method, taxonomy=args.taxonomy,
            receiver_leaves=frozenset(receiver_leaves or []),
            receiver_prefixes=tuple(receiver_prefixes or []),
            receiver_exact=frozenset(receiver_exact or []),
            severity=args.severity, source="manual",
            meta={"run_id": Path(args.run_dir).name,
                  "candidate_id": args.candidate_id or "unknown",
                  "taxonomy_version": result["taxonomy_version"]},
        )
        case = generate_golden_case(
            anchor["candidate"], entry,
            case_id=f"explorer-custom-sink-{method.lower()}", operator=args.operator,
        )
        out_dir = Path(args.golden_out)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{case['id']}.json"
        out_path.write_text(json.dumps(case, ensure_ascii=False, indent=2), "utf-8")
        print(f"[golden] 用例已生成：{out_path}（manifest 登记留人工）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
