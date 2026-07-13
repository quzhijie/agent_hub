"""Jump-to-terminal: switch the user's single viewer terminal to a seat.

This is a read-only action — it never sends keystrokes to the target session.
It only asks tmux to point an existing client at the seat's session.
"""
from __future__ import annotations

from . import focus, store, tmux


def jump_to(sess: dict) -> dict:
    name = sess["tmux_session"]
    tmux.validate_name(name)

    if not store.is_registered_tmux_name(name):
        return {"ok": False, "reason": "not a registered active seat"}

    if not tmux.has_session(name):
        return {"ok": False, "reason": "tmux session is gone (exited)"}

    client = tmux.viewer_client()
    if client:
        client_name, client_tty = client
        ok = tmux.switch_client(client_name, name)
        if ok:
            # Raise the exact terminal window/tab hosting the viewer so the
            # user lands there directly instead of hunting for it.
            focused = focus.focus_terminal_by_tty(client_tty)
            return {"ok": True, "jumped": True, "client": client_name,
                    "focused": focused, "session": name}
        return {"ok": False, "reason": "switch-client failed",
                "attach_command": tmux.attach_command(name)}

    # No viewer terminal attached yet — tell the user how to open one.
    return {
        "ok": True,
        "jumped": False,
        "session": name,
        "attach_command": tmux.attach_command(name),
        "hint": "No viewer terminal is attached. Run this in a terminal, then jump again.",
    }
