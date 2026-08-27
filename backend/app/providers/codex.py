"""Codex CLI provider rules. First-pass heuristics — refine with real samples."""
from __future__ import annotations

import os
import re
import shlex
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .base import Provider


class CodexProvider(Provider):
    name = "codex"
    default_binary = "codex"
    model_flag = "--model"
    model_choices = (
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4",
    )
    reasoning_effort_choices = ("low", "medium", "high", "xhigh", "max")
    resume_suffix = "resume --last"   # resume the most recent recorded session
    requires_exact_resume_with_prompt = True
    needs_outbound_proxy = True        # OpenAI API unreachable directly (China network)
    unrestricted_flags = "--dangerously-bypass-approvals-and-sandbox"
    # Non-interactive pipeline mode: `codex exec` reads the prompt from stdin and
    # runs to completion; the bypass flag skips every approval + the sandbox.
    headless_flags = "exec --dangerously-bypass-approvals-and-sandbox"

    def _reasoning_effort_arguments(self, reasoning_effort: str) -> str:
        # Codex exposes this setting through its TOML-compatible `--config`
        # override rather than a dedicated CLI switch. Quoting the whole
        # assignment preserves the inner TOML string through the shell.
        setting = f'model_reasoning_effort="{reasoning_effort}"'
        return f"--config {shlex.quote(setting)}"

    def resolve_resume_with_prompt_command(
        self, launch_command: str, initial_prompt: str, *, model: str = "",
        reasoning_effort: str = "",
        permission_mode: str = "default", native_session_id: str = "",
    ) -> str:
        if (initial_prompt or "").strip() and not (native_session_id or "").strip():
            raise ValueError(
                "could not identify this seat's exact Codex conversation; "
                "restore it as a fresh conversation instead"
            )
        return super().resolve_resume_with_prompt_command(
            launch_command, initial_prompt, model=model, reasoning_effort=reasoning_effort,
            permission_mode=permission_mode,
            native_session_id=native_session_id,
        )

    def resume_command_suffix(self, native_session_id: str = "") -> str:
        """Resume the exact Codex thread when Agent Hub has identified it."""
        value = (native_session_id or "").strip()
        if value:
            try:
                value = str(uuid.UUID(value))
            except ValueError as exc:
                raise ValueError("invalid Codex session id") from exc
            return f"resume {shlex.quote(value)}"
        return self.resume_suffix

    def find_native_session_id(self, working_dir: str, started_at: str) -> str:
        """Match an Agent Hub first launch to Codex's local thread index.

        Codex records each interactive thread with its cwd and creation epoch.
        Agent Hub records the same launch to second precision. A unique nearest
        match lets old seats migrate away from ambiguous ``resume --last`` and
        lets new seats persist the native thread id for all later restarts.

        This is intentionally best-effort: Codex owns the schema, so a missing
        database, a future schema change, or an ambiguous simultaneous launch
        returns no id and leaves the established fallback untouched.
        """
        try:
            launch_time = datetime.fromisoformat(started_at).timestamp()
        except (TypeError, ValueError):
            return ""
        codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        candidates = []
        for path in codex_home.glob("state_*.sqlite"):
            match = re.fullmatch(r"state_(\d+)\.sqlite", path.name)
            if match:
                candidates.append((int(match.group(1)), path))
        if not candidates:
            return ""
        state_db = max(candidates)[1]
        try:
            connection = sqlite3.connect(
                f"{state_db.resolve().as_uri()}?mode=ro", uri=True,
            )
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT id, cwd, created_at
                   FROM threads
                   WHERE created_at BETWEEN ? AND ?""",
                # Codex can postpone creating the thread until a trust/login
                # screen is cleared. Allow that forward delay, but admit only
                # a few seconds before Agent Hub's launch timestamp so an older
                # conversation in the same cwd cannot win by proximity.
                (int(launch_time) - 5, int(launch_time) + 600),
            ).fetchall()
            connection.close()
        except (OSError, sqlite3.Error):
            return ""
        expected_cwd = os.path.realpath(working_dir)
        matches = []
        for row in rows:
            if os.path.realpath(str(row["cwd"])) != expected_cwd:
                continue
            try:
                native_id = str(uuid.UUID(str(row["id"])))
            except ValueError:
                continue
            matches.append((abs(float(row["created_at"]) - launch_time), native_id))
        if not matches:
            return ""
        matches.sort()
        if len(matches) > 1 and matches[0][0] == matches[1][0]:
            return ""
        return matches[0][1]

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
