#!/bin/bash
# Shelly Manager — Linux/macOS start script
# Creates a virtual environment automatically if not present

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

cd "$SCRIPT_DIR"

echo ""
echo "  =========================================="
echo "   Shelly Network Manager"
echo "   http://$(hostname -I | awk '{print $1}'):5000"
echo "  =========================================="
echo ""

# ── Create venv if missing ──────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "  [1/3] Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    echo "  [2/3] Installing dependencies..."
    "$VENV_DIR/bin/pip" install --quiet flask requests zeroconf
else
    echo "  [1/3] Virtual environment found."
    # Install/upgrade quietly in case packages are missing
    echo "  [2/3] Checking dependencies..."
    "$VENV_DIR/bin/pip" install --quiet flask requests zeroconf
fi

echo "  [3/3] Starting server..."
echo ""

# ── Run ─────────────────────────────────────────────────────────────
exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/shelly_manager.py"
