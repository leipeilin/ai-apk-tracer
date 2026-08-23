"""manifest 解码超时保护测试（M2-DEFECT-FIX D-1）。"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from typing import Any

import pytest

from app.analysis import manifest_extractor
from app.shared.errors import DependencyError


def _text_manifest_apk(tmp_path: Path) -> Path:
    apk = tmp_path / "plain.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("AndroidManifest.xml", "<manifest package=\"x\"/>")
    return apk


def _binary_manifest_apk(tmp_path: Path) -> Path:
    apk = tmp_path / "binary.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00binary-axml")
    return apk


class _FakeProcess:
    """有状态假进程（评审 R-1：kill 置位后 communicate 立返——回收不挂死）。"""

    def __init__(self, *, hang: bool = False, returncode: int = 0):
        self._hang = hang
        self._killed = False
        self.returncode: int | None = None if hang else returncode
        self.pid = 4242

    def kill(self) -> None:
        self._killed = True
        self.returncode = -9

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang and not self._killed:
            await asyncio.sleep(999)
        return (b"<manifest/>", b"")


def test_plain_text_manifest_short_circuits(tmp_path: Path) -> None:
    """A-1：纯文本 manifest 直写返回（零子进程）。"""

    from app.analysis.manifest_extractor import extract_decoded_manifest

    apk = _text_manifest_apk(tmp_path)
    out = tmp_path / "index" / "AndroidManifest.xml"
    result = asyncio.run(extract_decoded_manifest(apk, out))
    assert result == out
    assert out.read_text("utf-8").startswith("<manifest")


def test_decode_timeout_kills_process_and_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A-3：解码子进程超时 → kill 进程树语义 + MANIFEST_DECODE_TIMEOUT。"""

    from app.analysis.manifest_extractor import extract_decoded_manifest

    fake = _FakeProcess(hang=True)

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        assert kwargs.get("start_new_session") is True, "子进程须独立进程组（killpg 可达）"
        return fake

    monkeypatch.setattr(manifest_extractor.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(manifest_extractor.shutil, "which", lambda name: "/fake/jadx")
    monkeypatch.setattr(manifest_extractor, "_MANIFEST_DECODE_TIMEOUT_SECONDS", 0.2)

    def fake_killpg(pgid: int, sig: int) -> None:
        fake.kill()  # 进程组终止语义：置位 fake（二次 communicate 立返）

    monkeypatch.setattr(manifest_extractor.os, "killpg", fake_killpg)

    apk = _binary_manifest_apk(tmp_path)
    out = tmp_path / "index" / "AndroidManifest.xml"
    with pytest.raises(DependencyError) as exc_info:
        asyncio.run(extract_decoded_manifest(apk, out))
    assert exc_info.value.code == "MANIFEST_DECODE_TIMEOUT"
    assert fake._killed is True  # 进程被终止（残留挂死点消除）


def test_rmtree_timeout_tolerated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A-4：rmtree 超时容错——不抛、warning、走正常返回路径。"""

    from app.analysis.manifest_extractor import (
        _rmtree_with_timeout,
        extract_decoded_manifest,
    )

    called = {"rmtree": 0}

    def hanging_rmtree(path: Any, ignore_errors: bool = False) -> None:
        import time

        called["rmtree"] += 1
        # 3s 远大于注入的 0.2s 超时窗（触发 TimeoutError 分支），又不阻塞
        # pytest 退出（to_thread 线程非 daemon——sleep 999 会拖住整个进程）
        time.sleep(3)

    monkeypatch.setattr(manifest_extractor.shutil, "rmtree", hanging_rmtree)
    monkeypatch.setattr(manifest_extractor, "_RMTREE_TIMEOUT_SECONDS", 0.2)
    asyncio.run(_rmtree_with_timeout(tmp_path / "some-dir"))  # 不抛即通过
    assert called["rmtree"] == 1

    # 成功路径：jadx 正常返回 + rmtree 超时不影响结果返回
    fake = _FakeProcess(returncode=0)

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return fake

    async def fake_rmtree(path: Any) -> None:
        await asyncio.sleep(999)

    monkeypatch.setattr(manifest_extractor.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(manifest_extractor.shutil, "which", lambda name: "/fake/jadx")
    monkeypatch.setattr(manifest_extractor, "_rmtree_with_timeout", fake_rmtree)

    apk = _binary_manifest_apk(tmp_path)
    decode_dir = tmp_path / "index" / ".manifest-decode"
    decoded = decode_dir / "resources" / "AndroidManifest.xml"
    decoded.parent.mkdir(parents=True)
    decoded.write_text("<manifest/>", "utf-8")
    # 前置清理因目录存在触发挂起 rmtree（被超时兜底），jadx 正常后成功返回
    out = tmp_path / "index" / "AndroidManifest.xml"
    result = asyncio.run(extract_decoded_manifest(apk, out))
    assert result == out
    assert out.read_text("utf-8") == "<manifest/>"
