"""ds4 provider — Claude Code pinned to DeepSeek V4.

Same Claude Code TUI and behavior as the `claude` provider, so it REUSES every
detection/resume/headless rule unchanged; only the backend differs. A tiny
launcher (ds4_launch.sh) sets the DeepSeek ANTHROPIC_* env before exec'ing
`claude` — the key is loaded from ~/.env because tmux respawn-pane panes don't
source your shell rc. Lets you run a DeepSeek-backed Claude Code seat alongside
your Anthropic-account `claude` seats.
"""
from __future__ import annotations

from pathlib import Path

from .claude import ClaudeProvider

_LAUNCHER = str(Path(__file__).resolve().with_name("ds4_launch.sh"))


class Ds4Provider(ClaudeProvider):
    name = "ds4"
    default_binary = _LAUNCHER
    # resume_suffix ("--continue"), headless_flags ("-p --dangerously-skip-
    # permissions") and all detection patterns are inherited from ClaudeProvider
    # unchanged — under the hood it IS Claude Code.
