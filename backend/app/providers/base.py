"""Provider abstraction.

Each provider is a pure, testable set of rules over ANSI-cleaned pane text.
These rules NEVER send input to a terminal — they only read.

The status heuristics here are a conservative first pass. TUI agents redraw a
full screen every frame (spinner, token counters, a bordered input box at the
bottom), so single-frame guesses are unreliable — the sampler's main signal is
"did the frame change between samples". Refine these patterns against real,
de-identified capture-pane samples (see tests/).
"""
from __future__ import annotations

import re
import shutil

from ..textutil import last_lines, meaningful_tail

# --- generic patterns (shared by all providers) -----------------------------

_GENERIC_WAITING = [
    re.compile(r"\((?:y/n|yes/no|y/N|Y/n)\)", re.I),
    re.compile(r"\[(?:y/n|yes/no|Y/n|y/N)\]", re.I),
    re.compile(r"\bdo you want to\b", re.I),
    re.compile(r"\bproceed\?", re.I),
    re.compile(r"\ballow\b.*\?", re.I),
    re.compile(r"\bpress\s+(?:enter|return|any key)\b", re.I),
    re.compile(r"\bcontinue\?\s*$", re.I),
    re.compile(r"^\s*❯?\s*\d+\.\s", re.M),   # numbered selection menu
]

_GENERIC_GENERATING = [
    re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒]"),  # spinner glyphs
    re.compile(r"\besc to interrupt\b", re.I),
    re.compile(r"\bctrl\+c to (?:stop|interrupt|cancel)\b", re.I),
    re.compile(r"\b(?:thinking|generating|working|running|compiling)\b[.…]*", re.I),
]

_GENERIC_IDLE = [
    re.compile(r"[$%#]\s*$"),          # shell prompt
    re.compile(r"^\s*[>❯›»]\s*$", re.M),  # empty input marker line
]


class Provider:
    name = "base"
    default_binary: str | None = None

    # Subclasses append provider-specific patterns.
    waiting_patterns: list[re.Pattern] = []
    generating_patterns: list[re.Pattern] = []
    idle_patterns: list[re.Pattern] = []

    def __init__(self, tail_lines: int = 20):
        self.tail_lines = tail_lines

    # --- launch -----------------------------------------------------------
    def resolve_command(self, launch_command: str) -> str:
        lc = (launch_command or "").strip()
        if lc:
            return lc
        if self.default_binary:
            return shutil.which(self.default_binary) or self.default_binary
        raise ValueError(f"provider {self.name!r} requires an explicit launch command")

    # Suffix appended when RE-starting a seat that ran before, so the agent
    # resumes its last conversation instead of starting blank (e.g. claude's
    # "--continue"). Only applied to the DEFAULT command — a user-supplied
    # launch command is never mutated; the user knows their own flags best.
    resume_suffix: str | None = None

    def resolve_resume_command(self, launch_command: str) -> str:
        lc = (launch_command or "").strip()
        if lc or not self.resume_suffix:
            return self.resolve_command(lc)
        return f"{self.resolve_command('')} {self.resume_suffix}"

    # --- detection --------------------------------------------------------
    def _tail(self, frame: str) -> str:
        return last_lines(frame, self.tail_lines)

    def is_waiting(self, frame: str) -> bool:
        return self._match(frame, _GENERIC_WAITING + self.waiting_patterns)

    def is_generating(self, frame: str) -> bool:
        return self._match(frame, _GENERIC_GENERATING + self.generating_patterns)

    def is_idle_prompt(self, frame: str) -> bool:
        return self._match(frame, _GENERIC_IDLE + self.idle_patterns)

    def is_idle_prompt_specific(self, frame: str) -> bool:
        """Match ONLY this provider's own idle markers (not the generic ones).

        Strong enough to override "the frame changed": idle TUIs rotate
        tips/placeholders, which changes pixels without meaning work.
        """
        return self._match(frame, self.idle_patterns)

    def _match(self, frame: str, patterns: list[re.Pattern]) -> bool:
        tail = self._tail(frame)
        return any(p.search(tail) for p in patterns)

    def extract_last_message(self, frame: str, max_lines: int = 8) -> str:
        return meaningful_tail(frame, max_lines=max_lines)
