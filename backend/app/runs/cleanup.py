"""按安全边界清理扫描中间产物、敏感内容或完整任务目录。

v2026-08-09 修复：`CleanupService.cleanup("delete_run")` 必须同步删除数据库
记录（routes.py:275-276 之前散落的 ``repository.delete_run_record`` 调用
整合进此处），防止磁盘已删但 SQLite 仍残留孤儿导致前端 ``/api/runs`` 数量
不刷新。Cascade 由 SQLite 外键自动级联 findings。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.runs.storage import RunStorage
from app.shared.errors import ValidationError

PRUNE_DIRS = (
    "input", "decompile", "index", "rule-work", "rule-results", "slices",
    "ai-cache", "ai-trace", "logs", "tmp",
)
SENSITIVE_DIRS = PRUNE_DIRS + ("findings", "reports")


class CleanupService:
    """执行任务级清理并在清单中保留可审计记录。"""

    def __init__(self, storage: RunStorage, repository: Any = None):
        self.storage = storage
        self.repository = repository  # 可选：v2026-08-09 引入，用于 delete_run 同步

    def clear_shared_ai_cache(self, *, confirm: bool = False) -> dict[str, Any]:
        """显式清理共享缓存；绝不由任何单 run 清理策略隐式调用。"""

        if not confirm:
            raise ValidationError("清理共享 AI 缓存需要 confirm=true", "CACHE_DELETE_CONFIRMATION_REQUIRED")
        self.storage.clear_shared_ai_cache()
        return {"status": "completed", "deleted": ["shared_ai_cache"]}

    def cleanup(self, run_id: str, mode: str, confirm_delete: bool = False) -> dict[str, Any]:
        """按模式删除受控目录并返回清理结果。

        完整删除必须显式确认；非法模式或不安全路径会抛出 ``ValidationError``。
        ``delete_run`` 模式会同步删除 SQLite 任务记录（如提供了 repository）。
        """

        if mode not in {"prune_intermediates", "clear_sensitive_content", "delete_run"}:
            raise ValidationError("无效清理模式", "INVALID_CLEANUP_MODE")
        if mode == "delete_run" and not confirm_delete:
            raise ValidationError("完全删除任务需要 confirm_delete=true", "DELETE_CONFIRMATION_REQUIRED")
        run_dir = self.storage.run_dir(run_id)
        manifest = self.storage.read_manifest(run_id)
        record = {
            "mode": mode,
            "started_at": datetime.now(UTC).isoformat(),
            "status": "cleanup_in_progress",
            "deleted": [],
            "errors": [],
        }
        manifest.setdefault("cleanup_history", []).append(record)
        self.storage.write_manifest(run_id, manifest)
        if mode == "delete_run":
            self.storage.safe_remove_tree(run_dir)
            # v2026-08-09 同步数据库记录，避免磁盘已删但 SQLite 残留孤儿
            # （导致前端 /api/runs 数量不刷新）。SQLite 外键级联清理 findings。
            if self.repository is not None:
                try:
                    self.repository.delete_run_record(run_id)
                    record["deleted"].append("db_record")
                except Exception as exc:
                    record["errors"].append({"path": "db_record", "error": str(exc)})
            record["completed_at"] = datetime.now(UTC).isoformat()
            record["status"] = "partial" if record["errors"] else "completed"
            return {"run_id": run_id, "mode": mode, **record}
        targets = PRUNE_DIRS if mode == "prune_intermediates" else SENSITIVE_DIRS
        for relative in targets:
            path = run_dir / relative
            try:
                if path.exists():
                    self.storage.safe_remove_tree(path)
                    record["deleted"].append(relative)
            except Exception as exc:
                record["errors"].append({"path": relative, "error": str(exc)})
        record["completed_at"] = datetime.now(UTC).isoformat()
        record["status"] = "partial" if record["errors"] else "completed"
        if mode == "clear_sensitive_content":
            sanitized = {
                "schema_version": manifest.get("schema_version", "1.0.0"),
                "run_id": run_id,
                "created_at": manifest.get("created_at"),
                "status": manifest.get("status"),
                "stage": manifest.get("stage"),
                "apk": {"sha256": manifest.get("apk", {}).get("sha256"), "content_cleared": True},
                "cleanup_history": manifest["cleanup_history"],
            }
            self.storage.write_manifest(run_id, sanitized)
        else:
            self.storage.write_manifest(run_id, manifest)
        return {"run_id": run_id, "mode": mode, **record}
