#!/usr/bin/env bash
# Install the SSH-client-side iTerm2 focus helper as a macOS LaunchAgent.
set -euo pipefail

LABEL="com.quzhijie.agent-hub-client-focus"
PORT="${AGENT_HUB_FOCUS_PORT:-18788}"
USER_HOME="${HOME:?HOME is not set}"
INSTALL_DIR="$USER_HOME/Library/Application Support/Agent Hub/client-focus"
HELPER_DST="$INSTALL_DIR/helper.py"
LAUNCH_DIR="$USER_HOME/Library/LaunchAgents"
PLIST="$LAUNCH_DIR/$LABEL.plist"
LOG_DIR="$USER_HOME/Library/Logs/Agent Hub"
RAW_BASE="${AGENT_HUB_RAW_BASE:-https://raw.githubusercontent.com/quzhijie/agent_hub/main/client-focus}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: this helper is for macOS (the Mac running iTerm2)" >&2
  exit 1
fi

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
  echo "error: AGENT_HUB_FOCUS_PORT must be an integer between 1024 and 65535" >&2
  exit 1
fi

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST" "$HELPER_DST"
  rmdir "$INSTALL_DIR" 2>/dev/null || true
  echo "==> Agent Hub client focus helper uninstalled"
  exit 0
fi

PYTHON_BIN="${AGENT_HUB_FOCUS_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -n "$PYTHON_BIN" && "$PYTHON_BIN" != /* ]]; then
  PYTHON_BIN="$(command -v "$PYTHON_BIN" || true)"
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "error: python3 is required; install it first (for example: brew install python)" >&2
  exit 1
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 8))'; then
  echo "error: Python 3.8 or newer is required (found: $PYTHON_BIN)" >&2
  exit 1
fi

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
SCRIPT_DIR=""
if [[ -n "$SCRIPT_SOURCE" && -f "$SCRIPT_SOURCE" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
fi
HELPER_SRC="${SCRIPT_DIR:+$SCRIPT_DIR/helper.py}"
TEMP_DIR=""
if [[ -z "$HELPER_SRC" || ! -f "$HELPER_SRC" ]]; then
  TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agent-hub-focus.XXXXXX")"
  trap '[[ -z "${TEMP_DIR:-}" ]] || rm -rf "$TEMP_DIR"' EXIT
  HELPER_SRC="$TEMP_DIR/helper.py"
  echo "==> downloading client helper"
  /usr/bin/curl -fsSL "$RAW_BASE/helper.py" -o "$HELPER_SRC"
fi

mkdir -p "$INSTALL_DIR" "$LAUNCH_DIR" "$LOG_DIR"
install -m 755 "$HELPER_SRC" "$HELPER_DST"

# plistlib avoids fragile XML escaping when user paths contain spaces or '&'.
PLIST_PATH="$PLIST" HELPER_PATH="$HELPER_DST" LOG_PATH="$LOG_DIR/client-focus.log" \
LABEL_VALUE="$LABEL" PORT_VALUE="$PORT" PYTHON_PATH="$PYTHON_BIN" "$PYTHON_BIN" - <<'PY'
import os
import plistlib

payload = {
    "Label": os.environ["LABEL_VALUE"],
    "ProgramArguments": [
        os.environ["PYTHON_PATH"],
        "-u",
        os.environ["HELPER_PATH"],
        "--port",
        os.environ["PORT_VALUE"],
    ],
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ProcessType": "Background",
    "StandardOutPath": os.environ["LOG_PATH"],
    "StandardErrorPath": os.environ["LOG_PATH"],
}
with open(os.environ["PLIST_PATH"], "wb") as handle:
    plistlib.dump(payload, handle)
PY

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

READY=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if /usr/bin/curl -fsS --max-time 1 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.2
done

if [[ "$READY" != "1" ]]; then
  echo "error: helper did not start; inspect: $LOG_DIR/client-focus.log" >&2
  exit 1
fi

echo "==> Agent Hub client focus helper installed + running"
echo "    target: iTerm2"
echo "    address: http://127.0.0.1:$PORT (loopback only)"
echo "    no SSH command changes are needed; refresh the Agent Hub page"
echo "    uninstall: curl -fsSL $RAW_BASE/uninstall.sh | bash"
