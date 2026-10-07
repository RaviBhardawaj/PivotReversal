#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  Live Option Chain — macOS One-Click Launcher
#  Double-click this file in Finder to start the app.
#  To run WITHOUT Antigravity: just double-click this file.
# ═══════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Terminal title
echo -e "\033]0;Live Option Chain — Fibonacci Pivot Scanner\007"

echo ""
echo "╔═════════════════════════════════════════════════════════════════╗"
echo "║      LIVE OPTION CHAIN — FIBONACCI PIVOT SCANNER                ║"
echo "║      Angel One | NFO · BFO · MCX | 15M + 1H MTF Pivots         ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

# ── Find Python 3 (tries common macOS locations) ──
PYTHON=""
for p in \
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3" \
    "/usr/local/bin/python3" \
    "/opt/homebrew/bin/python3" \
    "$(which python3 2>/dev/null)"; do
    if [ -f "$p" ] || command -v "$p" &>/dev/null 2>&1; then
        PYTHON="$p"
        break
    fi
done

# Fallback to 'python3' on PATH
if [ -z "$PYTHON" ]; then
    PYTHON=$(which python3 2>/dev/null)
fi

if [ -z "$PYTHON" ]; then
    echo "❌  Python 3 not found!"
    echo "    Install from: https://www.python.org/downloads/"
    echo ""
    read -r -p "Press Enter to exit..."
    exit 1
fi

PY_VER=$("$PYTHON" --version 2>&1)
echo "  Python  : $PYTHON  ($PY_VER)"
echo "  Project : $SCRIPT_DIR"
echo "  Started : $(date '+%d-%b-%Y  %H:%M:%S')"
echo ""
echo "──────────────────────────────────────────────────────────────────"
echo ""

# ── Install missing packages ──
echo "  Checking required packages..."
REQUIRED_PKGS="smart-api-python python-dotenv gspread gspread-formatting pandas requests pyotp google-auth google-auth-oauthlib"
MISSING=""

check_pkg() {
    local import_name="$1"
    "$PYTHON" -c "import $import_name" 2>/dev/null
}

check_pkg SmartApi      || MISSING="$MISSING smart-api-python"
check_pkg dotenv        || MISSING="$MISSING python-dotenv"
check_pkg gspread       || MISSING="$MISSING gspread"
check_pkg gspread_formatting || MISSING="$MISSING gspread-formatting"
check_pkg pandas        || MISSING="$MISSING pandas"
check_pkg requests      || MISSING="$MISSING requests"
check_pkg pyotp         || MISSING="$MISSING pyotp"
check_pkg google.auth   || MISSING="$MISSING google-auth"

if [ -n "$MISSING" ]; then
    echo "  ⚠️  Missing packages:$MISSING"
    echo "  Installing now (this takes ~30 seconds the first time)..."
    echo ""
    "$PYTHON" -m pip install $MISSING --quiet --no-warn-script-location
    echo "  ✅ Packages installed!"
    echo ""
fi

echo "  ✅ All packages OK"
echo ""

# ── Check .env file ──
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "  ⚠️  .env file not found!"
    echo "  Create a .env file in: $SCRIPT_DIR"
    echo "  Required variables:"
    echo "    ANGEL_API_KEY=your_api_key"
    echo "    ANGEL_CLIENT_ID=your_client_id"
    echo "    ANGEL_MPIN=your_mpin"
    echo "    ANGEL_TOTP_SECRET=your_totp_secret"
    echo ""
    read -r -p "Press Enter to exit..."
    exit 1
fi

echo "  ✅ .env credentials found"
echo ""

# ── Launch ──
echo "🚀  Launching Live Option Chain..."
echo "    (Press Ctrl+C or close this window to stop)"
echo ""
echo "─────────────────────────────────────────────────────────────────"
echo ""

# caffeinate keeps the Mac (and its network) awake while the app runs, so macOS
# sleep / App Nap can't silently stall the WebSocket or the dashboard.
# If the app crashes or freezes (exit code != 0) it is relaunched automatically and
# resumes the live feed on its own. Closing the window normally (exit 0) ends it.
# Stop macOS App Nap from throttling the Python/Tk app when its window is in the background
defaults write org.python.python NSAppSleepDisabled -bool YES 2>/dev/null
unset LOC_AUTOSTART
while true; do
    caffeinate -dimsu "$PYTHON" "Options-angel_live-ok.py"
    EXIT_CODE=$?
    echo ""
    echo "──────────────────────────────────────────────────────────────────"
    if [ $EXIT_CODE -eq 0 ]; then
        echo "  ✅ Session ended at $(date '+%H:%M:%S')"
        break
    fi
    echo "  ⚠️  App exited with code $EXIT_CODE at $(date '+%H:%M:%S') — restarting in 5s (Ctrl+C to stop)"
    sleep 5
    export LOC_AUTOSTART=1
done
echo ""
read -r -p "  Press Enter to close this window..."
