"""调用 OpenAI 兼容模型，对受限代码切片执行严格、可审计的静态证据复核。"""

from __future__ import annotations

import asyncio  # noqa: F401 - 测试 monkeypatch 挂载点（ai_module.asyncio.sleep）
import hashlib
import json
import logging
import os
import random  # noqa: F401 - 测试 monkeypatch 挂载点（ai_module.random.uniform）
import time
from typing import Any

import httpx
from pydantic import ValidationError

from app.analysis.ai_cache import AICacheStore, build_cache_descriptor, build_cache_key
from app.analysis.ai_models import (
    AI_OUTPUT_MODEL_VERSIONS,
    DeepDiveInput,
    DeepDiveOutput,
    DeterministicSemanticBundle,
    ExplorerInput,
    ExplorerObservation,
    FinalizationInput,
    FinalizationOutput,
    L1TriageInput,
    L1TriageOutput,
    L2ReviewInput,
    L2ReviewOutput,
    PreflightInput,
    PreflightOutput,
    RepairInput,
    RepairOutput,
    StrictAIModel,
)
from app.analysis.ai_scheduler import TaskCircuit
from app.analysis.ai_transport import (
    AITransport,
    AITransportResult,
    retry_after_seconds,
)
from app.analysis.prompt_registry import (
    PromptRegistry,
    PromptRegistryError,
    RenderedPrompt,
)

_DEFAULT_MAX_REQUEST_BYTES = 524288  # 512 KiB
_DEFAULT_PROVIDER_MAX_IN_FLIGHT = 4
_DEFAULT_PROVIDER_MAX_COOLDOWN_SECONDS = 60.0
_DEFAULT_RETRY_COUNT = 1
_DEFAULT_RETRY_BASE_SECONDS = 0.05
_DEFAULT_RETRY_MAX_SECONDS = 30.0
_DEFAULT_RETRY_JITTER_SECONDS = 0.05
LOGGER = logging.getLogger(__name__)
_PROMPT_VERSIONS = {
    "preflight": "1.0.1",
    "l1-triage": "2.0.4",
    # 3.0.7：红线 23 静态可证例外 + refutation_basis 枚举（P1-5，2026-08-15）。
    # 3.0.6 引入 candidate.deterministic_facts 使用规则（P1-4），基于生效版本 3.0.4 派生
    # ——3.0.5 已入库但从未被 _PROMPT_VERSIONS 引用。
    "l2-review": "3.0.7",
    "repair": "1.0.1",
    "finalization": "1.0.3",
}
_RETRYABLE_HTTP_STATUSES = {408, 425, 429}
_VALID_GUARD_STATUSES = {
    "absent",
    "present_effective",
    "present_bypassable",
    "present_partial",
    "unknown",
}


