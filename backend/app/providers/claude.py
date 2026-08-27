"""Claude Code provider rules. First-pass heuristics — refine with real samples."""
from __future__ import annotations

import json
import os
import re
import shlex
import uuid
from datetime import datetime
from pathlib import Path

from .base import Provider


class ClaudeProvider(Provider):
    name = "claude"
    default_binary = "claude"
    model_flag = "--model"
    model_choices = ("sonnet", "opus", "haiku")
    reasoning_effort_choices = ("low", "medium", "high", "xhigh", "max")
    reasoning_effort_flag = "--effort"
    resume_suffix = "--continue"   # reopen the last conversation in this working dir
    requires_exact_resume_with_prompt = True
    needs_outbound_proxy = True     # Anthropic API unreachable directly (China network)
    unrestricted_flags = "--dangerously-skip-permissions"
    # Non-interactive pipeline mode: -p reads the prompt from stdin, prints, exits.
    # Also skips the workspace-trust dialog that would otherwise hang a fresh
    # worktree. --dangerously-skip-permissions keeps it from stopping mid-run.
    headless_flags = "-p --dangerously-skip-permissions"

    @staticmethod
    def _validated_session_id(native_session_id: str) -> str:
        try:
            return str(uuid.UUID((native_session_id or "").strip()))
        except ValueError as exc:
            raise ValueError("invalid Claude session id") from exc

    def new_native_session_id(self) -> str:
        return str(uuid.uuid4())

    def initial_session_arguments(self, native_session_id: str = "") -> str:
        if not native_session_id:
            return ""
        return f"--session-id {shlex.quote(self._validated_session_id(native_session_id))}"

    def resume_command_suffix(self, native_session_id: str = "") -> str:
        if not native_session_id:
            return self.resume_suffix
        return f"--resume {shlex.quote(self._validated_session_id(native_session_id))}"

    def find_native_session_id(self, working_dir: str, started_at: str) -> str:
        """Match a legacy seat to Claude's cwd-scoped JSONL conversation."""
        try:
            launch_time = datetime.fromisoformat(started_at).timestamp()
        except (TypeError, ValueError):
            return ""
        claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
        expected_cwd = os.path.realpath(working_dir)
        project_dir = claude_home / "projects" / expected_cwd.replace(os.sep, "-")
        matches = []
        for path in project_dir.glob("*.jsonl"):
            try:
                file_id = str(uuid.UUID(path.stem))
            except ValueError:
                continue
            first_event = None
            try:
                with path.open(errors="replace") as handle:
                    for index, line in enumerate(handle):
                        if index >= 64:
                            break
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get("cwd") and event.get("timestamp"):
                            first_event = event
                            break
            except OSError:
                continue
            if not first_event or os.path.realpath(str(first_event["cwd"])) != expected_cwd:
                continue
            try:
                created_at = datetime.fromisoformat(
                    str(first_event["timestamp"]).replace("Z", "+00:00")
                ).timestamp()
                event_id = str(uuid.UUID(str(first_event.get("sessionId") or file_id)))
            except (TypeError, ValueError):
                continue
            if event_id != file_id or not launch_time - 5 <= created_at <= launch_time + 600:
                continue
            matches.append((abs(created_at - launch_time), event_id))
        if not matches:
            return ""
        matches.sort()
        if len(matches) > 1 and matches[0][0] == matches[1][0]:
            return ""
        return matches[0][1]

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
