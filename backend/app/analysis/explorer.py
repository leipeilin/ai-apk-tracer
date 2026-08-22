"""探索 Agent 驱动循环（T2.5b，方案 §2.4）。

ExplorerOrchestrator 是循环驱动者（模型不自循环）：每轮构造 ExplorerInput
→ AI 协议执行（analyzer.explore_entry，经 ai_call 回调注入 run 级 AI 预算
计费）→ 解析 ExplorerObservation → read_requests 本地执行（CallTreeService）
→ 上下文累积 → loop.done 或预算终止。每轮输入摘要与输出落盘可审计
（run_dir/explorer/observations.json）；候选落盘 candidates.json。

设计：docs/analysis/2026-08-22-t2-5b-implementation-plan.md（含评审
R-1~R-10 修订：ai_call 预算回调/circuit_breaking 短路判据/轮输入哈希落盘/
not_found 统一结构/8KB 截断/prompt_version 前缀拼接）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.analysis.ai_models import ExplorerInput, ExplorerObservation
from app.config import ExplorerSettings

LOGGER = logging.getLogger(__name__)

# 单次读码结果的上下文注入上限（评审 R-1 风险对策：轮间累积膨胀控制）
_MAX_CONTEXT_BYTES_PER_REQUEST = 8 * 1024

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
    ) -> None:
        self._ai_call = ai_call
        self._call_tree = call_tree
        self._settings = settings
        self._run_dir = run_dir
        self._ai_requests_used = 0
        self._read_requests_used = 0

    @property
    def ai_requests_used(self) -> int:
        return self._ai_requests_used

    @property
    def read_requests_used(self) -> int:
        return self._read_requests_used

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
        try:
            if operation == "get_method_body":
                result = self._call_tree.get_method_body(target)
                return result if result is not None else {"not_found": target}
            if operation == "get_callees":
                return self._call_tree.get_callees(target)
            if operation == "get_callers":
                return self._call_tree.get_callers(target)
            if operation == "search_symbol":
                return {"results": self._call_tree.search_symbol(target)}
        except Exception:  # noqa: BLE001 - 检索失败按未命中处理（循环不挂）
            LOGGER.warning("读码请求执行失败", extra={"operation": operation, "target": target})
        return {"not_found": target}

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
