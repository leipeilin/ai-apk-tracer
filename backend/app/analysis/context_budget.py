"""Deterministic context slicing and AI request budget policy."""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from app.config import ContextBudgetSettings

POLICY_VERSION = "context-budget-v2.0"
_NUMBERED_LINE = re.compile(r"^\s*(\d+)\s*\| ?(.*)$")
_GUARD_WORD = re.compile(
    r"permission|guard|authori[sz]ation|checkCalling|enforceCalling|Binder\.getCallingUid|"
    r"SecurityException|allowlist|whitelist",
    re.IGNORECASE,
)
_PRIORITY_WORDS = {
    "source": 1,
    "sink": 1,
    "path": 1,
    "guard": 1,
    "entry": 1,
    "anchor": 1,
    "uncertain": 2,
    "gap": 2,
    "request": 2,
    "ambiguous": 2,
}


def estimate_tokens(value: Any) -> int:
    """Return a deterministic tokenizer-free approximation based on UTF-8 bytes."""

    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return max(1, math.ceil(len(value.encode("utf-8")) / 4))


@dataclass(frozen=True)
class ContextBudget:
    """Deterministic builder limits for one slice construction or extension."""

    max_contexts: int = 24
    max_additions: int = 8
    max_bytes: int = 96_000

    def __post_init__(self) -> None:
        for name in ("max_contexts", "max_additions", "max_bytes"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")

    @classmethod
    def from_settings(cls, settings: ContextBudgetSettings | Mapping[str, Any] | None) -> ContextBudget:
        if settings is None:
            values: Mapping[str, Any] = {}
        elif isinstance(settings, ContextBudgetSettings):
            values = settings.model_dump()
        else:
            values = settings
        max_input_tokens = int(values.get("max_input_tokens", 24_000))
        return cls(
            max_contexts=int(values.get("max_contexts", values.get("max_contexts_per_slice", 24))),
            max_additions=int(values.get("max_additions", values.get("max_additions_per_request", 8))),
            max_bytes=int(values.get("max_bytes", values.get("max_context_bytes_per_slice", max_input_tokens * 4))),
        )

    @property
    def limits(self) -> dict[str, int]:
        return {
            "max_contexts": self.max_contexts,
            "max_additions": self.max_additions,
            "max_bytes": self.max_bytes,
        }

    def usage(self, contexts: list[dict[str, Any]], additions: int) -> dict[str, int]:
        return {
            "contexts": len(contexts),
            "additions": additions,
            "bytes": sum(context_bytes(context) for context in contexts),
        }

    def rejection_reason(
        self,
        contexts: list[dict[str, Any]],
        context: dict[str, Any],
        additions: int,
    ) -> str | None:
        if len(contexts) >= self.max_contexts:
            return "context_limit"
        if additions >= self.max_additions:
            return "addition_limit"
        if self.usage(contexts, additions)["bytes"] + context_bytes(context) > self.max_bytes:
            return "byte_limit"
        return None


def context_bytes(context: Mapping[str, Any]) -> int:
    """Measure one context as stable compact UTF-8 JSON."""

    value = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return len(value.encode("utf-8"))


class ContextBudgeter:
    """Keep proof-critical contexts and omit lower-priority material deterministically."""

    def __init__(self, settings: ContextBudgetSettings | Mapping[str, Any] | None = None):
        if settings is None:
            self.settings = ContextBudgetSettings()
        elif isinstance(settings, ContextBudgetSettings):
            self.settings = settings
        else:
            self.settings = ContextBudgetSettings(**dict(settings))

    def trim(self, slice_document: dict[str, Any]) -> dict[str, Any]:
        """Return a deep-copied AI slice with deterministic trimming and audit metadata.

        Manifest/authorization, source/sink/path anchors and guard contexts are mandatory. Optional
        contexts are ranked then admitted by stable context ID until count/token limits; edges and
        guards are filtered to retained IDs. ``cannot_trim_safely`` keeps the original contexts for
        audit but is a hard caller contract not to send them, because a proof anchor or mandatory set
        cannot fit without unsound evidence loss.
        """

        original = deepcopy(slice_document)
        candidate = original.get("candidate", {})
        anchors = _anchor_lines(candidate)
        guard_contexts = _guard_context_ids(original)
        ranked: list[tuple[int, str, dict[str, Any], bool]] = []
        unsafe_reasons: list[dict[str, Any]] = []
        trimmed_count = 0

        for context in original.get("contexts", []):
            priority = _context_priority(context, candidate, anchors, guard_contexts)
            core = priority <= 1
            prepared, trim_status = self._trim_large_context(context, anchors, guard_contexts, core)
            if trim_status == "cannot_trim_safely":
                unsafe_reasons.append({
                    "context_id": context.get("context_id"),
                    "reason": "proof_anchors_exceed_context_window",
                })
                prepared = deepcopy(context)
            elif trim_status == "omit":
                priority = max(priority, 3)
            elif trim_status == "trimmed":
                trimmed_count += 1
            ranked.append((priority, str(context.get("context_id", "")), prepared, core))

        ranked.sort(key=lambda item: (item[0], item[1]))
        mandatory = [item for item in ranked if item[3]]
        optional = [item for item in ranked if not item[3]]
        existing_omissions = deepcopy(original.get("omitted_contexts", []))
        base = deepcopy(original)
        base["contexts"] = []
        base["edges"] = []
        base["guards"] = []
        base["omitted_contexts"] = existing_omissions
        base["policy_version"] = POLICY_VERSION
        base.pop("budget", None)

        selected = [item[2] for item in mandatory]
        cannot_fit = bool(unsafe_reasons) or len(selected) > self.settings.max_contexts_per_slice
        mandatory_probe = deepcopy(base)
        mandatory_probe["contexts"] = selected
        if estimate_tokens(mandatory_probe) > self.settings.max_input_tokens:
            cannot_fit = True
            unsafe_reasons.append({"reason": "proof_contexts_exceed_input_token_budget"})

        omitted: list[dict[str, Any]] = []
        if not cannot_fit:
            for priority, context_id, context, _ in optional:
                if len(selected) >= self.settings.max_contexts_per_slice:
                    omitted.append({"context_id": context_id, "priority": priority, "reason": "context_count_budget"})
                    continue
                probe = deepcopy(base)
                probe["contexts"] = [*selected, context]
                if estimate_tokens(probe) <= self.settings.max_input_tokens:
                    selected.append(context)
                else:
                    omitted.append({"context_id": context_id, "priority": priority, "reason": "input_token_budget"})
        else:
            selected = [item[2] for item in ranked]

        retained_ids = {str(context.get("context_id")) for context in selected}
        result = deepcopy(base)
        result["contexts"] = selected
        result["edges"] = [
            edge for edge in original.get("edges", [])
            if str(edge.get("from")) in retained_ids and str(edge.get("to")) in retained_ids
        ]
        result["guards"] = [
            guard for guard in original.get("guards", [])
            if str(guard.get("context_id")) in retained_ids
        ]
        result["omitted_contexts"] = [*existing_omissions, *omitted]
        builder_budget = ContextBudget.from_settings(self.settings)
        prior_additions = int(original.get("context_budget", {}).get("usage", {}).get("additions", 0))
        result["context_budget"] = {
            "status": "limited" if result["omitted_contexts"] else "within_budget",
            "limits": builder_budget.limits,
            "usage": builder_budget.usage(selected, prior_additions),
            "omitted_context_count": len(result["omitted_contexts"]),
        }
        original_tokens = estimate_tokens(original)
        retained_tokens = estimate_tokens(result)
        status = "cannot_trim_safely" if cannot_fit else ("trimmed" if omitted or trimmed_count else "within_budget")
        result["budget"] = {
            "status": status,
            "policy_version": POLICY_VERSION,
            "max_input_tokens": self.settings.max_input_tokens,
            "max_output_tokens": self.settings.max_output_tokens,
            "estimated_input_tokens": retained_tokens,
            "original_estimated_input_tokens": original_tokens,
            "original_context_count": len(original.get("contexts", [])),
            "retained_context_count": len(selected),
            "omitted_context_count": len(omitted),
            "trimmed_context_count": trimmed_count,
            "unsafe_reasons": unsafe_reasons,
        }
        return result

    def _trim_large_context(
        self,
        context: dict[str, Any],
        anchors: dict[str, set[int]],
        guard_contexts: set[str],
        core: bool,
    ) -> tuple[dict[str, Any], str]:
        rows = _content_rows(context)
        if len(rows) <= self.settings.max_lines_per_context:
            return deepcopy(context), "unchanged"

        path = str(context.get("path") or "")
        context_id = str(context.get("context_id") or "")
        important = {
            line for line in anchors.get(path, set())
            if int(context.get("start_line", 0)) <= line <= int(context.get("end_line", 0))
        }
        if context_id in guard_contexts or _GUARD_WORD.search(str(context.get("reason") or "")):
            important.update(line for line, text in rows if _GUARD_WORD.search(text))
        if not important:
            return (deepcopy(context), "cannot_trim_safely" if core else "omit")
        first, last = min(important), max(important)
        if last - first + 1 > self.settings.max_lines_per_context:
            return deepcopy(context), "cannot_trim_safely"

        available = self.settings.max_lines_per_context - (last - first + 1)
        start = max(int(context.get("start_line", first)), first - available // 2)
        end = start + self.settings.max_lines_per_context - 1
        context_end = int(context.get("end_line", last))
        if end > context_end:
            end = context_end
            start = max(int(context.get("start_line", first)), end - self.settings.max_lines_per_context + 1)
        kept = [(line, text) for line, text in rows if start <= line <= end]
        if not important.issubset({line for line, _ in kept}):
            return deepcopy(context), "cannot_trim_safely"

        updated = deepcopy(context)
        raw = "\n".join(text for _, text in kept)
        updated["start_line"] = start
        updated["end_line"] = end
        updated["content"] = "\n".join(f"{line:>6} | {text}" for line, text in kept)
        updated["content_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        updated["trimmed"] = True
        updated["original_start_line"] = context.get("start_line")
        updated["original_end_line"] = context.get("end_line")
        return updated, "trimmed"


def _anchor_lines(candidate: Mapping[str, Any]) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {}
    for field in ("locations", "sources", "sinks", "propagation_paths"):
        for item in candidate.get(field, []) or []:
            if not isinstance(item, Mapping):
                continue
            path = item.get("path")
            line = item.get("line") or item.get("start_line")
            try:
                line_number = int(line)
            except (TypeError, ValueError):
                continue
            if isinstance(path, str) and path and line_number >= 1:
                result.setdefault(path, set()).add(line_number)
    return result


def _guard_context_ids(document: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("context_id")) for item in document.get("guards", []) or []
        if isinstance(item, Mapping) and item.get("context_id")
    }


def _context_priority(
    context: Mapping[str, Any],
    candidate: Mapping[str, Any],
    anchors: Mapping[str, set[int]],
    guard_contexts: set[str],
) -> int:
    kind = str(context.get("kind") or "")
    context_id = str(context.get("context_id") or "")
    reason = str(context.get("reason") or "").lower()
    if kind == "manifest_component" or "manifest" in reason or "authorization" in reason:
        return 0
    path = str(context.get("path") or "")
    start = int(context.get("start_line") or 0)
    end = int(context.get("end_line") or 0)
    if context_id in guard_contexts or any(start <= line <= end for line in anchors.get(path, set())):
        return 1
    method_name = str(context.get("method_name") or "")
    if method_name and method_name in {str(item) for item in candidate.get("entry_points", []) or []}:
        return 1
    for word, priority in _PRIORITY_WORDS.items():
        if word in reason:
            return priority
    return 3


def _content_rows(context: Mapping[str, Any]) -> list[tuple[int, str]]:
    content = str(context.get("content") or "")
    parsed: list[tuple[int, str]] = []
    for offset, row in enumerate(content.splitlines()):
        match = _NUMBERED_LINE.match(row)
        if match:
            parsed.append((int(match.group(1)), match.group(2)))
        else:
            parsed.append((int(context.get("start_line") or 1) + offset, row))
    return parsed
