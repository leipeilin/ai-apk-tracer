"""从 DEX 反编译伪源码提取结构信息并构建 SQLite 代码索引。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from app.analysis.index_store import SCHEMA_VERSION, SQLiteCodeIndexWriter

SOURCE_SUFFIXES = {".java", ".kt", ".smali"}
# 手动同步点（v2026-08-09）：调用者身份校验 API 核心集，与
# rules/shared/dataflow.py GUARD_METHODS 同源（本集合是它的"调用者校验"子集，
# 不含 enforceReadPermission/enforceWritePermission 等资源权限强制 API）。
# 一致性由 tests/test_guard_call_check_consistency.py 参数化测试强制。
GUARD_CALLER_CHECK_METHODS = frozenset({
    "checkCallingPermission", "enforceCallingPermission", "checkCallingOrSelfPermission",
    "enforceCallingOrSelfPermission", "checkSignatures", "checkUidSignatures",
    "getNameForUid", "getPackageInfo",
})
SYMBOL_RE = re.compile(r"\b(?:class|interface|enum|object|fun|void|public|private|protected)\s+([A-Za-z_$][\w$]*)")
CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
CALL_SITE_RE = re.compile(
    r"(?:(?P<receiver>[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*\.\s*)?"
    r"(?P<method>[A-Za-z_$][\w$]*)\s*\("
)
PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)", re.MULTILINE)
IMPORT_RE = re.compile(r"^\s*import\s+([\w.*$]+)", re.MULTILINE)
CLASS_RE = re.compile(
    r"\b(class|interface|enum|object)\s+([A-Za-z_$][\w$]*)"
    r"(?:\s+extends\s+([\w.$<>]+))?"
    r"(?:\s+implements\s+([^\{]+))?"
    r"(?:\s*:\s*([^\{]+))?\s*\{?"
)
METHOD_RE = re.compile(
    r"^[ \t]*(?:(?:@[\w.]+(?:\([^)]*\))?)[ \t\r\n]*)*"
    r"(?:(?:public|protected|private|static|final|abstract|synchronized|native|default|open|override|internal|suspend|inline|operator|infix|external)[ \t]+)*"
    r"(?:fun[ \t]+)?(?:[\w.$<>\[\]?,]+[ \t]+)?(?:[\w.$<>\[\]?,]+\.)?"
    r"([A-Za-z_$][\w$]*)[ \t\r\n]*\(([^;{}]*?)\)[ \t\r\n]*"
    r"(?::[ \t\r\n]*[^\{=]+)?[ \t\r\n]*(\{|=)?",
    re.MULTILINE,
)
SMALI_METHOD_RE = re.compile(r"^\s*\.method\s+(.+?)\s+([\w$<>-]+)\((.*?)\)(\S+)")
CONTROL_WORDS = {"if", "for", "while", "switch", "catch", "when", "return", "throw", "new", "super", "this", "synchronized"}

# Decompiler output frequently omits imports. These stable SDK/library type names provide
# deterministic type resolution; a class declared in the same file still takes precedence.
_COMMON_PLATFORM_TYPES = {
    "Activity": "android.app.Activity", "Application": "android.app.Application",
    "Service": "android.app.Service", "Context": "android.content.Context",
    "ContextWrapper": "android.content.ContextWrapper", "Intent": "android.content.Intent",
    "IntentFilter": "android.content.IntentFilter", "BroadcastReceiver": "android.content.BroadcastReceiver",
    "ContentProvider": "android.content.ContentProvider", "ContentResolver": "android.content.ContentResolver",
    "ContentValues": "android.content.ContentValues", "ContentProviderOperation": "android.content.ContentProviderOperation",
    "ContentProviderResult": "android.content.ContentProviderResult", "Uri": "android.net.Uri",
    "Bundle": "android.os.Bundle", "PersistableBundle": "android.os.PersistableBundle",
    "Parcel": "android.os.Parcel", "ParcelFileDescriptor": "android.os.ParcelFileDescriptor",
    "CancellationSignal": "android.os.CancellationSignal", "Handler": "android.os.Handler",
    "Cursor": "android.database.Cursor", "MatrixCursor": "android.database.MatrixCursor",
    "SQLiteDatabase": "android.database.sqlite.SQLiteDatabase", "WebView": "android.webkit.WebView",
    "SensorManager": "android.hardware.SensorManager", "LocationManager": "android.location.LocationManager",
    "BluetoothGatt": "android.bluetooth.BluetoothGatt", "BluetoothSocket": "android.bluetooth.BluetoothSocket",
    "Fragment": "android.app.Fragment", "LiveData": "androidx.lifecycle.LiveData",
    "MutableLiveData": "androidx.lifecycle.MutableLiveData", "ContextCompat": "androidx.core.content.ContextCompat",
    "LocalBroadcastManager": "androidx.localbroadcastmanager.content.LocalBroadcastManager",
    "FragmentFactory": "androidx.fragment.app.FragmentFactory", "NavController": "androidx.navigation.NavController",
    "File": "java.io.File", "FileInputStream": "java.io.FileInputStream",
    "FileOutputStream": "java.io.FileOutputStream", "ArrayList": "java.util.ArrayList",
    "Map": "java.util.Map", "StringBuilder": "java.lang.StringBuilder",
}


def build_code_index(
    source_root: Path,
    output_path: Path,
    max_file_size_kb: int = 512,
    component_max_file_size_kb: int | None = None,
    priority_component_fqcns: set[str] | None = None,
) -> dict[str, Any]:
    """索引 Java、Kotlin 与 Smali，并只对 Manifest 组件放宽文件上限。"""

    started_at = time.monotonic()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    database_path = output_path.parent / "analysis.sqlite3"
    writer = SQLiteCodeIndexWriter(database_path)
    max_bytes = max_file_size_kb * 1024
    component_max_bytes = (component_max_file_size_kb or max_file_size_kb) * 1024
    priority_component_paths = {
        fqcn.split("$", 1)[0].replace(".", "/")
        for fqcn in (priority_component_fqcns or set())
        if fqcn
    }
    skipped_files: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    stats = {
        "file_count": 0,
        "skipped_file_count": 0,
        "class_count": 0,
        "method_count": 0,
        "call_site_count": 0,
    }
    try:
        for path in sorted(source_root.rglob("*")):
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            relative = path.relative_to(source_root).as_posix()
            size = path.stat().st_size
            relative_without_suffix = relative.removesuffix(path.suffix)
            component_related = relative_without_suffix.split("$", 1)[0] in priority_component_paths
            content: str | None = None
            if not component_related and max_bytes < size <= component_max_bytes:
                content = path.read_text("utf-8", errors="replace")
                component_related = _file_declares_priority_component(
                    content, priority_component_fqcns or set()
                )
            effective_max_bytes = component_max_bytes if component_related else max_bytes
            if size > effective_max_bytes:
                writer.add_skipped(relative, size, "FILE_SIZE_LIMIT")
                skipped_files.append({
                    "path": relative,
                    "size_bytes": size,
                    "reason": "FILE_SIZE_LIMIT",
                    "component_related": component_related,
                })
                stats["skipped_file_count"] += 1
                continue
            content = content if content is not None else path.read_text("utf-8", errors="replace")
            structure = _extract_structure(relative, content, path.suffix.lower())
            file = {
                "path": relative,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "line_count": content.count("\n") + 1,
                "package": structure["package"],
                "imports": structure["imports"],
                "symbols": sorted(set(SYMBOL_RE.findall(content))),
                "calls": structure["calls"],
                "classes": structure["classes"],
                "methods": structure["methods"],
                "content": content,
            }
            writer.add_file(file, size)
            stats["file_count"] += 1
            stats["class_count"] += len(file["classes"])
            stats["method_count"] += len(file["methods"])
            stats["call_site_count"] += sum(len(method.get("call_sites", [])) for method in file["methods"])
            digest.update(relative.encode())
            digest.update(file["sha256"].encode())
        writer.finish({"root": source_root.as_posix(), "stats": stats, "content_sha256": digest.hexdigest()})
    except Exception:
        writer.db.close()
        if database_path.exists():
            database_path.unlink()
        raise
    stats["index_build_seconds"] = round(time.monotonic() - started_at, 3)
    stats["database_size_bytes"] = database_path.stat().st_size
    descriptor = {
        "schema_version": SCHEMA_VERSION,
        "type": "sqlite",
        "root": source_root.resolve().as_posix(),
        "database_path": database_path.resolve().as_posix(),
        "database_relative_path": database_path.relative_to(output_path.parent.parent).as_posix(),
        "stats": stats,
        "skipped_files": skipped_files,
        "content_sha256": digest.hexdigest(),
    }
    output_path.write_text(json.dumps(descriptor, ensure_ascii=False, indent=2), "utf-8")
    output_path.chmod(0o600)
    return descriptor


def _file_declares_priority_component(content: str, priority_fqcns: set[str]) -> bool:
    """用包名和声明类名识别非约定文件名中的 Manifest 组件。"""

    if not priority_fqcns:
        return False
    package_match = PACKAGE_RE.search(content)
    package = package_match.group(1) if package_match else ""
    in_block_comment = False
    for line in content.splitlines():
        sanitized, in_block_comment = _strip_comments_and_strings(line, in_block_comment)
        match = CLASS_RE.search(sanitized)
        if not match:
            continue
        declared = f"{package}.{match.group(2)}" if package else match.group(2)
        if any(
            fqcn == declared or fqcn.startswith(f"{declared}$")
            for fqcn in priority_fqcns
        ):
            return True
    return False


def _extract_structure(path: str, content: str, suffix: str) -> dict[str, Any]:
    """从 Java、Kotlin 或 Smali 伪源码中保守提取类、方法与调用结构。

    Java/Kotlin 先生成与原文等长的 masked 文本定位声明和括号，再用相同 offset 回到 raw
    文本恢复参数、字符串 key 与证据行；无法配对的方法体或调用会跳过而非猜测。Smali 仅按
    descriptor 建立参数事实，并显式保留 register-flow 未证明的 critical limitation。
    """

    if suffix == ".smali":
        return _extract_smali_structure(path, content)
    lines = content.splitlines()
    package_match = PACKAGE_RE.search(content)
    package = package_match.group(1) if package_match else ""
    imports = sorted(set(IMPORT_RE.findall(content)))
    # 类声明只从可执行文本提取，避免 JADX 注释中的 ``class name`` 等说明被误当成类型。
    sanitized_lines: list[str] = []
    in_block_comment = False
    for line in lines:
        sanitized, in_block_comment = _strip_comments_and_strings(line, in_block_comment)
        sanitized_lines.append(sanitized)

    classes: list[dict[str, Any]] = []
    for index, line in enumerate(sanitized_lines):
        match = CLASS_RE.search(line)
        if not match:
            continue
        class_name = match.group(2)
        kotlin_parents = match.group(5) or ""
        extends = _clean_type(match.group(3)) or _first_kotlin_parent(kotlin_parents)
        implements = _split_types(match.group(4) or "")
        if kotlin_parents:
            kotlin_types = _split_types(kotlin_parents)
            if extends and kotlin_types and kotlin_types[0] == extends:
                kotlin_types = kotlin_types[1:]
            implements.extend(item for item in kotlin_types if item not in implements and item != extends)
        end_line = _brace_end(lines, index) if "{" in "\n".join(sanitized_lines[index:index + 3]) else len(lines)
        classes.append({
            "id": f"{path}#{class_name}:{index + 1}",
            "name": class_name,
            "kind": match.group(1),
            "extends": extends,
            "implements": implements,
            "start_line": index + 1,
            "end_line": end_line,
        })

    # JADX 生成的 AIDL Stub 大量使用 ``Outer.a``。必须保留 lexical owner，不能把同包
    # 多个内部类都压扁成 ``package.a``，否则 Binder 返回类型会产生伪歧义。
    for class_info in sorted(classes, key=lambda item: (item["start_line"], -item["end_line"])):
        owners = [
            item for item in classes
            if item is not class_info
            and item["start_line"] < class_info["start_line"] <= item["end_line"]
        ]
        owner = min(owners, key=lambda item: item["end_line"] - item["start_line"]) if owners else None
        owner_local_name = owner.get("local_name") if owner else None
        local_name = f"{owner_local_name}.{class_info['name']}" if owner_local_name else class_info["name"]
        class_info["owner"] = owner.get("qualified_name") if owner else None
        class_info["local_name"] = local_name
        class_info["qualified_name"] = f"{package}.{local_name}" if package else local_name
        class_info["binary_name"] = class_info["qualified_name"].replace(
            f"{package}.{owner_local_name}.", f"{package}.{owner_local_name}$", 1
        ) if package and owner_local_name else class_info["qualified_name"]

    language = "kotlin" if suffix == ".kt" else "java"
    local_types = {str(item["name"]): str(item["qualified_name"]) for item in classes}
    file_type_environment = _type_environment(
        [], content, package, imports, "", local_types=local_types
    )
    file_type_environment.update(local_types)
    masked_content_parts: list[str] = []
    in_block_comment = False
    for raw_line in content.splitlines(keepends=True):
        masked_line, in_block_comment = _strip_comments_and_strings_preserve(raw_line, in_block_comment)
        masked_content_parts.append(masked_line)
    masked_content = "".join(masked_content_parts)
    methods: list[dict[str, Any]] = []
    for match in METHOD_RE.finditer(masked_content):
        name = content[match.start(1):match.end(1)]
        start_line = content.count("\n", 0, match.start(1)) + 1
        index = start_line - 1
        opening = masked_content.find("(", match.end(1))
        closing = _matching_paren(masked_content, opening) if opening >= 0 else None
        body_marker = _method_body_marker(masked_content, closing + 1) if closing is not None else None
        if body_marker is None:
            continue
        declaration = content[match.start():body_marker + 1]
        header = content[content.rfind("\n", 0, opening) + 1:opening]
        if (
            name in CONTROL_WORDS
            or declaration.lstrip().startswith(("class ", "interface ", "enum ", "object "))
            or ("fun" not in header and re.search(rf"\.\s*{re.escape(name)}\s*$", header))
        ):
            continue
        parameters_text = content[opening + 1:closing].strip()
        end_line = _brace_end(lines, index) if masked_content[body_marker] == "{" else start_line
        class_info = _containing_class(classes, start_line)
        snippet = "\n".join(lines[index:end_line])
        method_id = f"{path}#{(class_info['name'] + '.') if class_info else ''}{name}:{start_line}"
        raw_executable, masked_executable = _method_texts(snippet)
        qualified_class = class_info["qualified_name"] if class_info else f"{package}.<file>" if package else f"{path}.<file>"
        structured_parameters = parse_structured_parameters(
            parameters_text,
            language=language,
            method_name=name,
            package=package,
            imports=imports,
            local_types=local_types,
        )
        descriptor = _source_method_descriptor(declaration, name, structured_parameters, class_info)
        receiver_type = _kotlin_receiver_type(declaration, name) if language == "kotlin" else None
        type_environment = {
            **file_type_environment,
            **_type_environment(
                structured_parameters,
                snippet,
                package,
                imports,
                qualified_class,
                receiver_type=receiver_type,
                local_types=local_types,
            ),
        }
        call_sites = _extract_call_sites(
            method_id, raw_executable, masked_executable, start_line,
            type_environment, package, imports, qualified_class,
        )
        flow_ir = _build_flow_ir(raw_executable, masked_executable, start_line, call_sites)
        calls = sorted({call["method_name"] for call in call_sites if call["method_name"] != name})
        class_name = class_info["name"] if class_info else None
        methods.append({
            "id": method_id,
            "name": name,
            "class_name": class_name,
            "qualified_class": qualified_class,
            "signature": re.sub(r"\s+", " ", declaration).strip()[:500],
            "descriptor": descriptor,
            "symbol_key": f"{qualified_class}#{name}{descriptor}",
            "parameters": parameters_text,
            "structured_parameters": structured_parameters,
            "source_language": language,
            "smali_descriptor_only": False,
            "start_line": start_line,
            "end_line": end_line,
            "calls": calls,
            "call_sites": call_sites,
            "flow_ir": flow_ir,
            "summary": {
                **_build_method_summary(structured_parameters, raw_executable, call_sites, flow_ir),
                "source_language": language,
                "smali_descriptor_only": False,
            },
        })
    all_calls = sorted({call for method in methods for call in method["calls"]})
    return {"package": package, "imports": imports, "classes": classes, "methods": methods, "calls": all_calls}


def _extract_smali_structure(path: str, content: str) -> dict[str, Any]:
    """解析 Smali 类声明、方法边界及 invoke 目标名称。"""

    lines = content.splitlines()
    class_name = None
    super_name = None
    interfaces: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(".class "):
            class_name = _smali_type(stripped.split()[-1])
        elif stripped.startswith(".super "):
            super_name = _smali_type(stripped.split()[-1])
        elif stripped.startswith(".implements "):
            interfaces.append(_smali_type(stripped.split()[-1]))
    simple_name = class_name.rsplit(".", 1)[-1] if class_name else Path(path).stem
    classes = [{
        "id": f"{path}#{simple_name}:1",
        "name": simple_name,
        "qualified_name": class_name or simple_name,
        "kind": "class",
        "extends": super_name,
        "implements": interfaces,
        "start_line": 1,
        "end_line": len(lines),
    }]
    methods: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        match = SMALI_METHOD_RE.match(lines[index])
        if not match:
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not lines[end].lstrip().startswith(".end method"):
            end += 1
        end_line = min(end + 1, len(lines))
        snippet = "\n".join(lines[index:end_line])
        name = match.group(2)
        method_id = f"{path}#{simple_name}.{name}:{index + 1}"
        call_sites = _extract_smali_call_sites(method_id, snippet, index + 1)
        flow_ir = [
            {"op": "call", "ordinal": call["ordinal"], "line": call["start_line"]}
            for call in call_sites
        ]
        calls = sorted({call["method_name"] for call in call_sites})
        descriptor = f"({_smali_descriptor_types(match.group(3))})->{_smali_descriptor_type(match.group(4))}"
        qualified_class = class_name or simple_name
        is_static = "static" in match.group(1).split()
        structured_parameters = parse_structured_parameters(
            match.group(3),
            language="smali",
            method_name=name,
            smali_static=is_static,
        )
        limitation = {
            "code": "SMALI_REGISTER_FLOW_UNPROVEN",
            "critical": True,
            "reason": "Smali parameters are indexed from descriptors; register flow is not SSA-proven.",
        }
        summary = _build_method_summary(structured_parameters, snippet, call_sites, flow_ir)
        summary["source_language"] = "smali"
        summary["smali_descriptor_only"] = True
        summary["coverage"] = {"parameter_register_flow": "unproven"}
        summary["limitations"] = [limitation]
        methods.append({
            "id": method_id,
            "name": name,
            "class_name": simple_name,
            "qualified_class": qualified_class,
            "signature": lines[index].strip()[:500],
            "descriptor": descriptor,
            "symbol_key": f"{qualified_class}#{name}{descriptor}",
            "parameters": match.group(3),
            "structured_parameters": structured_parameters,
            "source_language": "smali",
            "smali_descriptor_only": True,
            "coverage": {"parameter_register_flow": "unproven"},
            "limitations": [limitation],
            "start_line": index + 1,
            "end_line": end_line,
            "calls": calls,
            "call_sites": call_sites,
            "flow_ir": flow_ir,
            "summary": summary,
        })
        index = end_line
    return {
        "package": class_name.rsplit(".", 1)[0] if class_name and "." in class_name else "",
        "imports": [],
        "classes": classes,
        "methods": methods,
        "calls": sorted({call for method in methods for call in method["calls"]}),
    }


def _build_method_summary(
    structured_parameters: list[dict[str, Any]],
    executable: str,
    call_sites: list[dict[str, Any]],
    flow_ir: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成供跨方法传播使用的 v2 本地符号摘要。"""

    parameters = [
        str(parameter["name"])
        for parameter in structured_parameters
        if parameter.get("name")
    ]
    returns = re.findall(r"\breturn\s+([^;]+)", executable)
    parameter_to_return = [
        parameter for parameter in parameters
        if any(re.search(rf"\b{re.escape(parameter)}\b", value) for value in returns)
    ]
    sink_names = {
        "loadUrl", "evaluateJavascript", "startActivity", "startService", "startForegroundService",
        "sendBroadcast", "sendOrderedBroadcast", "execSQL", "rawQuery", "insert", "update", "delete",
        "requestLocationUpdates", "registerListener", "startForeground",
    }
    parameter_to_sink = []
    for call in call_sites:
        if call["method_name"] not in sink_names:
            continue
        used = [
            parameter for parameter in parameters
            if any(re.search(rf"\b{re.escape(parameter)}\b", argument) for argument in call.get("arguments", []))
        ]
        if used:
            parameter_to_sink.append({
                "parameters": used,
                "method_name": call["method_name"],
                "line": call["start_line"],
            })
    # 手动同步点（v2026-08-09）：引用模块级 GUARD_CALLER_CHECK_METHODS，
    # 与 rules/shared/dataflow.py GUARD_METHODS 同源；getCallingUid/getCallingPid
    # 是身份来源（IDENTITY_SOURCE_METHODS），一并标记。
    guard_names = {*GUARD_CALLER_CHECK_METHODS, "getCallingUid"}
    side_effect_names = {
        "startForeground", "requestLocationUpdates", "registerListener", "startSport", "pauseSport",
        "resumeSport", "finishSport", "stopSelf", "sendBroadcast", "notify",
    }
    parameter_slot_mutations = []
    for call in call_sites:
        receiver = str(call.get("receiver_text") or "").strip()
        if receiver not in parameters or call.get("method_name") not in {
            "putExtra", "putExtras", "putAll", "putString", "putInt", "putLong",
            "putBoolean", "putParcelable", "replaceExtras", "fillIn",
        }:
            continue
        arguments = call.get("arguments", [])
        parameter_slot_mutations.append({
            "parameter": receiver,
            "operation": call["method_name"],
            "key": _literal_key(arguments[0]) if arguments else None,
            "ordinal": call.get("ordinal"),
        })
    return {
        "version": 2,
        "parameters": parameters,
        "parameter_to_return": parameter_to_return,
        "parameter_to_sink": parameter_to_sink,
        "parameter_slot_mutations": parameter_slot_mutations,
        "returns": [item for item in flow_ir if item.get("op") == "return"],
        "field_reads": sorted(set(re.findall(r"\bthis\.([A-Za-z_$][\w$]*)\b", executable))),
        "field_writes": sorted(set(re.findall(r"(?:this\.)?([A-Za-z_$][\w$]*)\s*=", executable))),
        "guards": [call for call in call_sites if call["method_name"] in guard_names],
        "side_effects": [call for call in call_sites if call["method_name"] in side_effect_names],
    }


