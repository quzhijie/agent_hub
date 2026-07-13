#!/usr/bin/env bash
# Agent Hub launcher. Creates a dedicated venv on first run.
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"

export PYTHONPATH="$PWD/backend${PYTHONPATH:+:$PYTHONPATH}"

if [ ! -x "$PY" ]; then
  echo "==> creating venv"
  python3 -m venv "$VENV"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet fastapi "uvicorn[standard]" pydantic pytest httpx
fi

case "${1:-run}" in
  run)
    exec "$PY" -m app.main
    ;;
  test)
    exec "$PY" -m pytest
    ;;
  *)
    echo "usage: ./run.sh [run|test]" >&2
    exit 2
    ;;
esac
