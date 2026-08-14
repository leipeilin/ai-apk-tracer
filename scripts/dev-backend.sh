#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON="$ROOT/backend/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  printf '%s\n' '缺少 backend/.venv；请使用 Python 3.12 创建虚拟环境并安装 requirements.txt。' >&2
  exit 1
fi
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else "需要 Python 3.12")'
cd "$ROOT/backend"
exec "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "${AI_APK_TRACER_PORT:-8000}"
