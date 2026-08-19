#!/usr/bin/env bash
# Launch Codex pinned to DeepSeek V4 flash (the agent-hub "ds4-co" provider).
# Same `codex` binary; DeepSeek backend via an ISOLATED CODEX_HOME
# (~/.codex-ds4) built by DeepSeek's official codex setup script. Keeping a
# separate CODEX_HOME means ds4-co's model config, models.json and sessions
# never bleed into your normal ~/.codex (gpt-5.x + OAuth) — the two coexist.
# The API key is baked into ~/.codex-ds4/config.toml (experimental_bearer_token);
# ~/.env is sourced as a convenience/fallback.
set -a; [ -f "$HOME/.env" ] && . "$HOME/.env"; set +a
export CODEX_HOME="$HOME/.codex-ds4"
exec codex "$@"
