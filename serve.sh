#!/usr/bin/env bash
# Start the local web UI with the project virtualenv.
# Usage: ./serve.sh [--host 127.0.0.1] [--port 8765]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "Virtual environment not found at .venv/" >&2
  echo "Create it first:" >&2
  echo "  python3 -m venv .venv" >&2
  echo "  source .venv/bin/activate" >&2
  echo "  pip install -r requirements.txt" >&2
  echo "  playwright install chromium" >&2
  exit 1
fi

exec "$PYTHON" app.py --serve "$@"