class OpenAICompatibleAnalyzer:
    """在明确允许外发代码时调用 OpenAI 兼容分析服务。"""

    version = "2.2.0"

    def __init__(
        self,
        settings: Any,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_backoff_seconds: float | None = None,
        prompt_registry: PromptRegistry | None = None,
        registry: PromptRegistry | None = None,
        ai_transport: AITransport | None = None,
    ):
        """保存外部模型配置并初始化版本化 Prompt registry。"""

        if prompt_registry is not None and registry is not None and prompt_registry is not registry:
            raise ValueError("不能同时注入不同的 prompt_registry 和 registry")
        self.settings = settings
        self.transport = transport
        self.retry_backoff_seconds = retry_backoff_seconds
        self.prompt_registry = prompt_registry or registry or PromptRegistry()
        self.registry = self.prompt_registry
        self._ai_transport = ai_transport or AITransport(
            settings,
            transport=transport,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        self._owns_ai_transport = ai_transport is None
        self._cache_store: AICacheStore | None = None
        self._task_circuit: TaskCircuit | None = None
        self._max_output_tokens: int | None = None
        self._budget_policy: dict[str, Any] = {}

    @property
    def _max_request_bytes(self) -> int:
        return getattr(self.settings, "max_request_bytes", 0) or _DEFAULT_MAX_REQUEST_BYTES

    def configure_cache(
        self,
        ai_cache_dir: str | os.PathLike[str] | None = None,
        *,
        disable: bool = False,
        reset: bool = False,
    ) -> None:
        """配置调用方显式提供的 ``ai-cache`` 目录；不推断任何路径。

        目录可以跨 run 共享，复用边界由 prompt/schema/model/context/budget 等完整缓存身份
        隔离，而非目录或 run_id；``reset`` 仅重置当前 analyzer 的 store 引用，不删除缓存。
        """

        if reset or disable or ai_cache_dir is None:
            self._cache_store = None
        if disable or ai_cache_dir is None:
            return
        self._cache_store = AICacheStore(
            ai_cache_dir,
            max_entry_bytes=getattr(self.settings, "cache_max_entry_bytes", 2097152),
        )

    def configure_budget_identity(
        self,
        max_output_tokens: int | None,
        budget_policy: dict[str, Any] | None,
    ) -> None:
        """记录影响请求与缓存身份的输出和上下文预算策略。"""

        effective = max_output_tokens or getattr(self.settings, "max_output_tokens", None)
        self._max_output_tokens = effective
        self._budget_policy = dict(budget_policy or {})

    async def aclose(self) -> None:
        """关闭独占 transport；进程级注入的 transport 由 AIRuntime 关闭。"""

        if self._owns_ai_transport:
            await self._ai_transport.aclose()

    def checkpoint_identity(
        self,
        candidate: dict[str, Any],
        context_slice: dict[str, Any],
    ) -> dict[str, Any]:
        """返回足以使 candidate checkpoint 对 prompt/schema/model/context 失效的身份。"""

        is_l1 = candidate.get("evidence_level") == "L1"
        prompt_id = "l1-triage" if is_l1 else "l2-review"
        output_model = L1TriageOutput if is_l1 else L2ReviewOutput
        try:
            semantic_bundle = _semantic_bundle(candidate, context_slice)
            model_input: StrictAIModel
            if is_l1:
                model_input = L1TriageInput.model_validate({
                    "semantic_bundle": semantic_bundle.model_dump(mode="json"),
                    "round": _analysis_round(context_slice, None),
                    "previous_output": None,
                })
            else:
                model_input = L2ReviewInput.model_validate({
                    "semantic_bundle": semantic_bundle.model_dump(mode="json"),
                    "round": _analysis_round(context_slice, None),
                    "l1_triage": None,
                    "previous_output": None,
                })
            rendered = self.prompt_registry.render(
                prompt_id,
                _PROMPT_VERSIONS[prompt_id],
                {_prompt_variable(prompt_id): _canonical_json(model_input.model_dump(mode="json"))},
            )
            prompt_identity = _checkpoint_prompt_identity(rendered, output_model)
            finalization_input = FinalizationInput.model_validate({
                "semantic_bundle": semantic_bundle.model_dump(mode="json"),
                "l1_triage": None,
                "l2_review": None,
            })
            finalization_rendered = self.prompt_registry.render(
                "finalization",
                _PROMPT_VERSIONS["finalization"],
                {
                    _prompt_variable("finalization"): _canonical_json(
                        finalization_input.model_dump(mode="json")
                    )
                },
            )
            finalization_identity = _checkpoint_prompt_identity(
                finalization_rendered, FinalizationOutput
            )
        except (ValidationError, PromptRegistryError, TypeError, ValueError):
            prompt_identity = {"prompt_id": prompt_id, "version": _PROMPT_VERSIONS[prompt_id]}
            finalization_identity = {
                "prompt_id": "finalization",
                "version": _PROMPT_VERSIONS["finalization"],
            }
        base_url = self.settings.base_url.rstrip("/")
        provider_identity = {
            "provider_kind": "openai-compatible",
            "base_url_hash": _sha256(base_url),
            "api_key_env_hash": _sha256(self.settings.api_key_env),
        }
        identity = {
            "protocol": "strict-json-v2",
            "analysis_track": "l1_triage" if is_l1 else "l2_review",
            **provider_identity,
            "model": self.settings.model,
            "temperature": 0,
            "max_output_tokens": self._max_output_tokens,
            "budget_policy": self._budget_policy,
            "analyzer_version": self.version,
            "prompt": prompt_identity,
            "finalization_prompt": finalization_identity,
        }
        identity["config_fingerprint"] = _sha256(_canonical_json(identity))
        return identity

    def set_task_circuit(self, circuit: TaskCircuit | None) -> None:
        """设置编排层可选的任务级熔断器。"""

        if circuit is not None and not isinstance(circuit, TaskCircuit):
            raise TypeError("circuit 必须是 TaskCircuit 或 None")
        self._task_circuit = circuit

    def availability(self) -> tuple[bool, str]:
        """检查 AI 开关、代码外发授权、端点、模型和密钥是否齐备。"""

        result = self._local_configuration_result()
        return result["status"] == "passed", result["message"]

    async def preflight(self) -> dict[str, Any]:
        """使用 registry 中不含代码的 Prompt 验证严格 JSON 输出协议。"""

        local_result = self._local_configuration_result()
        if local_result["status"] != "passed":
            return local_result

        preflight_input = PreflightInput.model_validate({
            "provider_kind": "openai-compatible",
            "model": self.settings.model,
            "response_format": "json_object",
            "required_capabilities": [
                "strict_json_object",
                "required_fields",
                "forbid_extra_fields",
            ],
        })
        result = await self._invoke_prompt(
            "preflight",
            _PROMPT_VERSIONS["preflight"],
            preflight_input,
            PreflightOutput,
            "preflight",
        )
        if result["status"] != "completed":
            return result

        output = PreflightOutput.model_validate(result["analysis"])
        metadata = result["metadata"]
        if output.ok is not True or output.analysis_complete is not True:
            return _preflight_result(
                "failed",
                "response_invalid",
                False,
                metadata.get("http_status"),
                "AI preflight 未确认严格输出能力",
                metadata,
                circuit_breaking=True,
            )
        return _preflight_result(
            "passed",
            "configured",
            False,
            metadata.get("http_status"),
            "AI preflight 通过",
            metadata,
            circuit_breaking=False,
        )

    async def triage_l1(
        self,
        candidate: dict[str, Any],
        context_slice: dict[str, Any],
        previous_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """使用严格 L1TriageInput/L1TriageOutput 执行 L1 分诊。"""

        unavailable = self._analysis_unavailable_result()
        if unavailable is not None:
            return unavailable
        try:
            model_input = L1TriageInput.model_validate({
                "semantic_bundle": _semantic_bundle(candidate, context_slice).model_dump(mode="json"),
                "round": _analysis_round(context_slice, previous_analysis),
                "previous_output": _pure_analysis(previous_analysis),
            })
        except (TypeError, ValueError, ValidationError):
            return _analysis_failure(
                "schema_invalid",
                False,
                None,
                "L1 AI 输入不符合严格协议",
                {**self._base_metadata(), "analysis_track": "l1_triage", "attempts": 0},
                circuit_breaking=False,
            )
        return await self._invoke_prompt(
            "l1-triage",
            _PROMPT_VERSIONS["l1-triage"],
            model_input,
            L1TriageOutput,
            "l1_triage",
            context_slice=context_slice,
        )

    async def review_l2(
        self,
        candidate: dict[str, Any],
        context_slice: dict[str, Any],
        previous_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """使用严格 L2ReviewInput/L2ReviewOutput 执行 L2 证据复核。"""

        unavailable = self._analysis_unavailable_result()
        if unavailable is not None:
            return unavailable
        try:
            previous_output = _pure_analysis(previous_analysis)
            l1_triage = _extract_l1_triage(previous_analysis)
            model_input = L2ReviewInput.model_validate({
                "semantic_bundle": _semantic_bundle(candidate, context_slice).model_dump(mode="json"),
                "round": _analysis_round(context_slice, previous_analysis),
                "l1_triage": l1_triage.model_dump(mode="json") if l1_triage is not None else None,
                "previous_output": previous_output,
            })
        except (TypeError, ValueError, ValidationError):
            return _analysis_failure(
                "schema_invalid",
                False,
                None,
                "L2 AI 输入不符合严格协议",
                {**self._base_metadata(), "analysis_track": "l2_review", "attempts": 0},
                circuit_breaking=False,
            )
        return await self._invoke_prompt(
            "l2-review",
            _PROMPT_VERSIONS["l2-review"],
            model_input,
            L2ReviewOutput,
            "l2_review",
            context_slice=context_slice,
        )

    async def finalize(
        self,
        candidate: dict[str, Any],
        context_slice: dict[str, Any],
        previous_analysis: dict[str, Any],
    ) -> dict[str, Any]:
        """使用 FinalizationInput/Output 归并最后一次严格分析，不直接作策略裁决。"""

        unavailable = self._analysis_unavailable_result()
        if unavailable is not None:
            return unavailable
        try:
            is_l1 = candidate.get("evidence_level") == "L1"
            l1_triage = _extract_l1_triage(previous_analysis) if is_l1 else None
            l2_review = _extract_l2_review(previous_analysis) if not is_l1 else None
            if l1_triage is None and l2_review is None:
                raise ValueError("finalization 缺少可归并的严格分析输出")
            model_input = FinalizationInput.model_validate({
                "semantic_bundle": _semantic_bundle(candidate, context_slice).model_dump(mode="json"),
                "l1_triage": l1_triage.model_dump(mode="json") if l1_triage is not None else None,
                "l2_review": l2_review.model_dump(mode="json") if l2_review is not None else None,
            })
        except (TypeError, ValueError, ValidationError):
            return _analysis_failure(
                "schema_invalid",
                False,
                None,
                "AI finalization 输入不符合严格协议",
                {**self._base_metadata(), "analysis_track": "finalization", "attempts": 0},
                circuit_breaking=False,
            )
        result = await self._invoke_prompt(
            "finalization",
            _PROMPT_VERSIONS["finalization"],
            model_input,
            FinalizationOutput,
            "finalization",
            context_slice=context_slice,
        )
        if result.get("status") == "completed":
            result["analysis"] = _adapt_finalization_analysis(
                FinalizationOutput.model_validate(result["analysis"]),
                previous_analysis,
                "l1_triage" if is_l1 else "l2_review",
            )
        return result

    async def analyze(
        self,
        candidate: dict[str, Any],
        context_slice: dict[str, Any],
        previous_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """兼容 facade：按 evidence_level 分轨并适配为现有编排层协议。"""

        if candidate.get("evidence_level") == "L1":
            result = await self.triage_l1(candidate, context_slice, previous_analysis)
            if result.get("status") == "completed":
                result["analysis"] = _adapt_l1_analysis(
                    L1TriageOutput.model_validate(result["analysis"]), candidate
                )
            return result

        result = await self.review_l2(candidate, context_slice, previous_analysis)
        if result.get("status") == "completed":
            result["analysis"] = _adapt_l2_analysis(
                L2ReviewOutput.model_validate(result["analysis"])
            )
        return result

    async def explore_entry(self, model_input: ExplorerInput) -> dict[str, Any]:
        """使用严格 ExplorerInput/ExplorerObservation 执行单轮探索（T2.5b）。

        model_input 由驱动层（ExplorerOrchestrator）构造——上下文累积是驱动
        职责；本方法只执行协议（复用 render→cache→budget→transport→
        strict-parse→repair 状态机；评审 R-3：ExplorerObservation 无
        analysis_complete/evidence_refs——缓存判据恒 no-op，属预期放弃缓存）。
        """

        unavailable = self._analysis_unavailable_result()
        if unavailable is not None:
            return unavailable
        return await self._invoke_prompt(
            "explorer",
            "1.0.0",
            model_input,
            ExplorerObservation,
            "explorer",
        )

    async def deep_dive_entry(self, model_input: DeepDiveInput) -> dict[str, Any]:
        """单轮深挖协议执行（T2.8）：partial 候选补齐事实（禁止改写链）。

        与 explore_entry 同模式复用状态机；prompt_version 沿先例硬编码
        "1.0.0"（registry 哈希门禁 + test_config 注册对齐护栏兜底；config
        的 deep_dive_prompt_version 为声明性字段）。缓存判据同 explorer 轨
        no-op 放弃（DeepDiveOutput.evidence_refs 对无切片校验恒不通过）。
        """

        unavailable = self._analysis_unavailable_result()
        if unavailable is not None:
            return unavailable
        return await self._invoke_prompt(
            "explorer-deep-dive",
            "1.0.0",
            model_input,
            DeepDiveOutput,
            "explorer-deep-dive",
        )

    async def _invoke_prompt(
        self,
        prompt_id: str,
        prompt_version: str,
        model_input: StrictAIModel,
        output_model: type[StrictAIModel],
        analysis_track: str,
        *,
        context_slice: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """执行 render→cache→budget→transport→strict-parse→单次 repair 状态机。

        preflight 默认与普通分析同级：可兼容围栏/包裹文本，首次校验失败后最多进入一次独立
        repair；仅当 ai.preflight_strict_protocol=true 时才禁止宽松解析与 repair 并立即熔断。
        preflight 在宽松解析与 repair 均失败后仍然熔断，避免带着不可用协议继续跑全量候选。
        只有严格模型完成且 evidence 引用属于当前切片时才接受并
        写缓存。请求超限、transport/HTTP/协议失败均返回分类结果；缓存读写失败只记 metadata，
        不改变网络调用或已接受输出的语义。
        """

        started = time.monotonic()
        metadata = {
            **self._base_metadata(),
            "analysis_track": analysis_track,
            "structured_output_mode": "json_object",
            "initial_attempts": 0,
            "repair_attempts": 0,
            "attempts": 0,
            "initial_response_hash": None,
            "repair_response_hash": None,
            "accepted_response_hash": None,
            "protocol_relaxed": False,
            "protocol_relaxation": None,
        }
        try:
            input_json = _canonical_json(model_input.model_dump(mode="json"))
            rendered = self.prompt_registry.render(
                prompt_id,
                prompt_version,
                {_prompt_variable(prompt_id): input_json},
            )
        except (PromptRegistryError, TypeError, ValueError):
            _finish_latency(metadata, started)
            return _analysis_failure(
                "prompt_registry_invalid",
                False,
                None,
                "AI Prompt registry 加载或渲染失败",
                metadata,
                circuit_breaking=True,
            )

        messages = _messages(rendered)
        payload = _chat_payload(
            self.settings.model,
            messages,
            self._max_output_tokens,
            disable_thinking=bool(getattr(self.settings, "disable_thinking", False)),
            thinking_param=str(getattr(self.settings, "thinking_param", "thinking")),
        )
        metadata.update(_prompt_metadata(rendered, messages, payload))
        metadata["model_input_hash"] = _sha256(input_json)
        if context_slice is not None:
            try:
                metadata["input_slice_hash"] = _sha256(_canonical_json(context_slice))
            except (TypeError, ValueError):
                metadata["input_slice_hash"] = None

        body_bytes = _payload_bytes(payload)
        metadata["request_bytes"] = len(body_bytes)
        cache_descriptor = None
        cache_key = None
        if self._cache_store is not None:
            metadata["cache_hit"] = False
            try:
                cache_descriptor = build_cache_descriptor(
                    provider_kind="openai-compatible",
                    base_url=self.settings.base_url.rstrip("/"),
                    model=self.settings.model,
                    analyzer_version=self.version,
                    prompt_id=rendered["id"],
                    prompt_version=rendered["version"],
                    system_template_hash=metadata["system_template_sha256"],
                    user_template_hash=metadata["user_template_sha256"],
                    input_schema_hash=metadata["input_schema_sha256"],
                    output_schema_hash=metadata["output_schema_sha256"],
                    model_input_hash=metadata["model_input_hash"],
                    input_slice_hash=metadata.get("input_slice_hash"),
                    request_hash=metadata["request_hash"],
                    output_model_name=output_model.__name__,
                    output_model_version=AI_OUTPUT_MODEL_VERSIONS[output_model.__name__],
                    protocol_version="strict-json-v2",
                    analysis_track=analysis_track,
                    scope_hash=_model_input_candidate_hash(model_input, "scope_key"),
                    fact_hash=_model_input_candidate_hash(model_input, "deterministic_fact_hash"),
                    context_hash=metadata.get("input_slice_hash"),
                    prompt_hash=metadata["prompt_template_hash"],
                    schema_hash=_sha256(_canonical_json(metadata["schema_sha256"])),
                    temperature=0.0,
                    max_output_tokens=self._max_output_tokens,
                    budget_policy_hash=_sha256(_canonical_json(self._budget_policy)),
                )
                cache_key = build_cache_key(cache_descriptor)
                metadata["cache_key"] = cache_key
                cached_output = self._cache_store.load(cache_descriptor, key=cache_key)
            except (OSError, ValueError, TypeError, KeyError):
                cache_descriptor = None
                cache_key = None
                metadata["cache_error"] = "descriptor_or_read_failed"
            else:
                if cached_output is not None and _cacheable_output(cached_output, context_slice):
                    metadata["cache_hit"] = True
                    _finish_latency(metadata, started)
                    return {
                        "status": "completed",
                        "analysis": cached_output.model_dump(mode="json"),
                        "metadata": metadata,
                    }
                if cached_output is not None:
                    metadata["cache_rejected"] = "evidence_refs_invalid"

        if len(body_bytes) > self._max_request_bytes:
            _finish_latency(metadata, started)
            return _analysis_failure(
                "input_too_large",
                False,
                None,
                f"AI 请求体 {len(body_bytes)} 字节超过预算 {self._max_request_bytes}",
                metadata,
                circuit_breaking=False,
            )

        initial_started = time.monotonic()
        transport_result = await self._post_chat_completions(payload)
        response = transport_result.response
        metadata["initial_attempts"] = transport_result.attempts
        metadata["attempts"] = transport_result.attempts
        metadata["initial_latency_ms"] = round((time.monotonic() - initial_started) * 1000)
        if response is None:
            classification, recoverable, message, circuit_breaking = _transport_failure_details(
                transport_result.failure
            )
            _finish_latency(metadata, started)
            return _analysis_failure(
                classification,
                recoverable,
                transport_result.failure_http_status,
                message,
                metadata,
                circuit_breaking=circuit_breaking,
            )

        metadata["http_status"] = response.status_code
        metadata["initial_http_status"] = response.status_code
        if not response.is_success:
            classification, recoverable, message = _classify_http_error(response)
            _finish_latency(metadata, started)
            return _analysis_failure(
                classification,
                recoverable,
                response.status_code,
                message,
                metadata,
                circuit_breaking=classification in {"auth_failed", "model_not_found"},
            )

        strict_preflight = bool(getattr(self.settings, "preflight_strict_protocol", False))
        preflight_strict_only = analysis_track == "preflight" and strict_preflight
        metadata["preflight_strict_protocol"] = strict_preflight
        content = ""
        parsed: Any = None
        invalid_classification = "response_invalid"
        validation_errors: list[str]
        try:
            content = _response_content(response)
            metadata["initial_response_hash"] = _sha256(content)
            if not content:
                # 记录空响应的诊断事实：finish_reason / 用量 / 推理 token
                # （deepseek 思维模式默认开启，推理 token 挤占 max_tokens 时 content 为空）
                metadata["empty_initial_content"] = True
                try:
                    raw_choices = response.json().get("choices") or []
                    if raw_choices:
                        metadata["finish_reason"] = raw_choices[0].get("finish_reason")
                    usage = response.json().get("usage") or {}
                    metadata["completion_tokens"] = usage.get("completion_tokens")
                    details = usage.get("completion_tokens_details") or {}
                    metadata["reasoning_tokens"] = details.get("reasoning_tokens")
                except (ValueError, KeyError, TypeError, IndexError):
                    LOGGER.debug("空响应诊断字段解析失败（忽略）")
            parsed, relaxation = _parse_structured_response_details(
                content,
                allow_relaxed=not preflight_strict_only,
            )
            if relaxation is not None:
                metadata["protocol_relaxed"] = True
                metadata["protocol_relaxation"] = relaxation
            output = output_model.model_validate(parsed)
        except _DuplicateJSONKeyError:
            _finish_latency(metadata, started)
            return _analysis_failure(
                "response_invalid",
                False,
                response.status_code,
                "AI 响应 JSON 包含重复键",
                metadata,
                circuit_breaking=False,
            )
        except ValidationError as exc:
            invalid_classification = "schema_invalid"
            validation_errors = _validation_error_messages(exc)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            validation_errors = ["响应不是可校验的单一 JSON 对象"]
        else:
            accepted_hash = metadata["initial_response_hash"]
            metadata["accepted_response_hash"] = accepted_hash
            metadata["response_hash"] = accepted_hash
            self._save_accepted_output(
                cache_descriptor, cache_key, output, metadata, context_slice
            )
            _finish_latency(metadata, started)
            return {
                "status": "completed",
                "analysis": output.model_dump(mode="json"),
                "metadata": metadata,
            }

        metadata["initial_validation_errors"] = validation_errors[:16]

        if preflight_strict_only:
            _finish_latency(metadata, started)
            return _analysis_failure(
                invalid_classification,
                False,
                response.status_code,
                "AI preflight 必须直接返回单一严格 JSON 对象（preflight_strict_protocol=true）",
                metadata,
                circuit_breaking=True,
            )

        metadata["format_repair_attempted"] = True
        invalid_output = parsed if parsed is not None else (content if content else None)
        repair_result = await self._repair_output(
            output_model,
            invalid_output,
            validation_errors,
            rendered["schema_sha256"]["output"],
            metadata,
        )
        _finish_latency(metadata, started)
        if repair_result["status"] != "completed":
            classification = repair_result.get("classification", invalid_classification)
            return _analysis_failure(
                classification,
                repair_result.get("recoverable", False),
                repair_result.get("http_status", response.status_code),
                repair_result.get("message", "AI 返回格式无效"),
                metadata,
                circuit_breaking=(
                    classification in {"auth_failed", "model_not_found"}
                    or analysis_track == "preflight"
                ),
            )

        output = repair_result["output"]
        accepted_hash = metadata["repair_response_hash"]
        metadata["accepted_response_hash"] = accepted_hash
        metadata["response_hash"] = accepted_hash
        self._save_accepted_output(
            cache_descriptor, cache_key, output, metadata, context_slice
        )
        return {
            "status": "completed",
            "analysis": output.model_dump(mode="json"),
            "metadata": metadata,
        }

    def _save_accepted_output(
        self,
        descriptor: Any,
        cache_key: str | None,
        output: StrictAIModel,
        metadata: dict[str, Any],
        context_slice: dict[str, Any] | None,
    ) -> None:
        if self._cache_store is None or descriptor is None or cache_key is None:
            return
        if not _cacheable_output(output, context_slice):
            metadata["cache_written"] = False
            metadata["cache_error"] = "not_strict_completed_or_evidence_refs_invalid"
            return
        if getattr(output, "analysis_complete", None) is not True:
            metadata["cache_written"] = False
            metadata["cache_skip_reason"] = "analysis_incomplete"
            return
        if isinstance(output, PreflightOutput) and output.ok is not True:
            metadata["cache_written"] = False
            metadata["cache_skip_reason"] = "preflight_not_accepted"
            return
        try:
            result = self._cache_store.save(descriptor, output, key=cache_key)
        except (OSError, ValueError, TypeError, KeyError):
            metadata["cache_written"] = False
            metadata["cache_error"] = "write_failed"
            return
        metadata["cache_written"] = result.written
        if not result.written:
            metadata["cache_error"] = "write_failed"

    async def _repair_output(
        self,
        target_model: type[StrictAIModel],
        invalid_output: Any,
        validation_errors: list[str],
        output_schema_sha256: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            repair_input = RepairInput.model_validate({
                "target_output_model": target_model.__name__,
                "invalid_output": invalid_output,
                "validation_errors": validation_errors[:64],
                "output_schema_sha256": output_schema_sha256,
            })
            repair_input_json = _canonical_json(repair_input.model_dump(mode="json"))
            rendered = self.prompt_registry.render(
                "repair",
                _PROMPT_VERSIONS["repair"],
                {_prompt_variable("repair"): repair_input_json},
            )
        except (PromptRegistryError, TypeError, ValueError, ValidationError):
            return {
                "status": "failed",
                "classification": "schema_invalid",
                "recoverable": False,
                "http_status": None,
                "message": "AI repair 输入或 Prompt 无效",
            }

        messages = _messages(rendered)
        payload = _chat_payload(
            self.settings.model,
            messages,
            self._max_output_tokens,
            disable_thinking=bool(getattr(self.settings, "disable_thinking", False)),
            thinking_param=str(getattr(self.settings, "thinking_param", "thinking")),
        )
        repair_metadata = _prompt_metadata(rendered, messages, payload)
        metadata["repair"] = repair_metadata
        metadata["repair_messages_hash"] = repair_metadata["messages_hash"]
        metadata["repair_request_hash"] = repair_metadata["request_hash"]
        body_bytes = _payload_bytes(payload)
        metadata["repair_request_bytes"] = len(body_bytes)
        if len(body_bytes) > self._max_request_bytes:
            return {
                "status": "failed",
                "classification": "input_too_large",
                "recoverable": False,
                "http_status": None,
                "message": "AI repair 请求体超过预算",
            }

        repair_started = time.monotonic()
        transport_result = await self._post_chat_completions(payload)
        response = transport_result.response
        metadata["repair_attempts"] = transport_result.attempts
        metadata["attempts"] = metadata["initial_attempts"] + transport_result.attempts
        metadata["repair_latency_ms"] = round((time.monotonic() - repair_started) * 1000)
        if response is None:
            classification, recoverable, message, _ = _transport_failure_details(
                transport_result.failure
            )
            return {
                "status": "failed",
                "classification": classification,
                "recoverable": recoverable,
                "http_status": transport_result.failure_http_status,
                "message": f"AI repair {message}",
            }

        metadata["repair_http_status"] = response.status_code
        if not response.is_success:
            classification, recoverable, message = _classify_http_error(response)
            return {
                "status": "failed",
                "classification": classification,
                "recoverable": recoverable,
                "http_status": response.status_code,
                "message": message,
            }

        repair_content = ""
        try:
            repair_content = _response_content(response)
            metadata["repair_response_hash"] = _sha256(repair_content)
            parsed_repair, relaxation = _parse_structured_response_details(repair_content)
            if relaxation is not None:
                metadata["protocol_relaxed"] = True
                metadata["protocol_relaxation"] = relaxation
            repair_output = RepairOutput.model_validate(parsed_repair)
            if repair_output.analysis_complete is not True:
                raise ValueError("格式修复未完整完成")
            repaired = target_model.model_validate(repair_output.repaired_output)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, ValidationError):
            return {
                "status": "failed",
                "classification": "schema_invalid",
                "recoverable": False,
                "http_status": response.status_code,
                "message": "AI repair 结果不符合目标严格协议",
            }
        return {"status": "completed", "output": repaired}

    def _analysis_unavailable_result(self) -> dict[str, Any] | None:
        local_result = self._local_configuration_result()
        if local_result["status"] == "passed":
            return None
        return {
            "status": "skipped",
            "reason": local_result["message"],
            "classification": local_result["classification"],
            "recoverable": local_result["recoverable"],
            "circuit_breaking": local_result.get("circuit_breaking", False),
            "metadata": local_result["metadata"],
        }

    def _local_configuration_result(self) -> dict[str, Any]:
        metadata = {**self._base_metadata(), "attempts": 0}
        if not self.settings.enabled:
            return _preflight_result(
                "skipped", "disabled", False, None, "AI 分析未启用，按配置跳过", metadata,
                circuit_breaking=False,
            )
        if not self.settings.allow_external_code:
            return _preflight_result(
                "skipped", "external_code_not_allowed", False, None,
                "未明确允许向外部模型发送代码，按配置跳过", metadata,
                circuit_breaking=False,
            )
        if not self.settings.base_url or not self.settings.model:
            return _preflight_result(
                "skipped", "missing_configuration", False, None,
                "OpenAI-compatible base_url/model 未配置，按配置跳过", metadata,
                circuit_breaking=False,
            )
        if not os.environ.get(self.settings.api_key_env):
            return _preflight_result(
                "skipped", "missing_credentials", False, None,
                "OpenAI-compatible API key 环境变量未配置，按配置跳过", metadata,
                circuit_breaking=False,
            )
        return _preflight_result(
            "passed", "configured", False, None, "configured", metadata,
            circuit_breaking=False,
        )

    def _base_metadata(self) -> dict[str, Any]:
        base_url = self.settings.base_url.rstrip("/") if self.settings.base_url else None
        return {
            "provider_kind": "openai-compatible",
            "base_url_hash": _sha256(base_url) if base_url else None,
            "model": self.settings.model,
            "analyzer_version": self.version,
        }

    def _provider_controller(self):
        return self._ai_transport.provider_controller()

    async def _post_chat_completions(
        self, payload: dict[str, Any]
    ) -> AITransportResult:
        headers = {"Authorization": f"Bearer {os.environ[self.settings.api_key_env]}"}
        return await self._ai_transport.post_chat_completions(
            payload,
            headers,
            circuit=self._task_circuit,
        )

    async def _retry_backoff(self, response: httpx.Response | None, attempt: int) -> None:
        """兼容入口；实际退避策略由共享 transport 实施。"""

        await self._ai_transport.retry_backoff(response, attempt)


def _preflight_result(
    status: str,
    classification: str,
    recoverable: bool,
    http_status: int | None,
    message: str,
    metadata: dict[str, Any],
    *,
    circuit_breaking: bool = False,
) -> dict[str, Any]:
    return {
        "status": status,
        "classification": classification,
        "recoverable": recoverable,
        "circuit_breaking": circuit_breaking,
        "http_status": http_status,
        "message": message,
        "metadata": metadata,
    }


def _analysis_failure(
    classification: str,
    recoverable: bool,
    http_status: int | None,
    message: str,
    metadata: dict[str, Any],
    *,
    circuit_breaking: bool = False,
) -> dict[str, Any]:
    metadata["http_status"] = http_status
    return {
        "status": "failed",
        "classification": classification,
        "recoverable": recoverable,
        "circuit_breaking": circuit_breaking,
        "http_status": http_status,
        "message": message,
        "error": {
            "code": f"AI_{classification.upper()}",
            "classification": classification,
            "recoverable": recoverable,
            "circuit_breaking": circuit_breaking,
            "http_status": http_status,
            "message": message,
        },
        "metadata": metadata,
    }


def _transport_failure_details(
    failure: str | None,
) -> tuple[str, bool, str, bool]:
    if failure == "auth_failed":
        return "auth_failed", False, "AI 服务鉴权熔断已打开", True
    if failure == "model_not_found":
        return "model_not_found", False, "AI 模型熔断已打开", True
    if failure == "circuit_open":
        return "circuit_open", False, "任务级 AI 熔断已打开", False
    return "transient_failure", True, "AI 服务网络请求失败", False


def _classify_http_error(response: httpx.Response) -> tuple[str, bool, str]:
    status = response.status_code
    marker = _response_error_marker(response)
    if status in {401, 403}:
        return "auth_failed", False, "AI 服务鉴权失败"
    if status == 404 or _is_explicit_model_error(marker):
        return "model_not_found", False, "AI 模型不存在或不可用"
    if status in {400, 422}:
        return "request_incompatible", False, "AI 请求与服务能力不兼容"
    if status == 429:
        return "rate_limited", True, "AI 服务请求频率受限"
    if status >= 500 or status in {408, 425}:
        return "transient_failure", True, "AI 服务暂时不可用"
    return "request_incompatible", False, "AI 服务拒绝请求"


def _response_error_marker(response: httpx.Response) -> str:
    try:
        body = response.json()
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(body, dict):
        return ""
    error = body.get("error", body)
    if not isinstance(error, dict):
        return ""
    values = [error.get("code"), error.get("type"), error.get("message")]
    return " ".join(str(value).lower() for value in values if value is not None)[:2000]


def _is_explicit_model_error(marker: str) -> bool:
    return any(value in marker for value in (
        "model_not_found",
        "model not found",
        "model does not exist",
        "model doesn't exist",
        "unknown model",
        "invalid model",
        "no such model",
    ))


def _response_content(response: httpx.Response) -> str:
    content = response.json()["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise TypeError("response content is not a string")
    return content


class _DuplicateJSONKeyError(ValueError):
    pass


def _parse_structured_response(content: str) -> dict[str, Any]:
    """解析唯一键 JSON；非 preflight 调用可兼容围栏或包裹文本。"""

    parsed, _ = _parse_structured_response_details(content)
    return parsed


def _parse_structured_response_details(
    content: str,
    *,
    allow_relaxed: bool = True,
) -> tuple[dict[str, Any], str | None]:
    try:
        return _loads_unique_json_object(content), None
    except _DuplicateJSONKeyError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError):
        if not allow_relaxed:
            raise json.JSONDecodeError("Expected one strict JSON object", content, 0)

    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        fenced = "\n".join(lines).strip()
        try:
            return _loads_unique_json_object(fenced), "markdown_fence"
        except _DuplicateJSONKeyError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    result = _extract_first_json_object(content)
    if result is not None:
        return _loads_unique_json_object(result), "surrounding_text"
    raise json.JSONDecodeError("No valid JSON object found", content, 0)


def _loads_unique_json_object(content: str) -> dict[str, Any]:
    parsed = json.loads(
        content,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(parsed, dict):
        raise TypeError("JSON 根值必须是对象")
    return parsed


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(f"JSON 包含重复键: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON 包含非法常量: {value}")


def _extract_first_json_object(text: str) -> str | None:
    """返回文本中第一个括号平衡的 JSON 对象。"""

    start = -1
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start:index + 1]
    return None


def _semantic_bundle(
    candidate: dict[str, Any], context_slice: dict[str, Any]
) -> DeterministicSemanticBundle:
    candidate_json = _json_dict(candidate)
    raw_contexts = context_slice.get("contexts", [])
    if not isinstance(raw_contexts, list):
        raise TypeError("contexts must be a list")
    contexts = [_json_dict(context) for context in raw_contexts[:256]]
    return DeterministicSemanticBundle.model_validate({
        "candidate": candidate_json,
        "contexts": contexts,
        "manifest_facts": [],
        "semantic_facts": [],
        "blocking_gaps": [],
        "uncertainties": [],
    })


def _extract_l1_triage(previous_analysis: dict[str, Any] | None) -> L1TriageOutput | None:
    if not isinstance(previous_analysis, dict):
        return None
    candidates = [previous_analysis]
    strict_output = previous_analysis.get("strict_output")
    if isinstance(strict_output, dict):
        candidates.insert(0, strict_output)
    allowed_fields = set(L1TriageOutput.model_fields)
    for value in candidates:
        try:
            selected = {key: item for key, item in value.items() if key in allowed_fields}
            return L1TriageOutput.model_validate(selected)
        except ValidationError:
            continue
    return None


def _extract_l2_review(previous_analysis: dict[str, Any] | None) -> L2ReviewOutput | None:
    if not isinstance(previous_analysis, dict):
        return None
    candidates = [previous_analysis]
    strict_output = previous_analysis.get("strict_output")
    if isinstance(strict_output, dict):
        candidates.insert(0, strict_output)
    allowed_fields = set(L2ReviewOutput.model_fields)
    for value in candidates:
        try:
            selected = {key: item for key, item in value.items() if key in allowed_fields}
            return L2ReviewOutput.model_validate(selected)
        except ValidationError:
            continue
    return None


def _analysis_round(
    context_slice: dict[str, Any], previous_analysis: dict[str, Any] | None
) -> int:
    history = context_slice.get("request_history", [])
    if isinstance(history, list) and history:
        return min(len(history), 16)
    return 1 if previous_analysis is not None else 0


def _adapt_l1_analysis(output: L1TriageOutput, candidate: dict[str, Any]) -> dict[str, Any]:
    guard_status = candidate.get("guard_status", "unknown")
    if guard_status not in _VALID_GUARD_STATUSES:
        guard_status = "unknown"
    disposition = output.triage_disposition
    return {
        **output.model_dump(mode="json"),
        "guard_status": guard_status,
        "promotion_recommended": disposition == "potential_chain",
        "confidence_tier": "low" if disposition == "insufficient" else "medium",
        "candidate_verdict": disposition,
        "analysis_track": "l1_triage",
    }


def _adapt_l2_analysis(output: L2ReviewOutput) -> dict[str, Any]:
    verdict = output.verdict
    return {
        **output.model_dump(mode="json"),
        "promotion_recommended": verdict == "supports_candidate",
        "candidate_verdict": verdict,
        "analysis_track": "l2_review",
    }


def _adapt_finalization_analysis(
    output: FinalizationOutput,
    previous_analysis: dict[str, Any],
    source_analysis_track: str,
) -> dict[str, Any]:
    """保留原始语义轨道；review_recommendation 仅作建议，不写 review_status。"""

    verdict = output.verdict
    return {
        **output.model_dump(mode="json"),
        "confidence_tier": previous_analysis.get("confidence_tier", "medium"),
        "guard_status": previous_analysis.get("guard_status", "unknown"),
        "context_requests": [],
        "promotion_recommended": verdict == "supports_candidate",
        "candidate_verdict": verdict,
        "analysis_track": "finalization",
        "source_analysis_track": source_analysis_track,
    }


# 代码注入字段：分析结果经 orchestrator/adapt 注入的派生字段，不属于模型输出 schema。
# 扩片时 previous_output 必须剥离，否则模型照抄导致 extra_forbidden（实测 12 候选 failed）。
_INJECTED_ANALYSIS_FIELDS = frozenset({
    "analysis_track", "candidate_verdict", "promotion_recommended",
    "verified_evidence_refs", "invalid_evidence_refs", "source_analysis_track",
})


def _pure_analysis(value: Any) -> dict[str, Any] | None:
    """剥离代码注入字段，只保留模型 schema 允许的字段；None 原样返回。"""

    if value is None:
        return None
    normalized = _json_dict(value)
    return {k: v for k, v in normalized.items() if k not in _INJECTED_ANALYSIS_FIELDS}


def _json_dict(value: Any) -> dict[str, Any]:
    normalized = json.loads(_canonical_json(value))
    if not isinstance(normalized, dict):
        raise TypeError("value must be a JSON object")
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _prompt_variable(prompt_id: str) -> str:
    return prompt_id.replace("-", "_") + "_input_json"


def _messages(rendered: RenderedPrompt) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": rendered["system"]},
        {"role": "user", "content": rendered["user"]},
    ]


def _chat_payload(
    model: str,
    messages: list[dict[str, str]],
    max_output_tokens: int | None = None,
    disable_thinking: bool = False,
    thinking_param: str = "thinking",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    if max_output_tokens is not None:
        payload["max_tokens"] = max_output_tokens
    if disable_thinking:
        # deepseek-v4-flash 思维模式默认开启，推理 token 挤占 max_tokens 导致
        # content 为空或截断（实测 131/138 初始响应为空串）。JSON 判定无需思维过程。
        payload[thinking_param] = {"type": "disabled"}

    return payload


def _payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _checkpoint_prompt_identity(
    rendered: RenderedPrompt,
    output_model: type[StrictAIModel],
) -> dict[str, Any]:
    messages = _messages(rendered)
    return {
        "prompt_id": rendered["id"],
        "version": rendered["version"],
        "template": rendered["template_sha256"],
        "template_hash": _sha256(_canonical_json(rendered["template_sha256"])),
        "rendered": rendered["rendered_sha256"],
        "rendered_hash": _sha256(_canonical_json(rendered["rendered_sha256"])),
        "messages_hash": _sha256(_canonical_json(messages)),
        "schema": rendered["schema_sha256"],
        "schema_hash": _sha256(_canonical_json(rendered["schema_sha256"])),
        "output_model": output_model.__name__,
        "output_model_version": AI_OUTPUT_MODEL_VERSIONS[output_model.__name__],
    }


def _prompt_metadata(
    rendered: RenderedPrompt,
    messages: list[dict[str, str]],
    payload: dict[str, Any],
) -> dict[str, Any]:
    template_hash = _sha256(_canonical_json(rendered["template_sha256"]))
    rendered_hash = _sha256(_canonical_json(rendered["rendered_sha256"]))
    messages_hash = _sha256(_canonical_json(messages))
    request_hash = _sha256(_canonical_json(payload))
    return {
        "prompt_id": rendered["id"],
        "prompt_version": rendered["version"],
        "prompt_template_version": rendered["version"],
        "template_sha256": rendered["template_sha256"],
        "rendered_sha256": rendered["rendered_sha256"],
        "schema_sha256": rendered["schema_sha256"],
        "system_template_sha256": rendered["template_sha256"]["system"],
        "user_template_sha256": rendered["template_sha256"]["user"],
        "system_rendered_sha256": rendered["rendered_sha256"]["system"],
        "user_rendered_sha256": rendered["rendered_sha256"]["user"],
        "input_schema_sha256": rendered["schema_sha256"]["input"],
        "output_schema_sha256": rendered["schema_sha256"]["output"],
        "prompt_template_hash": template_hash,
        "prompt_template_sha256": template_hash,
        "rendered_prompt_hash": rendered_hash,
        "rendered_prompt_sha256": rendered_hash,
        "messages_hash": messages_hash,
        "messages_sha256": messages_hash,
        "request_hash": request_hash,
        "request_sha256": request_hash,
        "prompt_hash": template_hash,
        "prompt_hash_semantics": "template_sha256",
        "legacy_prompt_hash_messages_sha256": messages_hash,
    }


def _validation_error_messages(exc: ValidationError) -> list[str]:
    messages = []
    for error in exc.errors(include_url=False, include_input=False)[:64]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "output"
        error_type = str(error.get("type", "validation_error"))
        messages.append(f"{location}: {error_type}")
    return messages or ["输出不符合目标 schema"]


def _finish_latency(metadata: dict[str, Any], started: float) -> None:
    metadata["latency_ms"] = round((time.monotonic() - started) * 1000)
    metadata["total_latency_ms"] = metadata["latency_ms"]


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    return retry_after_seconds(response)


def _model_input_candidate_hash(model_input: StrictAIModel, field: str) -> str:
    value = model_input.model_dump(mode="json")
    candidate = value.get("semantic_bundle", {}).get("candidate", {})
    selected = candidate.get(field)
    if selected is None:
        selected = candidate
    return _sha256(_canonical_json(selected))


def _cacheable_output(
    output: StrictAIModel,
    context_slice: dict[str, Any] | None,
) -> bool:
    value = output.model_dump(mode="json")
    if value.get("analysis_complete") is not True:
        return False
    if isinstance(output, PreflightOutput):
        return output.ok is True
    evidence_refs = value.get("evidence_refs")
    return (
        isinstance(evidence_refs, list)
        and bool(evidence_refs)
        and _evidence_refs_valid(output, context_slice)
    )


def _evidence_refs_valid(
    output: StrictAIModel,
    context_slice: dict[str, Any] | None,
) -> bool:
    value = output.model_dump(mode="json")
    references = list(value.get("evidence_refs", []))
    for field in (
        "suggested_sources",
        "suggested_sinks",
        "guard_observations",
    ):
        references.extend(value.get(field, []))
    for gap in value.get("blocking_gaps", []):
        if isinstance(gap, dict):
            references.extend(gap.get("evidence_refs", []))
    if not references:
        return True
    if context_slice is None:
        return False
    contexts = {
        context.get("context_id"): context
        for context in context_slice.get("contexts", [])
        if isinstance(context, dict) and isinstance(context.get("context_id"), str)
    }
    for reference in references:
        if not isinstance(reference, dict):
            return False
        context = contexts.get(reference.get("context_id"))
        if context is None:
            return False
        reference_path = reference.get("path")
        context_path = context.get("path")
        if reference_path is not None and reference_path != context_path:
            return False
        line = reference.get("line")
        end_line = reference.get("end_line")
        if line is not None or end_line is not None:
            try:
                start_bound = int(context["start_line"])
                end_bound = int(context["end_line"])
                line_number = int(line) if line is not None else start_bound
                reference_end = int(end_line) if end_line is not None else line_number
            except (KeyError, TypeError, ValueError):
                return False
            if not start_bound <= line_number <= reference_end <= end_bound:
                return False
    return True


def _sha256(value: str) -> str:
    """返回非敏感配置或模型输入的稳定摘要。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
