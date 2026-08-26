"""探索候选人工队列构建（T2.10，方案 §2.0/§5.4 + M2 验收 §4.3.3）。

partial/unverified/pending 候选的人工队列视图数据源：投影（脱 hops 全文
与轮审计——防响应膨胀）+ 服务端预排序（置信度主键 → deep_dive 证据次键
→ 跳回查完整度——评审 R-1：unverified 无深挖证据不被系统性埋没）+
计数汇总（validated 仅计数不进列表——已并入主链 findings）。

设计：docs/analysis/explorer-track/2026-08-22-t2-10-implementation-plan.md（含评审
R-1~R-9 修订）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# 置信度排序权重（未知值 0 排最后）
_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

# 列表主体档位（validated 已进主链 findings——仅计数对照，评审 D1）
_QUEUE_STATUSES = {"partially_validated", "unverified", "pending"}


def build_explorer_queue(candidates: Sequence[Any]) -> dict[str, Any]:
    """探索候选 → 人工队列（投影 + 预排序 + 计数）。空/畸形输入全容错。"""

    entries: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "validated": 0, "partially_validated": 0, "unverified": 0,
        "pending": 0, "total": 0, "queue_length": 0, "deep_dive_completed": 0,
    }
    for raw in candidates:
        if not isinstance(raw, Mapping):
            continue
        counts["total"] += 1
        validation = raw.get("validation") or {}
        status = str(validation.get("status") or "pending")
        if status in counts:
            counts[status] += 1
        else:
            counts["pending"] += 1
            status = "pending"

        proposal = raw.get("chain_proposal") or {}
        hops = proposal.get("hops") or []
        hop_count = len(hops) if isinstance(hops, list) else 0
        confidence = str(proposal.get("confidence") or "")
        deep_dive = raw.get("deep_dive") or None
        deep_dive_view = None
        deep_dive_evidence = 0
        if isinstance(deep_dive, Mapping):
            evidence_refs = deep_dive.get("evidence_refs") or []
            resolved = deep_dive.get("resolved_facts") or []
            deep_dive_evidence = len(evidence_refs) if isinstance(evidence_refs, list) else 0
            deep_dive_view = {
                "status": deep_dive.get("status"),
                "evidence_count": deep_dive_evidence,
                "confirmed_fact_count": (
                    sum(1 for item in resolved
                        if isinstance(item, Mapping) and item.get("conclusion") == "confirmed")
                    if isinstance(resolved, list) else 0
                ),
                "remaining_gap_count": (
                    len(deep_dive.get("remaining_gaps") or [])
                    if isinstance(deep_dive.get("remaining_gaps"), list) else 0
                ),
                "unverifiable_evidence_count": int(
                    deep_dive.get("unverifiable_evidence_count") or 0),
                "evidence_truncated_count": int(
                    deep_dive.get("evidence_truncated_count") or 0),
                "requests_used": int(deep_dive.get("requests_used") or 0),
            }
            if deep_dive.get("status") == "completed":
                counts["deep_dive_completed"] += 1

        verified = validation.get("verified_hop_count")
        hop_ratio = (int(verified) / hop_count) if isinstance(verified, int) and hop_count else 0.0
        entry = {
            "candidate_id": raw.get("candidate_id"),
            "component": _component_view(raw.get("component")),
            "chain": {
                "source": proposal.get("source"),
                "sink": proposal.get("sink"),
                "hop_count": hop_count,
            },
            "validation": {
                "status": status,
                "verified_hop_count": verified if isinstance(verified, int) else None,
                "failed_hop_indices": validation.get("failed_hop_indices") or [],
                "blocked_by_guard": bool(validation.get("blocked_by_guard")),
                "custom_sink_proposal": bool(validation.get("custom_sink_proposal")),
                "notes": validation.get("notes"),
            },
            "deep_dive": deep_dive_view,
            "confidence": confidence or None,
            "sort_keys": {
                "confidence_rank": _CONFIDENCE_RANK.get(confidence, 0),
                "deep_dive_evidence": deep_dive_evidence,
                "hop_ratio": hop_ratio,
            },
        }
        entries.append(entry)

    # 预排序（评审 R-1）：置信度 ↓ → deep_dive 证据 ↓ → 跳完整度 ↓ → id 稳定序
    entries.sort(key=lambda item: (
        -item["sort_keys"]["confidence_rank"],
        -item["sort_keys"]["deep_dive_evidence"],
        -item["sort_keys"]["hop_ratio"],
        str(item["candidate_id"] or ""),
    ))
    queue = [entry for entry in entries if entry["validation"]["status"] in _QUEUE_STATUSES]
    counts["queue_length"] = len(queue)
    return {"entries": queue, "counts": counts}


def _component_view(component: Any) -> dict[str, Any]:
    if not isinstance(component, Mapping):
        return {"kind": None, "name": None, "entry_method": None}
    return {
        "kind": component.get("kind"),
        "name": component.get("name"),
        "entry_method": component.get("entry_method"),
    }
