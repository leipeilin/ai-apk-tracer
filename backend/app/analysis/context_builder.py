"""围绕规则候选构建可追溯、可按请求扩展的最小代码切片。"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from app.analysis.context_budget import ContextBudget, context_bytes
from app.analysis.index_store import SQLiteCodeIndexReader
from app.config import ContextBudgetSettings

# 手动同步点（v2026-08-09）：与 rules/shared/dataflow.py GUARD_METHODS 同源。
# 调用者身份校验 API 核心集的一致性由 tests/test_guard_call_check_consistency.py
# 参数化测试强制——新增 API 后此处不同步测试立即失败（曾漏
# enforceCallingOrSelfPermission/checkUidSignatures 两个历史项）。
GUARD_PATTERN = re.compile(
    r"(?:checkCallingPermission|enforceCallingPermission|checkCallingOrSelfPermission|"
    r"enforceCallingOrSelfPermission|checkSignatures|checkUidSignatures|Binder\.getCallingUid|"
    r"getNameForUid|getPackageInfo|enforceReadPermission|"
    r"enforceWritePermission|SecurityException|requireNotNull|validate|whitelist|allowlist)",
    re.IGNORECASE,
)
LIFECYCLE_METHODS = {
    "activity": {"onCreate", "onNewIntent", "onActivityResult", "onResume"},
    "service": {"onBind", "onStartCommand", "onHandleIntent", "onCreate"},
    "provider": {"query", "insert", "update", "delete", "openFile", "call", "applyBatch"},
    "receiver": {"onReceive"},
}
ALLOWED_REQUEST_TYPES = {"method", "class", "component", "callers", "callees", "file_symbols"}


class ContextBuilder:
    """构建与证据关联的代码切片，避免向模型暴露完整代码索引。"""

    version = "2.0.0"

    def __init__(
        self,
        code_index: dict[str, Any],
        budget_settings: ContextBudgetSettings | ContextBudget | dict[str, Any] | None = None,
        *,
        budget: ContextBudget | None = None,
    ):
        """加载结构元数据并建立方法、类及近似调用边的内存查找表。

        SQLite 模式只加载结构，不预加载代码正文；正文仅在生成具体切片时按路径读取。
        """

        self.code_index = code_index
        if isinstance(budget_settings, ContextBudget):
            if budget is not None:
                raise ValueError("provide budget only once")
            budget = budget_settings
            budget_settings = None
        self.budget_settings = (
            budget_settings if isinstance(budget_settings, ContextBudgetSettings)
            else ContextBudgetSettings(**(budget_settings or {}))
        )
        self.context_budget = budget or ContextBudget.from_settings(self.budget_settings)
        self.index_reader: SQLiteCodeIndexReader | None = None
        if code_index.get("type") == "sqlite":
            self.index_reader = SQLiteCodeIndexReader(code_index)
            indexed_files = self.index_reader.load_lightweight_structure_files()
        else:
            indexed_files = code_index.get("files", [])
        self.files = {item["path"]: item for item in indexed_files}
        self.methods: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.methods_by_name: dict[str, list[str]] = {}
        self.methods_by_symbol_key: dict[str, list[str]] = {}
        self.classes: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.classes_by_simple: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for file in self.files.values():
            for method in file.get("methods", []):
                method_id = str(method["id"])
                self.methods[method_id] = (file, method)
                self.methods_by_name.setdefault(str(method["name"]), []).append(method_id)
                self.methods_by_symbol_key.setdefault(_canonical_target(method), []).append(method_id)
            for class_info in file.get("classes", []):
                match = (file, class_info)
                self.classes[str(class_info["id"])] = match
                if class_info.get("qualified_name"):
                    self.classes[str(class_info["qualified_name"])] = match
                self.classes_by_simple.setdefault(str(class_info["name"]), []).append(match)
        self.callers: dict[str, set[str]] = {method_id: set() for method_id in self.methods}
        self.callees: dict[str, set[str]] = {method_id: set() for method_id in self.methods}
        self.symbol_resolution_gaps: list[dict[str, Any]] = []
        self._gaps_by_caller: dict[str, list[dict[str, Any]]] = {}
        self._loaded_callers: set[str] = set()
        self._loaded_callees: set[str] = set()
        self._loaded_gaps: set[str] = set()
        self._symbol_resolution_gap_count = 0
        if self.index_reader:
            self._symbol_resolution_gap_count = self.index_reader.count_ambiguous_call_sites()
        else:
            self._build_call_edges()
            self._loaded_callers.update(self.methods)
            self._loaded_callees.update(self.methods)
            self._loaded_gaps.update(self.methods)
            self._symbol_resolution_gap_count = len(self.symbol_resolution_gaps)

    def _load_file_on_demand(self, path: str) -> dict[str, Any] | None:
        """按需加载索引中存在但不在组件 flow scope 内的文件（P1-4 修 sink 静默丢失）。

        ``self.files`` 初始只含组件 flow scope 的轻量结构；sink anchor 所在文件不在
        scope 时此前静默丢弃（`PATH_NOT_INDEXED` unresolved），AI 看不到 sink 上下文。
        本方法从 SQLite 索引按路径加载完整文件结构并注册进查找表；索引中不存在
        （无法加载）返回 ``None``，由调用方产出 ``SINK_CONTEXT_UNAVAILABLE`` gap。
        """

        if not self.index_reader:
            return None
        file = self.index_reader.get_file_metadata(path)
        if file is None:
            return None
        self.files[path] = file
        for method in file.get("methods", []):
            method_id = str(method["id"])
            self.methods[method_id] = (file, method)
            self.methods_by_name.setdefault(str(method["name"]), []).append(method_id)
            self.methods_by_symbol_key.setdefault(_canonical_target(method), []).append(method_id)
        for class_info in file.get("classes", []):
            match = (file, class_info)
            self.classes[str(class_info["id"])] = match
            if class_info.get("qualified_name"):
                self.classes[str(class_info["qualified_name"])] = match
            self.classes_by_simple.setdefault(str(class_info["name"]), []).append(match)
        return file

    def build_initial(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """从候选锚点和组件入口方法构建首轮最小切片。"""

        slice_document = {
            "schema_version": "1.0.0",
            "builder_version": self.version,
            "slice_id": self._slice_id(candidate),
            "candidate": _candidate_summary(candidate),
            "contexts": [],
            "edges": [],
            "guards": [],
            "request_history": [],
            "unresolved": [],
            "limitations": list(candidate.get("limitations", [])),
            "omitted_contexts": [],
        }
        selected: set[str] = set()
        budget_state = {"additions": 0}
        # Inject manifest component facts as a traceable context.
        manifest_ctx = _build_manifest_context(candidate)
        if manifest_ctx:
            self._admit_context(slice_document, manifest_ctx, selected, budget_state)
        anchors = _candidate_anchors(candidate)
        for anchor in anchors:
            path = anchor.get("path")
            line = _int_or_none(anchor.get("line"))
            if path and path in self.files:
                method_id = self._method_at(path, line) if line else None
                if method_id:
                    self._add_method(
                        slice_document, method_id, selected, budget_state,
                        reason=anchor.get("reason", "candidate_anchor"),
                    )
                else:
                    self._add_window(
                        slice_document, path, line or 1, selected, budget_state,
                        reason=anchor.get("reason", "candidate_anchor"),
                    )
            elif path and path != "AndroidManifest.xml":
                # P1-4（2026-08-15）修 sink 静默丢失：sink anchor 所在文件不在组件
                # flow scope 时按需加载（此前静默 PATH_NOT_INDEXED，AI 看不到 sink
                # 上下文）；索引中确实无法加载时才产出 SINK_CONTEXT_UNAVAILABLE gap。
                if anchor.get("reason") == "sensitive_sink":
                    loaded_file = self._load_file_on_demand(path)
                    if loaded_file:
                        method_id = self._method_at(path, line) if line else None
                        if method_id:
                            self._add_method(
                                slice_document, method_id, selected, budget_state,
                                reason="sensitive_sink",
                            )
                        else:
                            self._add_window(
                                slice_document, path, line or 1, selected, budget_state,
                                reason="sensitive_sink",
                            )
                    else:
                        slice_document["unresolved"].append({
                            "type": "sink_context_unavailable",
                            "path": path,
                            "line": line,
                            "reason": "SINK_CONTEXT_UNAVAILABLE",
                        })
                else:
                    slice_document["unresolved"].append({"type": "anchor", "path": path, "line": line, "reason": "PATH_NOT_INDEXED"})

        component_name = candidate.get("component_name")
        class_match = self._resolve_class(component_name) if component_name else None
        if class_match:
            file, class_info = class_match
            lifecycle = LIFECYCLE_METHODS.get(candidate.get("component"), set())
            class_methods = [
                method for method in file.get("methods", [])
                if method.get("class_name") == class_info["name"] and method.get("name") in lifecycle
            ]
            for method in class_methods:
                self._add_method(
                    slice_document, method["id"], selected, budget_state,
                    reason="component_entry_point",
                )
            if not class_methods and not selected:
                self._add_class_summary(
                    slice_document, file, class_info, selected, budget_state,
                    reason="component_class",
                )
        elif component_name:
            slice_document["unresolved"].append({"type": "component", "target": component_name, "reason": "CLASS_NOT_INDEXED"})

        if not slice_document["contexts"]:
            slice_document["limitations"].append("候选未能映射到已索引方法，无法构建代码切片")
        self._refresh_edges_and_guards(slice_document)
        self._refresh_unresolved(slice_document)
        self._refresh_budget(slice_document, budget_state["additions"])
        return slice_document

    def extend(self, slice_document: dict[str, Any], requests: list[dict[str, Any]]) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
        """按 canonical 请求扩展切片，并为每项返回明确、可审计的状态。"""

        updated = deepcopy(slice_document)
        updated.setdefault("omitted_contexts", [])
        selected = {context["context_id"] for context in updated.get("contexts", [])}
        added_before = len(selected)
        total_additions = 0
        results: list[dict[str, Any]] = []
        history_keys = {item.get("dedup_key") for item in updated.get("request_history", [])}
        for request in requests:
            normalized = self.normalize_request(request)
            canonical_target = _request_target(normalized)
            dedup_key = json.dumps({
                "type": normalized["type"],
                "target": canonical_target,
                "path": normalized.get("path"),
                "line": normalized.get("line"),
                "relation": normalized.get("relation"),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if dedup_key in history_keys:
                results.append({**normalized, "status": "duplicate_request", "added": 0})
                continue
            history_keys.add(dedup_key)
            updated.setdefault("request_history", []).append({"dedup_key": dedup_key, **normalized})
            request_type = normalized["type"]
            if request_type not in ALLOWED_REQUEST_TYPES:
                result = {**normalized, "status": "unsupported", "added": 0}
                results.append(result)
                updated.setdefault("unresolved", []).append({**normalized, "code": "REQUEST_TYPE_UNSUPPORTED"})
                continue

            req_path = normalized.get("path")
            relation_name = normalized.get("relation")
            if request_type == "method" and relation_name in {"callers", "callees"}:
                request_type = relation_name
            before = len(selected)
            omitted_before = len(updated["omitted_contexts"])
            budget_state = {"additions": 0}
            target_resolved = False
            status_override: str | None = None
            details: dict[str, Any] = {}

            if request_type in {"method", "callers", "callees"}:
                resolution = self._resolve_method_request(normalized)
                if resolution["status"] != "resolved":
                    status_override = resolution["status"]
                    details.update({
                        "matches": resolution["method_ids"],
                        "alternatives": resolution["alternatives"],
                    })
                else:
                    target_resolved = True
                    method_id = resolution["method_ids"][0]
                    if request_type == "method":
                        self._add_method(
                            updated, method_id, selected, budget_state,
                            reason=normalized["reason"],
                        )
                    else:
                        self._ensure_call_relations(method_id, request_type)
                        relation = self.callers if request_type == "callers" else self.callees
                        related = sorted(relation.get(method_id, set()))
                        if not related:
                            status_override = "empty_relation"
                        for related_id in related:
                            self._add_method(
                                updated, related_id, selected, budget_state,
                                reason=normalized["reason"],
                            )
            elif request_type in {"class", "component"}:
                target = normalized["target"]
                manifest_match = any(
                    target in {str(ctx.get("component_name") or ""), str(ctx.get("context_id") or "")}
                    for ctx in updated.get("contexts", [])
                    if ctx.get("kind") == "manifest_component"
                )
                if request_type == "component" and target and manifest_match:
                    target_resolved = True
                else:
                    simple = target.rsplit(".", 1)[-1]
                    class_matches = self.classes_by_simple.get(simple, []) if simple else []
                    exact_match = self.classes.get(target)
                    if exact_match:
                        class_matches = [exact_match]
                    class_matches = _unique_class_matches(class_matches)
                    if len(class_matches) > 1:
                        status_override = "ambiguous"
                        alternatives = sorted(
                            str(item[1].get("qualified_name") or item[1].get("id"))
                            for item in class_matches
                        )
                        details.update({"matches": alternatives, "alternatives": alternatives})
                    elif class_matches:
                        target_resolved = True
                        file, class_info = class_matches[0]
                        methods = sorted(
                            (method for method in file.get("methods", []) if method.get("class_name") == class_info["name"]),
                            key=lambda item: (item.get("start_line", 0), item.get("id", "")),
                        )
                        for ordinal, method in enumerate(methods):
                            if ordinal >= self.budget_settings.max_methods_per_class_request and method["id"] not in selected:
                                context = self._method_context(method["id"], normalized["reason"])
                                self._record_omission(updated, context, "class_method_limit")
                                continue
                            self._add_method(
                                updated, method["id"], selected, budget_state,
                                reason=normalized["reason"],
                            )
                        if not methods:
                            self._add_class_summary(
                                updated, file, class_info, selected, budget_state,
                                reason=normalized["reason"],
                            )
                        details["matched_method_count"] = len(methods)
            elif request_type == "file_symbols":
                path = req_path or normalized["target"]
                file = self.files.get(path)
                if file:
                    target_resolved = True
                    self._add_file_symbols(
                        updated, file, selected, budget_state,
                        reason=normalized["reason"],
                    )
                else:
                    status_override = "not_indexed" if path else "not_found"

            added = len(selected) - before
            request_omissions = updated["omitted_contexts"][omitted_before:]
            if status_override:
                status = status_override
            elif added and request_omissions:
                status = "fulfilled_limited"
            elif added:
                status = "fulfilled"
            elif request_omissions:
                status = "budget_limited"
            elif target_resolved:
                status = "already_present"
            elif req_path and req_path not in self.files:
                status = "not_indexed"
            else:
                status = "not_found"
            if request_omissions:
                details["omissions"] = deepcopy(request_omissions)
            if status in {"ambiguous", "not_indexed", "not_found", "budget_limited", "fulfilled_limited"}:
                updated.setdefault("unresolved", []).append({
                    **normalized,
                    "code": "REQUEST_TARGET_BUDGET_LIMITED" if status == "fulfilled_limited" else f"REQUEST_TARGET_{status.upper()}",
                })
            results.append({**normalized, "status": status, "added": added, **details})
            total_additions += budget_state["additions"]

        self._refresh_edges_and_guards(updated)
        self._refresh_unresolved(updated)
        self._refresh_budget(updated, total_additions)
        return updated, len(selected) - added_before, results

    @staticmethod
    def normalize_request(request: dict[str, Any]) -> dict[str, Any]:
        """Return the bounded canonical request shape without guessing unknown types."""

        request_type = str(request.get("type", "")).strip().lower()[:100]
        relation = str(request.get("relation", "")).strip().lower()[:100] or None
        line = _int_or_none(request.get("line"))
        if line is not None and line < 1:
            line = None
        return {
            "type": request_type,
            "target": str(request.get("target", "")).strip()[:1000],
            "index_method_id": str(request.get("index_method_id", "")).strip()[:1000] or None,
            "canonical_method_target": str(request.get("canonical_method_target", "")).strip()[:1000] or None,
            "symbol_key": str(request.get("symbol_key", "")).strip()[:1000] or None,
            "descriptor": str(request.get("descriptor", "")).strip()[:1000] or None,
            "path": str(request.get("path", "")).strip()[:1000] or None,
            "line": line,
            "relation": relation,
            "reason_code": str(request.get("reason_code", "MODEL_CONTEXT_REQUEST")).strip()[:200] or "MODEL_CONTEXT_REQUEST",
            "reason": str(request.get("reason", "需要补充上下文")).strip()[:1000] or "需要补充上下文",
        }

    def close(self) -> None:
        """关闭持有的共享只读索引连接。"""

        if self.index_reader:
            self.index_reader.close()
            self.index_reader = None

    def _content(self, file: dict[str, Any]) -> str:
        if "content" in file:
            return str(file["content"])
        if not self.index_reader:
            raise KeyError(file["path"])
        return self.index_reader.get_content(file["path"])

    def _build_call_edges(self) -> None:
        """优先使用索引解析的唯一目标；歧义调用只记录 gap，不猜测边。"""

        for method_id, (file, method) in self.methods.items():
            call_sites = method.get("call_sites", [])
            if call_sites:
                for call in call_sites:
                    target = call.get("resolved_target_id")
                    status = call.get("resolve_status")
                    if status == "resolved" and target in self.methods and target != method_id:
                        self.callees[method_id].add(target)
                        self.callers[target].add(method_id)
                    elif status == "ambiguous":
                        gap = {
                            "type": "symbol",
                            "code": "SYMBOL_TARGET_AMBIGUOUS",
                            "caller_method_id": method_id,
                            "path": file["path"],
                            "line": call.get("start_line"),
                            "method_name": call.get("method_name"),
                            "descriptor": call.get("method_descriptor"),
                        }
                        self.symbol_resolution_gaps.append(gap)
                        self._gaps_by_caller.setdefault(method_id, []).append(gap)
                continue
            # 兼容旧索引；只允许同类唯一或全局唯一目标。
            for call_name in method.get("calls", []):
                targets = self.methods_by_name.get(call_name, [])
                same_class = [
                    target for target in targets
                    if self.methods[target][1].get("qualified_class") == method.get("qualified_class")
                ]
                chosen = same_class if len(same_class) == 1 else (targets if len(targets) == 1 else [])
                if not chosen and len(targets) > 1:
                    gap = {
                        "type": "symbol",
                        "code": "SYMBOL_TARGET_AMBIGUOUS",
                        "caller_method_id": method_id,
                        "path": file["path"],
                        "method_name": call_name,
                    }
                    self.symbol_resolution_gaps.append(gap)
                    self._gaps_by_caller.setdefault(method_id, []).append(gap)
                for target in chosen:
                    if target != method_id:
                        self.callees[method_id].add(target)
                        self.callers[target].add(method_id)

    def _ensure_call_relations(self, method_id: str, relation: str = "both") -> None:
        """按当前切片目标懒加载调用关系，避免常驻全量 call_sites 双向图。"""

        if not self.index_reader or method_id not in self.methods:
            return
        need_callers = relation in {"callers", "both"} and method_id not in self._loaded_callers
        need_callees = relation in {"callees", "both"} and method_id not in self._loaded_callees
        need_gaps = method_id not in self._loaded_gaps
        if not (need_callers or need_callees or need_gaps):
            return
        relations = self.index_reader.get_call_relations_for_methods(
            [method_id],
            include_callers=need_callers,
            include_callees=need_callees,
        )
        if need_callers:
            for caller_id in relations["callers"][method_id]:
                if caller_id in self.methods and caller_id != method_id:
                    self.callers[method_id].add(caller_id)
                    self.callees[caller_id].add(method_id)
            self._loaded_callers.add(method_id)
        if need_callees:
            for callee_id in relations["callees"][method_id]:
                if callee_id in self.methods and callee_id != method_id:
                    self.callees[method_id].add(callee_id)
                    self.callers[callee_id].add(method_id)
            self._loaded_callees.add(method_id)
        if need_gaps:
            gaps = relations["gaps"][method_id]
            if gaps:
                self._gaps_by_caller[method_id] = gaps
                self.symbol_resolution_gaps.extend(gaps)
            self._loaded_gaps.add(method_id)

    def _methods_at(self, path: str, line: int | None) -> list[str]:
        if line is None or path not in self.files:
            return []
        matches = [
            method for method in self.files[path].get("methods", [])
            if method["start_line"] <= line <= method["end_line"]
        ]
        if not matches:
            return []
        smallest_span = min(method["end_line"] - method["start_line"] for method in matches)
        return sorted(
            str(method["id"]) for method in matches
            if method["end_line"] - method["start_line"] == smallest_span
        )

    def _method_at(self, path: str, line: int | None) -> str | None:
        matches = self._methods_at(path, line)
        return matches[0] if len(matches) == 1 else None

    def _resolve_method_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Resolve one method without guessing across ambiguous alternatives."""

        raw_target = str(request.get("target") or "")
        target = _request_target(request)
        path = request.get("path")
        line = request.get("line")
        index_method_id = request.get("index_method_id")

        for candidate in (index_method_id, raw_target):
            if candidate and candidate in self.methods:
                return self._method_resolution("resolved", [str(candidate)])

        canonical_candidates = []
        for candidate in (
            request.get("symbol_key"),
            request.get("canonical_method_target"),
            raw_target,
        ):
            if candidate and candidate not in canonical_candidates:
                canonical_candidates.append(str(candidate))
        descriptor = request.get("descriptor")
        descriptor_target = raw_target or target
        if descriptor and descriptor_target and not descriptor_target.endswith(str(descriptor)):
            canonical_candidates.insert(0, f"{descriptor_target}{descriptor}")
        for canonical in canonical_candidates:
            matches = sorted(self.methods_by_symbol_key.get(canonical, []))
            if matches:
                return self._method_resolution("resolved" if len(matches) == 1 else "ambiguous", matches)

        if path and path not in self.files:
            return self._method_resolution("not_indexed", [])
        if path and line is not None:
            matches = self._methods_at(path, line)
            if matches:
                return self._method_resolution("resolved" if len(matches) == 1 else "ambiguous", matches)

        if not target:
            return self._method_resolution("not_found", [])
        cleaned = re.sub(r":\d+$", "", target)
        tail = cleaned.split(".java#", 1)[-1] if ".java#" in cleaned else cleaned
        alias_values = {cleaned, tail}
        if "#" in tail:
            alias_values.add(tail.split("#", 1)[-1])
        alias_values.update(value.replace("$", ".") for value in tuple(alias_values))

        matches = []
        for method_id, (file, method) in self.methods.items():
            if path and file["path"] != path:
                continue
            fqcn = str(method.get("qualified_class") or method.get("class_name") or "")
            class_name = str(method.get("class_name") or "")
            name = str(method["name"])
            aliases = {
                name,
                f"{class_name}.{name}" if class_name else name,
                f"{fqcn}.{name}",
                f"{fqcn}#{name}",
            }
            aliases.update(value.replace("$", ".") for value in tuple(aliases))
            if aliases & alias_values:
                matches.append(method_id)
        matches = sorted(set(matches))
        return self._method_resolution(
            "resolved" if len(matches) == 1 else "ambiguous" if matches else "not_found",
            matches,
        )

    def _method_resolution(self, status: str, method_ids: list[str]) -> dict[str, Any]:
        alternatives = []
        for method_id in sorted(method_ids):
            file, method = self.methods[method_id]
            alternatives.append({
                "index_method_id": method_id,
                "canonical_method_target": _canonical_target(method),
                "path": file["path"],
                "line": method["start_line"],
            })
        return {"status": status, "method_ids": sorted(method_ids), "alternatives": alternatives}

    def _resolve_methods(self, target: str, path: str | None = None, line: int | None = None) -> list[str]:
        """Compatibility wrapper returning all exact resolution candidates."""

        return self._resolve_method_request({
            "target": target,
            "index_method_id": None,
            "canonical_method_target": None,
            "symbol_key": None,
            "descriptor": None,
            "path": path,
            "line": line,
        })["method_ids"]

    def _resolve_class(self, target: str | None) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if not target:
            return None
        if target in self.classes:
            return self.classes[target]
        simple = target.rsplit(".", 1)[-1]
        matches = self.classes_by_simple.get(simple, [])
        return matches[0] if len(matches) == 1 else None

    def _method_context(self, method_id: str, reason: str) -> dict[str, Any]:
        file, method = self.methods[method_id]
        lines = self._content(file).splitlines()
        start, end = method["start_line"], method["end_line"]
        snippet = "\n".join(lines[start - 1:end])
        fqcn = method.get("qualified_class") or method.get("class_name") or ""
        descriptor = method.get("descriptor") or ""
        canonical = _canonical_target(method)
        return {
            "context_id": method_id,
            "index_method_id": method_id,
            "canonical_method_target": canonical,
            "symbol_key": canonical,
            "kind": "method",
            "path": file["path"],
            "start_line": start,
            "end_line": end,
            "class_name": fqcn,
            "method_name": method["name"],
            "descriptor": descriptor,
            "signature": method.get("signature"),
            "calls": method.get("calls", []),
            "reason": reason,
            "content_sha256": hashlib.sha256(snippet.encode()).hexdigest(),
            "content": _numbered(snippet, start),
        }

    def _add_method(
        self,
        document: dict[str, Any],
        method_id: str,
        selected: set[str],
        budget_state: dict[str, int],
        reason: str,
    ) -> bool:
        if method_id in selected or method_id not in self.methods:
            return False
        return self._admit_context(
            document, self._method_context(method_id, reason), selected, budget_state,
        )

    def _add_window(
        self,
        document: dict[str, Any],
        path: str,
        line: int,
        selected: set[str],
        budget_state: dict[str, int],
        reason: str,
    ) -> bool:
        file = self.files[path]
        lines = self._content(file).splitlines()
        start = max(1, line - 12)
        end = min(len(lines), line + 12)
        context_id = f"{path}#window:{start}-{end}"
        if context_id in selected:
            return False
        snippet = "\n".join(lines[start - 1:end])
        context = {
            "context_id": context_id,
            "kind": "code_window",
            "path": path,
            "start_line": start,
            "end_line": end,
            "reason": reason,
            "content_sha256": hashlib.sha256(snippet.encode()).hexdigest(),
            "content": _numbered(snippet, start),
        }
        return self._admit_context(document, context, selected, budget_state)

    def _add_class_summary(
        self,
        document: dict[str, Any],
        file: dict[str, Any],
        class_info: dict[str, Any],
        selected: set[str],
        budget_state: dict[str, int],
        reason: str,
    ) -> bool:
        context_id = f"{class_info['id']}#summary"
        if context_id in selected:
            return False
        methods = [method for method in file.get("methods", []) if method.get("class_name") == class_info["name"]]
        content = "\n".join(method.get("signature", method["name"]) for method in methods) or "类中未解析到方法"
        context = {
            "context_id": context_id,
            "kind": "class_summary",
            "path": file["path"],
            "start_line": class_info["start_line"],
            "end_line": class_info["end_line"],
            "class_name": class_info.get("qualified_name"),
            "extends": class_info.get("extends"),
            "implements": class_info.get("implements", []),
            "reason": reason,
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "content": content,
        }
        return self._admit_context(document, context, selected, budget_state)

    def _add_file_symbols(
        self,
        document: dict[str, Any],
        file: dict[str, Any],
        selected: set[str],
        budget_state: dict[str, int],
        reason: str,
    ) -> bool:
        context_id = f"{file['path']}#symbols"
        if context_id in selected:
            return False
        rows = [f"class {item['qualified_name']}" for item in file.get("classes", [])]
        rows.extend(method.get("signature", method["name"]) for method in file.get("methods", []))
        content = "\n".join(rows) or "未解析到结构化符号"
        context = {
            "context_id": context_id,
            "kind": "file_symbols",
            "path": file["path"],
            "start_line": 1,
            "end_line": file["line_count"],
            "reason": reason,
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "content": content,
        }
        return self._admit_context(document, context, selected, budget_state)

    def _admit_context(
        self,
        document: dict[str, Any],
        context: dict[str, Any],
        selected: set[str],
        budget_state: dict[str, int],
    ) -> bool:
        context_id = str(context["context_id"])
        if context_id in selected:
            return False
        rejection = self.context_budget.rejection_reason(
            document.get("contexts", []), context, budget_state["additions"],
        )
        if rejection:
            self._record_omission(document, context, rejection)
            return False
        document.setdefault("contexts", []).append(context)
        selected.add(context_id)
        budget_state["additions"] += 1
        return True

    @staticmethod
    def _record_omission(document: dict[str, Any], context: dict[str, Any], reason: str) -> None:
        omission = {
            "context_id": context["context_id"],
            "kind": context.get("kind"),
            "path": context.get("path"),
            "reason": reason,
            "requested_reason": context.get("reason"),
            "bytes": context_bytes(context),
        }
        document.setdefault("omitted_contexts", []).append(omission)

    def _refresh_budget(self, document: dict[str, Any], additions: int) -> None:
        omissions = document.get("omitted_contexts", [])
        document["context_budget"] = {
            "status": "limited" if omissions else "within_budget",
            "limits": self.context_budget.limits,
            "usage": self.context_budget.usage(document.get("contexts", []), additions),
            "omitted_context_count": len(omissions),
        }

    def _refresh_edges_and_guards(self, document: dict[str, Any]) -> None:
        """重建切片内调用边，并标注仅作为候选事实的 Guard 关键词。"""

        context_ids = {context["context_id"] for context in document.get("contexts", [])}
        for method_id in sorted(context_ids & self.methods.keys()):
            self._ensure_call_relations(method_id)
        edges = []
        for caller in sorted(context_ids & self.callees.keys()):
            for callee in sorted(self.callees[caller]):
                if callee in context_ids:
                    edges.append({"from": caller, "to": callee, "type": "calls", "status": "fact"})
        document["edges"] = edges
        guards = []
        for context in document.get("contexts", []):
            for match in GUARD_PATTERN.finditer(context.get("content", "")):
                guards.append({
                    "context_id": context["context_id"],
                    "kind": "guard_candidate",
                    "text": match.group(0),
                    "status": "fact",
                })
        document["guards"] = guards

    def _refresh_unresolved(self, document: dict[str, Any]) -> None:
        """重建 unresolved：仅保留与当前 context 方法相关的符号歧义 + 请求级缺口 + 全局摘要。"""

        context_ids = {context["context_id"] for context in document.get("contexts", [])}
        method_ids = {
            ctx_id for ctx_id in context_ids
            if ctx_id in self.methods
        }
        relevant: list[dict[str, Any]] = []
        seen: set[str] = set()
        for method_id in sorted(method_ids):
            for gap in self._gaps_by_caller.get(method_id, []):
                marker = json.dumps(gap, ensure_ascii=False, sort_keys=True)
                if marker not in seen:
                    seen.add(marker)
                    relevant.append(deepcopy(gap))
        request_gaps = [
            item for item in document.get("unresolved", [])
            if item.get("type") != "symbol" or item.get("code") != "SYMBOL_TARGET_AMBIGUOUS"
        ]
        document["unresolved"] = relevant + request_gaps
        total_global = self._symbol_resolution_gap_count
        if total_global > len(relevant):
            document["unresolved"].append({
                "type": "symbol_summary",
                "code": "GLOBAL_SYMBOL_GAPS_OMITTED",
                "total_count": total_global,
                "relevant_count": len(relevant),
            })

    @staticmethod
    def _slice_id(candidate: dict[str, Any]) -> str:
        # v2026-08-14 修复（CONTEXT_SLICE_MISMATCH）：链身份纳入 slice 键。
        # 此前只哈希 rule_id+component+locations，同组件同 locations 的多条
        # 不同链候选共用同一 slice（AI 跨链污染、finding.sinks 与 slice 不一致）。
        # sources/sinks/propagation_paths 用 _anchor_projection 投影（只取
        # path/line/kind/method_name），防止 AI 扩展的无关字段抖动导致 slice 碎片化。
        stable = json.dumps({
            "rule_id": candidate.get("rule_id"),
            "component": candidate.get("component_name"),
            "locations": candidate.get("locations", []),
            "sources": _anchor_projection(candidate.get("sources", [])),
            "sinks": _anchor_projection(candidate.get("sinks", [])),
            "propagation_paths": _anchor_projection(candidate.get("propagation_paths", [])),
        }, ensure_ascii=False, sort_keys=True)
        return "slice_" + hashlib.sha256(stable.encode()).hexdigest()[:20]


