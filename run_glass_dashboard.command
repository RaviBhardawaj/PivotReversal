#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  Glass Dashboard — the project's web dashboard, on its own.
#  Double-click in Finder. Opens http://127.0.0.1:8780 in your browser.
#  Use it INSTEAD of run_option_chain.command, not at the same time.
# ═══════════════════════════════════════════════════════════════
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
echo -e "\033]0;Glass Dashboard — Fibonacci Pivot Reversal\007"

PYTHON=""
for p in \
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3" \
    "/usr/local/bin/python3" \
    "/opt/homebrew/bin/python3" \
    "$(which python3 2>/dev/null)"; do
    if [ -n "$p" ] && [ -x "$p" ]; then PYTHON="$p"; break; fi
done
if [ -z "$PYTHON" ]; then
    echo "❌  Python 3 not found. Install it from https://www.python.org/downloads/"
    read -r -p "Press Enter to exit..."; exit 1
fi
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "⚠️  .env file not found in $SCRIPT_DIR (Angel One keys are needed)."
    read -r -p "Press Enter to exit..."; exit 1
fi
if pgrep -f "Options-angel_live-ok.py" >/dev/null 2>&1; then
    echo "⚠️  The Python launcher (Options-angel_live-ok.py) is running."
    echo "    The dashboard opens, but START and AUTO-TRADE stay blocked until you close the launcher."
    echo ""
fi
echo "  Python : $PYTHON ($("$PYTHON" --version 2>&1))"
echo "  URL    : http://127.0.0.1:${GLASS_DASHBOARD_PORT:-8780}/"
echo ""
# caffeinate keeps the Mac awake while the dashboard runs (same as the launcher).
# A crash or freeze (exit code != 0) restarts it after 5 s; Ctrl+C ends it.
while true; do
    caffeinate -dimsu "$PYTHON" glass_server.py
    CODE=$?
    if [ $CODE -eq 0 ] || [ $CODE -eq 130 ]; then break; fi
    echo "  ⚠️  Dashboard exited with code $CODE at $(date '+%H:%M:%S'), restarting in 5 s (Ctrl+C to stop)"
    GLASS_NO_BROWSER=1; export GLASS_NO_BROWSER
    sleep 5
done
echo ""
read -r -p "  Glass dashboard stopped. Press Enter to close this window..."
