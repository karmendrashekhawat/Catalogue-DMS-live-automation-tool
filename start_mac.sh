#!/bin/bash
set -e

echo ""
echo "  DMS Media Suite — Spyne"
echo ""

if ! command -v python3 &>/dev/null; then
    echo "  [ERROR] Python 3 not found."
    echo "  Mac:   brew install python3"
    echo "  Linux: sudo apt install python3 python3-pip"
    exit 1
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "  [1/3] Installing dependencies (first run only)..."
python3 -m pip install -q -r "$DIR/requirements.txt"

echo "  [2/3] Installing Playwright browser (first run only)..."
python3 -m playwright install chromium || true

echo "  [3/3] Starting control panel..."
echo ""
echo "  Opening http://localhost:7433"
echo "  Press Ctrl+C to stop."
echo ""

python3 "$DIR/server.py"
