#!/usr/bin/env bash
# One-click local run: the first launch creates .venv and installs the Python
# dependencies and Patchright Chromium; later launches just start the server.
# Usage: bash start-mac-linux.sh [port]   (default 8000)
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8000}"
VENV_PY=.venv/bin/python
# Keep returned video links on the port actually served.
export DOLA_PORT="$PORT"

if [ ! -x "$VENV_PY" ]; then
  PY="$(command -v python3 || command -v python || true)"
  if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    echo "[ERROR] Python 3.10 or newer was not found. Install Python 3.12 from https://www.python.org/downloads/" >&2
    exit 1
  fi
  echo "Creating virtual environment .venv ..."
  "$PY" -m venv .venv
fi

# Reinstall only when requirements.txt changed since the last successful install.
if ! cmp -s requirements.txt .venv/requirements.installed; then
  echo "Installing Python dependencies, the first run can take a few minutes..."
  "$VENV_PY" -m pip install -r requirements.txt
  cp requirements.txt .venv/requirements.installed
fi
"$VENV_PY" -m patchright install chromium

# No private proxy by default, so Chromium uses the normal/system connection.
# A proxy can be enabled later in the dashboard Settings tab.
[ -f .env.local ] || echo "DOLA_PROXY=" > .env.local

URL="http://127.0.0.1:$PORT/"
echo
echo "Dashboard: $URL"
echo "Keep this terminal open while working. Press Ctrl+C to stop the server."
echo
if command -v curl >/dev/null; then
  (
    for _ in $(seq 1 90); do
      if curl -fs -o /dev/null "${URL}health"; then
        if command -v open >/dev/null; then open "$URL"
        elif command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1 || true
        fi
        exit 0
      fi
      sleep 1
    done
  ) &
fi
exec "$VENV_PY" -m uvicorn server:app --host 127.0.0.1 --port "$PORT"
