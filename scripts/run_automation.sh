#!/bin/bash
#═══════════════════════════════════════════════════════════════
# Taey-Ed: Headless automation launcher
#
# Runs the pipeline from source tree using venv Python.
# Kills any running Taey-Ed GUI app first to avoid conflicts.
#
# Usage:
#   ./scripts/run_automation.sh coursera                 # Coursera, Chrome, unlimited
#   ./scripts/run_automation.sh coursera --max-screens 5 # Stop after 5 screens
#   ./scripts/run_automation.sh coursera --course myid   # Specific course
#   ./scripts/run_automation.sh --stop                   # Stop running automation
#
# For CCM (Claude Code) use:
#   bash /Users/user/taey-ed/scripts/run_automation.sh coursera
#
#═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
PID_FILE="/tmp/taey-ed-cli.pid"
LOG_FILE="/tmp/taey-ed-cli.log"

# Handle --stop
if [ "${1:-}" = "--stop" ]; then
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "Stopping automation (PID $PID)..."
            kill "$PID"
            sleep 2
            if kill -0 "$PID" 2>/dev/null; then
                kill -9 "$PID" 2>/dev/null || true
            fi
            echo "Stopped."
        else
            echo "PID $PID not running."
        fi
        rm -f "$PID_FILE"
    else
        echo "No automation running (no PID file)."
    fi
    exit 0
fi

# Check venv exists
if [ ! -x "$VENV_PYTHON" ]; then
    echo "FATAL: venv not found at $VENV_PYTHON"
    echo "  Run full_rebuild.sh first to create venv."
    exit 1
fi

# Kill running Taey-Ed GUI to avoid accessibility conflicts
if pgrep -xq "Taey-Ed"; then
    echo "Killing Taey-Ed GUI app..."
    pkill -x "Taey-Ed" 2>/dev/null || true
    sleep 2
fi

# Kill any existing CLI automation
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Killing previous CLI automation (PID $OLD_PID)..."
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PID_FILE"
fi

echo "Starting Taey-Ed CLI automation..."
echo "  Log: $LOG_FILE"
echo "  Stop: $0 --stop"

# Run from project dir so 'app' package is importable
cd "$PROJECT_DIR"
"$VENV_PYTHON" run_cli.py "$@" > "$LOG_FILE" 2>&1 &
CLI_PID=$!
echo "$CLI_PID" > "$PID_FILE"

echo "  PID: $CLI_PID"

# Wait a few seconds and check it's still alive
sleep 3
if kill -0 "$CLI_PID" 2>/dev/null; then
    echo "  Running. Tail log with: tail -f $LOG_FILE"
    # Show first few lines of output
    head -20 "$LOG_FILE" 2>/dev/null || true
else
    echo "  FAILED — check $LOG_FILE"
    cat "$LOG_FILE" 2>/dev/null | tail -20
    rm -f "$PID_FILE"
    exit 1
fi
