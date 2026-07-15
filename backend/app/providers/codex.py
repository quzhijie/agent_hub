"""Codex CLI provider rules. First-pass heuristics — refine with real samples."""
from __future__ import annotations

import re

from .base import Provider


class CodexProvider(Provider):
    name = "codex"
    default_binary = "codex"
    resume_suffix = "resume --last"   # resume the most recent recorded session
    # Non-interactive pipeline mode: `codex exec` reads the prompt from stdin and
    # runs to completion; the bypass flag skips every approval + the sandbox.
    headless_flags = "exec --dangerously-bypass-approvals-and-sandbox"

    waiting_patterns = [
        re.compile(r"\ballow (?:command|this)\b", re.I),
        re.compile(r"\bapprove\b.*\?", re.I),
        re.compile(r"\bpress\s+y\b", re.I),
        re.compile(r"\brun this command\?", re.I),
        re.compile(r"❯\s*(?:Yes|No|Approve|Deny)", re.I),
    ]
    # STRONG: Codex's live footer is "• Working (8s • esc to interrupt)". Match the
    # present-tense "Working (" — NOT the finished divider "─ Worked for 9m 51s ─"
    # (past tense), which is an idle screen.
    strong_generating_patterns = [
        re.compile(r"\bWorking\b[^\n]{0,30}\(", re.I),
        re.compile(r"\(\s*\d+\s*s\b[^)]*esc to interrupt", re.I),
    ]
    generating_patterns = [
        re.compile(r"\bthinking\b[.…]", re.I),
    ]
    # Position-aware footer scan (see Provider.footer_state): present-tense
    # "Working (…" is live; the past-tense "─ Worked for 9m 51s ─" divider is done.
    live_line_patterns = [
        re.compile(r"\bWorking\b[^\n]{0,30}\(", re.I),
        re.compile(r"\(\s*\d+\s*s\b[^)]*esc to interrupt", re.I),
        re.compile(r"\besc to interrupt\b", re.I),
        re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒]"),
    ]
    done_line_patterns = [
        re.compile(r"\bWorked for \d+\s*[hms]\b", re.I),
    ]
    idle_patterns = [
        re.compile(r"│\s*>\s*(?:│\s*)?$", re.M),
        # input line, empty or with a placeholder suggestion ("› Implement {feature}").
        # Safe: waiting/generating are checked first, so a working codex never lands here.
        re.compile(r"^\s*›\s", re.M),
    ]
