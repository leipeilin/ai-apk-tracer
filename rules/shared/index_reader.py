"""为规则执行器提供受限、只读的源码索引查询能力。"""

from __future__ import annotations

import json
import re
import sqlite3
import zlib
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "2.9.0"


def _load_json(value: Any) -> Any:
    """兼容读取索引 2.7+ 的压缩 JSON BLOB 与旧版明文 JSON。"""

    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        return json.loads(raw)
    return json.loads(value)


def _source_component_name(name: str) -> str:
    """将 Manifest/JVM 的 ``Outer$Inner`` 统一为源码索引使用的点号名称。"""

    return name.replace("$", ".")


def _binder_type_exact_names(target: str, package: str) -> set[str]:
    """返回 Binder 类型可接受的点号/JVM 内部类精确名称。"""

    normalized = target.replace("$", ".")
    names = {target, normalized}
    first = normalized.split(".", 1)[0]
    if package and first[:1].isupper() and not normalized.startswith(f"{package}."):
        names.add(f"{package}.{normalized}")
    for name in list(names):
        if "." in name:
            prefix, leaf = name.rsplit(".", 1)
            names.add(f"{prefix}${leaf}")
    return {name for name in names if name}


GLOBAL_RULE_TERMS = {
    "DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION": ["registerReceiver", "RECEIVER_EXPORTED", "RECEIVER_NOT_EXPORTED"],
    "IMPLICIT_BROADCAST_SENSITIVE_DATA": ["sendBroadcast", "putExtra"],
    "ORDERED_BROADCAST_UNRESTRICTED": ["sendOrderedBroadcast"],
    # WebView 家族（§12.2 ②）：FTS 先缩小候选文件集，正则判定在 detector 内执行。
    "WEBVIEW_JS_BRIDGE_EXPOSED": ["addJavascriptInterface"],
    "WEBVIEW_FILE_ACCESS_ENABLED": ["setAllowFileAccess", "setAllowFileAccessFromFileURLs"],
    "WEBVIEW_UNIVERSAL_ACCESS_FROM_FILE": ["setAllowUniversalAccessFromFileURLs"],
    "WEBVIEW_SSL_ERROR_IGNORED": ["onReceivedSslError", "handler.proceed"],
    "WEBVIEW_EXTERNAL_CONTENT": ["setJavaScriptEnabled", "loadUrl"],
    # 密码学/证书校验族（§12.2 ③）。
    "TRUST_MANAGER_ALL_ACCEPT": ["checkServerTrusted", "X509TrustManager"],
    "HOSTNAME_VERIFIER_ALWAYS_TRUE": ["HostnameVerifier", "verify"],
    "WEAK_CIPHER_ECB": ["Cipher.getInstance", "AES/ECB"],
}
FLOW_INTRINSIC_METHODS = {
    "Intent", "Bundle", "getIntent", "getStringExtra", "getIntExtra", "getLongExtra", "getBooleanExtra",
    "getParcelableExtra", "getSerializableExtra", "getExtras", "getData", "getDataString",
    "getAction", "getQueryParameter", "getPath", "getPathSegments", "getLastPathSegment",
    "readString", "readInt", "readLong", "readBundle", "readParcelable", "getString", "get",
    "optString", "optInt", "getParcelable", "getSerializable", "putExtra", "putString",
    "putInt", "putLong", "putBoolean", "putParcelable", "putSerializable", "putCharSequence",
    "putExtras", "putAll", "fillIn", "replaceExtras", "toString", "trim", "substring",
    "concat", "append", "format", "valueOf", "parse", "loadUrl", "evaluateJavascript",
    "addJavascriptInterface", "startActivity", "startService", "startForegroundService",
    "bindService", "sendBroadcast", "sendOrderedBroadcast", "execSQL", "rawQuery", "insert",
    "update", "delete", "open", "write", "startForeground", "requestLocationUpdates", "registerListener",
    "startSport", "pauseSport", "resumeSport", "finishSport", "isAllowedHttps", "isValidUrl",
    "validateUrl", "allowedScheme", "isAllowedScheme", "isHttpsUrl", "isTrustedUrl",
    "equals", "contains", "forName", "instantiate", "newInstance", "getDeclaredConstructor",
    "getConstructor", "apply", "commit", "notify", "onChanged", "postValue", "dispatch", "emit",
    "connect", "disconnect", "openConnection", "startScan", "stopScan", "writeCharacteristic",
    "writeDescriptor", "bulkTransfer", "controlTransfer", "transceive", "getLastLocation",
}


