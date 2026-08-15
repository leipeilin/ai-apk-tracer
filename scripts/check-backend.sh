#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON="$ROOT/backend/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  printf '%s\n' '缺少 backend/.venv；请使用 Python 3.12 创建虚拟环境并安装 requirements.txt。' >&2
  exit 1
fi
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else "需要 Python 3.12")'
"$PYTHON" -m compileall -q "$ROOT/backend/app" "$ROOT/rules"
cd "$ROOT/backend"
"$PYTHON" -m pytest
"$PYTHON" -c 'from pathlib import Path; import yaml; root=Path("../rules"); paths=list(root.glob("*/*/rule.yaml")); assert len(paths)==30, f"expected 30 rule contracts, got {len(paths)}"; assert sum(not yaml.safe_load(p.read_text())["builtin"] for p in paths)==0; print("规则契约检查通过: 30")'
