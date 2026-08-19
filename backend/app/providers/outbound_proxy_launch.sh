#!/usr/bin/env bash
# Load the user's outbound proxy for providers that need international APIs.
# This runs inside the tmux pane: launchd and a long-lived tmux server do not
# inherit variables that were later exported from an interactive shell.
set -euo pipefail

proxy_env="${HOME:?HOME is required}/.config/proxy.env"
if [[ -r "$proxy_env" ]]; then
  # The same user-owned file is already sourced by ~/.zshrc. Sourcing it here
  # preserves quoted values and references such as HTTP_PROXY="$HTTPS_PROXY".
  # shellcheck disable=SC1090
  source "$proxy_env"
fi

exec "$@"
