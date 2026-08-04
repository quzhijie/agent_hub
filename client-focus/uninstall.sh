#!/usr/bin/env bash
set -euo pipefail

LABEL="com.quzhijie.agent-hub-client-focus"
USER_HOME="${HOME:?HOME is not set}"
INSTALL_DIR="$USER_HOME/Library/Application Support/Agent Hub/client-focus"
PLIST="$USER_HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: this helper is for macOS" >&2
  exit 1
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST" "$INSTALL_DIR/helper.py"
rmdir "$INSTALL_DIR" 2>/dev/null || true
echo "==> Agent Hub client focus helper uninstalled"
