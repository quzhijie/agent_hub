"""Codex CLI provider rules. First-pass heuristics — refine with real samples."""
from __future__ import annotations

import re

from .base import Provider


class CodexProvider(Provider):
    name = "codex"
    default_binary = "codex"
    resume_suffix = "resume --last"   # resume the most recent recorded session

    waiting_patterns = [
        re.compile(r"\ballow (?:command|this)\b", re.I),
        re.compile(r"\bapprove\b.*\?", re.I),
        re.compile(r"\bpress\s+y\b", re.I),
        re.compile(r"\brun this command\?", re.I),
        re.compile(r"❯\s*(?:Yes|No|Approve|Deny)", re.I),
    ]
    generating_patterns = [
        re.compile(r"esc to interrupt", re.I),
        re.compile(r"\bworking\b[.…]", re.I),
        re.compile(r"\bthinking\b[.…]", re.I),
    ]
    idle_patterns = [
        re.compile(r"│\s*>\s*(?:│\s*)?$", re.M),
        # input line, empty or with a placeholder suggestion ("› Implement {feature}").
        # Safe: waiting/generating are checked first, so a working codex never lands here.
        re.compile(r"^\s*›\s", re.M),
    ]
