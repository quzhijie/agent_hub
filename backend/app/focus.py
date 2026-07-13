"""Bring the terminal window hosting a given tty to the front (macOS).

Used by jump: tmux tells us which tty the viewer client sits on; Terminal.app /
iTerm2 expose the tty of every tab via AppleScript, so we can select exactly
that tab and raise its window. Pure window management — never types anything.

First use triggers macOS's one-time Automation permission prompt
("python wants to control Terminal"); if denied we just return False and the
web falls back to telling the user the switch happened without the raise.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess

log = logging.getLogger("agent_hub.focus")

_OSASCRIPT = shutil.which("osascript")
_TTY_RE = re.compile(r"^/dev/[A-Za-z0-9]+$")

# Guarded by "is running" so we never LAUNCH a terminal app that isn't open.
_SCRIPT = '''
on run argv
  set target to item 1 of argv
  if application "Terminal" is running then
    tell application "Terminal"
      repeat with w in windows
        repeat with t in tabs of w
          if (tty of t) is target then
            set selected of t to true
            set index of w to 1
            activate
            return "ok"
          end if
        end repeat
      end repeat
    end tell
  end if
  if application "iTerm2" is running then
    tell application "iTerm2"
      repeat with w in windows
        repeat with t in tabs of w
          repeat with s in sessions of t
            if (tty of s) is target then
              select s
              select t
              select w
              activate
              return "ok"
            end if
          end repeat
        end repeat
      end repeat
    end tell
  end if
  return "no"
end run
'''


def focus_terminal_by_tty(tty: str | None) -> bool:
    if not tty or not _OSASCRIPT or not _TTY_RE.match(tty):
        return False
    try:
        r = subprocess.run([_OSASCRIPT, "-", tty], input=_SCRIPT,
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and r.stdout.strip() == "ok"
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - defensive
        log.debug("focus failed", exc_info=True)
        return False
