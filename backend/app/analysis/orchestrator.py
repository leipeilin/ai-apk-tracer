"""编排 APK 扫描阶段，并串联反编译、规则、AI 与证据聚合。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.analysis.ai import OpenAICompatibleAnalyzer
from app.analysis.ai_runtime import AIRuntime
from app.analysis.ai_scheduler import IndexedJob, JobStatus, TaskCircuit, run_indexed_jobs
from app.analysis.ai_trace import AITraceStore, candidate_input_key
from app.analysis.candidate_funnel import CandidateFunnel, propagate_representative_analysis
from app.analysis.context_budget import ContextBudgeter
from app.analysis.context_builder import (
    ContextBuilder,
    finding_slice_sink_mismatch,
    slice_unavailable_is_defect,
)
from app.analysis.coverage import finalize_run_coverage
from app.analysis.decompiler import JadxAdapter
from app.analysis.guard_verifier import apply_guard_verification
from app.analysis.indexer import build_code_index
from app.analysis.index_store import SQLiteCodeIndexReader
from app.analysis.manifest import parse_manifest
from app.analysis.manifest_extractor import extract_decoded_manifest
from app.analysis.rule_runner import RuleRunner
from app.config import WORKSPACE_ROOT, Settings
from app.findings.aggregate import aggregate_candidates
from app.findings.decision import DecisionEngine
from app.findings.evidence import summarize_evidence_integrity, verify_candidate
from app.runs.storage import RunStorage
from app.shared.errors import AppError
from app.shared.repository import SQLiteRepository, scope_finding_id

logger = logging.getLogger(__name__)


class ScanOrchestrator:
    """按固定阶段推进单次扫描，并同步数据库与任务清单状态。"""

    def __init__(
        self,
        settings: Settings,
        repository: SQLiteRepository,
        storage: RunStorage,
        ai_runtime: AIRuntime | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.storage = storage
        self.decompiler = JadxAdapter(
            settings.source_analysis.jadx_path,
            settings.source_analysis.decompile_timeout_seconds,
        )
        self.rule_runner = RuleRunner(WORKSPACE_ROOT / "rules", settings.rule_runtime)
        self.ai_runtime = ai_runtime or AIRuntime(settings.ai)
        self._owns_ai_runtime = ai_runtime is None
        self.ai = self.ai_runtime.create_analyzer(
            cache_dir=storage.shared_ai_cache_dir(),
            max_output_tokens=settings.context_budget.max_output_tokens,
            budget_policy=settings.context_budget.model_dump(mode="json"),
        )
        self.context_budgeter = ContextBudgeter(settings.context_budget)
        self._ai_requests_used = 0
        self._ai_budget_lock = asyncio.Lock()
        self._context_extend_lock = asyncio.Lock()

    async def scan(self, run_id: str) -> None:
        """执行一次扫描，并将未处理异常收敛为失败状态与审计记录。"""

        try:
            await self._run(run_id)
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "SCAN_UNEXPECTED_ERROR"
            message = exc.message if isinstance(exc, AppError) else "扫描发生内部错误"
            logger.exception("扫描失败", extra={"run_id": run_id, "error_code": code})
            self.repository.update_run(run_id, status="failed", stage="failed", error_code=code, error_message=message)
            try:
                self._record_stage(run_id, "failed", "failed", {"error_code": code, "message": message})
            except Exception:
                logger.exception("写入失败状态时出错", extra={"run_id": run_id})
        finally:
            if self._owns_ai_runtime:
                await self.ai_runtime.aclose()

    async def _run(self, run_id: str) -> None:
        """依次完成反编译、规则预扫、切片、AI、证据校验与聚合阶段。"""

        run_dir = self.storage.run_dir(run_id)
        apk_path = run_dir / "input" / "app.apk"
        # 阶段状态先落数据库和清单，再执行对应工作，保证失败时仍可定位扫描进度。
        self._stage(run_id, "basic_check")
        self._record_stage(run_id, "basic_check", "completed", {"apk_validated": True})

        run = self.repository.get_run(run_id)
        coverage_gaps: list[dict[str, Any]] = []
        source_enabled = run.get("config", {}).get("source_analysis", {}).get(
            "enabled", self.settings.source_analysis.enabled
        )
        if source_enabled:
            self._stage(run_id, "decompiling")
            artifact = await self.decompiler.decompile(apk_path, run_dir / "decompile")
            manifest_path = run_dir / "decompile" / artifact["manifest_path"]
            coverage_gaps.extend(artifact.get("coverage_gaps", []))
            run_manifest = self.storage.read_manifest(run_id)
            run_manifest.setdefault("artifacts", []).append({
                "type": "decompile",
                "adapter": artifact["adapter"],
                "adapter_version": artifact["adapter_version"],
                "status": artifact["status"],
                "exit_code": artifact["exit_code"],
                "error_count": artifact.get("error_count"),
                "source_file_count": artifact.get("source_file_count", 0),
                "file_count": len(artifact["files"]),
                "manifest_path": artifact["manifest_path"],
                "artifact_manifest_path": "decompile/artifact-manifest.json",
                "diagnostics": artifact.get("diagnostics", {}),
            })
            self.storage.write_manifest(run_id, run_manifest)
            self._record_stage(run_id, "decompiling", artifact["status"], {
                "file_count": len(artifact["files"]),
                "source_file_count": artifact.get("source_file_count", 0),
                "exit_code": artifact["exit_code"],
                "error_count": artifact.get("error_count"),
                "coverage_gaps": artifact.get("coverage_gaps", []),
            })
            source_root = run_dir / "decompile" / "sources"
            if not source_root.exists():
                source_root = run_dir / "decompile"
        else:
            self._record_stage(run_id, "decompiling", "skipped", {"reason": "source_analysis.enabled=false"})
            manifest_path = await extract_decoded_manifest(apk_path, run_dir / "index" / "AndroidManifest.xml")
            source_root = run_dir / "decompile"

        manifest = parse_manifest(manifest_path, self.settings.analysis_platform_api)
        (run_dir / "index" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
        code_index = build_code_index(
            source_root,
            run_dir / "index" / "code-index.json",
            self.settings.source_analysis.max_file_size_kb,
            self.settings.source_analysis.component_max_file_size_kb,
            {
                str(component.get("name"))
                for component in manifest.get("components", [])
                if component.get("name")
            },
        ) if source_enabled else {"schema_version": "1.0.0", "files": [], "content_sha256": None}

        self._stage(run_id, "rule_prescan")
        candidates, failures = await asyncio.to_thread(
            self.rule_runner.run_all,
            run_dir,
            {
                "manifest": manifest,
                "index": {
                    **code_index,
                    "allowed_index_root": (run_dir / "index").resolve().as_posix(),
                } if source_enabled else None,
                "config": self._run_config(),
            },
        )
        rule_component_gaps = list(self.rule_runner.last_coverage_gaps)
        self._record_stage(
            run_id,
            "rule_prescan",
            "partial" if failures or rule_component_gaps else "completed",
            {
                "candidate_count": len(candidates),
                "rule_failures": failures,
                "component_coverage_gaps": rule_component_gaps,
                "rule_total_count": len(self.rule_runner.discover()),
            },
        )
        for candidate in candidates:
            candidate.setdefault("analysis_status", "rule_only")
            candidate.setdefault("dataflow_status", "not_proven" if candidate.get("evidence_level") == "L2" else "not_applicable")
            candidate.setdefault("authorization_status", "unknown")
            candidate.setdefault("impact_status", "potential")

        # 确定性 guard 验证（v2026-08-09 方案 X'）：funnel 之前对全部候选检测前置
        # guard（当前：debuggable）。命中写 guard_blocked + manifest_facts——funnel
        # 据此不送 AI（源头消除，同 LocalBroadcast effect_verified 模式），decision
        # 据此给 blocked 状态（条件不可利用，非误报）。必须在 funnel 前，否则
        # ai_required 判定看不到 guard 事实（此前 AI 后检测被证实为 no-op）。
        if source_enabled:
            guard_index_path = str(run_dir / "index" / "analysis.sqlite3")
            guard_manifest_facts = {
                "debuggable": manifest.get("debuggable"),
                "target_sdk": manifest.get("target_sdk"),
            }
            for candidate in candidates:
                apply_guard_verification(candidate, guard_index_path, guard_manifest_facts)

        self._stage(run_id, "candidate_funnel")
        funnel_result = CandidateFunnel(self.settings.funnel).process(candidates)
        candidates = funnel_result.candidates
        self._record_stage(run_id, "candidate_funnel", "completed", funnel_result.summary)

        self._stage(run_id, "code_slicing")
        slice_candidate_indexes = [
            index for index in funnel_result.representative_indexes
            if _should_build_slice(candidates[index])
        ]
        context_builder = (
            ContextBuilder(code_index, self.settings.context_budget)
            if source_enabled and slice_candidate_indexes else None
        )
        slices: dict[int, dict[str, Any]] = {}
        if context_builder:
            total_contexts = 0
            for candidate_index in slice_candidate_indexes:
                candidate = candidates[candidate_index]
                slice_document = context_builder.build_initial(candidate)
                slices[candidate_index] = slice_document
                candidate["slice_id"] = slice_document["slice_id"]
                candidate["slice_refs"] = [context["context_id"] for context in slice_document["contexts"]]
                self._write_slice(run_dir, slice_document, 0)
                total_contexts += len(slice_document["contexts"])
            (run_dir / "slices" / "candidates.json").write_text(
                json.dumps(candidates, ensure_ascii=False, indent=2), "utf-8"
            )
            self._record_stage(run_id, "code_slicing", "completed", {
                "slice_count": len(slices),
                "context_count": total_contexts,
                "index_stats": code_index.get("stats", {}),
            })
        elif source_enabled:
            self._record_stage(run_id, "code_slicing", "completed", {
                "slice_count": 0,
                "context_count": 0,
                "reason": "没有需要深度分析的 L2 候选",
                "index_stats": code_index.get("stats", {}),
            })
        else:
            self._record_stage(run_id, "code_slicing", "skipped", {"reason": "source_analysis.enabled=false"})

        self._stage(run_id, "ai_analysis")
        await self._run_ai_stage(
            run_id,
            candidates,
            slice_candidate_indexes,
            slices,
            context_builder,
            source_enabled,
            run_dir,
        )
        propagate_representative_analysis(candidates)

        if context_builder:
            context_builder.close()
        coverage_gaps = _finalize_run_coverage(
            candidates,
            coverage_gaps,
            failures,
            code_index,
            rule_component_gaps,
        )
        self._stage(run_id, "evidence_integrity_validation")
        evidence_reader = SQLiteCodeIndexReader(code_index) if source_enabled else None
        try:
            verified = [verify_candidate(candidate, code_index, evidence_reader) for candidate in candidates]
        finally:
            if evidence_reader:
                evidence_reader.close()
        # guard 验证已在 funnel 前统一执行（方案 X'，见 candidate_funnel 阶段前）——
        # guard_blocked 候选不送 AI 且 decision 判 blocked，此处不再重复检测。
        DecisionEngine().apply(verified)
        findings = aggregate_candidates(verified)
        integrity_summary = summarize_evidence_integrity(verified, findings)
        self._record_stage(
            run_id,
            "evidence_integrity_validation",
            "completed",
            integrity_summary,
        )

        self._stage(run_id, "aggregation")
        app = {
            "version_code": manifest["version_code"],
            "version_name": manifest["version_name"],
            "compile_sdk_version": manifest["compile_sdk_version"],
            "compile_sdk_codename": manifest["compile_sdk_codename"],
            "package": manifest["package"],
        }
        finding_slice_mismatches = 0
        for finding in findings:
            finding["app"] = app
            scope_finding_id(run_id, finding)
            # v2026-08-14 产品侧自检：finding.sinks 与其 slice 的 candidate.sinks
            # 不一致时在 blocking_gaps 追加标记（critical gap → severity 自动 pending），
            # 并累计 run 级计数写入 manifest。不拒绝落库——mismatch 仍是人工复核材料，
            # 标记 + 计数已足够暴露。context_slice 在下一行 evidence 落盘时获取。
            context_slice = self._latest_slice(run_dir, finding.get("slice_id"))
            slice_issues = finding_slice_sink_mismatch(finding, context_slice)
            # S5（2026-08-16）：SLICE_UNAVAILABLE 对 L1/rule_only/组员 finding 属
            # 设计形态（l1_skip_ai、代表项携带 slice），不计入 mismatch。
            if (
                slice_issues
                and slice_issues[0]["code"] == "SLICE_UNAVAILABLE"
                and not slice_unavailable_is_defect(finding)
            ):
                slice_issues = []
            if slice_issues:
                finding_slice_mismatches += 1
                existing_codes = {gap.get("code") for gap in finding.get("blocking_gaps", [])}
                finding.setdefault("blocking_gaps", []).extend(
                    issue for issue in slice_issues if issue["code"] not in existing_codes
                )
            path = run_dir / "findings" / f"{finding['id']}.json"
            path.write_text(json.dumps(finding, ensure_ascii=False, indent=2), "utf-8")
            evidence = run_dir / "reports" / "evidence" / f"{finding['id']}.json"
            evidence.write_text(json.dumps({
                "finding": finding,
                "manifest_components": self._relevant_manifest_components(manifest, finding),
                "permission_definitions": self._relevant_permissions(manifest, finding),
                "context_slice": context_slice,
            }, ensure_ascii=False, indent=2), "utf-8")
        self.repository.replace_findings(run_id, findings)
        self._record_stage(run_id, "aggregation", "completed", {
            "finding_count": len(findings),
            "finding_slice_mismatches": finding_slice_mismatches,
        })
        self.repository.update_run(run_id, status="completed", stage="completed")
        self.storage.update_manifest(
            run_id,
            status="completed",
            stage="completed",
            analysis_incomplete=bool(coverage_gaps),
            coverage_gaps=coverage_gaps,
            finding_slice_mismatches=finding_slice_mismatches,
            completed_at=datetime.now(UTC).isoformat(),
        )

    async def _run_ai_stage(
        self,
        run_id: str,
        candidates: list[dict[str, Any]],
        slice_candidate_indexes: list[int],
        slices: dict[int, dict[str, Any]],
        context_builder: ContextBuilder | None,
        source_enabled: bool,
        run_dir: Path,
    ) -> None:
        """执行一次任务级预检、恢复 checkpoint，并有界调度代表候选。

        任务熔断只阻止尚未开始的候选；已运行候选保留实际结果。共享 AI cache 可跨 run
        命中，而 checkpoint 仅在当前 run 内按候选输入身份恢复。只有漏斗代表项发送 AI，
        分析完成后再由三重身份校验传播给同组成员。
        """

        # Include both L1 (for triage) and L2 (for review) candidates
        ai_candidate_indexes = [
            index for index, candidate in enumerate(candidates)
            if index in slice_candidate_indexes
        ]
        if not ai_candidate_indexes or not context_builder:
            skip_reason = (
                "source_analysis.enabled=false" if not source_enabled
                else "没有需要 AI 分析的候选"
            )
            preflight = {
                "status": "skipped",
                "classification": "no_candidates" if ai_candidate_indexes == [] else "no_context",
                "recoverable": False,
                "http_status": None,
                "message": skip_reason,
                "metadata": {"attempts": 0},
            }
            for candidate_index in ai_candidate_indexes:
                _mark_ai_unavailable(candidates[candidate_index], "ai_skipped", skip_reason, preflight)
            self._record_stage(run_id, "ai_analysis", "skipped", {
                "reason": skip_reason,
                "preflight": preflight,
                "circuit_open": False,
                "analyzed": 0,
                "completed": 0,
                "failed": 0,
                "skipped": len(ai_candidate_indexes),
                "incomplete": 0,
            })
            return

        preflight = await self.ai.preflight()
        circuit_open = preflight.get("status") != "passed"
        circuit_reason = preflight.get("message", "AI preflight 未通过") if circuit_open else None
        if circuit_open:
            recoverable = preflight.get("recoverable") is True
            analysis_status = "ai_failed" if recoverable else "ai_skipped"
            # Pipeline v2 只标记实际入选的代表候选；旧调用未带漏斗字段时保持兼容。
            circuit_indexes = (
                ai_candidate_indexes
                if any("is_ai_representative" in candidate for candidate in candidates)
                else [
                    index for index, candidate in enumerate(candidates)
                    if candidate.get("evidence_level") == "L2"
                ]
            )
            for candidate_index in circuit_indexes:
                _mark_ai_unavailable(
                    candidates[candidate_index],
                    analysis_status,
                    circuit_reason,
                    preflight,
                )
            stage_status = "failed" if recoverable else "skipped"
            self._record_stage(run_id, "ai_analysis", stage_status, {
                "reason": circuit_reason,
                "preflight": preflight,
                "circuit_open": True,
                "analyzed": 0,
                "completed": 0,
                "failed": len(circuit_indexes) if recoverable else 0,
                "skipped": 0 if recoverable else len(circuit_indexes),
                "incomplete": 0,
            })
            return

        self._ai_requests_used = 0
        circuit = TaskCircuit()
        if hasattr(self.ai, "set_task_circuit"):
            self.ai.set_task_circuit(circuit)
        trace_store = AITraceStore(run_dir / "ai-trace")
        analyzed_count = 0
        analyzed_lock = asyncio.Lock()

        async def analyze_job(job: IndexedJob[dict[str, Any]]) -> dict[str, Any]:
            nonlocal analyzed_count, circuit_reason
            candidate_index = job.index
            candidate = candidates[candidate_index]
            slice_document = job.value
            if hasattr(self.ai, "checkpoint_identity"):
                analyzer_identity = self.ai.checkpoint_identity(candidate, slice_document)
            else:
                analyzer_identity = {"analyzer": type(self.ai).__name__}
            input_key = candidate_input_key(candidate, slice_document, analyzer_identity)
            restored = trace_store.completed(candidate_index, input_key)
            if restored is not None:
                candidate.clear()
                candidate.update(restored["candidate"])
                return {**restored.get("result", {}), "checkpoint_hit": True}

            async with analyzed_lock:
                analyzed_count += 1
            result = await self._analyze_with_expansion(
                candidate,
                slice_document,
                context_builder,
                run_dir,
                trace_store=trace_store,
                candidate_index=candidate_index,
                input_key=input_key,
            )
            if result.get("circuit_breaking") is True:
                circuit_reason = result.get("message", "AI 不可恢复错误触发任务级断路")
                circuit.open(circuit_reason)
            self._append_ai_trace(
                trace_store,
                candidate,
                candidate_index,
                input_key,
                "candidate_completed",
                {
                    "status": result.get("status"),
                    "stop_reason": result.get("stop_reason"),
                    "cache_hit": any(
                        entry.get("result", {}).get("metadata", {}).get("cache_hit") is True
                        for entry in result.get("trace", [])
                    ),
                    "accepted_response_hashes": [
                        entry.get("result", {}).get("metadata", {}).get("accepted_response_hash")
                        for entry in result.get("trace", [])
                        if entry.get("result", {}).get("metadata", {}).get("accepted_response_hash")
                    ],
                },
            )
            if result.get("status") == "completed":
                trace_store.save_completed(candidate_index, input_key, candidate, result)
            return result

        jobs = [IndexedJob(index, slices[index]) for index in sorted(slices)]
        scheduled = await run_indexed_jobs(
            jobs,
            analyze_job,
            max_concurrency=self.settings.ai.candidate_concurrency,
            circuit=circuit,
            opens_circuit=lambda result: result.get("circuit_breaking") is True,
        )
        ai_results: list[dict[str, Any]] = []
        ai_counts = {"completed": 0, "failed": 0, "skipped": 0, "incomplete": 0}
        for scheduled_result in scheduled.results:
            candidate_index = scheduled_result.index
            candidate = candidates[candidate_index]
            if scheduled_result.status == JobStatus.SKIPPED:
                reason = circuit.reason or "AI 任务级断路已打开"
                _mark_ai_unavailable(candidate, "ai_skipped", reason, preflight)
                result: dict[str, Any] = {
                    "status": "skipped",
                    "reason": reason,
                    "classification": "circuit_open",
                    "recoverable": False,
                    "circuit_breaking": False,
                }
            elif scheduled_result.status == JobStatus.FAILED:
                result = {
                    "status": "failed",
                    "classification": "worker_failure",
                    "recoverable": False,
                    "circuit_breaking": False,
                    "message": "AI candidate worker 执行失败",
                }
                _mark_ai_unavailable(candidate, "ai_failed", result["message"], preflight)
            else:
                result = scheduled_result.value or {"status": "failed"}
            ai_results.append({
                "candidate_index": candidate_index,
                "rule_id": candidate.get("rule_id"),
                **result,
            })
            ai_counts[result["status"]] = ai_counts.get(result["status"], 0) + 1

        legacy_results = run_dir / "ai-cache" / "results.json"
        legacy_results.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        legacy_results.write_text(json.dumps(ai_results, ensure_ascii=False, indent=2), "utf-8")
        legacy_results.chmod(0o600)
        try:
            manifest = self.storage.read_manifest(run_id)
        except Exception:
            manifest = None
        if manifest is not None:
            manifest.setdefault("artifacts", []).append({
                "type": "ai_results",
                "path": "ai-cache/results.json",
                "legacy_trace": True,
                "trace_root": "ai-trace",
            })
            self.storage.write_manifest(run_id, manifest)
        circuit_open = circuit.is_open
        circuit_reason = circuit.reason or circuit_reason
        unsuccessful = ai_counts["failed"] + ai_counts["skipped"] + ai_counts["incomplete"]
        summary = {
            "preflight": preflight,
            "circuit_open": circuit_open,
            "analyzed": analyzed_count,
            "peak_concurrent": scheduled.stats.peak_active,
            **ai_counts,
        }
        if circuit_reason:
            summary["circuit_reason"] = circuit_reason
        self._record_stage(
            run_id,
            "ai_analysis",
            "partial" if unsuccessful else "completed",
            summary,
        )

    async def _analyze_with_expansion(
        self,
        candidate: dict[str, Any],
        initial_slice: dict[str, Any],
        context_builder: ContextBuilder,
        run_dir: Path,
        *,
        trace_store: AITraceStore | None = None,
        candidate_index: int | None = None,
        input_key: str | None = None,
    ) -> dict[str, Any]:
        """按模型的精确上下文请求扩片，并执行统一的候选与任务预算。"""

        max_expansions = self.settings.context_budget.max_expansions_per_candidate
        current_slice = initial_slice
        previous_analysis: dict[str, Any] | None = None
        trace: list[dict[str, Any]] = []
        round_number = 0
        request_count = 0
        started = time.monotonic()
        while True:
            result, analysis_slice, budget_reason = await self._budgeted_ai_call(
                candidate,
                current_slice,
                previous_analysis,
                request_count,
                started,
            )
            if result is None:
                return self._finish_context_budget(candidate, trace, analysis_slice, budget_reason)
            request_count += 1
            trace_entry: dict[str, Any] = {
                "round": round_number,
                "slice_id": current_slice["slice_id"],
                "context_count": len(analysis_slice.get("contexts", [])),
                "slice_budget": analysis_slice.get("budget", {}),
                "result": result,
            }
            trace.append(trace_entry)
            self._append_ai_trace(
                trace_store,
                candidate,
                candidate_index,
                input_key,
                "round",
                trace_entry,
            )
            if result.get("status") != "completed":
                skipped = result.get("status") == "skipped"
                status = "skipped" if skipped else "failed"
                candidate["analysis_status"] = "ai_skipped" if skipped else "ai_failed"
                message = result.get("reason") if skipped else result.get("error", {}).get("message", "AI 分析失败")
                if skipped:
                    candidate["ai_skip_reason"] = message
                candidate.setdefault("ai_blocking_gaps", []).append({
                    "code": "AI_ANALYSIS_SKIPPED" if skipped else "AI_ANALYSIS_FAILED",
                    "critical": not candidate.get("deterministic_chain_verified", False),
                    "message": message,
                })
                candidate["ai_analysis_trace"] = trace
                candidate["ai_stop_reason"] = "ai_skipped" if skipped else "ai_failed"
                return {
                    "status": status,
                    "stop_reason": candidate["ai_stop_reason"],
                    "trace": trace,
                    "classification": result.get("classification"),
                    "recoverable": result.get("recoverable"),
                    "circuit_breaking": result.get("circuit_breaking", False),
                    "message": message,
                }

            analysis = result["analysis"]
            valid_refs, invalid_refs = self._verify_ai_evidence_refs(analysis, analysis_slice)
            analysis["verified_evidence_refs"] = valid_refs
            analysis["invalid_evidence_refs"] = invalid_refs
            trace_entry["verified_evidence_refs"] = valid_refs
            trace_entry["invalid_evidence_refs"] = invalid_refs
            if invalid_refs:
                analysis.setdefault("blocking_gaps", []).append({
                    "code": "AI_EVIDENCE_REF_INVALID",
                    "critical": True,
                    "count": len(invalid_refs),
                })
                analysis["promotion_recommended"] = False

            requests = analysis.get("context_requests", [])
            if analysis.get("analysis_complete"):
                candidate["ai_stop_reason"] = "analysis_complete"
                self._apply_ai_analysis(candidate, analysis, trace, analysis_slice)
                return {
                    "status": "completed",
                    "stop_reason": "analysis_complete",
                    "trace": trace,
                    "final_analysis": analysis,
                }
            if not requests:
                analysis.setdefault("blocking_gaps", []).append({
                    "code": "AI_ANALYSIS_INCOMPLETE",
                    "critical": True,
                    "message": "AI 返回 analysis_complete=false，但未提供可执行的 context_requests",
                })
                analysis["promotion_recommended"] = False
                candidate["ai_stop_reason"] = "analysis_incomplete_no_requests"
                self._apply_ai_analysis(candidate, analysis, trace, analysis_slice, "ai_incomplete")
                return {
                    "status": "incomplete",
                    "stop_reason": "analysis_incomplete_no_requests",
                    "trace": trace,
                    "final_analysis": analysis,
                }

            if round_number >= max_expansions:
                return self._finish_context_budget(
                    candidate, trace, analysis_slice, "expansion_budget_exhausted"
                )

            async with self._context_extend_lock:
                expanded_slice, added_count, request_results = context_builder.extend(
                    current_slice, requests
                )
            trace_entry["context_requests"] = requests
            trace_entry["request_results"] = request_results
            trace_entry["added_context_count"] = added_count
            round_number += 1
            self._write_slice(run_dir, expanded_slice, round_number)

            # 达到扩片预算：执行一次受请求预算约束的最终收尾。
            if round_number >= max_expansions and added_count > 0:
                final_result, final_slice, budget_reason = await self._budgeted_ai_call(
                    candidate,
                    expanded_slice,
                    analysis,
                    request_count,
                    started,
                    finalization=True,
                )
                if final_result is None:
                    return self._finish_context_budget(candidate, trace, final_slice, budget_reason)
                request_count += 1
                final_trace_entry = {
                    "round": round_number,
                    "slice_id": expanded_slice["slice_id"],
                    "context_count": len(final_slice.get("contexts", [])),
                    "slice_budget": final_slice.get("budget", {}),
                    "result": final_result,
                    "max_rounds_finalization": True,
                }
                trace.append(final_trace_entry)
                self._append_ai_trace(
                    trace_store,
                    candidate,
                    candidate_index,
                    input_key,
                    "round",
                    final_trace_entry,
                )
                if final_result.get("status") == "completed":
                    final_analysis = final_result.get("analysis", {})
                    valid_refs, invalid_refs = self._verify_ai_evidence_refs(
                        final_analysis, final_slice
                    )
                    final_analysis["verified_evidence_refs"] = valid_refs
                    final_analysis["invalid_evidence_refs"] = invalid_refs
                    if invalid_refs:
                        final_analysis["promotion_recommended"] = False
                    if final_analysis.get("analysis_complete") is not False:
                        candidate["ai_stop_reason"] = "expansion_budget_finalized"
                        self._apply_ai_analysis(candidate, final_analysis, trace, final_slice)
                        return {
                            "status": "completed",
                            "stop_reason": "expansion_budget_finalized",
                            "trace": trace,
                            "final_analysis": final_analysis,
                        }
                # 收尾失败：标记 incomplete
                analysis.setdefault("blocking_gaps", []).append({
                    "code": "AI_MAX_ROUNDS_REACHED",
                    "critical": True,
                    "message": f"AI 分析达到最大扩片次数 {max_expansions}，收尾未能完成",
                })
                analysis["analysis_complete"] = False
                analysis["promotion_recommended"] = False
                candidate["ai_stop_reason"] = "expansion_budget_exhausted"
                self._apply_ai_analysis(candidate, analysis, trace, final_slice, "ai_incomplete")
                return {
                    "status": "incomplete",
                    "stop_reason": "expansion_budget_exhausted",
                    "trace": trace,
                    "final_analysis": analysis,
                }

            if added_count == 0:
                # Check if all results are already_present (not not_found)
                all_already_present = all(
                    r.get("status") in {"already_present", "duplicate_request", "empty_relation"}
                    for r in request_results
                )
                if all_already_present:
                    # Finalization round: ask AI to conclude based on existing evidence
                    final_result, final_slice, budget_reason = await self._budgeted_ai_call(
                        candidate,
                        expanded_slice,
                        analysis,
                        request_count,
                        started,
                        finalization=True,
                    )
                    if final_result is None:
                        return self._finish_context_budget(candidate, trace, final_slice, budget_reason)
                    request_count += 1
                    final_trace_entry = {
                        "round": round_number,
                        "slice_id": expanded_slice["slice_id"],
                        "context_count": len(final_slice.get("contexts", [])),
                        "slice_budget": final_slice.get("budget", {}),
                        "result": final_result,
                        "finalization_round": True,
                    }
                    trace.append(final_trace_entry)
                    self._append_ai_trace(
                        trace_store,
                        candidate,
                        candidate_index,
                        input_key,
                        "round",
                        final_trace_entry,
                    )
                    if final_result.get("status") == "completed":
                        final_analysis = final_result.get("analysis", {})
                        valid_refs, invalid_refs = self._verify_ai_evidence_refs(
                            final_analysis, final_slice
                        )
                        final_analysis["verified_evidence_refs"] = valid_refs
                        final_analysis["invalid_evidence_refs"] = invalid_refs
                        if invalid_refs:
                            final_analysis["promotion_recommended"] = False
                        if final_analysis.get("analysis_complete") is not False:
                            candidate["ai_stop_reason"] = "expansion_stalled_finalized"
                            self._apply_ai_analysis(candidate, final_analysis, trace, final_slice)
                            return {
                                "status": "completed",
                                "stop_reason": "expansion_stalled_finalized",
                                "trace": trace,
                                "final_analysis": final_analysis,
                            }
                # Stalled: no new contexts and no finalization possible
                analysis.setdefault("blocking_gaps", []).append({
                    "code": "CONTEXT_EXPANSION_STALLED",
                    "critical": True,
                    "message": "模型请求未解析到新的索引上下文，深度分析自然终止",
                })
                analysis["analysis_complete"] = False
                analysis["promotion_recommended"] = False
                candidate["ai_stop_reason"] = "context_expansion_stalled"
                self._apply_ai_analysis(candidate, analysis, trace, expanded_slice, "ai_incomplete")
                return {
                    "status": "incomplete",
                    "stop_reason": "context_expansion_stalled",
                    "trace": trace,
                    "final_analysis": analysis,
                }
            previous_analysis = analysis
            current_slice = expanded_slice

    async def _budgeted_ai_call(
        self,
        candidate: dict[str, Any],
        slice_document: dict[str, Any],
        previous_analysis: dict[str, Any] | None,
        request_count: int,
        started: float,
        *,
        finalization: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], str]:
        """在发送前统一执行体积、候选请求数、任务请求数与墙钟预算。"""

        budget = self.settings.context_budget
        budgeted_slice = self.context_budgeter.trim(slice_document)
        if budgeted_slice.get("budget", {}).get("status") == "cannot_trim_safely":
            return None, budgeted_slice, "cannot_trim_safely"
        if request_count >= budget.max_requests_per_candidate:
            return None, budgeted_slice, "candidate_request_budget_exhausted"
        if time.monotonic() - started >= budget.max_candidate_wall_seconds:
            return None, budgeted_slice, "candidate_wall_time_exhausted"
        async with self._ai_budget_lock:
            if self._ai_requests_used >= budget.max_requests_per_run:
                return None, budgeted_slice, "run_request_budget_exhausted"
            self._ai_requests_used += 1
        if finalization:
            if previous_analysis is None:
                return None, budgeted_slice, "finalization_input_missing"
            result = await self.ai.finalize(candidate, budgeted_slice, previous_analysis)
        else:
            result = await self.ai.analyze(candidate, budgeted_slice, previous_analysis)
        return result, budgeted_slice, ""

    def _finish_context_budget(
        self,
        candidate: dict[str, Any],
        trace: list[dict[str, Any]],
        final_slice: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """将预算耗尽收敛为可审计的 incomplete，而不是静默截断证据。"""

        messages = {
            "cannot_trim_safely": "关键证据无法在输入 token 预算内安全保留",
            "candidate_request_budget_exhausted": "单候选 AI 请求预算已耗尽",
            "run_request_budget_exhausted": "单次扫描 AI 请求预算已耗尽",
            "candidate_wall_time_exhausted": "单候选 AI 分析墙钟预算已耗尽",
            "expansion_budget_exhausted": "单候选上下文扩片预算已耗尽",
        }
        analysis = {
            "analysis_complete": False,
            "blocking_gaps": [{
                "code": "CONTEXT_BUDGET_EXHAUSTED",
                "critical": True,
                "stop_reason": reason,
                "message": messages.get(reason, reason),
            }],
        }
        candidate["ai_stop_reason"] = reason
        self._apply_ai_analysis(candidate, analysis, trace, final_slice, "ai_incomplete")
        return {
            "status": "incomplete",
            "stop_reason": reason,
            "trace": trace,
            "final_analysis": analysis,
        }

    @staticmethod
    def _append_ai_trace(
        trace_store: AITraceStore | None,
        candidate: dict[str, Any],
        candidate_index: int | None,
        input_key: str | None,
        event: str,
        payload: dict[str, Any],
    ) -> None:
        if trace_store is None:
            return
        scope = str(
            candidate.get("scope_key")
            or candidate.get("candidate_id")
            or candidate.get("rule_id")
            or f"candidate-{candidate_index}"
        )
        track = str(
            candidate.get("analysis_track")
            or ("l1_triage" if candidate.get("evidence_level") == "L1" else "l2_review")
        )
        trace_store.append(scope, track, {
            "event": event,
            "candidate_index": candidate_index,
            "input_key": input_key,
            **payload,
        })

    @staticmethod
    def _verify_ai_evidence_refs(
        analysis: dict[str, Any], slice_document: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        contexts = {context["context_id"]: context for context in slice_document.get("contexts", [])}
        valid, invalid = [], []
        for reference in analysis.get("evidence_refs", []):
            context = contexts.get(reference.get("context_id"))
            line = reference.get("line")
            if context is None:
                invalid.append({**reference, "reason": "CONTEXT_ID_NOT_FOUND"})
                continue
            if line is not None:
                try:
                    line_number = int(line)
                except (TypeError, ValueError):
                    invalid.append({**reference, "reason": "LINE_INVALID"})
                    continue
                if not context["start_line"] <= line_number <= context["end_line"]:
                    invalid.append({**reference, "reason": "LINE_OUTSIDE_CONTEXT"})
                    continue
            valid.append({**reference, "path": context["path"], "verification": "fact"})
        return valid, invalid

    @staticmethod
    def _apply_ai_analysis(
        candidate: dict[str, Any],
        analysis: dict[str, Any],
        trace: list[dict[str, Any]],
        final_slice: dict[str, Any],
        analysis_status: str = "ai_completed",
    ) -> None:
        candidate["analysis_status"] = analysis_status
        candidate["ai_analysis"] = analysis
        candidate["ai_analysis_trace"] = trace
        # v2026-08-14：把 AI 运行元数据从 trace 最后成功轮的 result.metadata
        # 提升到 finding 顶层，供前端 AI observation 区块展示（此前 prompt_version/
        # schema_hash/provider/model 全是 None，元数据仅存在于 ai-trace 文件）。
        candidate.update(ScanOrchestrator._ai_runtime_metadata_from_trace(trace))
        candidate["slice_refs"] = [context["context_id"] for context in final_slice.get("contexts", [])]
        candidate["context_requests"] = final_slice.get("request_history", [])
        candidate["confidence_tier"] = analysis.get("confidence_tier", candidate.get("confidence_tier", "medium"))
        candidate["ai_guard_assessment"] = analysis.get("guard_status", "unknown")
        candidate["candidate_verdict"] = analysis.get("candidate_verdict") or analysis.get("verdict")
        candidate["analysis_track"] = analysis.get("analysis_track")
        candidate["ai_blocking_gaps"] = _merge_dict_list(
            candidate.get("ai_blocking_gaps", []), analysis.get("blocking_gaps", [])
        )
        if analysis.get("summary"):
            candidate["description"] = analysis["summary"]
        if analysis.get("promotion_recommended") is True and analysis.get("analysis_complete") is True:
            if candidate.get("evidence_level") == "L2":
                candidate["promotion_requested"] = True
            else:
                candidate["ai_promotion_proposal"] = {
                    "candidate_verdict": candidate.get("candidate_verdict"),
                    "suggested_sources": analysis.get("suggested_sources", []),
                    "suggested_sinks": analysis.get("suggested_sinks", []),
                    "suggested_paths": analysis.get("suggested_paths", []),
                }

    @staticmethod
    def _ai_runtime_metadata_from_trace(trace: list[dict[str, Any]]) -> dict[str, Any]:
        """从 AI 调用 trace 的最后成功轮提取运行时元数据到 finding 顶层。

        v2026-08-14：AI 阶段的 prompt_version/schema_hash/provider/model 等
        元数据此前只存在于 ai-trace 文件（trace[].result.metadata），finding 顶层
        从未写入导致前端 AI observation 区块显示"未记录"。此处从最后一条
        completed 轮的 result.metadata 提升字段。
        """
        for entry in reversed(trace or []):
            result = entry.get("result") or {}
            if result.get("status") != "completed":
                continue
            metadata = result.get("metadata") or {}
            if not metadata:
                continue
            schema = metadata.get("schema_sha256") or {}
            # schema_hash 不在 metadata 顶层（仅存在于 cache descriptor）；
            # 用 canonical json 派生保持前端可读。内联实现避免与 ai 模块深度耦合。
            schema_hash = metadata.get("schema_hash")
            if not schema_hash and schema:
                try:
                    canonical = json.dumps(
                        {k: schema[k] for k in sorted(schema)}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    schema_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                except Exception:
                    schema_hash = None
            return {
                "prompt_id": metadata.get("prompt_id"),
                "prompt_version": metadata.get("prompt_version") or metadata.get("prompt_template_version"),
                "prompt_hash": metadata.get("prompt_template_hash"),
                # output_model_version 不在 metadata 顶层（仅存在于 cache descriptor）
                "ai_schema_version": metadata.get("output_model_version"),
                "schema_hash": schema_hash,
                "input_schema_hash": (
                    schema.get("input") if isinstance(schema, dict) else metadata.get("input_schema_sha256")
                ),
                "output_schema_hash": (
                    schema.get("output") if isinstance(schema, dict) else metadata.get("output_schema_sha256")
                ),
                "provider_kind": metadata.get("provider_kind") or "openai-compatible",
                "provider": metadata.get("base_url_hash"),
                "model": metadata.get("model"),
                "cache_hit": metadata.get("cache_hit"),
                "ai_latency_ms": metadata.get("latency_ms"),
            }
        return {}

    @staticmethod
    def _relevant_manifest_components(manifest: dict[str, Any], finding: dict[str, Any]) -> list[dict[str, Any]]:
        """仅保留当前组件及共享 authority 的 Provider，形成最小证据闭包。"""

        component_name = finding.get("component_name")
        components = manifest.get("components", [])
        primary = next((item for item in components if item.get("name") == component_name), None)
        if primary is None:
            return []
        result = [primary]
        authorities = {
            value.strip() for value in str(primary.get("authorities") or "").split(";") if value.strip()
        }
        if authorities:
            result.extend(
                item for item in components
                if item.get("kind") == "provider"
                and item.get("name") != component_name
                and authorities.intersection({
                    value.strip() for value in str(item.get("authorities") or "").split(";") if value.strip()
                })
            )
        return result

    @staticmethod
    def _relevant_permissions(manifest: dict[str, Any], finding: dict[str, Any]) -> dict[str, str]:
        """保留当前发现引用的自定义权限定义。"""

        component_name = finding.get("component_name")
        component = next(
            (item for item in manifest.get("components", []) if item.get("name") == component_name),
            {},
        )
        names = {
            component.get("permission"), component.get("read_permission"), component.get("write_permission")
        }
        custom = manifest.get("custom_permissions", {})
        return {name: custom[name] for name in names if name in custom}

    @staticmethod
    def _latest_slice(run_dir: Path, slice_id: Any) -> dict[str, Any] | None:
        if not isinstance(slice_id, str):
            return None
        slice_dir = run_dir / "slices" / slice_id
        if slice_dir.is_symlink() or not slice_dir.is_dir():
            return None
        rounds = sorted(path for path in slice_dir.glob("round-*.json") if path.is_file() and not path.is_symlink())
        return json.loads(rounds[-1].read_text("utf-8")) if rounds else None

    @staticmethod
    def _write_slice(run_dir: Path, slice_document: dict[str, Any], round_number: int) -> None:
        slice_dir = run_dir / "slices" / slice_document["slice_id"]
        slice_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = slice_dir / f"round-{round_number:03d}.json"
        path.write_text(json.dumps(slice_document, ensure_ascii=False, indent=2), "utf-8")
        path.chmod(0o600)

    def _stage(self, run_id: str, stage: str) -> None:
        self.repository.update_run(run_id, status="running", stage=stage)
        self.storage.update_manifest(run_id, status="running", stage=stage)

    def _record_stage(self, run_id: str, stage: str, status: str, summary: dict[str, Any]) -> None:
        manifest = self.storage.read_manifest(run_id)
        manifest.setdefault("stages", []).append({
            "name": stage,
            "status": status,
            "ended_at": datetime.now(UTC).isoformat(),
            "summary": summary,
            "version": "1.0.0",
        })
        self.storage.write_manifest(run_id, manifest)

    def _run_config(self) -> dict[str, Any]:
        return {
            "analysis_platform_api": self.settings.analysis_platform_api,
            "source_analysis": self.settings.source_analysis.model_dump(mode="json"),
            "funnel": self.settings.funnel.model_dump(mode="json"),
            "context_budget": self.settings.context_budget.model_dump(mode="json"),
        }


def _mark_ai_unavailable(
    candidate: dict[str, Any],
    analysis_status: str,
    reason: str,
    preflight: dict[str, Any],
) -> None:
    candidate["analysis_status"] = analysis_status
    candidate["ai_status_reason"] = reason
    candidate["ai_preflight"] = preflight
    if analysis_status == "ai_skipped":
        candidate["ai_skip_reason"] = reason
        gap_code = "AI_ANALYSIS_SKIPPED"
    else:
        candidate["ai_failure_reason"] = reason
        gap_code = "AI_ANALYSIS_FAILED"
    candidate.setdefault("ai_blocking_gaps", []).append({
        "code": gap_code,
        "critical": not candidate.get("deterministic_chain_verified", False),
        "message": reason,
    })


def _finalize_run_coverage(
    candidates: list[dict[str, Any]],
    jadx_gaps: list[Any],
    rule_failures: list[Any],
    code_index: dict[str, Any],
    rule_component_gaps: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper around the structured coverage-domain module."""

    return finalize_run_coverage(
        candidates,
        jadx_gaps,
        rule_failures,
        code_index,
        rule_component_gaps,
    )