def _method_texts(snippet: str) -> tuple[str, str]:
    """返回等长 raw/masked 方法体；仅 masked 文本参与语法位置匹配。"""

    raw = list(snippet)
    opening = snippet.find("{")
    declaration_end = opening if opening >= 0 else snippet.find("=")
    if declaration_end >= 0:
        for index in range(declaration_end + 1):
            if raw[index] != "\n":
                raw[index] = " "
    raw_text = "".join(raw)
    sanitized_lines = []
    in_block_comment = False
    for line in raw_text.splitlines(keepends=True):
        sanitized, in_block_comment = _strip_comments_and_strings_preserve(line, in_block_comment)
        sanitized_lines.append(sanitized)
    return raw_text, "".join(sanitized_lines)


def _extract_call_sites(
    method_id: str,
    raw_text: str,
    masked_text: str,
    start_line: int,
    type_environment: dict[str, str],
    package: str,
    imports: list[str],
    qualified_class: str,
) -> list[dict[str, Any]]:
    """在 masked 文本定位调用，并从同 offset 的 raw 文本恢复参数与 key。"""

    calls: list[dict[str, Any]] = []
    for match in CALL_SITE_RE.finditer(masked_text):
        method_name = match.group("method")
        if method_name in CONTROL_WORDS:
            continue
        open_paren = match.end() - 1
        close_paren = _matching_paren(masked_text, open_paren)
        if close_paren is None:
            continue
        arguments = _split_arguments(raw_text[open_paren + 1:close_paren])
        assigned_to = _outer_assignment_target(masked_text, match.start(), close_paren)
        line = start_line + masked_text.count("\n", 0, match.start())
        end_line = start_line + masked_text.count("\n", 0, close_paren)
        receiver = re.sub(r"\s+", "", match.group("receiver") or "") or _recover_chained_receiver(
            raw_text, masked_text, match.start("method")
        )
        receiver_type = _resolve_receiver_type(
            receiver, type_environment, package, imports, qualified_class
        )
        argument_types = [
            _expression_type(argument, type_environment, package, imports)
            for argument in arguments
        ]
        calls.append({
            "id": None,
            "ordinal": 0,
            "receiver_text": receiver,
            "receiver_type": receiver_type,
            "method_name": method_name,
            "method_descriptor": f"({','.join(argument_types)})->?",
            "resolved_target_id": None,
            "resolve_status": "pending",
            "arguments": arguments,
            "assigned_to": assigned_to,
            "start_line": line,
            "end_line": end_line,
            "expression_kind": "constructor" if masked_text[max(0, match.start() - 5):match.start()].strip().endswith("new") else "invoke",
            "_offset": match.start(),
            "_end_offset": close_paren + 1,
        })
    # 调用按表达式求值完成位置排序：嵌套参数调用先于外层调用，跨语句仍保持源码顺序。
    calls.sort(key=lambda item: (int(item["_end_offset"]), int(item["_offset"])))
    for ordinal, call in enumerate(calls, start=1):
        call["ordinal"] = ordinal
        call["id"] = f"{method_id}@call:{ordinal}:{call['start_line']}"
    return calls


