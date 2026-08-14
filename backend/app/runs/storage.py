"""管理 APK 入库、任务目录、清单原子写入与防软链接清理。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Any

from app.analysis.apk_validation import validate_apk_zip
from app.shared.errors import NotFoundError, ValidationError

RUN_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{12}_[0-9a-f]{8}$")
PIPELINE_VERSION = "2.0.0"
ARTIFACT_SCHEMA_VERSIONS = {
    "candidate": "2.0.0",
    "run_manifest": "2.0.0",
    "report_payload": "2.0.0",
}
RUN_DIRS = (
    "input", "decompile", "index", "rule-work", "rule-results", "slices",
    "ai-cache", "ai-trace", "findings", "reports/evidence", "logs", "tmp",
)


class RunStorage:
    """维护隔离的任务目录，并约束文件摄入与删除边界。"""

    def __init__(self, data_root: Path, limits: Any):
        self.data_root = data_root.resolve()
        self.runs_root = self.data_root / "runs"
        self.shared_ai_cache_root = self.data_root / "ai-cache"
        self.limits = limits
        self.runs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.shared_ai_cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.shared_ai_cache_root, 0o700)

    def ingest(self, source: BinaryIO, filename: str, trace_id: str, config: dict) -> dict:
        """流式接收并校验 APK，成功后原子移入新建任务目录。

        临时文件始终在结束时删除；扩展名、大小或 ZIP 安全校验失败时抛出
        ``ValidationError``。
        """

        if not filename.lower().endswith(".apk"):
            raise ValidationError("仅支持 .apk 文件", "INVALID_APK_EXTENSION")
        incoming = self.data_root / f".incoming-{uuid.uuid4().hex}"
        digest = hashlib.sha256()
        total = 0
        max_bytes = self.limits.max_apk_size_mb * 1024 * 1024
        try:
            with incoming.open("xb") as target:
                os.chmod(incoming, 0o600)
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValidationError("APK 超过大小上限", "APK_TOO_LARGE")
                    digest.update(chunk)
                    target.write(chunk)
            validate_apk_zip(
                incoming,
                max_entries=self.limits.max_zip_entries,
                max_uncompressed_bytes=self.limits.max_uncompressed_mb * 1024 * 1024,
            )
            sha256 = digest.hexdigest()
            run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{sha256[:12]}_{uuid.uuid4().hex[:8]}"
            run_dir = self._new_run_dir(run_id)
            apk_path = run_dir / "input" / "app.apk"
            os.replace(incoming, apk_path)
            manifest = {
                "schema_version": ARTIFACT_SCHEMA_VERSIONS["run_manifest"],
                "pipeline_version": PIPELINE_VERSION,
                "artifact_schema_versions": dict(ARTIFACT_SCHEMA_VERSIONS),
                "run_id": run_id,
                "trace_id": trace_id,
                "created_at": datetime.now(UTC).isoformat(),
                "status": "queued",
                "stage": "queued",
                "apk": {"filename": Path(filename).name, "sha256": sha256, "size_bytes": total, "path": "input/app.apk"},
                "config": config,
                "engine": {"name": "AI-APK-Tracer", "version": "0.1.0"},
                "stages": [],
                "artifacts": [],
                "cleanup_history": [],
            }
            self.write_manifest(run_id, manifest)
            return {"id": run_id, "sha256": sha256, "manifest": manifest, "apk_path": apk_path}
        finally:
            if incoming.exists():
                incoming.unlink()

    def _new_run_dir(self, run_id: str) -> Path:
        self.validate_run_id(run_id)
        run_dir = self.runs_root / run_id
        run_dir.mkdir(mode=0o700)
        for relative in RUN_DIRS:
            (run_dir / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
        return run_dir

    def run_dir(self, run_id: str, must_exist: bool = True) -> Path:
        """返回通过格式校验的任务目录，默认要求目录存在且不是软链接。"""

        self.validate_run_id(run_id)
        path = self.runs_root / run_id
        if must_exist and (not path.is_dir() or path.is_symlink()):
            raise NotFoundError("run storage", run_id)
        return path

    def read_manifest(self, run_id: str) -> dict:
        """读取常规文件形式的任务清单，拒绝缺失文件与软链接。"""

        path = self.run_dir(run_id) / "manifest.json"
        if not path.is_file() or path.is_symlink():
            raise NotFoundError("run manifest", run_id)
        manifest = json.loads(path.read_text("utf-8"))
        manifest.setdefault("schema_version", "1.0.0")
        manifest.setdefault("pipeline_version", "1.0.0")
        manifest.setdefault("artifact_schema_versions", {})
        return manifest

    def write_manifest(self, run_id: str, manifest: dict) -> None:
        """以受限权限临时文件和原子替换写入任务清单。"""

        path = self.run_dir(run_id) / "manifest.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def update_manifest(self, run_id: str, **changes: Any) -> dict:
        """合并顶层清单字段、刷新更新时间并原子写回。"""

        manifest = self.read_manifest(run_id)
        manifest.update(changes)
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        self.write_manifest(run_id, manifest)
        return manifest

    def shared_ai_cache_dir(self) -> Path:
        """返回不隶属于任何单次 run 的进程共享内容寻址缓存目录。"""

        if self.shared_ai_cache_root.is_symlink() or not self.shared_ai_cache_root.is_dir():
            raise ValidationError("共享 AI 缓存目录不安全", "UNSAFE_AI_CACHE_PATH")
        return self.shared_ai_cache_root

    def clear_shared_ai_cache(self) -> None:
        """供显式人工维护调用；任务级 prune 永远不会触及共享缓存。"""

        self.safe_remove_tree(self.shared_ai_cache_dir())
        self.shared_ai_cache_root.mkdir(mode=0o700)
        os.chmod(self.shared_ai_cache_root, 0o700)

    def safe_remove_tree(self, path: Path) -> None:
        """使用目录相对操作删除目录树，全程拒绝跟随软链接。"""
        if not path.exists() and not path.is_symlink():
            return
        # 顶层和递归条目都按不跟随软链接的方式检查，避免清理越过任务目录边界。
        if path.is_symlink():
            raise ValidationError(f"拒绝清理软链接: {path.name}", "UNSAFE_CLEANUP_PATH")
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            self._remove_dir_at(parent_fd, path.name)
        finally:
            os.close(parent_fd)

    def _remove_dir_at(self, parent_fd: int, name: str) -> None:
        """基于父目录描述符递归删除常规文件和目录，拒绝链接及特殊文件。"""

        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise ValidationError(f"拒绝清理不安全目录: {name}", "UNSAFE_CLEANUP_PATH") from exc
        try:
            for entry_name in os.listdir(directory_fd):
                metadata = os.stat(entry_name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValidationError(f"拒绝清理软链接: {entry_name}", "UNSAFE_CLEANUP_PATH")
                if stat.S_ISDIR(metadata.st_mode):
                    self._remove_dir_at(directory_fd, entry_name)
                elif stat.S_ISREG(metadata.st_mode):
                    os.unlink(entry_name, dir_fd=directory_fd)
                else:
                    raise ValidationError(f"拒绝清理特殊文件: {entry_name}", "UNSAFE_CLEANUP_PATH")
        finally:
            os.close(directory_fd)
        os.rmdir(name, dir_fd=parent_fd)

    @staticmethod
    def validate_run_id(run_id: str) -> None:
        """校验任务标识格式，阻断路径注入与跨任务访问。"""

        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValidationError("无效 run_id", "INVALID_RUN_ID")
