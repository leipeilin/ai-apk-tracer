"""安全加载、校验并渲染工作区内版本化 Prompt。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from string import Formatter
from typing import Any, Mapping, TypedDict

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.analysis.ai_models import AI_MODEL_REGISTRY, SchemaSerialization, StrictAIModel
from app.config import WORKSPACE_ROOT


PROMPTS_ROOT = WORKSPACE_ROOT / "prompts"
SCHEMAS_ROOT = WORKSPACE_ROOT / "schemas"
MAX_REGISTRY_BYTES = 256 * 1024
MAX_TEMPLATE_BYTES = 256 * 1024
MAX_SCHEMA_BYTES = 2 * 1024 * 1024
MAX_VARIABLE_BYTES = 2 * 1024 * 1024
MAX_RENDERED_BYTES = 4 * 1024 * 1024
_PLACEHOLDER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SCHEMA_FILE_RE = re.compile(r"^ai_[a-z0-9_]+\.schema\.json$")


class PromptRegistryError(ValueError):
    """Prompt registry 或其受控文件不满足安全约束。"""


class _RegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _TemplateHashes(_RegistryModel):
    system: str = Field(pattern=r"^[0-9a-f]{64}$")
    user: str = Field(pattern=r"^[0-9a-f]{64}$")


class _SchemaHashes(_RegistryModel):
    input: str = Field(pattern=r"^[0-9a-f]{64}$")
    output: str = Field(pattern=r"^[0-9a-f]{64}$")


class PromptDefinition(_RegistryModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    system_file: str = Field(min_length=1, max_length=256)
    user_file: str = Field(min_length=1, max_length=256)
    allowed_placeholders: list[str] = Field(min_length=1, max_length=8)
    input_model: str = Field(min_length=1, max_length=128)
    output_model: str = Field(min_length=1, max_length=128)
    input_schema_file: str = Field(min_length=1, max_length=256)
    output_schema_file: str = Field(min_length=1, max_length=256)
    template_sha256: _TemplateHashes
    schema_sha256: _SchemaHashes


class _RegistryDocument(_RegistryModel):
    registry_version: str = Field(pattern=r"^[0-9]+$")
    prompts: list[PromptDefinition] = Field(min_length=1, max_length=64)


@dataclass(frozen=True, slots=True)
class CompiledPrompt:
    """已完成模板、模型和 schema 完整性校验的缓存对象。"""

    definition: PromptDefinition
    system_template: str
    user_template: str
    input_model: type[StrictAIModel]
    output_model: type[StrictAIModel]


class RenderedPrompt(TypedDict):
    id: str
    version: str
    system: str
    user: str
    template_sha256: dict[str, str]
    rendered_sha256: dict[str, str]
    schema_sha256: dict[str, str]
    input_model: str
    output_model: str


class _UniqueKeyLoader(yaml.SafeLoader):
    """拒绝 YAML 重复键，避免 registry 字段被静默覆盖。"""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise PromptRegistryError(f"registry.yaml 包含重复键: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class PromptRegistry:
    """从固定工作区目录按精确 ID 和版本加载 Prompt。"""

    def __init__(self) -> None:
        self._definitions = self._load_registry()
        self._compiled_cache: dict[tuple[str, str], CompiledPrompt] = {}

    @property
    def prompt_keys(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._definitions))

    def load(self, prompt_id: str, version: str) -> CompiledPrompt:
        """加载并缓存一个精确版本；不存在时拒绝隐式回退。"""

        key = (prompt_id, version)
        cached = self._compiled_cache.get(key)
        if cached is not None:
            return cached
        definition = self._definitions.get(key)
        if definition is None:
            raise PromptRegistryError(f"未知 Prompt ID/version: {prompt_id}@{version}")
        compiled = self._compile(definition)
        self._compiled_cache[key] = compiled
        return compiled

    def render(self, prompt_id: str, version: str, variables: Mapping[str, str]) -> RenderedPrompt:
        """使用完整且无额外项的变量集合渲染 system/user 两层模板。"""

        compiled = self.load(prompt_id, version)
        allowed = set(compiled.definition.allowed_placeholders)
        provided = set(variables)
        missing = sorted(allowed - provided)
        unknown = sorted(provided - allowed)
        if missing or unknown:
            details = []
            if missing:
                details.append(f"缺失变量 {missing}")
            if unknown:
                details.append(f"未知变量 {unknown}")
            raise PromptRegistryError("；".join(details))

        checked: dict[str, str] = {}
        for name, value in variables.items():
            if type(value) is not str:
                raise PromptRegistryError(f"变量 {name} 必须是 UTF-8 字符串")
            try:
                encoded = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise PromptRegistryError(f"变量 {name} 不是有效 UTF-8 文本") from exc
            if len(encoded) > MAX_VARIABLE_BYTES:
                raise PromptRegistryError(f"变量 {name} 超过大小上限")
            checked[name] = value

        try:
            system = compiled.system_template.format_map(checked)
            user = compiled.user_template.format_map(checked)
        except (KeyError, ValueError) as exc:
            raise PromptRegistryError("Prompt 渲染失败") from exc
        if len(system.encode("utf-8")) + len(user.encode("utf-8")) > MAX_RENDERED_BYTES:
            raise PromptRegistryError("渲染后的 Prompt 超过大小上限")

        definition = compiled.definition
        return {
            "id": definition.id,
            "version": definition.version,
            "system": system,
            "user": user,
            "template_sha256": {
                "system": definition.template_sha256.system,
                "user": definition.template_sha256.user,
            },
            "rendered_sha256": {
                "system": _sha256(system.encode("utf-8")),
                "user": _sha256(user.encode("utf-8")),
            },
            "schema_sha256": {
                "input": definition.schema_sha256.input,
                "output": definition.schema_sha256.output,
            },
            "input_model": definition.input_model,
            "output_model": definition.output_model,
        }

    def _load_registry(self) -> dict[tuple[str, str], PromptDefinition]:
        raw = _read_controlled_file(PROMPTS_ROOT, "registry.yaml", MAX_REGISTRY_BYTES)
        try:
            parsed = yaml.load(raw.decode("utf-8", errors="strict"), Loader=_UniqueKeyLoader)
            document = _RegistryDocument.model_validate(parsed)
        except UnicodeDecodeError as exc:
            raise PromptRegistryError("registry.yaml 不是有效 UTF-8") from exc
        except (yaml.YAMLError, ValidationError, TypeError) as exc:
            raise PromptRegistryError("registry.yaml 格式无效") from exc

        definitions: dict[tuple[str, str], PromptDefinition] = {}
        for definition in document.prompts:
            key = (definition.id, definition.version)
            if key in definitions:
                raise PromptRegistryError(f"重复 Prompt ID/version: {definition.id}@{definition.version}")
            _validate_definition_paths(definition)
            _validate_placeholders_declared(definition.allowed_placeholders)
            if definition.input_model not in AI_MODEL_REGISTRY:
                raise PromptRegistryError(f"未知输入模型: {definition.input_model}")
            if definition.output_model not in AI_MODEL_REGISTRY:
                raise PromptRegistryError(f"未知输出模型: {definition.output_model}")
            definitions[key] = definition
        return definitions

    def _compile(self, definition: PromptDefinition) -> CompiledPrompt:
        """编译精确版本，并校验模板摘要、placeholder 和 Pydantic schema 字节一致性。

        任一文件越界、symlink、大小、摘要或模型版本不一致均整体失败；不允许自动选择其他
        Prompt 版本或使用近似 schema，以保证缓存与审计身份可重建。
        """

        system_bytes = _read_controlled_file(PROMPTS_ROOT, definition.system_file, MAX_TEMPLATE_BYTES)
        user_bytes = _read_controlled_file(PROMPTS_ROOT, definition.user_file, MAX_TEMPLATE_BYTES)
        _require_hash(system_bytes, definition.template_sha256.system, definition.system_file)
        _require_hash(user_bytes, definition.template_sha256.user, definition.user_file)

        system = _decode_nonempty(system_bytes, definition.system_file)
        user = _decode_nonempty(user_bytes, definition.user_file)
        system_fields = _template_fields(system, definition.system_file)
        user_fields = _template_fields(user, definition.user_file)
        allowed = definition.allowed_placeholders
        if system_fields:
            raise PromptRegistryError("system 模板不得包含 placeholder")
        if user_fields != allowed:
            raise PromptRegistryError(
                f"user 模板必须且只能各使用一次声明的 placeholder: {allowed}"
            )

        input_model = AI_MODEL_REGISTRY[definition.input_model]
        output_model = AI_MODEL_REGISTRY[definition.output_model]
        self._validate_schema(
            definition.input_schema_file,
            definition.schema_sha256.input,
            input_model,
        )
        self._validate_schema(
            definition.output_schema_file,
            definition.schema_sha256.output,
            output_model,
        )
        return CompiledPrompt(definition, system, user, input_model, output_model)

    @staticmethod
    def _validate_schema(schema_file: str, expected_hash: str, model: type[StrictAIModel]) -> None:
        raw = _read_controlled_file(SCHEMAS_ROOT, schema_file, MAX_SCHEMA_BYTES)
        _require_hash(raw, expected_hash, schema_file)
        expected = SchemaSerialization.bytes_for(model)
        if raw != expected:
            raise PromptRegistryError(f"Schema 与 Pydantic 模型不一致: {schema_file}")
        try:
            document = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PromptRegistryError(f"Schema 不是有效 UTF-8 JSON: {schema_file}") from exc
        if document != model.model_json_schema():
            raise PromptRegistryError(f"Schema 内容与模型不一致: {schema_file}")


def _validate_definition_paths(definition: PromptDefinition) -> None:
    expected_system = f"{definition.id}/{definition.version}/system.md"
    expected_user = f"{definition.id}/{definition.version}/user.md"
    if definition.system_file != expected_system or definition.user_file != expected_user:
        raise PromptRegistryError(
            f"Prompt 文件必须匹配固定 ID/version 目录: {definition.id}@{definition.version}"
        )
    for schema_file in (definition.input_schema_file, definition.output_schema_file):
        if not _SCHEMA_FILE_RE.fullmatch(schema_file):
            raise PromptRegistryError(f"非法 Schema 文件名: {schema_file}")


def _validate_placeholders_declared(placeholders: list[str]) -> None:
    if len(placeholders) != 1:
        raise PromptRegistryError("每个 user 模板必须声明且只声明一个规范 JSON placeholder")
    if len(set(placeholders)) != len(placeholders):
        raise PromptRegistryError("allowed_placeholders 不得重复")
    if any(not _PLACEHOLDER_RE.fullmatch(name) for name in placeholders):
        raise PromptRegistryError("allowed_placeholders 包含非法名称")
    if not placeholders[0].endswith("_json"):
        raise PromptRegistryError("user 模板 placeholder 必须表示规范 JSON")


def _template_fields(template: str, label: str) -> list[str]:
    fields: list[str] = []
    try:
        parsed = Formatter().parse(template)
        for _, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if not _PLACEHOLDER_RE.fullmatch(field_name) or format_spec or conversion:
                raise PromptRegistryError(f"模板包含非法 placeholder: {label}")
            fields.append(field_name)
    except ValueError as exc:
        raise PromptRegistryError(f"模板花括号无效: {label}") from exc
    return fields


def _read_controlled_file(root: Path, relative_name: str, max_bytes: int) -> bytes:
    if root.is_symlink():
        raise PromptRegistryError(f"受控根目录不得是 symlink: {root.name}")
    try:
        root_resolved = root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise PromptRegistryError(f"受控根目录不存在: {root}") from exc
    if not root_resolved.is_dir():
        raise PromptRegistryError(f"受控根路径不是目录: {root}")

    pure = PurePosixPath(relative_name)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise PromptRegistryError(f"拒绝越界路径: {relative_name}")
    if "\\" in relative_name:
        raise PromptRegistryError(f"拒绝非 POSIX 路径: {relative_name}")

    candidate = root.joinpath(*pure.parts)
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise PromptRegistryError(f"拒绝 symlink: {relative_name}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise PromptRegistryError(f"文件不存在或越界: {relative_name}") from exc

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise PromptRegistryError(f"无法安全打开文件: {relative_name}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PromptRegistryError(f"受控路径不是普通文件: {relative_name}")
        if info.st_size == 0:
            raise PromptRegistryError(f"受控文件为空: {relative_name}")
        if info.st_size > max_bytes:
            raise PromptRegistryError(f"受控文件超过大小上限: {relative_name}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > max_bytes:
        raise PromptRegistryError(f"受控文件超过大小上限: {relative_name}")
    return raw


def _decode_nonempty(raw: bytes, label: str) -> str:
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PromptRegistryError(f"文件不是有效 UTF-8: {label}") from exc
    if not value.strip():
        raise PromptRegistryError(f"文件没有有效内容: {label}")
    return value


def _require_hash(raw: bytes, expected: str, label: str) -> None:
    actual = _sha256(raw)
    if actual != expected:
        raise PromptRegistryError(f"SHA-256 不匹配: {label}")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
