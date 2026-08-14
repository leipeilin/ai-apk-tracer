"""调用方定址、可跨 run 复用且失效安全的 AI 响应缓存。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.analysis.ai_models import (
    AICacheDescriptor,
    AICacheEntry,
    PreflightOutput,
    SchemaSerialization,
    StrictAIModel,
    get_ai_output_model,
)


DEFAULT_MAX_ENTRY_BYTES = 2 * 1024 * 1024
_CACHE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


class _CacheSafetyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AICacheWriteResult:
    """缓存写入状态；调用方可以报告失败但继续使用原 AI 结果。"""

    key: str
    written: bool
    error: str | None = None


def canonical_json_bytes(value: Any) -> bytes:
    """使用稳定 JSON 表示，供描述符和已接受输出计算摘要。"""

    document = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return document.encode("utf-8", errors="strict")


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def build_cache_descriptor(
    *,
    provider_kind: str,
    base_url: str,
    model: str,
    analyzer_version: str,
    prompt_id: str,
    prompt_version: str,
    system_template_hash: str,
    user_template_hash: str,
    input_schema_hash: str,
    output_schema_hash: str,
    model_input_hash: str,
    request_hash: str,
    input_slice_hash: str | None = None,
    output_model_name: str | None = None,
    output_model_version: str | None = None,
    protocol_version: str | None = None,
    analysis_track: str | None = None,
    scope_hash: str | None = None,
    fact_hash: str | None = None,
    context_hash: str | None = None,
    prompt_hash: str | None = None,
    schema_hash: str | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    budget_policy_hash: str | None = None,
) -> AICacheDescriptor:
    """由调用元数据构建描述符；原始 base URL 不会进入返回值。"""

    if type(base_url) is not str:
        raise TypeError("base_url 必须是字符串")
    base_url_hash = hashlib.sha256(base_url.encode("utf-8", errors="strict")).hexdigest()
    return AICacheDescriptor.model_validate(
        {
            "descriptor_version": "1",
            "provider_kind": provider_kind,
            "base_url_hash": base_url_hash,
            "model": model,
            "analyzer_version": analyzer_version,
            "prompt_id": prompt_id,
            "prompt_version": prompt_version,
            "system_template_hash": system_template_hash,
            "user_template_hash": user_template_hash,
            "input_schema_hash": input_schema_hash,
            "output_schema_hash": output_schema_hash,
            "model_input_hash": model_input_hash,
            "input_slice_hash": input_slice_hash,
            "request_hash": request_hash,
            "output_model_name": output_model_name,
            "output_model_version": output_model_version,
            "protocol_version": protocol_version,
            "analysis_track": analysis_track,
            "scope_hash": scope_hash,
            "fact_hash": fact_hash,
            "context_hash": context_hash,
            "prompt_hash": prompt_hash,
            "schema_hash": schema_hash,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "budget_policy_hash": budget_policy_hash,
        }
    )


def build_cache_key(descriptor: AICacheDescriptor) -> str:
    """缓存键是完整描述符规范 JSON 的 SHA-256。"""

    checked = AICacheDescriptor.model_validate(descriptor)
    return canonical_json_hash(checked.model_dump(mode="json"))


def is_valid_cache_key(key: object) -> bool:
    return type(key) is str and _CACHE_KEY_RE.fullmatch(key) is not None


class AICacheStore:
    """在调用方显式提供的 ``ai-cache`` 目录内读写内容寻址缓存。

    目录可以由多个 run 共享；跨 run 复用只由完整描述符身份决定，run_id 不参与键。
    读取时会重新校验描述符、schema、输出摘要和 evidence，任何损坏或版本漂移均按
    miss 处理；写入失败也不得改变当前 AI 分析结果。
    """

    def __init__(self, cache_dir: str | os.PathLike[str], *, max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES) -> None:
        if type(max_entry_bytes) is not int or max_entry_bytes <= 0:
            raise ValueError("max_entry_bytes 必须是正整数")
        self.cache_dir = Path(cache_dir)
        self.entries_dir = self.cache_dir / "entries"
        self.max_entry_bytes = max_entry_bytes

    def load(self, descriptor: AICacheDescriptor, *, key: str | None = None) -> StrictAIModel | None:
        """安全读取并用当前输出模型重新校验；任意异常都按 miss 处理。

        即使共享目录中已有同名文件，也只有键、完整描述符、当前 schema、规范输出摘要
        及 evidence 约束全部一致才命中，避免旧 run 或其他模型配置污染当前分析。
        """

        try:
            checked_descriptor = AICacheDescriptor.model_validate(descriptor)
            expected_key = build_cache_key(checked_descriptor)
            cache_key = expected_key if key is None else key
            if not is_valid_cache_key(cache_key) or cache_key != expected_key:
                return None

            raw = self._read_entry(cache_key)
            if raw is None:
                return None
            _parse_strict_json(raw)
            entry = AICacheEntry.model_validate_json(raw, strict=True)
            if entry.descriptor != checked_descriptor:
                return None
            if build_cache_key(entry.descriptor) != cache_key:
                return None
            if canonical_json_hash(entry.accepted_output) != entry.accepted_output_hash:
                return None

            model_name = entry.descriptor.output_model_name
            model_version = entry.descriptor.output_model_version
            if model_name is None or model_version is None:
                return None
            output_model = get_ai_output_model(model_name, model_version)
            if output_model is None:
                return None
            if SchemaSerialization.sha256_for(output_model) != entry.descriptor.output_schema_hash:
                return None

            accepted = output_model.model_validate(entry.accepted_output)
            if not _has_required_cache_evidence(accepted):
                return None
            canonical_output = accepted.model_dump(mode="json")
            if canonical_output != entry.accepted_output:
                return None
            if canonical_json_hash(canonical_output) != entry.accepted_output_hash:
                return None
            return accepted
        except Exception:
            return None

    def save(
        self,
        descriptor: AICacheDescriptor,
        accepted_output: StrictAIModel | Mapping[str, Any],
        *,
        key: str | None = None,
    ) -> AICacheWriteResult:
        """原子写入严格输出；失败通过结果报告且绝不抛给分析流程。"""

        fallback_key = key if is_valid_cache_key(key) else ""
        temp_path: Path | None = None
        try:
            checked_descriptor = AICacheDescriptor.model_validate(descriptor)
            expected_key = build_cache_key(checked_descriptor)
            cache_key = expected_key if key is None else key
            fallback_key = cache_key if type(cache_key) is str else ""
            if not is_valid_cache_key(cache_key) or cache_key != expected_key:
                raise _CacheSafetyError("缓存键无效或与描述符不一致")

            model_name = checked_descriptor.output_model_name
            model_version = checked_descriptor.output_model_version
            if model_name is None or model_version is None:
                raise _CacheSafetyError("缓存描述符缺少输出模型身份")
            output_model = get_ai_output_model(model_name, model_version)
            if output_model is None:
                raise _CacheSafetyError("输出模型名称或版本不是当前注册版本")
            if SchemaSerialization.sha256_for(output_model) != checked_descriptor.output_schema_hash:
                raise _CacheSafetyError("输出 schema 摘要与当前模型不一致")

            if isinstance(accepted_output, StrictAIModel):
                output_value: Any = accepted_output.model_dump(mode="json")
            else:
                output_value = dict(accepted_output)
            accepted = output_model.model_validate(output_value)
            if not _has_required_cache_evidence(accepted):
                raise _CacheSafetyError("严格完成输出缺少 evidence_refs")
            canonical_output = accepted.model_dump(mode="json")
            now = datetime.now(timezone.utc)
            entry = AICacheEntry(
                schema_version="1",
                descriptor=checked_descriptor,
                accepted_output=canonical_output,
                accepted_output_hash=canonical_json_hash(canonical_output),
                created_at=now,
                updated_at=now,
            )
            serialized = canonical_json_bytes(entry.model_dump(mode="json"))
            if len(serialized) > self.max_entry_bytes:
                raise _CacheSafetyError("缓存记录超过大小上限")

            self._prepare_directories()
            destination = self.entries_dir / f"{cache_key}.json"
            self._require_safe_destination(destination)
            descriptor_fd, temp_name = tempfile.mkstemp(
                dir=self.entries_dir,
                prefix=f".{cache_key}.",
                suffix=".tmp",
            )
            temp_path = Path(temp_name)
            try:
                os.fchmod(descriptor_fd, 0o600)
                with os.fdopen(descriptor_fd, "wb") as stream:
                    stream.write(serialized)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                try:
                    os.close(descriptor_fd)
                except OSError:
                    pass
                raise

            self._require_safe_destination(destination)
            os.replace(temp_path, destination)
            temp_path = None
            self._fsync_directory(self.entries_dir)
            return AICacheWriteResult(key=cache_key, written=True)
        except Exception as exc:
            return AICacheWriteResult(
                key=fallback_key,
                written=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _read_entry(self, key: str) -> bytes | None:
        if not self._is_safe_directory(self.cache_dir):
            return None
        if not self._is_safe_directory(self.entries_dir):
            return None
        path = self.entries_dir / f"{key}.json"
        try:
            path_info = os.lstat(path)
        except OSError:
            return None
        if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
            return None
        if path_info.st_size == 0 or path_info.st_size > self.max_entry_bytes:
            return None

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(path, flags)
        except OSError:
            return None
        try:
            info = os.fstat(file_descriptor)
            if not stat.S_ISREG(info.st_mode):
                return None
            if info.st_size == 0 or info.st_size > self.max_entry_bytes:
                return None
            chunks: list[bytes] = []
            remaining = self.max_entry_bytes + 1
            while remaining > 0:
                chunk = os.read(file_descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if not raw or len(raw) > self.max_entry_bytes:
                return None
            return raw
        finally:
            os.close(file_descriptor)

    def _prepare_directories(self) -> None:
        self._create_or_require_directory(self.cache_dir)
        self._create_or_require_directory(self.entries_dir)

    @staticmethod
    def _create_or_require_directory(path: Path) -> None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            path.mkdir(mode=0o700)
            info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _CacheSafetyError(f"缓存目录不安全: {path.name}")

    @staticmethod
    def _is_safe_directory(path: Path) -> bool:
        try:
            info = os.lstat(path)
        except OSError:
            return False
        return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)

    @staticmethod
    def _require_safe_destination(path: Path) -> None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise _CacheSafetyError("缓存目标不是安全的普通文件")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return
        try:
            try:
                os.fsync(descriptor)
            except OSError:
                pass
        finally:
            os.close(descriptor)


def _has_required_cache_evidence(output: StrictAIModel) -> bool:
    value = output.model_dump(mode="json")
    if value.get("analysis_complete") is not True:
        return False
    if isinstance(output, PreflightOutput):
        return True
    evidence_refs = value.get("evidence_refs")
    return isinstance(evidence_refs, list) and bool(evidence_refs)


def _parse_strict_json(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _CacheSafetyError) as exc:
        raise _CacheSafetyError("缓存记录不是严格 UTF-8 JSON") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _CacheSafetyError("缓存 JSON 包含重复键")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise _CacheSafetyError(f"缓存 JSON 包含非法常量: {value}")
