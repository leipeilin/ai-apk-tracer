"""提供代码索引的 SQLite 写入器与共享只读查询接口。"""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = "2.9.0"


def _compact_json(value: Any) -> str:
    """以稳定紧凑格式保存普通 JSON 字段。"""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _pack_json(value: Any) -> sqlite3.Binary:
    """压缩高基数 IR/参数字段，避免重复 JSON key 占用大量 SQLite 页。"""

    return sqlite3.Binary(zlib.compress(_compact_json(value).encode("utf-8"), level=6))


def _load_json(value: Any) -> Any:
    """兼容读取 2.7 压缩 BLOB 与旧版明文 JSON。"""

    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        return json.loads(raw)
    return json.loads(value)


class SQLiteCodeIndexWriter:
    """以批量提交方式创建供后续规则和切片共享的代码索引。"""

    def __init__(self, database_path: Path):
        """创建新的索引数据库，并应用适合一次性批量构建的 SQLite 参数。"""

        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if self.database_path.exists():
            self.database_path.unlink()
        self.db = sqlite3.connect(self.database_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("PRAGMA cache_size=-65536")
        self._initialize()
        self._pending = 0

    def _initialize(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                line_count INTEGER NOT NULL,
                package_name TEXT NOT NULL,
                imports_json TEXT NOT NULL,
                symbols_json TEXT NOT NULL,
                calls_json TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE TABLE classes (
                id TEXT PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id),
                name TEXT NOT NULL,
                qualified_name TEXT,
                kind TEXT NOT NULL,
                extends_name TEXT,
                implements_json TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_classes_name ON classes(name);
            CREATE INDEX IF NOT EXISTS idx_classes_qualified_name ON classes(qualified_name);
            CREATE TABLE methods (
                id TEXT PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id),
                name TEXT NOT NULL,
                class_name TEXT,
                qualified_class TEXT,
                signature TEXT,
                descriptor TEXT NOT NULL,
                symbol_key TEXT NOT NULL,
                parameters_text TEXT,
                parameters_json BLOB NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                calls_json TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                flow_ir_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_methods_name ON methods(name);
            CREATE INDEX IF NOT EXISTS idx_methods_qualified_class ON methods(qualified_class);
            CREATE INDEX IF NOT EXISTS idx_methods_qualified_class_name_descriptor
                ON methods(qualified_class, name, descriptor);
            CREATE INDEX IF NOT EXISTS idx_methods_file_start_end
                ON methods(file_id, start_line, end_line);
            CREATE TABLE call_sites (
                id INTEGER PRIMARY KEY,
                method_id TEXT NOT NULL REFERENCES methods(id),
                ordinal INTEGER NOT NULL,
                receiver_text TEXT,
                receiver_type TEXT,
                method_name TEXT NOT NULL,
                method_descriptor TEXT NOT NULL,
                resolved_target_id TEXT REFERENCES methods(id),
                resolve_status TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                assigned_to TEXT,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                expression_kind TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_call_sites_method_ordinal ON call_sites(method_id, ordinal);
            CREATE INDEX IF NOT EXISTS idx_call_sites_resolved_target_id ON call_sites(resolved_target_id);
            CREATE TABLE skipped_files (
                path TEXT PRIMARY KEY,
                size_bytes INTEGER NOT NULL,
                reason TEXT NOT NULL
            );
            -- 外部内容 FTS 仅保存倒排索引，正文以 files.content 为唯一事实源，
            -- 避免大型 APK 在 files 与 FTS shadow table 中重复存储完整伪源码。
            CREATE VIRTUAL TABLE code_fts USING fts5(
                path UNINDEXED,
                content,
                content='files',
                content_rowid='id',
                tokenize='unicode61 tokenchars ''_$'''
            );
            """
        )
        self.db.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))

    def add_file(self, file: dict[str, Any], size_bytes: int) -> None:
        """写入文件内容、全文检索数据及其类和方法结构。"""

        cursor = self.db.execute(
            """INSERT INTO files
            (path, sha256, size_bytes, line_count, package_name, imports_json, symbols_json, calls_json, content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                file["path"], file["sha256"], size_bytes, file["line_count"], file.get("package", ""),
                _compact_json(file.get("imports", [])),
                _compact_json(file.get("symbols", [])),
                _compact_json(file.get("calls", [])),
                file["content"],
            ),
        )
        file_id = int(cursor.lastrowid)
        self.db.execute("INSERT INTO code_fts(rowid, path, content) VALUES (?, ?, ?)", (file_id, file["path"], file["content"]))
        self.db.executemany(
            """INSERT INTO classes
            (id, file_id, name, qualified_name, kind, extends_name, implements_json, start_line, end_line)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    item["id"], file_id, item["name"], item.get("qualified_name"), item["kind"],
                    item.get("extends"), _compact_json(item.get("implements", [])),
                    item["start_line"], item["end_line"],
                )
                for item in file.get("classes", [])
            ],
        )
        self.db.executemany(
            """INSERT INTO methods
            (id, file_id, name, class_name, qualified_class, signature, descriptor, symbol_key,
             parameters_text, parameters_json, start_line, end_line, calls_json, summary_json, flow_ir_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    item["id"], file_id, item["name"], item.get("class_name"), item.get("qualified_class"),
                    item.get("signature"), item["descriptor"], item["symbol_key"], item.get("parameters", ""),
                    _pack_json(item.get("structured_parameters", item.get("parameters_json", []))),
                    item["start_line"], item["end_line"], _compact_json(item.get("calls", [])),
                    _pack_json(item.get("summary", {})),
                    _pack_json(item.get("flow_ir", [])),
                )
                for item in file.get("methods", [])
            ],
        )
        self.db.executemany(
            """INSERT INTO call_sites
            (method_id, ordinal, receiver_text, receiver_type, method_name, method_descriptor,
             resolved_target_id, resolve_status, arguments_json, assigned_to,
             start_line, end_line, expression_kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    method["id"], int(call.get("ordinal", 0)), call.get("receiver_text"), call.get("receiver_type"),
                    call["method_name"], call.get("method_descriptor", "()->?"),
                    call.get("resolved_target_id"), call.get("resolve_status", "pending"),
                    _pack_json(call.get("arguments", [])), call.get("assigned_to"),
                    call["start_line"], call["end_line"], call.get("expression_kind", "invoke"),
                )
                for method in file.get("methods", [])
                for call in method.get("call_sites", [])
            ],
        )
        self._pending += 1
        if self._pending >= 250:
            self.db.commit()
            self._pending = 0

    def _resolve_call_targets(self) -> None:
        """仅在同类或同包候选唯一时写入确定调用边，歧义调用不猜测目标。"""

        methods = list(self.db.execute(
            "SELECT id, name, qualified_class, descriptor FROM methods ORDER BY id"
        ))
        by_class: dict[tuple[str, str, int], list[sqlite3.Row]] = {}
        by_package: dict[tuple[str, str, int], list[sqlite3.Row]] = {}
        classes_by_simple: dict[str, set[str]] = {}
        for method in methods:
            qualified_class = str(method["qualified_class"] or "")
            method_name = str(method["name"])
            arity = len(_descriptor_parameters(str(method["descriptor"])))
            if qualified_class:
                by_class.setdefault((qualified_class, method_name, arity), []).append(method)
                by_package.setdefault((_package_name(qualified_class), method_name, arity), []).append(method)
                classes_by_simple.setdefault(qualified_class.rsplit(".", 1)[-1], set()).add(qualified_class)

        cursor = self.db.execute(
            """SELECT cs.id, cs.method_name, cs.method_descriptor, cs.receiver_text, cs.receiver_type,
                      caller.qualified_class AS caller_class
               FROM call_sites cs JOIN methods caller ON caller.id=cs.method_id
               WHERE EXISTS (SELECT 1 FROM methods target WHERE target.name=cs.method_name)
               ORDER BY cs.id"""
        )
        while True:
            rows = cursor.fetchmany(10_000)
            if not rows:
                break
            updates = []
            for call in rows:
                caller_class = str(call["caller_class"] or "")
                caller_package = caller_class.rsplit(".", 1)[0] if "." in caller_class else ""
                method_name = str(call["method_name"])
                call_descriptor = str(call["method_descriptor"])
                arity = len(_descriptor_parameters(call_descriptor))
                receiver_text = str(call["receiver_text"] or "")
                receiver_type = str(call["receiver_type"] or "")
                scoped: list[sqlite3.Row]
                if receiver_text == "super":
                    # 父类 dispatch 不能回连到当前实现，否则会制造伪递归和错误数据流。
                    scoped = []
                elif receiver_type:
                    receiver_fqcn = _receiver_fqcn(
                        receiver_type, caller_class, caller_package, classes_by_simple
                    )
                    if not receiver_fqcn:
                        scoped = []
                    else:
                        # 已由 import/type environment 解析到精确 FQCN 时允许跨包调用；
                        # 仍要求方法名、参数个数和 descriptor 唯一，避免简单名串线。
                        scoped = by_class.get((receiver_fqcn, method_name, arity), [])
                elif receiver_text in {"", "this"}:
                    scoped = by_class.get((caller_class, method_name, arity), [])
                    if not scoped:
                        scoped = by_package.get((caller_package, method_name, arity), [])
                else:
                    # 显式 receiver 但类型未知时不能回连到 caller 同名方法；保持 pending，
                    # 由 typed taxonomy 产生保守 gap，而不是制造伪递归 wrapper。
                    scoped = []
                scoped = [
                    method for method in scoped
                    if _descriptors_compatible(call_descriptor, str(method["descriptor"]))
                ]
                if len(scoped) == 1:
                    updates.append((str(scoped[0]["id"]), "resolved", call["id"]))
                elif len(scoped) > 1:
                    updates.append((None, "ambiguous", call["id"]))
                # 无内部候选的外部库调用保持 pending，不为 170 万调用点写入无意义 unresolved。
            if updates:
                self.db.executemany(
                    "UPDATE call_sites SET resolved_target_id=?, resolve_status=? WHERE id=?",
                    updates,
                )

    def add_skipped(self, path: str, size_bytes: int, reason: str) -> None:
        """记录因资源边界未进入索引的文件及原因。"""

        self.db.execute(
            "INSERT INTO skipped_files(path, size_bytes, reason) VALUES (?, ?, ?)",
            (path, size_bytes, reason),
        )

    def finish(self, metadata: dict[str, Any]) -> None:
        """完成调用目标解析，写入元数据并关闭数据库。"""

        self._resolve_call_targets()
        for key, value in metadata.items():
            self.db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (key, _compact_json(value) if not isinstance(value, str) else value),
            )
        self.db.commit()
        self.db.execute("PRAGMA optimize")
        self.db.close()
        self.database_path.chmod(0o600)


def _package_name(qualified_class: str) -> str:
    return qualified_class.rsplit(".", 1)[0] if "." in qualified_class else ""


def _receiver_fqcn(
    receiver_type: str,
    caller_class: str,
    caller_package: str,
    classes_by_simple: dict[str, set[str]],
) -> str | None:
    normalized = receiver_type.replace("/", ".").strip("L;")
    if normalized in {"this", "super"}:
        return caller_class or None
    if "." in normalized:
        return normalized
    same_package = [
        value for value in classes_by_simple.get(normalized, [])
        if _package_name(value) == caller_package
    ]
    return same_package[0] if len(same_package) == 1 else None


def _descriptor_parameters(descriptor: str) -> list[str]:
    if not descriptor.startswith("(") or ")" not in descriptor:
        return []
    raw = descriptor[1:descriptor.index(")")]
    return [] if not raw else [item.strip() for item in raw.split(",")]


def _descriptors_compatible(call_descriptor: str, method_descriptor: str) -> bool:
    call_types = _descriptor_parameters(call_descriptor)
    method_types = _descriptor_parameters(method_descriptor)
    if len(call_types) != len(method_types):
        return False
    return all(
        call_type == "?"
        or method_type == "?"
        or call_type.rsplit(".", 1)[-1] == method_type.rsplit(".", 1)[-1]
        for call_type, method_type in zip(call_types, method_types)
    )


class SQLiteCodeIndexReader:
    """通过 immutable 只读连接查询共享代码索引。"""

    def __init__(self, descriptor: dict[str, Any]):
        """校验索引文件与 descriptor/meta 版本后建立不可变只读连接。"""

        descriptor_version = str(descriptor.get("schema_version") or "")
        if descriptor_version != SCHEMA_VERSION:
            raise ValueError(
                f"INDEX_SCHEMA_REBUILD_REQUIRED: expected {SCHEMA_VERSION}, descriptor has {descriptor_version or 'missing'}"
            )
        database_path = Path(descriptor["database_path"])
        if database_path.is_symlink() or not database_path.is_file():
            raise ValueError("共享代码索引不存在或是软链接")
        self.database_path = database_path.resolve()
        # immutable 只读 URI 与 query_only 双重约束，避免规则和切片阶段改写共享索引。
        self.db = sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro&immutable=1",
            uri=True,
        )
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
        self._method_index_cache: dict[str, dict[str, Any]] | None = None

    def close(self) -> None:
        """关闭共享只读索引连接。"""

        self.db.close()

    def iter_file_metadata(self) -> Iterator[dict[str, Any]]:
        """按路径顺序惰性产出文件结构元数据。"""

        rows = self.db.execute(
            "SELECT id, path, sha256, line_count, package_name, imports_json, symbols_json, calls_json FROM files ORDER BY path"
        )
        for row in rows:
            yield self._file_metadata(row)

    def load_lightweight_structure_files(self) -> list[dict[str, Any]]:
        """加载文件、类和轻量方法元数据，不读取调用点或大型方法 IR。"""

        files: dict[int, dict[str, Any]] = {}
        for row in self.db.execute(
            "SELECT id, path, sha256, line_count, package_name, imports_json, symbols_json, calls_json FROM files ORDER BY path"
        ):
            file_id = int(row["id"])
            files[file_id] = {
                "_file_id": file_id,
                "path": row["path"],
                "sha256": row["sha256"],
                "line_count": int(row["line_count"]),
                "package": row["package_name"],
                "imports": json.loads(row["imports_json"]),
                "symbols": json.loads(row["symbols_json"]),
                "calls": json.loads(row["calls_json"]),
                "classes": [],
                "methods": [],
            }
        for row in self.db.execute("SELECT * FROM classes ORDER BY file_id, start_line"):
            files[int(row["file_id"])]["classes"].append(self._class(row))
        for row in self.db.execute(
            """SELECT id, file_id, name, class_name, qualified_class, signature,
                      descriptor, symbol_key, parameters_text, parameters_json, start_line, end_line,
                      calls_json
               FROM methods ORDER BY file_id, start_line"""
        ):
            structured_parameters = _load_json(row["parameters_json"])
            source_language = (
                structured_parameters[0].get("source_language") if structured_parameters else None
            )
            smali_descriptor_only = bool(
                structured_parameters and structured_parameters[0].get("smali_descriptor_only")
            )
            files[int(row["file_id"])]["methods"].append({
                "id": row["id"],
                "name": row["name"],
                "class_name": row["class_name"],
                "qualified_class": row["qualified_class"],
                "signature": row["signature"],
                "descriptor": row["descriptor"],
                "symbol_key": row["symbol_key"],
                "parameters": row["parameters_text"],
                "structured_parameters": structured_parameters,
                "source_language": source_language,
                "smali_descriptor_only": smali_descriptor_only,
                "start_line": int(row["start_line"]),
                "end_line": int(row["end_line"]),
                "calls": json.loads(row["calls_json"]),
            })
        return list(files.values())

    def load_structure_files(self) -> list[dict[str, Any]]:
        """批量加载不含源码正文的文件、类和方法结构。"""

        files: dict[int, dict[str, Any]] = {}
        for row in self.db.execute(
            "SELECT id, path, sha256, line_count, package_name, imports_json, symbols_json, calls_json FROM files ORDER BY path"
        ):
            file_id = int(row["id"])
            files[file_id] = {
                "_file_id": file_id,
                "path": row["path"],
                "sha256": row["sha256"],
                "line_count": int(row["line_count"]),
                "package": row["package_name"],
                "imports": json.loads(row["imports_json"]),
                "symbols": json.loads(row["symbols_json"]),
                "calls": json.loads(row["calls_json"]),
                "classes": [],
                "methods": [],
            }
        for row in self.db.execute("SELECT * FROM classes ORDER BY file_id, start_line"):
            files[int(row["file_id"])]["classes"].append(self._class(row))
        methods_by_id: dict[str, dict[str, Any]] = {}
        for row in self.db.execute("SELECT * FROM methods ORDER BY file_id, start_line"):
            method = self._method(row)
            method["call_sites"] = []
            methods_by_id[str(row["id"])] = method
            files[int(row["file_id"])]["methods"].append(method)
        for row in self.db.execute("SELECT * FROM call_sites ORDER BY method_id, ordinal"):
            method = methods_by_id.get(str(row["method_id"]))
            if method is not None:
                method["call_sites"].append(self._call_site(row))
        return list(files.values())

    def get_file_metadata(self, path: str) -> dict[str, Any] | None:
        """按索引内相对路径读取文件结构元数据。"""

        row = self.db.execute(
            "SELECT id, path, sha256, line_count, package_name, imports_json, symbols_json, calls_json FROM files WHERE path=?",
            (path,),
        ).fetchone()
        return self._file_metadata(row) if row else None

    def get_content(self, path: str) -> str:
        """读取索引中的源码正文，路径不存在时抛出 ``KeyError``。"""

        row = self.db.execute("SELECT content FROM files WHERE path=?", (path,)).fetchone()
        if row is None:
            raise KeyError(path)
        return str(row[0])

    def load_method_index(self) -> dict[str, dict[str, Any]]:
        """加载并缓存证据校验所需的全量轻量方法索引。"""

        if self._method_index_cache is None:
            self._method_index_cache = {
                str(row["id"]): {
                    "id": row["id"],
                    "name": row["name"],
                    "symbol_key": row["symbol_key"],
                    "parameters": row["parameters_text"],
                    "structured_parameters": _load_json(row["parameters_json"]),
                    "start_line": int(row["start_line"]),
                    "end_line": int(row["end_line"]),
                    "path": row["path"],
                }
                for row in self.db.execute(
                    """SELECT m.id, m.name, m.symbol_key, m.parameters_text, m.parameters_json,
                              m.start_line, m.end_line, f.path
                       FROM methods m JOIN files f ON f.id=m.file_id"""
                )
            }
        return self._method_index_cache

    def get_methods(self, file_id: int) -> list[dict[str, Any]]:
        """按起始行返回指定文件的方法结构及真实调用点。"""

        return self.get_methods_for_files([file_id]).get(file_id, [])

    def get_methods_for_files(self, file_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
        """用固定两次查询批量返回目标文件的方法和调用点。"""

        ids = sorted({int(file_id) for file_id in file_ids})
        result: dict[int, list[dict[str, Any]]] = {file_id: [] for file_id in ids}
        if not ids:
            return result
        placeholders = ",".join("?" for _ in ids)
        methods_by_id: dict[str, dict[str, Any]] = {}
        for row in self.db.execute(
            f"SELECT * FROM methods WHERE file_id IN ({placeholders}) ORDER BY file_id, start_line",
            ids,
        ):
            method = self._method(row)
            method["call_sites"] = []
            methods_by_id[str(row["id"])] = method
            result[int(row["file_id"])].append(method)
        for row in self.db.execute(
            f"""SELECT cs.* FROM call_sites cs
                JOIN methods m ON m.id=cs.method_id
                WHERE m.file_id IN ({placeholders})
                ORDER BY cs.method_id, cs.ordinal""",
            ids,
        ):
            methods_by_id[str(row["method_id"])]["call_sites"].append(self._call_site(row))
        return result

    def get_call_sites(self, method_id: str) -> list[dict[str, Any]]:
        """返回指定方法内已排除注释、字符串和声明的结构化调用点。"""

        return self.get_call_sites_for_methods([method_id]).get(method_id, [])

    def get_call_sites_for_methods(self, method_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """批量返回目标方法的调用点，并在各方法内保持 ordinal 顺序。"""

        ids = sorted({str(method_id) for method_id in method_ids})
        result: dict[str, list[dict[str, Any]]] = {method_id: [] for method_id in ids}
        for offset in range(0, len(ids), 10_000):
            chunk = ids[offset:offset + 10_000]
            placeholders = ",".join("?" for _ in chunk)
            for row in self.db.execute(
                f"SELECT * FROM call_sites WHERE method_id IN ({placeholders}) ORDER BY method_id, ordinal",
                chunk,
            ):
                result[str(row["method_id"])].append(self._call_site(row))
        return result

    def get_call_relations_for_methods(
        self,
        method_ids: Iterable[str],
        *,
        include_callers: bool = True,
        include_callees: bool = True,
    ) -> dict[str, dict[str, list[Any]]]:
        """按目标方法分片查询直接 callers、callees 及目标方法内的歧义调用。"""

        ids = sorted({str(method_id) for method_id in method_ids})
        result: dict[str, dict[str, list[Any]]] = {
            "callers": {method_id: [] for method_id in ids},
            "callees": {method_id: [] for method_id in ids},
            "gaps": {method_id: [] for method_id in ids},
        }
        for offset in range(0, len(ids), 900):
            chunk = ids[offset:offset + 900]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            if include_callees:
                for row in self.db.execute(
                    f"""SELECT method_id, resolved_target_id FROM call_sites
                        WHERE method_id IN ({placeholders})
                          AND resolve_status='resolved' AND resolved_target_id IS NOT NULL
                        ORDER BY method_id, ordinal""",
                    chunk,
                ):
                    result["callees"][str(row["method_id"])].append(str(row["resolved_target_id"]))
            if include_callers:
                for row in self.db.execute(
                    f"""SELECT resolved_target_id, method_id FROM call_sites
                        WHERE resolved_target_id IN ({placeholders}) AND resolve_status='resolved'
                        ORDER BY resolved_target_id, method_id, ordinal""",
                    chunk,
                ):
                    result["callers"][str(row["resolved_target_id"])].append(str(row["method_id"]))
            for row in self.db.execute(
                f"""SELECT cs.method_id, cs.start_line, cs.method_name, cs.method_descriptor,
                           f.path
                    FROM call_sites cs
                    JOIN methods m ON m.id=cs.method_id
                    JOIN files f ON f.id=m.file_id
                    WHERE cs.method_id IN ({placeholders}) AND cs.resolve_status='ambiguous'
                    ORDER BY cs.method_id, cs.ordinal""",
                chunk,
            ):
                method_id = str(row["method_id"])
                result["gaps"][method_id].append({
                    "type": "symbol",
                    "code": "SYMBOL_TARGET_AMBIGUOUS",
                    "caller_method_id": method_id,
                    "path": row["path"],
                    "line": int(row["start_line"]),
                    "method_name": row["method_name"],
                    "descriptor": row["method_descriptor"],
                })
        for relation in ("callers", "callees"):
            for method_id, values in result[relation].items():
                result[relation][method_id] = sorted(set(values))
        return result

    def count_ambiguous_call_sites(self) -> int:
        """返回歧义调用点总数，不加载其记录。"""

        return int(self.db.execute(
            "SELECT COUNT(*) FROM call_sites WHERE resolve_status='ambiguous'"
        ).fetchone()[0])

    def get_classes(self, file_id: int) -> list[dict[str, Any]]:
        """按起始行返回指定文件的类结构。"""

        return self.get_classes_for_files([file_id]).get(file_id, [])

    def get_classes_for_files(self, file_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
        """用一次查询按文件批量返回类结构。"""

        ids = sorted({int(file_id) for file_id in file_ids})
        result: dict[int, list[dict[str, Any]]] = {file_id: [] for file_id in ids}
        if not ids:
            return result
        placeholders = ",".join("?" for _ in ids)
        for row in self.db.execute(
            f"SELECT * FROM classes WHERE file_id IN ({placeholders}) ORDER BY file_id, start_line",
            ids,
        ):
            result[int(row["file_id"])].append(self._class(row))
        return result

    def component_files(self, component_name: str) -> list[dict[str, Any]]:
        """按组件限定名、简单名或常见源码路径查找相关文件。"""

        simple = component_name.rsplit(".", 1)[-1]
        rows = self.db.execute(
            """SELECT DISTINCT f.id, f.path, f.sha256, f.line_count, f.package_name,
               f.imports_json, f.symbols_json, f.calls_json, f.content
               FROM files f
               LEFT JOIN classes c ON c.file_id=f.id
               WHERE c.qualified_name=? OR c.name=? OR f.path LIKE ? OR f.path LIKE ?
               ORDER BY f.path""",
            (component_name, simple, f"%/{simple}.java", f"%/{simple}.kt"),
        ).fetchall()
        return [self._full_file(row) for row in rows]

    def search_files(self, terms: list[str]) -> list[dict[str, Any]]:
        """使用经过字符白名单过滤的词项执行全文检索。"""

        safe_terms = [term for term in terms if term and all(char.isalnum() or char in "_.$" for char in term)]
        if not safe_terms:
            return []
        query = " OR ".join(f'"{term}"' for term in safe_terms)
        rows = self.db.execute(
            """SELECT DISTINCT f.id, f.path, f.sha256, f.line_count, f.package_name,
               f.imports_json, f.symbols_json, f.calls_json, f.content
               FROM code_fts JOIN files f ON f.id=code_fts.rowid
               WHERE code_fts MATCH ? ORDER BY f.path""",
            (query,),
        ).fetchall()
        return [self._full_file(row) for row in rows]

    def stats(self) -> dict[str, int]:
        """返回索引文件、类、方法及跳过文件的数量统计。"""

        return {
            "file_count": self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "class_count": self.db.execute("SELECT COUNT(*) FROM classes").fetchone()[0],
            "method_count": self.db.execute("SELECT COUNT(*) FROM methods").fetchone()[0],
            "call_site_count": self.db.execute("SELECT COUNT(*) FROM call_sites").fetchone()[0],
            "skipped_file_count": self.db.execute("SELECT COUNT(*) FROM skipped_files").fetchone()[0],
        }

    def _file_metadata(self, row: sqlite3.Row) -> dict[str, Any]:
        file = {
            "_file_id": int(row["id"]),
            "path": row["path"],
            "sha256": row["sha256"],
            "line_count": int(row["line_count"]),
            "package": row["package_name"],
            "imports": json.loads(row["imports_json"]),
            "symbols": json.loads(row["symbols_json"]),
            "calls": json.loads(row["calls_json"]),
        }
        file["classes"] = self.get_classes(file["_file_id"])
        file["methods"] = self.get_methods(file["_file_id"])
        return file

    def _full_file(self, row: sqlite3.Row) -> dict[str, Any]:
        file = self._file_metadata(row)
        file["content"] = row["content"]
        return file

    @staticmethod
    def _class(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "qualified_name": row["qualified_name"],
            "kind": row["kind"], "extends": row["extends_name"],
            "implements": json.loads(row["implements_json"]),
            "start_line": int(row["start_line"]), "end_line": int(row["end_line"]),
        }

    @staticmethod
    def _method(row: sqlite3.Row) -> dict[str, Any]:
        structured_parameters = _load_json(row["parameters_json"])
        summary = _load_json(row["summary_json"])
        source_language = summary.get("source_language") or (
            structured_parameters[0].get("source_language") if structured_parameters else None
        )
        smali_descriptor_only = bool(summary.get("smali_descriptor_only"))
        return {
            "id": row["id"], "name": row["name"], "class_name": row["class_name"],
            "qualified_class": row["qualified_class"], "signature": row["signature"],
            "descriptor": row["descriptor"], "symbol_key": row["symbol_key"],
            "parameters": row["parameters_text"],
            "structured_parameters": structured_parameters,
            "source_language": source_language,
            "smali_descriptor_only": smali_descriptor_only,
            "coverage": summary.get("coverage", {}),
            "limitations": summary.get("limitations", []),
            "start_line": int(row["start_line"]),
            "end_line": int(row["end_line"]), "calls": json.loads(row["calls_json"]),
            "summary": summary,
            "flow_ir": _load_json(row["flow_ir_json"]),
        }

    @staticmethod
    def _call_site(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "ordinal": int(row["ordinal"]),
            "receiver_text": row["receiver_text"],
            "receiver_type": row["receiver_type"],
            "method_name": row["method_name"],
            "method_descriptor": row["method_descriptor"],
            "resolved_target_id": row["resolved_target_id"],
            "resolve_status": row["resolve_status"],
            "arguments": _load_json(row["arguments_json"]),
            "assigned_to": row["assigned_to"],
            "start_line": int(row["start_line"]),
            "end_line": int(row["end_line"]),
            "expression_kind": row["expression_kind"],
        }
