#!/bin/bash
# Double-click before going live. Step 1 only reads (login, funds, contract, order book, positions).
# Step 2 is optional: one 1-lot NIFTY BUY LIMIT 20% below LTP, modified, then cancelled (market hours only).
cd "$(dirname "$0")"
PYTHON=""
for p in /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
         /usr/local/bin/python3 /opt/homebrew/bin/python3 "$(which python3 2>/dev/null)"; do
    if [ -n "$p" ] && [ -x "$p" ]; then PYTHON="$p"; break; fi
done
"$PYTHON" live_order_check.py
echo ""
echo "Send one test order that is cancelled straight away?"
read -r -p "Type ANGEL, DHAN or XTS (or press Enter to skip): " BRK
if [ -n "$BRK" ]; then
    "$PYTHON" live_order_check.py --place "$BRK"
fi
echo ""
read -n 1 -s -r -p "Press any key to close..."
echo ""
