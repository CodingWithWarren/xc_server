#!/usr/bin/env bash
# Launch the XC Training server using the Node binary bundled with VS Code
# (system Node/npm are not installed in this environment).
set -euo pipefail

NODE="${NODE_BIN:-/home/warren/.vscode-server/bin/6928394f91b684055b873eecb8bc281365131f1c/node}"

if [ ! -x "$NODE" ]; then
  # Fall back to whatever node is on PATH
  NODE="$(command -v node || true)"
fi

if [ -z "$NODE" ]; then
  echo "No Node.js binary found. Install Node >= 22.5 or set NODE_BIN." >&2
  exit 1
fi

cd "$(dirname "$0")"
exec "$NODE" --env-file-if-exists=.env src/index.js
