"""核验 agent（T2.11，方案 §2.7——L2 review 的 agent 化演进）。

验证导向的受控取证循环：命题清单由确定性代码从候选事实生成（不从提出者
描述生成）；盲验输入只含可回查事实层（剥离 hypothesis/impact_proposal/
confidence/reasoning/needs_expansion 与 evidence_refs[].claim）；终止条件
=命题全部判定（代码判定，非模型自声明 loop.done）；输出对齐 L2 关键决策
字段（verdict/flaw_holds/exploitability/refutation_basis），DecisionEngine
消费方式不变（分流与适配层为 T2.12）。

设计：docs/analysis/2026-08-22-t2-11-implementation-plan.md（含评审
R-1~R-10 修订：reader 通道/一致性规则 4/request_budget 提前终止/claim
剥离/undecided_claim_indices 物化/claims 空快速返回/首轮双路径上下文）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.analysis.ai_models import VerifyInput, VerifyOutput
from app.analysis.explorer import (
    _evidence_key,
    dispatch_read,
    file_window,
    filter_evidence,
)
from app.config import VerifySettings

LOGGER = logging.getLogger(__name__)

# 单段上下文 8KB 截断（对齐 explorer 轮间累积膨胀控制）
_MAX_SEGMENT_BYTES = 8 * 1024

# VerifyInput.code_context（LongText max 10_000）留余量
_MAX_VERIFY_CONTEXT_CHARS = 9500

# schema 上界（ai_verify_{input,output}.schema.json）
_MAX_EVIDENCE = 64

# 核心命题类型（一致性校验规则 3/4 作用域——prompt 硬约束 3 的确定性落地）
_CORE_CLAIM_KINDS = {"entry_reachable", "source_controllability", "propagation", "sink_behavior"}

# 字段缺失（get → None）与显式 "unknown" 均视为未知不触发（评审 R-10②）
_UNKNOWN_STATUSES = {None, "unknown"}


def build_verify_claims(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    """命题清单生成器（确定性——方案 §2.7：不从 Agent1 描述生成）。

    六类命题按候选确定性字段触发；索引 0 起连续；上限 32（schema）。
    """

    component_name = str(candidate.get("component_name") or "")
    sources = [s for s in (candidate.get("sources") or []) if isinstance(s, Mapping)]
    sinks = [s for s in (candidate.get("sinks") or []) if isinstance(s, Mapping)]

    def _loc(item: Mapping[str, Any]) -> str:
        path, line = item.get("path"), item.get("line")
        return f"{path}:{line}" if path else "未知位置"

    pending: list[tuple[str, str]] = []
    if component_name:
        pending.append((
            "entry_reachable",
            f"入口组件 {component_name} 的入口是否可被外部触发（exported 或隐式 intent 可达）",
        ))
    if sources:
        pending.append((
            "source_controllability",
            (
                f"source（{_loc(sources[0])}）的值是否攻击者可控"
                "（源自入口参数/外部输入而非硬编码常量）"
            ),
        ))
    if sources and sinks:
        pending.append((
            "propagation",
            (
                f"攻击者可控值是否从 source（{_loc(sources[0])}）传播到 sink（{_loc(sinks[0])}），"
                "无中途净化、终止或覆盖"
            ),
        ))
    if sinks:
        pending.append((
            "sink_behavior",
            f"sink（{_loc(sinks[0])}）是否执行真实敏感操作（非空实现、非已失效包装）",
        ))
    if candidate.get("guard_status") not in _UNKNOWN_STATUSES or candidate.get("guard_blocked"):
        pending.append((
            "guard_effective",
            "Guard 检查是否有效阻断攻击路径（release 配置下 fail-closed）",
        ))
    if candidate.get("authorization_status") not in _UNKNOWN_STATUSES:
        pending.append((
            "authorization",
            "权限/签名级授权是否阻止外部应用触发该组件",
        ))
    return [
        {"index": index, "kind": kind, "statement": statement}
        for index, (kind, statement) in enumerate(pending[:32])
    ]


def build_deterministic_facts(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    """盲验事实（结构化 VerifyFact——剥离 severity/confidence/hypothesis 语义）。"""

    facts: list[tuple[str, str]] = []
    component_name = str(candidate.get("component_name") or "")
    sources = [s for s in (candidate.get("sources") or []) if isinstance(s, Mapping)]
    sinks = [s for s in (candidate.get("sinks") or []) if isinstance(s, Mapping)]

    def _loc(item: Mapping[str, Any]) -> str:
        path, line = item.get("path"), item.get("line")
        return f"{path}:{line}" if path else "未知位置"

    if component_name:
        facts.append((
            "component",
            f"组件 {component_name}（类型 {candidate.get('component') or 'unknown'}）",
        ))
    reachability = candidate.get("reachability_status")
    if reachability:
        facts.append(("reachability", f"入口可达性状态：{reachability}"))
    guard_status = candidate.get("guard_status")
    blocked_note = "（release 包被 debuggable guard 确定性阻断）" if candidate.get("guard_blocked") else ""
    if guard_status not in _UNKNOWN_STATUSES:
        facts.append(("guard", f"Guard 状态：{guard_status}{blocked_note}"))
    elif candidate.get("guard_blocked"):
        facts.append(("guard", f"Guard 状态：未知{blocked_note}"))
    authorization = candidate.get("authorization_status")
    if authorization not in _UNKNOWN_STATUSES:
        facts.append(("authorization", f"授权状态：{authorization}"))
    if sources:
        text = str(sources[0].get("text") or "").strip()
        facts.append(("source", f"source 位置：{_loc(sources[0])}（{text}）" if text
                      else f"source 位置：{_loc(sources[0])}"))
    if sinks:
        text = str(sinks[0].get("text") or "").strip()
        facts.append(("sink", f"sink 位置：{_loc(sinks[0])}（{text}）" if text
                      else f"sink 位置：{_loc(sinks[0])}"))
    return [{"fact_type": fact_type, "statement": statement} for fact_type, statement in facts]


def build_chain_facts(explorer_candidate: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """探索候选剥离版链事实（投影 chain_proposal，剥离五假设字段 + claim）。

    评审 R-5：evidence_refs 投影时 claim 置 None（claim 为提出者生成文本，
    防锚定核验器）。
    """

    if not explorer_candidate:
        return None
    proposal = explorer_candidate.get("chain_proposal")
    if not isinstance(proposal, Mapping):
        return None
    source, sink = str(proposal.get("source") or "").strip(), str(proposal.get("sink") or "").strip()
    hops = [hop for hop in (proposal.get("hops") or []) if isinstance(hop, Mapping)]
    if not source or not sink or not hops:
        return None
    evidence_refs = [
        {**ref, "claim": None}
        for ref in (proposal.get("evidence_refs") or [])
        if isinstance(ref, Mapping)
    ]
    call_tree_refs = [
        str(ref) for ref in (proposal.get("call_tree_refs") or []) if isinstance(ref, str)
    ]
    return {
        "source": source,
        "sink": sink,
        "hops": hops,
        "call_tree_refs": call_tree_refs,
        "evidence_refs": evidence_refs,
    }


class VerifyAgent:
    """核验 agent 受控取证循环（L2 agent 化演进——方案 §2.7）。

    ai_call 回调（async (VerifyInput) -> dict）由调用方注入（T2.12 接预算
    包装；测试注入替身）。reader 为索引只读句柄（评审 R-1：证据回查需
    files 表）。终止条件=命题全部判定（代码判定，非模型自声明 loop.done）。
    """

    def __init__(
        self,
        ai_call: Callable[[VerifyInput], Awaitable[dict[str, Any]]],
        call_tree: Any,
        settings: VerifySettings,
        run_dir: Path,
        reader: Any,
    ) -> None:
        self._ai_call = ai_call
        self._call_tree = call_tree
        self._settings = settings
        self._run_dir = run_dir
        self._reader = reader
        self._ai_requests_used = 0
        self._read_requests_used = 0

    @property
    def ai_requests_used(self) -> int:
        return self._ai_requests_used

    @property
    def read_requests_used(self) -> int:
        return self._read_requests_used

    async def verify(
        self,
        candidate: Mapping[str, Any],
        explorer_candidate: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """单候选核验（不改写输入候选）。返回契约见任务方案 §3.2（T2.12 消费）。"""

        claims = build_verify_claims(candidate)
        if not claims:
            # 评审 R-8：无命题可证（schema minItems=1 防线前移）——快速返回
            result = {
                "status": "skipped", "terminated_by": "no_claims", "output": None,
                "claims": [], "rounds": [], "requests_used": 0, "read_requests_used": 0,
                "undecided_claim_indices": [], "consistency_downgraded": False,
            }
            self._append_observation(candidate, result)
            return result

        facts = build_deterministic_facts(candidate)
        chain_facts = build_chain_facts(explorer_candidate)
        verdicts: dict[int, dict[str, Any]] = {}
        evidence_pool: list[dict[str, Any]] = []
        evidence_keys: set[tuple[Any, ...]] = set()
        evidence_truncated = 0
        unverifiable = 0
        rounds: list[dict[str, Any]] = []
        context_keys: set[Any] = set()
        context_segments: list[str] = []
        prompt_version, model_name = "verify/1.0.0", "unknown"
        terminated_by = "round_budget"
        final_output: VerifyOutput | None = None
        candidate_id = str(candidate.get("candidate_id") or candidate.get("chain_id") or "unknown")

        code_context = self._initial_context(candidate, context_keys, context_segments)

        for round_index in range(1, self._settings.max_rounds_per_candidate + 1):
            try:
                model_input = VerifyInput.model_validate({
                    "candidate_id": candidate_id,
                    "claims": claims,
                    "chain_facts": chain_facts,
                    "evidence_refs": evidence_pool,
                    "deterministic_facts": facts,
                    "code_context": code_context,
                })
            except ValidationError:
                LOGGER.warning(
                    "VerifyInput 构造失败（候选畸形）", extra={"candidate_id": candidate_id},
                )
                return self._terminal_result(
                    candidate, "failed", "error", rounds, claims, verdicts,
                    evidence_truncated=evidence_truncated, unverifiable=unverifiable,
                    prompt_version=prompt_version, model=model_name,
                )

            result = await self._ai_call(model_input)
            self._ai_requests_used += 1
            metadata = result.get("metadata") or {}
            prompt_version = f"verify/{metadata.get('prompt_version') or '1.0.0'}"
            model_name = metadata.get("model") or "unknown"
            input_hash = _input_hash(model_input)

            if result.get("status") != "completed":
                circuit = bool(result.get("circuit_breaking")) or result.get("status") == "skipped"
                rounds.append({
                    "round_index": round_index, "model_input_hash": input_hash,
                    "prompt_version": prompt_version, "model": model_name,
                    "status": "skipped" if circuit else "error", "output": None,
                })
                return self._terminal_result(
                    candidate, "skipped" if circuit else "failed",
                    "short_circuit" if circuit else "error",
                    rounds, claims, verdicts,
                    evidence_truncated=evidence_truncated, unverifiable=unverifiable,
                    prompt_version=prompt_version, model=model_name,
                )

            try:
                output = VerifyOutput.model_validate(result.get("analysis") or {})
            except ValidationError:
                LOGGER.warning(
                    "VerifyOutput 解析失败（repair 兜底后仍失败）",
                    extra={"candidate_id": candidate_id},
                )
                rounds.append({
                    "round_index": round_index, "model_input_hash": input_hash,
                    "prompt_version": prompt_version, "model": model_name,
                    "status": "output_invalid", "output": None,
                })
                return self._terminal_result(
                    candidate, "failed", "error", rounds, claims, verdicts,
                    evidence_truncated=evidence_truncated, unverifiable=unverifiable,
                    prompt_version=prompt_version, model=model_name,
                )

            # 取证读码执行（预算 = 总额 − 已用；评审 R-4：预算内截断）
            remaining_requests = self._settings.max_requests_per_candidate - self._read_requests_used
            executed = self._execute_reads(output, max(remaining_requests, 0))

            # 证据回查过滤（claim 剥离 + 跨轮去重 + 上界截断计数）
            new_refs, dropped = filter_evidence(
                [ref.model_dump(mode="json") for ref in output.evidence_refs], self._reader
            )
            unverifiable += dropped
            for ref in new_refs:
                ref = {**ref, "claim": None}
                key = _evidence_key(ref)
                if key in evidence_keys:
                    continue
                if len(evidence_pool) >= _MAX_EVIDENCE:
                    evidence_truncated += 1
                    continue
                evidence_keys.add(key)
                evidence_pool.append(ref)

            # 命题判定合并（后轮同 index 覆盖前轮）
            for verdict in output.claims_verdicts:
                verdict_dict = verdict.model_dump(mode="json")
                verdict_refs, verdict_dropped = filter_evidence(
                    verdict_dict.get("evidence") or [], self._reader
                )
                unverifiable += verdict_dropped
                verdict_dict["evidence"] = [{**ref, "claim": None} for ref in verdict_refs]
                verdicts[verdict.index] = verdict_dict

            final_output = output
            rounds.append({
                "round_index": round_index, "model_input_hash": input_hash,
                "prompt_version": prompt_version, "model": model_name,
                "status": "completed", "output": output.model_dump(mode="json"),
            })

            # 终止判定（代码判定——loop.done 仅记录不作依据）
            undecided = [c["index"] for c in claims if c["index"] not in verdicts]
            if not undecided:
                terminated_by = "all_claims_decided"
                break
            # 读码预算耗尽且尚有未判定命题 → 提前终止（评审 R-4：省空转轮）
            if self._settings.max_requests_per_candidate - self._read_requests_used <= 0:
                terminated_by = "request_budget"
                break
            # 上下文累积（下一轮）
            if executed["texts"]:
                context_segments.extend(executed["texts"])
                code_context = "\n---\n".join(context_segments)[:_MAX_VERIFY_CONTEXT_CHARS]

        undecided = [c["index"] for c in claims if c["index"] not in verdicts]
        if final_output is None:
            return self._terminal_result(
                candidate, "failed", "error", rounds, claims, verdicts,
                evidence_truncated=evidence_truncated, unverifiable=unverifiable,
                prompt_version=prompt_version, model=model_name,
            )

        aggregated = final_output.model_dump(mode="json")
        aggregated["claims_verdicts"] = [verdicts[index] for index in sorted(verdicts)]
        aggregated["evidence_refs"] = evidence_pool
        aggregated["analysis_complete"] = not undecided
        aggregated["read_requests"] = []
        aggregated["loop"] = {"done": not undecided, "reason": terminated_by}
        if unverifiable or evidence_truncated:
            aggregated["evidence_filter_note"] = (
                f"不可回查证据已丢弃 {unverifiable} 条；超界截断 {evidence_truncated} 条"
            )
        downgraded, aggregated = self._validate_consistency(aggregated, claims, verdicts)

        result = {
            "status": "completed",
            "terminated_by": terminated_by,
            "output": aggregated,
            "claims": claims,
            "rounds": rounds,
            "requests_used": len(rounds),
            "read_requests_used": self._read_requests_used,
            "undecided_claim_indices": undecided,
            "consistency_downgraded": downgraded,
        }
        self._append_observation(candidate, result)
        return result

    # ------------------------------------------------------------------
    # 一致性校验（schema 注记的实现层落地——评审 R-2 补规则 4）
    # ------------------------------------------------------------------

    def _validate_consistency(
        self, aggregated: dict[str, Any], claims: list[dict[str, Any]],
        verdicts: dict[int, dict[str, Any]],
    ) -> tuple[bool, dict[str, Any]]:
        """整体判定与命题判定一致性（违例确定性降级 unresolved）。"""

        verdict = aggregated.get("verdict")
        flaw_holds = aggregated.get("flaw_holds")
        claim_kind = {claim["index"]: claim["kind"] for claim in claims}
        core_refuted = any(
            item["conclusion"] == "refuted"
            for index, item in verdicts.items() if claim_kind.get(index) in _CORE_CLAIM_KINDS
        )
        core_unknown = any(
            item["conclusion"] == "still_unknown"
            for index, item in verdicts.items() if claim_kind.get(index) in _CORE_CLAIM_KINDS
        )
        reasons: list[str] = []
        if verdict == "supports_candidate" and flaw_holds is False:
            reasons.append("supports_candidate 与 flaw_holds=False 矛盾")
        if verdict == "refutes_candidate" and flaw_holds is True:
            reasons.append("refutes_candidate 与 flaw_holds=True 矛盾")
        if verdict == "supports_candidate" and core_refuted:
            reasons.append("核心命题存在 refuted")
        if verdict == "supports_candidate" and core_unknown:
            reasons.append("核心命题存在 still_unknown")
        if reasons:
            aggregated["verdict"] = "unresolved"
            aggregated["consistency_note"] = (
                "整体判定与命题判定不一致，已确定性降级：" + "；".join(reasons)
            )
            return True, aggregated
        return False, aggregated

    # ------------------------------------------------------------------
    # 上下文与读码执行
    # ------------------------------------------------------------------

    def _initial_context(
        self, candidate: Mapping[str, Any],
        context_keys: set[Any], context_segments: list[str],
    ) -> str | None:
        """首轮 code_context（评审 R-9 双路径：method_id 方法体 / path:line 行窗口）。"""

        sources = [s for s in (candidate.get("sources") or []) if isinstance(s, Mapping)]
        sinks = [s for s in (candidate.get("sinks") or []) if isinstance(s, Mapping)]

        def _append(key: Any, payload: dict[str, Any] | None) -> None:
            if payload is None or key in context_keys:
                return
            serialized = json.dumps(payload, ensure_ascii=False)
            if len(serialized) > _MAX_SEGMENT_BYTES:
                serialized = serialized[:_MAX_SEGMENT_BYTES] + '…", "truncated": true}'
            if sum(len(segment) for segment in context_segments) + len(serialized) > _MAX_VERIFY_CONTEXT_CHARS:
                return
            context_keys.add(key)
            context_segments.append(serialized)

        for item in [*sources, *sinks]:
            method_id = item.get("method_id")
            if method_id:
                body = dispatch_read(self._call_tree, "get_method_body", str(method_id), None, None)
                if isinstance(body, dict) and "not_found" not in body:
                    _append(method_id, body)
                continue
            path, line = item.get("path"), item.get("line")
            if path and isinstance(line, int):
                _append((path, line), file_window(self._reader, str(path), line))
        return "\n---\n".join(context_segments) or None

    def _execute_reads(self, output: VerifyOutput, budget: int) -> dict[str, Any]:
        """执行本轮取证读码请求（限额内截断；未命中统一 not_found）。"""

        records: list[dict[str, Any]] = []
        texts: list[str] = []
        for request in output.read_requests[: max(budget, 0)]:
            payload = dispatch_read(
                self._call_tree, request.operation, request.target, request.path, request.line
            )
            self._read_requests_used += 1
            records.append({"operation": request.operation, "target": request.target})
            serialized = json.dumps(payload, ensure_ascii=False)
            if len(serialized) > _MAX_SEGMENT_BYTES:
                serialized = serialized[:_MAX_SEGMENT_BYTES] + '…", "truncated": true}'
            texts.append(serialized)
        return {"records": records, "texts": texts}

    # ------------------------------------------------------------------
    # 终态与落盘
    # ------------------------------------------------------------------

    def _terminal_result(
        self, candidate: Mapping[str, Any], status: str, terminated_by: str,
        rounds: list[dict[str, Any]], claims: list[dict[str, Any]],
        verdicts: dict[int, dict[str, Any]], *,
        evidence_truncated: int = 0, unverifiable: int = 0,
        prompt_version: str = "verify/1.0.0", model: str = "unknown",
    ) -> dict[str, Any]:
        """失败/跳过终态（无整体 observation——缺口清单照常物化）。"""

        result = {
            "status": status,
            "terminated_by": terminated_by,
            "output": None,
            "claims": claims,
            "rounds": rounds,
            "requests_used": len(rounds),
            "read_requests_used": self._read_requests_used,
            "undecided_claim_indices": [
                claim["index"] for claim in claims if claim["index"] not in verdicts
            ],
            "consistency_downgraded": False,
        }
        self._append_observation(candidate, result)
        return result

    def _append_observation(
        self, candidate: Mapping[str, Any], result: Mapping[str, Any]
    ) -> None:
        """轮审计落盘（追加模式——对齐 explorer/observations.json 先例）。"""

        path = self._run_dir / "verify" / "observations.json"
        try:
            payload: dict[str, Any] = {"entries": []}
            if path.is_file():
                try:
                    loaded = json.loads(path.read_text("utf-8"))
                    if isinstance(loaded.get("entries"), list):
                        payload = loaded
                except (json.JSONDecodeError, OSError):
                    LOGGER.warning("verify/observations.json 损坏，重新初始化")
            payload["entries"].append({
                "candidate_id": str(candidate.get("candidate_id") or "unknown"),
                "status": result.get("status"),
                "terminated_by": result.get("terminated_by"),
                "requests_used": result.get("requests_used"),
                "read_requests_used": result.get("read_requests_used"),
                "undecided_claim_indices": result.get("undecided_claim_indices"),
                "consistency_downgraded": result.get("consistency_downgraded"),
                "rounds": list(result.get("rounds") or []),
            })
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
            os.chmod(path, 0o600)
        except OSError:
            LOGGER.warning("verify/observations.json 写入失败（审计降级）")


def _input_hash(model_input: VerifyInput) -> str:
    """轮输入哈希（输入可审计复现的最小载体——对齐 explorer 模式）。"""

    return hashlib.sha256(
        json.dumps(model_input.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# 适配层（T2.12，M0 审查 §4.2）：VerifyOutput 聚合 → L2 analysis 形状
# ---------------------------------------------------------------------------


def _to_evidence_reference(ref: Mapping[str, Any]) -> dict[str, Any] | None:
    """ExplorerEvidenceRef → EvidenceReference（context_id=path#window 格式）。

    无 line 证据不可定位进 window 格式——静默丢弃（D3：保守削弱不拦截；
    丢弃计数由 verify_agent 的 evidence_filter_note 承载）。
    """

    path, line = ref.get("path"), ref.get("line")
    if not path or not isinstance(line, int):
        return None
    end = ref.get("end_line") if isinstance(ref.get("end_line"), int) else line
    return {
        "context_id": f"{path}#window:{line}-{end}",
        "path": path, "line": line, "end_line": end,
        "claim": f"verify agent 回查通过的证据位置（{path}:{line}）",
    }


def adapt_verify_result(verify_result: Mapping[str, Any]) -> dict[str, Any]:
    """VerifyOutput 聚合 → L2 analysis dict（_adapt_l2_analysis 同构 + 溯源）。

    铁律（M0 审查 §4.2）：补齐字段全部确定性默认值——harm/reachability_class/
    impact_vector/reverse_exclusion 生产代码零消费（纯前向兼容存储）；guard_status
    ="unknown" 为诚实缺省（决策层不消费分析层 guard——评审 R-6，仅审计展示）。
    evidence_refs 转 EvidenceReference 后由调用方注入 ai_evidence_contexts
    （评审 R-1：聚合层 _ai_evidence_contexts 优先读取显式注入）。
    """

    output = verify_result.get("output") or {}
    refs = [
        reference for reference in (
            _to_evidence_reference(ref) for ref in output.get("evidence_refs") or []
        ) if reference is not None
    ]
    undecided = list(verify_result.get("undecided_claim_indices") or [])
    verdict = output.get("verdict")
    claims_verdicts = output.get("claims_verdicts") or []
    claim_kind_by_index = {
        claim["index"]: claim.get("kind") for claim in verify_result.get("claims") or []
    }
    guard_claim = next(
        (item for item in claims_verdicts
         if claim_kind_by_index.get(item.get("index")) == "guard_effective"), None
    )
    return {
        "summary": output.get("summary"),
        "verdict": verdict,
        "confidence_tier": output.get("confidence_tier"),
        "guard_status": "unknown",
        "evidence_refs": refs,
        "blocking_gaps": (
            [{"code": "VERIFY_CLAIMS_UNDECIDED", "critical": True,
              "message": f"核验预算内未完成全部命题判定（未判定 {len(undecided)} 项）",
              "evidence_refs": []}]
            if undecided else []
        ),
        "uncertainties": [],
        "context_requests": [],
        "flaw_holds": output.get("flaw_holds"),
        "exploitability": output.get("exploitability"),
        "harm": {"impact_type": "other", "impact_target": "verify agent 适配默认值（未评估）",
                 "server_confirmation_required": False},
        "reachability_class": "local",
        "impact_vector": {"confidentiality": "none", "integrity": "none",
                          "availability": "none", "privileges_required": "low",
                          "attack_complexity": "high", "user_interaction": "none"},
        "reverse_exclusion": [],
        "confidence_rationale": (
            f"verify agent 核验：terminated_by={verify_result.get('terminated_by')}，"
            f"命题未判定 {len(undecided)} 项"
            + ("，整体判定经一致性校验降级" if verify_result.get("consistency_downgraded") else "")
        ),
        "refutation_basis": output.get("refutation_basis") or [],
        "analysis_complete": bool(output.get("analysis_complete")),
        "promotion_recommended": verdict == "supports_candidate" and not undecided,
        "candidate_verdict": verdict,
        "analysis_track": "verify",
        "verified_evidence_refs": refs,
        "invalid_evidence_refs": [],
        "verify_agent": {
            "terminated_by": verify_result.get("terminated_by"),
            "requests_used": verify_result.get("requests_used"),
            "read_requests_used": verify_result.get("read_requests_used"),
            "undecided_claim_indices": undecided,
            "consistency_downgraded": verify_result.get("consistency_downgraded"),
            # 评审 R-6：核验已产出的 guard_effective 命题判定（人工视图/审计）
            "guard_claim_verdict": (
                {"conclusion": guard_claim.get("conclusion"), "reasoning": guard_claim.get("reasoning")}
                if guard_claim else None
            ),
        },
    }


def evidence_contexts_for(analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
    """适配 analysis → ai_evidence_contexts 注入体（评审 R-1）。

    聚合层 _ai_evidence_contexts 优先读取 candidate["ai_evidence_contexts"]
    （evidence.py:669-674）——合成 code_window 上下文使 path#window 引用
    可回查（否则 slice_refs 恢复通道查不到合成窗口 ID，全量 CONTEXT_ID_NOT_FOUND）。
    """

    return [
        {"context_id": ref["context_id"], "kind": "code_window", "path": ref["path"],
         "start_line": ref["line"], "end_line": ref["end_line"]}
        for ref in analysis.get("evidence_refs") or []
        if isinstance(ref, Mapping) and ref.get("context_id")
    ]
