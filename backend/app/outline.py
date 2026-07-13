"""Deterministic outline → steps parser. No LLM.

Lenient on purpose: an agent asked to "write an outline with steps" naturally
emits markdown headings, a numbered list, or checkboxes — we accept whichever it
used, so you rarely have to instruct it about any format. Whatever we extract is
shown back to you in the create dialog to edit/reorder before launch, so a
mis-parse costs a 5-second fix, not a format negotiation.
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_NUMBERED = re.compile(r"^\s{0,3}(?:\d+[.)]|step\s+\d+)[:.)：]?\s+(.+)$", re.I)
_CHECK = re.compile(r"^\s{0,3}[-*+]\s*\[.\]\s+(.+)$")
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+(.+)$")


def _split(text: str, pat: re.Pattern) -> list[dict]:
    """Each matching line starts a step; lines under it (until the next match)
    are that step's body."""
    steps: list[dict] = []
    cur: dict | None = None
    for ln in text.splitlines():
        m = pat.match(ln)
        if m:
            if cur:
                steps.append(cur)
            cur = {"title": m.group(1).strip(), "body": []}
        elif cur is not None:
            cur["body"].append(ln)
    if cur:
        steps.append(cur)
    for s in steps:
        s["body"] = "\n".join(s["body"]).strip()
    return steps


def parse_steps(text: str) -> list[dict]:
    """Return [{title, body}]. Tries the strong markers (headings) first, then
    numbered / checkbox / bullet lists; falls back to the whole text as one step."""
    # (pattern, min steps to accept it) — headings are unambiguous so 1 is enough;
    # the weaker list markers need >=2 so a single stray bullet isn't a "pipeline".
    for pat, need in ((_HEADING, 1), (_NUMBERED, 2), (_CHECK, 2), (_BULLET, 2)):
        steps = _split(text, pat)
        if len(steps) >= need:
            return steps
    body = text.strip()
    return [{"title": "步骤 1", "body": body}] if body else []
