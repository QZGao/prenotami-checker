#!/bin/bash
# PrenotaMi Slot Checker - Background Runner
# Runs the Python loop mode in the background.
#
# Usage:
#   Start: nohup ./run_loop.sh &
#   Stop:  kill $(cat .runner.pid)
#   Logs:  tail -f logs/checker.log

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Save PID for easy stopping
echo $$ > .runner.pid

echo "[$(date)] PrenotaMi checker loop started (PID: $$)"
echo "[$(date)] Starting python checker loop..."
echo "[$(date)] To stop: kill $(cat .runner.pid)"

if [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
elif [ -f "venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
else
    echo "Virtual environment not found. Expected .venv/ or venv/." >&2
    exit 1
fi

python3 checker.py --loop
