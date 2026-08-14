#!/usr/bin/env python3
"""存量回溯自检：比对 finding.sinks 与其 slice 的 candidate.sinks 一致性。

v2026-08-14（CONTEXT_SLICE_MISMATCH 防线，形态 B）：扫描期自检（orchestrator
内嵌）只覆盖新 run；本脚本对**已完成** run 批量回溯，暴露历史污染面。

用法：
  python scripts/check-finding-slice-consistency.py <run_id> [--fix] [--export out.csv]
  python scripts/check-finding-slice-consistency.py all [--fix] [--export out.csv]

默认只读；--fix 给 mismatch finding 的 blocking_gaps 补标记 + manifest 记计数
（幂等：已有标记不重复追加）。复用 context_builder.finding_slice_sink_mismatch 纯函数。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = WORKSPACE_ROOT / "backend"
RUNS_ROOT = WORKSPACE_ROOT / ".ai-apk-tracer" / "runs"
sys.path.insert(0, str(BACKEND_ROOT))

from app.analysis.context_builder import finding_slice_sink_mismatch  # noqa: E402

MISMATCH_CODE = "FINDING_SLICE_SINK_MISMATCH"


def scan_run(run_dir: Path) -> dict[str, Any]:
    """只读扫描一个 run 的所有 finding，返回统计与明细。"""
    findings_dir = run_dir / "findings"
    evidence_dir = run_dir / "reports" / "evidence"
    run_id = run_dir.name
    stats = {"run_id": run_id, "findings": 0, "mismatch": 0, "slice_missing": 0, "consistent": 0}
    details: list[dict[str, Any]] = []
    if not findings_dir.exists():
        return {**stats, "details": details, "run_dir": run_dir}
    for fp in sorted(findings_dir.glob("*.json")):
        fid = fp.stem
        evfp = evidence_dir / f"{fid}.json"
        if not evfp.exists():
            # 无 evidence 文件：不计入（旧 run 可能没有该产物）
            continue
        try:
            finding = json.loads(fp.read_text("utf-8"))
            evidence = json.loads(evfp.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        stats["findings"] += 1
        context_slice = evidence.get("context_slice")
        issues = finding_slice_sink_mismatch(finding, context_slice)
        if not issues:
            stats["consistent"] += 1
            continue
        if issues[0]["code"] == "SLICE_UNAVAILABLE":
            # v2026-08-14 修正：L1 或 rule_only 的 finding 天然无 slice（_should_build_slice
            # 对 L1 且 ai_eligible≠True 返回 False），这是设计预期不是异常。
            # 只有"本该进 AI/slice 的 finding"（L2 且 analysis_status != rule_only）
            # 缺 slice 才算 slice_missing。
            evidence_level = finding.get("evidence_level")
            analysis_status = finding.get("analysis_status")
            if not (evidence_level == "L2" and analysis_status != "rule_only"):
                stats["consistent"] += 1  # 无 slice 属正常，视为一致
                continue
            stats["slice_missing"] += 1
            details.append({
                "finding_id": fid, "category": "SLICE_UNAVAILABLE",
                "finding_sinks": _sink_lines(finding.get("sinks", [])),
                "slice_sinks": "", "slice_id": "",
            })
        else:
            stats["mismatch"] += 1
            issue = issues[0]
            details.append({
                "finding_id": fid, "category": MISMATCH_CODE,
                "finding_sinks": _sink_lines(finding.get("sinks", [])),
                "slice_sinks": _sink_lines((context_slice.get("candidate") or {}).get("sinks", [])),
                "slice_id": context_slice.get("slice_id", ""),
            })
    return {**stats, "details": details, "run_dir": run_dir}


def _sink_lines(sinks: list[Any]) -> str:
    return ";".join(
        f"{s.get('path', '').split('/')[-1]}:{s.get('line')}"
        for s in sinks if isinstance(s, dict)
    )


def apply_fix(run_dir: Path, result: dict[str, Any]) -> int:
    """给 mismatch finding 的 blocking_gaps 补标记 + manifest 计数（幂等）。"""
    evidence_dir = run_dir / "reports" / "evidence"
    manifest_path = run_dir / "manifest.json"
    fixed = 0
    for detail in result["details"]:
        if detail["category"] != MISMATCH_CODE:
            continue
        fid = detail["finding_id"]
        fp = run_dir / "findings" / f"{fid}.json"
        if not fp.exists():
            continue
        try:
            finding = json.loads(fp.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        existing = {gap.get("code") for gap in finding.get("blocking_gaps", []) if isinstance(gap, dict)}
        if MISMATCH_CODE in existing:
            continue
        finding.setdefault("blocking_gaps", []).append({
            "code": MISMATCH_CODE,
            "critical": True,
            "finding_sinks": detail["finding_sinks"],
            "slice_sinks": detail["slice_sinks"],
            "slice_id": detail["slice_id"],
        })
        fp.write_text(json.dumps(finding, ensure_ascii=False, indent=2), "utf-8")
        # 同步 evidence 里的 finding 副本
        evfp = evidence_dir / f"{fid}.json"
        if evfp.exists():
            try:
                evidence = json.loads(evfp.read_text("utf-8"))
                evidence["finding"] = finding
                evfp.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), "utf-8")
            except (json.JSONDecodeError, OSError):
                pass
        fixed += 1
    if manifest_path.exists() and result["mismatch"]:
        try:
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["finding_slice_mismatches"] = result["mismatch"]
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
        except (json.JSONDecodeError, OSError):
            pass
    # v2026-08-14：磁盘 JSON 已补标记后，同步 SQLite（repository.replace_findings
    # 只替换机器可重算字段，保留 review_status/reason 等人工状态）。条件是
    # result["mismatch"]>0 而非 fixed>0——幂等重跑时磁盘标记已存在（fixed=0），
    # 但数据库可能尚未同步，仍需执行 upsert（replace_findings 本身幂等）。
    if result["mismatch"]:
        try:
            from app.config import Settings
            from app.shared.repository import SQLiteRepository

            settings = Settings()
            repo = SQLiteRepository(settings.resolved_database_path())
            repo.initialize()
            findings = [
                json.loads(fp.read_text("utf-8"))
                for fp in sorted((run_dir / "findings").glob("*.json"))
            ]
            repo.replace_findings(run_dir.name, findings)
        except Exception as exc:  # 数据库同步失败不阻塞磁盘修复
            print(f"  ⚠ 数据库同步失败（磁盘已修复）: {type(exc).__name__}: {str(exc)[:80]}")
    return fixed


def main() -> int:
    parser = argparse.ArgumentParser(description="finding.sinks ↔ slice 一致性存量自检")
    parser.add_argument("run_id", help="目标任务 ID，或 all 扫描全部 run")
    parser.add_argument("--fix", action="store_true", help="给 mismatch finding 补 blocking_gaps 标记 + manifest 计数")
    parser.add_argument("--export", metavar="CSV", help="导出明细 CSV")
    args = parser.parse_args()

    if args.run_id == "all":
        run_dirs = sorted(RUNS_ROOT.glob("*/")) if RUNS_ROOT.exists() else []
    else:
        run_dir = RUNS_ROOT / args.run_id
        run_dirs = [run_dir] if run_dir.exists() else []
    if not run_dirs:
        print(f"未找到 run: {args.run_id}")
        return 1

    total_mismatch = total_slice_missing = total_consistent = total_findings = 0
    all_details: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        result = scan_run(run_dir)
        stats = result
        print(f"run {stats['run_id']}")
        print(f"  findings={stats['findings']} | mismatch={stats['mismatch']} "
              f"| slice_missing={stats['slice_missing']} | consistent={stats['consistent']}")
        total_findings += stats["findings"]
        total_mismatch += stats["mismatch"]
        total_slice_missing += stats["slice_missing"]
        total_consistent += stats["consistent"]
        all_details.extend(result["details"])
        if args.fix:
            fixed = apply_fix(run_dir, result)
            if fixed:
                print(f"  --fix: {fixed} 个 finding 已补标记")

    print(f"\n总计: findings={total_findings} | mismatch={total_mismatch} "
          f"| slice_missing={total_slice_missing} | consistent={total_consistent}")

    if args.export and all_details:
        lines = ["finding_id,category,finding_sinks,slice_sinks,slice_id"]
        for d in all_details:
            lines.append(f"{d['finding_id']},{d['category']},\"{d['finding_sinks']}\",\"{d['slice_sinks']}\",{d['slice_id']}")
        Path(args.export).write_text("\n".join(lines) + "\n", "utf-8")
        print(f"明细已导出: {args.export} ({len(all_details)} 行)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
