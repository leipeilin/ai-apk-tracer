#!/usr/bin/env python
"""sink taxonomy 双源同步校验（P1，评审 2026-08-27-ruleset-quality-review.md）。

校验 rules/sink_taxonomy/versions.yaml 的 base 条目与
rules/shared/dataflow.py::classify_operation_taxonomy 的一致性：

1. CONFLICT（error，退出码 1）：某 base 条目的 receiver 证据探针在 dataflow
   命中但 taxonomy 与条目不一致——同一调用两轨分类不同（验收用例：write 条目
   的设备流 receiver 曾归 file_mutation 而 dataflow 归 device_protocol_output）。
2. ORPHAN（warning）：条目的全部 receiver 证据在 dataflow 均不命中——dataflow
   侧无对应分支（dataflow 变更未同步，或条目为宽松偏离仅剩 exact 失配）。
3. PASS_WITH_INFO：仅部分证据命中（如 leaves 宽松偏离是 versions.yaml 头部
   声明的口径——receiver 证据缺失时宽松命中；constructor 条目仅 constructor
   形态命中）。
4. COVERAGE（info）：dataflow 签名分支中的 method 名不在 versions.yaml base
   条目——人工评审后决定是否补录（宽松匹配与"子集"口径由 backend 消费端
   app/analysis/sink_taxonomy.py 声明，非 versions.yaml 头部）。

探针原理：method_descriptor 留空 → _signature_checked_effect 走
OPERATION_SIGNATURE_GAP 路径（is_effect=True 且返回种子 taxonomy），
从而绕过 arity 校验、只验证 receiver family × method 的映射。
manual 条目（source=manual）自 P4 起经 dataflow 的 _manual_sink_table 运行时回退
消费（classify_operation_taxonomy 尾部），探针对其验证 method×receiver×taxonomy
映射（预期命中 kind=custom_sink 且 taxonomy 一致）并校验 verified_arity 结构
（若存在必须为非空 int 列表——解析失败会使条目静默退化为提案形态、闭链能力
无声丢失，P4 核验 R-5）。

探针的结构性不可探测边界（核验 R-5，文档化）：
1. 参数敏感分支：如 ParcelFileDescriptor.open 的只读降级（实参含
   mode_read_only/'r' 且无写模式时 dataflow 归 data_disclosure，
   dataflow.py 的 open 分支）——探针 arguments=[] 恒走默认路径，versions.yaml
   的 open 条目以 file_mutation 为准（消费端仅做命中判定、taxonomy 值不
   外泄，见 backend/app/analysis/explorer_validation.py）。
2. receiver 级反向缺口：dataflow 为已有方法名新增 receiver/family 时，
   CONFLICT（yaml 未声明该 receiver 即无探针）与 COVERAGE（仅方法名粒度）
   均无信号；中期应把 COVERAGE 升级为（方法×receiver）粒度。
3. same_package_leaf 路径（dataflow 的 same_package_leaf helper，依赖
   containing_class）在探针下恒 False，无法覆盖（manual 回退同款限制）。

CI 接入：backend/tests/test_sink_taxonomy.py::test_versions_yaml_synced_with_dataflow
以 subprocess 调用本脚本并断言退出码 0。

用法：
  backend/.venv/bin/python scripts/check_sink_taxonomy_sync.py [--yaml <path>]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rules"))

from shared.dataflow import classify_operation_taxonomy  # noqa: E402

DEFAULT_YAML = ROOT / "rules" / "sink_taxonomy" / "versions.yaml"
DATAFLOW_PY = ROOT / "rules" / "shared" / "dataflow.py"


def _probe(receiver_type: str, method: str, expression_kind: str) -> dict:
    return {
        "method_name": method,
        "receiver_type": receiver_type,
        "receiver_text": "probe",
        "resolved_target_id": "",
        "resolved_target": "",
        "method_descriptor": "",
        "expression_kind": expression_kind,
        "arguments": [],
    }


def _classify(receiver_type: str, method: str) -> dict | None:
    """两种 expression_kind 探针中任一 is_effect 即返回其结果。"""

    for kind in ("call", "constructor"):
        result = classify_operation_taxonomy(
            _probe(receiver_type, method, kind),
            containing_method_name="__sync_probe__",
            containing_class="",
        )
        if result.get("is_effect"):
            return result
    return None


def _evidence_variants(entry: dict) -> list[tuple[str, str]]:
    """展开条目的 receiver 证据为 (label, receiver_type) 探针序列。

    - leaves：直接用 leaf 名（dataflow 的 leaf 匹配仅接受裸简单名）。
    - prefixes：prefix + "Probe" 合成 FQCN。
    - exact：原文 + `$`→`.` 归一变体（dataflow 的 Settings family exact 用
      点分隔，versions.yaml 沿用 $ 形态——两形态任一命中即可）。
    """

    variants: list[tuple[str, str]] = []
    for leaf in entry.get("receiver_leaves") or []:
        variants.append((f"leaf:{leaf}", str(leaf)))
    for prefix in entry.get("receiver_prefixes") or []:
        variants.append((f"prefix:{prefix}", f"{prefix}Probe"))
    for exact in entry.get("receiver_exact") or []:
        exact = str(exact)
        variants.append((f"exact:{exact}", exact))
        if "$" in exact:
            dot_form = exact.replace("$", ".")
            variants.append((f"exact:{dot_form}", dot_form))
    return variants


def _dataflow_method_names() -> set[str]:
    """粗扫 dataflow.py 中出现于签名分支的 method 名（COVERAGE info 用）。"""

    text = DATAFLOW_PY.read_text("utf-8")
    names: set[str] = set()
    for match in re.finditer(r'"([A-Za-z_$][\w$]*)"\s*:\s*frozenset', text):
        names.add(match.group(1))
    for match in re.finditer(r'method_name\s*==\s*"([A-Za-z_$][\w$]*)"', text):
        names.add(match.group(1))
    for match in re.finditer(r"method_name\s+in\s+\{([^}]*)\}", text):
        for item in re.findall(r'"([A-Za-z_$][\w$]*)"', match.group(1)):
            names.add(item)
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="sink taxonomy 双源同步校验")
    parser.add_argument("--yaml", default=str(DEFAULT_YAML), help="versions.yaml 路径")
    parser.add_argument("--strict", action="store_true", help="ORPHAN 也视为失败（默认仅 CONFLICT 失败）")
    args = parser.parse_args()

    document = yaml.safe_load(Path(args.yaml).read_text("utf-8"))
    entries = document.get("entries") or []
    base_entries = [e for e in entries if e.get("source") == "base"]
    manual_entries = [e for e in entries if e.get("source") == "manual"]

    conflicts: list[str] = []
    orphans: list[str] = []
    infos: list[str] = []
    passed = 0

    # manual 条目（P4 核验 R-5/R-8）：verified_arity 结构校验 + 探针验证回退命中
    for entry in manual_entries:
        method = str(entry.get("method") or "")
        taxonomy = str(entry.get("taxonomy") or "")
        raw_arities = entry.get("verified_arity")
        if raw_arities is not None:
            if not isinstance(raw_arities, list) or not raw_arities or not all(
                isinstance(value, int) and not isinstance(value, bool) for value in raw_arities
            ):
                conflicts.append(
                    f"{method}({taxonomy}): manual verified_arity 结构非法（须为非空 int 列表，"
                    f"实际 {raw_arities!r}——解析失败会静默退化为提案形态）"
                )
                continue
        variants = _evidence_variants(entry)
        if not variants:
            orphans.append(f"{method}: manual 条目无任何 receiver 证据（消费端任意 receiver 命中，N-6）")
            continue
        hits, mismatches = [], []
        for label, receiver_type in variants:
            result = _classify(receiver_type, method)
            if result is None:
                continue
            if str(result.get("taxonomy")) != taxonomy:
                mismatches.append(f"{label}→dataflow:{result.get('taxonomy')}")
            else:
                hits.append(label)
        head = f"manual {method}({taxonomy})"
        if mismatches:
            conflicts.append(f"{head}: 回退冲突 {'; '.join(mismatches)}")
        elif hits:
            passed += 1
        else:
            infos.append(f"{head}: 探针未命中回退（receiver 证据可能依赖 same_package/exact 形态——人工复核）")

    for entry in base_entries:
        method = str(entry.get("method") or "")
        taxonomy = str(entry.get("taxonomy") or "")
        variants = _evidence_variants(entry)
        if not variants:
            orphans.append(f"{method}: 无 receiver 证据（leaves/prefixes/exact 全缺）")
            continue

        hits: list[str] = []
        misses: list[str] = []
        mismatches: list[str] = []
        for label, receiver_type in variants:
            result = _classify(receiver_type, method)
            if result is None:
                misses.append(label)
            elif str(result.get("taxonomy")) != taxonomy:
                mismatches.append(f"{label}→dataflow:{result.get('taxonomy')}")
            else:
                hits.append(label)

        head = f"{method}({taxonomy})"
        if mismatches:
            conflicts.append(f"{head}: 冲突 {'; '.join(mismatches)}（命中证据 {len(hits)}）")
        elif hits:
            passed += 1
            if misses:
                infos.append(f"{head}: 宽松证据未命中 dataflow（{', '.join(misses)}）——backend 消费端（app/analysis/sink_taxonomy.py）声明的宽松匹配口径或 constructor 形态限定")
        else:
            orphans.append(f"{head}: 全部证据（{', '.join(misses)}）在 dataflow 无分支")

    yaml_methods = {str(e.get("method")) for e in base_entries}
    coverage = sorted(_dataflow_method_names() - yaml_methods)

    print(
        f"[sync] base 条目 {len(base_entries)} 条：PASS {passed}，CONFLICT {len(conflicts)}，"
        f"ORPHAN {len(orphans)}；manual 条目 {len(manual_entries)} 条（含回退探针）"
    )
    for line in conflicts:
        print(f"  [CONFLICT] {line}")
    for line in orphans:
        print(f"  [ORPHAN]   {line}")
    for line in infos:
        print(f"  [info]     {line}")
    if coverage:
        print(f"[coverage] dataflow 有而 base 条目无的 method（{len(coverage)}，人工评审补录候选）:")
        print(f"  {', '.join(coverage)}")

    if conflicts:
        return 1
    if args.strict and orphans:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
