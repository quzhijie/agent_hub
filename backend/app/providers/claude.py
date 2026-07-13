"""Claude Code provider rules. First-pass heuristics — refine with real samples."""
from __future__ import annotations

import re

from .base import Provider


class ClaudeProvider(Provider):
    name = "claude"
    default_binary = "claude"
    resume_suffix = "--continue"   # reopen the last conversation in this working dir

    waiting_patterns = [
        re.compile(r"Do you want to (?:proceed|make this edit|create)", re.I),
        re.compile(r"❯\s*\d+\.\s*(?:Yes|No)", re.I),   # permission dialog choices
        re.compile(r"\bwould you like to\b", re.I),
    ]
    generating_patterns = [
        re.compile(r"esc to interrupt", re.I),
        re.compile(r"\(\s*\d[\d.,]*\s*tokens?", re.I),
        # Claude's transient status verbs shown while working.
        re.compile(r"\b(?:Herding|Cerebrating|Percolating|Simmering|Noodling|Forging)\b", re.I),
    ]
    idle_patterns = [
        # The bottom input box: "│ >            │" (with or without trailing border).
        re.compile(r"│\s*>\s*(?:│\s*)?$", re.M),
        # idle hint line; newer UIs use ❯ + NBSP (\s matches NBSP in py3 str re)
        re.compile(r"^\s*[>❯]\s+Try\b", re.M),
    ]
