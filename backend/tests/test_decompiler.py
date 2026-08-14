from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from app.analysis.decompiler import JadxAdapter
from app.shared.errors import DependencyError


def fake_jadx(tmp_path: Path, *, exit_code: int, create_manifest: bool, create_source: bool, errors: int = 0) -> Path:
    executable = tmp_path / f"fake-jadx-{exit_code}-{int(create_manifest)}-{int(create_source)}"
    executable.write_text(
        f"""#!{sys.executable}
import sys
from pathlib import Path
out = Path(sys.argv[sys.argv.index('-d') + 1])
out.mkdir(parents=True, exist_ok=True)
if {create_manifest!r}:
    manifest = out / 'resources' / 'AndroidManifest.xml'
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("<manifest package='com.example' />", encoding='utf-8')
if {create_source!r}:
    source = out / 'sources' / 'com' / 'example' / 'Demo.java'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('package com.example; class Demo {{}}', encoding='utf-8')
print('INFO - processing ...')
if {errors}:
    print('ERROR - finished with errors, count: {errors}')
raise SystemExit({exit_code})
""",
        "utf-8",
    )
    os.chmod(executable, 0o700)
    return executable


def test_exit_code_three_with_usable_outputs_is_partial(tmp_path: Path) -> None:
    executable = fake_jadx(tmp_path, exit_code=3, create_manifest=True, create_source=True, errors=389)
    output = tmp_path / "output"
    artifact = asyncio.run(JadxAdapter(str(executable), timeout_seconds=10).decompile(tmp_path / "sample.apk", output))

    assert artifact["status"] == "partial"
    assert artifact["exit_code"] == 3
    assert artifact["error_count"] == 389
    assert artifact["source_file_count"] == 1
    assert artifact["coverage_gaps"][0]["code"] == "JADX_PARTIAL_DECOMPILATION"
    assert "finished with errors" in (output / artifact["diagnostics"]["stdout_path"]).read_text("utf-8")


def test_exit_code_three_with_manifest_only_allows_manifest_rules(tmp_path: Path) -> None:
    executable = fake_jadx(tmp_path, exit_code=3, create_manifest=True, create_source=False, errors=8)
    artifact = asyncio.run(
        JadxAdapter(str(executable), timeout_seconds=10).decompile(tmp_path / "sample.apk", tmp_path / "output")
    )

    assert artifact["status"] == "partial"
    assert artifact["source_file_count"] == 0
    assert {gap["code"] for gap in artifact["coverage_gaps"]} == {
        "JADX_PARTIAL_DECOMPILATION",
        "JADX_NO_PSEUDO_SOURCE",
    }


def test_nonzero_exit_without_manifest_is_failure_with_stdout_diagnostic(tmp_path: Path) -> None:
    executable = fake_jadx(tmp_path, exit_code=3, create_manifest=False, create_source=False, errors=12)

    with pytest.raises(DependencyError) as exc:
        asyncio.run(
            JadxAdapter(str(executable), timeout_seconds=10).decompile(tmp_path / "sample.apk", tmp_path / "output")
        )

    assert exc.value.code == "JADX_FAILED"
    assert "finished with errors, count: 12" in exc.value.message


def test_zero_exit_with_outputs_is_success(tmp_path: Path) -> None:
    executable = fake_jadx(tmp_path, exit_code=0, create_manifest=True, create_source=True)
    artifact = asyncio.run(
        JadxAdapter(str(executable), timeout_seconds=10).decompile(tmp_path / "sample.apk", tmp_path / "output")
    )

    assert artifact["status"] == "success"
    assert artifact["exit_code"] == 0
    assert artifact["error_count"] is None
    assert artifact["coverage_gaps"] == []