def _anchor_projection(items: list[Any]) -> list[dict[str, Any]]:
    """锚点投影：只保留影响链身份的最小字段（path/line/kind/method_name）。

    v2026-08-14 新增：_slice_id 用此投影代替全量 json，避免 AI 在 sources/sinks
    上附加的扩展字段（如 evidence_refs、verify_status 等）抖动导致同一链生成
    不同 slice_id，也避免不同链因仅差异字段未被纳入而碰撞。
    """
    projected = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = {key: item.get(key) for key in ("path", "line", "kind", "method_name") if key in item}
        projected.append(entry)
    return projected


def finding_slice_sink_mismatch(
    finding: dict[str, Any],
    context_slice: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """产品侧自检：比对 finding.sinks 与其 slice 的 candidate.sinks 是否一致。

    v2026-08-14 新增（CONTEXT_SLICE_MISMATCH 防线）：finding 的 sinks 来自聚合
    primary，context_slice 的 candidate.sinks 是建 slice 时的链尾；二者不一致表示
    链身份错位（AI 可能为错误链下结论、证据不可回查）。返回 [] 表示一致；
    非空为 mismatch 详情（含 code/critical/finding_sinks/slice_sinks/slice_id）。
    context_slice 为 None 时返回非 critical 的 SLICE_UNAVAILABLE 记录。
    此函数是纯函数，扫描期（orchestrator）与存量回溯（CLI）共用。
    """
    if not context_slice:
        return [{"code": "SLICE_UNAVAILABLE", "critical": False}]
    finding_sinks = _anchor_projection(finding.get("sinks", []))
    slice_sinks = _anchor_projection((context_slice.get("candidate") or {}).get("sinks", []))
    if sorted(finding_sinks, key=lambda d: json.dumps(d, sort_keys=True)) == sorted(
        slice_sinks, key=lambda d: json.dumps(d, sort_keys=True)
    ):
        return []
    return [{
        "code": "FINDING_SLICE_SINK_MISMATCH",
        "critical": True,
        "finding_sinks": finding_sinks,
        "slice_sinks": slice_sinks,
        "slice_id": context_slice.get("slice_id"),
    }]


def _candidate_anchors(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    anchors = []
    for field, reason in (("locations", "rule_location"), ("sources", "taint_source"), ("sinks", "sensitive_sink")):
        for item in candidate.get(field, []):
            if isinstance(item, dict):
                anchors.append({"path": item.get("path"), "line": item.get("line"), "reason": reason})
    return anchors


def _candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "rule_id", "rule_version", "component", "component_name", "title", "description",
        "severity_hint", "confidence_tier", "evidence_level", "reachability_status", "guard_status",
        "authorization_status", "authorization_matrix", "authorization_operation", "scope_key", "chain_key",
        "locations", "entry_points", "sources", "sinks", "propagation_paths", "blocking_gaps", "coverage_gaps",
        "limitations", "platform_assumptions",
        # P1-4（2026-08-15）：规则层已算出但此前未下发的确定性事实。
        # 缺少这些字段时 AI 只能看到"候选断言 + 代码窗口"，无从区分"值流已证明到 sink 参数"
        # 与"仅控制流共现"——这是它几乎从不输出 refutes_candidate 的直接原因
        # （基线 run unresolved 135/136 = 99.3%）。
        "flow_kind", "dataflow_status", "deterministic_chain_verified",
        "operation_taxonomy", "impact_status", "final_reaching_state", "input_control",
        # P1-5 打通（2026-08-15）：路由注入规则输出的目标固定性事实。3.0.7 提示词要求
        # refutation_basis 每一项必须在 candidate.deterministic_facts 中找到对应事实；
        # 若切片不下发该字段，AI 无从输出 fixed_local_target，交叉验证永不触发
        # （safe 但无效）。顶层摘要与 deterministic_facts 双通道下发。
        "resolved_target_fixed",
        # P0①（2026-08-15）：目标注册事实——fixed_local_target 采信前提是"目标固定
        # 且可达"（已注册）；未注册目标是崩溃 DoS，不得被反证吞掉。随切片下发供 AI
        # 区分"固定且可达的误报"与"固定但未注册的 DoS"。
        "resolved_target_registered",
        # R-1（2026-08-15）：动态 receiver flag 分级——AI 区分"确认暴露且可判定"
        # （confirmed_exported_clean）与"无法排除暴露"（unresolved_flag），
        # 避免对 gap 未解析形态浪费判定。
        "receiver_flag_tier",
        # P1-5 打通（2026-08-15）：规则层产出的 sink 调用点事实——constant_sink_argument
        # 反证依赖 sink_argument_constant，no_real_call_site 反证依赖 call_site_exists
        # （红线 13 死代码）。缺这两个字段时 AI 无从输出对应 basis，交叉验证永不触发。
        "call_site_exists", "sink_argument_constant",
        # S2（2026-08-16）：发送方可达性——False = 发送方方法无 manifest 入口
        # 反向可达（SDK 死代码），支撑 sender_unreachable 反证。
        "sender_reachable", "sdk_dead_code",
    }
    summary = {key: deepcopy(value) for key, value in candidate.items() if key in allowed}
    summary["deterministic_facts"] = _deterministic_facts(candidate)
    return summary


def _deterministic_facts(candidate: dict[str, Any]) -> dict[str, Any]:
    """把规则层的确定性结论显式摊平给 AI，避免它从代码窗口重新猜测。

    只搬运规则已经算出的事实，不做任何新推断——这里多写一个字段，AI 就少猜一次。
    """

    sinks = [sink for sink in candidate.get("sinks") or [] if isinstance(sink, dict)]
    flow_kind = candidate.get("flow_kind")
    return {
        # 值流是否真正到达 sink 参数。control_to_sink 表示"仅分支条件受控"，
        # taint 引擎已确定无 untrusted 值到达 sink 实参。
        "value_flow_reaches_sink_argument": flow_kind == "source_to_sink",
        "flow_kind": flow_kind,
        "dataflow_status": candidate.get("dataflow_status"),
        "deterministic_chain_verified": candidate.get("deterministic_chain_verified") is True,
        "guard_status": candidate.get("guard_status"),
        "authorization_status": candidate.get("authorization_status"),
        "operation_taxonomy": candidate.get("operation_taxonomy"),
        "sink_effect_verified": [
            {
                "path": sink.get("path"),
                "line": sink.get("line"),
                "effect_verified": sink.get("effect_verified"),
                "resolve_status": sink.get("resolve_status"),
                "receiver_type": sink.get("receiver_type"),
            }
            for sink in sinks
        ],
        "critical_gap_codes": sorted({
            str(gap.get("code"))
            for gap in candidate.get("blocking_gaps") or []
            if isinstance(gap, dict) and gap.get("critical") is True
        }),
        # P1-5 打通（2026-08-15）：路由注入规则输出的目标固定性事实（仅 target_selection
        # 类候选产出；bulk_extras_forwarding 无目标决策，该字段为 None 时不输出）。
        "resolved_target_fixed": candidate.get("resolved_target_fixed"),
        # P0①（2026-08-15）：目标注册事实——False 表示未注册（崩溃 DoS），
        # None 表示无显式目标可查（不参与 fixed_local_target 采信前置）。
        "resolved_target_registered": candidate.get("resolved_target_registered"),
        # R-1（2026-08-15）：动态 receiver flag 分级（DYNAMIC_RECEIVER 规则产出；
        # 其他规则候选无此字段为 None 时不输出）。
        "receiver_flag_tier": candidate.get("receiver_flag_tier"),
        # P1-5 打通（2026-08-15）：sink 调用点事实——call_site_exists=False 支撑
        # no_real_call_site（红线 13 死代码），sink_argument_constant=True 支撑
        # constant_sink_argument。规则层已产出（_attach_sink_argument_facts），
        # 随切片下发供 AI 输出 basis。
        "call_site_exists": candidate.get("call_site_exists"),
        "sink_argument_constant": candidate.get("sink_argument_constant"),
        # S2（2026-08-16）：发送方可达性事实（规则层 _attach_sink_argument_facts 产出）。
        "sender_reachable": candidate.get("sender_reachable"),
        "sdk_dead_code": candidate.get("sdk_dead_code"),
    }


def _numbered(content: str, start_line: int) -> str:
    return "\n".join(f"{line_no:>6} | {line}" for line_no, line in enumerate(content.splitlines(), start_line))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _canonical_target(method: dict[str, Any]) -> str:
    """Return exactly the index symbol key used for cross-round resolution."""

    symbol_key = str(method.get("symbol_key") or "")
    if symbol_key:
        return symbol_key
    fqcn = method.get("qualified_class") or method.get("class_name") or ""
    descriptor = method.get("descriptor") or ""
    return f"{fqcn}#{method['name']}{descriptor}"


def _request_target(request: dict[str, Any]) -> str:
    return str(
        request.get("index_method_id")
        or request.get("symbol_key")
        or request.get("canonical_method_target")
        or request.get("target")
        or ""
    )


def _unique_class_matches(
    matches: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    result = []
    seen: set[tuple[str, str]] = set()
    for file, class_info in matches:
        marker = (str(file.get("path") or ""), str(class_info.get("id") or ""))
        if marker not in seen:
            seen.add(marker)
            result.append((file, class_info))
    return result


def _build_manifest_context(candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Build a traceable manifest_component context from candidate metadata."""

    component_name = candidate.get("component_name")
    component_type = candidate.get("component")
    if not component_name or not component_type:
        return None
    ctx_id = f"manifest:{component_type}:{component_name}"
    content_parts = [
        f"component_type: {component_type}",
        f"component_name: {component_name}",
        f"exported: {candidate.get('reachability_status', 'unknown')}",
        f"authorization_status: {candidate.get('authorization_status', 'unknown')}",
        f"guard_status: {candidate.get('guard_status', 'unknown')}",
    ]
    if candidate.get("entry_points"):
        content_parts.append(f"entry_points: {', '.join(candidate['entry_points'])}")
    content = "\n".join(content_parts)
    return {
        "context_id": ctx_id,
        "kind": "manifest_component",
        "path": "AndroidManifest.xml",
        # Manifest 是 XML 无代码行号。用 null 而非 0——模型会照抄输入行号，
        # 0 违反 L2ReviewOutput EvidenceReference 的 line>=1 约束导致 schema_invalid
        # （实测 run 20260808T045452Z：81/147 候选因此失败）。
        "start_line": None,
        "end_line": None,
        "component_type": component_type,
        "component_name": component_name,
        "exported": candidate.get("reachability_status") in {"reachable", "conditional"},
        "permission": candidate.get("authorization_status", "unknown"),
        "intent_filters": [],
        "authorization_matrix": [],
        "reason": "manifest_component_fact",
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content": content,
    }
