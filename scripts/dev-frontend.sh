#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
NODE_BIN="/Users/perrinlei/.workbuddy/binaries/node/versions/22.22.2/bin"
PATH="$NODE_BIN:$PATH"
exec "$NODE_BIN/npm" run dev --prefix "$ROOT/frontend" -- --host 127.0.0.1
