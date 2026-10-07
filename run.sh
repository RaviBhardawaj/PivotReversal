#!/bin/bash
cd "$(dirname "$0")"
# Keep the Mac awake and auto-restart the app (with the feed) if it crashes or freezes
unset LOC_AUTOSTART
while true; do
    caffeinate -dimsu /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 "Options-angel_live-ok.py"
    [ $? -eq 0 ] && break
    echo "App exited — restarting in 5s (Ctrl+C to stop)"; sleep 5
    export LOC_AUTOSTART=1
done
