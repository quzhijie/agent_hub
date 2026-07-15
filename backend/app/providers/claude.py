"""Claude Code provider rules. First-pass heuristics — refine with real samples."""
from __future__ import annotations

import re

from .base import Provider


class ClaudeProvider(Provider):
    name = "claude"
    default_binary = "claude"
    resume_suffix = "--continue"   # reopen the last conversation in this working dir
    # Unattended pipeline mode: never stop for a permission dialog. `auto` mode
    # still escalates risky actions to a human (would hang the pipeline), so we
    # go all the way — the isolated worktree is the safety boundary. Swap to
    # "--permission-mode acceptEdits" (or "auto") here if you want it softer.
    autonomous_flags = "--dangerously-skip-permissions"

    waiting_patterns = [
        re.compile(r"Do you want to (?:proceed|make this edit|create)", re.I),
        re.compile(r"❯\s*\d+\.\s*(?:Yes|No)", re.I),   # permission dialog choices
        re.compile(r"\bwould you like to\b", re.I),
    ]
    # STRONG: only ever on screen while Claude is generating. The live footer is
    # "<glyph> <Verb>… (5m 11s · ↓ 22.0k tokens)". Note the '…' (ellipsis) and the
    # elapsed timer — these separate a WORKING verb ("✳ Whatchamacalliting…") from
    # a FINISHED one ("✻ Brewed for 0s": past tense, no '…', no live timer) and
    # from the "✻ Welcome to Claude Code" banner. The verb list is a convenience;
    # the structural patterns above it catch any rotating/newly-added verb.
    strong_generating_patterns = [
        re.compile(r"[↑↓]\s*[\d.,]+\s*k?\s*tokens?\b", re.I),   # "↓ 22.0k tokens"
        re.compile(r"\(\s*\d[\d.,]*\s*tokens?", re.I),          # older "(N tokens)" footer
        re.compile(r"^\s*[✻✳✶✽✺✵✷✸✹★][^\n]{0,48}(?:…|\.\.\.)", re.M),  # "<glyph> <Verb>…" status row
        re.compile(r"\bRunning\b[^\n]{0,48}(?:…|\.\.\.)", re.I),       # "Running 1 shell command…"
        # Named status verbs Claude Code rotates through (belt & suspenders on top
        # of the structural patterns above); the trailing '…' avoids matching prose.
        re.compile(
            r"\b(?:Accomplishing|Actioning|Actualizing|Baking|Booping|Brewing|"
            r"Calculating|Cerebrating|Channelling|Churning|Coalescing|Cogitating|"
            r"Computing|Concocting|Conjuring|Considering|Contemplating|Cooking|"
            r"Crafting|Crunching|Deciphering|Deliberating|Determining|Digesting|"
            r"Divining|Doing|Effecting|Elucidating|Enchanting|Envisioning|Finagling|"
            r"Forging|Formulating|Frolicking|Generating|Hatching|Herding|Honking|"
            r"Ideating|Imagining|Incubating|Inferring|Jazzing|Manifesting|Marinating|"
            r"Meandering|Moseying|Mulling|Musing|Mustering|Noodling|Percolating|"
            r"Perusing|Philosophising|Pondering|Pontificating|Processing|Puttering|"
            r"Puzzling|Reticulating|Ruminating|Scheming|Schlepping|Shimmying|Shucking|"
            r"Simmering|Smooshing|Spelunking|Stewing|Sussing|Synthesizing|Thinking|"
            r"Tinkering|Transmuting|Unfurling|Vibing|Whatchamacalliting|Wibbling|"
            r"Working|Wrangling)(?:…|\.\.\.)", re.I),
    ]
    # Per-line markers for the position-aware footer scan (see Provider.footer_state).
    # LIVE lines only appear while generating; DONE lines only after the turn ends.
    # The scan reads bottom-up, so whichever is LOWER (more recent) wins — a stale
    # 'Running…' above a 'Cogitated for 9m 48s' no longer reads as working.
    live_line_patterns = [
        re.compile(r"\(\s*\d+\s*[hms]\b"),                            # live "读秒" timer "(58s" / "(4m 15s"
        re.compile(r"[↑↓]\s*[\d.,]+\s*k?\s*tokens?\b", re.I),         # "↓ 3.1k tokens"
        re.compile(r"[✻✳✶✽✺✵✷✸✹★][^\n]{0,48}(?:…|\.\.\.)"),          # "✶ <Verb>…" status row
        re.compile(r"\bRunning\b[^\n]{0,48}(?:…|\.\.\.)", re.I),      # "Running 1 shell command…"
        re.compile(r"\besc to interrupt\b", re.I),
        re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒]"),                     # braille spinner
    ]
    done_line_patterns = [
        # past-tense footer: "✻ Cogitated for 9m 48s" / "✻ Worked for 7m 31s".
        # This is the ONE authoritative "turn finished" marker: it's past tense,
        # so it can never coexist with live generation.
        re.compile(r"[✻✳✶✽✺✵✷✸✹★]\s*\w+ for \d+\s*[hms]\b", re.I),
        re.compile(r"new task\?\s*/clear to save", re.I),             # post-turn idle hint
    ]
    # NOTE: the "How is Claude doing this session?" survey is deliberately NOT a
    # done marker. Claude pops it up WHILE still working (observed with a live
    # "<Verb>… (5m 11s · still thinking)" footer sitting right above it), so as a
    # position-aware "turn finished" line it wrongly read a busy seat as 空闲. A
    # genuinely idle survey screen is still caught as idle by the ❯ input box in
    # idle_patterns below, so nothing is lost.
    idle_patterns = [
        # The bottom input box: "│ >            │" (with or without trailing border).
        re.compile(r"│\s*>\s*(?:│\s*)?$", re.M),
        # idle hint line; newer UIs use ❯ + NBSP (\s matches NBSP in py3 str re)
        re.compile(r"^\s*[>❯]\s+Try\b", re.M),
        # current UI: a bare "❯" prompt row between two ──── rules (no box, no
        # "Try" hint). Making it a SPECIFIC idle marker lets it beat "frame
        # changed", so a /clear (whole-screen repaint) no longer reads as active.
        # Safe: a working Claude is caught by strong_generating first.
        re.compile(r"^\s*❯\s*$", re.M),
    ]
