"""ds4-co provider — Codex CLI pinned to DeepSeek V4 flash.

Same Codex CLI TUI and behavior as the `codex` provider, so it REUSES every
detection/resume/headless rule unchanged; only the backend differs. A tiny
launcher (ds4co_launch.sh) points CODEX_HOME at an isolated config dir
(~/.codex-ds4) built by DeepSeek's official codex setup script, then exec's
`codex`. Lets you run a DeepSeek-backed Codex seat alongside your normal
OpenAI-account `codex` seats.
"""
from __future__ import annotations

from pathlib import Path

from .codex import CodexProvider

_LAUNCHER = str(Path(__file__).resolve().with_name("ds4co_launch.sh"))


class Ds4CoProvider(CodexProvider):
    name = "ds4-co"
    default_binary = _LAUNCHER
    # DeepSeek API is domestic and reachable directly — opt out of the
    # outbound proxy that the codex provider it inherits from enables.
    needs_outbound_proxy = False
    # resume_suffix ("resume --last"), headless_flags ("exec --dangerously-
    # bypass-approvals-and-sandbox") and all detection patterns are inherited
    # from CodexProvider unchanged — under the hood it IS Codex CLI.
