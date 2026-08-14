"""确定性 guard 验证器（v2026-08-09，§12 后续项）。

背景：源码级评判发现 AI 在"前置 guard 语义"上有系统性盲区——ADBDebugActivity 的
`(getApplicationInfo().flags & 2)==0 → return`（FLAG_DEBUGGABLE 检查）被 AI 忽略，
导致 release 包（debuggable=false）下不可利用的链路被判为高置信（评分 6，2/6 误报）。

本模块在 AI 分析后、decision 前对候选做确定性 guard 检测：
- 数据源：run 索引库 analysis.sqlite3 的 files.content（方法原文，只读）。
- 当前实现：debuggable 检查（模式匹配 + manifest debuggable 状态判定是否阻断）。
- 输出：guard_blocks 列表，decision 层据此降级 AI 判定。

与规则层"消除假闭链"（LocalBroadcast）同思路：确定性层兜住 AI 的语义盲区。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

# debuggable guard 代码模式（方法体级别）
_DEBUGGABLE_FLAG_READ = re.compile(
    r"getApplicationInfo\s*\(\s*\)\s*\.\s*flags\s*&\s*(?:0x)?2\b|"
    r"\bflags\s*&\s*(?:0x)?2\b|"
    r"ApplicationInfo\.FLAG_DEBUGGABLE|"
    r"\bFLAG_DEBUGGABLE\b",
    re.S,
)
# 与 guard 匹配的 early-return 结构：if 条件（允许嵌套括号，不含 {）+ 紧跟 return
_EARLY_RETURN = re.compile(r"\bif\b[^{]{0,220}\{\s*return\b|\bif\b[^;{]{0,220}\breturn\b", re.S)
# 方法签名（含 body 起点）
_METHOD_SIGNATURE = re.compile(r"(?P<sig>[\w<>\[\],.\s]+\b\w+\s*\([^)]*\)\s*(?:throws\s+[\w.,\s]+)?\s*\{)", re.S)
# 方法调用（排除关键字）
_METHOD_CALL = re.compile(r"\b(?!if|for|while|switch|return|super|new|catch|synchronized|throw)([a-zA-Z_]\w*)\s*\(")


def _file_content(con: sqlite3.Connection, path: str) -> str | None:
    row = con.execute(
        "SELECT content FROM files WHERE path = ? LIMIT 1", (path,)
    ).fetchone()
    if not row or not row[0]:
        return None
    raw = row[0]
    try:
        return raw.decode() if isinstance(raw, bytes) else raw
    except (UnicodeDecodeError, AttributeError):
        return None


def _method_at_line(content: str, line: int) -> tuple[str, str] | None:
    """提取包含目标行的方法体（签名 + body），用于检测入口/链路方法内的 guard。

    返回 (signature, body)；无法定位时返回 None。
    """

    lines = content.splitlines()
    if not lines or line < 1 or line > len(lines):
        return None
    target = line - 1
    # 从目标行向前找最近的、body 起点在该行之前的方法签名
    for idx in range(target, -1, -1):
        match = _METHOD_SIGNATURE.search(lines[idx])
        if not match:
            continue
        sig = match.group("sig")
        open_pos = sig.rfind("{")
        # 配平括号找方法体结束
        depth = 0
        started = False
        for j in range(idx, len(lines)):
            chunk = lines[j]
            if j == idx:
                chunk = chunk[open_pos:] if open_pos >= 0 else chunk
            for ch in chunk:
                if ch == "{":
                    depth += 1
                    started = True
                elif ch == "}":
                    depth -= 1
                    if started and depth == 0:
                        body = "\n".join(lines[idx:j + 1])
                        return sig[:120], body
        # 未配平（方法体跨文件尾）→ 返回从签名到结尾
        if started:
            return sig[:120], "\n".join(lines[idx:])
    return None


def _has_debuggable_guard(method_body: str) -> bool:
    """方法体是否含 debuggable 前置 guard（读 FLAG_DEBUGGABLE + early return）。

    早期 return 必须在 flag 读取附近（前 120 / 后 300 字符窗口内），避免把
    方法内无关的 if-return 误判为 guard。
    """

    match = _DEBUGGABLE_FLAG_READ.search(method_body)
    if not match:
        return False
    window = method_body[max(0, match.start() - 120): match.end() + 300]
    return bool(_EARLY_RETURN.search(window))


def _method_calls(body: str) -> list[str]:
    """提取方法体内的被调用方法名（一层）。"""

    return [m for m in _METHOD_CALL.findall(body)]


def _find_method_by_name(content: str, name: str) -> tuple[str, str] | None:
    """在同一文件内按方法名定位方法体（取第一个），用于一层调用跟随。"""

    for match in _METHOD_SIGNATURE.finditer(content):
        sig = match.group("sig")
        if not re.search(rf"\b{re.escape(name)}\s*\(", sig):
            continue
        start = match.start()
        open_pos = sig.rfind("{")
        depth = 0
        started = False
        lines = content[start:].splitlines(keepends=True)
        for k, chunk in enumerate(lines):
            seg = chunk
            if k == 0:
                seg = chunk[open_pos:] if open_pos >= 0 else chunk
            for ch in seg:
                if ch == "{":
                    depth += 1
                    started = True
                elif ch == "}":
                    depth -= 1
                    if started and depth == 0:
                        body = "".join(lines[:k + 1])
                        return sig[:120], body
        if started:
            return sig[:120], "".join(lines)
    return None


def verify_candidate_guards(candidate: dict[str, Any], index_path: str | None) -> list[dict[str, Any]]:
    """对候选的 source/sink 证据做确定性 guard 检测（当前：debuggable）。

    manifest 为 debuggable=true（debug 包）时 guard 不阻断，直接返回 []；
    仅 release 包（debuggable 非 true）下 guard 才构成阻断。
    """

    manifest = candidate.get("manifest_facts") or {}
    if manifest.get("debuggable") is True:
        return []
    if not index_path or not Path(index_path).exists():
        return []
    blocked: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(f"file:{index_path}?mode=ro&immutable=1", uri=True)
        con.execute("PRAGMA query_only=ON")
        try:
            evidence = list(candidate.get("sources") or []) + list(candidate.get("sinks") or [])
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                path = item.get("path")
                line = item.get("line")
                if not path or not isinstance(line, int) or line < 1:
                    continue
                content = _file_content(con, str(path))
                if not content:
                    continue
                located = _method_at_line(content, line)
                if not located:
                    continue
                sig, body = located
                method_name = sig.split("(")[0].rsplit(" ", 1)[-1].strip()
                if _has_debuggable_guard(body):
                    blocked.append({
                        "type": "debuggable",
                        "path": str(path),
                        "line": line,
                        "method": method_name,
                    })
                    break
                # 一层调用跟随：入口方法本身无 guard 时，检查其直接调用的方法
                # （如 onNewIntent → handleIntent，guard 在 handleIntent 开头）。
                for call in _method_calls(body):
                    if call == method_name:
                        continue
                    called = _find_method_by_name(content, call)
                    if called and _has_debuggable_guard(called[1]):
                        blocked.append({
                            "type": "debuggable",
                            "path": str(path),
                            "line": line,
                            "method": call,
                        })
                        break
                if any(b.get("type") == "debuggable" for b in blocked):
                    break
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return []
    return blocked


def apply_guard_verification(
    candidate: dict[str, Any],
    index_path: str | None,
    manifest_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """候选后处理入口：写 guard_blocks + guard_blocked；幂等（已有则跳过）。

    guard_blocked（bool）是 funnel `_pipeline_requires_ai` 跳 AI 用的快捷标志（方案 X'）；
    guard_blocks（list）是 decision 判 blocked 语义用的证据。两字段必须同写同删。
    """

    if candidate.get("guard_blocks"):
        return candidate
    if manifest_facts is not None:
        candidate["manifest_facts"] = manifest_facts
    blocks = verify_candidate_guards(candidate, index_path)
    if blocks:
        candidate["guard_blocks"] = blocks
        candidate["guard_blocked"] = True
    return candidate
