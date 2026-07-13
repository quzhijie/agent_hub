"""Terminal text cleaning helpers used by status detection."""
from __future__ import annotations

import re

# CSI / OSC / other escape sequences.
_ANSI_RE = re.compile(
    r"""
    \x1B\[[0-?]*[ -/]*[@-~]        # CSI ... command
    | \x1B\][^\x07\x1B]*(?:\x07|\x1B\\)   # OSC ... BEL/ST
    | \x1B[@-Z\\-_]               # two-char escapes
    """,
    re.VERBOSE,
)

# Box-drawing / rule characters that make up TUI chrome borders.
_BOX_CHARS = set("─━│┃┄┅┆┇┈┉┊┋┌┍┎┏┐┑┒┓└┕┖┗┘┙┚┛├┤┬┴┼╭╮╯╰═║╔╗╚╝╠╣╦╩╬▔▁▏▕")


def strip_ansi(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    # collapse carriage returns (progress redraws) — keep the last segment per line
    out = []
    for line in text.split("\n"):
        if "\r" in line:
            line = line.split("\r")[-1]
        out.append(line)
    return "\n".join(out)


def clean_frame(raw: str) -> str:
    """ANSI-stripped, right-trimmed, trailing-blank-trimmed pane text."""
    text = strip_ansi(raw)
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    while lines and lines[0] == "":
        lines.pop(0)
    return "\n".join(lines)


_PROMPT_ONLY = re.compile(r"[>❯›»|]+")


def _is_chrome(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    # Line made only of box-drawing/space/punctuation borders.
    core = s.strip("".join(_BOX_CHARS) + " \t╌╍·.-_=")
    if core == "":
        return True
    # An empty input box, e.g. "│ >            │" -> core is just a prompt marker.
    if _PROMPT_ONLY.fullmatch(core):
        return True
    return False


def meaningful_tail(frame: str, max_lines: int = 8) -> str:
    """Last few non-chrome content lines — a preview of what the agent last said."""
    lines = frame.split("\n")
    picked: list[str] = []
    for line in reversed(lines):
        if _is_chrome(line):
            continue
        picked.append(line.rstrip())
        if len(picked) >= max_lines:
            break
    picked.reverse()
    return "\n".join(picked).strip()


def last_lines(frame: str, n: int) -> str:
    return "\n".join(frame.split("\n")[-n:])
