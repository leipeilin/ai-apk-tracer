"""从 APK 提取或借助受控外部工具解码 Android Manifest。"""

from __future__ import annotations

import asyncio
import os
import shutil
import zipfile
from pathlib import Path

from app.shared.errors import DependencyError, ValidationError


async def extract_decoded_manifest(apk_path: Path, output_path: Path) -> Path:
    """提取可读 Manifest；二进制格式依次尝试 apkanalyzer 与 JADX 解码。

    返回解码文件路径；缺少解码器或解码失败时抛出应用异常。
    """

    with zipfile.ZipFile(apk_path) as archive:
        raw = archive.read("AndroidManifest.xml")
    if raw.lstrip().startswith(b"<"):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(raw)
        return output_path
    apkanalyzer = shutil.which("apkanalyzer")
    if apkanalyzer is not None:
        process = await asyncio.create_subprocess_exec(
            apkanalyzer,
            "manifest",
            "print",
            str(apk_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0 and stdout.lstrip().startswith(b"<"):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(stdout)
            return output_path

    jadx = shutil.which("jadx")
    if jadx is None:
        raise DependencyError(
            "二进制 AndroidManifest.xml 需要 jadx 或 Android SDK apkanalyzer 解码",
            "MANIFEST_DECODER_NOT_FOUND",
        )
    decode_dir = output_path.parent / ".manifest-decode"
    if decode_dir.exists():
        shutil.rmtree(decode_dir)
    process = await asyncio.create_subprocess_exec(
        jadx,
        "--no-src",
        "-d",
        str(decode_dir),
        str(apk_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
    )
    _, stderr = await process.communicate()
    decoded = decode_dir / "resources" / "AndroidManifest.xml"
    if process.returncode != 0 or not decoded.is_file():
        shutil.rmtree(decode_dir, ignore_errors=True)
        raise ValidationError(
            f"jadx 无法解码 Manifest: {stderr.decode('utf-8', 'replace')[-1000:]}",
            "MANIFEST_PARSE_FAILED",
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(decoded, output_path)
    shutil.rmtree(decode_dir, ignore_errors=True)
    return output_path
