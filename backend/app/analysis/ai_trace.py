"""运行级增量 AI trace 与 candidate checkpoint 持久化。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def candidate_input_key(
    candidate: dict[str, Any],
    initial_slice: dict[str, Any],
    analyzer_identity: dict[str, Any],
) -> str:
    """对完整候选输入、初始上下文和模型协议身份计算恢复键。"""

    return canonical_hash({
        "checkpoint_protocol": "candidate-completed-v1",
        "candidate": candidate,
        "initial_slice": initial_slice,
        "analyzer": analyzer_identity,
    })


class AITraceStore:
    """使用 O_APPEND JSONL 和 0600 原子 checkpoint 保存可恢复状态。"""

    def __init__(self, trace_dir: str | os.PathLike[str]) -> None:
        self.trace_dir = Path(trace_dir)
        self.checkpoint_path = self.trace_dir / "checkpoint.json"
        self._lock = threading.Lock()
        self._prepare_directory(self.trace_dir, parents=True)

    def append(
        self,
        scope: str,
        track: str,
        entry: dict[str, Any],
    ) -> Path:
        scope_name = _safe_name(scope)
        track_name = _safe_name(track)
        scope_dir = self.trace_dir / scope_name
        self._prepare_directory(scope_dir)
        path = scope_dir / f"{track_name}.jsonl"
        document = {
            "schema_version": "1",
            "recorded_at": datetime.now(UTC).isoformat(),
            **entry,
        }
        payload = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        with self._lock:
            descriptor = os.open(path, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                written = os.write(descriptor, payload)
                if written != len(payload):
                    raise OSError("AI trace 未完整写入")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return path

    def completed(self, candidate_index: int, input_key: str) -> dict[str, Any] | None:
        checkpoint = self._read_checkpoint()
        entry = checkpoint.get("completed", {}).get(str(candidate_index))
        if not isinstance(entry, dict) or entry.get("input_key") != input_key:
            return None
        if entry.get("status") != "completed" or not isinstance(entry.get("candidate"), dict):
            return None
        if not isinstance(entry.get("result"), dict):
            return None
        expected_hash = canonical_hash({
            "input_key": input_key,
            "candidate": entry["candidate"],
            "result": entry["result"],
        })
        if entry.get("entry_hash") != expected_hash:
            return None
        return entry

    def save_completed(
        self,
        candidate_index: int,
        input_key: str,
        candidate: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        completed_entry = {
            "status": "completed",
            "input_key": input_key,
            "candidate": candidate,
            "result": result,
            "completed_at": datetime.now(UTC).isoformat(),
            "round_resume": None,
            "entry_hash": canonical_hash({
                "input_key": input_key,
                "candidate": candidate,
                "result": result,
            }),
        }
        with self._lock:
            checkpoint = self._read_checkpoint()
            checkpoint.setdefault("completed", {})[str(candidate_index)] = completed_entry
            checkpoint["updated_at"] = datetime.now(UTC).isoformat()
            self._atomic_write(checkpoint)

    def _read_checkpoint(self) -> dict[str, Any]:
        try:
            if self.checkpoint_path.is_symlink() or not self.checkpoint_path.is_file():
                raise FileNotFoundError
            document = json.loads(self.checkpoint_path.read_text("utf-8"))
            if not isinstance(document, dict) or document.get("schema_version") != "1":
                raise ValueError
            if not isinstance(document.get("completed"), dict):
                raise ValueError
            return document
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"schema_version": "1", "completed": {}}

    @staticmethod
    def _prepare_directory(path: Path, *, parents: bool = False) -> None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            path.mkdir(parents=parents, mode=0o700)
            info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError(f"AI trace 目录不安全: {path.name}")
        os.chmod(path, 0o700)

    def _atomic_write(self, document: dict[str, Any]) -> None:
        if self.checkpoint_path.is_symlink():
            raise OSError("checkpoint 目标不能是软链接")
        self._prepare_directory(self.trace_dir, parents=True)
        descriptor, name = tempfile.mkstemp(
            dir=self.trace_dir,
            prefix=".checkpoint.",
            suffix=".tmp",
        )
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            payload = json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.checkpoint_path)
            os.chmod(self.checkpoint_path, 0o600)
            directory_fd = os.open(
                self.trace_dir,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()


def _safe_name(value: str) -> str:
    if _SAFE_NAME.fullmatch(value):
        return value
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()[:32]
