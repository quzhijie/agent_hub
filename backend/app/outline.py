"""Deterministic outline → steps parser. No LLM.

Lenient on purpose: an agent asked to "write an outline with steps" naturally
emits markdown headings, a numbered list, or checkboxes — we accept whichever it
used, so you rarely have to instruct it about any format. Whatever we extract is
shown back to you in the create dialog to edit/reorder before launch, so a
mis-parse costs a 5-second fix, not a format negotiation.

Heading rule: we split on ONE heading level, not on every `#`..`######`. A real
outline nests (a `# Title`, `## Phase N` steps, `### sub-points` inside a step),
so cutting on every level shatters one step into its own subsections and promotes
the document title and reference sections into bogus steps. Instead we pick the
outermost heading level that actually *repeats* (the shallowest level seen >=2
times, code fences excluded) and cut only there; deeper headings stay in the
body. Fenced code blocks are skipped so a `# comment` inside a shell block is
never mistaken for a heading.
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_NUMBERED = re.compile(r"^\s{0,3}(?:\d+[.)]|step\s+\d+)[:.)：]?\s+(.+)$", re.I)
_CHECK = re.compile(r"^\s{0,3}[-*+]\s*\[.\]\s+(.+)$")
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+(.+)$")
_FENCE = re.compile(r"^\s{0,3}(?:```|~~~)")
# A section a well-formed outline flags as reference-not-a-step, e.g.
# "## 0. Global conventions (non-step — every agent reads this)" or
# "## 0. 全局约定（非步骤，供参考）". Must be a parenthesised marker (ASCII or
# fullwidth) so an unparenthesised mention of "step" never trips it.
_NONSTEP = re.compile(r"[（(][^）)]*(?:non-?step|非步骤)[^）)]*[）)]", re.I)


def _lines(text: str):
    """Yield (line, in_fence). Lines inside ``` / ~~~ code blocks — and the fence
    lines themselves — are flagged so callers never treat them as markers."""
    in_fence = False
    for ln in text.splitlines():
        if _FENCE.match(ln):
            in_fence = not in_fence
            yield ln, True
        else:
            yield ln, in_fence


def _heading_level(text: str) -> int | None:
    """The level to split on: the shallowest heading level that occurs >=2 times
    outside code fences (the outermost sectioning that actually repeats). If no
    level repeats, use the shallowest level present (a single-heading outline is
    one step). None when there are no headings at all."""
    counts: dict[int, int] = {}
    for ln, in_fence in _lines(text):
        if in_fence:
            continue
        m = _HEADING.match(ln)
        if m:
            lvl = len(m.group(1))
            counts[lvl] = counts.get(lvl, 0) + 1
    if not counts:
        return None
    repeated = [lvl for lvl, n in counts.items() if n >= 2]
    return min(repeated) if repeated else min(counts)


def _split_headings(text: str, level: int) -> list[dict]:
    """Cut only on headings of exactly `level`; deeper/shallower headings and all
    other lines become the current step's body."""
    steps: list[dict] = []
    cur: dict | None = None
    for ln, in_fence in _lines(text):
        m = None if in_fence else _HEADING.match(ln)
        if m and len(m.group(1)) == level:
            if cur:
                steps.append(cur)
            cur = {"title": m.group(2).strip(), "body": []}
        elif cur is not None:
            cur["body"].append(ln)
    if cur:
        steps.append(cur)
    for s in steps:
        s["body"] = "\n".join(s["body"]).strip()
    return steps


def _split(text: str, pat: re.Pattern) -> list[dict]:
    """List-marker split (numbered / checkbox / bullet). Each matching line starts
    a step; lines under it (until the next match) are that step's body. Code
    fences are skipped."""
    steps: list[dict] = []
    cur: dict | None = None
    for ln, in_fence in _lines(text):
        m = None if in_fence else pat.match(ln)
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


def _drop_nonstep(steps: list[dict]) -> list[dict]:
    """Drop sections a well-formed outline marks '(non-step …)' / '(非步骤…)' —
    global conventions, an appendix, etc. They are reference, not executable
    steps: they still travel to the agents inside the copied OUTLINE.md, but no
    seat is spawned for them. Keep the unfiltered list if that would leave
    nothing (an all-non-step outline is degenerate — better one step than zero)."""
    kept = [s for s in steps if not _NONSTEP.search(s["title"])]
    return kept or steps


def parse_steps(text: str) -> list[dict]:
    """Return [{title, body}]. Tries headings first (split on the one repeating
    level), then numbered / checkbox / bullet lists; falls back to the whole text
    as one step. Sections explicitly marked non-step are dropped from the result."""
    level = _heading_level(text)
    if level is not None:
        steps = _split_headings(text, level)
        if steps:
            return _drop_nonstep(steps)
    # the weaker list markers need >=2 so a single stray bullet isn't a "pipeline".
    for pat, need in ((_NUMBERED, 2), (_CHECK, 2), (_BULLET, 2)):
        steps = _split(text, pat)
        if len(steps) >= need:
            return _drop_nonstep(steps)
    body = text.strip()
    return [{"title": "步骤 1", "body": body}] if body else []
