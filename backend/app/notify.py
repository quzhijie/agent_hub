"""Best-effort macOS notifications. Never raises, never blocks the sampler.

Prefers terminal-notifier (clicking the banner opens the dashboard URL);
falls back to osascript, whose notifications cannot carry a click action —
clicking those just opens Script Editor showing our own one-liner, which
looks alarming but is harmless.
"""
from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("agent_hub.notify")

_TN = shutil.which("terminal-notifier")
_OSASCRIPT = shutil.which("osascript")
# Click handler: focuses an EXISTING dashboard tab in Chrome, opens one only
# when none is there — plain -open would spawn a new tab on every click.
_FOCUS_SCRIPT = Path(__file__).resolve().parent / "dashboard_focus.applescript"


def _click_action(open_url: str) -> list[str]:
    if _OSASCRIPT and _FOCUS_SCRIPT.exists():
        port = str(urlsplit(open_url).port or 80)
        cmd = (f"{_OSASCRIPT} {shlex.quote(str(_FOCUS_SCRIPT))} "
               f"{shlex.quote(open_url)} {shlex.quote(port)}")
        return ["-execute", cmd]
    return ["-open", open_url]


def notify(title: str, message: str, open_url: str | None = None) -> None:
    try:
        if _TN:
            args = [_TN, "-title", title, "-message", message,
                    "-sound", "Glass", "-group", "agent-hub"]
            if open_url:
                args += _click_action(open_url)
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if not _OSASCRIPT:
            return
        # osascript takes the script as one string: strip characters that could
        # escape the AppleScript string literal (names come from user input).
        def q(s: str) -> str:
            return s.replace("\\", "").replace('"', "'")

        script = f'display notification "{q(message)}" with title "{q(title)}" sound name "Glass"'
        subprocess.Popen([_OSASCRIPT, "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:  # pragma: no cover - defensive
        log.debug("notification failed", exc_info=True)
