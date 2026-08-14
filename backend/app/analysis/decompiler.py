"""封装 JADX 反编译，并记录 DEX 反编译伪源码及覆盖缺口。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
from pathlib import Path

from app.shared.errors import DependencyError, ValidationError

_ERROR_COUNT_RE = re.compile(r"finished with errors,\s*count:\s*(\d+)", re.IGNORECASE)
_DIAGNOSTIC_LOG_MAX_BYTES = 1024 * 1024


class JadxAdapter:
    """以受控子进程运行 JADX，并生成可审计的反编译产物清单。"""

    version = "1.1.0"

    def __init__(self, executable: str = "jadx", timeout_seconds: int = 600):
        """配置 JADX 可执行文件和单次反编译墙钟超时。"""

        self.executable = executable
        self.timeout_seconds = timeout_seconds

    async def decompile(self, apk_path: Path, output_dir: Path) -> dict:
        """执行 JADX 并返回产物、诊断信息及覆盖缺口清单。

        即使 JADX 非零退出，只要 Manifest 可用仍返回 ``partial`` 结果；超时、
        缺少工具或无可解析 Manifest 时抛出 ``DependencyError``。
        """

        executable = shutil.which(self.executable)
        if executable is None:
            raise DependencyError("未找到 jadx；请安装 jadx 或关闭 source_analysis", "JADX_NOT_FOUND")
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        process = await asyncio.create_subprocess_exec(
            executable,
            "--show-bad-code",
            "--deobf",
            "-d",
            str(output_dir),
            str(apk_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout_seconds)
        except TimeoutError as exc:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            raise DependencyError("jadx 反编译超时", "JADX_TIMEOUT") from exc

        stdout_text = stdout.decode("utf-8", "replace")
        stderr_text = stderr.decode("utf-8", "replace")
        diagnostics_dir = output_dir / "diagnostics"
        diagnostics_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_limited_log(diagnostics_dir / "jadx-stdout.log", stdout_text)
        _write_limited_log(diagnostics_dir / "jadx-stderr.log", stderr_text)

        manifest_path = self._find_manifest_path(output_dir)
        source_files = [
            path for path in output_dir.rglob("*")
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in {".java", ".kt"}
        ]
        error_count = _extract_error_count(stdout_text, stderr_text)

        if manifest_path is None:
            diagnostic = _diagnostic_summary(stdout_text, stderr_text)
            raise DependencyError(
                f"jadx 未生成可解析 Manifest（退出码 {process.returncode}）: {diagnostic}",
                "JADX_FAILED",
            )

        # JADX 非零退出不等同于全量失败：Manifest 可用时保留产物，并显式记录覆盖缺口。
        status = "success" if process.returncode == 0 and error_count in (None, 0) else "partial"
        coverage_gaps = []
        if status == "partial":
            coverage_gaps.append({
                "code": "JADX_PARTIAL_DECOMPILATION",
                "message": f"jadx 返回退出码 {process.returncode}，部分代码可能未成功反编译",
                "error_count": error_count,
            })
        if not source_files:
            coverage_gaps.append({
                "code": "JADX_NO_PSEUDO_SOURCE",
                "message": "jadx 未生成 Java/Kotlin 伪源码，仅可继续执行 Manifest/资源级检查",
                "error_count": error_count,
            })
            status = "partial"

        files = []
        for path in sorted(output_dir.rglob("*")):
            if path.is_symlink():
                raise ValidationError("jadx 产物包含软链接", "UNSAFE_DECOMPILE_ARTIFACT")
            if path.is_file():
                files.append({
                    "path": path.relative_to(output_dir).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                    "kind": _kind(path),
                })

        artifact = {
            "schema_version": "1.0.0",
            "adapter": "jadx",
            "adapter_version": self.version,
            "executable": executable,
            "exit_code": process.returncode,
            "status": status,
            "manifest_path": manifest_path.relative_to(output_dir).as_posix(),
            "files": files,
            "source_file_count": len(source_files),
            "error_count": error_count,
            "coverage_gaps": coverage_gaps,
            "diagnostics": {
                "stdout_path": "diagnostics/jadx-stdout.log",
                "stderr_path": "diagnostics/jadx-stderr.log",
                "stdout_summary": stdout_text[-4000:],
                "stderr_summary": stderr_text[-4000:],
            },
        }
        (output_dir / "artifact-manifest.json").write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2), "utf-8"
        )
        return artifact

    @staticmethod
    def _find_manifest_path(output_dir: Path) -> Path | None:
        """仅在 JADX 约定位置查找非软链接 Manifest。"""

        candidates = [output_dir / "resources" / "AndroidManifest.xml", output_dir / "AndroidManifest.xml"]
        return next((candidate for candidate in candidates if candidate.is_file() and not candidate.is_symlink()), None)


def _extract_error_count(stdout: str, stderr: str) -> int | None:
    match = _ERROR_COUNT_RE.search(f"{stdout}\n{stderr}")
    return int(match.group(1)) if match else None


def _diagnostic_summary(stdout: str, stderr: str) -> str:
    combined = "\n".join(part.strip() for part in (stderr, stdout) if part.strip())
    return combined[-4000:] or "jadx 未输出诊断信息"


def _write_limited_log(path: Path, content: str) -> None:
    encoded = content.encode("utf-8", "replace")
    if len(encoded) > _DIAGNOSTIC_LOG_MAX_BYTES:
        encoded = b"[truncated: keeping final 1 MiB]\n" + encoded[-_DIAGNOSTIC_LOG_MAX_BYTES:]
    path.write_bytes(encoded)
    os.chmod(path, 0o600)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _kind(path: Path) -> str:
    if path.name == "AndroidManifest.xml":
        return "manifest"
    if path.suffix == ".smali":
        return "smali"
    if path.suffix in {".java", ".kt"}:
        return "pseudo_source"
    if "diagnostics" in path.parts:
        return "diagnostic"
    return "resource"
