#!/usr/bin/env bash
# Nestick Tech Lead Generator — SkelerSecurity Intelligence Engine
# Desktop launcher for macOS and Linux.
# Double-click, or run ./Nestick.sh from a terminal.
set -euo pipefail

cd "$(dirname "$0")"

# --- locate a usable Python -------------------------------------------------
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "Nestick needs Python 3.10 or newer."
  echo "Install it from https://www.python.org/downloads/ and run this again."
  read -r -p "Press Enter to close…" _ || true
  exit 1
fi

# --- private virtualenv so we never touch the system Python -----------------
VENV=".nestick-venv"
if [ ! -d "$VENV" ]; then
  echo "First run: setting up (about 30 seconds)…"
  "$PY" -m venv "$VENV" 2>/dev/null || {
    echo "Could not create a virtual environment."
    echo "On Debian/Ubuntu try:  sudo apt install python3-venv"
    read -r -p "Press Enter to close…" _ || true
    exit 1
  }
fi

VPY="$VENV/bin/python"
"$VPY" -c 'import httpx' 2>/dev/null || {
  echo "Installing dependencies…"
  "$VPY" -m pip install --quiet --upgrade pip
  "$VPY" -m pip install --quiet -r requirements.txt || {
    echo "Dependency installation failed. Are you online?"
    read -r -p "Press Enter to close…" _ || true
    exit 1
  }
}

echo "Starting Nestick Tech Lead Generator…"
echo "Keep this terminal open while you use the app."
echo
exec "$VPY" -m nestick.desktop "$@"