def _outer_assignment_target(masked_text: str, call_start: int, call_end: int) -> str | None:
    """仅把赋值绑定到最外层返回调用，链中间调用不获得 assigned_to。"""

    suffix = masked_text[call_end:]
    if re.match(r"\s*\.\s*[A-Za-z_$][\w$]*\s*\(", suffix):
        return None
    statement_start = max(
        masked_text.rfind(";", 0, call_start),
        masked_text.rfind("\n", 0, call_start),
        masked_text.rfind("{", 0, call_start),
    ) + 1
    prefix = masked_text[statement_start:call_start]
    assigned = re.search(r"\b([A-Za-z_$][\w$]*)\s*=\s*(?:[\w.$<>?\[\]]+\s+)?[^=]*$", prefix)
    return assigned.group(1) if assigned else None


def _recover_chained_receiver(raw_text: str, masked_text: str, method_start: int) -> str | None:
    index = method_start - 1
    while index >= 0 and masked_text[index].isspace():
        index -= 1
    if index < 0 or masked_text[index] != ".":
        return None
    end = index
    depth = 0
    index -= 1
    while index >= 0:
        char = masked_text[index]
        if char in ")]}":
            depth += 1
        elif char in "([{":
            if depth:
                depth -= 1
            else:
                break
        elif depth == 0 and char in ";=,+-*/!?:{}\n":
            break
        index -= 1
    value = raw_text[index + 1:end].strip()
    return re.sub(r"\s+", "", value) or None


