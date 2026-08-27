"""有序轻量 IR、validation-state 与跨方法符号传播。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from shared.receiver_registration import is_exact_on_receive, parse_receiver_registrations

SOURCE_METHODS = {
    "getStringExtra", "getIntExtra", "getLongExtra", "getBooleanExtra", "getParcelableExtra",
    "getSerializableExtra", "getExtras", "getData", "getDataString", "getAction",
    "getQueryParameter", "getPath", "getPathSegments", "getLastPathSegment", "readString",
    "readInt", "readLong", "readBundle", "readParcelable",
}
TRANSFORM_METHODS = {
    "toString", "trim", "substring", "concat", "append", "format", "valueOf", "parse",
    "getString", "get", "optString", "optInt", "getParcelable", "getSerializable",
}
SLOT_GET_METHODS = {
    "getStringExtra", "getIntExtra", "getLongExtra", "getBooleanExtra", "getParcelableExtra",
    "getSerializableExtra", "getString", "get", "optString", "optInt", "getParcelable",
    "getSerializable",
}
SLOT_PUT_METHODS = {
    "putExtra", "putString", "putInt", "putLong", "putBoolean", "putParcelable",
    "putSerializable", "putCharSequence",
}
SLOT_MERGE_METHODS = {"putExtras", "putAll", "fillIn"}
FRAGMENT_REFLECTION_METHODS = {"forName", "instantiate", "newInstance", "getDeclaredConstructor"}
VALIDATOR_METHODS = {
    "isallowedhttps", "isvalidurl", "validateurl", "allowedscheme", "isallowedscheme",
    "ishttpsurl", "istrustedurl",
}
GUARD_METHODS = {
    "checkCallingPermission", "enforceCallingPermission", "checkCallingOrSelfPermission",
    "enforceCallingOrSelfPermission", "checkSignatures", "checkUidSignatures",
    "enforceReadPermission", "enforceWritePermission", "enforcePermission",
    # v2026-08-09（Cluster E 误报根因）：MarketCallerVerifier 类模式
    # `getNameForUid(uid).equals("com.xiaomi.market")` 是 Android 上最常见的
    # 调用者包名校验之一，此前不在 GUARD_METHODS 导致 Binder/Provider 规则
    # 把"存在精确包名校验"的服务误报为 caller check missing。
    "getNameForUid", "getPackageInfo",
}
IDENTITY_SOURCE_METHODS = {"getCallingUid", "getCallingPid"}
ENFORCE_GUARD_METHODS = {name for name in GUARD_METHODS if name.startswith("enforce")}
CHECK_GUARD_METHODS = GUARD_METHODS - ENFORCE_GUARD_METHODS
# v2026-08-16（S8）：未解析调用只有在"可能是调用者身份校验 wrapper"时才产生
# 保守 GUARD_CALL_TARGET_UNRESOLVED gap。业务调用（DI 工厂 getInstance、getter、
# 回调、log 等）即使解析失败也不是 caller check 候选，不得阻塞确定性闭合——
# health 重跑实证：SportXmsApiImpl.finishSport 的 4 个 gap 全部来自
# getSportType/getInstance/call 等业务调用。
_GUARD_WRAPPER_RECEIVER_LEAVES = frozenset({
    "Context", "ContextWrapper", "Binder", "PackageManager", "ActivityManager",
    "ServiceManager", "AppOpsManager", "PermissionChecker", "AppOps",
})
_GUARD_WRAPPER_NAME_RE = re.compile(
    r"(?i)(?:calling|caller|permission|signature|getuid|getcalling|getpackageinfo|checkaccess|enforcecall)"
)
GUARD_STATUSES = {"absent", "present_effective", "present_bypassable", "present_partial", "unknown"}


def _is_guard_wrapper_candidate(call: dict[str, Any]) -> bool:
    """未解析/歧义调用是否可能是调用者身份校验 wrapper（S8，v2026-08-16）。

    仅当 receiver 是 Context/Binder/PackageManager 等具备调用者身份校验能力的
    类型，或方法名含调用者身份语义（calling/caller/permission/signature/uid 等）
    时，才把它当作潜在 guard wrapper 保留保守 gap。业务调用（DI 工厂、getter、
    回调、日志）即使解析失败也不是 caller check 候选。
    """

    receiver_type = str(call.get("receiver_type") or "")
    receiver_leaf = receiver_type.rsplit(".", 1)[-1].rsplit("$", 1)[-1]
    if receiver_leaf in _GUARD_WRAPPER_RECEIVER_LEAVES:
        return True
    method_name = str(call.get("method_name") or "")
    return bool(_GUARD_WRAPPER_NAME_RE.search(method_name))
OPERATION_TAXONOMY = {
    "data_disclosure",
    "persistent_state_write",
    "device_protocol_output",
    "callback_event_injection",
    "location_sensor_collection",
    "connection_session_control",
    "ui_navigation",
    "file_mutation",
    "database_mutation",
    "unknown_effect",
}
UNKNOWN_KEY = "<UNKNOWN_KEY>"
TRUST_ORDER = {"trusted": 0, "validated": 1, "maybe_untrusted": 2, "untrusted": 3}
DEFAULT_MAX_CHAINS = 256
DEFAULT_MAX_IR_STEPS = 20_000
DEFAULT_MAX_CALL_DEPTH = 32
DEFAULT_MAX_METHODS = 2_000
MAX_VALUE_LINEAGES = 256
PATH_MODEL = "linear_ir_v1"


@dataclass
class ValueFact:
    """绑定到具体 value version 的来源、信任状态与对象别名。"""

    version: str
    state: str
    source: dict[str, Any] | None
    path: list[dict[str, Any]] = field(default_factory=list)
    object_id: str | None = None
    definition: dict[str, Any] | None = None
    lineages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FlowState:
    locals: dict[str, ValueFact] = field(default_factory=dict)
    objects: dict[str, dict[str, ValueFact]] = field(default_factory=dict)
    unknown_objects: set[str] = field(default_factory=set)
    pending_validators: dict[str, tuple[str, str]] = field(default_factory=dict)


class DataFlowAnalyzer:
    """按 flow_ir 顺序解释闭包方法，并沿唯一解析边传播参数、返回值和对象 mutation。"""

    def __init__(
        self,
        files: list[dict[str, Any]],
        entry_method_ids: list[str] | None = None,
        scope_gaps: list[dict[str, Any]] | None = None,
        *,
        max_chains: int = DEFAULT_MAX_CHAINS,
        max_ir_steps: int = DEFAULT_MAX_IR_STEPS,
        max_call_depth: int = DEFAULT_MAX_CALL_DEPTH,
        max_methods: int = DEFAULT_MAX_METHODS,
    ):
        self.files = files
        self.methods: list[dict[str, Any]] = []
        self.methods_by_id: dict[str, dict[str, Any]] = {}
        self.method_file: dict[str, dict[str, Any]] = {}
        for file in files:
            for method in file.get("methods", []):
                record = {**method, "path": file["path"]}
                record.setdefault("id", f"{file['path']}#{method.get('name')}:{method.get('start_line', 1)}")
                self.methods.append(record)
                self.methods_by_id[record["id"]] = record
                self.method_file[record["id"]] = file
        self.methods.sort(key=lambda item: (str(item.get("id") or ""), str(item.get("path") or "")))
        self.entry_method_ids = set(entry_method_ids or [])
        self.gaps = list(scope_gaps or [])
        self.max_chains = max(1, int(max_chains))
        self.max_ir_steps = max(1, int(max_ir_steps))
        self.max_call_depth = max(1, int(max_call_depth))
        self.max_methods = max(1, int(max_methods))
        self.reaching_definitions: list[dict[str, Any]] = []
        self.validation_transitions: list[dict[str, Any]] = []
        self.slot_overwrites: list[dict[str, Any]] = []
        self.fragment_suppressions: list[dict[str, Any]] = []
        self._version = 0
        self._object = 0
        self._ir_steps = 0
        self._executed_methods: set[str] = set()
        self._chain_count = 0
        self._budget_gaps: list[dict[str, Any]] = []
        self._enumeration_stopped = False
        self.summaries, self.summary_fixpoint = self._compose_summaries()
        if self.summary_fixpoint["status"] != "converged":
            self.gaps.append({"code": "SUMMARY_FIXPOINT_LIMIT", "critical": True})

    def analyze_entry(self, entry_names: set[str]) -> dict[str, Any]:
        """收集所有精确入口的独立 Source→Sink chain，并保留首链兼容字段。"""

        self._reset_execution_budget()
        entries = sorted((
            method for method in self.methods
            if (self.entry_method_ids and method["id"] in self.entry_method_ids)
            or (not self.entry_method_ids and method.get("name") in entry_names)
        ), key=lambda item: (str(item.get("id") or ""), str(item.get("name") or "")))
        chains: list[dict[str, Any]] = []
        for method in entries:
            if self._enumeration_stopped:
                break
            state = self._initial_state(method)
            found, returned = self._execute_method(method, state, [], set(), [], 0)
            if returned and returned.state in {"untrusted", "maybe_untrusted"} and (returned.source or {}).get("source_kind") == "sensitive_result":
                return_instruction = next(
                    (item for item in reversed(method.get("flow_ir", [])) if item.get("op") == "return"),
                    {"line": method.get("end_line", method.get("start_line", 1))},
                )
                found.append({
                    "source": returned.source,
                    "sink": {
                        **_evidence(
                            method,
                            int(return_instruction.get("line", method.get("end_line", 1))),
                            "return sensitive result",
                            "entry_return",
                        ),
                        "taxonomy": "data_disclosure",
                        "effect_verified": True,
                    },
                    "path": [*returned.path, _evidence(
                        method,
                        int(return_instruction.get("line", method.get("end_line", 1))),
                        "return",
                        "return",
                    )],
                    "blocking_gaps": [],
                    "dataflow_status": "interprocedural" if any(
                        node.get("method_id") and node.get("method_id") != method["id"]
                        for node in returned.path
                    ) else "intraprocedural",
                    "final_reaching_state": returned.state,
                    "flow_kind": "return_disclosure",
                })
            for chain in found:
                chain["entry_method_id"] = method["id"]
                chain["entry_method_name"] = method.get("name")
                chains.append(chain)
        return self._finalize_chains(chains)

    def _reset_execution_budget(self) -> None:
        self._ir_steps = 0
        self._executed_methods = set()
        self._chain_count = 0
        self._budget_gaps = []
        self._enumeration_stopped = False

    def _add_budget_gap(
        self,
        code: str,
        *,
        usage: int,
        limit: int,
        **details: Any,
    ) -> dict[str, Any]:
        existing = next((item for item in self._budget_gaps if item.get("code") == code), None)
        if existing is not None:
            self._enumeration_stopped = True
            return existing
        gap = {
            "code": code,
            "critical": True,
            "usage": int(usage),
            "limit": int(limit),
            **details,
        }
        self._budget_gaps.append(gap)
        self._enumeration_stopped = True
        return gap

    def _finalize_chains(self, chains: list[dict[str, Any]]) -> dict[str, Any]:
        unique: dict[str, dict[str, Any]] = {}
        for chain in chains:
            chain = self._complete_chain(chain)
            unique.setdefault(chain["chain_id"], chain)
        ordered = sorted(unique.values(), key=_chain_sort_key)
        budget_gaps = _unique_gaps(self._budget_gaps)
        if budget_gaps:
            for chain in ordered:
                chain["blocking_gaps"] = _unique_gaps([*chain.get("blocking_gaps", []), *budget_gaps])
                chain["dataflow_status"] = "not_proven"
        first = ordered[0] if ordered else {
            "source": None, "sink": None, "path": [], "blocking_gaps": [],
            "dataflow_status": "not_proven", "final_reaching_state": None,
            "entry_method_id": None, "entry_method_name": None,
        }
        result = {**first, "chains": ordered}
        return self._finalize(result)

    def _complete_chain(self, chain: dict[str, Any]) -> dict[str, Any]:
        source = chain.get("source") or {}
        sink = chain.get("sink") or {}
        flow_kind = str(chain.get("flow_kind") or "source_to_sink")
        identity = {
            "entry_method_id": chain.get("entry_method_id"),
            "source": _stable_evidence_identity(source),
            "sink": _stable_evidence_identity(sink),
            "ordered_path": [
                _stable_evidence_identity(node)
                for node in chain.get("path", [])
                if isinstance(node, dict)
            ],
            "flow_kind": flow_kind,
        }
        chain_id = "dfc_" + hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        completed = {
            **chain,
            "chain_id": chain_id,
            "flow_kind": flow_kind,
            "path_model": PATH_MODEL,
            "blocking_gaps": _unique_gaps(chain.get("blocking_gaps", [])),
        }
        if any(gap.get("critical") is True for gap in completed["blocking_gaps"]):
            completed["dataflow_status"] = "not_proven"
        return completed

    def _finalize(self, result: dict[str, Any]) -> dict[str, Any]:
        result["coverage_gaps"] = _unique_gaps([*self.gaps, *self._budget_gaps])
        result["reaching_definitions"] = list(self.reaching_definitions)
        result["validation_transitions"] = list(self.validation_transitions)
        result["slot_overwrites"] = list(self.slot_overwrites)
        result["fragment_suppressions"] = list(self.fragment_suppressions)
        result["router_validation_bypasses"] = [
            {
                "finding_type": "ROUTER_VALIDATION_BYPASS",
                "routing_target": item.get("routing_target"),
                "key": item["key"],
                "old_version": item["previous_version"],
                "new_version": item.get("new_version"),
                "overwrite_operation": item["operation"],
                "overwrite": {key: value for key, value in item.items() if key not in {"code", "previous_version", "new_version", "operation", "key", "routing_target"}},
                "final_sink": result.get("sink"),
            }
            for item in self.slot_overwrites if result.get("sink")
        ]
        result["method_summaries"] = dict(self.summaries)
        result["summary_fixpoint"] = dict(self.summary_fixpoint)
        if any(
            gap.get("critical") is True
            for gap in [*result["coverage_gaps"], *result.get("blocking_gaps", [])]
        ):
            result["dataflow_status"] = "not_proven"
        result["guard_coverage"] = self.guard_coverage(result)
        return result

    def _initial_state(self, method: dict[str, Any]) -> FlowState:
        state = FlowState()
        structured = method.get("structured_parameters")
        if isinstance(structured, list) and structured:
            parameters = [item for item in structured if isinstance(item, dict) and item.get("name")]
            structured_available = True
        else:
            parameters = [{"name": name} for name in _parameter_names(
                str(method.get("parameters") or method.get("signature") or "")
            )]
            structured_available = False
        for item in parameters:
            parameter = str(item["name"])
            source_kind = str(item.get("source_kind") or "") if structured_available else ""
            if source_kind:
                source = {
                    **_evidence(method, int(method.get("start_line", 1)), parameter, "entry_parameter"),
                    "source_kind": source_kind,
                    "source_basis": item.get("source_basis"),
                    "parameter_position": item.get("position"),
                    "parameter_type": item.get("qualified_type") or item.get("normalized_type"),
                }
                object_id = self._new_object(
                    state,
                    unknown=source_kind in {"intent", "extras", "bundle", "provider_extras", "provider_operations"},
                )
                self._define(state, parameter, "untrusted", source, [source], object_id, source)
                continue
            lower = parameter.lower()
            legacy_source = not structured_available and lower in {"intent", "uri", "bundle", "data", "parcel", "extras"}
            if legacy_source:
                source = {
                    **_evidence(method, int(method.get("start_line", 1)), parameter, "entry_parameter"),
                    "source_kind": "legacy_parameter_name",
                    "source_basis": "legacy-name-fallback",
                }
                object_id = self._new_object(state, unknown=lower in {"intent", "bundle", "extras"})
                self._define(state, parameter, "untrusted", source, [source], object_id, source)
            else:
                self._define(state, parameter, "trusted", None, [], None, _evidence(
                    method, int(method.get("start_line", 1)), parameter, "entry_parameter"
                ))
        for limitation in method.get("limitations", []) or (method.get("summary") or {}).get("limitations", []):
            if isinstance(limitation, dict):
                self.gaps.append({**limitation, "method": method["id"]})
        return state

    def _execute_method(
        self,
        method: dict[str, Any],
        state: FlowState,
        prefix: list[dict[str, Any]],
        active: set[str],
        path_gaps: list[dict[str, Any]],
        depth: int,
        inherited_control: ValueFact | None = None,
    ) -> tuple[list[dict[str, Any]], ValueFact | None]:
        """线性解释单个方法，并为每个独立 lineage 产出独立 chain。

        ``flow_ir`` 只保留源码求值顺序，不声称完整 CFG/路径敏感性；未证明的 branch/return
        会附 critical gap。调用深度、方法数、IR step 或 chain 预算一旦耗尽即停止后续枚举，
        已发现 chain 也会在 finalize 时降为 not_proven。递归不展开，避免循环伪造闭合路径。
        """

        if self._enumeration_stopped:
            return [], None
        if depth > self.max_call_depth:
            self._add_budget_gap(
                "DATAFLOW_CALL_DEPTH_EXCEEDED",
                usage=depth,
                limit=self.max_call_depth,
                method=method["id"],
            )
            return [], None
        if method["id"] in active:
            gap = {
                "code": "RECURSIVE_FLOW_APPROXIMATION", "critical": True,
                "method": method["id"],
            }
            self.gaps.append(gap)
            return [], None
        if method["id"] not in self._executed_methods:
            if len(self._executed_methods) >= self.max_methods:
                self._add_budget_gap(
                    "DATAFLOW_METHOD_BUDGET_EXCEEDED",
                    usage=len(self._executed_methods) + 1,
                    limit=self.max_methods,
                    method=method["id"],
                )
                return [], None
            self._executed_methods.add(method["id"])
        active = {*active, method["id"]}
        calls = {int(call.get("ordinal", index + 1)): call for index, call in enumerate(method.get("call_sites", []))}
        flow_ir = method.get("flow_ir") or [
            {"op": "call", "ordinal": ordinal, "line": call.get("start_line")}
            for ordinal, call in sorted(calls.items())
        ]
        call_results: dict[int, ValueFact | None] = {}
        chains: list[dict[str, Any]] = []
        returned: ValueFact | None = None
        control_fact = inherited_control
        # P0-1：control_fact 的作用域栈。每项为 (block_end_line, previous_control_fact)；
        # 执行越过 block_end_line 时弹栈还原，使"分支条件可控"不再蔓延到整个方法。
        # inherited_control 不入栈——它由调用方的分支支配，在本方法内全程有效。
        control_scopes: list[tuple[int, ValueFact | None]] = []
        current_gaps = list(path_gaps)
        for instruction in flow_ir:
            if self._enumeration_stopped:
                break
            instruction_line = _instruction_line(instruction, method)
            while control_scopes and instruction_line > control_scopes[-1][0]:
                _, control_fact = control_scopes.pop()
            if self._ir_steps >= self.max_ir_steps:
                self._add_budget_gap(
                    "DATAFLOW_IR_STEP_BUDGET_EXCEEDED",
                    usage=self._ir_steps,
                    limit=self.max_ir_steps,
                    method=method["id"],
                )
                break
            self._ir_steps += 1
            operation = instruction.get("op")
            if operation == "call":
                ordinal = int(instruction.get("ordinal", 0))
                call = calls.get(ordinal)
                if not call:
                    continue
                found, call_returned = self._execute_call(
                    method, call, state, prefix, active, call_results, current_gaps, depth, control_fact
                )
                chains.extend(found)
                call_results[ordinal] = call_returned
            elif operation == "assign":
                target = str(instruction.get("target") or "")
                from_call = instruction.get("from_call_ordinal")
                if from_call and int(from_call) in call_results:
                    # 调用已在其真实执行位置完成 strong update，避免赋值 IR 再杀死返回值。
                    continue
                fact = self._eval_expr(method, state, str(instruction.get("expression") or ""), int(instruction.get("line", 1)))
                self._assign_fact(state, target, fact, method, instruction, "assignment")
            elif operation == "branch_hint":
                condition_fact = self._eval_expr(
                    method,
                    state,
                    str(instruction.get("condition") or ""),
                    int(instruction.get("line", method.get("start_line", 1))),
                )
                if condition_fact and condition_fact.state in {"untrusted", "maybe_untrusted"}:
                    block_end_line = instruction.get("block_end_line")
                    if block_end_line is None:
                        # 作用域无法可靠推断（括号未闭合/方法体被截断/旧索引无该字段）。
                        # 退回旧行为（持续到方法末尾）但显式标注，避免"未知"被当作"无限制"。
                        current_gaps = _unique_gaps([*current_gaps, {
                            "code": "CONTROL_SCOPE_UNRESOLVED",
                            "critical": True,
                            "method": method["id"],
                            "line": instruction_line,
                            "construct": "branch",
                        }])
                        control_fact = condition_fact
                    else:
                        control_scopes.append((int(block_end_line), control_fact))
                        control_fact = condition_fact
                proven = self._branch_is_proven_fail_closed(state, instruction)
                self._apply_branch_validation(method, state, instruction)
                if not proven:
                    current_gaps = _unique_gaps([*current_gaps, {
                        "code": "LINEAR_IR_PATH_SENSITIVITY_LIMITATION",
                        "critical": True,
                        "method": method["id"],
                        "line": int(instruction.get("line", method.get("start_line", 1))),
                        "construct": "branch",
                    }])
            elif operation == "return":
                from_call = instruction.get("from_call_ordinal")
                if from_call and int(from_call) in call_results:
                    returned = call_results[int(from_call)]
                else:
                    returned = self._eval_expr(
                        method, state, str(instruction.get("expression") or ""), int(instruction.get("line", 1))
                    )
                if self._return_is_conditional(method, instruction):
                    current_gaps = _unique_gaps([*current_gaps, {
                        "code": "LINEAR_IR_PATH_SENSITIVITY_LIMITATION",
                        "critical": True,
                        "method": method["id"],
                        "line": int(instruction.get("line", method.get("start_line", 1))),
                        "construct": "conditional_return",
                    }])
                    continue
                break
        return chains, returned

    def _execute_call(
        self,
        method: dict[str, Any],
        call: dict[str, Any],
        state: FlowState,
        prefix: list[dict[str, Any]],
        active: set[str],
        call_results: dict[int, ValueFact | None],
        path_gaps: list[dict[str, Any]],
        depth: int,
        control_fact: ValueFact | None,
    ) -> tuple[list[dict[str, Any]], ValueFact | None]:
        """按真实 ordinal 执行一次调用，传播多 lineage、对象槽 mutation 与返回值。

        只有唯一 resolved target 或经 owner/descriptor 验证的外部 operation 才跨边传播/作为
        Sink；未知 receiver 不升级为效果。每个到达 Sink 的 source lineage 单独生成 chain，
        不把不同 Source 的路径合并。callee 的 strong update 在调用位置完成，assignment IR
        不得再次覆盖返回值。
        """

        name = str(call.get("method_name") or "")
        arguments = [str(value) for value in call.get("arguments", [])]
        receiver_expr = str(call.get("receiver_text") or "")
        line = int(call.get("start_line", method.get("start_line", 1)))
        call_node = _call_evidence(method, call, "call")
        argument_facts = []
        current_ordinal = int(call.get("ordinal", 0))
        prior_calls = [
            item for item in method.get("call_sites", [])
            if int(item.get("ordinal", 0)) < current_ordinal
        ]
        for argument in arguments:
            nested = [
                item for item in prior_calls
                if str(item.get("method_name") or "") in argument
                and int(item.get("ordinal", 0)) in call_results
            ]
            if nested:
                argument_facts.append(call_results[int(nested[-1]["ordinal"])])
            else:
                argument_facts.append(self._eval_expr(method, state, argument, line))
        receiver_fact = self._eval_expr(method, state, receiver_expr, line) if receiver_expr else None

        if name in SLOT_PUT_METHODS | SLOT_MERGE_METHODS | {"replaceExtras"}:
            self._apply_slot_call(method, state, call, receiver_fact, argument_facts)

        if _is_validator(name) and argument_facts:
            local_name = _simple_name(arguments[0])
            fact = argument_facts[0]
            if local_name and fact and call.get("assigned_to"):
                state.pending_validators[str(call["assigned_to"])] = (local_name, fact.version)
            if local_name and fact and self._validator_fail_closed(method, call, arguments[0]):
                self._validate_current(state, local_name, fact.version, method, call_node)

        contextual_call = {
            **call,
            "containing_class": method.get("qualified_class"),
            "qualified_class": method.get("qualified_class"),
        }
        operation = classify_call_operation(
            contextual_call, str(method.get("name") or ""), str(method.get("qualified_class") or "")
        )
        target = self.methods_by_id.get(str(call.get("resolved_target_id") or ""))
        if target and not operation.get("verified"):
            operation = {**operation, "is_sink": False, "is_effect": False}
        if operation["kind"] == "fragment_reflection" and self._fragment_target_fail_closed(method, call, arguments):
            self.fragment_suppressions.append({
                "reason": "fixed_mapping_or_fail_closed_allowlist",
                "call": _call_evidence(method, call, "fragment_reflection"),
                "arguments": arguments,
            })
            operation = {**operation, "is_sink": False}

        chains: list[dict[str, Any]] = []
        direct_reaching: list[ValueFact] = []
        if operation["is_sink"]:
            direct_reaching = self._untrusted_reaching_facts(state, [*argument_facts, receiver_fact])
            for reaching in direct_reaching:
                source_identity = _stable_evidence_identity(reaching.source or {})
                reaching_indices = [
                    index for index, fact in enumerate(argument_facts)
                    if any(
                        _stable_evidence_identity(lineage.get("source") or {}) == source_identity
                        for lineage in (self._fact_lineages(fact) if fact else [])
                    )
                ]
                sink = {
                    **call_node,
                    "containing_method_name": call_node.get("method_name"),
                    "method_name": name,
                    "kind": operation["kind"],
                    "taxonomy": operation["taxonomy"],
                    "operation_name": name,
                    "receiver_type": call.get("receiver_type"),
                    "arguments": arguments,
                    "reaching_argument_indices": reaching_indices,
                    "effect_verified": operation["verified"],
                    "reaching_value_version": reaching.version,
                    "validation_state": reaching.state,
                }
                crosses_method = bool(prefix) or any(
                    node.get("method_id") and node.get("method_id") != method["id"]
                    for node in reaching.path
                )
                chain = {
                    "source": reaching.source,
                    "sink": sink,
                    "path": [*prefix, *reaching.path, call_node],
                    "blocking_gaps": _unique_gaps([
                        *path_gaps,
                        *([operation["gap"]] if operation.get("gap") else []),
                    ]),
                    "dataflow_status": "interprocedural" if crosses_method else "intraprocedural",
                    "final_reaching_state": reaching.state,
                    "flow_kind": "source_to_sink",
                }
                if self._chain_count >= self.max_chains:
                    self._add_budget_gap(
                        "DATAFLOW_CHAIN_BUDGET_EXCEEDED",
                        usage=self._chain_count,
                        limit=self.max_chains,
                        method=method["id"],
                        ordinal=call.get("ordinal"),
                    )
                    break
                self._chain_count += 1
                chains.append(chain)
            if not direct_reaching and control_fact and control_fact.state in {"untrusted", "maybe_untrusted"}:
                chains.append({
                    "source": control_fact.source,
                    "sink": {
                        **call_node,
                        "containing_method_name": call_node.get("method_name"),
                        "method_name": name,
                        "kind": operation["kind"],
                        "taxonomy": operation["taxonomy"],
                        "operation_name": name,
                        "receiver_type": call.get("receiver_type"),
                        "arguments": arguments,
                        "reaching_argument_indices": [],
                        "effect_verified": operation["verified"],
                        "validation_state": control_fact.state,
                    },
                    "path": [*prefix, *control_fact.path, call_node],
                    "blocking_gaps": _unique_gaps([
                        *path_gaps,
                        *([operation["gap"]] if operation.get("gap") else []),
                    ]),
                    "dataflow_status": "interprocedural" if prefix else "intraprocedural",
                    "final_reaching_state": control_fact.state,
                    "flow_kind": "control_to_sink",
                })

        returned: ValueFact | None = None
        if target and not operation.get("verified"):
            parameters = _method_parameter_names(target)
            callee = FlowState(
                objects=state.objects,
                unknown_objects=state.unknown_objects,
            )
            for index, parameter in enumerate(parameters):
                fact = argument_facts[index] if index < len(argument_facts) else None
                if fact:
                    fact = ValueFact(
                        fact.version,
                        fact.state,
                        fact.source,
                        [*fact.path, call_node],
                        fact.object_id,
                        fact.definition,
                        [
                            {**lineage, "path": [*lineage.get("path", []), call_node]}
                            for lineage in self._fact_lineages(fact)
                        ],
                    )
                self._assign_fact(callee, parameter, fact, target, {"line": target.get("start_line", 1)}, "parameter")
            found, returned = self._execute_method(
                target, callee, [*prefix, call_node], active, path_gaps, depth + 1, control_fact
            )
            chains.extend(found)
        elif not target and (
            call.get("resolve_status") in {"ambiguous", "unresolved"}
            and not _call_has_confirmed_gap_exemption(call, operation)
            and any(
                fact and fact.state in {"untrusted", "maybe_untrusted"}
                for fact in [*argument_facts, receiver_fact]
            )
        ):
            ambiguous = call.get("resolve_status") == "ambiguous"
            self.gaps.append({
                "code": "SYMBOL_TARGET_AMBIGUOUS" if ambiguous else "CALL_TARGET_UNRESOLVED",
                "critical": True,
                "method": name, "caller": method["id"], "ordinal": call.get("ordinal"),
            })

        if returned is None and operation.get("verified") and operation.get("taxonomy") == "data_disclosure":
            sensitive_source = {
                **call_node,
                "kind": "sensitive_result",
                "source_kind": "sensitive_result",
                "taxonomy": "data_disclosure",
                "operation_name": name,
                "arguments": arguments,
                "receiver_type": call.get("receiver_type"),
                "assigned_to": call.get("assigned_to"),
            }
            returned = self._fact("untrusted", sensitive_source, [sensitive_source])
        if returned is None:
            returned = self._external_call_result(method, state, call, receiver_fact, argument_facts)
        assigned = call.get("assigned_to")
        if assigned:
            self._assign_fact(state, str(assigned), returned, method, call, "call_result")
        return chains, returned

    def _untrusted_reaching_facts(
        self, state: FlowState, facts: list[ValueFact | None]
    ) -> list[ValueFact]:
        candidates: list[ValueFact] = []
        for fact in facts:
            if not fact:
                continue
            if fact.object_id and state.objects.get(fact.object_id):
                candidates.extend(state.objects[fact.object_id].values())
                if fact.object_id in state.unknown_objects:
                    candidates.append(fact)
            else:
                candidates.append(fact)
        unique: dict[str, ValueFact] = {}
        for fact in candidates:
            lineages = self._fact_lineages(fact)
            if not lineages and fact.state in {"untrusted", "maybe_untrusted"}:
                lineages = [{"source": fact.source, "path": list(fact.path), "state": fact.state}]
            for lineage in lineages:
                lineage_state = str(lineage.get("state") or fact.state)
                if lineage_state not in {"untrusted", "maybe_untrusted"}:
                    continue
                source = lineage.get("source")
                path = list(lineage.get("path") or fact.path)
                marker = json.dumps(
                    {
                        "source": _stable_evidence_identity(source or {}),
                        "path": [_stable_evidence_identity(node) for node in path],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                unique.setdefault(marker, ValueFact(
                    version=fact.version,
                    state=lineage_state,
                    source=source,
                    path=path,
                    object_id=None,
                    definition=fact.definition,
                    lineages=[{"source": source, "path": path, "state": lineage_state}],
                ))
        return [unique[marker] for marker in sorted(unique)]

    @staticmethod
    def _branch_is_proven_fail_closed(state: FlowState, instruction: dict[str, Any]) -> bool:
        if not instruction.get("fail_closed"):
            return False
        condition = str(instruction.get("condition") or "")
        if any(_is_validator(name) for name in re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", condition)):
            return True
        return any(
            re.fullmatch(rf"\s*!\s*{re.escape(result_name)}\s*", condition)
            for result_name in state.pending_validators
        )

    @staticmethod
    def _return_is_conditional(method: dict[str, Any], instruction: dict[str, Any]) -> bool:
        line = int(instruction.get("line", method.get("start_line", 1)))
        relative = max(0, line - int(method.get("start_line", 1)))
        lines = str(method.get("content") or "").splitlines()
        current = lines[relative] if relative < len(lines) else ""
        before_return = current.split("return", 1)[0]
        if re.search(r"\b(?:if|when|while|for)\s*\(", before_return):
            return True
        prefix = "\n".join(lines[:relative])
        return prefix.count("{") - prefix.count("}") > 1

    def _external_call_result(
        self,
        method: dict[str, Any],
        state: FlowState,
        call: dict[str, Any],
        receiver: ValueFact | None,
        arguments: list[ValueFact | None],
    ) -> ValueFact | None:
        name = str(call.get("method_name") or "")
        line = int(call.get("start_line", method.get("start_line", 1)))
        if call.get("expression_kind") == "constructor" and name in {"Intent", "Bundle"}:
            return self._fact("trusted", None, [], self._new_object(state, unknown=False))
        if name in SLOT_GET_METHODS:
            return self._read_slot(method, state, call, receiver)
        if name == "getExtras":
            source_call = _call_evidence(method, call, "source")
            if receiver and receiver.state in {"untrusted", "maybe_untrusted"} and receiver.source:
                return self._fact(
                    "maybe_untrusted" if receiver.object_id in state.unknown_objects else receiver.state,
                    receiver.source,
                    [*receiver.path, source_call],
                    receiver.object_id,
                    lineages=[
                        {**lineage, "path": [*lineage.get("path", []), source_call]}
                        for lineage in self._fact_lineages(receiver)
                    ],
                )
            if _is_trusted_source_extraction(call, str(method.get("qualified_class") or "")):
                object_id = self._new_object(state, unknown=True)
                return self._fact("untrusted", source_call, [source_call], object_id)
            return self._worst_fact(state, [fact for fact in [receiver, *arguments] if fact])
        if name in SOURCE_METHODS:
            source_call = _call_evidence(method, call, "source")
            if receiver and receiver.state in {"untrusted", "maybe_untrusted"} and receiver.source:
                return self._fact(
                    receiver.state,
                    receiver.source,
                    [*receiver.path, source_call],
                    lineages=[
                        {**lineage, "path": [*lineage.get("path", []), source_call]}
                        for lineage in self._fact_lineages(receiver)
                    ],
                )
            if _is_trusted_source_extraction(call, str(method.get("qualified_class") or "")):
                return self._fact("untrusted", source_call, [source_call])
            return self._worst_fact(state, [fact for fact in [receiver, *arguments] if fact])
        if name in TRANSFORM_METHODS or name in {"concat", "append"}:
            return self._worst_fact(state, [fact for fact in [receiver, *arguments] if fact])
        if _is_validator(name):
            return self._fact("trusted", None, [])
        return self._worst_fact(state, [fact for fact in [receiver, *arguments] if fact])

    def _eval_expr(
        self,
        method: dict[str, Any],
        state: FlowState,
        expression: str,
        line: int,
    ) -> ValueFact | None:
        value = expression.strip().rstrip(";")
        if not value:
            return None
        value = re.sub(r"^\([^()]+\)\s*", "", value)
        simple = _simple_name(value)
        if simple and simple in state.locals:
            fact = state.locals[simple]
            if fact.object_id:
                aggregate = self._object_aggregate(state, fact.object_id)
                if aggregate:
                    # Receiver 的整体状态是派生视图，不能把 object_id 回写到具体 slot 值；
                    # 否则后续 Sink 会把单个已验证 slot 再展开成整个容器的最坏状态。
                    return ValueFact(
                        version=aggregate.version,
                        state=aggregate.state,
                        source=aggregate.source or fact.source,
                        path=list(aggregate.path or fact.path),
                        object_id=fact.object_id,
                        definition=aggregate.definition,
                        lineages=list(aggregate.lineages or fact.lineages),
                    )
            return fact
        constructed = re.match(r"new\s+(?:[\w$.]*\.)?(Intent|Bundle)\b", value)
        if constructed:
            return self._fact("trusted", None, [], self._new_object(state, unknown=False))
        slot_get = re.search(
            r"(.+?)\.\s*(getStringExtra|getIntExtra|getLongExtra|getBooleanExtra|getParcelableExtra|"
            r"getSerializableExtra|getString|get|optString|optInt|getParcelable|getSerializable)\s*\(\s*([\"'][^\"']*[\"'])",
            value,
        )
        if slot_get:
            receiver = self._eval_expr(method, state, slot_get.group(1), line)
            key = _literal_key(slot_get.group(3))
            return self._read_object_slot(
                method,
                state,
                receiver,
                key,
                line,
                slot_get.group(2),
                {"method_name": slot_get.group(2), "start_line": line},
            )
        if re.search(r"\bgetIntent\s*\(\s*\)", value) and any(name in value for name in SOURCE_METHODS):
            source = _evidence(method, line, value[:200], "source")
            return self._fact("untrusted", source, [source])
        if re.search(r"\bgetIntent\s*\(\s*\)", value):
            source = _evidence(method, line, "getIntent()", "source")
            return self._fact("untrusted", source, [source], self._new_object(state, unknown=True))
        if re.fullmatch(r"(?:null|true|false|-?\d+(?:\.\d+)?[fFdDlL]?|[\"'].*[\"'])", value, re.S):
            return self._fact("trusted", None, [])
        referenced = [state.locals[name] for name in state.locals if _contains_name(value, name)]
        return self._worst_fact(state, referenced) if referenced else self._fact("trusted", None, [])

    def _read_slot(
        self,
        method: dict[str, Any],
        state: FlowState,
        call: dict[str, Any],
        receiver: ValueFact | None,
    ) -> ValueFact:
        arguments = call.get("arguments", [])
        key = _literal_key(str(arguments[0])) if arguments else None
        return self._read_object_slot(
            method, state, receiver, key,
            int(call.get("start_line", method.get("start_line", 1))), str(call.get("method_name")), call,
        )

    def _read_object_slot(
        self,
        method: dict[str, Any],
        state: FlowState,
        receiver: ValueFact | None,
        key: str | None,
        line: int,
        method_name: str,
        call: dict[str, Any] | None = None,
    ) -> ValueFact:
        if receiver and receiver.object_id:
            slots = state.objects.setdefault(receiver.object_id, {})
            if key is not None and key in slots:
                return slots[key]
            if UNKNOWN_KEY in slots:
                return slots[UNKNOWN_KEY]
            if receiver.object_id not in state.unknown_objects:
                return self._fact("trusted", None, [], definition={"line": line, "slot": key})
        extraction = _call_evidence(method, call, "source") if call else _evidence(method, line, f"{method_name}({key})", "source")
        if receiver and receiver.state in {"untrusted", "maybe_untrusted"} and receiver.source:
            return self._fact(
                receiver.state,
                receiver.source,
                [*receiver.path, extraction],
                lineages=[
                    {**lineage, "path": [*lineage.get("path", []), extraction]}
                    for lineage in self._fact_lineages(receiver)
                ],
            )
        if call and _is_trusted_source_extraction(call):
            return self._fact("untrusted", extraction, [extraction])
        return receiver or self._fact("trusted", None, [])

    def _apply_slot_call(
        self,
        method: dict[str, Any],
        state: FlowState,
        call: dict[str, Any],
        receiver: ValueFact | None,
        arguments: list[ValueFact | None],
    ) -> None:
        if not receiver or not receiver.object_id:
            return
        object_id = receiver.object_id
        slots = state.objects.setdefault(object_id, {})
        name = str(call.get("method_name") or "")
        raw_arguments = call.get("arguments", [])
        if name in SLOT_PUT_METHODS:
            key = _literal_key(str(raw_arguments[0])) if raw_arguments else None
            value = arguments[1] if len(arguments) > 1 else None
            if key is not None:
                old = slots.get(key)
                new_value = value or self._fact("trusted", None, [])
                slots[key] = new_value
                if old and old.state == "validated":
                    self._record_slot_overwrite(method, call, key, old, new_value, name)
            else:
                slots[UNKNOWN_KEY] = value or self._fact("maybe_untrusted", None, [])
                self._wildcard_overwrite(method, state, object_id, call)
            return
        source = arguments[0] if arguments else None
        if name == "replaceExtras":
            replacement = source or self._fact("trusted", None, [])
            for key, old in list(slots.items()):
                if old.state == "validated":
                    self._record_slot_overwrite(method, call, key, old, replacement, "replaceExtras")
            slots.clear()
            state.unknown_objects.discard(object_id)
        if source and source.object_id:
            for key, fact in state.objects.get(source.object_id, {}).items():
                if key == UNKNOWN_KEY:
                    continue
                slots[key] = fact
            if source.object_id in state.unknown_objects or UNKNOWN_KEY in state.objects.get(source.object_id, {}):
                self._wildcard_overwrite(method, state, object_id, call)
        elif source and source.state in {"untrusted", "maybe_untrusted"}:
            slots[UNKNOWN_KEY] = source
            self._wildcard_overwrite(method, state, object_id, call)

    def _wildcard_overwrite(self, method: dict[str, Any], state: FlowState, object_id: str, call: dict[str, Any]) -> None:
        slots = state.objects.setdefault(object_id, {})
        wildcard = slots.get(UNKNOWN_KEY) or self._fact("maybe_untrusted", _call_evidence(method, call, "source"), [])
        slots[UNKNOWN_KEY] = wildcard
        state.unknown_objects.add(object_id)
        for key, old in list(slots.items()):
            if key == UNKNOWN_KEY:
                continue
            if old.state == "validated":
                self._record_slot_overwrite(method, call, key, old, wildcard, str(call.get("method_name")))
            if old.state in {"validated", "trusted"}:
                slots[key] = self._fact("maybe_untrusted", wildcard.source or old.source, [*old.path, _call_evidence(method, call, "slot_overwrite")])

    def _record_slot_overwrite(
        self,
        method: dict[str, Any],
        call: dict[str, Any],
        key: str,
        old: ValueFact,
        new: ValueFact,
        operation: str,
    ) -> None:
        record = {
            "code": "VALIDATED_SLOT_OVERWRITTEN",
            "path": method["path"], "line": int(call.get("start_line", 1)),
            "method_id": method["id"], "ordinal": call.get("ordinal"),
            "key": key, "operation": operation, "previous_version": old.version,
            "new_version": new.version, "routing_target": call.get("receiver_text"),
        }
        self.slot_overwrites.append(record)

    def _validator_fail_closed(self, method: dict[str, Any], call: dict[str, Any], argument: str) -> bool:
        """仅接受与当前 validator 调用同一条件节点绑定的 fail-closed 分支。"""

        call_line = int(call.get("start_line", method.get("start_line", 1)))
        method_name = str(call.get("method_name") or "")
        for branch in method.get("flow_ir", []):
            if branch.get("op") != "branch_hint" or not branch.get("fail_closed"):
                continue
            if int(branch.get("line", -1)) != call_line:
                continue
            condition = str(branch.get("condition") or "")
            if method_name in condition and argument.strip() in condition:
                return True
        return False

    def _apply_branch_validation(self, method: dict[str, Any], state: FlowState, instruction: dict[str, Any]) -> None:
        condition = str(instruction.get("condition") or "")
        referenced_validators = [
            item for item in state.pending_validators
            if re.search(rf"\b{re.escape(item)}\b", condition)
        ]
        if not instruction.get("fail_closed"):
            if referenced_validators:
                self.gaps.append({
                    "code": "BRANCH_FLOW_APPROXIMATION",
                    "critical": True,
                    "method": method["id"],
                    "line": int(instruction.get("line", method.get("start_line", 1))),
                })
            return
        matched = False
        for result_name, (value_name, version) in list(state.pending_validators.items()):
            if re.fullmatch(rf"\s*!\s*{re.escape(result_name)}\s*", condition):
                self._validate_current(state, value_name, version, method, instruction)
                matched = True
        if referenced_validators and not matched:
            self.gaps.append({
                "code": "BRANCH_FLOW_APPROXIMATION",
                "critical": True,
                "method": method["id"],
                "line": int(instruction.get("line", method.get("start_line", 1))),
            })

    def _validate_current(
        self,
        state: FlowState,
        name: str,
        version: str,
        method: dict[str, Any],
        evidence: dict[str, Any],
    ) -> None:
        current = state.locals.get(name)
        if not current or current.version != version or current.state not in {"untrusted", "maybe_untrusted"}:
            return
        previous = current.state
        validation = _evidence(
            method, int(evidence.get("line", 1)), f"validate {name}", "validation"
        )
        state.locals[name] = ValueFact(
            version=current.version,
            state="validated",
            source=current.source,
            path=[*current.path, validation],
            object_id=current.object_id,
            definition=current.definition,
            lineages=[
                {
                    **lineage,
                    "state": "validated",
                    "path": [*lineage.get("path", []), validation],
                }
                for lineage in self._fact_lineages(current)
            ],
        )
        self.validation_transitions.append({
            "value": name, "version": version, "from": previous, "to": "validated",
            "path": method["path"], "line": int(evidence.get("line", 1)),
        })

    def _assign_fact(
        self,
        state: FlowState,
        target: str,
        fact: ValueFact | None,
        method: dict[str, Any],
        instruction: dict[str, Any],
        kind: str,
    ) -> ValueFact:
        line = int(instruction.get("line") or instruction.get("start_line") or method.get("start_line", 1))
        definition = _evidence(method, line, target, kind)
        if fact is None:
            return self._define(state, target, "trusted", None, [], None, definition)
        return self._define(
            state,
            target,
            fact.state,
            fact.source,
            [*fact.path, definition],
            fact.object_id,
            definition,
            lineages=[
                {**lineage, "path": [*lineage.get("path", []), definition]}
                for lineage in self._fact_lineages(fact)
            ],
        )

    def _define(
        self,
        state: FlowState,
        target: str,
        trust: str,
        source: dict[str, Any] | None,
        path: list[dict[str, Any]],
        object_id: str | None,
        definition: dict[str, Any] | None,
        lineages: list[dict[str, Any]] | None = None,
    ) -> ValueFact:
        self._version += 1
        previous = state.locals.get(target)
        fact = ValueFact(
            f"v{self._version}",
            trust,
            source,
            path,
            object_id,
            definition,
            list(lineages) if lineages is not None else self._initial_lineages(trust, source, path),
        )
        state.locals[target] = fact
        self.reaching_definitions.append({
            "value": target, "version": fact.version,
            "killed_version": previous.version if previous else None,
            "state": trust,
            "path": (definition or {}).get("path"), "line": (definition or {}).get("line"),
        })
        return fact

    def _fact(
        self,
        trust: str,
        source: dict[str, Any] | None,
        path: list[dict[str, Any]],
        object_id: str | None = None,
        definition: dict[str, Any] | None = None,
        lineages: list[dict[str, Any]] | None = None,
    ) -> ValueFact:
        self._version += 1
        return ValueFact(
            f"v{self._version}",
            trust,
            source,
            list(path),
            object_id,
            definition,
            list(lineages) if lineages is not None else self._initial_lineages(trust, source, path),
        )

    def _new_object(self, state: FlowState, unknown: bool) -> str:
        self._object += 1
        object_id = f"o{self._object}"
        state.objects[object_id] = {}
        if unknown:
            state.unknown_objects.add(object_id)
        return object_id

    @staticmethod
    def _initial_lineages(
        trust: str,
        source: dict[str, Any] | None,
        path: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if source is None:
            return []
        return [{"source": source, "path": list(path), "state": trust}]

    def _fact_lineages(self, fact: ValueFact) -> list[dict[str, Any]]:
        if fact.lineages:
            return list(fact.lineages)
        return self._initial_lineages(fact.state, fact.source, fact.path)

    def _merge_facts(
        self,
        facts: list[ValueFact],
        object_id: str | None = None,
    ) -> ValueFact | None:
        if not facts:
            return None
        if len(facts) == 1 and object_id is None:
            return facts[0]
        worst_state = max(facts, key=lambda item: TRUST_ORDER.get(item.state, 3)).state
        unique: dict[str, dict[str, Any]] = {}
        for fact in facts:
            for lineage in self._fact_lineages(fact):
                normalized = {
                    "source": _stable_evidence_identity(lineage.get("source") or {}),
                    "path": [
                        _stable_evidence_identity(node)
                        for node in lineage.get("path", [])
                        if isinstance(node, dict)
                    ],
                    "state": str(lineage.get("state") or fact.state),
                }
                marker = json.dumps(
                    normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                unique.setdefault(marker, {
                    "source": lineage.get("source"),
                    "path": list(lineage.get("path") or fact.path),
                    "state": str(lineage.get("state") or fact.state),
                })
        ordered = [unique[marker] for marker in sorted(unique)]
        if len(ordered) > MAX_VALUE_LINEAGES:
            self.gaps.append({
                "code": "DATAFLOW_LINEAGE_BUDGET_EXCEEDED",
                "critical": True,
                "usage": len(ordered),
                "limit": MAX_VALUE_LINEAGES,
            })
            ordered = ordered[:MAX_VALUE_LINEAGES]
        worst_lineages = [
            lineage for lineage in ordered
            if TRUST_ORDER.get(str(lineage.get("state")), 3) == TRUST_ORDER.get(worst_state, 3)
        ]
        primary = (worst_lineages or ordered)[0] if ordered else {
            "source": None,
            "path": [],
            "state": worst_state,
        }
        return self._fact(
            worst_state,
            primary.get("source"),
            list(primary.get("path") or []),
            object_id,
            lineages=ordered,
        )

    def _object_aggregate(self, state: FlowState, object_id: str) -> ValueFact | None:
        facts = list(state.objects.get(object_id, {}).values())
        if object_id in state.unknown_objects and not facts:
            return self._fact("maybe_untrusted", None, [], object_id)
        return self._merge_facts(facts, object_id) if facts else None

    def _worst_fact(self, state: FlowState, facts: list[ValueFact]) -> ValueFact | None:
        expanded: list[ValueFact] = []
        for fact in facts:
            if fact.object_id and state.objects.get(fact.object_id):
                aggregate = self._object_aggregate(state, fact.object_id)
                expanded.append(aggregate or fact)
            else:
                expanded.append(fact)
        return self._merge_facts(expanded)

    def _fragment_target_fail_closed(
        self, method: dict[str, Any], call: dict[str, Any], arguments: list[str]
    ) -> bool:
        name = str(call.get("method_name") or "")
        expression = (
            arguments[0] if name == "forName" and arguments
            else arguments[-1] if name == "instantiate" and arguments
            else str(call.get("receiver_text") or "")
        ).strip()
        if re.fullmatch(r"(?:[\"'][^\"']+[\"']|[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\.class(?:\.getName\(\))?)", expression):
            return True
        variable = _simple_name(expression)
        if not variable:
            return False
        content = str(method.get("content") or "")
        escaped = re.escape(variable)
        allowlist = re.search(
            rf"if\s*\(\s*(?:!\s*[\w.$]*(?:ALLOWED|allowlist|allowed)[\w.$]*\s*\.\s*contains\s*\(\s*{escaped}\s*\)|"
            rf"[\w.$]*(?:ALLOWED|allowlist|allowed)[\w.$]*\s*\.\s*contains\s*\(\s*{escaped}\s*\)\s*==\s*false)\s*\)\s*"
            rf"(?:\{{\s*)?(?:return\b|throw\s+new\b)",
            content,
            re.I | re.S,
        )
        if allowlist:
            return True
        fixed_mapping = bool(
            re.search(rf"switch\s*\(\s*{escaped}\s*\)", content)
            and re.search(r"default\s*:\s*(?:return\b|throw\s+new\b)", content, re.S)
            and re.search(r"(?:\.class(?:\.getName\(\))?|[\"'][A-Za-z_$][\w$.]+[\"'])", content)
        )
        return fixed_mapping

    def _method_operation_summary(self, method: dict[str, Any]) -> dict[str, Any]:
        """输出可复用的结构化副作用摘要；未知 receiver 不伪装成已确认效果。"""

        side_effects: list[dict[str, Any]] = []
        callbacks: list[dict[str, Any]] = []
        device_protocol: list[dict[str, Any]] = []
        for call in method.get("call_sites", []):
            operation = classify_operation_taxonomy(call, str(method.get("name") or ""), str(method.get("qualified_class") or ""))
            if not operation.get("is_effect"):
                continue
            effect = {
                "taxonomy": operation["taxonomy"],
                "kind": operation["kind"],
                "verified": operation["verified"],
                "call": _call_evidence(method, call, "side_effect"),
                "arguments": list(call.get("arguments", [])),
            }
            if operation.get("gap"):
                effect["gap"] = operation["gap"]
            side_effects.append(effect)
            if operation["taxonomy"] == "callback_event_injection":
                callbacks.append(effect)
            if operation["taxonomy"] == "device_protocol_output":
                device_protocol.append(effect)
        indexed = method.get("summary") or {}
        preconditions = [
            {
                "condition": str(item.get("condition") or ""),
                "line": int(item.get("line", method.get("start_line", 1))),
                "fail_closed": bool(item.get("fail_closed")),
            }
            for item in method.get("flow_ir", []) if item.get("op") == "branch_hint"
        ]
        return {
            "side_effects": side_effects,
            "written_fields": sorted(set(indexed.get("field_writes", []))),
            "callbacks": callbacks,
            "device_protocol": device_protocol,
            "preconditions": preconditions,
        }

    def _compose_summaries(self) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """对有限参数事实集合做单调 worklist 合成，递归通过集合稳定自然收敛。"""

        summaries: dict[str, dict[str, Any]] = {}
        for method in self.methods:
            parameters = _method_parameter_names(method)
            indexed = method.get("summary") or {}
            operation_summary = self._method_operation_summary(method)
            summaries[method["id"]] = {
                "parameters": parameters,
                "return": {parameters.index(name) for name in indexed.get("parameter_to_return", []) if name in parameters},
                "sink": {
                    parameters.index(name)
                    for item in indexed.get("parameter_to_sink", [])
                    for name in item.get("parameters", []) if name in parameters
                },
                "mutations": {
                    parameters.index(item["parameter"])
                    for item in indexed.get("parameter_slot_mutations", []) if item.get("parameter") in parameters
                },
                **operation_summary,
            }
        # 每轮若未稳定至少新增一个有限的 (method, category, parameter) 事实；
        # 因而以事实格大小加一作为真实收敛上界，不使用会截断长调用链的固定轮数。
        limit = 1 + sum(3 * len(value["parameters"]) for value in summaries.values())
        for iteration in range(1, limit + 1):
            changed = False
            for method in self.methods:
                summary = summaries[method["id"]]
                parameters = summary["parameters"]
                returns = [str(item.get("expression") or "") for item in method.get("flow_ir", []) if item.get("op") == "return"]
                for call in method.get("call_sites", []):
                    target = summaries.get(str(call.get("resolved_target_id") or ""))
                    if not target:
                        continue
                    for category in ("sink", "mutations"):
                        for target_index in target[category]:
                            arguments = call.get("arguments", [])
                            if target_index >= len(arguments):
                                continue
                            for index, parameter in enumerate(parameters):
                                if _contains_name(str(arguments[target_index]), parameter) and index not in summary[category]:
                                    summary[category].add(index)
                                    changed = True
                    if call.get("assigned_to") and any(_contains_name(value, str(call["assigned_to"])) for value in returns):
                        for target_index in target["return"]:
                            arguments = call.get("arguments", [])
                            if target_index >= len(arguments):
                                continue
                            for index, parameter in enumerate(parameters):
                                if _contains_name(str(arguments[target_index]), parameter) and index not in summary["return"]:
                                    summary["return"].add(index)
                                    changed = True
            if not changed:
                serializable = {
                    method_id: {
                        **value,
                        "return": sorted(value["return"]),
                        "sink": sorted(value["sink"]),
                        "mutations": sorted(value["mutations"]),
                    }
                    for method_id, value in summaries.items()
                }
                return serializable, {"status": "converged", "iterations": iteration, "limit": limit}
        serializable = {
            method_id: {**value, "return": sorted(value["return"]), "sink": sorted(value["sink"]), "mutations": sorted(value["mutations"])}
            for method_id, value in summaries.items()
        }
        return serializable, {"status": "limit_reached", "iterations": limit, "limit": limit}

    def fragment_reflection_analysis(self, flow: dict[str, Any] | None = None) -> dict[str, Any]:
        """汇总外部 class-name 到 Fragment 反射构造点的确定性数据流。"""

        flow = flow or self.analyze_entry({"onCreate", "onNewIntent"})
        sink = flow.get("sink") or {}
        relevant_calls = [
            {
                "method": method["id"],
                "call": _call_evidence(method, call, "fragment_reflection"),
                "receiver_type": call.get("receiver_type"),
                "arguments": list(call.get("arguments", [])),
            }
            for method in self.methods for call in method.get("call_sites", [])
            if classify_operation_taxonomy(call, str(method.get("name") or ""), str(method.get("qualified_class") or "")).get("kind") == "fragment_reflection"
        ]
        gaps = [
            gap for gap in [*flow.get("coverage_gaps", []), *flow.get("blocking_gaps", [])]
            if gap.get("critical") is True
        ]
        if sink.get("kind") == "fragment_reflection":
            return {
                "status": "verified" if not gaps else "not_proven",
                "sink_kind": "fragment_reflection",
                "source": flow.get("source"),
                "class_name_sink": sink,
                "method_path": [node.get("method_id") for node in flow.get("path", []) if node.get("method_id")],
                "allowlist": "absent",
                "fixed_mapping": False,
                "coverage_gaps": _unique_gaps(gaps),
            }
        if self.fragment_suppressions:
            return {
                "status": "suppressed",
                "sink_kind": "fragment_reflection",
                "source": None,
                "class_name_sink": self.fragment_suppressions[0]["call"],
                "allowlist": "fail_closed_or_fixed_mapping",
                "fixed_mapping": True,
                "coverage_gaps": [],
            }
        if relevant_calls:
            unresolved = {
                "code": "FRAGMENT_REFLECTION_DATAFLOW_UNRESOLVED",
                "critical": True,
                "candidate_count": len(relevant_calls),
            }
            return {
                "status": "not_proven", "sink_kind": "fragment_reflection",
                "source": None, "class_name_sink": relevant_calls[0]["call"],
                "allowlist": "unknown", "fixed_mapping": False,
                "coverage_gaps": _unique_gaps([*gaps, unresolved]),
            }
        return {"status": "not_found", "sink_kind": "fragment_reflection", "coverage_gaps": []}

    def _dispatch_sources(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        sources = []
        for call in entry.get("call_sites", []):
            name = str(call.get("method_name") or "")
            if name != "getAction" and name not in SLOT_GET_METHODS:
                continue
            arguments = call.get("arguments", [])
            key = _literal_key(str(arguments[0])) if name != "getAction" and arguments else None
            sources.append({
                "variable": call.get("assigned_to"),
                "event": "action" if name == "getAction" else "extra",
                "key": key,
                "source": _call_evidence(entry, call, "service_event_source"),
            })
        return sources

    def _condition_regions(
        self, entry: dict[str, Any], sources: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        content = str(entry.get("content") or "")
        start_line = int(entry.get("start_line", 1))
        regions: list[dict[str, Any]] = []
        for source in sources:
            variable = str(source.get("variable") or "")
            if not variable:
                key = str(source.get("key") or "")
                method_name = "getAction" if source.get("event") == "action" else None
                for line_match in re.finditer(r"[^\n]*\bif\s*\([^\n]*", content):
                    line_text = line_match.group(0)
                    if method_name and method_name not in line_text:
                        continue
                    if key and key not in line_text:
                        continue
                    if not method_name and not key:
                        continue
                    opening = content.find("{", line_match.start(), min(len(content), line_match.end() + 200))
                    if opening >= 0:
                        closing = _matching_brace(content, opening)
                        region_end = closing if closing is not None else line_match.end()
                    else:
                        semicolon = content.find(";", line_match.end())
                        region_end = semicolon if semicolon >= 0 else line_match.end()
                    comparison = re.search(r"(?:==|!=)\s*(-?\d+)|(-?\d+)\s*(?:==|!=)", line_text)
                    literals = re.findall(r"[\"']([^\"']+)[\"']", line_text)
                    event_value = next((item for item in (comparison.groups() if comparison else ()) if item), None)
                    regions.append({
                        **source,
                        "event_value": event_value or (literals[-1] if literals else None),
                        "condition": line_text.strip()[:300],
                        "condition_line": start_line + content.count("\n", 0, line_match.start()),
                        "start_line": start_line + content.count("\n", 0, line_match.start()),
                        "end_line": start_line + content.count("\n", 0, region_end),
                    })
                continue
            for switch in re.finditer(rf"\bswitch\s*\(\s*{re.escape(variable)}\s*\)\s*\{{", content):
                closing = _matching_brace(content, switch.end() - 1)
                if closing is None:
                    continue
                body_start, body_end = switch.end(), closing
                cases = list(re.finditer(r"\bcase\s+([^:]+)\s*:", content[body_start:body_end]))
                for index, case in enumerate(cases):
                    absolute_start = body_start + case.start()
                    absolute_end = body_start + (cases[index + 1].start() if index + 1 < len(cases) else body_end - body_start)
                    event = case.group(1).strip().strip('"\'')
                    regions.append({
                        **source,
                        "event_value": event,
                        "condition": f"switch({variable}) case {case.group(1).strip()}",
                        "condition_line": start_line + content.count("\n", 0, absolute_start),
                        "start_line": start_line + content.count("\n", 0, absolute_start),
                        "end_line": start_line + content.count("\n", 0, absolute_end),
                    })
            for line_match in re.finditer(r"[^\n]*\bif\s*\([^\n]*", content):
                line_text = line_match.group(0)
                if not _contains_name(line_text, variable):
                    continue
                opening = content.find("{", line_match.start(), min(len(content), line_match.end() + 200))
                if opening >= 0:
                    closing = _matching_brace(content, opening)
                    region_end = closing if closing is not None else line_match.end()
                else:
                    semicolon = content.find(";", line_match.end())
                    region_end = semicolon if semicolon >= 0 else line_match.end()
                literals = re.findall(r"[\"']([^\"']+)[\"']", line_text)
                regions.append({
                    **source,
                    "event_value": literals[0] if literals else None,
                    "condition": line_text.strip()[:300],
                    "condition_line": start_line + content.count("\n", 0, line_match.start()),
                    "start_line": start_line + content.count("\n", 0, line_match.start()),
                    "end_line": start_line + content.count("\n", 0, region_end),
                })
        return regions

    def _effects_from_region(
        self, entry: dict[str, Any], region: dict[str, Any], gap_code_prefix: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        calls = [
            call for call in entry.get("call_sites", [])
            if int(region["start_line"]) <= int(call.get("start_line", 0)) <= int(region["end_line"])
            and call.get("method_name") not in SOURCE_METHODS | SLOT_GET_METHODS | {"equals", "contains"}
        ]
        effects: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        for call in calls:
            operation = classify_operation_taxonomy(call, str(entry.get("name") or ""), str(entry.get("qualified_class") or ""))
            if operation.get("is_effect") and operation.get("verified"):
                effects.append({
                    "taxonomy": operation["taxonomy"], "kind": operation["kind"],
                    "verified": True,
                    "call": _call_evidence(entry, call, "side_effect"),
                    "method_path": [entry["id"]],
                    "gap": operation.get("gap"),
                })
                continue
            target = self.methods_by_id.get(str(call.get("resolved_target_id") or ""))
            if target:
                nested, nested_gaps = self._collect_method_effects(target, [entry["id"], target["id"]], set())
                effects.extend(nested)
                gaps.extend(nested_gaps)
            elif call.get("resolve_status") in {"ambiguous", "unresolved"}:
                gaps.append({
                    "code": f"{gap_code_prefix}_BRANCH_TARGET_"
                    f"{'AMBIGUOUS' if call.get('resolve_status') == 'ambiguous' else 'UNRESOLVED'}",
                    "critical": True, "method": call.get("method_name"),
                    "caller": entry["id"], "ordinal": call.get("ordinal"),
                })
        return effects, gaps

    def _collect_method_effects(
        self, method: dict[str, Any], path: list[str], active: set[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if method["id"] in active or len(path) > 12:
            return [], [{"code": "EFFECT_PATH_RECURSIVE_OR_TOO_DEEP", "critical": True, "method": method["id"]}]
        active = {*active, method["id"]}
        effects: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        for call in method.get("call_sites", []):
            operation = classify_operation_taxonomy(call, str(method.get("name") or ""), str(method.get("qualified_class") or ""))
            if operation.get("is_effect"):
                effects.append({
                    "taxonomy": operation["taxonomy"], "kind": operation["kind"],
                    "verified": operation["verified"],
                    "call": _call_evidence(method, call, "side_effect"),
                    "method_path": path,
                    "gap": operation.get("gap"),
                })
                if operation.get("gap"):
                    gaps.append(operation["gap"])
                continue
            target = self.methods_by_id.get(str(call.get("resolved_target_id") or ""))
            if target:
                nested, nested_gaps = self._collect_method_effects(target, [*path, target["id"]], active)
                effects.extend(nested)
                gaps.extend(nested_gaps)
            elif call.get("resolve_status") == "ambiguous":
                gaps.append({
                    "code": "EFFECT_CALL_TARGET_AMBIGUOUS", "critical": True,
                    "method": call.get("method_name"), "caller": method["id"], "ordinal": call.get("ordinal"),
                })
        return effects, gaps

    def started_service_state_machine(self, flow: dict[str, Any] | None = None) -> dict[str, Any]:
        """将 onStartCommand 的 action/extra 条件区域绑定到区域内可达的真实副作用。

        仅同时存在外部 dispatch source、条件 region、verified effect 且无 critical gap 时状态为
        verified。存在可达效果却无法绑定 event/source 时保守返回 not_proven；该结果描述 started
        service 的事件→效果状态机，不等同于生命周期方法存在本身就是漏洞。
        """

        entry = next((method for method in self.methods if method.get("name") == "onStartCommand"), None)
        if not entry:
            return {"status": "not_found", "transitions": [], "coverage_gaps": []}
        flow = flow or self.analyze_entry({"onStartCommand"})
        sources = self._dispatch_sources(entry)
        regions = self._condition_regions(entry, sources)
        transitions: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        for region in regions:
            effects, effect_gaps = self._effects_from_region(entry, region, "SERVICE_EVENT_EFFECT")
            gaps.extend(effect_gaps)
            for effect in effects:
                transitions.append({
                    "event": region.get("event_value") or region.get("event"),
                    "key": region.get("key"),
                    "source": region["source"],
                    "condition_line": region["condition_line"],
                    "condition": region["condition"],
                    "method_path": effect["method_path"],
                    "effect": effect["call"],
                    "effect_taxonomy": effect["taxonomy"],
                    "effect_kind": effect["kind"],
                    "preconditions": [region["condition"]],
                    "verified": effect["verified"],
                })
        reachable = self._reachable_side_effects(entry)
        if reachable and not transitions:
            gaps.append({
                "code": "SERVICE_EVENT_EFFECT_BINDING_UNKNOWN", "critical": True,
                "reachable_effect_count": len(reachable),
            })
        if not sources and reachable:
            gaps.append({"code": "SERVICE_EVENT_SOURCE_UNKNOWN", "critical": True})
        gaps = _unique_gaps(gaps)
        deterministic = bool(
            transitions
            and all(item["verified"] for item in transitions)
            and not any(gap.get("critical") is True for gap in gaps)
        )
        return {
            "status": "verified" if deterministic else "not_proven",
            "entry_method": entry["id"],
            "events": sorted({str(item["event"]) for item in transitions if item.get("event")}),
            "transitions": transitions,
            "dataflow": flow,
            "reachable_effects": reachable,
            "coverage_gaps": gaps,
        }

    def _analyze_exact_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        self._reset_execution_budget()
        state = self._initial_state(entry)
        chains, _ = self._execute_method(entry, state, [], set(), [], 0)
        for chain in chains:
            chain["entry_method_id"] = entry["id"]
            chain["entry_method_name"] = entry.get("name")
        return self._finalize_chains(chains)

    def receiver_input_analysis(self, entry: dict[str, Any] | None = None) -> dict[str, Any]:
        """证明 onReceive 外部值直接或经条件控制进入副作用。"""

        targets = [method for method in self.methods if is_exact_on_receive(method)]
        if entry is None:
            if len(targets) != 1:
                return {
                    "status": "not_proven", "effects": [], "transitions": [],
                    "coverage_gaps": [{
                        "code": "RECEIVER_TARGET_AMBIGUOUS" if targets else "RECEIVER_TARGET_UNRESOLVED",
                        "critical": True, "candidate_count": len(targets),
                    }],
                }
            entry = targets[0]
        flow = self._analyze_exact_entry(entry)
        transitions: list[dict[str, Any]] = []
        reachable = {str(entry["id"])}
        pending = [entry]
        while pending:
            current = pending.pop()
            for call in current.get("call_sites", []):
                target = self.methods_by_id.get(str(call.get("resolved_target_id") or ""))
                if target and str(target["id"]) not in reachable:
                    reachable.add(str(target["id"]))
                    pending.append(target)
        gaps: list[dict[str, Any]] = [
            gap for gap in self.gaps
            if not (gap.get("caller") or gap.get("method"))
            or str(gap.get("caller") or gap.get("method")) in reachable
        ]
        sources = self._dispatch_sources(entry)
        for region in self._condition_regions(entry, sources):
            effects, effect_gaps = self._effects_from_region(entry, region, "RECEIVER_INPUT_EFFECT")
            gaps.extend(effect_gaps)
            for effect in effects:
                transitions.append({
                    "event": region.get("event_value") or region.get("event"),
                    "key": region.get("key"), "source": region["source"],
                    "condition_line": region["condition_line"], "condition": region["condition"],
                    "method_path": effect["method_path"], "effect": effect["call"],
                    "effect_taxonomy": effect["taxonomy"], "effect_kind": effect["kind"],
                    "preconditions": [region["condition"]], "verified": effect["verified"],
                })
        effects = []
        if flow.get("sink"):
            effects.append({
                "source": flow.get("source"), "effect": flow["sink"],
                "effect_taxonomy": flow["sink"].get("taxonomy"),
                "effect_kind": flow["sink"].get("kind"),
                "method_path": [node.get("method_id") for node in flow.get("path", []) if node.get("method_id")],
                "path": flow.get("path", []),
                "verified": flow["sink"].get("effect_verified") is True,
            })
        gaps.extend(flow.get("blocking_gaps", []))
        if not effects and not transitions and self._reachable_side_effects(entry):
            gaps.append({"code": "RECEIVER_INPUT_EFFECT_BINDING_UNKNOWN", "critical": True})
        gaps = _unique_gaps(gaps)
        deterministic = bool(
            (effects or transitions)
            and all(item.get("verified") for item in [*effects, *transitions])
            and not any(gap.get("critical") is True for gap in gaps)
        )
        return {
            "status": "verified" if deterministic else "not_proven",
            "on_receive": entry["id"], "effects": effects, "transitions": transitions,
            "dataflow": flow, "coverage_gaps": gaps,
        }

    def dynamic_receiver_bindings(self, manifest: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """使用共享注册解析结果精确绑定 Receiver.onReceive(Context, Intent) 与副作用。"""

        manifest = manifest or {}
        bindings = []
        exact_receivers = [method for method in self.methods if is_exact_on_receive(method)]
        for file in self.files:
            for registration in parse_receiver_registrations(file, manifest):
                method = self.methods_by_id.get(str(registration.get("method_id") or ""))
                if method is None:
                    continue
                call = registration["call"]
                receiver_expr = str(registration.get("receiver_expression") or "")
                receiver_qualified_class = registration.get("receiver_class")
                receiver_class = (
                    str(receiver_qualified_class).rsplit(".", 1)[-1]
                    if receiver_qualified_class else None
                )
                target_gaps: list[dict[str, Any]] = []
                if receiver_qualified_class:
                    exact_targets = [
                        candidate for candidate in exact_receivers
                        if candidate.get("qualified_class") == receiver_qualified_class
                    ]
                    if exact_targets:
                        targets = exact_targets
                    else:
                        targets = [
                            candidate for candidate in exact_receivers
                            if str(candidate.get("qualified_class") or "").rsplit(".", 1)[-1] == receiver_class
                        ]
                        if len(targets) == 1:
                            target_gaps.append({
                                "code": "RECEIVER_CLASS_QUALIFICATION_UNRESOLVED",
                                "critical": True,
                                "receiver_class": receiver_qualified_class,
                                "resolved_qualified_class": targets[0].get("qualified_class"),
                            })
                elif receiver_expr.strip().startswith("new BroadcastReceiver"):
                    targets = [
                        candidate for candidate in exact_receivers
                        if candidate.get("path") == method.get("path")
                        and candidate.get("qualified_class") == method.get("qualified_class")
                    ]
                else:
                    targets = []
                gaps: list[dict[str, Any]] = [
                    *registration.get("coverage_gaps", []), *target_gaps,
                ]
                analysis: dict[str, Any] | None = None
                registration_effects: list[dict[str, Any]] = []
                if len(targets) == 1:
                    analysis = self.receiver_input_analysis(targets[0])
                    gaps.extend(analysis.get("coverage_gaps", []))
                    if any(
                        item.get("verified") is True
                        for item in analysis.get("transitions", [])
                    ):
                        gaps = [
                            gap for gap in gaps
                            if gap.get("code") != "LINEAR_IR_PATH_SENSITIVITY_LIMITATION"
                        ]
                    if not any(
                        item.get("verified") is True
                        for item in [
                            *analysis.get("effects", []),
                            *analysis.get("transitions", []),
                        ]
                    ):
                        registration_effects = [
                            {
                                "source": None,
                                "effect": item["call"],
                                "effect_taxonomy": item["effect_taxonomy"],
                                "effect_kind": item["effect_kind"],
                                "method_path": item["method_path"],
                                "path": item["method_path"],
                                "verified": True,
                            }
                            for item in self._reachable_side_effects(targets[0])
                            if item.get("verified") is True
                        ]
                        if registration_effects:
                            gaps = [
                                gap for gap in gaps
                                if gap.get("code") != "RECEIVER_INPUT_EFFECT_BINDING_UNKNOWN"
                            ]
                else:
                    gaps.append({
                        "code": "RECEIVER_TARGET_AMBIGUOUS" if len(targets) > 1 else "RECEIVER_TARGET_UNRESOLVED",
                        "critical": True,
                        "receiver_expression": receiver_expr,
                        "receiver_class": receiver_class,
                        "candidate_count": len(targets),
                    })
                gaps = _unique_gaps(gaps)
                effective_effects = list((analysis or {}).get("effects", [])) or registration_effects
                transitions = list((analysis or {}).get("transitions", []))
                attacker_actions = {
                    str(item.get("action"))
                    for item in registration.get("action_authorization", [])
                    if item.get("action") and item.get("status") != "protected"
                }
                if transitions:
                    confirmed_effects = [
                        item for item in transitions
                        if item.get("verified") is True
                        and str(item.get("event") or "") in attacker_actions
                    ]
                else:
                    confirmed_effects = [
                        item for item in effective_effects
                        if item.get("verified") is True and attacker_actions
                    ]
                effect_items = [*effective_effects, *transitions]
                verified_effects = [item for item in effect_items if item.get("verified") is True]
                binding_complete = bool(
                    registration.get("reportable")
                    and registration.get("externally_reachable")
                    and confirmed_effects
                    and not any(gap.get("critical") is True for gap in gaps)
                )
                flag = registration.get("flag_expression")
                method_paths = [
                    [str(method_id) for method_id in (item.get("method_path") or []) if method_id]
                    for item in confirmed_effects
                ]
                flow_status = str(((analysis or {}).get("dataflow") or {}).get("dataflow_status") or "")
                if confirmed_effects:
                    dataflow_status = "interprocedural" if (
                        flow_status == "interprocedural" or any(len(set(path)) > 1 for path in method_paths)
                    ) else "intraprocedural"
                else:
                    dataflow_status = "not_proven"
                bindings.append({
                    "registration": {
                        **_call_evidence(method, call, "receiver_registration"),
                        "api_family": registration.get("api_family"),
                        "overload": registration.get("overload"),
                        "platform_branch": registration.get("platform_branch"),
                        "receiver_expression": receiver_expr,
                        "filter_expression": registration.get("filter_expression"),
                        "permission_expression": registration.get("permission_expression"),
                        "flags_expression": registration.get("flags_expression"),
                        "flag": flag if flag is not None else "legacy_unspecified",
                        "flag_value": registration.get("flag_value"),
                        "flags_value": registration.get("flags_value"),
                        "flag_status": registration.get("flag_status"),
                        "export_status": registration.get("export_status"),
                        "permission": registration.get("permission"),
                        "permission_status": registration.get("permission_status"),
                        "permission_policy": registration.get("permission_policy"),
                        "actions": registration.get("actions", []),
                        "unresolved_action_expressions": registration.get("unresolved_action_expressions", []),
                        "action_authorization": registration.get("action_authorization", []),
                        "protected_actions_only": registration.get("protected_actions_only", False),
                        "externally_reachable": registration.get("externally_reachable"),
                        "local_broadcast": registration.get("local_broadcast"),
                        "reportable": registration.get("reportable"),
                    },
                    "api_family": registration.get("api_family"),
                    "overload": registration.get("overload"),
                    "receiver_expression": receiver_expr,
                    "receiver_class": receiver_class,
                    "receiver_qualified_class": receiver_qualified_class,
                    "filter_expression": registration.get("filter_expression"),
                    "permission_expression": registration.get("permission_expression"),
                    "flags_expression": registration.get("flags_expression"),
                    "platform_branch": registration.get("platform_branch"),
                    "flag": flag if flag is not None else "legacy_unspecified",
                    "flag_value": registration.get("flag_value"),
                    "flags_value": registration.get("flags_value"),
                    "flag_status": registration.get("flag_status"),
                    "export_status": registration.get("export_status"),
                    "externally_reachable": registration.get("externally_reachable"),
                    "local_broadcast": registration.get("local_broadcast"),
                    "permission": registration.get("permission"),
                    "permission_status": registration.get("permission_status"),
                    "permission_policy": registration.get("permission_policy"),
                    "actions": registration.get("actions", []),
                    "unresolved_action_expressions": registration.get("unresolved_action_expressions", []),
                    "action_authorization": registration.get("action_authorization", []),
                    "protected_actions_only": registration.get("protected_actions_only", False),
                    "reportable": registration.get("reportable", False),
                    "on_receive": targets[0]["id"] if len(targets) == 1 else None,
                    "effects": effective_effects,
                    "transitions": transitions,
                    "confirmed_effects": confirmed_effects,
                    "effect_path": [
                        item.get("path") or item.get("method_path")
                        for item in effect_items
                    ],
                    "effect_binding_proven": bool(confirmed_effects),
                    "dataflow_status": dataflow_status,
                    "binding_complete": binding_complete,
                    "deterministic": binding_complete,
                    "coverage_gaps": gaps,
                })
        return bindings

    def effect_chains(self, entry_method_id: str) -> list[dict[str, Any]]:
        """返回指定 concrete entry 可达的每个 typed effect，不要求效果参数携带 taint。"""

        entry = self.methods_by_id.get(str(entry_method_id))
        if entry is None:
            return []
        chains = []
        for effect in self._reachable_side_effects(entry):
            if not effect.get("verified"):
                continue
            sink = dict(effect["call"])
            sink.update({
                "kind": effect["effect_kind"],
                "taxonomy": effect["effect_taxonomy"],
                "effect_verified": True,
            })
            method_path = list(effect.get("method_path") or [entry["id"]])
            chains.append(self._complete_chain({
                "entry_method_id": entry["id"],
                "entry_method_name": entry.get("name"),
                "source": _evidence(
                    entry, int(entry.get("start_line", 1)),
                    str(entry.get("name") or "binder entry"), "capability_entry",
                ),
                "sink": sink,
                "path": [
                    {"method_id": method_id, "kind": "method", "text": method_id}
                    for method_id in method_path
                ] + [sink],
                "blocking_gaps": [effect["gap"]] if effect.get("gap") else [],
                "dataflow_status": "interprocedural" if len(method_path) > 1 else "intraprocedural",
                "final_reaching_state": "capability",
                "flow_kind": "capability_effect",
            }))
        if not chains:
            entry_operation = classify_operation_taxonomy({
                "method_name": entry.get("name"),
                "method_descriptor": entry.get("descriptor"),
                "receiver_type": entry.get("qualified_class"),
                "receiver_text": "this",
            })
            if entry_operation.get("is_effect") and entry_operation.get("verified"):
                sink = _evidence(
                    entry, int(entry.get("start_line", 1)),
                    str(entry.get("name") or "capability entry"), entry_operation["kind"],
                )
                sink.update({
                    "taxonomy": entry_operation["taxonomy"],
                    "effect_verified": True,
                    "method_name": entry.get("name"),
                    "receiver_type": entry.get("qualified_class"),
                })
                chains.append(self._complete_chain({
                    "entry_method_id": entry["id"],
                    "entry_method_name": entry.get("name"),
                    "source": _evidence(
                        entry, int(entry.get("start_line", 1)),
                        str(entry.get("name") or "binder entry"), "capability_entry",
                    ),
                    "sink": sink,
                    "path": [sink],
                    "blocking_gaps": [],
                    "dataflow_status": "interprocedural",
                    "final_reaching_state": "capability",
                    "flow_kind": "capability_effect",
                }))
        return sorted(chains, key=_chain_sort_key)

    def guard_segment(
        self,
        method_id: str,
        *,
        boundary_ordinal: int,
        start_line: int | None = None,
        end_line: int | None = None,
        start_ordinal: int | None = None,
        end_ordinal: int | None = None,
    ) -> dict[str, Any]:
        """只在给定 transaction segment 内判定 dispatch 前 Guard，禁止借用其他 case。"""

        method = self.methods_by_id.get(str(method_id))
        if method is None:
            return {
                "status": "unknown", "guards": [], "identity_sources": [],
                "blocking_gaps": [{"code": "GUARD_ENTRY_METHOD_UNRESOLVED", "critical": True}],
            }
        start = int(start_line if start_line is not None else method.get("start_line", 1))
        end = int(end_line if end_line is not None else method.get("end_line", start))
        calls = [
            call for call in method.get("call_sites", [])
            if start <= int(call.get("start_line", 0)) <= end
            and int(call.get("ordinal", 0)) < int(boundary_ordinal)
            and (start_ordinal is None or int(call.get("ordinal", 0)) >= int(start_ordinal))
            and (end_ordinal is None or int(call.get("ordinal", 0)) <= int(end_ordinal))
        ]
        lines = str(method.get("content") or "").splitlines()
        base = int(method.get("start_line", 1))
        sliced = "\n".join(lines[max(0, start - base):max(0, end - base + 1)])
        segment = {
            **method,
            "content": sliced,
            "start_line": start,
            "end_line": end,
            "call_sites": calls,
        }
        outcome = self._method_guard_outcome(
            segment,
            max((int(call.get("ordinal", 0)) for call in calls), default=0) + 1,
            set(),
        )
        identity_clear = next(
            (call for call in calls if call.get("method_name") == "clearCallingIdentity"), None
        )
        if identity_clear:
            outcome["blocking_gaps"] = _unique_gaps([*outcome.get("blocking_gaps", []), {
                "code": "CALLING_IDENTITY_CLEARED_BEFORE_EFFECT",
                "critical": True,
                "method": method_id,
                "ordinal": identity_clear.get("ordinal"),
            }])
            if outcome["status"] != "present_bypassable":
                outcome["status"] = "unknown"
        return outcome

    def guard_coverage(
        self,
        flow: dict[str, Any] | None = None,
        *,
        entry_method_id: str | None = None,
        sink: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按实际 entry→Sink 有序路径证明 fail-closed Guard 的局部支配。"""

        flow = flow or {}
        sink = sink or flow.get("sink")
        entry_method_id = entry_method_id or flow.get("entry_method_id")
        sink_method = self.methods_by_id.get(str((sink or {}).get("method_id") or ""))
        if sink_method is None and sink:
            candidates = [
                method for method in self.methods
                if method.get("name") == sink.get("method_name")
                and (not sink.get("path") or method.get("path") == sink.get("path"))
                and int(method.get("start_line", 1)) <= int(sink.get("line", method.get("end_line", 10**9))) <= int(method.get("end_line", 10**9))
            ]
            sink_method = candidates[0] if len(candidates) == 1 else None
        if sink_method is None:
            return {
                "status": "unknown", "guards": [], "identity_sources": [],
                "entry_method_id": entry_method_id, "sink": sink,
                "blocking_gaps": [{"code": "GUARD_SINK_METHOD_UNRESOLVED", "critical": True}],
            }
        entry_method_id = entry_method_id or sink_method["id"]
        entry = self.methods_by_id.get(str(entry_method_id))
        if entry is None:
            return {
                "status": "unknown", "guards": [], "identity_sources": [],
                "entry_method_id": entry_method_id, "sink": sink,
                "blocking_gaps": [{"code": "GUARD_ENTRY_METHOD_UNRESOLVED", "critical": True}],
            }
        chain = self._actual_method_chain(flow, entry, sink_method)
        if chain is None:
            return {
                "status": "unknown", "guards": [], "identity_sources": [],
                "entry_method_id": entry_method_id, "sink": sink,
                "blocking_gaps": [{"code": "GUARD_PATH_UNRESOLVED", "critical": True}],
            }
        guards: list[dict[str, Any]] = []
        identities: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        outcomes: list[str] = []
        for index, method in enumerate(chain):
            if index + 1 < len(chain):
                next_id = chain[index + 1]["id"]
                transitions = [
                    call for call in method.get("call_sites", [])
                    if call.get("resolved_target_id") == next_id
                ]
                if len(transitions) != 1:
                    gaps.append({"code": "GUARD_PATH_CALL_AMBIGUOUS", "critical": True, "method": method["id"]})
                    continue
                boundary = int(transitions[0].get("ordinal", 0))
            else:
                boundary = int((sink or {}).get("ordinal") or self._ordinal_at_line(method, int((sink or {}).get("line", method.get("end_line", 1)))))
            outcome = self._method_guard_outcome(method, boundary, set())
            outcomes.append(outcome["status"])
            guards.extend(outcome["guards"])
            identities.extend(outcome["identity_sources"])
            gaps.extend(outcome["blocking_gaps"])
        critical_flow_gap = any(
            not isinstance(gap, dict) or gap.get("critical", True)
            for gap in [*flow.get("coverage_gaps", []), *flow.get("blocking_gaps", [])]
        )
        if "present_effective" in outcomes and not gaps:
            status = "present_effective"
        elif "present_bypassable" in outcomes:
            status = "present_bypassable"
        elif "present_partial" in outcomes:
            status = "present_partial"
        elif gaps or critical_flow_gap or "unknown" in outcomes:
            status = "unknown"
        else:
            status = "absent"
        return {
            "status": status,
            "guards": guards,
            "identity_sources": identities,
            "entry_method_id": entry_method_id,
            "entry_method_name": entry.get("name"),
            "sink": sink,
            "method_chain": [method["id"] for method in chain],
            "blocking_gaps": _unique_gaps(gaps),
        }

    def guard_summary(self, method_names: set[str] | None = None) -> dict[str, Any]:
        """兼容接口；按每个入口整体保守聚合，不能替代 Sink 级 coverage。"""

        selected = [method for method in self.methods if not method_names or method.get("name") in method_names]
        coverages = []
        for method in selected:
            boundary = max((int(call.get("ordinal", 0)) for call in method.get("call_sites", [])), default=0) + 1
            outcome = self._method_guard_outcome(method, boundary, set())
            coverages.append({**outcome, "entry_method_id": method["id"]})
        if not coverages:
            return {"status": "absent", "guards": [], "identity_sources": [], "entries": []}
        statuses = {coverage["status"] for coverage in coverages}
        if statuses == {"present_effective"}:
            status = "present_effective"
        elif "present_effective" in statuses and "absent" in statuses:
            status = "present_bypassable"
        elif "unknown" in statuses:
            status = "unknown"
        elif "present_bypassable" in statuses:
            status = "present_bypassable"
        elif "present_partial" in statuses:
            status = "present_partial"
        else:
            status = "absent"
        return {
            "status": status,
            "guards": [guard for coverage in coverages for guard in coverage["guards"]],
            "identity_sources": [item for coverage in coverages for item in coverage["identity_sources"]],
            "entries": coverages,
        }

    def _actual_method_chain(
        self, flow: dict[str, Any], entry: dict[str, Any], sink_method: dict[str, Any]
    ) -> list[dict[str, Any]] | None:
        ids = [entry["id"]]
        for node in flow.get("path", []):
            method_id = node.get("method_id")
            if method_id and method_id in self.methods_by_id and method_id != ids[-1]:
                ids.append(method_id)
        if sink_method["id"] != ids[-1]:
            ids.append(sink_method["id"])
        chain = [self.methods_by_id[method_id] for method_id in ids]
        for left, right in zip(chain, chain[1:]):
            if sum(call.get("resolved_target_id") == right["id"] for call in left.get("call_sites", [])) != 1:
                return None
        return chain

    def _method_guard_outcome(
        self, method: dict[str, Any], boundary_ordinal: int, active: set[str]
    ) -> dict[str, Any]:
        """判定当前方法在给定 Sink/callee 边界前是否存在 fail-closed Guard。

        enforce 仅在未被捕获继续且不位于未证明条件分支时有效；check 必须有与返回值绑定的
        deny→return/throw。唯一 resolved wrapper 可递归继承结论，递归、未解析调用、条件支配
        不明或 clearCallingIdentity 均产生保守 gap，绝不以“出现了权限 API”代替支配证明。
        """

        if method["id"] in active:
            return {
                "status": "unknown", "guards": [], "identity_sources": [],
                "blocking_gaps": [{"code": "GUARD_WRAPPER_RECURSIVE", "critical": True, "method": method["id"]}],
            }
        active = {*active, method["id"]}
        guards: list[dict[str, Any]] = []
        identities: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        statuses: list[str] = []
        calls = sorted(method.get("call_sites", []), key=lambda call: int(call.get("ordinal", 0)))
        for call in calls:
            ordinal = int(call.get("ordinal", 0))
            if ordinal >= boundary_ordinal:
                continue
            name = str(call.get("method_name") or "")
            if name in IDENTITY_SOURCE_METHODS:
                identities.append(_call_evidence(method, call, "identity_source"))
                continue
            if name == "clearCallingIdentity":
                gaps.append({
                    "code": "CALLING_IDENTITY_CLEARED_BEFORE_EFFECT",
                    "critical": True,
                    "method": method["id"],
                    "ordinal": ordinal,
                })
                continue
            if name in ENFORCE_GUARD_METHODS:
                evidence = _guard_evidence(method, call, True)
                if self._guard_caught_and_continues(method, call, boundary_ordinal):
                    evidence["fail_closed"] = False
                    evidence["reason"] = "SecurityException_or_exception_caught_then_continues"
                    statuses.append("present_bypassable")
                elif self._call_is_conditional(method, call):
                    # 没有完整 CFG 时不能证明条件分支中的 enforce 支配所有到 Sink 的路径。
                    evidence["fail_closed"] = False
                    evidence["reason"] = "conditional_guard_dominance_unproven"
                    statuses.append("present_partial")
                    gaps.append({
                        "code": "GUARD_DOMINANCE_UNKNOWN", "critical": True,
                        "method": method["id"], "ordinal": ordinal,
                    })
                else:
                    statuses.append("present_effective")
                guards.append(evidence)
                continue
            if name in CHECK_GUARD_METHODS:
                effective, complex_branch = self._check_guard_fail_closed(method, call, boundary_ordinal)
                conditional = self._call_is_conditional(method, call)
                evidence = _guard_evidence(method, call, effective and not conditional)
                evidence["result_used"] = effective or complex_branch
                guards.append(evidence)
                statuses.append("present_effective" if effective and not conditional else "present_partial")
                if conditional:
                    evidence["reason"] = "conditional_guard_dominance_unproven"
                    gaps.append({
                        "code": "GUARD_DOMINANCE_UNKNOWN", "critical": True,
                        "method": method["id"], "ordinal": ordinal,
                    })
                elif complex_branch and not effective:
                    gaps.append({
                        "code": "GUARD_CHECK_BRANCH_UNPROVEN", "critical": True,
                        "method": method["id"], "ordinal": ordinal,
                    })
                continue
            target_id = str(call.get("resolved_target_id") or "")
            target = self.methods_by_id.get(target_id)
            if target:
                wrapped = self._method_guard_outcome(
                    target,
                    max((int(item.get("ordinal", 0)) for item in target.get("call_sites", [])), default=0) + 1,
                    active,
                )
                if wrapped["guards"]:
                    evidence = _call_evidence(method, call, "wrapped_guard")
                    evidence["resolved_target_id"] = target_id
                    evidence["fail_closed"] = wrapped["status"] == "present_effective"
                    evidence["callee_guards"] = wrapped["guards"]
                    guards.append(evidence)
                    identities.extend(wrapped["identity_sources"])
                    gaps.extend(wrapped["blocking_gaps"])
                    statuses.append(wrapped["status"])
            elif (
                call.get("resolve_status") in {"ambiguous", "unresolved"}
                and _is_guard_wrapper_candidate(call)
            ):
                gaps.append({
                    "code": "GUARD_CALL_TARGET_UNRESOLVED", "critical": True,
                    "method": method["id"], "ordinal": ordinal,
                })
        if "present_effective" in statuses and not gaps:
            status = "present_effective"
        elif "present_bypassable" in statuses:
            status = "present_bypassable"
        elif statuses:
            status = "present_partial"
        elif gaps:
            status = "unknown"
        else:
            status = "absent"
        return {"status": status, "guards": guards, "identity_sources": identities, "blocking_gaps": gaps}

    def _check_guard_fail_closed(
        self, method: dict[str, Any], call: dict[str, Any], boundary_ordinal: int
    ) -> tuple[bool, bool]:
        assigned = str(call.get("assigned_to") or "")
        ordinal = int(call.get("ordinal", 0))
        branches = [
            item for item in method.get("flow_ir", [])
            if item.get("op") == "branch_hint"
            and int(item.get("line", 0)) >= int(call.get("start_line", 0))
            and int(item.get("line", 0)) <= self._line_for_ordinal(method, boundary_ordinal)
        ]
        if assigned:
            matching = [item for item in branches if _contains_name(str(item.get("condition") or ""), assigned)]
            for branch in matching:
                condition = str(branch.get("condition") or "")
                denial = bool(re.search(
                    rf"(?:!\s*{re.escape(assigned)}\b|{re.escape(assigned)}\s*!=\s*(?:PackageManager\.)?(?:PERMISSION_GRANTED|0)|{re.escape(assigned)}\s*==\s*(?:PackageManager\.)?(?:PERMISSION_DENIED|-1)|(?:PERMISSION_GRANTED|0)\s*!=\s*{re.escape(assigned)})",
                    condition,
                ))
                # v2026-08-09（Cluster E 根因）：`String nameForUid = getNameForUid(uid);
                # if (nameForUid == null) { return false; }` 的调用者身份校验——
                # assigned 变量判空后 fail-closed，与 PERMISSION_DENIED 同权。
                if not denial:
                    denial = bool(re.search(
                        rf"{re.escape(assigned)}\s*==\s*null\b", condition,
                    ))
                if branch.get("fail_closed") and denial:
                    return True, False
            return False, bool(matching)
        content = str(method.get("content") or "")
        name = re.escape(str(call.get("method_name") or ""))
        inline = re.search(
            rf"if\s*\([^)]*{name}\s*\([^)]*\)[^)]*(?:!=\s*(?:PackageManager\.)?PERMISSION_GRANTED|==\s*(?:PackageManager\.)?PERMISSION_DENIED)[^)]*\)\s*(?:\{{\s*)?(?:return\b|throw\s+new\b)",
            content,
            re.S,
        )
        # v2026-08-09（Cluster E 根因）：getNameForUid 返回包名字符串，
        # denial 模式是 `!pkg.equals(expected)` / `pkg == null` → deny/return，
        # 或 `!expected.equals(pkg)` 的 fail-closed 形式。两者都直接证明调用者
        # 身份校验存在且失败即拒绝——与 PERMISSION_DENIED 同权。
        if not inline and name == r"getNameForUid":
            inline = re.search(
                r"(?:equals\s*\(\s*[\"'][^\"']+[\"']\s*\)|==\s*null|!=\s*null)"
                r"[^;{]*\b(?:return\s+false|return\s+null|throw\s+new\b)",
                content,
                re.S,
            )
        return bool(inline), bool(re.search(rf"if\s*\([^)]*{name}\s*\(", content))

    @staticmethod
    def _call_is_conditional(method: dict[str, Any], call: dict[str, Any]) -> bool:
        """保守识别条件 body 中的调用，包括无花括号的跨行语句。"""

        content = str(method.get("content") or "")
        relative_line = max(0, int(call.get("start_line", 1)) - int(method.get("start_line", 1)))
        lines = content.splitlines(keepends=True)
        line_offset = sum(len(line) for line in lines[:relative_line])
        method_name = str(call.get("method_name") or "")
        local_offset = lines[relative_line].find(method_name) if relative_line < len(lines) else -1
        call_offset = line_offset + max(0, local_offset)
        prefix = content[:call_offset]
        depth = prefix.count("{") - prefix.count("}")
        # 方法自身主体通常贡献一层；更深层意味着处于 if/loop/try 等局部块。
        if depth > 1:
            return True

        # 无花括号 body 可以与条件跨多行。只在调用位于右括号之后时判为
        # conditional；调用本身位于 ``if (check(...))`` 条件中不属于条件执行。
        controls = list(re.finditer(r"\b(?:if|when|while|for)\s*\(", prefix))
        for control in reversed(controls):
            opening = content.find("(", control.start(), call_offset)
            if opening < 0:
                continue
            closing = _matching_delimiter(content, opening, "(", ")")
            if closing is None or closing >= call_offset:
                continue
            body_start = closing + 1
            while body_start < call_offset and content[body_start].isspace():
                body_start += 1
            if body_start > call_offset:
                continue
            if body_start < call_offset and content[body_start] == "{":
                continue
            between = content[body_start:call_offset]
            if ";" not in between and not re.search(r"\belse\b", between):
                return True
            break

        # ``else`` 的无花括号 body 也只有条件可达。
        tail = prefix[max(prefix.rfind(";"), prefix.rfind("}"), prefix.rfind("{")) + 1:]
        return bool(re.search(r"\belse\s*$", tail))

    def _guard_caught_and_continues(
        self, method: dict[str, Any], call: dict[str, Any], boundary_ordinal: int
    ) -> bool:
        content = str(method.get("content") or "")
        guard_line = int(call.get("start_line", method.get("start_line", 1)))
        boundary_line = self._line_for_ordinal(method, boundary_ordinal)
        relative_guard = max(0, guard_line - int(method.get("start_line", 1)))
        relative_boundary = max(relative_guard, boundary_line - int(method.get("start_line", 1)))
        lines = content.splitlines()
        prefix = "\n".join(lines[:relative_boundary + 1])
        guard_offset = len("\n".join(lines[:relative_guard]))
        if relative_guard:
            guard_offset += 1
        if relative_guard < len(lines):
            name_offset = lines[relative_guard].find(str(call.get("method_name") or ""))
            guard_offset += max(0, name_offset)
        for match in re.finditer(r"try\s*\{(?P<body>[\s\S]*?)\}\s*catch\s*\([^)]*\)\s*\{(?P<catch>[\s\S]*?)\}", prefix):
            if match.start("body") <= guard_offset <= match.end("body"):
                return not bool(re.search(r"\b(?:return|throw)\b", match.group("catch")))
        return False

    @staticmethod
    def _ordinal_at_line(method: dict[str, Any], line: int) -> int:
        later = [
            int(call.get("ordinal", 0)) for call in method.get("call_sites", [])
            if int(call.get("start_line", 0)) >= line
        ]
        return min(later) if later else max((int(call.get("ordinal", 0)) for call in method.get("call_sites", [])), default=0) + 1

    @staticmethod
    def _line_for_ordinal(method: dict[str, Any], ordinal: int) -> int:
        call = next((item for item in method.get("call_sites", []) if int(item.get("ordinal", 0)) == ordinal), None)
        return int((call or {}).get("start_line", method.get("end_line", method.get("start_line", 1))))

    def _reachable_side_effects(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        queue = deque([(entry, [entry["id"]])])
        visited, results = set(), []
        while queue:
            method, path = queue.popleft()
            if method["id"] in visited:
                continue
            visited.add(method["id"])
            for call in method.get("call_sites", []):
                operation = classify_operation_taxonomy(call, str(method.get("name") or ""), str(method.get("qualified_class") or ""))
                if operation.get("is_effect"):
                    results.append({
                        "event": "external_intent", "effect_kind": operation["kind"],
                        "effect_taxonomy": operation["taxonomy"], "verified": operation["verified"],
                        "call": _call_evidence(method, call, "side_effect"), "method_path": path,
                        "gap": operation.get("gap"),
                    })
                target = self.methods_by_id.get(str(call.get("resolved_target_id") or ""))
                if target and len(path) < 8:
                    queue.append((target, [*path, target["id"]]))
        return results


def _normalize_operation_receiver_type(value: str) -> str:
    """规范化源码、索引和 JVM 形式的 receiver type，不读取变量名。"""

    normalized = str(value or "").strip().replace("/", ".")
    if normalized.startswith("L") and normalized.endswith(";"):
        normalized = normalized[1:-1]
    normalized = re.sub(r"<.*>", "", normalized).rstrip("?")
    while normalized.endswith("[]"):
        normalized = normalized[:-2]
    return normalized


def _receiver_family_matches(
    receiver_type: str,
    *,
    exact: frozenset[str] = frozenset(),
    prefixes: tuple[str, ...] = (),
    leaves: frozenset[str] = frozenset(),
) -> bool:
    """仅按精确 FQCN、受控包前缀或显式 leaf allowlist 匹配 family。"""

    normalized = _normalize_operation_receiver_type(receiver_type)
    if not normalized:
        return False
    leaf = normalized.rsplit(".", 1)[-1].rsplit("$", 1)[-1]
    leaf_match = "." not in normalized and "$" not in normalized and leaf in leaves
    return normalized in exact or leaf_match or any(
        normalized.startswith(prefix) for prefix in prefixes
    )


def _is_trusted_source_extraction(
    call: dict[str, Any], containing_class: str = ""
) -> bool:
    """仅受信 platform source family 的精确签名可凭空建立 source。"""

    name = str(call.get("method_name") or "")
    arity = _operation_descriptor_arity(str(call.get("method_descriptor") or ""))
    receiver_type = _normalize_operation_receiver_type(str(call.get("receiver_type") or ""))
    families = (
        (
            frozenset({"android.content.Intent"}), frozenset({"Intent"}),
            {
                "getStringExtra": {1}, "getIntExtra": {2}, "getLongExtra": {2},
                "getBooleanExtra": {2}, "getParcelableExtra": {1, 2},
                "getSerializableExtra": {1, 2}, "getExtras": {0}, "getData": {0},
                "getDataString": {0}, "getAction": {0},
            },
        ),
        (
            frozenset({"android.net.Uri", "java.net.URI"}), frozenset({"Uri", "URI"}),
            {"getQueryParameter": {1}, "getPath": {0}, "getPathSegments": {0}, "getLastPathSegment": {0}},
        ),
        (
            frozenset({"android.os.Parcel"}), frozenset({"Parcel"}),
            {"readString": {0}, "readInt": {0}, "readLong": {0}, "readBundle": {0, 1}, "readParcelable": {1, 2}},
        ),
        (
            frozenset({"android.os.Bundle", "android.os.PersistableBundle"}),
            frozenset({"Bundle", "PersistableBundle"}),
            {
                "getString": {1, 2}, "get": {1}, "getParcelable": {1, 2},
                "getSerializable": {1, 2},
            },
        ),
        (
            frozenset({"org.json.JSONObject"}), frozenset({"JSONObject"}),
            {"getString": {1}, "get": {1}, "optString": {1, 2}, "optInt": {1, 2}},
        ),
    )
    return arity is not None and any(
        arity in signatures.get(name, set())
        and _receiver_family_matches(receiver_type, exact=exact, leaves=leaves)
        for exact, leaves, signatures in families
    )


def _jvm_descriptor_arity(parameters: str) -> int | None:
    index = 0
    arity = 0
    primitives = frozenset("ZBCSIJFD")
    while index < len(parameters):
        while index < len(parameters) and parameters[index] == "[":
            index += 1
        if index >= len(parameters):
            return None
        marker = parameters[index]
        if marker in primitives:
            index += 1
        elif marker in {"L", "T"}:
            closing = parameters.find(";", index + 1)
            if closing < 0:
                return None
            index = closing + 1
        else:
            return None
        arity += 1
    return arity


def _operation_descriptor_arity(descriptor: str) -> int | None:
    """解析 ``(?,?)->?``/源码类型列表及原始 JVM method descriptor。"""

    value = str(descriptor or "").strip()
    if not value.startswith("(") or ")" not in value:
        return None
    closing = value.find(")")
    parameters = value[1:closing].strip()
    suffix = value[closing + 1:].strip()
    if suffix.startswith("->"):
        if not parameters:
            return 0
        parts = [item.strip() for item in parameters.split(",")]
        return len(parts) if all(parts) else None
    if not suffix or not re.fullmatch(
        r"(?:V|\[*[ZBCSIJFD]|\[*L[^;]+;|\[*T[^;]+;)", suffix
    ):
        return None
    return _jvm_descriptor_arity(parameters)


def _signature_checked_effect(
    call: dict[str, Any],
    allowed_arities: frozenset[int],
    taxonomy: str,
    kind: str,
) -> dict[str, Any]:
    """receiver family 已确认后，以 descriptor arity 决定 verified/candidate/reject。"""

    descriptor = str(call.get("method_descriptor") or "")
    arity = _operation_descriptor_arity(descriptor)
    if arity is not None and arity not in allowed_arities:
        return {"is_effect": False, "taxonomy": "unknown_effect", "kind": "not_sensitive", "verified": False}
    if arity is None:
        return {
            "is_effect": True,
            "taxonomy": taxonomy,
            "kind": kind,
            "verified": False,
            "gap": {
                "code": "OPERATION_SIGNATURE_GAP",
                "critical": True,
                "method_name": str(call.get("method_name") or ""),
                "receiver_type": str(call.get("receiver_type") or "") or None,
                "method_descriptor": descriptor or None,
                "allowed_arities": sorted(allowed_arities),
            },
        }
    return {"is_effect": True, "taxonomy": taxonomy, "kind": kind, "verified": True}


def _call_has_confirmed_gap_exemption(
    call: dict[str, Any], operation: dict[str, Any]
) -> bool:
    """仅确认真实 external API、validator、container、source 或 intrinsic transform。"""

    if operation.get("verified") and (
        operation.get("is_effect")
        or operation.get("kind") in {"container_slot_mutation", "container_mutation"}
    ):
        return True
    method_name = str(call.get("method_name") or "")
    if _is_validator(method_name):
        return True
    arity = _operation_descriptor_arity(str(call.get("method_descriptor") or ""))
    if arity is None:
        return False
    receiver_type = _normalize_operation_receiver_type(str(call.get("receiver_type") or ""))

    def receiver(
        *,
        exact: frozenset[str] = frozenset(),
        prefixes: tuple[str, ...] = (),
        leaves: frozenset[str] = frozenset(),
    ) -> bool:
        return _receiver_family_matches(
            receiver_type, exact=exact, prefixes=prefixes, leaves=leaves
        )

    intent_sources = {
        "getStringExtra": {1}, "getIntExtra": {2}, "getLongExtra": {2},
        "getBooleanExtra": {2}, "getParcelableExtra": {1, 2},
        "getSerializableExtra": {1, 2}, "getExtras": {0}, "getData": {0},
        "getDataString": {0}, "getAction": {0},
    }
    if receiver(
        exact=frozenset({"android.content.Intent"}), leaves=frozenset({"Intent"})
    ) and arity in intent_sources.get(method_name, set()):
        return True

    uri_sources = {
        "getQueryParameter": {1}, "getPath": {0}, "getPathSegments": {0},
        "getLastPathSegment": {0},
    }
    if receiver(
        exact=frozenset({"android.net.Uri", "java.net.URI"}),
        leaves=frozenset({"Uri", "URI"}),
    ) and arity in uri_sources.get(method_name, set()):
        return True

    parcel_sources = {
        "readString": {0}, "readInt": {0}, "readLong": {0},
        "readBundle": {0, 1}, "readParcelable": {1, 2},
    }
    if receiver(
        exact=frozenset({"android.os.Parcel"}), leaves=frozenset({"Parcel"})
    ) and arity in parcel_sources.get(method_name, set()):
        return True

    container_reads = {
        "getString": {1, 2}, "get": {1}, "optString": {1, 2}, "optInt": {1, 2},
        "getParcelable": {1, 2}, "getSerializable": {1, 2},
    }
    if receiver(
        exact=frozenset({
            "android.os.Bundle", "android.os.PersistableBundle", "org.json.JSONObject",
            "org.json.JSONArray", "java.util.Map", "Map",
        }),
        leaves=frozenset({"Bundle", "PersistableBundle", "JSONObject", "JSONArray"}),
    ) and arity in container_reads.get(method_name, set()):
        return True

    if method_name == "toString" and arity == 0:
        return True
    string_transforms = {
        "trim": {0}, "substring": {1, 2}, "concat": {1}, "format": {1, 2, 3, 4},
        "valueOf": {1},
    }
    if receiver(
        exact=frozenset({"java.lang.String", "String"}), leaves=frozenset({"String"})
    ) and arity in string_transforms.get(method_name, set()):
        return True
    if receiver(
        exact=frozenset({"java.lang.StringBuilder", "java.lang.StringBuffer"}),
        leaves=frozenset({"StringBuilder", "StringBuffer"}),
    ) and method_name == "append" and arity in {1, 3}:
        return True
    return False


def classify_operation_taxonomy(
    call: dict[str, Any],
    containing_method_name: str = "",
    containing_class: str = "",
) -> dict[str, Any]:
    """用 resolved target、receiver family 与签名统一分类外部 operation。"""

    method_name = str(call.get("method_name") or "")
    receiver_type = _normalize_operation_receiver_type(str(call.get("receiver_type") or ""))
    receiver_text = str(call.get("receiver_text") or "").strip()
    resolved_target = str(call.get("resolved_target_id") or call.get("resolved_target") or "")
    implicit_receiver = receiver_text in {"", "this", "super"}
    def family(
        *,
        exact: frozenset[str] = frozenset(),
        prefixes: tuple[str, ...] = (),
        leaves: frozenset[str] = frozenset(),
    ) -> bool:
        return _receiver_family_matches(
            receiver_type, exact=exact, prefixes=prefixes, leaves=leaves
        )

    def checked(
        signatures: dict[str, frozenset[int]], taxonomy: str, kind: str
    ) -> dict[str, Any] | None:
        arities = signatures.get(method_name)
        return _signature_checked_effect(call, arities, taxonomy, kind) if arities else None

    def same_package_leaf(leaves: frozenset[str]) -> bool:
        package = containing_class.rsplit(".", 1)[0] if "." in containing_class else ""
        leaf = receiver_type.rsplit(".", 1)[-1]
        return bool(package and receiver_type == f"{package}.{leaf}" and leaf in leaves)

    # 应用内 resolved wrapper 必须进入真实 callee；不得先凭任何 API family 闭链。
    if resolved_target:
        return {"is_effect": False, "taxonomy": "unknown_effect", "kind": "resolved_wrapper", "verified": False}

    provider_crud = {"query", "insert", "update", "delete", "openFile", "call", "applyBatch"}
    if method_name in provider_crud and containing_method_name == method_name and implicit_receiver:
        return {"is_effect": False, "taxonomy": "unknown_effect", "kind": "provider_crud_entry", "verified": False}

    container_family = family(
        exact=frozenset({
            "android.content.Intent", "android.os.Bundle", "android.os.PersistableBundle",
        }),
        leaves=frozenset({"Intent", "Bundle", "PersistableBundle"}),
    )
    if method_name in SLOT_PUT_METHODS | SLOT_MERGE_METHODS | {"replaceExtras"} and container_family:
        return {"is_effect": False, "taxonomy": "unknown_effect", "kind": "container_slot_mutation", "verified": True}

    arguments = [str(value) for value in call.get("arguments", [])]
    fragment_factory_shape = bool(
        "fragmentfactory" in containing_method_name.lower()
        and _operation_descriptor_arity(str(call.get("method_descriptor") or "")) == 2
        and arguments
        and re.search(r"(?:class|fragment)[_$a-z0-9]*name", arguments[-1], re.I)
    )
    fragment_factory_owner = family(
        exact=frozenset({"androidx.fragment.app.FragmentFactory"}),
        leaves=frozenset({"FragmentFactory"}),
    )
    if fragment_factory_shape and fragment_factory_owner:
        return _signature_checked_effect(
            call, frozenset({2}), "ui_navigation", "fragment_reflection"
        )
    if fragment_factory_shape:
        return {
            "is_effect": True,
            "taxonomy": "ui_navigation",
            "kind": "fragment_reflection",
            "verified": False,
            "gap": {
                "code": "FRAGMENT_FACTORY_PROVENANCE_GAP",
                "critical": True,
                "method_name": method_name,
                "receiver_type": receiver_type or None,
            },
        }

    class_family = family(exact=frozenset({"java.lang.Class", "Class"}))
    if method_name == "forName" and class_family:
        return _signature_checked_effect(
            call, frozenset({1, 3}), "ui_navigation", "fragment_reflection"
        )
    if method_name in {"getDeclaredConstructor", "getConstructor", "newInstance"} and class_family:
        result = checked({
            "getDeclaredConstructor": frozenset({0, 1, 2, 3, 4}),
            "getConstructor": frozenset({0, 1, 2, 3, 4}),
            "newInstance": frozenset({0}),
        }, "ui_navigation", "fragment_reflection")
        if result:
            return result
    if family(
        exact=frozenset({"java.lang.reflect.Constructor"}),
        leaves=frozenset({"Constructor"}),
    ) and method_name == "newInstance":
        return _signature_checked_effect(
            call, frozenset({0, 1, 2, 3, 4}), "ui_navigation", "fragment_reflection"
        )
    if family(
        exact=frozenset({"androidx.fragment.app.FragmentFactory"}),
        leaves=frozenset({"FragmentFactory"}),
    ) and method_name == "instantiate":
        return _signature_checked_effect(
            call, frozenset({2}), "ui_navigation", "fragment_reflection"
        )
    if family(
        exact=frozenset({"android.app.Fragment", "androidx.fragment.app.Fragment"}),
        leaves=frozenset({"Fragment"}),
    ) and method_name == "instantiate":
        return _signature_checked_effect(
            call, frozenset({2, 3}), "ui_navigation", "fragment_reflection"
        )

    context_family = family(
        exact=frozenset({
            "android.content.Context", "android.content.ContextWrapper",
            "android.app.Activity", "android.app.Service",
        }),
        leaves=frozenset({"Context", "ContextWrapper", "Activity", "Service"}),
    )
    if context_family:
        result = checked({
            "startActivity": frozenset({1, 2}), "startActivities": frozenset({1, 2}),
        }, "ui_navigation", "component_launch")
        if result:
            return result
        result = checked({
            "bindService": frozenset({3, 4, 5}), "startService": frozenset({1}),
            "startForegroundService": frozenset({1}), "stopSelf": frozenset({0, 1}),
            "startForeground": frozenset({2, 3}),
        }, "connection_session_control", "connection_session_control")
        if result:
            return result
        result = checked({
            "sendBroadcast": frozenset({1, 2}),
            "sendOrderedBroadcast": frozenset({2, 3, 7, 8}),
        }, "callback_event_injection", "broadcast")
        if result:
            return result

    if family(
        exact=frozenset({
            "androidx.navigation.NavController", "android.app.FragmentTransaction",
            "android.app.FragmentManager", "androidx.fragment.app.FragmentTransaction",
            "androidx.fragment.app.FragmentManager",
        }),
        prefixes=("androidx.navigation.",),
        leaves=frozenset({"NavController", "FragmentTransaction", "FragmentManager"}),
    ):
        result = checked({
            "navigate": frozenset({1, 2, 3, 4}), "replace": frozenset({2, 3, 4}),
            "add": frozenset({1, 2, 3, 4}), "show": frozenset({1, 2}),
        }, "ui_navigation", "ui_navigation")
        if result:
            return result

    if family(
        exact=frozenset({
            "android.location.LocationManager",
            "com.google.android.gms.location.FusedLocationProviderClient",
        }),
        prefixes=("com.google.android.gms.location.",),
        leaves=frozenset({"LocationManager", "FusedLocationProviderClient", "LocationClient"}),
    ):
        result = checked({
            "requestLocationUpdates": frozenset({1, 3, 4, 5, 6, 7}),
            "getLastLocation": frozenset({0, 1}), "getCurrentLocation": frozenset({2, 3}),
            # 评审 2026-08-27 E2：LocationManager 的真实 API 是 getLastKnownLocation(String)，
            # getLastLocation 是 FusedLocationProviderClient 的 API（此前张冠李戴）。
            "getLastKnownLocation": frozenset({1}),
        }, "location_sensor_collection", "location")
        if result:
            return result

    sensor_leaves = frozenset({"SensorManager", "SensorService", "SensorClient"})
    if family(
        exact=frozenset({"android.hardware.SensorManager"}),
        prefixes=("android.hardware.",),
        leaves=sensor_leaves,
    ) or same_package_leaf(sensor_leaves):
        result = checked({
            "registerListener": frozenset({3, 4}), "startGymSensor": frozenset({0, 1, 2}),
            "startStepSensor": frozenset({0, 1, 2}), "startAccSensor": frozenset({0, 1, 2}),
            "restartAccSensor": frozenset({0, 1, 2}), "pauseOrStopSensor": frozenset({0, 1}),
        }, "location_sensor_collection", "sensor")
        if result:
            return result

    sport_leaves = frozenset({
        "SportManager", "WorkoutManager", "FitnessManager", "SportService",
        "SportApiStub", "WorkoutApiStub", "FitnessApiStub",
        # v2026-08-16（S1/S3）：小米运动导出接口（SportXms 等）的运动控制/数据面。
        "ISportRemoteState", "ISportRemoteData", "SportRemoteState", "SportRemoteData",
    })
    sport_family = (
        family(leaves=sport_leaves)
        or same_package_leaf(sport_leaves)
        or family(prefixes=("com.xiaomi.fitness.sport_manager_export.",))
    )
    if sport_family:
        result = checked({"startSport": frozenset({0, 1, 2, 3})}, "location_sensor_collection", "sport_state")
        if result:
            return result
        result = checked({
            "pauseSport": frozenset({0, 1, 2}), "resumeSport": frozenset({0, 1, 2}),
            "finishSport": frozenset({0, 1, 2, 3}),
        }, "connection_session_control", "sport_state")
        if result:
            return result

    connection_family = family(
        exact=frozenset({
            "android.bluetooth.BluetoothGatt", "android.bluetooth.BluetoothSocket",
            "android.net.wifi.WifiManager", "android.net.ConnectivityManager",
            "java.net.Socket", "android.hardware.usb.UsbDeviceConnection",
        }),
        leaves=frozenset({
            "BluetoothGatt", "BluetoothSocket", "WifiManager", "ConnectivityManager",
            "Socket", "UsbDeviceConnection",
        }),
    )
    if connection_family:
        result = checked({
            "connect": frozenset({0, 1}), "disconnect": frozenset({0}),
            "startScan": frozenset({0, 1, 2}), "stopScan": frozenset({0, 1}),
        }, "connection_session_control", "connection_session_control")
        if result:
            return result
    if family(
        exact=frozenset({"java.net.URL", "java.net.URLConnection", "java.net.HttpURLConnection"}),
        leaves=frozenset({"URL", "URLConnection", "HttpURLConnection"}),
    ):
        result = checked({"openConnection": frozenset({0, 1}), "connect": frozenset({0})}, "connection_session_control", "connection_session_control")
        if result:
            return result

    if family(
        exact=frozenset({"android.bluetooth.BluetoothGatt"}),
        leaves=frozenset({"BluetoothGatt"}),
    ):
        result = checked({
            "writeCharacteristic": frozenset({1, 2}), "writeDescriptor": frozenset({1, 2}),
        }, "device_protocol_output", "device_protocol_output")
        if result:
            return result
    if family(
        exact=frozenset({"android.hardware.usb.UsbDeviceConnection"}),
        leaves=frozenset({"UsbDeviceConnection"}),
    ):
        result = checked({
            "bulkTransfer": frozenset({4, 5, 6}), "controlTransfer": frozenset({7, 8}),
        }, "device_protocol_output", "device_protocol_output")
        if result:
            return result
    if family(
        prefixes=("android.nfc.tech.",),
        leaves=frozenset({"IsoDep", "NfcA", "NfcB", "NfcF", "NfcV", "TagTechnology"}),
    ) and method_name == "transceive":
        return _signature_checked_effect(
            call, frozenset({1}), "device_protocol_output", "device_protocol_output"
        )
    if family(
        leaves=frozenset({"BluetoothOutputStream", "UsbOutputStream", "NfcOutputStream", "ProtocolWriter"})
    ) and method_name == "write":
        return _signature_checked_effect(
            call, frozenset({1, 3}), "device_protocol_output", "device_protocol_output"
        )

    if family(
        exact=frozenset({
            "androidx.lifecycle.LiveData", "androidx.lifecycle.MutableLiveData",
            "java.util.Observable", "org.greenrobot.eventbus.EventBus",
        }),
        prefixes=("androidx.lifecycle.", "kotlinx.coroutines.flow.", "io.reactivex.", "org.greenrobot.eventbus."),
        leaves=frozenset({"LiveData", "MutableLiveData", "EventBus"}),
    ):
        result = checked({
            "postValue": frozenset({1}), "setValue": frozenset({1}),
            "onChanged": frozenset({1}), "dispatch": frozenset({1}), "emit": frozenset({1}),
        }, "callback_event_injection", "callback_event_injection")
        if result:
            return result
    if family(
        exact=frozenset({"android.app.NotificationManager"}),
        leaves=frozenset({"NotificationManager"}),
    ) and method_name == "notify":
        return _signature_checked_effect(
            call, frozenset({2, 3}), "callback_event_injection", "callback_event_injection"
        )

    shared_preferences_family = family(
        exact=frozenset({"android.content.SharedPreferences.Editor", "SharedPreferences.Editor"})
    )
    if shared_preferences_family:
        result = checked({
            "put": frozenset({2}), "putString": frozenset({2}), "putInt": frozenset({2}),
            "putLong": frozenset({2}), "putBoolean": frozenset({2}), "remove": frozenset({1}),
            "clear": frozenset({0}), "apply": frozenset({0}), "commit": frozenset({0}),
        }, "persistent_state_write", "persistent_state_write")
        if result:
            return result
    if family(
        prefixes=("androidx.datastore.",), leaves=frozenset({"DataStore"})
    ) and method_name == "updateData":
        return _signature_checked_effect(
            call, frozenset({1}), "persistent_state_write", "persistent_state_write"
        )
    if family(
        exact=frozenset({"android.provider.Settings.Secure", "android.provider.Settings.Global"}),
        leaves=frozenset({"Secure", "Global"}),
    ):
        result = checked({
            "putString": frozenset({3}), "putInt": frozenset({3}),
            "putLong": frozenset({3}), "putBoolean": frozenset({3}),
        }, "persistent_state_write", "persistent_state_write")
        if result:
            return result

    content_resolver_family = family(
        exact=frozenset({"android.content.ContentResolver"}),
        leaves=frozenset({"ContentResolver"}),
    )
    if content_resolver_family:
        result = checked({
            "insert": frozenset({2, 3}), "update": frozenset({3, 4}),
            "delete": frozenset({2, 3}), "applyBatch": frozenset({2}),
        }, "database_mutation", "content_mutation")
        if result:
            return result
        result = checked({"query": frozenset({4, 5, 6})}, "data_disclosure", "content_query")
        if result:
            return result

    database_family = family(
        exact=frozenset({
            "android.database.sqlite.SQLiteDatabase", "androidx.sqlite.db.SupportSQLiteDatabase",
        }),
        prefixes=("android.database.sqlite.", "androidx.sqlite.db."),
        leaves=frozenset({"SQLiteDatabase", "SupportSQLiteDatabase"}),
    )
    if database_family:
        result = checked({
            "insert": frozenset({3}), "update": frozenset({4, 5}), "delete": frozenset({3, 4}),
            "execSQL": frozenset({1, 2}), "applyBatch": frozenset({1, 2}),
            "compileStatement": frozenset({1}),
        }, "database_mutation", "database_mutation")
        if result:
            return result
        result = checked({
            "query": frozenset({7, 8, 9}), "rawQuery": frozenset({2, 3}),
        }, "data_disclosure", "database_query")
        if result:
            return result

    if family(
        exact=frozenset({"android.os.ParcelFileDescriptor"}),
        leaves=frozenset({"ParcelFileDescriptor"}),
    ) and method_name == "open":
        signature = _signature_checked_effect(call, frozenset({2}), "file_mutation", "file_open")
        if signature.get("verified"):
            arguments = " ".join(str(value) for value in call.get("arguments", [])).lower()
            read_only = any(token in arguments for token in ("mode_read_only", '"r"', "'r'")) and not any(
                token in arguments for token in ("mode_write", "mode_read_write", '"w', "'w", '"rw', "'rw")
            )
            if read_only:
                return {"is_effect": True, "taxonomy": "data_disclosure", "kind": "file_read", "verified": True}
        return signature
    if str(call.get("expression_kind") or "") == "constructor" and family(
        exact=frozenset({"java.io.FileInputStream", "java.io.RandomAccessFile"}),
        leaves=frozenset({"FileInputStream", "RandomAccessFile"}),
    ) and method_name in {"FileInputStream", "RandomAccessFile"}:
        return _signature_checked_effect(call, frozenset({1, 2}), "data_disclosure", "file_read")
    if str(call.get("expression_kind") or "") == "constructor" and family(
        exact=frozenset({"android.database.MatrixCursor"}), leaves=frozenset({"MatrixCursor"}),
    ) and method_name == "MatrixCursor":
        return _signature_checked_effect(call, frozenset({1, 2}), "data_disclosure", "cursor_result")

    file_family = family(
        exact=frozenset({
            "java.io.File", "java.io.FileOutputStream", "java.io.FileWriter",
            "java.io.RandomAccessFile", "java.io.BufferedWriter", "java.nio.file.Files",
            "java.nio.channels.FileChannel",
        }),
        prefixes=("java.nio.file.",),
        leaves=frozenset({
            "File", "FileOutputStream", "FileWriter", "RandomAccessFile",
            "BufferedWriter", "Files", "FileChannel",
        }),
    )
    if file_family:
        result = checked({
            "delete": frozenset({0, 1}), "write": frozenset({1, 2, 3}),
            "append": frozenset({1, 3}), "truncate": frozenset({1}),
            "renameTo": frozenset({1}), "mkdir": frozenset({0}), "mkdirs": frozenset({0}),
            "createNewFile": frozenset({0}), "open": frozenset({1, 2, 3}),
        }, "file_mutation", "file_delete" if method_name == "delete" else "file_mutation")
        if result:
            return result

    if family(
        exact=frozenset({"android.webkit.WebView"}), leaves=frozenset({"WebView"})
    ):
        result = checked({
            "loadUrl": frozenset({1, 2}), "evaluateJavascript": frozenset({2}),
            "addJavascriptInterface": frozenset({2}),
        }, "data_disclosure", "webview")
        if result:
            return result

    if family(
        exact=frozenset({
            "okhttp3.Call", "retrofit2.Call", "java.net.http.HttpClient",
            "org.apache.http.client.HttpClient", "okhttp3.RequestBody",
        }),
        prefixes=("okhttp3.", "retrofit2.", "org.apache.http."),
    ):
        result = checked({
            "send": frozenset({1, 2}), "enqueue": frozenset({1}),
            "execute": frozenset({0, 1, 2, 3}), "write": frozenset({1}),
        }, "data_disclosure", "data_disclosure")
        if result:
            return result

    if family(exact=frozenset({"java.util.Map", "Map"})) and method_name == "put":
        if _operation_descriptor_arity(str(call.get("method_descriptor") or "")) == 2:
            return {"is_effect": False, "taxonomy": "unknown_effect", "kind": "container_mutation", "verified": True}
        return {"is_effect": False, "taxonomy": "unknown_effect", "kind": "not_sensitive", "verified": False}
    return {"is_effect": False, "taxonomy": "unknown_effect", "kind": "not_sensitive", "verified": False}


def classify_call_operation(
    call: dict[str, Any], containing_method_name: str = "", containing_class: str = ""
) -> dict[str, Any]:
    """兼容 Sink 判定接口，并附带统一 operation taxonomy。"""

    operation = classify_operation_taxonomy(call, containing_method_name, containing_class)
    return {**operation, "is_sink": operation["is_effect"]}


def _matching_delimiter(text: str, opening: int, left: str, right: str) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
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
        elif char == left:
            depth += 1
        elif char == right:
            depth -= 1
            if depth == 0:
                return index
    return None


def _matching_brace(text: str, opening: int) -> int | None:
    return _matching_delimiter(text, opening, "{", "}")


def _method_parameter_names(method: dict[str, Any]) -> list[str]:
    structured = method.get("structured_parameters")
    if isinstance(structured, list) and structured:
        return [str(item["name"]) for item in structured if isinstance(item, dict) and item.get("name")]
    return _parameter_names(str(method.get("parameters") or method.get("signature") or ""))


def _stable_evidence_identity(evidence: dict[str, Any]) -> dict[str, Any]:
    """只保留可由源码/索引稳定重建的 evidence 与 callsite 身份。"""

    keys = (
        "path",
        "line",
        "text",
        "kind",
        "method_name",
        "method_id",
        "ordinal",
        "resolved_target_id",
        "resolve_status",
        "evidence_id",
        "source_kind",
        "source_basis",
        "parameter_position",
        "parameter_type",
        "taxonomy",
        "receiver_type",
        "effect_verified",
        "sensitive_result",
        "sensitive_data_evidence",
    )
    return {key: evidence.get(key) for key in keys if evidence.get(key) is not None}


def _chain_sort_key(chain: dict[str, Any]) -> tuple[Any, ...]:
    source = chain.get("source") or {}
    sink = chain.get("sink") or {}
    return (
        str(chain.get("entry_method_id") or ""),
        str(source.get("method_id") or ""),
        int(source.get("ordinal") or 0),
        str(sink.get("method_id") or ""),
        int(sink.get("ordinal") or 0),
        str(sink.get("taxonomy") or ""),
        str(chain.get("flow_kind") or ""),
        str(chain.get("chain_id") or ""),
    )


def _parameter_names(value: str) -> list[str]:
    inside = value[value.find("(") + 1:value.rfind(")")] if "(" in value and ")" in value else value
    results = []
    for raw in inside.split(","):
        clean = re.sub(r"@[\w.]+(?:\([^)]*\))?", "", raw).strip()
        tokens = re.findall(r"[A-Za-z_$][\w$]*", clean)
        if tokens:
            results.append(tokens[-1])
    return results


def _contains_name(expression: str, name: str) -> bool:
    return bool(re.search(rf"\b{re.escape(name)}\b", expression))


def _simple_name(expression: str) -> str | None:
    value = expression.strip()
    return value if re.fullmatch(r"[A-Za-z_$][\w$]*", value) else None


def _literal_key(expression: str) -> str | None:
    match = re.fullmatch(r"\s*[\"']([^\"']*)[\"']\s*", expression or "")
    return match.group(1) if match else None


def _is_validator(name: str) -> bool:
    normalized = re.sub(r"[^A-Za-z]", "", name).lower()
    return normalized in VALIDATOR_METHODS


def _evidence(method: dict[str, Any], line: int, text: str, kind: str) -> dict[str, Any]:
    return {
        "path": method["path"], "line": line, "text": text, "kind": kind,
        "method_name": method.get("name"), "method_id": method.get("id"),
        "status": "fact", "evidence_id": f"{method['path']}:{line}",
    }


def _call_evidence(method: dict[str, Any], call: dict[str, Any], kind: str) -> dict[str, Any]:
    receiver = f"{call.get('receiver_text')}." if call.get("receiver_text") else ""
    return {
        **_evidence(
            method, int(call.get("start_line", method.get("start_line", 1))),
            f"{receiver}{call.get('method_name')}(...)", kind,
        ),
        "ordinal": call.get("ordinal"),
        "resolved_target_id": call.get("resolved_target_id"),
        "resolve_status": call.get("resolve_status"),
    }


def _guard_evidence(method: dict[str, Any], call: dict[str, Any], fail_closed: bool) -> dict[str, Any]:
    return {**_call_evidence(method, call, "guard"), "fail_closed": fail_closed}


def _fail_closed(content: str) -> bool:
    return bool(re.search(r"(?:if\s*\([^)]*\)\s*)?(?:throw\s+new\s+SecurityException|return\s+(?:false|null|0)?\s*;)", content, re.S))


def _receiver_class(content: str, expression: str, package: str = "") -> str | None:
    direct = re.search(r"new\s+([A-Za-z_$][\w$.]*)", expression)
    if direct and direct.group(1).rsplit(".", 1)[-1] != "BroadcastReceiver":
        return direct.group(1)
    variable = expression.strip().removeprefix("this.")
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", variable):
        return None
    constructed = re.search(
        rf"\b(?:[A-Za-z_$][\w$<>.]*\s+)?(?:this\.)?{re.escape(variable)}\s*=\s*new\s+([A-Za-z_$][\w$.]*)",
        content,
    )
    if constructed:
        return constructed.group(1)
    declaration = re.search(
        rf"\b([A-Za-z_$][\w$.]*)\s+(?:this\.)?{re.escape(variable)}\s*(?:[;=])", content
    )
    if not declaration:
        return None
    type_name = declaration.group(1)
    if "." not in type_name and package and type_name != "BroadcastReceiver":
        return f"{package}.{type_name}"
    return type_name


def _receiver_actions_for_filter(content: str, filter_expression: str) -> list[str]:
    expression = filter_expression.strip()
    direct = re.findall(r"IntentFilter\s*\(\s*[\"']([^\"']+)[\"']", expression)
    variable = _simple_name(expression)
    if not variable:
        return sorted(set(direct))
    escaped = re.escape(variable)
    actions = re.findall(
        rf"\b{escaped}\s*=\s*new\s+IntentFilter\s*\(\s*[\"']([^\"']+)[\"']", content
    )
    actions.extend(re.findall(
        rf"\b{escaped}\s*\.\s*addAction\s*\(\s*[\"']([^\"']+)[\"']", content
    ))
    return sorted(set(actions))


def _unique_gaps(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, result = set(), []
    for gap in gaps:
        marker = (gap.get("code"), gap.get("method"), gap.get("caller"), gap.get("ordinal"))
        if marker not in seen:
            seen.add(marker)
            result.append(gap)
    return result


def _instruction_line(instruction: dict[str, Any], method: dict[str, Any]) -> int:
    """IR 指令的源码行，缺失时回退到方法起始行（保守：不触发作用域弹栈）。"""

    raw = instruction.get("line")
    if raw is None:
        return int(method.get("start_line", 1) or 1)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(method.get("start_line", 1) or 1)
