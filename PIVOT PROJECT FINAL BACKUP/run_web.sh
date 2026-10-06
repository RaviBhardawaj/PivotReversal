#!/bin/bash
cd "$(dirname "$0")"
echo "=========================================================="
echo "⚡ Starting Quantum Terminal — Web UI Dashboard"
echo "🌐 URL: http://127.0.0.1:8055"
echo "=========================================================="
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 "web_server.py"
