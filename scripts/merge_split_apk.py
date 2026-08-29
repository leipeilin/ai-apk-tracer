#!/usr/bin/env python3
"""将 split APK 的 dex 合并进 base APK，生成仅供静态分析的单一 APK。

背景：split APK 分发（App Bundle）下，base.apk 的清单声明了 split 中的组件，
但实现类在 split 的 dex 里——仅分析 base.apk 会产生"导出组件类未在代码索引中"
的反编译缺口（2026-08-29 com.xiaomi.xmsf run 实证，18 个导出组件静默丢失）。

只合并 *.dex（规则轨/探索轨与 JADX 反编译仅依赖 dex 与 base 清单）；
split 的资源、lib 与清单片段不参与合并。输出未重签名，仅供分析，不可安装。
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

DEX_NAME_RE = re.compile(r"^classes\d*\.dex$")


def max_dex_index(names: list[str]) -> int:
    index = 1
    for name in names:
        match = re.fullmatch(r"classes(\d+)\.dex", name)
        if match:
            index = max(index, int(match.group(1)))
    return index


def merge(base_path: Path, split_paths: list[Path], output_path: Path) -> None:
    with zipfile.ZipFile(base_path) as base:
        base_names = base.namelist()
        if "AndroidManifest.xml" not in base_names:
            raise SystemExit("base.apk 缺少 AndroidManifest.xml")
        next_index = max_dex_index(base_names) + 1
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as out:
            for entry in base.infolist():
                out.writestr(entry, base.read(entry.filename))
            added: list[tuple[str, Path, str]] = []
            for split_path in split_paths:
                with zipfile.ZipFile(split_path) as split:
                    for name in split.namelist():
                        if not DEX_NAME_RE.fullmatch(name):
                            continue
                        target = (
                            name
                            if name not in base_names
                            and not any(a[0] == name for a in added)
                            else f"classes{next_index}.dex"
                        )
                        if target != name:
                            next_index += 1
                        out.writestr(target, split.read(name))
                        added.append((target, split_path, name))
    print(f"输出: {output_path} ({output_path.stat().st_size / 1048576:.1f} MB)")
    for target, split_path, source in added:
        print(f"  + {target}  <-  {split_path.name}:{source}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path, help="base.apk 路径")
    parser.add_argument("--split", required=True, action="append", type=Path, help="split APK 路径（可重复）")
    parser.add_argument("--output", required=True, type=Path, help="合并输出路径")
    args = parser.parse_args()
    for path in [args.base, *args.split]:
        if not path.is_file():
            raise SystemExit(f"文件不存在: {path}")
    merge(args.base, args.split, args.output)


if __name__ == "__main__":
    sys.exit(main())
