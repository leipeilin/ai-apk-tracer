#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
"$ROOT/scripts/check-backend.sh"
NODE_BIN="/Users/perrinlei/.workbuddy/binaries/node/versions/22.22.2/bin"
PATH="$NODE_BIN:$PATH" "$NODE_BIN/npm" run build --prefix "$ROOT/frontend"
