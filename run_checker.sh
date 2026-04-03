#!/bin/bash
# Wrapper script for a single PrenotaMi checker run.

cd "$(dirname "$0")"

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

python3 checker.py "$@"
