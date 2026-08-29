#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
"$ROOT/scripts/check-backend.sh"
# node 解析：PATH 优先，其次 ~/.workbuddy 本地安装（任意版本），都不存在则报错
if command -v npm >/dev/null 2>&1; then
  exec npm run build --prefix "$ROOT/frontend"
fi
for d in "$HOME"/.workbuddy/binaries/node/versions/*/bin; do
  if [ -x "$d/npm" ]; then
    PATH="$d:$PATH" exec "$d/npm" run build --prefix "$ROOT/frontend"
  fi
done
echo "错误：PATH 中找不到 npm，且 ~/.workbuddy 下无本地 node 安装（需 Node.js 20+）" >&2
exit 1
