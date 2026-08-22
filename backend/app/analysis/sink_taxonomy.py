"""sink taxonomy 版本化文件：加载/匹配/升级闭环（T2.9，方案 §2.2/§2.5）。

backend 侧 custom_sink_proposal 判定的独立数据源（零依赖红线——backend
不 import rules 模块；rules/sink_taxonomy/versions.yaml 是唯一共享数据层，
种子自 rules/shared/dataflow.py classify_operation_taxonomy 提炼）。

升级闭环：人工确认（promote_custom_sink）→ taxonomy 版本化扩展 → 候选
重校验（revalidate_run_candidates，副本不落盘）→ golden 用例生成
（generate_golden_case——形状对齐 backend/app/evaluation/golden.py
GoldenCase 模型，manifest 合并留人工）。

设计：docs/analysis/2026-08-22-t2-9-implementation-plan.md（含评审
R-1~R-11 修订：receiver 规范化/leaf 末段提取宽匹配/arity 预留/
GoldenCase 必填字段/reader 重建路径）。
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger(__name__)

# component.kind → golden case category 映射（R-1：GoldenCase 必填字段来源）
_CATEGORY_BY_COMPONENT_KIND = {
    "activity": "activity", "service": "service", "receiver": "broadcast",
    "provider": "provider",
}


@dataclass(frozen=True)
class SinkTaxonomyEntry:
    """版本化条目：method + receiver 三态约束 + taxonomy。

    receiver 匹配语义（评审 R-2）：leaves=末段提取宽匹配（receiver_type
    规范化后按 "." 取末段）；prefixes=包前缀；exact=FQCN 全名。receiver
    证据缺失（None/空）→ 宽松命中（与 rules 空值失配口径的主动偏离——
    索引 receiver 缺失常见，严格失配伤召回）。arities 为结构预留
    （v1 空=不校验，评审 R-4）。
    """

    method: str
    taxonomy: str
    receiver_leaves: frozenset[str] = frozenset()
    receiver_prefixes: tuple[str, ...] = ()
    receiver_exact: frozenset[str] = frozenset()
    arities: tuple[int, ...] | None = None
    source: str = "base"
    severity: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def load_sink_taxonomy(path: Path) -> list[SinkTaxonomyEntry] | None:
    """容错加载：缺失/损坏/结构异常 → None（判定禁用——保守 False 兼容 T2.6）。

    文件合法存在 → 返回条目列表（可为空——空列表=启用判定且零已知 sink，
    所有 sink 未命中全标 custom）；manual 与 base 同名同约束冲突时
    manual 优先（人工覆盖种子）。
    """

    if not path.is_file():
        return None
    try:
        payload = yaml.safe_load(path.read_text("utf-8")) or {}
    except (yaml.YAMLError, OSError):
        LOGGER.warning("sink taxonomy 文件读取失败（判定禁用）", extra={"path": str(path)})
        return None
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        LOGGER.warning("sink taxonomy 结构异常（entries 非列表——判定禁用）")
        return None
    entries: list[SinkTaxonomyEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            continue
        method, taxonomy = raw.get("method"), raw.get("taxonomy")
        if not method or not taxonomy:
            LOGGER.warning("sink taxonomy 条目缺 method/taxonomy（跳过）", extra={"raw": raw})
            continue
        arities_raw = raw.get("arities")
        entries.append(SinkTaxonomyEntry(
            method=str(method),
            taxonomy=str(taxonomy),
            receiver_leaves=frozenset(str(v) for v in raw.get("receiver_leaves") or []),
            receiver_prefixes=tuple(str(v) for v in raw.get("receiver_prefixes") or []),
            receiver_exact=frozenset(str(v) for v in raw.get("receiver_exact") or []),
            arities=tuple(int(v) for v in arities_raw) if isinstance(arities_raw, list) else None,
            source=str(raw.get("source") or "base"),
            severity=str(raw["severity"]) if raw.get("severity") else None,
            meta={k: v for k, v in raw.items()
                  if k not in {"method", "taxonomy", "receiver_leaves", "receiver_prefixes",
                               "receiver_exact", "arities", "source", "severity"}},
        ))
    # manual 优先（同名同约束去重：base 在前 manual 在后，倒序保首见 manual）
    deduped: dict[tuple, SinkTaxonomyEntry] = {}
    for entry in reversed(entries):
        key = (entry.method, tuple(sorted(entry.receiver_leaves)),
               tuple(sorted(entry.receiver_prefixes)), tuple(sorted(entry.receiver_exact)),
               entry.arities)
        deduped.setdefault(key, entry)
    return list(reversed(list(deduped.values())))


def normalize_receiver_type(receiver_type: str | None) -> str | None:
    """receiver 规范化：剥 smali 形态 `Lcom/foo/Bar;`（含 `/`→`.`）与泛型。

    索引存储为普通 FQCN（com.example.C）——smali 剥离为防御路径（评审 R-2）。
    """

    if not receiver_type:
        return None
    text = str(receiver_type).strip()
    if text.startswith("L") and text.endswith(";"):
        text = text[1:-1].replace("/", ".")
    text = text.split("<", 1)[0].strip()
    return text or None


def sink_matches_taxonomy(
    method_name: str | None,
    receiver_type: str | None,
    entries: Sequence[SinkTaxonomyEntry],
) -> SinkTaxonomyEntry | None:
    """命中判定：method 精确匹配 + receiver 三态约束任一满足。

    receiver 证据缺失（None/空/规范化后空）→ 宽松命中（D2：缺失≠失配）。
    """

    if not method_name:
        return None
    normalized = normalize_receiver_type(receiver_type)
    leaf = normalized.rsplit(".", 1)[-1].rsplit("$", 1)[-1] if normalized else None
    for entry in entries:
        if entry.method != method_name:
            continue
        if not normalized:
            return entry  # 无 receiver 证据——宽松命中（条目存在即算）
        if entry.receiver_exact and normalized in entry.receiver_exact:
            return entry
        if entry.receiver_prefixes and any(
            normalized.startswith(prefix) for prefix in entry.receiver_prefixes
        ):
            return entry
        if entry.receiver_leaves and leaf in entry.receiver_leaves:
            return entry
        if not (entry.receiver_exact or entry.receiver_prefixes or entry.receiver_leaves):
            return entry  # 条目无约束——任意 receiver 命中（manual 语义人工负责）
    return None


def sink_method_from_method_id(method_id: str | None) -> str | None:
    """`path#Class.method:line` / `path#method:line` → 方法名（R-10 兜底形态）。"""

    if not method_id or "#" not in method_id:
        return None
    tail = method_id.split("#", 1)[1]
    signature = tail.split(":", 1)[0]
    if "." not in signature:
        return signature or None
    return signature.rsplit(".", 1)[-1] or None


# ---------------------------------------------------------------------------
# 升级闭环（方案 §2.5：人工确认 → 版本化扩展 → 重校验 → golden）
# ---------------------------------------------------------------------------


def _next_taxonomy_version(version: str) -> str:
    """补丁位递增（1.0.0 → 1.0.1）；畸形版本回退 1.0.1。"""

    try:
        parts = [int(part) for part in version.split(".")]
        while len(parts) < 3:
            parts.append(0)
        parts[2] += 1
        return ".".join(str(part) for part in parts[:3])
    except (ValueError, IndexError):
        return "1.0.1"


def _entry_raw(entry: SinkTaxonomyEntry) -> dict[str, Any]:
    raw: dict[str, Any] = {"method": entry.method, "taxonomy": entry.taxonomy}
    if entry.receiver_leaves:
        raw["receiver_leaves"] = sorted(entry.receiver_leaves)
    if entry.receiver_prefixes:
        raw["receiver_prefixes"] = list(entry.receiver_prefixes)
    if entry.receiver_exact:
        raw["receiver_exact"] = sorted(entry.receiver_exact)
    if entry.arities is not None:
        raw["arities"] = list(entry.arities)
    raw["source"] = entry.source
    if entry.severity:
        raw["severity"] = entry.severity
    raw.update(entry.meta)
    return raw


def promote_custom_sink(
    taxonomy_path: Path,
    *,
    method: str,
    taxonomy: str,
    receiver_leaves: list[str] | None = None,
    receiver_prefixes: list[str] | None = None,
    receiver_exact: list[str] | None = None,
    severity: str | None = None,
    operator: str,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """人工确认 → taxonomy 版本化扩展（追加 manual 条目 + 版本递增）。

    幂等：同 (method, taxonomy, receiver 约束) 的 manual 条目已存在 →
    skipped；base 条目同名同约束 → 升级为 manual（source 改写 + 确认元数据）。
    单人操作约定（评审 R-9）：并发 promote 的版本冲突在 git 层解决。
    """

    existing = load_sink_taxonomy(taxonomy_path) or []  # 缺失/损坏 → 从零冷启动（N-1）
    target_key = (
        tuple(sorted(receiver_leaves or [])),
        tuple(receiver_prefixes or []),
        tuple(sorted(receiver_exact or [])),
    )
    for entry in existing:
        if entry.method != method:
            continue
        entry_key = (
            tuple(sorted(entry.receiver_leaves)),
            tuple(entry.receiver_prefixes),
            tuple(sorted(entry.receiver_exact)),
        )
        if entry_key != target_key:
            continue
        if entry.source == "manual":
            return {"status": "skipped", "taxonomy_version": _current_version(taxonomy_path), "entry": None}
        return {
            "status": "upgraded",
            "taxonomy_version": _rewrite_taxonomy(taxonomy_path, existing, entry, taxonomy,
                                                  severity, operator, provenance),
            "entry": method,
        }
    new_entry = SinkTaxonomyEntry(
        method=method, taxonomy=taxonomy,
        receiver_leaves=frozenset(receiver_leaves or []),
        receiver_prefixes=tuple(receiver_prefixes or []),
        receiver_exact=frozenset(receiver_exact or []),
        severity=severity, source="manual",
        meta={
            "confirmed_at": datetime.now(UTC).date().isoformat(),
            "confirmed_by": operator,
            **(dict(provenance) if provenance else {}),
        },
    )
    existing.append(new_entry)
    version = _rewrite_taxonomy(taxonomy_path, existing, new_entry, taxonomy,
                                severity, operator, provenance)
    return {"status": "appended", "taxonomy_version": version, "entry": method}


def _current_version(taxonomy_path: Path) -> str:
    if not taxonomy_path.is_file():
        return "1.0.0"
    try:
        payload = yaml.safe_load(taxonomy_path.read_text("utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return "1.0.0"
    return str(payload.get("taxonomy_version") or "1.0.0")


def _rewrite_taxonomy(
    taxonomy_path: Path,
    entries: Sequence[SinkTaxonomyEntry],
    upgraded: SinkTaxonomyEntry | None,
    taxonomy: str,
    severity: str | None,
    operator: str,
    provenance: Mapping[str, Any] | None,
) -> str:
    """整体重写（版本递增 + 条目写回；upgraded 条目 source 改 manual）。"""

    old_version = _current_version(taxonomy_path)
    new_version = _next_taxonomy_version(old_version)
    raw_entries: list[dict[str, Any]] = []
    for entry in entries:
        if upgraded is not None and entry is upgraded:
            upgraded_meta = dict(entry.meta)
            upgraded_meta.update({
                "confirmed_at": datetime.now(UTC).date().isoformat(),
                "confirmed_by": operator,
                **(dict(provenance) if provenance else {}),
            })
            upgraded_meta.pop("source", None)
            raw_entries.append(_entry_raw(SinkTaxonomyEntry(
                method=entry.method, taxonomy=taxonomy,
                receiver_leaves=entry.receiver_leaves,
                receiver_prefixes=entry.receiver_prefixes,
                receiver_exact=entry.receiver_exact,
                arities=entry.arities, severity=severity or entry.severity,
                source="manual", meta=upgraded_meta,
            )))
        else:
            raw_entries.append(_entry_raw(entry))
    payload = {
        "schema_version": "1.0",
        "taxonomy_version": new_version,
        "description": (
            "sink taxonomy 版本化文件（T2.9）——backend custom_sink_proposal 判定数据源；"
            "种子（base）自 rules/shared/dataflow.py 提炼，人工扩展（manual）经 "
            "scripts/promote_custom_sink.py 追加；单人操作约定，版本冲突 git 层解决。"
        ),
        "entries": raw_entries,
    }
    taxonomy_path.parent.mkdir(parents=True, exist_ok=True)
    taxonomy_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), "utf-8"
    )
    return new_version


def revalidate_run_candidates(run_dir: Path, taxonomy_path: Path) -> dict[str, Any]:
    """候选重校验（副本不落盘——评审 D4/R-5）：新 taxonomy 重跑三档校验。

    reader 从 run_dir/index/code-index.json 重建（缺失→降级报告）；
    manifest_facts 从 run_dir/manifest.json 读（缺失→{}）。
    """

    candidates_path = run_dir / "explorer" / "candidates.json"
    if not candidates_path.is_file():
        return {"total": 0, "status_changes": [], "counts": {}}
    try:
        raw = json.loads(candidates_path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        LOGGER.warning("explorer/candidates.json 读取失败（重校验空转）")
        return {"total": 0, "status_changes": [], "counts": {}}
    if not isinstance(raw, list):
        return {"total": 0, "status_changes": [], "counts": {}}

    index_path = run_dir / "index" / "code-index.json"
    if not index_path.is_file():
        LOGGER.warning("run 索引缺失（重校验降级——无 reader 无法回查）")
        return {"total": len(raw), "status_changes": [],
                "counts": {}, "degraded": "index_missing"}
    try:
        code_index = json.loads(index_path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"total": len(raw), "status_changes": [],
                "counts": {}, "degraded": "index_unreadable"}

    from app.analysis.explorer_validation import validate_explorer_candidates
    from app.analysis.index_store import SQLiteCodeIndexReader

    entries = load_sink_taxonomy(taxonomy_path)
    manifest_facts: dict[str, Any] = {}
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest_facts = {
                "debuggable": (json.loads(manifest_path.read_text("utf-8")) or {}).get("debuggable"),
                "target_sdk": (json.loads(manifest_path.read_text("utf-8")) or {}).get("target_sdk"),
            }
        except (json.JSONDecodeError, OSError):
            manifest_facts = {}

    before_states = {
        str(item.get("candidate_id")): (
            (item.get("validation") or {}).get("status"),
            (item.get("validation") or {}).get("custom_sink_proposal"),
        )
        for item in raw if isinstance(item, dict)
    }
    working = copy.deepcopy(raw)
    reader = SQLiteCodeIndexReader(code_index)
    try:
        counts = validate_explorer_candidates(
            working, reader, str(run_dir / "index" / "analysis.sqlite3"),
            manifest_facts, taxonomy_entries=entries,
        )
    finally:
        reader.close()
    status_changes: list[dict[str, Any]] = []
    for item in working:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id"))
        before_status, before_custom = before_states.get(candidate_id, (None, None))
        validation = item.get("validation") or {}
        after = (validation.get("status"), validation.get("custom_sink_proposal"))
        if (before_status, before_custom) != after:
            status_changes.append({
                "candidate_id": candidate_id,
                "before": before_status, "after": after[0],
                "custom_before": before_custom, "custom_after": after[1],
            })
    return {"total": len(raw), "status_changes": status_changes, "counts": counts}


def generate_golden_case(
    candidate: Mapping[str, Any],
    entry: SinkTaxonomyEntry,
    *,
    case_id: str,
    operator: str,
) -> dict[str, Any]:
    """golden 用例生成（形状对齐 GoldenCase 模型——评审 R-1）。

    label=positive、rule=EXPLORER_AGENT；manifest 合并留人工（D5）。
    """

    component = candidate.get("component") or {}
    proposal = candidate.get("chain_proposal") or {}
    hops = proposal.get("hops") or []
    component_name = str(component.get("name") or "unknown")
    last_hop = hops[-1] if hops else {}
    to_method_id = str(last_hop.get("to_method_id") or "")
    path = to_method_id.split("#", 1)[0] if "#" in to_method_id else "synthetic/explorer.java"
    method = sink_method_from_method_id(to_method_id) or str(proposal.get("sink") or entry.method)
    validation = candidate.get("validation") or {}
    return {
        "id": case_id,
        "category": _CATEGORY_BY_COMPONENT_KIND.get(
            str(component.get("kind") or ""), "explorer"),
        "label": "positive",
        "rule": "EXPLORER_AGENT",
        "component": str(component.get("kind") or "unknown"),
        "entry": str(component.get("entry_method") or "unknown"),
        "operation": (
            f"explorer-promoted custom sink {method}@{component_name} "
            f"(taxonomy={entry.taxonomy}, 人工确认 by {operator})"
        ),
        "expected": {
            "candidate": True,
            "dataflow": "not_proven",
            "auth": "unknown",
            "guard": "unknown",
            "taxonomy": entry.taxonomy,
            "verdict": "report",
        },
        "must_not_report": [],
        "sources": [{
            "path": str((proposal.get("evidence_refs") or [{}])[0].get("path") or path),
            "symbol": str(proposal.get("source") or "explorer source"),
            "kind": "explorer_source",
        }],
        "sinks": [{
            "path": path,
            "symbol": method,
            "kind": "custom_sink",
        }],
        "tags": ["explorer-promotion", entry.taxonomy, "custom-sink", validation.get("status") or "unknown"],
        "provenance": [{
            "kind": "explorer-promotion",
            "reference": (
                f"{entry.meta.get('run_id', 'unknown')}/{entry.meta.get('candidate_id', 'unknown')}"
                f"@v{entry.meta.get('taxonomy_version', 'unknown')}"
                if entry.meta.get("run_id") or entry.meta.get("candidate_id")
                else f"manual-confirm/{entry.method}@{operator}"
            ),
        }],
    }
