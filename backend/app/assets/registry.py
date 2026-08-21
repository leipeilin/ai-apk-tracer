"""资产注册表：APK 副本（内容寻址）+ assets 表 CRUD（T1.2）。

设计依据：docs/analysis/2026-08-22-t0-8-implementation-plan.md（表结构）与
docs/analysis/2026-08-22-t1-2-implementation-plan.md（含评审 R-1~R-8 修订）。

关键语义：
- 副本内容寻址 assets_root/<sha256[:2]>/<sha256>/<basename>——与 apk_sha256
  UNIQUE 约束同源防重，删除即删目录（同 sha256 唯一资产，无引用计数）；
- 重复注册抛 ConflictError（409 + details.asset_id），**冲突时保留副本**
  （内容寻址天然幂等：同 sha256 内容必然一致，清理反而会误删既有/在用副本，
  评审 R-1）；
- registry 为纯领域模块：不读配置、不做 API 门禁（assets.enabled 检查在
  API 层，T1.4）；SQL 全部参数绑定；事务与 FK 复用 repository.connect()。
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from app.analysis.apk_validation import validate_apk_zip
from app.runs.storage import RunStorage
from app.shared.errors import ConflictError, NotFoundError, ValidationError
from app.shared.repository import SQLiteRepository

LOGGER = logging.getLogger(__name__)

ASSET_STATUS_VALUES = ("ready", "scanning", "error")
ASSET_SOURCE_UPLOAD = "local_upload"


class AssetRegistry:
    """资产注册表：内容寻址 APK 副本 + 元数据 CRUD（T0.8 assets 表）。

    资产 id 与 run id 同风格（``{UTC时间戳}_{sha256[:12]}_{uuid[:8]}``），
    以 docstring 标注防混淆（评审 R-5）。
    """

    def __init__(
        self,
        repository: SQLiteRepository,
        storage: RunStorage,
        assets_root: Path,
    ) -> None:
        # storage 复用两点：limits（settings.storage 同源——大小/zip 条目/解压
        # 体积三参数与 run 入库同源注入，评审 R-2）与 safe_remove_tree（防软
        # 链接删除，评审 R-4）。
        self._repository = repository
        self._storage = storage
        self._assets_root = assets_root.resolve()
        self._assets_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    # ------------------------------------------------------------------
    # 注册（APK 副本 + 元数据）
    # ------------------------------------------------------------------

    def register(self, source: BinaryIO, filename: str, package_name: str) -> dict[str, Any]:
        """流式接收 APK，内容寻址落副本后登记元数据并返回规范化记录。

        同 sha256 重复注册抛 ``ConflictError``（details 含既有 asset_id）；
        扩展名/大小/ZIP 结构/输入校验失败抛 ``ValidationError``；临时文件
        始终在结束时清理（对齐 RunStorage.ingest 模式）。
        """

        if not package_name or not package_name.strip():
            raise ValidationError("package_name 不能为空", "PACKAGE_NAME_REQUIRED")
        if not filename.lower().endswith(".apk"):
            raise ValidationError("仅支持 .apk 文件", "INVALID_APK_EXTENSION")
        # 拒绝含路径分隔符的文件名（显式拒绝而非 sanitize：安全默认，
        # N-2 路径穿越直接 422，不静默取 basename）
        if not filename or filename != Path(filename).name or filename in (".", ".."):
            raise ValidationError("非法 APK 文件名", "INVALID_APK_FILENAME")
        basename = filename

        limits = self._storage.limits
        digest = hashlib.sha256()
        total = 0
        max_bytes = limits.max_apk_size_mb * 1024 * 1024
        incoming = self._assets_root / f".incoming-{uuid.uuid4().hex}"
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
                max_entries=limits.max_zip_entries,
                max_uncompressed_bytes=limits.max_uncompressed_mb * 1024 * 1024,
            )
            sha256 = digest.hexdigest()
            # 内容寻址落位：同 sha256 重复注册时覆盖写（内容必然一致，无害；
            # 评审 R-1——INSERT 冲突后保留副本，不清删）。
            copy_path = self._apk_path(sha256, basename)
            copy_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(incoming, copy_path)
        finally:
            if incoming.exists():
                incoming.unlink()

        asset_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{sha256[:12]}_{uuid.uuid4().hex[:8]}"
        now = datetime.now(UTC).isoformat()
        try:
            with self._repository.connect() as db:
                db.execute(
                    """INSERT INTO assets
                    (id, package_name, apk_filename, apk_sha256, source, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        asset_id,
                        package_name.strip(),
                        basename,
                        sha256,
                        ASSET_SOURCE_UPLOAD,
                        "ready",
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            existing = self._find_by_sha256(sha256)
            raise ConflictError(
                "该 APK 已注册（sha256 重复）",
                code="ASSET_ALREADY_REGISTERED",
                details={"asset_id": existing["id"] if existing else None, "apk_sha256": sha256},
            ) from exc
        return self.get(asset_id)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, asset_id: str) -> dict[str, Any]:
        """读取单个资产，不存在时抛 ``NotFoundError``。"""

        with self._repository.connect() as db:
            row = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise NotFoundError("asset", asset_id)
        return self._asset_row(row)

    def list_assets(self, status: str | None = None) -> list[dict[str, Any]]:
        """按可选状态过滤资产列表，按创建时间倒序。"""

        with self._repository.connect() as db:
            if status is None:
                rows = db.execute("SELECT * FROM assets ORDER BY created_at DESC").fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM assets WHERE status=? ORDER BY created_at DESC", (status,)
                ).fetchall()
        return [self._asset_row(row) for row in rows]

    # ------------------------------------------------------------------
    # 更新（白名单字段，对齐 repository.update_run 先例）
    # ------------------------------------------------------------------

    def update_status(self, asset_id: str, status: str) -> dict[str, Any]:
        """更新资产状态（ready/scanning/error），资产不存在抛 ``NotFoundError``。"""

        if status not in ASSET_STATUS_VALUES:
            raise ValidationError(f"非法资产状态: {status}", "INVALID_ASSET_STATUS")
        now = datetime.now(UTC).isoformat()
        with self._repository.connect() as db:
            cursor = db.execute(
                "UPDATE assets SET status=?, updated_at=? WHERE id=?", (status, now, asset_id)
            )
            if cursor.rowcount == 0:
                raise NotFoundError("asset", asset_id)
        return self.get(asset_id)

    def link_run(self, asset_id: str, run_id: str) -> dict[str, Any]:
        """登记资产最近一次 run（T1.3 批量编排调用）。

        run 不存在先抛 ``NotFoundError``（评审 R-4：避免 FK 裸 IntegrityError
        逃逸为 500）。
        """

        self._repository.get_run(run_id)
        now = datetime.now(UTC).isoformat()
        with self._repository.connect() as db:
            cursor = db.execute(
                "UPDATE assets SET last_run_id=?, updated_at=? WHERE id=?", (run_id, now, asset_id)
            )
            if cursor.rowcount == 0:
                raise NotFoundError("asset", asset_id)
        return self.get(asset_id)

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------

    def delete(self, asset_id: str) -> None:
        """删除资产记录与内容寻址副本目录。

        顺序（评审 R-4）：先删 DB 记录（成功后副本即孤儿），再删副本目录；
        目录删除失败（含防软链接拒绝）仅记录日志不回滚 DB——记录删除是主语义，
        孤儿目录可由后续 cleanup 兜底。
        """

        asset = self.get(asset_id)
        with self._repository.connect() as db:
            db.execute("DELETE FROM assets WHERE id=?", (asset_id,))
        copy_dir = self._apk_path(asset["apk_sha256"], asset["apk_filename"]).parent
        try:
            self._storage.safe_remove_tree(copy_dir)
        except ValidationError:
            LOGGER.warning("资产副本目录删除跳过（不安全路径）: %s", copy_dir)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _find_by_sha256(self, sha256: str) -> dict[str, Any] | None:
        with self._repository.connect() as db:
            row = db.execute("SELECT * FROM assets WHERE apk_sha256=?", (sha256,)).fetchone()
        return self._asset_row(row) if row is not None else None

    def _apk_path(self, sha256: str, basename: str) -> Path:
        """内容寻址副本路径（普通方法而非 property——带参无法成 property，评审 R-5）。"""

        return self._assets_root / sha256[:2] / sha256 / basename

    def _asset_row(self, row: Any) -> dict[str, Any]:
        """行规范化：列直传 + apk_path 计算字段（服务端路径，API 层序列化时脱敏，T1.4）。"""

        record = dict(row)
        record["apk_path"] = str(self._apk_path(record["apk_sha256"], record["apk_filename"]))
        return record