def _build_flow_ir(
    raw_text: str,
    masked_text: str,
    start_line: int,
    call_sites: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """生成按源码 offset 排序的轻量有序 IR。"""

    events: list[tuple[int, int, dict[str, Any]]] = []
    for call in call_sites:
        events.append((int(call["_end_offset"]), 1, {
            "op": "call", "ordinal": call["ordinal"], "line": call["start_line"],
        }))
    assignment_re = re.compile(r"(?<![=!<>])\b([A-Za-z_$][\w$]*)\s*=\s*(?!=)([^;\n]+)")
    for match in assignment_re.finditer(masked_text):
        left = match.group(1)
        expression = raw_text[match.start(2):match.end(2)].strip()
        related = [
            call for call in call_sites
            if call.get("assigned_to") == left
            and match.start(2) <= int(call["_offset"]) < match.end(2)
        ]
        events.append((match.end(), 2, {
            "op": "assign",
            "target": left,
            "expression": expression,
            "from_call_ordinal": related[-1]["ordinal"] if related else None,
            "line": start_line + masked_text.count("\n", 0, match.start()),
        }))
    for match in re.finditer(r"\breturn\s+([^;\n}]+)", masked_text):
        related = [
            call for call in call_sites
            if match.start(1) <= int(call.get("_offset", -1)) < match.end(1)
        ]
        # Java/Kotlin 先求值 return 表达式中的调用，再执行返回。
        events.append((match.end(1), 3, {
            "op": "return",
            "expression": raw_text[match.start(1):match.end(1)].strip(),
            "from_call_ordinal": related[-1]["ordinal"] if related else None,
            "line": start_line + masked_text.count("\n", 0, match.start()),
        }))
    for match in re.finditer(r"\b(?:if|while|for|switch|when|catch)\s*\(", masked_text):
        opening = masked_text.find("(", match.start(), match.end())
        closing = _matching_paren(masked_text, opening) if opening >= 0 else None
        if closing is None:
            continue
        following = masked_text[closing + 1:closing + 301]
        block_end_line = _branch_block_end_line(masked_text, closing + 1, start_line)
        events.append((closing + 1, 0, {
            "op": "branch_hint",
            "condition": raw_text[opening + 1:closing].strip(),
            "fail_closed": bool(re.match(
                r"\s*(?:\{\s*)?(?:return\b|throw\s+new\b|[^{};]+;\s*(?:return\b|throw\s+new\b))",
                following,
                re.S,
            )),
            "line": start_line + masked_text.count("\n", 0, match.start()),
            # P0-1：分支作用域末行。None 表示无法可靠推断（消费方须按"作用域未解析"
            # 保守处理并产出 CONTROL_SCOPE_UNRESOLVED gap），不得当作"无作用域限制"。
            "block_end_line": block_end_line,
        }))
    result = [event for _, _, event in sorted(events, key=lambda item: (item[0], item[1]))]
    for call in call_sites:
        call.pop("_offset", None)
        call.pop("_end_offset", None)
    return result


def _literal_key(expression: str) -> str | None:
    match = re.fullmatch(r"\s*[\"']([^\"']*)[\"']\s*", expression or "")
    return match.group(1) if match else None


def _extract_smali_call_sites(method_id: str, snippet: str, start_line: int) -> list[dict[str, Any]]:
    """将 Smali invoke 指令转换为与 Java/Kotlin 一致的调用点事实。"""

    calls = []
    pattern = re.compile(r"invoke-\w+(?:/range)?\s+\{([^}]*)\},\s+L([^;]+);->([\w$<>-]+)\(([^)]*)\)(\S+)")
    for index, match in enumerate(pattern.finditer(snippet)):
        line = start_line + snippet.count("\n", 0, match.start())
        calls.append({
            "id": f"{method_id}@smali:{index + 1}:{line}",
            "ordinal": index + 1,
            "receiver_text": match.group(2).replace("/", "."),
            "receiver_type": match.group(2).replace("/", "."),
            "method_name": match.group(3),
            "method_descriptor": f"({_smali_descriptor_types(match.group(4))})->{_smali_descriptor_type(match.group(5))}",
            "resolved_target_id": None,
            "resolve_status": "pending",
            "arguments": [item.strip() for item in match.group(1).split(",") if item.strip()],
            "assigned_to": None,
            "start_line": line,
            "end_line": line,
            "expression_kind": "smali_invoke",
        })
    return calls


def _branch_block_end_line(masked_text: str, body_start: int, start_line: int) -> int | None:
    """推断分支体（含 else / else if 链）的末行，供数据流限定 control_fact 作用域。

    P0-1（2026-08-15）：此前 IR 不携带块边界，`control_fact` 一旦置位便持续到方法结束，
    导致"分支条件可控"被解释为"整段代码可控"——基线 run 中 98.6% 的候选由此产生。

    覆盖两种块形态：
    - 花括号块 `{ ... }`：括号配对定位末行；
    - 单语句体 `if (c) doSomething();`：以首个 `;` 结尾。

    `else` / `else if` 分支同样受同一条件支配（条件为假时执行），因此整条 if-else 链
    合并为一个作用域，避免 else 内的 sink 逃逸判定。

    返回 None 表示无法可靠推断（如括号未闭合、体被截断），调用方必须保守处理。
    """

    index = body_start
    length = len(masked_text)
    while index < length and masked_text[index] in " \t\r\n":
        index += 1
    if index >= length:
        return None

    if masked_text[index] == "{":
        depth = 0
        end_index = None
        for cursor in range(index, length):
            char = masked_text[cursor]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end_index = cursor
                    break
        if end_index is None:
            return None
    else:
        # 单语句体：到分号为止。遇到块结束符先于分号出现说明体被截断，判为不可推断。
        end_index = None
        for cursor in range(index, length):
            char = masked_text[cursor]
            if char == ";":
                end_index = cursor
                break
            if char == "}":
                return None
        if end_index is None:
            return None

    # else / else if：与 if 共享同一支配条件，作用域延伸至整条链末尾。
    tail = masked_text[end_index + 1:]
    else_match = re.match(r"\s*else\b", tail)
    if else_match:
        else_body_start = end_index + 1 + else_match.end()
        nested = re.match(r"\s*if\s*\(", masked_text[else_body_start:])
        if nested:
            opening = masked_text.find("(", else_body_start)
            closing = _matching_paren(masked_text, opening) if opening >= 0 else None
            if closing is None:
                return None
            chained = _branch_block_end_line(masked_text, closing + 1, start_line)
            return chained
        chained = _branch_block_end_line(masked_text, else_body_start, start_line)
        return chained

    return start_line + masked_text.count("\n", 0, end_index)


def _matching_paren(text: str, opening: int) -> int | None:
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _method_body_marker(text: str, start: int) -> int | None:
    """定位参数列表后的方法体标记，忽略返回类型中的泛型嵌套。"""

    angle_depth = 0
    for index in range(start, min(len(text), start + 1000)):
        char = text[index]
        if char == "<":
            angle_depth += 1
        elif char == ">" and angle_depth:
            angle_depth -= 1
        elif not angle_depth and char in "{=":
            return index
        elif not angle_depth and char in ";}":
            return None
    return None


def _split_arguments(value: str) -> list[str]:
    """仅按括号层级拆分参数；调用方必须已恢复完整的 raw 参数区间。

    该轻量解析不解释泛型或语言语义，未闭合输入也不会补造参数边界；其结果只用于保守的
    调用点事实，后续 descriptor/解析状态仍决定能否作为确定性证据。
    """

    arguments: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(value):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            arguments.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail or value.strip():
        arguments.append(tail)
    return arguments


def parse_structured_parameters(
    parameters_text: str,
    *,
    language: str,
    method_name: str,
    package: str = "",
    imports: list[str] | None = None,
    smali_static: bool = False,
    local_types: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """统一解析 Java、Kotlin 与 Smali 显式参数，并标注 Android 入口角色。"""

    imports = imports or []
    if language == "smali":
        result = []
        register_index = 0 if smali_static else 1
        for position, raw_descriptor in enumerate(_smali_descriptor_parts(parameters_text)):
            normalized_type = _smali_descriptor_type(raw_descriptor)
            width = 2 if normalized_type in {"long", "double"} and not normalized_type.endswith("[]") else 1
            source_kind, source_basis = _parameter_source_role(method_name, position, normalized_type)
            result.append({
                "position": position,
                "name": f"p{register_index}",
                "declared_type": raw_descriptor,
                "normalized_type": normalized_type,
                "qualified_type": normalized_type,
                "descriptor": raw_descriptor,
                "register": f"p{register_index}",
                "register_width": width,
                "language": "smali",
                "source_language": "smali",
                "source_kind": source_kind,
                "source_basis": source_basis,
                "smali_descriptor_only": True,
            })
            register_index += width
        return result

    result = []
    for position, raw_parameter in enumerate(_split_balanced(parameters_text)):
        value = _strip_parameter_annotations(raw_parameter).strip()
        value, _ = _split_top_level_once(value, "=")
        modifiers = re.compile(r"\b(?:final|vararg|crossinline|noinline)\b")
        is_vararg = bool(re.search(r"\bvararg\b|\.\.\.", value))
        value = modifiers.sub("", value).strip()
        colon = _top_level_index(value, ":")
        if language == "kotlin" or colon >= 0:
            if colon < 0:
                name, declared_type = value, "?"
            else:
                name, declared_type = value[:colon].strip(), value[colon + 1:].strip()
            name_match = re.search(r"[A-Za-z_$][\w$]*$", name)
            name = name_match.group(0) if name_match else f"arg{position}"
            if is_vararg and declared_type != "?":
                declared_type = f"{declared_type}[]"
        else:
            name_match = re.search(r"([A-Za-z_$][\w$]*)(\s*(?:\[\s*\]\s*)*)$", value)
            if name_match:
                name = name_match.group(1)
                declared_type = value[:name_match.start(1)].strip()
                if name_match.group(2).strip():
                    declared_type += "[]" * name_match.group(2).count("[")
            else:
                name, declared_type = f"arg{position}", "?"
            declared_type = declared_type.replace("...", "[]").strip()
        normalized_type = _normalize_type(declared_type)
        qualified_type = _qualified_parameter_type(
            normalized_type, package, imports, language, local_types=local_types
        )
        descriptor = _jvm_type_descriptor(qualified_type)
        width = 2 if normalized_type in {"long", "double", "Long", "Double"} else 1
        source_kind, source_basis = _parameter_source_role(method_name, position, normalized_type)
        result.append({
            "position": position,
            "name": name,
            "declared_type": declared_type or "?",
            "normalized_type": normalized_type,
            "qualified_type": qualified_type,
            "descriptor": descriptor,
            "register": None,
            "register_width": width,
            "language": language,
            "source_language": language,
            "source_kind": source_kind,
            "source_basis": source_basis,
            "smali_descriptor_only": False,
        })
    return result


def _split_balanced(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    in_default_expression = False
    pairs = {")": "(", "]": "[", "}": "{", ">": "<"}
    for index, char in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char == "<" and not in_default_expression:
            stack.append(char)
        elif char in ")]}>" and stack and stack[-1] == pairs[char]:
            stack.pop()
        elif char == "=" and not stack:
            in_default_expression = True
        elif char == "," and not stack:
            parts.append(value[start:index].strip())
            start = index + 1
            in_default_expression = False
    tail = value[start:].strip()
    if tail or value.strip():
        parts.append(tail)
    return parts


def _strip_parameter_annotations(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "@":
            result.append(value[index])
            index += 1
            continue
        index += 1
        while index < len(value) and (value[index].isalnum() or value[index] in "_.$:"):
            index += 1
        while index < len(value) and value[index].isspace():
            index += 1
        if index < len(value) and value[index] == "(":
            closing = _matching_paren(value, index)
            index = len(value) if closing is None else closing + 1
        while index < len(value) and value[index].isspace():
            index += 1
    return "".join(result)


def _top_level_index(value: str, target: str) -> int:
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    pairs = {")": "(", "]": "[", "}": "{", ">": "<"}
    for index, char in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "([{<":
            stack.append(char)
        elif char in ")]}>" and stack and stack[-1] == pairs[char]:
            stack.pop()
        elif char == target and not stack:
            return index
    return -1


def _split_top_level_once(value: str, delimiter: str) -> tuple[str, str | None]:
    index = _top_level_index(value, delimiter)
    return (value, None) if index < 0 else (value[:index].strip(), value[index + 1:].strip())


def _source_method_descriptor(
    declaration: str,
    name: str,
    structured_parameters: list[dict[str, Any]],
    class_info: dict[str, Any] | None,
) -> str:
    parameter_types = [str(item["normalized_type"]) for item in structured_parameters]
    name_match = re.search(rf"\b{re.escape(name)}\s*\(", declaration)
    opening = declaration.find("(", name_match.start() if name_match else 0)
    closing = _matching_paren(declaration, opening) if opening >= 0 else None
    suffix = declaration[closing + 1:] if closing is not None else ""
    kotlin_return = re.match(r"\s*:\s*(.*?)(?:\{|=|$)", suffix, re.S)
    if kotlin_return:
        return_type = _normalize_type(kotlin_return.group(1))
    elif re.search(r"\bfun\b", declaration):
        return_type = "?"
    else:
        name_offset = declaration.rfind(name, 0, opening if opening >= 0 else len(declaration))
        prefix = _strip_parameter_annotations(declaration[:name_offset]).strip()
        prefix = re.sub(
            r"\b(?:public|protected|private|static|final|abstract|synchronized|native|default|open|override|internal|suspend|inline|operator|infix|external|fun)\b",
            " ",
            prefix,
        ).strip()
        return_type = _normalize_type(prefix.split()[-1]) if prefix else (
            class_info["name"] if class_info and name == class_info["name"] else "void"
        )
    return f"({','.join(parameter_types)})->{return_type}"


def _normalize_type(value: str) -> str:
    value = re.sub(r"\b(?:in|out)\s+", "", value).strip()
    result: list[str] = []
    depth = 0
    for char in value:
        if char == "<":
            depth += 1
        elif char == ">" and depth:
            depth -= 1
        elif depth == 0:
            result.append(char)
    cleaned = "".join(result).replace("?", "").replace(" ", "").strip()
    return cleaned or "?"


def _qualified_parameter_type(
    type_name: str,
    package: str,
    imports: list[str],
    language: str,
    *,
    local_types: dict[str, str] | None = None,
) -> str:
    dimensions = len(type_name) - len(type_name.rstrip("[]"))
    array_suffix = "[]" * (dimensions // 2)
    base = type_name[:-dimensions] if dimensions else type_name
    common = {
        "String": "kotlin.String" if language == "kotlin" else "java.lang.String",
        "Object": "java.lang.Object",
        "Any": "kotlin.Any",
        "Integer": "java.lang.Integer",
        "Boolean": "java.lang.Boolean" if language == "java" else "kotlin.Boolean",
        "Long": "java.lang.Long" if language == "java" else "kotlin.Long",
        "Double": "java.lang.Double" if language == "java" else "kotlin.Double",
    }
    qualified = common.get(base, _qualify_type(base, package, imports, local_types=local_types))
    return qualified + array_suffix


def _jvm_type_descriptor(type_name: str) -> str:
    dimensions = len(type_name) - len(type_name.rstrip("[]"))
    base = type_name[:-dimensions] if dimensions else type_name
    prefixes = "[" * (dimensions // 2)
    primitives = {
        "void": "V", "Unit": "V", "kotlin.Unit": "V", "boolean": "Z", "Boolean": "Z",
        "kotlin.Boolean": "Z", "byte": "B", "Byte": "B", "char": "C", "Char": "C",
        "short": "S", "Short": "S", "int": "I", "Int": "I", "long": "J", "Long": "J",
        "float": "F", "Float": "F", "double": "D", "Double": "D", "?": "?",
    }
    descriptor = primitives.get(base)
    if descriptor is None:
        descriptor = f"L{base.replace('.', '/')};"
    return prefixes + descriptor


def _parameter_source_role(method_name: str, position: int, normalized_type: str) -> tuple[str | None, str | None]:
    simple_type = normalized_type.rstrip("[]").rsplit(".", 1)[-1]
    lifecycle = {
        "onReceive": {1: ("Intent", "intent")},
        "onStartCommand": {0: ("Intent", "intent")},
        "onBind": {0: ("Intent", "intent")},
        "onNewIntent": {0: ("Intent", "intent")},
        # JADX/测试夹具会把路由入口折叠为 onCreate(Intent, Bundle)；只按位置+类型标注，
        # 不依赖混淆后变量名，也不会把标准 onCreate(Bundle) 的 saved state 当外部输入。
        "onCreate": {0: ("Intent", "intent"), 1: ("Bundle", "extras")},
    }
    expected = lifecycle.get(method_name, {}).get(position)
    if expected and simple_type == expected[0]:
        kind = expected[1]
        return kind, f"android-entrypoint-signature:{method_name}[{position}]:{simple_type}"
    provider_roles = {
        "query": [("Uri", "provider_uri"), ("String", "provider_projection"), ("String", "provider_selection"), ("String", "provider_selection_args"), ("String", "provider_sort_order")],
        "insert": [("Uri", "provider_uri"), ("ContentValues", "provider_values")],
        "update": [("Uri", "provider_uri"), ("ContentValues", "provider_values"), ("String", "provider_selection"), ("String", "provider_selection_args")],
        "delete": [("Uri", "provider_uri"), ("String", "provider_selection"), ("String", "provider_selection_args")],
        "openFile": [("Uri", "provider_uri"), ("String", "provider_mode")],
        "call": [("String", "provider_method"), ("String", "provider_argument"), ("Bundle", "provider_extras")],
        "applyBatch": [("ArrayList", "provider_operations")],
    }
    roles = provider_roles.get(method_name, [])
    if position < len(roles) and simple_type == roles[position][0]:
        kind = roles[position][1]
        return kind, f"android-provider-signature:{method_name}[{position}]:{simple_type}"
    return None, None


def _kotlin_receiver_type(declaration: str, name: str) -> str | None:
    match = re.search(
        rf"\bfun\s+(?:<[^>]+>\s*)?(.+?)\.\s*{re.escape(name)}\s*\(",
        declaration,
        re.S,
    )
    return _normalize_type(match.group(1)) if match else None


def _type_environment(
    structured_parameters: list[dict[str, Any]],
    content: str,
    package: str,
    imports: list[str],
    qualified_class: str,
    *,
    receiver_type: str | None = None,
    local_types: dict[str, str] | None = None,
) -> dict[str, str]:
    implicit_receiver = (
        _qualify_type(receiver_type, package, imports, local_types=local_types)
        if receiver_type else qualified_class
    )
    environment = {"this": implicit_receiver, "super": qualified_class}
    for parameter in structured_parameters:
        if parameter.get("name"):
            environment[str(parameter["name"])] = str(parameter.get("qualified_type") or parameter.get("normalized_type") or "?")
    java_declaration = re.compile(
        r"\b([A-Za-z_$][\w.$]*(?:\s*<[^;=()]+>)?(?:\[\])?)\s+([A-Za-z_$][\w$]*)\s*(?=[=;,])"
    )
    for type_name, variable in java_declaration.findall(content):
        if type_name not in {"return", "new", "class", "interface"}:
            environment[variable] = _qualify_type(
                _normalize_type(type_name), package, imports, local_types=local_types
            )
    for variable, type_name in re.findall(
        r"\b(?:val|var)\s+([A-Za-z_$][\w$]*)\s*:\s*([A-Za-z_$][\w.$<>?\[\]]*)",
        content,
    ):
        environment[variable] = _qualify_type(
            _normalize_type(type_name), package, imports, local_types=local_types
        )
    return environment


def _qualify_type(
    type_name: str,
    package: str,
    imports: list[str],
    *,
    local_types: dict[str, str] | None = None,
) -> str:
    base = type_name.rstrip("[]")
    if not base or base == "?" or base in {
        "void", "boolean", "byte", "char", "short", "int", "long", "float", "double",
        "String", "Object", "Integer", "Long", "Boolean", "Byte", "Character", "Short", "Float", "Double",
    }:
        return type_name
    suffix = type_name[len(base):]
    first = base.split(".", 1)[0]
    imported = next((item for item in imports if item.rsplit(".", 1)[-1] == first), None)
    if imported:
        return imported + base[len(first):] + suffix
    if first in (local_types or {}):
        return str((local_types or {})[first]) + base[len(first):] + suffix
    if base in _COMMON_PLATFORM_TYPES:
        return _COMMON_PLATFORM_TYPES[base] + suffix
    # 小写开头通常已是完整包名；大写开头的 ``Outer.Inner`` 是同包内部类相对名。
    if "." in base and first[:1].islower():
        return type_name
    return f"{package}.{base}{suffix}" if package else type_name


def _resolve_receiver_type(
    receiver: str | None,
    environment: dict[str, str],
    package: str,
    imports: list[str],
    qualified_class: str,
) -> str:
    if not receiver:
        return qualified_class
    if receiver in {"this", "super"}:
        return environment.get(receiver, qualified_class)
    leaf = receiver.rsplit(".", 1)[-1]
    if leaf in environment:
        return environment[leaf]
    root = receiver.split(".", 1)[0]
    if root in environment:
        return environment[root]
    if leaf[:1].isupper():
        return _qualify_type(receiver, package, imports)
    return ""


def _expression_type(
    expression: str,
    environment: dict[str, str],
    package: str,
    imports: list[str],
) -> str:
    value = expression.strip()
    if not value:
        return "?"
    if re.fullmatch(r"-?\d+[lL]", value):
        return "long"
    if re.fullmatch(r"-?\d+", value):
        return "int"
    if re.fullmatch(r"-?\d+\.\d+[fF]?", value):
        return "float" if value.lower().endswith("f") else "double"
    if value in {"true", "false"}:
        return "boolean"
    if value.startswith(('"', "'")):
        return "String" if value.startswith('"') else "char"
    constructed = re.match(r"new\s+([A-Za-z_$][\w.$<>]*)", value)
    if constructed:
        return _qualify_type(_normalize_type(constructed.group(1)), package, imports)
    if value in environment:
        return environment[value]
    return "?"


def _smali_descriptor_parts(descriptor: str) -> list[str]:
    parts = []
    index = 0
    while index < len(descriptor):
        start = index
        while index < len(descriptor) and descriptor[index] == "[":
            index += 1
        if index >= len(descriptor):
            break
        if descriptor[index] == "L":
            end = descriptor.find(";", index)
            if end < 0:
                break
            index = end + 1
        else:
            index += 1
        parts.append(descriptor[start:index])
    return parts


def _smali_descriptor_types(descriptor: str) -> str:
    return ",".join(_smali_descriptor_type(item) for item in _smali_descriptor_parts(descriptor))


def _smali_descriptor_type(descriptor: str) -> str:
    dimensions = len(descriptor) - len(descriptor.lstrip("["))
    raw = descriptor[dimensions:]
    primitives = {
        "V": "void", "Z": "boolean", "B": "byte", "C": "char", "S": "short",
        "I": "int", "J": "long", "F": "float", "D": "double",
    }
    value = primitives.get(raw, raw.strip("L;").replace("/", "."))
    return value + "[]" * dimensions


def _strip_comments_and_strings_preserve(line: str, in_block_comment: bool) -> tuple[str, bool]:
    """清洗单行文本并以空格保留原字符宽度，保证证据位置稳定。"""

    result = list(line)
    index = 0
    quote: str | None = None
    while index < len(line):
        if in_block_comment:
            if line.startswith("*/", index):
                result[index:index + 2] = "  "
                in_block_comment = False
                index += 2
            else:
                if line[index] != "\n":
                    result[index] = " "
                index += 1
            continue
        if quote:
            if line[index] == "\\":
                if line[index] != "\n":
                    result[index] = " "
                if index + 1 < len(line) and line[index + 1] != "\n":
                    result[index + 1] = " "
                index += 2
                continue
            if line[index] == quote:
                result[index] = " "
                quote = None
            elif line[index] != "\n":
                result[index] = " "
            index += 1
            continue
        if line.startswith("//", index):
            while index < len(line) and line[index] != "\n":
                result[index] = " "
                index += 1
            continue
        if line.startswith("/*", index):
            result[index:index + 2] = "  "
            in_block_comment = True
            index += 2
            continue
        if line[index] in {'"', "'"}:
            quote = line[index]
            result[index] = " "
        index += 1
    return "".join(result), in_block_comment


def _brace_end(lines: list[str], start: int) -> int:
    depth = 0
    opened = False
    in_block_comment = False
    for index in range(start, len(lines)):
        sanitized, in_block_comment = _strip_comments_and_strings(lines[index], in_block_comment)
        for char in sanitized:
            if char == "{":
                depth += 1
                opened = True
            elif char == "}" and opened:
                depth -= 1
                if depth <= 0:
                    return index + 1
    return len(lines)


def _strip_comments_and_strings(line: str, in_block_comment: bool) -> tuple[str, bool]:
    result: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(line):
        if in_block_comment:
            end = line.find("*/", index)
            if end < 0:
                return "".join(result), True
            index = end + 2
            in_block_comment = False
            continue
        if quote:
            if line[index] == "\\":
                index += 2
                continue
            if line[index] == quote:
                quote = None
            index += 1
            continue
        if line.startswith("//", index):
            break
        if line.startswith("/*", index):
            in_block_comment = True
            index += 2
            continue
        if line[index] in {'"', "'"}:
            quote = line[index]
            index += 1
            continue
        result.append(line[index])
        index += 1
    return "".join(result), in_block_comment


def _containing_class(classes: list[dict[str, Any]], line: int) -> dict[str, Any] | None:
    matches = [item for item in classes if item["start_line"] <= line <= item["end_line"]]
    return min(matches, key=lambda item: item["end_line"] - item["start_line"]) if matches else None


def _clean_type(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"<.*?>", "", value).strip().rstrip("()")


def _split_types(value: str) -> list[str]:
    results = []
    for item in value.split(","):
        cleaned = _clean_type(item.split("(", 1)[0])
        if cleaned:
            results.append(cleaned)
    return results


def _first_kotlin_parent(value: str) -> str | None:
    types = _split_types(value)
    return types[0] if types else None


def _smali_type(value: str) -> str:
    return value.strip("L;").replace("/", ".")