def _should_build_slice(candidate: dict[str, Any]) -> bool:
    """仅为漏斗选中的 AI 代表候选构建代码切片。"""

    if "ai_eligible" in candidate and candidate.get("ai_eligible") is not True:
        return False
    if candidate.get("evidence_level") == "L2":
        return True
    if candidate.get("evidence_level") != "L1":
        return False
    if candidate.get("auxiliary"):
        return False
    if candidate.get("reachability_status") not in {"reachable", "conditional", None, ""}:
        return False
    if candidate.get("authorization_status") in {"strongly_protected", "protected"}:
        return False
    if not candidate.get("component_name"):
        return False
    return True


def _candidate_depends_on_skipped_files(
    candidate: dict[str, Any], skipped_files: list[dict[str, Any]]
) -> bool:
    """仅在候选组件或证据明确关联超限文件时传播 critical gap。"""

    if not skipped_files:
        return False
    skipped_paths = {str(item.get("path") or "") for item in skipped_files}
    skipped_source_keys = {
        str(Path(path).with_suffix("")).replace("\\", "/").split("$", 1)[0]
        for path in skipped_paths if path
    }
    component_name = str(candidate.get("component_name") or "")
    component_source_key = component_name.split("$", 1)[0].replace(".", "/")
    if component_source_key and component_source_key in skipped_source_keys:
        return True
    for field in ("locations", "sources", "sinks"):
        for evidence in candidate.get(field, []):
            if isinstance(evidence, dict) and str(evidence.get("path") or "") in skipped_paths:
                return True
    component_package = component_name.rsplit(".", 1)[0] if "." in component_name else ""
    for return_type in candidate.get("binder_return_types", []):
        normalized = str(return_type).replace("$", ".")
        if normalized and normalized.split(".", 1)[0][:1].isupper() and component_package:
            normalized = f"{component_package}.{normalized}"
        type_key = normalized.replace(".", "/")
        possible_source_keys = {type_key}
        if "/" in type_key:
            possible_source_keys.add(type_key.rsplit("/", 1)[0])
        if possible_source_keys.intersection(skipped_source_keys):
            return True
    return False


def _merge_dict_list(left: list[Any], right: list[Any]) -> list[Any]:
    result = []
    seen = set()
    for item in [*left, *right]:
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, dict) else str(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result
