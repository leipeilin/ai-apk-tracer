"""探索候选三档校验（T2.6，方案 §2.5）。

对 ExplorerCandidate 做确定性回查，原地填充 `validation` 字段：
- 跳回查：from/to_method_id 存在于 methods 表 + call_sites 存在
  (method_id, start_line) 行且 resolved_target_id == to_method_id、
  resolve_status == 'resolved'（方案 §2.5 原文规则）；
- 三档（T0.1 schema 冻结）：validated=全跳通过；partially_validated=
  至少一跳通过；unverified=零跳通过；
- blocked_by_guard：既有 guard_verifier 以首跳定位（入口在 release 包
  是否被 debuggable guard 确定性阻断——D3；authorization 阻断能力
  不存在，延后记录）；
- custom_sink_proposal（T2.9 接通）：sink 未命中 taxonomy 版本化文件
  → 标记 True 且状态封顶 partially_validated（方案 §2.2"进入
  partially_validated 或人工队列"——升级闭环经 promote 后重校验升档）；
  taxonomy_entries=None 禁用判定（保守 False，兼容 T2.6 行为）。

设计：docs/analysis/explorer-track/2026-08-22-t2-6-implementation-plan.md（含评审
R-1~R-8 修订）；2026-08-22-t2-9-implementation-plan.md（含评审 R-1~R-11）。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from typing import Any

from app.analysis.guard_verifier import verify_candidate_guards

LOGGER = logging.getLogger(__name__)


def validate_explorer_candidates(
    candidates: list[dict[str, Any]],
    reader: Any,
    index_path: str,
    manifest_facts: dict[str, Any],
    taxonomy_entries: Sequence[Any] | None = None,
) -> dict[str, int]:
    """原地填充候选 validation（三档），返回 {status: count} 摘要。

    校验失败不抛（阶段主链保护——单候选异常降级 unverified + notes）。
    taxonomy_entries（T2.9）：sink taxonomy 版本化条目；None=判定禁用。
    """

    counts = {"validated": 0, "partially_validated": 0, "unverified": 0}
    for candidate in candidates:
        status = _validate_one(candidate, reader, index_path, manifest_facts, taxonomy_entries)
        counts[status] = counts.get(status, 0) + 1
    return counts


def _validate_one(
    candidate: dict[str, Any],
    reader: Any,
    index_path: str,
    manifest_facts: dict[str, Any],
    taxonomy_entries: Sequence[Any] | None = None,
) -> str:
    proposal = candidate.get("chain_proposal") or {}
    hops = proposal.get("hops") or []
    if not isinstance(hops, list) or not hops:
        candidate["validation"] = {
            "status": "unverified",
            "verified_hop_count": 0,
            "failed_hop_indices": list(range(len(hops))) if isinstance(hops, list) else [],
            "blocked_by_guard": False,
            "custom_sink_proposal": False,
            "notes": "hops 缺失或结构异常，无法回查",
        }
        return "unverified"

    try:
        verified, failed, line_mismatch = _verify_hops(reader, hops)
    except Exception:
        LOGGER.exception("探索候选跳回查异常", extra={"candidate_id": candidate.get("candidate_id")})
        candidate["validation"] = {
            "status": "unverified", "verified_hop_count": None,
            "failed_hop_indices": [], "blocked_by_guard": False,
            "custom_sink_proposal": False,
            "notes": "回查过程异常（索引查询失败）",
        }
        return "unverified"

    blocked = _guard_blocked(hops[0], index_path, manifest_facts)
    verified_count = len(verified)
    if verified_count == len(hops):
        status = "validated"
        notes = f"{verified_count}/{len(hops)} 跳回查通过"
    elif verified_count > 0:
        status = "partially_validated"
        notes = f"{verified_count}/{len(hops)} 跳回查通过；失败跳 {failed}"
    else:
        status = "unverified"
        notes = f"跳均不可回查（失败跳 {failed}）"
    if line_mismatch:
        # 评审 R-1 诊断：行号不匹配但调用边存在的跳——系统性 off-by-offset
        # 的信号（validated 恒偏低时查此计数）
        notes += f"；{len(line_mismatch)} 跳仅行号不匹配（调用边存在）"
    if blocked:
        notes += "；入口被 debuggable guard 确定性阻断（release 包不可达）"

    # T2.9：custom_sink_proposal 判定接通（taxonomy 版本化文件数据源）
    custom_sink = _custom_sink_proposal(hops[-1], reader, taxonomy_entries)
    if custom_sink and status == "validated":
        # 方案 §2.2：未命中不否决——封顶 partial（人工队列走升级闭环）
        status = "partially_validated"
        notes += "；sink 未命中 taxonomy（custom sink 待人工确认，封顶 partial）"

    candidate["validation"] = {
        "status": status,
        "verified_hop_count": verified_count,
        "failed_hop_indices": failed,
        "blocked_by_guard": blocked,
        "custom_sink_proposal": custom_sink,
        "notes": notes,
    }
    return status


def _custom_sink_proposal(
    last_hop: dict[str, Any], reader: Any, taxonomy_entries: Sequence[Any] | None
) -> bool:
    """sink 命中判定（T2.9）：未命中 taxonomy → True（畸形锚点不加重）。

    None=判定禁用（兼容 T2.6 行为）；空列表=启用且零已知 sink（全未命中）。
    """

    if taxonomy_entries is None:
        return False
    from app.analysis.sink_taxonomy import (
        sink_matches_taxonomy,
        sink_method_from_method_id,
    )

    method_name = sink_method_from_method_id(last_hop.get("to_method_id"))
    if not method_name:
        return False  # 锚点畸形（无 # 或空方法名）——判定跳过
    receiver_type = _sink_receiver(reader, last_hop)
    return sink_matches_taxonomy(method_name, receiver_type, taxonomy_entries) is None


def _sink_receiver(reader: Any, last_hop: dict[str, Any]) -> str | None:
    """链尾调用点 receiver_type（call_sites (from_id, line) 行；查不到 None）。"""

    from_id = last_hop.get("from_method_id")
    line = last_hop.get("call_site_line")
    if not from_id or not isinstance(line, int):
        return None
    try:
        row = reader.db.execute(
            "SELECT receiver_type FROM call_sites WHERE method_id = ? AND start_line = ? LIMIT 1",
            (from_id, line),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return str(row["receiver_type"]) if row["receiver_type"] else None


def _verify_hops(reader: Any, hops: list[dict[str, Any]]) -> tuple[list[int], list[int], list[int]]:
    """逐跳回查（方案 §2.5 规则），返回 (verified_indices, failed_indices, line_mismatch_indices)。"""

    verified: list[int] = []
    failed: list[int] = []
    line_mismatch: list[int] = []
    for index, hop in enumerate(hops):
        from_id = hop.get("from_method_id")
        to_id = hop.get("to_method_id")
        line = hop.get("call_site_line")
        if not from_id or not to_id or not isinstance(line, int):
            failed.append(index)
            continue
        methods_ok = _method_exists(reader, from_id) and _method_exists(reader, to_id)
        if not methods_ok:
            failed.append(index)
            continue
        rows = reader.db.execute(
            "SELECT resolved_target_id, resolve_status FROM call_sites WHERE method_id = ? AND start_line = ?",
            (from_id, line),
        ).fetchall()
        if any(
            row["resolved_target_id"] == to_id and row["resolve_status"] == "resolved"
            for row in rows
        ):
            verified.append(index)
            continue
        failed.append(index)
        # R-1 诊断：行号不匹配但 (from→to) 的 resolved 边存在
        edge_rows = reader.db.execute(
            "SELECT COUNT(*) AS n FROM call_sites WHERE method_id = ? AND resolved_target_id = ? AND resolve_status = 'resolved'",
            (from_id, to_id),
        ).fetchall()
        if edge_rows and int(edge_rows[0]["n"]) > 0:
            line_mismatch.append(index)
    return verified, failed, line_mismatch


def _method_exists(reader: Any, method_id: str) -> bool:
    row = reader.db.execute("SELECT 1 FROM methods WHERE id = ?", (method_id,)).fetchone()
    return row is not None


def _guard_blocked(first_hop: dict[str, Any], index_path: str, manifest_facts: dict[str, Any]) -> bool:
    """首跳 guard 检测（D3：入口在 release 是否被 debuggable guard 阻断）。

    path 取 from_method_id 前缀（与 files.path 同源——评审认可项）；解析
    失败跳过（blocked_by_guard=False 保守不阻断）。
    """

    from_id = str(first_hop.get("from_method_id") or "")
    line = first_hop.get("call_site_line")
    path = from_id.split("#", 1)[0] if "#" in from_id else ""
    if not path or not isinstance(line, int):
        return False
    try:
        blocks = verify_candidate_guards(
            {"manifest_facts": manifest_facts, "sources": [{"path": path, "line": line}]},
            index_path,
        )
    except Exception:  # noqa: BLE001 - guard 检测失败不阻断（保守）
        LOGGER.warning("guard 检测失败（按未阻断处理）", extra={"path": path})
        return False
    return bool(blocks)
