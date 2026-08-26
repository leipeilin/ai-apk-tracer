"""探索候选归一化与关联（T2.7，方案 §2.5/§2.6）。

validated ExplorerCandidate → 正式 Candidate 形状（T0.6 映射表落地），并入
funnel 主链走现有 L2 复核；partial / unverified 不归一化（留在
explorer/candidates.json，分别由 T2.8 deep_dive 与 T2.10 人工队列消费——
M2 验收 4.3.2：未通过校验的探索候选 0 条进入正式 finding）。

同链规则候选以 related_candidate_ids 关联（方案 §2.0/§4.8：关联不合并
identity——funnel 身份含 candidate_source 分源，此处仅做人工视图对照的
显式关联，口径与 M2 验收 §4.3.1"同一链"判定一致：source 组件一致且
sink 方法一致）。

设计：docs/analysis/explorer-track/2026-08-22-t2-7-implementation-plan.md（含评审 R-1~R-7
修订：guard 双字段语义 / notes 分支收紧 / description 留空防锚定 /
SEVERITY_KEYWORDS 生产侧单一事实源）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# severity 关键词启发式（映射表 §5；生产侧单一事实源——评审 R-5：
# test_normalization_mapping.py 反向 import 本常量做契约断言）
# ---------------------------------------------------------------------------

SEVERITY_KEYWORDS: list[tuple[list[str], str]] = [
    (["任意", "远程", "执行", "泄露", "敏感", "提权", "注入"], "high"),
    (["拒绝服务", "越权", "绕过", "数据"], "medium"),
    (["信息", "提示", "低风险", "暴露"], "low"),
]

# 三档校验档位 → 候选置信档（映射表 §2 #5；缺失/未知保守 low）
CONFIDENCE_TIER_BY_STATUS: dict[str, str] = {
    "validated": "high",
    "partially_validated": "medium",
    "unverified": "low",
    "pending": "low",
}

# 可归一化的组件类型（candidate.component 枚举；other → drop，映射表 §2 #3）
_NORMALIZABLE_COMPONENT_KINDS = frozenset({"activity", "service", "provider", "receiver"})


def severity_hint_for_impact(impact_proposal: str) -> str:
    """关键词启发式（映射表 §5）：按行序首个命中返回；未命中默认 medium。

    初始档封顶 high（表内不含 critical）——探索假设不直接判 critical，L2 复核确认后升级。
    """

    text = (impact_proposal or "").lower()
    for keywords, level in SEVERITY_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return level
    return "medium"


def _severity_keyword_hit(impact_proposal: str) -> bool:
    text = (impact_proposal or "").lower()
    return any(
        keyword in text
        for keywords, _ in SEVERITY_KEYWORDS
        for keyword in keywords
    )


# ---------------------------------------------------------------------------
# method_id 解析（映射表 §6：path#Class.method:line）
# ---------------------------------------------------------------------------

def _method_id_path(method_id: str) -> str:
    return method_id.split("#", 1)[0] if "#" in method_id else method_id


def _method_id_name(method_id: str) -> str:
    tail = method_id.split("#", 1)[1] if "#" in method_id else method_id
    return tail.rpartition(":")[0] if ":" in tail else tail


def _strip_sources_prefix(path: Any) -> Any:
    """剥离 evidence_refs 自由路径的 sources/ 前缀（对齐索引 path 口径，
    indexer.py:109 相对 source_root 无前缀；参考 api_surface.py:281 模式）。
    hops 派生 path 与 files.path 同源（T2.6 评审认可），调用方无需剥离。"""

    if not isinstance(path, str):
        return path
    return path.replace("\\", "/").removeprefix("sources/")


# ---------------------------------------------------------------------------
# 归一化主入口
# ---------------------------------------------------------------------------

def normalize_explorer_candidates(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """T2.6 校验后的 ExplorerCandidate 列表 → (归一化 Candidate 列表, 计数摘要)。

    只归一化 validation.status == "validated" 且 component.kind 可映射的候选；
    单候选异常跳过 + 计数（阶段主链保护，同 T2.6 模式），不中断批次。
    """

    counts = {
        "validated_total": 0,
        "normalized": 0,
        "component_other_dropped": 0,
        "guard_blocked_promoted": 0,
        "partial_kept": 0,
        "unverified_kept": 0,
        "normalization_errors": 0,
    }
    normalized: list[dict[str, Any]] = []
    for candidate in candidates:
        validation = candidate.get("validation") if isinstance(candidate, Mapping) else None
        status = validation.get("status") if isinstance(validation, Mapping) else None
        if status == "validated":
            counts["validated_total"] += 1
        elif status == "partially_validated":
            counts["partial_kept"] += 1
            continue
        else:  # unverified / pending / 缺失——保守留在探索产物（D3）
            counts["unverified_kept"] += 1
            continue
        try:
            result = _normalize_one(candidate, validation or {})
        except Exception:  # noqa: BLE001 - 单候选异常降级跳过（不中断批次）
            counts["normalization_errors"] += 1
            LOGGER.warning(
                "探索候选归一化异常（跳过）",
                extra={"explorer_candidate_id": candidate.get("candidate_id")},
            )
            continue
        if result is None:  # other 组件 drop（映射表 §2 #3）
            counts["component_other_dropped"] += 1
            continue
        if result.get("guard_blocked"):
            counts["guard_blocked_promoted"] += 1
        normalized.append(result)
        counts["normalized"] += 1
    return normalized, counts


def _normalize_one(candidate: dict[str, Any], validation: Mapping[str, Any]) -> dict[str, Any] | None:
    component = candidate.get("component") or {}
    if not isinstance(component, Mapping):
        raise TypeError("component 结构异常")
    kind = component.get("kind")
    if kind not in _NORMALIZABLE_COMPONENT_KINDS:
        return None
    proposal = candidate.get("chain_proposal") or {}
    if not isinstance(proposal, Mapping):
        raise TypeError("chain_proposal 结构异常")
    hops = proposal.get("hops") or []
    if not isinstance(hops, list) or not hops or not isinstance(hops[0], Mapping):
        raise ValueError("validated 候选缺少结构化 hops")
    evidence_refs = [ref for ref in (proposal.get("evidence_refs") or []) if isinstance(ref, Mapping)]
    impact = str(proposal.get("impact_proposal") or "")
    status = validation.get("status")

    severity = severity_hint_for_impact(impact)
    severity_hit = _severity_keyword_hit(impact)

    # locations：evidence_refs 转换；空时 hops[0] 近似定位（映射表 §2 #7）
    locations = [
        {
            "artifact": "code",
            "path": _strip_sources_prefix(ref.get("path")),
            "line": ref.get("line") or ref.get("end_line"),
        }
        for ref in evidence_refs
    ]
    first_hop = hops[0]
    first_path = _method_id_path(str(first_hop.get("from_method_id") or ""))
    if not locations:
        locations = [{"artifact": "code", "path": first_path, "line": first_hop.get("call_site_line")}]

    # sources：链首（evidence_refs[0] 或 hops[0] 定位 + source 文本，映射表 §2 #8）
    if evidence_refs:
        source_ref = evidence_refs[0]
        source_path = _strip_sources_prefix(source_ref.get("path"))
        source_line = source_ref.get("line") or source_ref.get("end_line")
    else:
        source_path, source_line = first_path, first_hop.get("call_site_line")
    sources = [{
        "kind": "source_expression",
        "status": "fact",
        "path": source_path,
        "line": source_line,
        "text": proposal.get("source"),
    }]

    # sinks：链尾（hops[-1] 定位 + sink 文本；method_id 供 related 匹配——
    # 与规则候选 sink.method_id 同为 indexer 方法 ID 形状，M2 验收"同一链"口径）
    last_hop = hops[-1]
    last_method_id = str(last_hop.get("to_method_id") or "")
    sinks = [{
        "kind": "sink_call",
        "status": "fact",
        "path": _method_id_path(last_method_id),
        "line": last_hop.get("call_site_line"),
        "text": proposal.get("sink"),
        "method_id": last_method_id,
    }]

    blocking_gaps = _assemble_blocking_gaps(validation, severity_hit)

    # guard 双字段（评审 R-3：funnel 读顶层布尔跳 AI；decision.py:880-884 只认
    # guard_blocks 列表判 blocked——对齐 apply_guard_verification"同写同删"契约）
    guard_blocked = bool(validation.get("blocked_by_guard"))

    result: dict[str, Any] = {
        # required 10 项（T0.6 映射表 §2）
        "rule_id": "EXPLORER_AGENT",
        "rule_version": candidate.get("prompt_version") or "explorer/1.0.0",
        "component": kind,
        "severity_hint": severity,
        "confidence_tier": CONFIDENCE_TIER_BY_STATUS.get(str(status), "low"),
        "evidence_level": "L2",
        "locations": locations,
        "sources": sources,
        "sinks": sinks,
        "blocking_gaps": blocking_gaps,
        # 非 required（映射表 §3；description 留空防锚定——评审 R-2，
        # AI 完成后由 _apply_ai_analysis 以 analysis.summary 回填）
        "title": "Explorer Candidate",
        "component_name": component.get("name"),
        "entry_points": [component.get("name")],
        "entry_method_id": component.get("entry_method"),
        "authorization_status": "unknown",
        "dataflow_status": "not_proven",
        "guard_status": "unknown",
        "reachability_status": "reachable" if component.get("exported") else "conditional",
        "analysis_status": "explorer_only",
        "deterministic_chain_verified": False,
        "chain_id": candidate.get("candidate_id"),
        "prompt_version": candidate.get("prompt_version"),
        "model": candidate.get("model"),
        # 探索轨关联字段（funnel 三分流依据 + 探索产物回溯）
        "candidate_source": "explorer",
        "explorer_candidate_id": candidate.get("candidate_id"),
        "explorer_validation_status": status,
    }
    if guard_blocked:
        result["guard_blocked"] = True
        result["guard_blocks"] = [{
            "type": "debuggable",
            "path": first_path,
            "line": first_hop.get("call_site_line"),
            "method": _method_id_name(str(first_hop.get("from_method_id") or "")),
        }]
    return result


def _assemble_blocking_gaps(validation: Mapping[str, Any], severity_hit: bool) -> list[dict[str, Any]]:
    """按映射表 §4（修订版）分支序组装（评审 R-4：validated 纯成功摘要不产
    EXPLORER_CHAIN_INCOMPLETE——T2.6 实现中 validated 的 notes 恒为
    "N/N 跳回查通过"成功摘要，产出该 gap 会名不副实）。"""

    gaps: list[dict[str, Any]] = []
    notes = validation.get("notes")
    status = validation.get("status")
    if status != "validated" or "异常" in str(notes or ""):
        gaps.append({
            "code": "EXPLORER_CHAIN_INCOMPLETE",
            "message": str(notes or "链不完整"),
            "critical": False,
            "evidence_refs": [],
        })
    for index in validation.get("failed_hop_indices") or []:
        gaps.append({
            "code": "EXPLORER_HOP_UNVERIFIED",
            "message": f"第 {index} 跳未通过 call_sites 回查",
            "critical": False,
            "evidence_refs": [],
        })
    if validation.get("custom_sink_proposal"):
        gaps.append({
            "code": "CUSTOM_SINK_PROPOSAL",
            "message": "sink 未命中 taxonomy，待人工确认",
            "critical": False,
            "evidence_refs": [],
        })
    if validation.get("blocked_by_guard"):
        gaps.append({
            "code": "EXPLORER_GUARD_BLOCKED",
            "message": "被 Guard/授权确定性阻断",
            "critical": True,
            "evidence_refs": [],
        })
    if severity_hit:
        gaps.append({
            "code": "EXPLORER_SEVERITY_HYPOTHESIS",
            "message": "severity_hint 基于探索假设文本启发式，待 L2 复核",
            "critical": False,
            "evidence_refs": [],
        })
    return gaps


# ---------------------------------------------------------------------------
# related_candidate_ids 关联（funnel 后调用——candidate_id 已生成）
# ---------------------------------------------------------------------------

def link_related_candidates(candidates: list[dict[str, Any]]) -> dict[str, int]:
    """探索归一化候选与规则候选同链时双向写 related_candidate_ids（幂等）。

    同链口径（M2 验收 §4.3.1）：component_name 相等 且 sink 方法一致——
    优先 method_id（indexer 方法 ID）精确匹配，缺失时退化 (path, line)。
    仅 explorer→rule 方向配对（同源候选不互写，N-4）。
    """

    rule_sink_index: dict[tuple[str, tuple], dict[str, Any]] = {}
    for candidate in candidates:
        if candidate.get("candidate_source") == "explorer":
            continue
        component_name = str(candidate.get("component_name") or "")
        for key in _sink_keys(candidate):
            rule_sink_index.setdefault((component_name, key), candidate)

    pair_count = 0
    explorer_linked = 0
    linked_rule_ids: set[str] = set()
    for candidate in candidates:
        if candidate.get("candidate_source") != "explorer":
            continue
        component_name = str(candidate.get("component_name") or "")
        self_id = str(candidate.get("candidate_id") or "")
        related: list[str] = list(candidate.get("related_candidate_ids") or [])
        matched_rule_candidates: dict[str, dict[str, Any]] = {}
        for key in _sink_keys(candidate):
            matched = rule_sink_index.get((component_name, key))
            if matched is not None and matched is not candidate:
                # 同一规则候选可被 method 键与 location 键先后命中——按 id 去重
                matched_rule_candidates.setdefault(str(matched.get("candidate_id") or ""), matched)
        if not matched_rule_candidates:
            continue
        for matched in matched_rule_candidates.values():
            matched_id = str(matched.get("candidate_id") or "")
            if matched_id and matched_id != self_id and matched_id not in related:
                related.append(matched_id)
                pair_count += 1
            if self_id and self_id != matched_id:
                matched_related = list(matched.get("related_candidate_ids") or [])
                if self_id not in matched_related:
                    matched_related.append(self_id)
                matched["related_candidate_ids"] = matched_related
                linked_rule_ids.add(matched_id)
        candidate["related_candidate_ids"] = related
        explorer_linked += 1
    return {
        "explorer_linked": explorer_linked,
        "rule_candidate_linked": len(linked_rule_ids),
        "pair_count": pair_count,
    }


def _sink_keys(candidate: dict[str, Any]) -> list[tuple]:
    """候选全部 sink 的匹配键（方法级优先 + 位置级退化）。

    一侧候选带 method_id、另一侧缺失时（规则候选 sink 形状多样），
    两侧都同时注册 method 键与 location 键——任一键相等即视为同一 sink
    （方法级与 M2 验收"同一链"判定一致；location 键 (path, line) 中
    line 两侧均为调用点行号，语义一致）。
    """

    keys: list[tuple] = []
    for sink in candidate.get("sinks") or []:
        if not isinstance(sink, Mapping):
            continue
        method_id = sink.get("method_id")
        if method_id:
            keys.append(("method", str(method_id)))
        path, line = sink.get("path"), sink.get("line")
        if path and isinstance(line, int):
            keys.append(("location", str(path), line))
    return keys
