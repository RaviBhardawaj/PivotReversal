#!/bin/bash
# Double-click to check today's trade SLs against the sweep LL / HH rule. Places no orders.
cd "$(dirname "$0")"
PYTHON=""
for p in /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
         /usr/local/bin/python3 /opt/homebrew/bin/python3 "$(which python3 2>/dev/null)"; do
    if [ -n "$p" ] && [ -x "$p" ]; then PYTHON="$p"; break; fi
done
"$PYTHON" check_sl.py
echo ""
read -n 1 -s -r -p "Press any key to close..."
echo ""
