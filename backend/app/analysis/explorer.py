"""探索 Agent 驱动循环（T2.5b/T2.8，方案 §2.4）。

ExplorerOrchestrator 是循环驱动者（模型不自循环）：每轮构造 ExplorerInput
→ AI 协议执行（analyzer.explore_entry，经 ai_call 回调注入 run 级 AI 预算
计费）→ 解析 ExplorerObservation → read_requests 本地执行（CallTreeService）
→ 上下文累积 → loop.done 或预算终止。每轮输入摘要与输出落盘可审计
（run_dir/explorer/observations.json）；候选落盘 candidates.json。

T2.8 增补：deep_dive_partials 对 partially_validated 候选执行深挖
（DeepDiveInput/DeepDiveOutput 协议）——补齐可回查证据与事实判定，
不改写链（hops/validation 不变，M2 验收 4.3-5.4）、不自动升级档位（D1）、
不进 funnel 主链（L2 复核独立裁决不受影响）。

设计：docs/analysis/2026-08-22-t2-5b-implementation-plan.md（含评审
R-1~R-10 修订：ai_call 预算回调/circuit_breaking 短路判据/轮输入哈希落盘/
not_found 统一结构/8KB 截断/prompt_version 前缀拼接）；
2026-08-22-t2-8-implementation-plan.md（含评审 R-1~R-9 修订：
reader 参数通道/停滞判定连续两轮/累积去重截断/allow_external_code 门禁/
批次短路/三本账公式）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.analysis.ai_models import (
    DeepDiveInput,
    DeepDiveOutput,
    ExplorerCandidateDeepDive,
    ExplorerInput,
    ExplorerObservation,
)
from app.config import ExplorerSettings

LOGGER = logging.getLogger(__name__)

# 单次读码结果的上下文注入上限（评审 R-1 风险对策：轮间累积膨胀控制）
_MAX_CONTEXT_BYTES_PER_REQUEST = 8 * 1024

# 深挖 code_context 总量上限（schema LongText max 10_000 留余量）
_MAX_DEEP_DIVE_CONTEXT_CHARS = 9500

# 深挖轮数 schema 钳制（ExplorerCandidateDeepDive.rounds max_length=16，评审 R-3）
_MAX_DEEP_DIVE_ROUNDS = 16

# 深挖 evidence 跨轮累积上限（schema max_length=64，评审 R-3）
_MAX_DEEP_DIVE_EVIDENCE = 64

# 入口 kind → ExplorerCandidateComponent.kind（五类枚举映射；binder/webview → other）
_KIND_MAP = {
    "activity": "activity", "service": "service", "provider": "provider",
    "receiver": "receiver", "binder": "other", "webview_bridge": "other",
}


class ExplorerOrchestrator:
    """探索轨循环驱动者（受控检索循环——方案 §2.4）。

    ai_call 回调（async (ExplorerInput) -> dict）由调用方注入：orchestrator
    以预算包装（run 级 max_requests_per_run 检查 + _ai_requests_used 自增，
    评审 R-1——直调 analyzer 会绕过计费）；测试注入 FakeAnalyzer。
    """

    def __init__(
        self,
        ai_call: Callable[[ExplorerInput], Awaitable[dict[str, Any]]],
        call_tree: Any,
        settings: ExplorerSettings,
        run_dir: Path,
        deep_dive_call: Callable[[DeepDiveInput], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._ai_call = ai_call
        self._call_tree = call_tree
        self._settings = settings
        self._run_dir = run_dir
        self._deep_dive_call = deep_dive_call
        self._ai_requests_used = 0
        self._read_requests_used = 0
        self._deep_dive_requests_used = 0

    @property
    def ai_requests_used(self) -> int:
        return self._ai_requests_used

    @property
    def read_requests_used(self) -> int:
        return self._read_requests_used

    @property
    def deep_dive_requests_used(self) -> int:
        """深挖 AI 调用数（复核账本组成部分——T2.8 三本账公式见任务方案 §3.3）。"""
        return self._deep_dive_requests_used

    async def explore_all(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """逐入口探索：method_id 非 None 者为有效起点（no_method 跳过并记录）；
        候选累计达 max_candidates_per_run 即止（剩余入口记 skipped）。"""

        candidates: list[dict[str, Any]] = []
        observations = self._load_observations()
        skipped_short_circuit = False
        for entry in entries:
            if skipped_short_circuit:
                observations["entries"].append({
                    "entry_id": entry.get("entry_id"), "terminated_by": "short_circuited",
                    "rounds": [], "candidate_count": 0,
                })
                continue
            entry_candidates, terminated_by, rounds = await self._explore_entry(entry)
            candidates.extend(entry_candidates)
            observations["entries"].append({
                "entry_id": entry.get("entry_id"),
                "terminated_by": terminated_by,
                "rounds": rounds,
                "candidate_count": len(entry_candidates),
            })
            if terminated_by == "short_circuit":
                skipped_short_circuit = True
            if len(candidates) >= self._settings.max_candidates_per_run:
                break
        self._write_observations(observations)
        self._write_candidates(candidates)
        return candidates

    async def _explore_entry(self, entry: dict[str, Any]) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        """单入口轮循环：返回（候选列表, 终止原因）。

        终止原因：loop_done（模型声明链已形成）/ budget（轮或请求预算耗尽）/
        error（AI 失败——非熔断类）/ short_circuit（熔断类失败——剩余入口跳过）/
        no_method（入口无方法起点）。
        """

        method_id = entry.get("method_id")
        if not method_id:
            return [], "no_method", []

        rounds: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        prompt_version = model = None
        code_context: list[str] = []
        prior_summary: str | None = None
        terminated_by = "budget"

        for round_index in range(1, self._settings.max_rounds_per_entry + 1):
            requests_budget = self._settings.max_requests_per_entry - self._read_requests_used
            model_input = ExplorerInput.model_validate({
                "round_index": round_index,
                "rounds_budget": self._settings.max_rounds_per_entry,
                "requests_budget": max(requests_budget, 0),
                "entry_json": json.dumps(entry, ensure_ascii=False),
                "attack_surface_json": None,  # 由调用方扩展点注入（攻击面上下文）
                "prior_observations": prior_summary,
                "code_context": "\n---\n".join(code_context) if code_context else None,
            })
            result = await self._ai_call(model_input)
            self._ai_requests_used += 1
            status = result.get("status")
            metadata = result.get("metadata") or {}
            prompt_version = f"explorer/{metadata.get('prompt_version') or '1.0.0'}"
            model = metadata.get("model") or "unknown"
            if status != "completed":
                # 评审 R-4：短路判据 = circuit_breaking（auth/model/transient 熔断类）；
                # skipped（AI 整体不可用）同短路；schema_invalid 单入口问题不短路
                terminated_by = (
                    "short_circuit"
                    if result.get("circuit_breaking") or status == "skipped"
                    else "error"
                )
                rounds.append({
                    "round_index": round_index,
                    "model_input_hash": _input_hash(model_input),
                    "prompt_version": prompt_version,
                    "model": model,
                    "status": status,
                    "observation": None,
                    "requests_executed": [],
                })
                return self._to_candidates(entry, proposals, prompt_version, model), terminated_by, rounds

            try:
                observation = ExplorerObservation.model_validate(result.get("analysis") or {})
            except Exception:
                LOGGER.exception("ExplorerObservation 解析失败", extra={"entry_id": entry.get("entry_id")})
                rounds.append({
                    "round_index": round_index, "model_input_hash": _input_hash(model_input),
                    "prompt_version": prompt_version, "model": model,
                    "status": "observation_invalid", "observation": None, "requests_executed": [],
                })
                return self._to_candidates(entry, proposals, prompt_version, model), "error", rounds

            proposals.extend(observation.chain_proposals and [p.model_dump(mode="json") for p in observation.chain_proposals] or [])
            executed = self._execute_read_requests(observation, max(requests_budget, 0))
            # 评审 R-4（T2.8）：explorer.allow_external_code=False 时不外发读回
            # 内容（合规门禁；读码仍执行留审计轨迹，模型轮输入可见预算扣减）
            if self._settings.allow_external_code:
                code_context.extend(executed["texts"])
            prior_summary = observation.component_summary.summary
            rounds.append({
                "round_index": round_index,
                "model_input_hash": _input_hash(model_input),
                "prompt_version": prompt_version,
                "model": model,
                "status": "completed",
                "observation": observation.model_dump(mode="json"),
                "requests_executed": executed["records"],
            })
            if observation.loop.done:
                terminated_by = "loop_done"
                break

        return (
            self._to_candidates(entry, proposals, prompt_version or "explorer/1.0.0", model or "unknown"),
            terminated_by,
            rounds,
        )

    # ------------------------------------------------------------------
    # deep_dive 深挖（T2.8，方案 §2.4/评审决断 1：补齐事实，非裁决）
    # ------------------------------------------------------------------

    async def deep_dive_partials(self, candidates: list[dict[str, Any]], reader: Any) -> dict[str, int]:
        """对 partially_validated 候选执行深挖（原地写 candidate["deep_dive"]）。

        reader 为索引只读句柄（评审 R-1：证据回查需 files 表——对齐
        validate_explorer_candidates 参数先例）。铁律：不改写 chain_proposal
        与 validation（M2 验收 4.3-5.4）；产物不进 funnel（D1 不自动升级）。
        deep_dive_call 未注入或批次熔断 → 剩余候选批量 skipped（评审 R-5）。
        """

        counts = {
            "partial_total": 0, "attempted": 0, "completed": 0, "incomplete": 0,
            "failed": 0, "skipped": 0, "requests_used": 0,
            "unverifiable_evidence_dropped": 0,
        }
        short_circuit = self._deep_dive_call is None
        for candidate in candidates:
            validation = candidate.get("validation") or {}
            if validation.get("status") != "partially_validated":
                continue
            counts["partial_total"] += 1
            if short_circuit:
                self._write_dive_skipped(candidate)
                counts["skipped"] += 1
                continue
            try:
                outcome = await self._deep_dive_one(candidate, reader)
            except Exception:
                LOGGER.exception(
                    "深挖执行异常（单候选降级 failed）",
                    extra={"candidate_id": candidate.get("candidate_id")},
                )
                self._write_dive_terminal(candidate, "failed")
                counts["failed"] += 1
                continue
            counts[outcome["status"]] += 1
            counts["unverifiable_evidence_dropped"] += outcome["unverifiable"]
            if outcome["circuit_breaking"]:
                # 评审 R-5：熔断类失败短路剩余候选（对齐 explore_all 语义）
                short_circuit = True
        counts["requests_used"] = self._deep_dive_requests_used
        counts["attempted"] = self._deep_dive_requests_used > 0
        return counts

    async def _deep_dive_one(self, candidate: dict[str, Any], reader: Any) -> dict[str, Any]:
        """单候选深挖轮循环（评审 R-2/R-3/R-9 修订后的 §3.3 伪码落地）。"""

        proposal = candidate.get("chain_proposal") or {}
        hops = proposal.get("hops") or []
        if not isinstance(hops, list) or not hops:
            self._write_dive_skipped(candidate)
            return {"status": "skipped", "circuit_breaking": False, "unverifiable": 0}

        missing_facts, facts_truncated = self._missing_facts(candidate)
        # 评审 R-9：初始证据池 = chain_proposal.evidence_refs 经回查过滤的存活项
        evidence_pool, unverifiable = self._filter_evidence(
            proposal.get("evidence_refs") or [], reader
        )
        evidence_keys = {_evidence_key(ref) for ref in evidence_pool}
        resolved: dict[int, dict[str, Any]] = {}
        remaining_gaps: list[str] = []
        rounds: list[dict[str, Any]] = []
        context_keys: set[Any] = set()
        context_segments: list[str] = []
        prompt_version, model_name = "explorer-deep-dive/1.0.0", "unknown"
        status = "incomplete"
        stagnant_rounds = 0
        evidence_truncated = 0

        max_rounds = min(self._settings.max_requests_per_candidate, _MAX_DEEP_DIVE_ROUNDS)
        for round_index in range(1, max_rounds + 1):
            code_context = self._deep_dive_context(
                candidate, reader, evidence_pool, context_keys, context_segments
            )
            try:
                model_input = DeepDiveInput.model_validate({
                    "candidate_id": candidate.get("candidate_id"),
                    "chain_proposal": proposal,
                    "missing_facts": missing_facts,
                    "existing_evidence_refs": evidence_pool,
                    "code_context": code_context,
                })
            except ValidationError:
                LOGGER.warning(
                    "DeepDiveInput 构造失败（候选畸形）",
                    extra={"candidate_id": candidate.get("candidate_id")},
                )
                self._write_dive_terminal(candidate, "failed")
                return {"status": "failed", "circuit_breaking": False, "unverifiable": unverifiable}

            result = await self._deep_dive_call(model_input)
            self._deep_dive_requests_used += 1
            metadata = result.get("metadata") or {}
            prompt_version = f"explorer-deep-dive/{metadata.get('prompt_version') or '1.0.0'}"
            model_name = metadata.get("model") or "unknown"
            input_hash = _input_hash(model_input)
            if result.get("status") != "completed":
                circuit = bool(result.get("circuit_breaking")) or result.get("status") == "skipped"
                rounds.append({
                    "round_index": round_index, "model_input_hash": input_hash,
                    "prompt_version": prompt_version, "model": model_name,
                    "status": "skipped" if circuit else "error", "output": None,
                })
                self._write_dive_terminal(
                    candidate, "skipped" if circuit else "failed",
                    prompt_version=prompt_version, model=model_name,
                    requests_used=len(rounds), rounds=rounds,
                    unverifiable=unverifiable,
                )
                return {"status": "skipped" if circuit else "failed",
                        "circuit_breaking": circuit, "unverifiable": unverifiable}

            try:
                output = DeepDiveOutput.model_validate(result.get("analysis") or {})
            except ValidationError:
                LOGGER.warning(
                    "DeepDiveOutput 解析失败（repair 兜底后仍失败）",
                    extra={"candidate_id": candidate.get("candidate_id")},
                )
                rounds.append({
                    "round_index": round_index, "model_input_hash": input_hash,
                    "prompt_version": prompt_version, "model": model_name,
                    "status": "output_invalid", "output": None,
                })
                self._write_dive_terminal(
                    candidate, "failed", prompt_version=prompt_version, model=model_name,
                    requests_used=len(rounds), rounds=rounds, unverifiable=unverifiable,
                )
                return {"status": "failed", "circuit_breaking": False, "unverifiable": unverifiable}

            # 证据回查过滤（评审 R-3：跨轮去重 + 上界截断计数）
            new_refs, dropped = self._filter_evidence(
                [ref.model_dump(mode="json") for ref in output.evidence_refs], reader
            )
            unverifiable += dropped
            for ref in new_refs:
                key = _evidence_key(ref)
                if key in evidence_keys:
                    continue
                if len(evidence_pool) >= _MAX_DEEP_DIVE_EVIDENCE:
                    evidence_truncated += 1
                    continue
                evidence_keys.add(key)
                evidence_pool.append(ref)

            # 事实判定合并：后轮同 claim_index 覆盖前轮（模型可修正判定）
            progressed = False
            for fact in output.resolved_facts:
                fact_dict = fact.model_dump(mode="json")
                fact_refs, fact_dropped = self._filter_evidence(fact_dict.get("evidence") or [], reader)
                unverifiable += fact_dropped
                fact_dict["evidence"] = fact_refs
                prior = resolved.get(fact.claim_index)
                resolved[fact.claim_index] = fact_dict
                if fact.conclusion != "still_unknown" and (
                    prior is None or prior.get("conclusion") != fact.conclusion
                ):
                    progressed = True

            rounds.append({
                "round_index": round_index, "model_input_hash": input_hash,
                "prompt_version": prompt_version, "model": model_name,
                "status": "completed", "output": output.model_dump(mode="json"),
            })
            remaining_gaps = [gap for gap in output.remaining_gaps]
            if output.analysis_complete:
                status = "completed"
                break
            # 评审 R-2：停滞 = round_index≥2 且连续两轮无新增 decided 判定
            stagnant_rounds = 0 if progressed else stagnant_rounds + 1
            if round_index >= 2 and stagnant_rounds >= 2:
                status = "incomplete"
                break

        final_gaps = remaining_gaps
        if facts_truncated:
            final_gaps = ["缺失事实清单超过 32 项被截断（深挖输入不完整）"] + final_gaps
        try:
            dive = ExplorerCandidateDeepDive.model_validate({
                "status": status,
                "prompt_version": prompt_version,
                "model": model_name,
                "requests_used": len(rounds),
                "resolved_facts": list(resolved.values()),
                "evidence_refs": evidence_pool,
                "remaining_gaps": final_gaps[:32],
                "unverifiable_evidence_count": unverifiable,
                "evidence_truncated_count": evidence_truncated,
                "rounds": rounds,
            })
            candidate["deep_dive"] = dive.model_dump(mode="json")
        except Exception:
            LOGGER.exception(
                "深挖结果模型组装失败（降级 failed）",
                extra={"candidate_id": candidate.get("candidate_id")},
            )
            self._write_dive_terminal(candidate, "failed")
            return {"status": "failed", "circuit_breaking": False, "unverifiable": unverifiable}
        return {"status": status, "circuit_breaking": False, "unverifiable": unverifiable}

    def _write_dive_terminal(
        self, candidate: dict[str, Any], status: str,
        *, prompt_version: str = "explorer-deep-dive/1.0.0", model: str = "unknown",
        requests_used: int = 0, rounds: list[dict[str, Any]] | None = None,
        unverifiable: int = 0,
    ) -> None:
        """终态写入（failed/skipped——无聚合产出）。"""

        try:
            dive = ExplorerCandidateDeepDive.model_validate({
                "status": status, "prompt_version": prompt_version, "model": model,
                "requests_used": requests_used, "resolved_facts": [],
                "evidence_refs": [], "remaining_gaps": [],
                "unverifiable_evidence_count": unverifiable,
                "rounds": rounds or [],
            })
            candidate["deep_dive"] = dive.model_dump(mode="json")
        except ValidationError:
            candidate["deep_dive"] = {
                "status": status, "prompt_version": prompt_version, "model": model,
                "requests_used": requests_used, "resolved_facts": [],
                "evidence_refs": [], "remaining_gaps": [],
                "unverifiable_evidence_count": unverifiable,
                "evidence_truncated_count": 0, "rounds": rounds or [],
            }

    def _write_dive_skipped(self, candidate: dict[str, Any]) -> None:
        """skipped 终态（未注入回调 / 批次短路 / 无可锚定链事实）。"""

        self._write_dive_terminal(candidate, "skipped")

    def _missing_facts(self, candidate: dict[str, Any]) -> tuple[list[str], bool]:
        """缺失事实清单（确定性生成，§3.3.1）；返回 (facts, 是否截断)。"""

        validation = candidate.get("validation") or {}
        proposal = candidate.get("chain_proposal") or {}
        hops = proposal.get("hops") or []
        facts: list[str] = []
        for index in validation.get("failed_hop_indices") or []:
            if not isinstance(index, int) or not (0 <= index < len(hops)):
                continue
            hop = hops[index]
            facts.append(
                f"第 {index} 跳调用关系待证实：{hop.get('from_method_id')} 第 "
                f"{hop.get('call_site_line')} 行未命中指向 {hop.get('to_method_id')} 的 resolved 调用边"
            )
        if validation.get("blocked_by_guard"):
            facts.append("入口可达性：该入口在 release 包是否被 debuggable guard 阻断")
        truncated = len(facts) > 32
        return facts[:32], truncated

    def _deep_dive_context(
        self, candidate: dict[str, Any], reader: Any,
        evidence_pool: list[dict[str, Any]],
        context_keys: set[Any], context_segments: list[str],
    ) -> str | None:
        """深挖 code_context 确定性组装（§3.3.2，素材跨轮累积去重）。

        素材序：① 失败跳 from/to 方法体（call_tree）② evidence 行窗口
        （files 表 ±40 行切片）③ 末跳 to 方法 callees（第 2 轮起一次性）。
        评审 R-4：allow_external_code=False → None（不外发代码片段）。
        """

        if not self._settings.allow_external_code:
            return None
        proposal = candidate.get("chain_proposal") or {}
        hops = proposal.get("hops") or []
        validation = candidate.get("validation") or {}

        def _append(key: Any, payload: dict[str, Any] | None) -> None:
            if payload is None or key in context_keys:
                return
            serialized = json.dumps(payload, ensure_ascii=False)
            if len(serialized) > _MAX_CONTEXT_BYTES_PER_REQUEST:
                serialized = serialized[:_MAX_CONTEXT_BYTES_PER_REQUEST] + '…", "truncated": true}'
            if sum(len(segment) for segment in context_segments) + len(serialized) > _MAX_DEEP_DIVE_CONTEXT_CHARS:
                return
            context_keys.add(key)
            context_segments.append(serialized)

        for index in validation.get("failed_hop_indices") or []:
            if not isinstance(index, int) or not (0 <= index < len(hops)):
                continue
            hop = hops[index]
            for method_id in (hop.get("from_method_id"), hop.get("to_method_id")):
                if method_id:
                    _append(method_id, self._get_method_body_safe(method_id))

        for ref in evidence_pool:
            path, line = ref.get("path"), ref.get("line")
            if isinstance(line, int):
                _append((path, line), self._file_window(reader, str(path), line))

        if len(context_segments) > 0 and hops:
            # 第 2 轮起一次性补充末跳 callees（首轮 context 非空即已过第 1 轮）
            last_to = hops[-1].get("to_method_id")
            if last_to and f"callees:{last_to}" not in context_keys:
                _append(f"callees:{last_to}", self._get_callees_safe(last_to))

        return "\n---\n".join(context_segments) or None

    def _get_method_body_safe(self, method_id: str) -> dict[str, Any] | None:
        try:
            return self._call_tree.get_method_body(method_id)
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            return None

    def _get_callees_safe(self, method_id: str) -> dict[str, Any] | None:
        try:
            return {"method_id": method_id, "callees": self._call_tree.get_callees(method_id)}
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            return None

    def _file_window(self, reader: Any, path: str, line: int) -> dict[str, Any] | None:
        """files 表行切片窗口（委托模块级公共函数）。"""

        return file_window(reader, path, line)

    def _filter_evidence(
        self, refs: list[Any], reader: Any
    ) -> tuple[list[dict[str, Any]], int]:
        """证据回查过滤（委托模块级公共函数——T2.11 单一实现防漂移）。"""

        return filter_evidence(refs, reader)

    # ------------------------------------------------------------------
    # read_requests 执行（本地检索——模型不自循环）
    # ------------------------------------------------------------------

    def _execute_read_requests(self, observation: ExplorerObservation, requests_budget: int) -> dict[str, Any]:
        """执行本轮读码请求（限额 = min(剩余预算, 8)；未命中统一 not_found 结构）"""

        records: list[dict[str, Any]] = []
        texts: list[str] = []
        for request in observation.read_requests[: max(min(requests_budget, len(observation.read_requests)), 0)]:
            payload = self._dispatch_read(request.operation, request.target, request.path, request.line)
            self._read_requests_used += 1
            records.append({"operation": request.operation, "target": request.target})
            serialized = json.dumps(payload, ensure_ascii=False)
            if len(serialized) > _MAX_CONTEXT_BYTES_PER_REQUEST:
                serialized = serialized[:_MAX_CONTEXT_BYTES_PER_REQUEST] + '…", "truncated": true}'
            texts.append(serialized)
        return {"records": records, "texts": texts}

    def _dispatch_read(self, operation: str, target: str, path: str | None, line: int | None) -> dict[str, Any]:
        return dispatch_read(self._call_tree, operation, target, path, line)

    # ------------------------------------------------------------------
    # 候选转换（T0.1 ExplorerCandidate schema）
    # ------------------------------------------------------------------

    def _to_candidates(
        self, entry: dict[str, Any], proposals: list[dict[str, Any]],
        prompt_version: str, model: str,
    ) -> list[dict[str, Any]]:
        component = self._component_projection(entry)
        candidates = []
        for proposal in proposals:
            candidates.append({
                "schema_version": "1.0.0",
                "candidate_id": f"expl_{uuid.uuid4().hex[:20]}",
                "source": "explorer_agent",
                "prompt_version": prompt_version,
                "model": model,
                "component": component,
                "api_entry_ref": entry.get("entry_id") or "unknown",
                "chain_proposal": proposal,
                "validation": None,  # T2.6 三档校验填充占位
            })
        return candidates

    def _component_projection(self, entry: dict[str, Any]) -> dict[str, Any]:
        """入口条目 → ExplorerCandidateComponent（评审 R-10：exported True
        ≠已证实导出——可能承袭攻击面高估；事实缺失兜底 False 保守）。"""

        kind = _KIND_MAP.get(str(entry.get("kind") or ""), "other")
        exported = entry.get("exported")
        return {
            "kind": kind,
            "name": str(entry.get("component_name") or "unknown"),
            "exported": bool(exported) if exported is not None else False,
            "entry_method": str(entry.get("entry_method") or "unknown"),
        }

    # ------------------------------------------------------------------
    # 落盘（observations.json / candidates.json）
    # ------------------------------------------------------------------

    def _load_observations(self) -> dict[str, Any]:
        path = self._run_dir / "explorer" / "observations.json"
        if path.is_file():
            try:
                payload = json.loads(path.read_text("utf-8"))
                if isinstance(payload.get("entries"), list):
                    return payload
            except (json.JSONDecodeError, OSError):
                LOGGER.warning("observations.json 损坏，重新初始化")
        return {"entries": []}

    def _write_observations(self, observations: dict[str, Any]) -> None:
        path = self._run_dir / "explorer" / "observations.json"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(observations, ensure_ascii=False, indent=2), "utf-8")
        path.chmod(0o600)

    def save_candidates(self, candidates: list[dict[str, Any]]) -> None:
        """候选落盘（公有：T2.6 校验后重写调用——评审 R-8 包装私有写盘）。"""
        self._write_candidates(candidates)

    def _write_candidates(self, candidates: list[dict[str, Any]]) -> None:
        path = self._run_dir / "explorer" / "candidates.json"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), "utf-8")
        path.chmod(0o600)


def _input_hash(model_input: ExplorerInput) -> str:
    """轮输入哈希（评审 R-5：输入可审计复现的最小载体——全量输入经 hash 锚定）。"""

    return hashlib.sha256(
        json.dumps(model_input.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _evidence_key(ref: dict[str, Any]) -> tuple[Any, ...]:
    """证据去重键（评审 R-3：跨轮累积按 (path, line, end_line) 去重）。"""

    return (ref.get("path"), ref.get("line"), ref.get("end_line"))


def filter_evidence(refs: list[Any], reader: Any) -> tuple[list[dict[str, Any]], int]:
    """证据回查过滤（文件存在 + 行界；不可回查丢弃并计数）。

    T2.11 提升为模块级公共函数：ExplorerOrchestrator（深挖）与 VerifyAgent
    （核验）共用（单一实现防漂移）。
    """

    kept: list[dict[str, Any]] = []
    dropped = 0
    for ref in refs:
        if isinstance(ref, dict) and evidence_verifiable(ref, reader):
            kept.append(ref)
        else:
            dropped += 1
    return kept, dropped


def evidence_verifiable(ref: dict[str, Any], reader: Any) -> bool:
    """单条证据可回查性（文件存在 + 行界；无行号仅查文件存在）。"""

    path = str(ref.get("path") or "")
    line, end = ref.get("line"), ref.get("end_line")
    if not path:
        return False
    content = file_content(reader, path)
    if content is None:
        return False
    if not isinstance(line, int):
        return True
    total = len(content.splitlines())
    end_line = end if isinstance(end, int) else line
    return 1 <= line <= total and end_line <= total and line <= end_line


def file_content(reader: Any, path: str) -> str | None:
    """按 path 取文件内容（回查尝试原样与剥离 sources/ 前缀两种形态）。"""

    for candidate_path in (path, path.removeprefix("sources/")):
        try:
            row = reader.db.execute(
                "SELECT content FROM files WHERE path = ?", (candidate_path,)
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is not None:
            return str(row["content"] or "")
    return None


def file_window(reader: Any, path: str, line: int) -> dict[str, Any] | None:
    """files 表行切片窗口（±40 行——get_method_body 不支持按行定位）。"""

    content = file_content(reader, path)
    if content is None:
        return None
    lines = content.splitlines()
    if not (1 <= line <= len(lines)):
        return None
    start, end = max(line - 41, 0), min(line + 40, len(lines))
    return {"path": path, "lines": [start + 1, end], "content": "\n".join(lines[start:end])}


def dispatch_read(
    call_tree: Any, operation: str, target: str, path: str | None, line: int | None
) -> dict[str, Any]:
    """结构化读码分发（四种操作；未命中/异常统一 not_found 结构）。

    T2.11 提升为模块级公共函数：ExplorerOrchestrator 与 VerifyAgent 共用
    （单一实现防漂移）。call_tree 为 duck-type（CallTreeService 四操作）。
    """

    try:
        if operation == "get_method_body":
            result = call_tree.get_method_body(target)
            return result if result is not None else {"not_found": target}
        if operation == "get_callees":
            return call_tree.get_callees(target)
        if operation == "get_callers":
            return call_tree.get_callers(target)
        if operation == "search_symbol":
            return {"results": call_tree.search_symbol(target)}
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        LOGGER.warning("读码请求执行失败", extra={"operation": operation, "target": target})
    return {"not_found": target}
