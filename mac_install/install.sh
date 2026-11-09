#!/bin/bash
# One-step update for the Pivot Reversal app on the Mac.
#   curl -fsSL https://raw.githubusercontent.com/RaviBhardawaj/PivotReversal/claude/project-thread-eykdqc/mac_install/install.sh | bash
# Installs: advanced strategy removed (fixes the startup crash), faster dashboards,
# current-expiry orders only, strike deletes that stick until Restore, and the redesigned
# glass dashboard (live trades panel, current-expiry watchlist).
# Every replaced file is backed up first; advanced_strategy* files are moved to the backup.
set -e
APP="${APP:-$HOME/Downloads/Pivot Reversal Strategy}"
BRANCH="${BRANCH:-claude/project-thread-eykdqc}"
BASE="https://raw.githubusercontent.com/RaviBhardawaj/PivotReversal/$BRANCH"
FILES="Options-angel_live-ok.py backfill_missed_entries.py deleted_strikes.py glass_dashboard.py web_server.py web/app.js glass_web/glass.js glass_web/glass.css glass_web/index.html tests/test_backfill_expiries.py tests/test_eod_plan_expiries.py tests/test_feed_failover.py tests/test_next_day_levels.py tests/test_next_expiry_gate.py tests/test_glass_current_expiry.py"

cd "$APP" || { echo "Folder not found: $APP"; exit 1; }
echo "Updating: $APP"

# 1. Download everything first; change nothing unless every file arrived and compiles.
TMP="$(mktemp -d)"
for f in $FILES; do
  mkdir -p "$TMP/$(dirname "$f")"
  curl -fsSL "$BASE/$f" -o "$TMP/$f" || { echo "Download failed: $f. Nothing was changed."; exit 1; }
done
PY="$(command -v python3)"
for p in /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 /Library/Frameworks/Python.framework/Versions/3.11/bin/python3; do [ -x "$p" ] && PY="$p" && break; done
for f in $FILES; do
  case "$f" in *.py) "$PY" -m py_compile "$TMP/$f" || { echo "Check failed: $f. Nothing was changed."; exit 1; };; esac
done

# 2. Back up, then install.
BK="_backup_$(date +%Y%m%d_%H%M%S)_oneclick"
mkdir -p "$BK"
for f in $FILES; do
  if [ -e "$f" ]; then mkdir -p "$BK/$(dirname "$f")"; cp -p "$f" "$BK/$f"; fi
  mkdir -p "$(dirname "$f")"; cp "$TMP/$f" "$f"
done
for f in advanced_strategy advanced_strategy_* tests/test_advanced_strategy.py; do
  [ -e "$f" ] && mv "$f" "$BK/" && echo "Moved $f to $BK/"
done
rm -rf "$TMP"
echo "Installed. Old files are in: $APP/$BK"

# 3. Restart the dashboard.
if pgrep -f "glass_server.py" >/dev/null || pgrep -f "run_glass_dashboard.command" >/dev/null; then
  # The run_glass_dashboard window restarts the server on its own after 5 s with the new files.
  pkill -f "glass_server.py" || true
  echo "Restarting the running dashboard (it comes back in about 5 seconds)..."
else
  open "$APP/run_glass_dashboard.command"
  echo "Started ./run_glass_dashboard.command in a new Terminal window."
fi
sleep 12
if curl -s -o /dev/null -m 5 "http://127.0.0.1:${GLASS_DASHBOARD_PORT:-8780}/"; then
  echo "Dashboard is up: http://127.0.0.1:${GLASS_DASHBOARD_PORT:-8780}/"
else
  echo "Dashboard still loading; check the Glass Dashboard Terminal window."
fi
