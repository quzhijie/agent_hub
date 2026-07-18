"""Thin, defensive wrapper over tmux.

By default every call targets tmux's shared DEFAULT socket (config.TMUX_SOCKET
== ""), so seats created here also appear in your normal `tmux` and in handmux
on your phone. kill/switch stay safe regardless: callers only ever kill sessions
whose names are registered in our DB (all `hub-*`). Set a socket name to
run on a dedicated, isolated server instead (the test suite does this).
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time

from .config import TMUX_SOCKET

_TMUX_BIN = shutil.which("tmux") or "tmux"
# No -L → tmux's default (shared) socket. A socket name → an isolated server.
_BASE = [_TMUX_BIN] + (["-L", TMUX_SOCKET] if TMUX_SOCKET else [])

# tmux target syntax uses '.' (window.pane) and ':' (window) as separators, and
# whitespace breaks exact matching — so restrict names to a safe charset.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class TmuxError(RuntimeError):
    pass


def validate_name(name: str) -> str:
    if not name or not _NAME_RE.match(name):
        raise TmuxError(f"unsafe tmux session name: {name!r}")
    return name


_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def slug(text: str, max_len: int = 12) -> str:
    """Reduce free-form text to the tmux-safe charset; may return ''."""
    return _SLUG_RE.sub("-", text.strip()).strip("-")[:max_len].strip("-")


def make_session_name(project_name: str, seat_name: str, session_id: str,
                      id_len: int = 4) -> str:
    """Human-readable session name — this is what handmux/tmux lists show.

    hub-<project>-<seat>-<id4>: the names carry the meaning, the short id
    guarantees uniqueness. Parts that slug away to nothing (e.g. pure CJK
    names) are dropped, worst case leaving hub-<id>.
    """
    parts = ["hub", slug(project_name), slug(seat_name), session_id[:id_len]]
    return "-".join(p for p in parts if p)


def _run(args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        _BASE + args, capture_output=True, text=True, timeout=timeout
    )


def available() -> bool:
    return shutil.which("tmux") is not None


def has_session(name: str) -> bool:
    validate_name(name)
    return _run(["has-session", "-t", f"={name}"]).returncode == 0


def new_session(name: str, working_dir: str, command: str,
               width: int = 220, height: int = 50) -> None:
    validate_name(name)
    # Two-step start: create the window on a placeholder, configure it, THEN
    # swap in the real command. Setting options after launching the real
    # command directly would race an instantly-crashing command (session gone
    # before remain-on-exit lands) and lose its dying output.
    r = _run([
        "new-session", "-d", "-s", name,
        "-x", str(width), "-y", str(height),
        "-c", working_dir, "sleep 30",
    ])
    if r.returncode != 0:
        raise TmuxError(f"new-session failed: {r.stderr.strip() or r.stdout.strip()}")
    # Keep this seat's window at the LARGEST attached client's size, scoped to
    # just this window so we never change your normal tmux. When the desktop
    # viewer and a mobile client (handmux) attach to the same seat at once, the
    # pane width won't collapse to the phone's — so capture-pane (status
    # detection) and the desktop view stay wide. Best-effort; ignored on tmux
    # too old to know the option.
    _run(["set-option", "-w", "-t", f"={name}:", "window-size", "largest"])
    # Keep the pane on screen after the agent exits (crash included) so its
    # dying output stays capturable — otherwise an instant crash (e.g. command
    # not found) evaporates without a trace. The sampler detects the dead pane
    # and records the last frame; restart kills the corpse first.
    _run(["set-option", "-w", "-t", f"={name}:", "remain-on-exit", "on"])
    # When THIS seat is removed we kill its session. If the viewer client happens
    # to be looking at it, tmux's default (detach-on-destroy on) would DETACH the
    # viewer — the terminal drops back to a shell and "取景器没了". Scope the
    # override to this session (not -g) so we never touch your normal tmux: when
    # a viewed seat dies, the viewer switches to the most-recently-active
    # remaining seat instead of detaching. Session-scoped (no -w), and the target
    # needs the trailing ':' — set-option rejects the bare '=name' exact form that
    # has-session/kill-session accept, same as the window options just above.
    _run(["set-option", "-t", f"={name}:", "detach-on-destroy", "off"])
    # Make the OUTER terminal's title track the seat you're viewing. tmux
    # defaults to set-titles off — so it never touches the title and the window
    # keeps showing whatever ran the `tmux attach` (you can't tell which seat is
    # on screen). Turning it on makes tmux retitle to the CURRENT session's name
    # as you switch seats, so the title bar always names the seat in front of
    # you. Both are session options (no -w), scoped to this seat like
    # detach-on-destroy above, so your normal tmux is left with its default off.
    # "#S" is the session name (hub-<project>-<seat>-<id>) — the same label
    # tmux/handmux list, so the vocabulary stays consistent.
    _run(["set-option", "-t", f"={name}:", "set-titles", "on"])
    _run(["set-option", "-t", f"={name}:", "set-titles-string", "#S"])
    r = _run(["respawn-pane", "-k", "-t", f"={name}:", "-c", working_dir, command])
    if r.returncode != 0:
        kill_session(name)
        raise TmuxError(f"respawn failed: {r.stderr.strip() or r.stdout.strip()}")


def _pane_pids(name: str) -> list[int]:
    """PIDs of the leader process in each of the session's panes (usually one).

    This is the root of everything the seat spawned — the shell tmux exec'd,
    under which the agent (hermes/codex/claude) and its whole subtree live.
    """
    r = _run(["list-panes", "-t", f"={name}:", "-F", "#{pane_pid}"])
    if r.returncode != 0:
        return []
    pids = []
    for tok in r.stdout.split():
        try:
            pids.append(int(tok))
        except ValueError:
            pass
    return pids


def _process_tree(roots: list[int]) -> list[int]:
    """Every PID in the process trees rooted at `roots` (roots included),
    ordered children-first so a caller can signal leaves before their parents.

    Built from a single `ps` snapshot so it's consistent and cheap. Must be
    called while the roots are still alive: a child that has setsid()'d keeps
    its parent link until that parent dies, so it's captured here — but once
    the session is torn down and the parent exits, the child reparents to init
    and this walk would no longer reach it. Snapshot first, then kill.
    """
    if not roots:
        return []
    r = subprocess.run(
        ["ps", "-Ao", "pid=,ppid="], capture_output=True, text=True, timeout=10
    )
    children: dict[int, list[int]] = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)

    ordered: list[int] = []
    seen: set[int] = set()

    def walk(pid: int) -> None:
        for child in children.get(pid, ()):  # descend first
            if child not in seen:
                seen.add(child)
                walk(child)
        ordered.append(pid)  # post-order → children precede their parent

    for root in roots:
        if root not in seen:
            seen.add(root)
            walk(root)
    return ordered


def _reap(pids: list[int]) -> None:
    """SIGTERM, brief grace, then SIGKILL a set of PIDs (child-first ordered).

    Best-effort: a PID that already died just raises ESRCH and is skipped.
    Never touches PID<=1 or our own backend process — a pane tree never
    contains the server, but we guard anyway.
    """
    me = os.getpid()
    targets = [p for p in pids if p > 1 and p != me]
    if not targets:
        return
    for sig in (signal.SIGTERM, None, signal.SIGKILL):
        if sig is None:
            time.sleep(0.3)  # let well-behaved processes exit on TERM
            continue
        for p in targets:
            try:
                os.kill(p, sig)
            except (ProcessLookupError, PermissionError):
                pass


def kill_session(name: str) -> None:
    """Destroy the seat's tmux session AND reap the process tree under it.

    `kill-session` alone only SIGHUPs the pane's foreground process group, so
    any child that called setsid()/double-forked (codex, hermes and claude all
    detach subprocesses this way) escapes the signal and is orphaned — a single
    such orphan pegged a CPU for four days before we caught it. So we snapshot
    the pane's whole descendant tree while the session (and thus every parent
    link) is still intact, tear the session down, then hard-reap any snapshot
    survivors ourselves.
    """
    validate_name(name)
    tree = _process_tree(_pane_pids(name))
    _run(["kill-session", "-t", f"={name}"])
    _reap(tree)


def send_text(name: str, text: str, submit: bool = True) -> None:
    """Type `text` into a pane and (by default) press Enter to submit.

    This is the ONE function in the app that writes INTO a terminal — everything
    else is read-only. It is only ever reached through orchestrator._send, which
    enforces the pipeline-membership allowlist first. Never call it directly with
    a caller-supplied target.

    Uses a named paste buffer + bracketed paste (`-p`) rather than `send-keys
    <text>`: send-keys would interpret words like "Enter"/"C-c" as key names and
    mangle newlines, whereas a bracketed paste is delivered to the TUI as literal
    input. A short gap before Enter lets the TUI settle so the submit registers.
    """
    validate_name(name)
    if not text:
        return
    r = _run(["set-buffer", "-b", "ah-send", "--", text])
    if r.returncode != 0:
        raise TmuxError(f"set-buffer failed: {r.stderr.strip() or r.stdout.strip()}")
    r = _run(["paste-buffer", "-t", f"={name}:", "-b", "ah-send", "-p", "-d"])
    if r.returncode != 0:
        raise TmuxError(f"paste-buffer failed: {r.stderr.strip() or r.stdout.strip()}")
    if submit:
        time.sleep(0.2)
        _run(["send-keys", "-t", f"={name}:", "Enter"])


def rename_session(old: str, new: str) -> bool:
    """Rename a LIVE session in place — pure relabel, the process isn't touched."""
    validate_name(old)
    validate_name(new)
    return _run(["rename-session", "-t", f"={old}", new]).returncode == 0


