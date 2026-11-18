#!/bin/bash
# macOS: double-click in Finder to start the Glass dashboard (http://127.0.0.1:8780).
# Same as running  bash run.sh  in Terminal.
cd "$(dirname "$0")"
bash run.sh
echo ""
read -r -p "Glass dashboard stopped. Press Enter to close this window..."
