#!/usr/bin/env python3
"""从 Pydantic 模型同步 AI Schema 及 Prompt registry 摘要。"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any

import yaml

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = WORKSPACE_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.analysis.ai_models import (  # noqa: E402
    AI_MODEL_REGISTRY,
    AI_SCHEMA_MODELS,
    SchemaSerialization,
)

PROMPTS_ROOT = WORKSPACE_ROOT / "prompts"
SCHEMAS_ROOT = WORKSPACE_ROOT / "schemas"
REGISTRY_PATH = PROMPTS_ROOT / "registry.yaml"
_REGISTRY_HEADER = (
    "# Prompt 只按精确 id/version 解析，不存在隐式版本 fallback。\n"
    "# template_sha256 与 schema_sha256 均计算对应文件提交后原始 bytes 的 SHA-256。\n"
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_registry() -> dict[str, Any]:
    document = yaml.safe_load(REGISTRY_PATH.read_bytes())
    if not isinstance(document, dict) or not isinstance(document.get("prompts"), list):
        raise ValueError("prompts/registry.yaml 缺少 prompts 列表")
    return document


def _expected_schema_bytes() -> dict[str, bytes]:
    return {
        filename: SchemaSerialization.bytes_for(model)
        for filename, model in sorted(AI_SCHEMA_MODELS.items())
    }


def _expected_registry_bytes(schema_bytes: dict[str, bytes]) -> bytes:
    document = _load_registry()
    seen: set[tuple[str, str]] = set()
    for definition in document["prompts"]:
        if not isinstance(definition, dict):
            raise ValueError("registry prompt 条目必须是对象")
        prompt_id = str(definition["id"])
        version = str(definition["version"])
        key = (prompt_id, version)
        if key in seen:
            raise ValueError(f"重复 Prompt: {prompt_id}@{version}")
        seen.add(key)

        expected_system = f"{prompt_id}/{version}/system.md"
        expected_user = f"{prompt_id}/{version}/user.md"
        if definition.get("system_file") != expected_system or definition.get("user_file") != expected_user:
            raise ValueError(f"Prompt 路径未精确匹配版本目录: {prompt_id}@{version}")

        input_name = str(definition["input_model"])
        output_name = str(definition["output_model"])
        input_file = str(definition["input_schema_file"])
        output_file = str(definition["output_schema_file"])
        for model_name, schema_file in ((input_name, input_file), (output_name, output_file)):
            model = AI_MODEL_REGISTRY.get(model_name)
            schema_model = AI_SCHEMA_MODELS.get(schema_file)
            if model is None or schema_model is not model:
                raise ValueError(f"{schema_file} 未由声明的 Pydantic 模型 {model_name} 生成")
            if schema_file not in schema_bytes:
                raise ValueError(f"未知 AI Schema: {schema_file}")

        system_raw = (PROMPTS_ROOT / expected_system).read_bytes()
        user_raw = (PROMPTS_ROOT / expected_user).read_bytes()
        definition["template_sha256"] = {
            "system": _sha256(system_raw),
            "user": _sha256(user_raw),
        }
        definition["schema_sha256"] = {
            "input": _sha256(schema_bytes[input_file]),
            "output": _sha256(schema_bytes[output_file]),
        }

    dumped = yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )
    return (_REGISTRY_HEADER + dumped).encode("utf-8")


def _differences(schema_bytes: dict[str, bytes], registry_bytes: bytes) -> list[Path]:
    drift: list[Path] = []
    for filename, expected in schema_bytes.items():
        path = SCHEMAS_ROOT / filename
        if not path.is_file() or path.read_bytes() != expected:
            drift.append(path)
    if not REGISTRY_PATH.is_file() or REGISTRY_PATH.read_bytes() != registry_bytes:
        drift.append(REGISTRY_PATH)
    return drift


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="写入稳定生成的 AI Schema 与 registry 摘要")
    mode.add_argument("--check", action="store_true", help="检查已提交文件是否与生成结果一致")
    args = parser.parse_args()

    schema_bytes = _expected_schema_bytes()
    registry_bytes = _expected_registry_bytes(schema_bytes)
    if args.write:
        for filename, raw in schema_bytes.items():
            (SCHEMAS_ROOT / filename).write_bytes(raw)
        REGISTRY_PATH.write_bytes(registry_bytes)
        return 0

    drift = _differences(schema_bytes, registry_bytes)
    if drift:
        for path in drift:
            print(f"out of sync: {path.relative_to(WORKSPACE_ROOT)}", file=sys.stderr)
        print("run scripts/sync-ai-protocol.py --write", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
