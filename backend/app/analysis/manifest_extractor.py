"""从 APK 提取或借助受控外部工具解码 Android Manifest。"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import zipfile
from pathlib import Path

from app.shared.errors import DependencyError, ValidationError

LOGGER = logging.getLogger(__name__)

# 单次解码子进程墙钟（manifest 远小于全量反编译 600s——M2-DEFECT-FIX D-1）
_MANIFEST_DECODE_TIMEOUT_SECONDS = 120
# 万级文件解码目录清理墙钟（超时放弃残留——下次运行前置清理兜底）
_RMTREE_TIMEOUT_SECONDS = 60
# kill 后进程回收的二次兜底（R-1：killpg 失败时派生进程持管道写端可致 communicate 永等）
_REAP_TIMEOUT_SECONDS = 10


async def _communicate_with_timeout(
    process: asyncio.subprocess.Process, timeout: float
) -> tuple[bytes, bytes]:
    """communicate + 墙钟兜底：超时 kill 进程树（对齐 JadxAdapter 先例）。

    回收仅为收尸（killpg 失败的派生进程可能持管道写端致 communicate 永等
    ——二次兜底 R-1）；无论回收成败，超时本身即失败（必抛）。"""

    try:
        return await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()
        try:
            await asyncio.wait_for(
                process.communicate(), _REAP_TIMEOUT_SECONDS)
        except TimeoutError:
            LOGGER.warning("Manifest 解码子进程回收超时（进程 pid=%s）", process.pid)
        raise DependencyError(
            f"Manifest 解码超时（>{timeout:.0f}s）", "MANIFEST_DECODE_TIMEOUT"
        )


async def _rmtree_with_timeout(path: Path) -> None:
    """rmtree 不阻塞事件循环（to_thread）+ 墙钟兜底。

    超时放弃残留（下次运行前置清理兜底；泄漏的删除线程与下次清理在
    ignore_errors=True 下并发幂等安全——评审 R-9）。
    """

    try:
        await asyncio.wait_for(
            asyncio.to_thread(shutil.rmtree, path, True), _RMTREE_TIMEOUT_SECONDS)
    except TimeoutError:
        LOGGER.warning(
            "manifest 解码目录清理超时（残留 %s——下次运行前置清理兜底）", path)


async def extract_decoded_manifest(apk_path: Path, output_path: Path) -> Path:
    """提取可读 Manifest；二进制格式依次尝试 apkanalyzer 与 JADX 解码。

    返回解码文件路径；缺少解码器或解码失败时抛出应用异常。子进程与目录
    清理均有墙钟兜底（M2-DEFECT-FIX D-1——大 APK 万级文件不再可无限阻塞）。
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
            start_new_session=True,
        )
        stdout, stderr = await _communicate_with_timeout(
            process, _MANIFEST_DECODE_TIMEOUT_SECONDS)
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
        await _rmtree_with_timeout(decode_dir)
    process = await asyncio.create_subprocess_exec(
        jadx,
        "--no-src",
        "-d",
        str(decode_dir),
        str(apk_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        start_new_session=True,
    )
    _, stderr = await _communicate_with_timeout(
        process, _MANIFEST_DECODE_TIMEOUT_SECONDS)
    decoded = decode_dir / "resources" / "AndroidManifest.xml"
    if process.returncode != 0 or not decoded.is_file():
        await _rmtree_with_timeout(decode_dir)
        raise ValidationError(
            f"jadx 无法解码 Manifest: {stderr.decode('utf-8', 'replace')[-1000:]}",
            "MANIFEST_PARSE_FAILED",
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(decoded, output_path)
    await _rmtree_with_timeout(decode_dir)
    return output_path