def pane_dead(name: str) -> bool:
    """True if the seat's pane exited but remain-on-exit kept it on screen."""
    validate_name(name)
    r = _run(["list-panes", "-t", f"={name}:", "-F", "#{pane_dead}"])
    return r.returncode == 0 and "1" in r.stdout.split()


def capture_pane(name: str, lines: int = 60, with_history: bool = False) -> str:
    validate_name(name)
    # capture-pane takes a PANE target: the "=name:" form keeps the exact
    # session match ("=name" alone is only valid for session targets).
    # with_history reaches back into scrollback — needed for dead panes, where
    # tmux scrolls the dying output out of the visible screen before stamping
    # the "Pane is dead" banner.
    args = ["capture-pane", "-p", "-t", f"={name}:"]
    if with_history:
        args += ["-S", f"-{lines}"]
    r = _run(args)
    if r.returncode != 0:
        return ""
    text = r.stdout
    return "\n".join(text.split("\n")[-lines:])


def list_clients() -> list[str]:
    r = _run(["list-clients", "-F", "#{client_name}"])
    if r.returncode != 0:
        return []
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def viewer_client() -> tuple[str, str] | None:
    """(client_name, client_tty) to point at 'jump' — the widest attached one.

    With only the desktop `attach`, that's it. When a mobile client (handmux)
    is also attached, the desktop terminal is almost always the wider one, so
    the desktop 'jump' button keeps driving the desktop, not the phone. The
    tty lets the backend raise the exact terminal tab hosting the viewer.
    """
    r = _run(["list-clients", "-F", "#{client_width}\t#{client_name}\t#{client_tty}"])
    if r.returncode != 0:
        return None
    best, best_w = None, -1
    for ln in r.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) < 3 or not parts[1].strip():
            continue
        try:
            w = int(parts[0])
        except ValueError:
            w = 0
        if w > best_w:
            best, best_w = (parts[1], parts[2]), w
    return best


