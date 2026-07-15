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

# STRONG: shown ONLY while the agent is actively generating. Unambiguous enough
# to beat a stray waiting/idle marker in streamed output (a "1." list, a
# "proceed?" inside a code block, the empty input box that's drawn even mid-run).
_STRONG_GENERATING = [
    re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒]"),  # braille/circle spinner (spins only while working)
    re.compile(r"\besc to interrupt\b", re.I),
    re.compile(r"\bctrl\+c to (?:stop|interrupt|cancel)\b", re.I),
    # The parenthesised live "读秒" timer: "(12s", "(5m 11s", "(8s". This is the
    # ONE marker present in every Claude/Codex working footer regardless of the
    # verb, whether it uses "…" or "...", or whether a token count is shown yet.
    # Case-SENSITIVE on [hms] so it excludes the idle footer's "(1M context)"
    # (uppercase M); a FINISHED footer says "for 9m 51s" / "Brewed for 0s" — no
    # parenthesis — so it is excluded too.
    re.compile(r"\(\s*\d+\s*[hms]\b"),
]

# WEAK: bare English verbs. A real permission prompt can legitimately contain
# "allow running this command?", so these are checked AFTER is_waiting — they
# only promote to active when nothing stronger (waiting) matched.
_WEAK_GENERATING = [
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
    strong_generating_patterns: list[re.Pattern] = []
    generating_patterns: list[re.Pattern] = []
    idle_patterns: list[re.Pattern] = []

    # Per-LINE markers for the position-aware footer scan (footer_state). A live
    # marker means "working right now"; a done marker means "turn finished". Only
    # the LOWEST (most recent) status line matters — see footer_state.
    live_line_patterns: list[re.Pattern] = []
    done_line_patterns: list[re.Pattern] = []

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

    # Flags that put the agent in unattended/autonomous mode. Appended ONLY for
    # pipeline-orchestrated seats, which run inside an isolated worktree+branch —
    # that worktree IS the risk boundary here. Without these the agent stalls at
    # its first permission/approval prompt and the whole pipeline hangs waiting
    # for a human. A user-supplied launch command is never mutated (they own
    # their flags); only the bare default binary gets the suffix.
    autonomous_flags: str | None = None

    def resolve_autonomous_command(self, launch_command: str) -> str:
        lc = (launch_command or "").strip()
        if lc or not self.autonomous_flags:
            return self.resolve_command(lc)
        return f"{self.resolve_command('')} {self.autonomous_flags}"

    # --- detection --------------------------------------------------------
    def _tail(self, frame: str) -> str:
        return last_lines(frame, self.tail_lines)

    def footer_state(self, frame: str) -> str | None:
        """Position-aware verdict from the LOWEST status line in the frame.

        Scans the tail bottom-up and returns on the FIRST line that is either a
        live-work marker ('active') or a turn-finished marker ('idle'); lines
        that are neither (chrome, prose, the input box) are skipped. Returns None
        when no status line is present, deferring to the generic rules.

        This is what stops a JUST-finished turn from reading active: its earlier
        'Running…' / '<Verb>…' lines are still in the captured window, but they
        sit ABOVE the finished footer ('Cogitated for 9m 48s'), so the finished
        line — being lower/more recent — wins.
        """
        if not (self.live_line_patterns or self.done_line_patterns):
            return None
        for line in reversed(self._tail(frame).split("\n")):
            if any(p.search(line) for p in self.live_line_patterns):
                return "active"
            if any(p.search(line) for p in self.done_line_patterns):
                return "idle"
        return None

    def is_waiting(self, frame: str) -> bool:
        return self._match(frame, _GENERIC_WAITING + self.waiting_patterns)

    def is_generating_strong(self, frame: str) -> bool:
        """Unambiguous 'actively working right now'.

        Present ONLY while the agent generates — a spinner, 'esc to interrupt',
        a live elapsed+token footer, or a rotating status verb ending in '…'.
        It is checked FIRST (before is_waiting) so that streamed output which
        happens to contain a '1.' menu or a 'proceed?' string doesn't flip a
        busy agent to waiting; and before the idle markers so the empty input
        box (drawn even mid-run) doesn't flip it to idle.
        """
        return self._match(frame, _STRONG_GENERATING + self.strong_generating_patterns)

    def is_generating(self, frame: str) -> bool:
        return self._match(frame, _WEAK_GENERATING + self.generating_patterns)

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
