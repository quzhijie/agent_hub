#!/usr/bin/env bash
# Launch Claude Code pinned to DeepSeek V4 (the agent-hub "ds4" provider).
# Same `claude` binary; DeepSeek backend via env. The API key comes from ~/.env
# because tmux respawn-pane panes are not login shells and don't source ~/.zshrc.
set -a; [ -f "$HOME/.env" ] && . "$HOME/.env"; set +a
# Separate config dir so ds4's model/session state never bleeds into your main ~/.claude.
export CLAUDE_CONFIG_DIR="$HOME/.claude-ds4"
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export ANTHROPIC_AUTH_TOKEN="${DEEPSEEK_API_KEY:-}"
export ANTHROPIC_MODEL="deepseek-v4-pro[1m]"          # [1m] -> Claude Code uses DeepSeek's full 1M context
export ANTHROPIC_DEFAULT_OPUS_MODEL="deepseek-v4-pro[1m]"
export ANTHROPIC_DEFAULT_SONNET_MODEL="deepseek-v4-pro[1m]"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="deepseek-v4-flash"
export CLAUDE_CODE_SUBAGENT_MODEL="deepseek-v4-flash"
exec claude "$@"
