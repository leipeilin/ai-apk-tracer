#!/usr/bin/env python3
"""生成 run 确定性产物基线清单（M0 补记承诺 / 实施计划 §4.1 通用门禁对照工具）。

用法：
    python3 scripts/baseline-manifest.py <run_id> <output.json>

纳入（确定性产物，字节级 diff 适用）：
- decompile/sources/** 与 decompile/resources/**（反编译输出）
- index/code-index.json 与 index/analysis.sqlite3（代码索引）
- rule-results/**（规则候选 JSON）
- slices/**（候选切片）
- manifest.json 的确定性字段子集（剔除时间戳 / trace_id / run_id / stages 等波动字段）

排除（非确定或临时）：
- ai-cache/、ai-trace/、findings/、reports/（含 AI 决策字段与模型输出，非字节级可复现；
  以 findings_count 作为数量基线）
- logs/、tmp/、rule-work/、input/（日志 / 临时 / 上传副本）

M1 各任务验收对照：对新 run 生成清单后 diff 两份 JSON；文件集合与哈希一致即
"默认配置产物 diff 为空"（口径详见 docs/analysis/milestones/2026-08-22-m1-baseline-runs.md）。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = WORKSPACE_ROOT / ".ai-apk-tracer" / "runs"

# 纳入的确定性产物目录 / 文件（相对 run 目录）
INCLUDED_DIRS = ("decompile/sources", "decompile/resources", "rule-results", "slices")
INCLUDED_FILES = ("index/code-index.json", "index/analysis.sqlite3")

# manifest.json 纳入的确定性字段（剔除波动字段：created_at/completed_at/updated_at/
# trace_id/run_id/stages/analysis_incomplete/cleanup_history）
MANIFEST_STABLE_FIELDS = (
    "apk",
    "artifact_schema_versions",
    "config",
    "coverage_gaps",
    "engine",
    "finding_slice_mismatches",
    "pipeline_version",
    "schema_version",
    "stage",
    "status",
)

CHUNK_SIZE = 1 << 20


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while chunk := fp.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_manifest(run_dir: Path) -> str:
    """manifest.json 剔除波动字段后的规范化序列化哈希。"""
    manifest = json.loads((run_dir / "manifest.json").read_text("utf-8"))
    stable = {key: manifest[key] for key in MANIFEST_STABLE_FIELDS if key in manifest}
    canonical = json.dumps(stable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_baseline(run_id: str) -> dict:
    run_dir = RUNS_DIR / run_id
    if not run_dir.is_dir():
        raise SystemExit(f"run 目录不存在: {run_dir}")

    files: dict[str, str] = {}
    for rel_dir in INCLUDED_DIRS:
        base = run_dir / rel_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                files[f"{rel_dir}/{path.relative_to(base).as_posix()}"] = _sha256_file(path)
    for rel_file in INCLUDED_FILES:
        path = run_dir / rel_file
        if path.is_file():
            files[rel_file] = _sha256_file(path)
    files["manifest.json#stable"] = _stable_manifest(run_dir)

    findings_dir = run_dir / "findings"
    findings_count = len(list(findings_dir.glob("*.json"))) if findings_dir.is_dir() else 0

    aggregate_input = "\n".join(f"{name}:{digest}" for name, digest in sorted(files.items()))
    aggregate = hashlib.sha256(aggregate_input.encode("utf-8")).hexdigest()
    return {
        "run_id": run_id,
        "tool": "scripts/baseline-manifest.py",
        "included": {
            "dirs": list(INCLUDED_DIRS),
            "files": list(INCLUDED_FILES) + ["manifest.json#stable"],
        },
        "summary": {
            "file_count": len(files),
            "aggregate_sha256": aggregate,
            "findings_count": findings_count,
        },
        "files": files,
    }


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    run_id, output = sys.argv[1], sys.argv[2]
    baseline = build_baseline(run_id)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), "utf-8")
    summary = baseline["summary"]
    print(f"run={run_id} files={summary['file_count']} aggregate={summary['aggregate_sha256'][:16]}… findings={summary['findings_count']}")
    print(f"baseline -> {output_path}")


if __name__ == "__main__":
    main()
