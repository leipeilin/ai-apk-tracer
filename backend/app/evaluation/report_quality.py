"""M4-T4.3 报告质量检查：AI/确定性分离、引用回查、PoC 骨架一致性。

消费 M3 的 ReportDocument（dict 形态）——输出结构化结果
（verdict PASS/WARN/FAIL + 逐项 violations），供 T4.4 优化门槛与
人工复核消费。设计：docs/analysis/milestones/2026-08-23-m4-t4-3-implementation-plan.md
（含评审 R-1~R-7 修订）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.reporting.poc import FINDING_COMPONENT_KINDS

_LEGAL_PROVENANCE = {"ai_report_protocol", "projected_from_l2_review"}
_LEGAL_POC_KINDS = {"intent", "uri", "binder_transaction", "broadcast", "provider_query"}
# 组件域与 poc.py 共享单一常量（审查 R-3——曾缺 webview/crypto/manifest
# 致真实 finding 报告误 FAIL）
_LEGAL_COMPONENT_KINDS = FINDING_COMPONENT_KINDS
_NOTE_KEYWORDS = ("占位符", "授权", "<PACKAGE>")


def check_report_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """三项质量检查 + verdict 聚合（FAIL > WARN > PASS）。

    容错：缺键 → 对应 violation（不抛——评审 A-5）。
    """

    separation = _check_separation(document)
    references = _check_references(document)
    poc = _check_poc(document)
    verdicts = [item["verdict"] for item in (separation, references, poc)]
    verdict = "FAIL" if "FAIL" in verdicts else ("WARN" if "WARN" in verdicts else "PASS")
    return {
        "verdict": verdict,
        "checks": {
            "ai_deterministic_separation": separation,
            "evidence_reference_integrity": references,
            "poc_skeleton_consistency": poc,
        },
    }


def _check_separation(document: Mapping[str, Any]) -> dict[str, Any]:
    """检查 1：AI/确定性分离（provenance 合法性为核心；键集交叉为防
    未来实现漂移的回归锚点——评审 R-4）。"""

    violations: list[str] = []
    ai_draft = document.get("ai_draft")
    deterministic = document.get("deterministic")
    if not isinstance(ai_draft, Mapping):
        violations.append("ai_draft 缺失或非对象")
        ai_draft = {}
    if not isinstance(deterministic, Mapping):
        violations.append("deterministic 缺失或非对象")
        deterministic = {}
    provenance = ai_draft.get("provenance")
    if provenance not in _LEGAL_PROVENANCE:
        violations.append(f"provenance 非法: {provenance!r}")
    crossed = sorted(set(ai_draft) & set(deterministic))
    if crossed:  # 回归锚点：当前合法产物恒空集
        violations.append(f"ai_draft 与 deterministic 键集交叉: {crossed}")
    return {"verdict": "FAIL" if violations else "PASS", "violations": violations}


def _check_references(document: Mapping[str, Any]) -> dict[str, Any]:
    """检查 2：引用回查——deterministic.sources/sinks 每条 path 非空、
    line 为 None 或 int>=1（口径与真实产物对齐：locations line 可 null）。"""

    violations: list[str] = []
    deterministic = document.get("deterministic")
    if not isinstance(deterministic, Mapping):
        return {"verdict": "WARN", "violations": ["deterministic 缺失——无法回查引用"]}
    for bucket in ("sources", "sinks", "locations"):  # M3/M4 审查 4.5：与投影三桶对齐
        items = deterministic.get(bucket)
        if items is None:
            continue  # 可选字段
        if not isinstance(items, list):
            violations.append(f"{bucket} 非列表")
            continue
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                violations.append(f"{bucket}[{index}] 非对象")
                continue
            path = item.get("path")
            if not isinstance(path, str) or not path:
                violations.append(f"{bucket}[{index}].path 空")
            line = item.get("line")
            if line is not None and (not isinstance(line, int) or line < 1):
                violations.append(f"{bucket}[{index}].line 非法: {line!r}")
    return {"verdict": "WARN" if violations else "PASS", "violations": violations}


def _check_poc(document: Mapping[str, Any]) -> dict[str, Any]:
    """检查 3：PoC 骨架一致性。

    分级（T4.3 原方案语义——2026-08-26 审查 R-7 修正，实现当初偷懒全 FAIL）：
    - FAIL 级：executable_files_created 非空（硬红线）、kind/component 枚举非法；
    - WARN 级：命令占位符形态、notes 声明关键词。
    """

    violations: list[str] = []
    fail = False
    poc = document.get("poc_skeleton")
    if not isinstance(poc, Mapping):
        return {"verdict": "FAIL", "violations": ["poc_skeleton 缺失或非对象"]}
    if poc.get("executable_files_created") != []:
        violations.append(f"executable_files_created 非空: {poc.get('executable_files_created')!r}")
        fail = True
    commands = poc.get("command_skeleton")
    if commands is not None:
        if not isinstance(commands, list):
            violations.append("command_skeleton 非列表")
        else:
            for index, command in enumerate(commands):
                if not isinstance(command, str) or not ("<" in command or command.startswith("#")):
                    violations.append(f"command_skeleton[{index}] 无占位符且非注释形态")
    if poc.get("kind") not in _LEGAL_POC_KINDS:
        violations.append(f"poc kind 非法: {poc.get('kind')!r}")
        fail = True
    if poc.get("component_kind") not in _LEGAL_COMPONENT_KINDS:
        violations.append(f"component_kind 非法: {poc.get('component_kind')!r}")
        fail = True
    notes = poc.get("notes")
    if isinstance(notes, list) and notes and not any(
        any(kw in str(note) for kw in _NOTE_KEYWORDS) for note in notes
    ):
        violations.append("notes 缺少占位符/授权声明关键词")
    return {
        "verdict": "FAIL" if fail else ("WARN" if violations else "PASS"),
        "violations": violations,
    }
