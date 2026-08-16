#!/bin/zsh
set -e

ROOT="${0:A:h}"
cd "$ROOT"
export SENSUS_PROJECT_DIR="$ROOT"

PYTHON="$ROOT/.venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  echo "Creating the local Python environment..."
  python3 -m venv "$ROOT/.venv" || {
    read -k 1 "?Python environment setup failed. Press any key to close..."
    exit 1
  }
fi

if [[ ! -f "$ROOT/.venv-installed" || "$ROOT/pyproject.toml" -nt "$ROOT/.venv-installed" ]]; then
  echo "Installing the SensUs workstation..."
  "$PYTHON" -m pip install -e "$ROOT" || {
    read -k 1 "?Installation failed. Press any key to close..."
    exit 1
  }
  touch "$ROOT/.venv-installed"
fi

PORT="${SENSUS_WEB_PORT:-8765}"
exec "$ROOT/macos/start_web_server.sh" "$ROOT" "$PORT"
