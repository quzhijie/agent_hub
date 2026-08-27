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
import shlex
import shutil
from pathlib import Path

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

# --- outbound proxy (per-provider) ------------------------------------------
#
# agent_hub runs as a launchd service whose plist exports only PATH+HOME, so the
# HTTPS_PROXY family from ~/.zshrc (sourced ~/.config/proxy.env) never reaches
# codex/claude spawned inside tmux sessions — they end up dialing api.openai.com
# / api.anthropic.com directly and timing out. Providers that must reach their
# API through the outbound proxy set `needs_outbound_proxy = True`; their default
# command is routed through a tiny launcher which sources proxy.env inside the
# pane. This preserves normal shell expansion (for example
# HTTP_PROXY="$HTTPS_PROXY") without putting credentials in tmux's stored
# pane_start_command. DeepSeek-backed variants (ds4, ds4-co) talk to a domestic
# API and explicitly opt out.

_PROXY_LAUNCHER = Path(__file__).with_name("outbound_proxy_launch.sh")


class Provider:
    name = "base"
    default_binary: str | None = None
    model_flag: str | None = None
    model_choices: tuple[str, ...] = ()
    reasoning_effort_choices: tuple[str, ...] = ()
    reasoning_effort_flag: str | None = None
    # Provider-native flag that explicitly disables interactive permission
    # prompts.  It is never part of the default command: a caller must select
    # the typed ``unrestricted`` session mode before the seat is created.
    unrestricted_flags: str | None = None

    # True for agents whose API is unreachable directly (codex/claude) — their
    # default launch command goes through outbound_proxy_launch.sh.
    needs_outbound_proxy: bool = False

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
    def _launch_prefix(self) -> str:
        """Proxy launcher to prepend to the DEFAULT launch command, or ''."""
        return shlex.quote(str(_PROXY_LAUNCHER)) if self.needs_outbound_proxy else ""

    def normalize_model(self, model: str) -> str:
        """Validate one provider-native model identifier, or the default."""
        value = (model or "").strip()
        if not value:
            return ""
        if self.model_flag is None:
            raise ValueError(f"provider {self.name!r} does not support model selection")
        if len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,199}", value):
            raise ValueError("invalid model identifier")
        return value

    def normalize_reasoning_effort(self, reasoning_effort: str) -> str:
        """Validate a provider-native reasoning setting, or use its default."""
        value = (reasoning_effort or "").strip()
        if not value:
            return ""
        if value not in self.reasoning_effort_choices:
            raise ValueError(
                f"provider {self.name!r} does not support reasoning effort {value!r}"
            )
        return value

    def _reasoning_effort_arguments(self, reasoning_effort: str) -> str:
        """Return shell-safe provider argv for one validated effort value."""
        if not self.reasoning_effort_flag:
            raise ValueError(f"provider {self.name!r} does not support reasoning effort")
        return f"{self.reasoning_effort_flag} {shlex.quote(reasoning_effort)}"

    def _apply_reasoning_effort(
        self, command: str, launch_command: str, reasoning_effort: str,
    ) -> str:
        selected = self.normalize_reasoning_effort(reasoning_effort)
        if not selected:
            return command
        if (launch_command or "").strip():
            raise ValueError("reasoning effort cannot be combined with a custom launch command")
        return f"{command} {self._reasoning_effort_arguments(selected)}"

    def resolve_command(
        self, launch_command: str, *, model: str = "", reasoning_effort: str = "",
    ) -> str:
        lc = (launch_command or "").strip()
        if lc:
            if model.strip():
                raise ValueError("model cannot be combined with a custom launch command")
            if reasoning_effort.strip():
                raise ValueError("reasoning effort cannot be combined with a custom launch command")
            return lc
        if self.default_binary:
            cmd = shutil.which(self.default_binary) or self.default_binary
            prefix = self._launch_prefix()
            command = f"{prefix} {cmd}" if prefix else cmd
            selected = self.normalize_model(model)
            if selected:
                command += f" {self.model_flag} {shlex.quote(selected)}"
            return self._apply_reasoning_effort(command, "", reasoning_effort)
        raise ValueError(f"provider {self.name!r} requires an explicit launch command")

    def permission_modes(self) -> tuple[str, ...]:
        """Permission choices this provider can honor without custom shell."""
        return ("default", "unrestricted") if self.unrestricted_flags else ("default",)

    def _apply_permission_mode(
        self, command: str, launch_command: str, permission_mode: str,
    ) -> str:
        if permission_mode == "default":
            return command
        if permission_mode != "unrestricted":
            raise ValueError(f"unknown permission mode: {permission_mode}")
        if (launch_command or "").strip():
            raise ValueError("permission mode cannot be combined with a custom launch command")
        if not self.unrestricted_flags:
            raise ValueError(f"provider {self.name!r} does not support unrestricted permissions")
        return f"{command} {self.unrestricted_flags}"

    # Suffix appended when RE-starting a seat that ran before, so the agent
    # resumes its last conversation instead of starting blank (e.g. claude's
    # "--continue"). Only applied to the DEFAULT command — a user-supplied
    # launch command is never mutated; the user knows their own flags best.
    resume_suffix: str | None = None
    requires_exact_resume_with_prompt = False

    def find_native_session_id(self, working_dir: str, started_at: str) -> str:
        """Best-effort lookup of the provider conversation behind one seat.

        Most providers do not expose a local, queryable conversation index, so
        their default is deliberately empty. Providers that do expose one may
        override this and let Agent Hub pin restarts to the exact conversation
        instead of relying on a process-global "most recent" shortcut.
        """
        return ""

    def new_native_session_id(self) -> str:
        """Allocate a provider conversation id before first launch, if supported."""
        return ""

    def initial_session_arguments(self, native_session_id: str = "") -> str:
        """Provider argv that pins a first launch to ``native_session_id``."""
        return ""

    def resume_command_suffix(self, native_session_id: str = "") -> str | None:
        """Provider argv used to resume one conversation.

        The base implementation preserves the historical provider-wide
        fallback. A provider may use ``native_session_id`` for exact resume.
        """
        return self.resume_suffix

    def resolve_resume_command(
        self, launch_command: str, *, model: str = "", reasoning_effort: str = "",
        permission_mode: str = "default",
        native_session_id: str = "",
    ) -> str:
        lc = (launch_command or "").strip()
        resume_suffix = self.resume_command_suffix(native_session_id)
        if lc or not resume_suffix:
            command = self.resolve_command(lc, model=model, reasoning_effort=reasoning_effort)
            return self._apply_permission_mode(command, lc, permission_mode)
        command = self._apply_permission_mode(
            self.resolve_command("", model=model, reasoning_effort=reasoning_effort), "", permission_mode,
        )
        return f"{command} {resume_suffix}"

    def resolve_resume_with_prompt_command(
        self, launch_command: str, initial_prompt: str, *, model: str = "",
        reasoning_effort: str = "",
        permission_mode: str = "default", native_session_id: str = "",
    ) -> str:
        """Resume a native conversation and submit one bounded context turn.

        This is used only after an explicit archived-seat restore. Custom
        commands have no known resume/prompt argv contract and are refused.
        """
        prompt = (initial_prompt or "").strip()
        if not prompt:
            return self.resolve_resume_command(
                launch_command, model=model, reasoning_effort=reasoning_effort,
                permission_mode=permission_mode,
                native_session_id=native_session_id,
            )
        if (
            (launch_command or "").strip()
            or not self.resume_command_suffix(native_session_id)
        ):
            raise ValueError("this provider cannot resume an old conversation with context")
        return (
            f"{self.resolve_resume_command('', model=model, reasoning_effort=reasoning_effort, permission_mode=permission_mode, native_session_id=native_session_id)} "
            f"{shlex.quote(prompt)}"
        )

    def resolve_initial_command(
        self, launch_command: str, initial_prompt: str, *, model: str = "",
        reasoning_effort: str = "",
        permission_mode: str = "default", native_session_id: str = "",
    ) -> str:
        """Build a first-launch command carrying one inert prompt argument.

        Agent Hub never types into an interactive seat.  A caller that supplies
        an initial prompt therefore gets it as a shell-quoted argv item on the
        provider's normal command.  Custom launch commands are refused: their
        argument contract is unknown, and appending text would silently change
        user-authored shell semantics.
        """
        prompt = (initial_prompt or "").strip()
        lc = (launch_command or "").strip()
        if lc and native_session_id:
            raise ValueError("native session id cannot be combined with a custom launch command")
        if (launch_command or "").strip():
            if prompt:
                raise ValueError("initial_prompt cannot be combined with a custom launch command")
            command = self.resolve_command(
                launch_command, model=model, reasoning_effort=reasoning_effort,
            )
            return self._apply_permission_mode(command, launch_command, permission_mode)
        command = self._apply_permission_mode(
            self.resolve_command("", model=model, reasoning_effort=reasoning_effort),
            "", permission_mode,
        )
        session_arguments = self.initial_session_arguments(native_session_id)
        if session_arguments:
            command = f"{command} {session_arguments}"
        if not prompt:
            return command
        return f"{command} {shlex.quote(prompt)}"

    # Flags that run the agent NON-interactively, reading the prompt from stdin
    # and never prompting for approval — for the pipeline runner, so a step needs
    # no TTY and can't stall on a permission/trust dialog. None if the provider
    # has no headless mode (such a provider can't be used in a pipeline).
    headless_flags: str | None = None

    def resolve_headless_command(self) -> str | None:
        if not self.headless_flags:
            return None
        return f"{self.resolve_command('')} {self.headless_flags}"

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
