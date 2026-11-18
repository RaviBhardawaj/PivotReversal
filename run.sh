#!/usr/bin/env bash
# Pivot Reversal — start the Glass dashboard on any Mac or Linux PC.
#   bash run.sh            → http://127.0.0.1:8780
# First run: makes a private Python environment (.venv), installs requirements.txt, and
# creates .env from .env.example for you to fill in. Later runs start straight away.
# A crash restarts the dashboard after 5 s; Ctrl+C stops it.
set -u
cd "$(dirname "$0")"

PY=""
for p in python3.12 python3.13 python3.11 python3 python; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$p"; break
  fi
done
[ -z "$PY" ] && { echo "Python 3.10 or newer is needed: https://www.python.org/downloads/"; exit 1; }

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up .venv with $($PY --version)..."
  "$PY" -m venv .venv || { echo "Could not create .venv (on Ubuntu/Debian: sudo apt install python3-venv)"; exit 1; }
fi
VPY=.venv/bin/python
if [ ! -f .venv/.req_ok ] || [ requirements.txt -nt .venv/.req_ok ]; then
  echo "Installing packages from requirements.txt..."
  "$VPY" -m pip install -q --upgrade pip && "$VPY" -m pip install -q -r requirements.txt || { echo "Package install failed."; exit 1; }
  touch .venv/.req_ok
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "Created .env from .env.example. Put your Angel One keys in it:"
  echo "    $(pwd)/.env"
  echo "then run  bash run.sh  again."
  exit 1
fi

KEEP_AWAKE=""
command -v caffeinate >/dev/null 2>&1 && KEEP_AWAKE="caffeinate -dimsu"   # macOS: don't sleep while it runs
echo "Glass dashboard: http://127.0.0.1:${GLASS_DASHBOARD_PORT:-8780}/   (Ctrl+C to stop)"
while true; do
  $KEEP_AWAKE "$VPY" glass_server.py
  CODE=$?
  { [ $CODE -eq 0 ] || [ $CODE -eq 130 ]; } && break
  echo "Dashboard exited with code $CODE at $(date '+%H:%M:%S'), restarting in 5 s (Ctrl+C to stop)"
  export GLASS_NO_BROWSER=1
  sleep 5
done
