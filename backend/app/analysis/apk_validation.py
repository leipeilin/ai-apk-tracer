"""在解包前校验 APK ZIP 的结构完整性与资源消耗边界。"""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path, PurePosixPath

from app.shared.errors import ValidationError


def validate_apk_zip(
    path: Path,
    *,
    max_entries: int = 100_000,
    max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024,
) -> None:
    """验证 APK 是安全、完整且不突破条目数和解压体积上限的 ZIP。

    校验只读取归档元数据与 CRC，不向文件系统解压任何条目。
    """

    if path.is_symlink() or not path.is_file():
        raise ValidationError("APK 路径不是常规文件", "INVALID_APK_FILE")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            # 在读取条目内容前先限制目录规模，避免畸形中央目录耗尽资源。
            if len(entries) > max_entries:
                raise ValidationError("APK ZIP 条目数量超限", "ZIP_ENTRY_LIMIT")
            names: set[str] = set()
            total_uncompressed = 0
            has_manifest = False
            for entry in entries:
                name = entry.filename
                if not name or "\x00" in name or "\\" in name:
                    raise ValidationError("APK 包含非法 ZIP 条目名", "UNSAFE_ZIP_ENTRY")
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts or pure.parts[0].endswith(":"):
                    raise ValidationError(f"APK 包含路径穿越条目: {name}", "ZIP_PATH_TRAVERSAL")
                normalized = pure.as_posix().rstrip("/")
                if normalized in names:
                    raise ValidationError(f"APK 包含重复 ZIP 条目: {name}", "DUPLICATE_ZIP_ENTRY")
                names.add(normalized)
                unix_mode = (entry.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(unix_mode)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValidationError(f"APK 包含链接或特殊条目: {name}", "UNSAFE_ZIP_ENTRY_TYPE")
                if entry.flag_bits & 0x1:
                    raise ValidationError("不支持加密 ZIP 条目", "ENCRYPTED_ZIP_ENTRY")
                total_uncompressed += entry.file_size
                if total_uncompressed > max_uncompressed_bytes:
                    raise ValidationError("APK 解压后总大小超限", "ZIP_UNCOMPRESSED_LIMIT")
                has_manifest = has_manifest or normalized == "AndroidManifest.xml"
            if not has_manifest:
                raise ValidationError("APK 缺少 AndroidManifest.xml", "APK_MANIFEST_MISSING")
            bad_entry = archive.testzip()
            if bad_entry is not None:
                raise ValidationError(f"APK ZIP 完整性校验失败: {bad_entry}", "APK_ZIP_CORRUPT")
    except zipfile.BadZipFile as exc:
        raise ValidationError("文件不是有效 APK ZIP", "INVALID_APK_ZIP") from exc