def viewer_focus_session() -> str | None:
    """The tmux session the viewer (widest attached client) is CURRENTLY showing.

    Read-only. The sampler polls this each cycle to implement view-acknowledge:
    a 等待输入/已完成 seat clears to 空闲 once you've had its session in front of
    you and then switched the viewer away. We track the widest client (the same
    one `jump`/`viewer_client` drive) so a phone (handmux) parked elsewhere never
    counts as "you looked at it"."""
    r = _run(["list-clients", "-F", "#{client_width}\t#{client_session}"])
    if r.returncode != 0:
        return None
    best, best_w = None, -1
    for ln in r.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) < 2 or not parts[1].strip():
            continue
        try:
            w = int(parts[0])
        except ValueError:
            w = 0
        if w > best_w:
            best, best_w = parts[1].strip(), w
    return best


def switch_client(client: str, name: str) -> bool:
    validate_name(name)
    r = _run(["switch-client", "-c", client, "-t", f"={name}"])
    return r.returncode == 0


def attach_command(name: str) -> str:
    validate_name(name)
    # Single-quote the target: in zsh a bare leading '=' triggers EQUALS
    # expansion (`=foo` → path of command `foo`), so `-t =name` pasted into a
    # zsh prompt fails with "name not found". Quoting keeps '=' literal, and the
    # name is already restricted to a shell-safe charset by validate_name.
    socket = f"-L {TMUX_SOCKET} " if TMUX_SOCKET else ""
    return f"tmux {socket}attach -t '={name}'"