class RuleIndexReader:
    """在允许目录内按需读取规则索引，避免将完整源码索引载入规则进程。"""

    def __init__(self, descriptor: dict[str, Any]):
        """校验索引边界及 descriptor/meta 版本，并建立不可变只读连接。"""
        descriptor_version = str(descriptor.get("schema_version") or "")
        if descriptor_version != SCHEMA_VERSION:
            raise ValueError(
                f"INDEX_SCHEMA_REBUILD_REQUIRED: expected {SCHEMA_VERSION}, descriptor has {descriptor_version or 'missing'}"
            )
        path = Path(descriptor["database_path"])
        expected_root = Path(descriptor["allowed_index_root"]).resolve()
        resolved = path.resolve()
        # 先拒绝符号链接、非文件和目录越界，再交由 SQLite 打开，防止规则读取任意宿主文件。
        if path.is_symlink() or not path.is_file() or expected_root not in resolved.parents:
            raise ValueError("规则索引路径不在允许的只读目录内")
        self.db = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro&immutable=1", uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA query_only=ON")
        try:
            row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            meta_version = str(row[0]) if row else "missing"
        except sqlite3.Error as exc:
            self.db.close()
            raise ValueError("INDEX_SCHEMA_REBUILD_REQUIRED: index meta is unreadable") from exc
        if meta_version != SCHEMA_VERSION:
            self.db.close()
            raise ValueError(
                f"INDEX_SCHEMA_REBUILD_REQUIRED: expected {SCHEMA_VERSION}, meta has {meta_version}"
            )

    def close(self) -> None:
        """关闭当前规则执行期间持有的索引连接。"""
        self.db.close()

    def sink_callers(
        self, method_id: str, *, class_name: str, method_name: str
    ) -> tuple[list[list[str]], bool]:
        """全索引中指向指定方法的调用点信息。

        P1-5 打通（2026-08-15，修订）：返回 ``(resolved_callers, has_unresolved)``——
        resolved_callers 是 resolve 成功调用点的实参列表（解压后字符串化）；
        has_unresolved 表示是否存在**解析失败的同名调用点**（pending/ambiguous，
        按 receiver 类名 + 方法名匹配）。

        修订前只查 ``resolved_target_id``，把"解析失败"的调用者当成"不存在"，
        导致两个假阴性方向错误：
        - call_site_exists 把 resolve 失败误判为死代码（红线 13 反证可被采信）；
        - sink_argument_constant 漏掉 pending 调用点里的变量实参而误判 True。
        保守原则：存在任何解析失败的调用点时，调用方不得据此判"死代码"或
        "参数全部常量"——resolve 失败 ≠ 无调用者（可为重载/泛型/Receiver 推断
        不足），宁可漏判不可误判（误判会被决策层采信为 ai_false_positive）。
        """

        rows = self.db.execute(
            "SELECT arguments_json FROM call_sites WHERE resolved_target_id = ?",
            (method_id,),
        ).fetchall()
        callers: list[list[str]] = []
        for row in rows:
            try:
                parsed = _load_json(row["arguments_json"])
            except Exception:  # 单条脏数据不影响其余调用点
                continue
            if isinstance(parsed, list):
                callers.append([str(item) for item in parsed])
        # 解析失败的同名调用点：receiver 类型与目标类一致（或子串匹配类名）。
        # resolve_status != 'resolved' 覆盖 pending/ambiguous；无 arguments 匹配
        # 语义时按 receiver+方法名保守计数，避免把同名无关方法算入。
        unresolved = self.db.execute(
            "SELECT count(*) FROM call_sites "
            "WHERE method_name = ? AND resolve_status != 'resolved' "
            "AND receiver_type LIKE ?",
            (method_name, f"%{class_name}%"),
        ).fetchone()[0]
        return callers, bool(unresolved)

    def component_files(self, component_name: str) -> list[dict[str, Any]]:
        """按 FQCN 或精确源码路径查询组件，简单名仅在全局唯一时回退。"""

        source_name = _source_component_name(component_name)
        simple = source_name.rsplit(".", 1)[-1]
        outer_name = component_name.split("$", 1)[0]
        java_path = outer_name.replace(".", "/") + ".java"
        kotlin_path = outer_name.replace(".", "/") + ".kt"
        rows = self.db.execute(
            """SELECT DISTINCT f.id, f.path, f.sha256, f.line_count, f.package_name,
               f.imports_json, f.symbols_json, f.calls_json, f.content
               FROM files f LEFT JOIN classes c ON c.file_id=f.id
               WHERE c.qualified_name=? OR f.path IN (?, ?)
               ORDER BY f.path""",
            (source_name, java_path, kotlin_path),
        ).fetchall()
        if rows:
            return self._files(rows)
        fallback = self.db.execute(
            """SELECT DISTINCT f.id, f.path, f.sha256, f.line_count, f.package_name,
               f.imports_json, f.symbols_json, f.calls_json, f.content,
               c.qualified_name
               FROM classes c JOIN files f ON f.id=c.file_id
               WHERE c.name=? ORDER BY c.qualified_name, f.path""",
            (simple,),
        ).fetchall()
        unique_classes = {str(row["qualified_name"]) for row in fallback if row["qualified_name"]}
        return self._files(fallback) if len(unique_classes) == 1 else []

    def component_flow_scope(self, component_fqcn: str, entry_names: set[str] | list[str]) -> dict[str, Any]:
        """从精确 FQCN 入口沿唯一解析调用边加载最小可达方法闭包。"""

        names = sorted({str(name) for name in entry_names if name})
        if not component_fqcn or not names:
            return {"files": [], "entry_method_ids": [], "method_ids": [], "gaps": []}
        source_component_fqcn = _source_component_name(component_fqcn)
        placeholders = ",".join("?" for _ in names)
        direct_entry_rows = self.db.execute(
            f"SELECT id FROM methods WHERE qualified_class=? AND name IN ({placeholders})",
            [source_component_fqcn, *names],
        ).fetchall()
        entry_classes = [source_component_fqcn]
        hierarchy_gaps: list[dict[str, Any]] = []
        current = source_component_fqcn
        visited = {current}
        # 生命周期方法可能实现于应用基类；仅沿索引中唯一可解析的 extends 链查找入口。
        for _ in range(8):
            row = self.db.execute(
                "SELECT extends_name FROM classes WHERE qualified_name=?",
                (current,),
            ).fetchone()
            parent = str(row["extends_name"] or "") if row else ""
            if not parent or parent.startswith(("android.", "androidx.", "java.", "kotlin.")):
                break
            if "." not in parent:
                package = current.rsplit(".", 1)[0] if "." in current else ""
                same_package = f"{package}.{parent}" if package else parent
                exact = self.db.execute(
                    "SELECT qualified_name FROM classes WHERE qualified_name=?",
                    (same_package,),
                ).fetchall()
                if len(exact) == 1:
                    parent = str(exact[0]["qualified_name"])
                else:
                    matches = self.db.execute(
                        "SELECT qualified_name FROM classes WHERE name=? ORDER BY qualified_name",
                        (parent,),
                    ).fetchall()
                    if len(matches) != 1:
                        hierarchy_gaps.append({
                            "code": "COMPONENT_PARENT_AMBIGUOUS" if matches else "COMPONENT_PARENT_UNRESOLVED",
                            "critical": True,
                            "component": component_fqcn,
                            "parent": parent,
                            "candidate_count": len(matches),
                        })
                        break
                    parent = str(matches[0]["qualified_name"])
            if parent in visited:
                hierarchy_gaps.append({
                    "code": "COMPONENT_HIERARCHY_CYCLE", "critical": True,
                    "component": component_fqcn, "parent": parent,
                })
                break
            visited.add(parent)
            entry_classes.append(parent)
            current = parent
        if direct_entry_rows:
            # 当前组件已提供入口时无需让无关平台/依赖父类的歧义污染确定性结果。
            entry_classes = [component_fqcn]
            hierarchy_gaps = []
        class_placeholders = ",".join("?" for _ in entry_classes)
        entry_rows = self.db.execute(
            f"""SELECT id FROM methods
                WHERE qualified_class IN ({class_placeholders})
                  AND name IN ({placeholders})
                ORDER BY CASE qualified_class WHEN ? THEN 0 ELSE 1 END, id""",
            [*entry_classes, *names, component_fqcn],
        ).fetchall()
        entry_ids = [str(row["id"]) for row in entry_rows]
        if not entry_ids:
            return {
                "files": [], "entry_method_ids": [], "method_ids": [],
                "gaps": [*hierarchy_gaps, {
                    "code": "COMPONENT_ENTRY_NOT_INDEXED", "critical": True,
                    "component": component_fqcn, "entry_names": names,
                    "searched_classes": entry_classes,
                }],
            }
        entry_placeholders = ",".join("?" for _ in entry_ids)
        reachable_rows = self.db.execute(
            f"""WITH RECURSIVE reachable(id) AS (
                    SELECT id FROM methods WHERE id IN ({entry_placeholders})
                    UNION
                    SELECT cs.resolved_target_id
                    FROM call_sites cs JOIN reachable r ON r.id=cs.method_id
                    WHERE cs.resolve_status='resolved' AND cs.resolved_target_id IS NOT NULL
                )
                SELECT id FROM reachable ORDER BY id""",
            entry_ids,
        ).fetchall()
        method_ids = [str(row["id"]) for row in reachable_rows]
        method_placeholders = ",".join("?" for _ in method_ids)
        gap_rows = self.db.execute(
            f"""SELECT cs.*, m.qualified_class AS caller_class
                FROM call_sites cs JOIN methods m ON m.id=cs.method_id
                WHERE cs.method_id IN ({method_placeholders})
                  AND cs.resolve_status IN ('ambiguous', 'unresolved')
                ORDER BY cs.method_id, cs.ordinal""",
            method_ids,
        ).fetchall()
        gaps = list(hierarchy_gaps)
        for row in gap_rows:
            if not row["assigned_to"] and not _load_json(row["arguments_json"]):
                continue
            method_name = str(row["method_name"] or "")
            if method_name in FLOW_INTRINSIC_METHODS:
                continue
            ambiguous = row["resolve_status"] == "ambiguous"
            gaps.append({
                "code": "SYMBOL_TARGET_AMBIGUOUS" if ambiguous else "CALL_TARGET_UNRESOLVED",
                "critical": True,
                "method": row["method_name"],
                "caller": row["method_id"],
                "ordinal": int(row["ordinal"]),
            })
        return {
            "files": self._load_flow_methods(method_ids),
            "entry_method_ids": entry_ids,
            "method_ids": method_ids,
            "gaps": gaps,
        }

    def provider_entry_scopes(self, component_fqcn: str) -> list[dict[str, Any]]:
        """按 Provider 每个 CRUD overload 独立加载可达方法闭包。"""

        source_component = _source_component_name(component_fqcn)
        entry_names = ("applyBatch", "call", "delete", "insert", "openFile", "query", "update")
        classes = [source_component]
        current = source_component
        provider_hierarchy_verified = False
        hierarchy_reason = "unresolved"
        for _ in range(8):
            class_row = self.db.execute(
                "SELECT extends_name FROM classes WHERE qualified_name=?", (current,)
            ).fetchone()
            parent = str(class_row["extends_name"] or "") if class_row else ""
            if parent in {"android.content.ContentProvider", "ContentProvider"}:
                provider_hierarchy_verified = True
                hierarchy_reason = "content_provider_parent"
                break
            if not parent or parent.startswith(("android.", "androidx.", "java.", "kotlin.")):
                hierarchy_reason = "missing_parent" if not parent else "non_provider_platform_parent"
                break
            if "." not in parent:
                package = current.rsplit(".", 1)[0] if "." in current else ""
                same_package = f"{package}.{parent}" if package else parent
                matches = self.db.execute(
                    "SELECT qualified_name FROM classes WHERE qualified_name=? OR name=? ORDER BY qualified_name",
                    (same_package, parent),
                ).fetchall()
                values = sorted({str(item["qualified_name"]) for item in matches})
                if len(values) != 1:
                    hierarchy_reason = "parent_ambiguous" if values else "parent_unresolved"
                    break
                parent = values[0]
            if parent in classes:
                hierarchy_reason = "hierarchy_cycle"
                break
            classes.append(parent)
            current = parent
        placeholders = ",".join("?" for _ in entry_names)
        class_placeholders = ",".join("?" for _ in classes)
        all_rows = self.db.execute(
            f"""SELECT id, name, descriptor, start_line, qualified_class FROM methods
                WHERE qualified_class IN ({class_placeholders}) AND name IN ({placeholders})
                ORDER BY name, descriptor, start_line, id""",
            [*classes, *entry_names],
        ).fetchall()
        selected: dict[tuple[str, str], sqlite3.Row] = {}
        for row in sorted(all_rows, key=lambda item: (
            classes.index(str(item["qualified_class"])), str(item["name"]),
            str(item["descriptor"]), int(item["start_line"]), str(item["id"]),
        )):
            if not _provider_override_descriptor_valid(str(row["name"]), str(row["descriptor"])):
                continue
            selected.setdefault((str(row["name"]), str(row["descriptor"])), row)
        rows = sorted(selected.values(), key=lambda item: (
            str(item["name"]), str(item["descriptor"]), int(item["start_line"]), str(item["id"]),
        ))
        scopes: list[dict[str, Any]] = []
        for row in rows:
            entry_id = str(row["id"])
            reachable = self.db.execute(
                """WITH RECURSIVE reachable(id) AS (
                       SELECT id FROM methods WHERE id=?
                       UNION
                       SELECT cs.resolved_target_id
                       FROM call_sites cs JOIN reachable r ON r.id=cs.method_id
                       WHERE cs.resolve_status='resolved' AND cs.resolved_target_id IS NOT NULL
                   ) SELECT id FROM reachable ORDER BY id""",
                (entry_id,),
            ).fetchall()
            method_ids = [str(item["id"]) for item in reachable]
            method_placeholders = ",".join("?" for _ in method_ids)
            gaps: list[dict[str, Any]] = [] if provider_hierarchy_verified else [{
                "code": "PROVIDER_INHERITANCE_UNPROVEN",
                "critical": True,
                "component": component_fqcn,
                "reason": hierarchy_reason,
            }]
            for call in self.db.execute(
                f"""SELECT * FROM call_sites
                    WHERE method_id IN ({method_placeholders})
                      AND resolve_status IN ('ambiguous', 'unresolved')
                    ORDER BY method_id, ordinal""",
                method_ids,
            ):
                method_name = str(call["method_name"] or "")
                if method_name in FLOW_INTRINSIC_METHODS:
                    continue
                if not call["assigned_to"] and not _load_json(call["arguments_json"]):
                    continue
                gaps.append({
                    "code": "SYMBOL_TARGET_AMBIGUOUS" if call["resolve_status"] == "ambiguous" else "CALL_TARGET_UNRESOLVED",
                    "critical": True,
                    "method": method_name,
                    "caller": call["method_id"],
                    "ordinal": int(call["ordinal"]),
                })
            scopes.append({
                "component": component_fqcn,
                "entry_method_id": entry_id,
                "entry_method_ids": [entry_id],
                "entry_name": str(row["name"]),
                "entry_descriptor": str(row["descriptor"]),
                "method_ids": method_ids,
                "files": self._load_flow_methods(method_ids),
                "gaps": gaps,
            })
        return scopes

    def _load_flow_methods(self, method_ids: list[str]) -> list[dict[str, Any]]:
        """批量还原闭包中的方法；不会把同文件其他方法带入数据流。"""

        if not method_ids:
            return []
        placeholders = ",".join("?" for _ in method_ids)
        file_rows = self.db.execute(
            f"""SELECT DISTINCT f.* FROM files f JOIN methods m ON m.file_id=f.id
                WHERE m.id IN ({placeholders}) ORDER BY f.path""",
            method_ids,
        ).fetchall()
        files: dict[int, dict[str, Any]] = {}
        for row in file_rows:
            file_id = int(row["id"])
            files[file_id] = {
                "path": row["path"], "sha256": row["sha256"], "line_count": int(row["line_count"]),
                "package": row["package_name"], "imports": json.loads(row["imports_json"]),
                "symbols": json.loads(row["symbols_json"]), "calls": json.loads(row["calls_json"]),
                "classes": [], "methods": [], "content": row["content"],
            }
        file_ids = sorted(files)
        file_placeholders = ",".join("?" for _ in file_ids)
        for row in self.db.execute(
            f"SELECT * FROM classes WHERE file_id IN ({file_placeholders}) ORDER BY file_id, start_line",
            file_ids,
        ):
            files[int(row["file_id"])]["classes"].append({
                "id": row["id"], "name": row["name"], "qualified_name": row["qualified_name"],
                "kind": row["kind"], "extends": row["extends_name"],
                "implements": json.loads(row["implements_json"]),
                "start_line": int(row["start_line"]), "end_line": int(row["end_line"]),
            })
        method_rows = self.db.execute(
            f"SELECT * FROM methods WHERE id IN ({placeholders}) ORDER BY file_id, start_line",
            method_ids,
        ).fetchall()
        content_lines = {file_id: str(files[file_id]["content"]).splitlines() for file_id in file_ids}
        methods_by_id: dict[str, dict[str, Any]] = {}
        for row in method_rows:
            file_id = int(row["file_id"])
            start_line, end_line = int(row["start_line"]), int(row["end_line"])
            structured_parameters = _load_json(row["parameters_json"])
            summary = _load_json(row["summary_json"])
            method = {
                "id": row["id"], "name": row["name"], "class_name": row["class_name"],
                "qualified_class": row["qualified_class"], "signature": row["signature"],
                "descriptor": row["descriptor"], "symbol_key": row["symbol_key"],
                "parameters": row["parameters_text"],
                "structured_parameters": structured_parameters,
                "source_language": summary.get("source_language"),
                "smali_descriptor_only": bool(summary.get("smali_descriptor_only")),
                "coverage": summary.get("coverage", {}),
                "limitations": summary.get("limitations", []),
                "start_line": start_line, "end_line": end_line,
                "calls": json.loads(row["calls_json"]), "summary": summary,
                "flow_ir": _load_json(row["flow_ir_json"]), "call_sites": [],
                "content": "\n".join(content_lines[file_id][start_line - 1:end_line]),
            }
            files[file_id]["methods"].append(method)
            methods_by_id[str(row["id"])] = method
        for row in self.db.execute(
            f"SELECT * FROM call_sites WHERE method_id IN ({placeholders}) ORDER BY method_id, ordinal",
            method_ids,
        ):
            methods_by_id[str(row["method_id"])]["call_sites"].append({
                "id": row["id"], "ordinal": int(row["ordinal"]),
                "receiver_text": row["receiver_text"], "receiver_type": row["receiver_type"],
                "method_name": row["method_name"], "method_descriptor": row["method_descriptor"],
                "resolved_target_id": row["resolved_target_id"], "resolve_status": row["resolve_status"],
                "arguments": _load_json(row["arguments_json"]), "assigned_to": row["assigned_to"],
                "start_line": int(row["start_line"]), "end_line": int(row["end_line"]),
                "expression_kind": row["expression_kind"],
            })
        return [files[file_id] for file_id in file_ids]

    def binder_components(self, service_fqcns: list[str]) -> dict[str, dict[str, Any]]:
        """用精确索引批量加载 Service 与最多四层 Binder 类型/继承闭包。

        查询仅命中目标 FQCN、同包唯一简单名、受限 legacy owner fallback 及其父类，不扫描
        Service 整包。每层歧义均产出 gap 而不任选目标；四轮是显式覆盖/成本上限，超出部分
        不会被当作已解析。transaction 随后仍须在 case 范围内唯一绑定最派生实现。
        """

        names = sorted({name for name in service_fqcns if name})
        if not names:
            return {}
        source_names = sorted({_source_component_name(name) for name in names})
        placeholders = ",".join("?" for _ in source_names)
        service_rows = self.db.execute(
            f"""SELECT c.id, c.name, c.qualified_name, c.file_id, f.package_name
                FROM classes c JOIN files f ON f.id=c.file_id
                WHERE c.qualified_name IN ({placeholders})
                ORDER BY c.qualified_name""",
            source_names,
        ).fetchall()
        service_rows_by_name = {str(row["qualified_name"]): row for row in service_rows}
        files = self._load_files_by_ids({int(row["file_id"]) for row in service_rows})
        result: dict[str, dict[str, Any]] = {}
        state: dict[str, dict[str, Any]] = {}
        for service_name in names:
            source_service_name = _source_component_name(service_name)
            row = service_rows_by_name.get(source_service_name)
            file = files.get(int(row["file_id"])) if row else None
            service_class = next((
                item for item in (file or {}).get("classes", [])
                if item.get("qualified_name") == source_service_name
            ), None)
            on_bind = next((
                method for method in (file or {}).get("methods", [])
                if method.get("qualified_class") == source_service_name and method.get("name") == "onBind"
            ), None)
            return_types = self._on_bind_return_types(file, on_bind)
            gaps = [] if row else [{"code": "SERVICE_CLASS_NOT_INDEXED", "critical": False}]
            state[service_name] = {
                "service_file": file,
                "service_class": service_class,
                "on_bind": on_bind,
                "return_types": return_types,
                "pending": list(return_types),
                "visited": set(),
                "selected_file_ids": {int(row["file_id"])} if row else set(),
                "inheritance_chain": [],
                "gaps": gaps,
                "package": service_name.rsplit(".", 1)[0] if "." in service_name else "",
                "type_scope": source_service_name.rsplit(".", 1)[0] if "." in source_service_name else "",
            }

        # 常见链路为 Service → 实现 Stub → 生成 Stub 父类；四轮足够且显式有界。
        for _ in range(4):
            targets = sorted({
                target for item in state.values() for target in item["pending"]
                if target and target not in item["visited"]
            })
            if not targets:
                break
            simple_names = sorted({target.replace("$", ".").rsplit(".", 1)[-1] for target in targets})
            exact_targets = sorted({
                exact
                for item in state.values()
                for target in item["pending"]
                if target and target not in item["visited"]
                for scope in {item["package"], item["type_scope"]}
                for exact in _binder_type_exact_names(target, scope)
            })
            target_placeholders = ",".join("?" for _ in exact_targets)
            simple_placeholders = ",".join("?" for _ in simple_names)
            class_rows = self.db.execute(
                f"""SELECT c.id, c.name, c.qualified_name, c.file_id, c.extends_name,
                           c.implements_json, f.package_name, f.path
                    FROM classes c JOIN files f ON f.id=c.file_id
                    WHERE c.qualified_name IN ({target_placeholders})
                       OR c.name IN ({simple_placeholders})
                    ORDER BY c.qualified_name, c.id""",
                [*exact_targets, *simple_names],
            ).fetchall()
            class_rows_by_exact: dict[str, list[sqlite3.Row]] = {}
            class_rows_by_simple: dict[str, list[sqlite3.Row]] = {}
            for row in class_rows:
                class_rows_by_exact.setdefault(str(row["qualified_name"]), []).append(row)
                class_rows_by_simple.setdefault(str(row["name"]), []).append(row)
            newly_selected_ids: set[int] = set()
            for item in state.values():
                pending = item["pending"]
                item["pending"] = []
                for target in pending:
                    if target in item["visited"]:
                        continue
                    item["visited"].add(target)
                    exact_names = {
                        exact
                        for scope in {item["package"], item["type_scope"]}
                        for exact in _binder_type_exact_names(target, scope)
                    }
                    matches = [
                        row for exact in exact_names for row in class_rows_by_exact.get(exact, [])
                    ]
                    if not matches:
                        normalized = target.replace("$", ".")
                        simple = normalized.rsplit(".", 1)[-1]
                        outer = normalized.rsplit(".", 1)[0].rsplit(".", 1)[-1] if "." in normalized else None
                        simple_matches = class_rows_by_simple.get(simple, [])
                        same_package = [
                            row for row in simple_matches if str(row["package_name"]) == item["package"]
                        ]
                        # 兼容旧索引：内部类 owner 尚未进入 qualified_name 时，以 Outer.java
                        # 限定候选。仍不唯一则继续产生 critical gap，绝不任选目标。
                        outer_file_matches = [
                            row for row in same_package
                            if outer and Path(str(row["path"])).stem == outer
                        ]
                        matches = outer_file_matches or same_package or (
                            simple_matches if len(simple_matches) == 1 else []
                        )
                    unique = {str(row["id"]): row for row in matches}
                    if len(unique) > 1:
                        item["gaps"].append({
                            "code": "BINDER_RETURN_TYPE_AMBIGUOUS",
                            "critical": True,
                            "type": target,
                            "candidate_count": len(unique),
                        })
                        continue
                    if not unique:
                        normalized_target = target.replace("$", ".")
                        outer_name = normalized_target.split(".", 1)[0]
                        outer_fqcn = f"{item['package']}.{outer_name}" if item["package"] else outer_name
                        owner_rows = self.db.execute(
                            """SELECT c.id, c.name, c.qualified_name, c.file_id, c.extends_name,
                                      c.implements_json, f.package_name, f.path
                               FROM classes c JOIN files f ON f.id=c.file_id
                               WHERE c.qualified_name=? ORDER BY c.id""",
                            (outer_fqcn,),
                        ).fetchall() if "." in normalized_target else []
                        if len(owner_rows) == 1:
                            owner_row = owner_rows[0]
                            file_id = int(owner_row["file_id"])
                            item["selected_file_ids"].add(file_id)
                            newly_selected_ids.add(file_id)
                            item["gaps"].append({
                                "code": "BINDER_NESTED_CLASS_OWNER_FALLBACK",
                                "critical": False,
                                "type": target,
                                "owner": outer_fqcn,
                            })
                            continue
                        item["gaps"].append({
                            "code": "BINDER_RETURN_TYPE_UNRESOLVED",
                            "critical": False,
                            "type": target,
                        })
                        continue
                    row = next(iter(unique.values()))
                    file_id = int(row["file_id"])
                    item["selected_file_ids"].add(file_id)
                    newly_selected_ids.add(file_id)
                    item["inheritance_chain"].append({
                        "class": row["qualified_name"],
                        "extends": row["extends_name"],
                        "implements": json.loads(row["implements_json"]),
                    })
                    if row["extends_name"]:
                        item["pending"].append(str(row["extends_name"]))
            files.update(self._load_files_by_ids(newly_selected_ids - set(files)))

        for service_name, item in state.items():
            selected_files = [files[file_id] for file_id in sorted(item["selected_file_ids"]) if file_id in files]
            transactions = _binder_transactions(selected_files)
            binding_gaps, implementation_ids = self._bind_binder_transactions(
                transactions, selected_files, item["inheritance_chain"]
            )
            flow_method_ids: list[str] = []
            if implementation_ids:
                seeds = sorted(implementation_ids)
                placeholders = ",".join("?" for _ in seeds)
                flow_method_ids = [
                    str(row["id"])
                    for row in self.db.execute(
                        f"""WITH RECURSIVE reachable(id) AS (
                               SELECT id FROM methods WHERE id IN ({placeholders})
                               UNION
                               SELECT cs.resolved_target_id
                               FROM call_sites cs JOIN reachable r ON r.id=cs.method_id
                               WHERE cs.resolve_status='resolved' AND cs.resolved_target_id IS NOT NULL
                           ) SELECT id FROM reachable ORDER BY id""",
                        seeds,
                    )
                ]
            result[service_name] = {
                "files": selected_files,
                "flow_files": self._load_flow_methods(flow_method_ids),
                "service_class": item["service_class"],
                "on_bind": item["on_bind"],
                "return_types": sorted(set(item["return_types"])),
                "inheritance_chain": item["inheritance_chain"],
                "transactions": transactions,
                "gaps": [
                    *item["gaps"], *binding_gaps,
                    *(gap for transaction in transactions for gap in transaction.get("gaps", [])),
                ],
            }
        return result

    @staticmethod
    def _bind_binder_transactions(
        transactions: list[dict[str, Any]],
        files: list[dict[str, Any]],
        inheritance_chain: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """按 concrete→parent 顺序和 name+descriptor 绑定最派生实现。"""

        methods = [method for file in files for method in file.get("methods", [])]
        chain = [str(item.get("class") or "") for item in inheritance_chain if item.get("class")]
        gaps: list[dict[str, Any]] = []
        implementation_ids: set[str] = set()
        for transaction in transactions:
            name = str(transaction.get("interface_method") or "")
            descriptor = str(transaction.get("dispatch_descriptor") or "")
            if not name:
                gap = {
                    "code": "BINDER_DISPATCH_TARGET_UNRESOLVED", "critical": True,
                    "transaction_code": transaction.get("code"),
                }
                transaction.setdefault("gaps", []).append(gap)
                gaps.append(gap)
                continue
            selected: list[dict[str, Any]] = []
            for qualified_class in chain:
                selected = [
                    method for method in methods
                    if method.get("qualified_class") == qualified_class
                    and method.get("name") == name
                    and method.get("name") != "onTransact"
                    and _binder_descriptors_compatible(descriptor, str(method.get("descriptor") or ""))
                ]
                if selected:
                    break
            if not selected:
                # 兼容没有完整 class hierarchy 的反编译结果，但仍要求全范围唯一。
                selected = [
                    method for method in methods
                    if method.get("name") == name and method.get("name") != "onTransact"
                    and _binder_descriptors_compatible(descriptor, str(method.get("descriptor") or ""))
                ]
            if len(selected) != 1:
                gap = {
                    "code": "BINDER_IMPLEMENTATION_AMBIGUOUS" if selected else "BINDER_IMPLEMENTATION_UNRESOLVED",
                    "critical": True,
                    "transaction_code": transaction.get("code"),
                    "method": name,
                    "descriptor": descriptor or None,
                    "candidate_count": len(selected),
                }
                transaction.setdefault("gaps", []).append(gap)
                gaps.append(gap)
                continue
            implementation = selected[0]
            transaction.update({
                "implementation_method_id": implementation.get("id"),
                "implementation_class": implementation.get("qualified_class"),
                "implementation_descriptor": implementation.get("descriptor"),
                "implementation_path": next(
                    (file.get("path") for file in files if implementation in file.get("methods", [])), None
                ),
                "implementation_line": implementation.get("start_line"),
            })
            implementation_ids.add(str(implementation["id"]))
        return gaps, implementation_ids

    def _load_files_by_ids(self, file_ids: set[int]) -> dict[int, dict[str, Any]]:
        """以文件、类、方法、调用点分离查询还原文件，避免笛卡尔积和 N+1。"""

        if not file_ids:
            return {}
        ids = sorted(file_ids)
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.execute(
            f"SELECT * FROM files WHERE id IN ({placeholders}) ORDER BY id",
            ids,
        ).fetchall()
        values = self._files(rows, include_class_id=True)
        return {int(row["id"]): value for row, value in zip(rows, values)}

    def binder_files(self, component_name: str) -> list[dict[str, Any]]:
        """兼容单组件调用；Binder 规则应优先使用 ``binder_components``。"""

        return self.binder_components([component_name]).get(component_name, {}).get("files", [])

    @staticmethod
    def _on_bind_return_types(file: dict[str, Any] | None, on_bind: dict[str, Any] | None) -> list[str]:
        if not file or not on_bind:
            return []
        values = set()
        descriptor = str(on_bind.get("descriptor") or "")
        if ")->" in descriptor:
            declared = descriptor.split(")->", 1)[1]
            if declared not in {"Object", "android.os.IBinder", "IBinder", "?", "void"}:
                values.add(declared)
        content = str(on_bind.get("content") or "")
        values.update(re.findall(r"\breturn\s+new\s+([A-Za-z_$][\w.$]*)", content))
        for field_name in re.findall(r"\breturn\s+(?!new\b)(?:this\.)?([A-Za-z_$][\w$]*)", content):
            field = re.search(
                rf"\b([A-Za-z_$][\w$<>.]*)\s+{re.escape(field_name)}\s*(?:=\s*new\s+([A-Za-z_$][\w.$]*))?",
                str(file.get("content") or ""),
            )
            if field:
                values.add(field.group(2) or field.group(1).split("<", 1)[0])
        return sorted(values)

    def search_for_rule(self, rule_id: str) -> list[dict[str, Any]]:
        """使用规则预定义词项执行按需全文检索，并返回命中的源码文件。"""
        terms = GLOBAL_RULE_TERMS.get(rule_id, [])
        query = " OR ".join(f'"{term}"' for term in terms)
        if not query:
            return []
        # FTS 先缩小候选集，后续规则只对命中文件执行较昂贵的正则判定。
        rows = self.db.execute(
            """SELECT DISTINCT f.id, f.path, f.sha256, f.line_count, f.package_name,
               f.imports_json, f.symbols_json, f.calls_json, f.content
               FROM code_fts JOIN files f ON f.id=code_fts.rowid
               WHERE code_fts MATCH ? ORDER BY f.path""",
            (query,),
        ).fetchall()
        return self._files(rows)

    def dynamic_receiver_scope(self) -> dict[str, Any]:
        """加载精确 registerReceiver 调用者及全部 onReceive 方法的可达闭包。"""

        registration_ids = [
            str(row["id"])
            for row in self.db.execute(
                """SELECT DISTINCT m.id
                   FROM call_sites cs JOIN methods m ON m.id=cs.method_id
                   WHERE cs.method_name='registerReceiver'
                   ORDER BY m.id"""
            )
        ]
        if not registration_ids:
            return {
                "files": [], "registration_method_ids": [], "entry_method_ids": [],
                "method_ids": [], "gaps": [],
            }
        entry_ids = [
            str(row["id"])
            for row in self.db.execute(
                "SELECT id FROM methods WHERE name='onReceive' ORDER BY id"
            )
        ]
        reachable_ids: list[str] = []
        if entry_ids:
            placeholders = ",".join("?" for _ in entry_ids)
            reachable_ids = [
                str(row["id"])
                for row in self.db.execute(
                    f"""WITH RECURSIVE reachable(id) AS (
                            SELECT id FROM methods WHERE id IN ({placeholders})
                            UNION
                            SELECT cs.resolved_target_id
                            FROM call_sites cs JOIN reachable r ON r.id=cs.method_id
                            WHERE cs.resolve_status='resolved' AND cs.resolved_target_id IS NOT NULL
                        )
                        SELECT id FROM reachable ORDER BY id""",
                    entry_ids,
                )
            ]
        method_ids = sorted(set(registration_ids + reachable_ids))
        gaps: list[dict[str, Any]] = []
        if reachable_ids:
            placeholders = ",".join("?" for _ in reachable_ids)
            for row in self.db.execute(
                f"""SELECT cs.* FROM call_sites cs
                    WHERE cs.method_id IN ({placeholders})
                      AND cs.resolve_status IN ('ambiguous', 'unresolved')
                    ORDER BY cs.method_id, cs.ordinal""",
                reachable_ids,
            ):
                if not row["assigned_to"] and not _load_json(row["arguments_json"]):
                    continue
                method_name = str(row["method_name"] or "")
                if method_name in FLOW_INTRINSIC_METHODS:
                    continue
                gaps.append({
                    "code": "SYMBOL_TARGET_AMBIGUOUS" if row["resolve_status"] == "ambiguous" else "CALL_TARGET_UNRESOLVED",
                    "critical": True,
                    "method": method_name,
                    "caller": row["method_id"],
                    "ordinal": int(row["ordinal"]),
                })
        return {
            "files": self._load_flow_methods(method_ids),
            "registration_method_ids": registration_ids,
            "entry_method_ids": entry_ids,
            "method_ids": method_ids,
            "gaps": gaps,
        }

    def dynamic_receiver_files(self) -> list[dict[str, Any]]:
        """兼容旧调用，返回动态 Receiver 精确分析范围。"""

        return self.dynamic_receiver_scope()["files"]

    def _file(self, row: sqlite3.Row) -> dict[str, Any]:
        """还原单个文件；内部仍使用批量路径，避免逐方法查询调用点。"""

        return self._files([row])[0]

    def _files(self, rows: list[sqlite3.Row], *, include_class_id: bool = False) -> list[dict[str, Any]]:
        """用三次结构查询批量还原目标文件、类、方法和调用点。"""

        if not rows:
            return []
        file_ids = sorted({int(row["id"]) for row in rows})
        placeholders = ",".join("?" for _ in file_ids)
        files: dict[int, dict[str, Any]] = {}
        content_lines: dict[int, list[str]] = {}
        for row in rows:
            file_id = int(row["id"])
            if file_id in files:
                continue
            content = str(row["content"])
            files[file_id] = {
                "path": row["path"],
                "sha256": row["sha256"],
                "line_count": int(row["line_count"]),
                "package": row["package_name"],
                "imports": json.loads(row["imports_json"]),
                "symbols": json.loads(row["symbols_json"]),
                "calls": json.loads(row["calls_json"]),
                "classes": [],
                "methods": [],
                "content": content,
            }
            content_lines[file_id] = content.splitlines()

        for item in self.db.execute(
            f"SELECT * FROM classes WHERE file_id IN ({placeholders}) ORDER BY file_id, start_line",
            file_ids,
        ):
            class_info = {
                "name": item["name"],
                "qualified_name": item["qualified_name"],
                "kind": item["kind"],
                "extends": item["extends_name"],
                "implements": json.loads(item["implements_json"]),
                "start_line": int(item["start_line"]),
                "end_line": int(item["end_line"]),
            }
            if include_class_id:
                class_info = {"id": item["id"], **class_info}
            files[int(item["file_id"])]["classes"].append(class_info)

        methods_by_id: dict[str, dict[str, Any]] = {}
        for method in self.db.execute(
            f"SELECT * FROM methods WHERE file_id IN ({placeholders}) ORDER BY file_id, start_line",
            file_ids,
        ):
            file_id = int(method["file_id"])
            start_line, end_line = int(method["start_line"]), int(method["end_line"])
            structured_parameters = _load_json(method["parameters_json"])
            summary = _load_json(method["summary_json"])
            value = {
                "id": method["id"],
                "name": method["name"],
                "class_name": method["class_name"],
                "qualified_class": method["qualified_class"],
                "signature": method["signature"],
                "descriptor": method["descriptor"],
                "symbol_key": method["symbol_key"],
                "parameters": method["parameters_text"],
                "structured_parameters": structured_parameters,
                "source_language": summary.get("source_language"),
                "smali_descriptor_only": bool(summary.get("smali_descriptor_only")),
                "coverage": summary.get("coverage", {}),
                "limitations": summary.get("limitations", []),
                "start_line": start_line,
                "end_line": end_line,
                "calls": json.loads(method["calls_json"]),
                "summary": summary,
                "flow_ir": _load_json(method["flow_ir_json"]),
                "call_sites": [],
                "content": "\n".join(content_lines[file_id][start_line - 1:end_line]),
            }
            files[file_id]["methods"].append(value)
            methods_by_id[str(method["id"])] = value

        for call in self.db.execute(
            f"""SELECT cs.* FROM call_sites cs
                JOIN methods m ON m.id=cs.method_id
                WHERE m.file_id IN ({placeholders})
                ORDER BY cs.method_id, cs.ordinal""",
            file_ids,
        ):
            methods_by_id[str(call["method_id"])]["call_sites"].append({
                "id": call["id"],
                "ordinal": int(call["ordinal"]),
                "receiver_text": call["receiver_text"],
                "receiver_type": call["receiver_type"],
                "method_name": call["method_name"],
                "method_descriptor": call["method_descriptor"],
                "resolved_target_id": call["resolved_target_id"],
                "resolve_status": call["resolve_status"],
                "arguments": _load_json(call["arguments_json"]),
                "assigned_to": call["assigned_to"],
                "start_line": int(call["start_line"]),
                "end_line": int(call["end_line"]),
                "expression_kind": call["expression_kind"],
            })
        return [files[int(row["id"])] for row in rows]


def _binder_transactions(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """解析 AIDL transaction，并把 dispatch/reply 证据限制在各自 case 范围。

    decimal、hex、TRANSACTION_* 与 FIRST_CALL_TRANSACTION 偏移可确定性求值；同一 case 中
    非样板 dispatch 不唯一或无法解析时产生 critical gap，不跨 case 借用调用。transaction
    code、ordinal、Parcel 读写和 descriptor 均保留供后续实现绑定与 Guard 分段校验。
    """

    constants_by_name: dict[str, int] = {}
    constants_by_code: dict[int, str] = {}
    descriptors: dict[str, str] = {}
    for file in files:
        content = str(file.get("content") or "")
        for descriptor in re.findall(r"\bDESCRIPTOR\s*=\s*[\"']([^\"']+)[\"']", content):
            for class_info in file.get("classes", []):
                descriptors[str(class_info.get("qualified_name") or "")] = descriptor
        for match in re.finditer(
            r"\bTRANSACTION_([A-Za-z_$][\w$]*)\s*=\s*([^;,\n]+)", content
        ):
            value = _binder_transaction_code(match.group(2), constants_by_name)
            if value is not None:
                constants_by_name[match.group(1)] = value
                constants_by_code[value] = match.group(1)
    ignored = {
        "enforceInterface", "writeNoException", "readException", "writeInterfaceToken",
        "readInt", "readLong", "readFloat", "readDouble", "readBoolean", "readString",
        "readBundle", "readParcelable", "readTypedObject", "createTypedArrayList",
        "writeInt", "writeLong", "writeFloat", "writeDouble", "writeBoolean", "writeString",
        "writeBundle", "writeParcelable", "writeTypedObject", "writeTypedList",
        "checkCallingPermission", "checkCallingOrSelfPermission", "enforceCallingPermission",
        "enforceCallingOrSelfPermission", "enforceInterface", "getCallingUid", "getCallingPid",
        "getNameForUid", "getPackageInfo", "clearCallingIdentity", "restoreCallingIdentity",
        "super", "onTransact",
    }
    transactions: list[dict[str, Any]] = []
    for file in files:
        for method in file.get("methods", []):
            if method.get("name") != "onTransact":
                continue
            content = str(method.get("content") or "")
            cases = list(re.finditer(
                r"\bcase\s+((?:TRANSACTION_)?[A-Za-z_$][\w$]*|0[xX][0-9a-fA-F]+|\d+)\s*:",
                content,
            ))
            switch_match = re.search(r"\bswitch\s*\(", content)
            method_start = int(method.get("start_line", 1))
            owner = str(method.get("qualified_class") or "")
            interface_descriptor = descriptors.get(owner) or next(iter(descriptors.values()), None)
            for index, case in enumerate(cases):
                token = case.group(1)
                symbolic_name = token.removeprefix("TRANSACTION_") if token.startswith("TRANSACTION_") else None
                code = constants_by_name.get(symbolic_name or "")
                if code is None:
                    code = _binder_transaction_code(token, constants_by_name)
                case_end = cases[index + 1].start() if index + 1 < len(cases) else len(content)
                body = content[case.end():case_end]
                case_line = method_start + content.count("\n", 0, case.start())
                end_line = method_start + content.count("\n", 0, case_end) - (1 if index + 1 < len(cases) else 0)
                case_calls = [
                    call for call in method.get("call_sites", [])
                    if case_line <= int(call.get("start_line", 0)) <= max(case_line, end_line)
                ]
                dispatch_candidates = [
                    call for call in case_calls
                    if str(call.get("method_name") or "") not in ignored
                    and not str(call.get("method_name") or "").startswith(("read", "write"))
                ]
                if symbolic_name:
                    named = [call for call in dispatch_candidates if call.get("method_name") == symbolic_name]
                    dispatch = named[0] if len(named) == 1 else (dispatch_candidates[0] if len(dispatch_candidates) == 1 else None)
                else:
                    dispatch = dispatch_candidates[0] if len(dispatch_candidates) == 1 else None
                interface_method = symbolic_name or constants_by_code.get(code) if code is not None else symbolic_name
                if not interface_method and dispatch:
                    interface_method = str(dispatch.get("method_name") or "") or None
                ordinal_values = [int(call.get("ordinal", 0)) for call in case_calls]
                reply_write_call_sites = [
                    {
                        "method_name": str(call.get("method_name") or ""),
                        "arguments": [str(value) for value in call.get("arguments", [])],
                        "receiver_text": call.get("receiver_text"),
                        "receiver_type": call.get("receiver_type"),
                        "start_line": int(call.get("start_line", case_line)),
                        "end_line": int(call.get("end_line", call.get("start_line", case_line))),
                        "ordinal": int(call.get("ordinal", 0)),
                    }
                    for call in case_calls
                    if str(call.get("method_name") or "").startswith("write")
                    and str(call.get("receiver_text") or "").replace(" ", "").rsplit(".", 1)[-1] == "reply"
                ]
                transactions.append({
                    "code": code,
                    "case_token": token,
                    "interface_method": interface_method,
                    "descriptor": interface_descriptor,
                    "on_transact_method_id": method.get("id"),
                    "on_transact_descriptor": method.get("descriptor"),
                    "switch_line": method_start + content.count("\n", 0, switch_match.start()) if switch_match else method_start,
                    "case_line": case_line,
                    "case_end_line": max(case_line, end_line),
                    "case_ordinal_start": min(ordinal_values) if ordinal_values else None,
                    "case_ordinal_end": max(ordinal_values) if ordinal_values else None,
                    "implementation_calls": [str(call.get("method_name") or "") for call in dispatch_candidates],
                    "parcel_reads": sorted(set(re.findall(r"\b(?:data|parcel\w*)\s*\.\s*(read\w+|createTypedArrayList)\s*\(", body, re.I))),
                    "parcel_writes": sorted(set(re.findall(r"\b(?:reply|parcel\w*)\s*\.\s*(write\w+)\s*\(", body, re.I))),
                    "reply_write_call_sites": reply_write_call_sites,
                    "dispatch_call_site": dict(dispatch) if dispatch else None,
                    "dispatch_ordinal": int(dispatch.get("ordinal", 0)) if dispatch else None,
                    "dispatch_descriptor": dispatch.get("method_descriptor") if dispatch else None,
                    "dispatch_assigned_to": dispatch.get("assigned_to") if dispatch else None,
                    "path": file["path"],
                    "line": case_line,
                    "gaps": [] if dispatch else [{
                        "code": "BINDER_DISPATCH_TARGET_AMBIGUOUS" if len(dispatch_candidates) > 1 else "BINDER_DISPATCH_TARGET_UNRESOLVED",
                        "critical": True,
                        "transaction_code": code,
                        "candidate_count": len(dispatch_candidates),
                    }],
                })
    return sorted(transactions, key=lambda item: (
        str(item.get("path") or ""), int(item.get("line") or 0),
        int(item["code"]) if item.get("code") is not None else 2**31,
    ))


def _binder_transaction_code(value: str, constants: dict[str, int]) -> int | None:
    """保守计算 transaction literal、symbolic constant 与 FIRST_CALL_TRANSACTION 偏移。"""

    expression = value.strip().strip("()")
    if re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", expression):
        return int(expression, 0)
    symbolic = expression.removeprefix("TRANSACTION_")
    if symbolic in constants:
        return constants[symbolic]
    match = re.fullmatch(
        r"(?:(?:IBinder\.)?FIRST_CALL_TRANSACTION|1)\s*([+-])\s*(0[xX][0-9a-fA-F]+|\d+)",
        expression,
    )
    if match:
        offset = int(match.group(2), 0)
        return 1 + offset if match.group(1) == "+" else 1 - offset
    return None


def _provider_override_descriptor_valid(name: str, descriptor: str) -> bool:
    """仅接受 Android ContentProvider 定义的标准 CRUD override 形状。"""

    if not descriptor.startswith("(") or ")->" not in descriptor:
        return False
    parameters_text, return_type = descriptor[1:].split(")->", 1)
    parameters = [] if not parameters_text else [item.strip() for item in parameters_text.split(",")]

    def leaf(value: str) -> str:
        normalized = value.strip().rstrip("?").replace("/", ".")
        suffix = "[]" if normalized.endswith("[]") else ""
        normalized = normalized.removesuffix("[]")
        return normalized.rsplit(".", 1)[-1] + suffix

    shape = tuple(leaf(value) for value in parameters)
    result = leaf(return_type)
    signatures: dict[str, set[tuple[tuple[str, ...], str]]] = {
        "query": {
            (("Uri", "String[]", "String", "String[]", "String"), "Cursor"),
            (("Uri", "String[]", "String", "String[]", "String", "CancellationSignal"), "Cursor"),
            (("Uri", "String[]", "Bundle", "CancellationSignal"), "Cursor"),
        },
        "insert": {
            (("Uri", "ContentValues"), "Uri"),
            (("Uri", "ContentValues", "Bundle"), "Uri"),
        },
        "update": {
            (("Uri", "ContentValues", "String", "String[]"), "int"),
            (("Uri", "ContentValues", "Bundle"), "int"),
        },
        "delete": {
            (("Uri", "String", "String[]"), "int"),
            (("Uri", "Bundle"), "int"),
        },
        "openFile": {
            (("Uri", "String"), "ParcelFileDescriptor"),
            (("Uri", "String", "CancellationSignal"), "ParcelFileDescriptor"),
        },
        "call": {(("String", "String", "Bundle"), "Bundle")},
        "applyBatch": {(("ArrayList",), "ContentProviderResult[]")},
    }
    return (shape, result) in signatures.get(name, set())


def _binder_descriptors_compatible(call_descriptor: str, method_descriptor: str) -> bool:
    """比较 dispatch 与实现参数；未知类型只放宽对应位置，不放宽 arity。"""

    def parameters(descriptor: str) -> list[str] | None:
        if not descriptor.startswith("(") or ")" not in descriptor:
            return None
        raw = descriptor[1:descriptor.index(")")]
        return [] if not raw else [item.strip() for item in raw.split(",")]

    left, right = parameters(call_descriptor), parameters(method_descriptor)
    if left is None or right is None:
        return not call_descriptor or not method_descriptor
    return len(left) == len(right) and all(
        a == "?" or b == "?" or a.rsplit(".", 1)[-1] == b.rsplit(".", 1)[-1]
        for a, b in zip(left, right)
    )
