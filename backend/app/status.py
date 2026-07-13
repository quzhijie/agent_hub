"""Status detection + the read-only sampler loop.

The sampler NEVER writes to a terminal. Its only tmux interaction is
`has-session` and `capture-pane` (both read-only).
"""
from __future__ import annotations

import asyncio
import logging

from . import notify as notify_mod
from . import store, tmux
from .providers.base import Provider
from .providers.registry import get_provider
from .textutil import clean_frame

log = logging.getLogger("agent_hub.status")


def compute_status(provider: Provider, prev_frame: str | None, curr_frame: str) -> tuple[str, bool]:
    """Return (status, changed).

    Precedence, in order:
      1. explicit waiting prompt (question / permission)  -> waiting
      2. generating indicator (spinner / "esc to interrupt") -> active
      3. provider-SPECIFIC idle marker visible            -> idle
         (beats "frame changed": idle TUIs rotate tips/placeholder suggestions,
          which repaints the screen without meaning any work — treating that as
          active caused spurious active→idle flaps and "已完成" notifications)
      4. frame changed since last sample                  -> active
      5. stable and a generic idle prompt is visible      -> idle
      6. otherwise                                          -> unknown  (never guess idle)

    A truly working agent never lands on rule 3: its working indicators
    (spinner, "esc to interrupt", status verbs) are caught by rule 2 first.
    """
    changed = curr_frame.strip() != (prev_frame or "").strip()
    if provider.is_waiting(curr_frame):
        return store.WAITING, changed
    if provider.is_generating(curr_frame):
        return store.ACTIVE, changed
    if provider.is_idle_prompt_specific(curr_frame):
        return store.IDLE, changed
    if changed:
        return store.ACTIVE, changed
    if provider.is_idle_prompt(curr_frame):
        return store.IDLE, changed
    return store.UNKNOWN, changed


class StatusSampler:
    def __init__(self, interval: float = 3.0, capture_lines: int = 60,
                 notify_enabled: bool = False, notify_url: str | None = None):
        self.interval = interval
        self.capture_lines = capture_lines
        self.notify_enabled = notify_enabled
        self.notify_url = notify_url            # clicking a banner opens this
        self._active_streak: dict[str, int] = {}  # sid -> consecutive ACTIVE samples
        self._frames: dict[str, str] = {}   # session_id -> last cleaned frame
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.sample_once)
            except Exception:  # pragma: no cover - defensive
                log.exception("status sample failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def sample_once(self) -> None:
        for sess in store.list_live_sessions():
            self._sample_session(sess)

    def _sample_session(self, sess: dict) -> None:
        sid = sess["id"]
        name = sess["tmux_session"]
        if not tmux.has_session(name):
            if sess["status"] != store.EXITED:
                store.update_status(sid, store.EXITED, sess.get("last_output", ""), activity=False)
            self._frames.pop(sid, None)
            self._active_streak.pop(sid, None)
            return

        provider = get_provider(sess["provider"]) if sess["provider"] in {
            "hermes", "claude", "codex", "custom"
        } else get_provider("custom")

        # Agent exited but remain-on-exit kept the pane: record its dying
        # output (e.g. "command not found") so the card shows WHY it died.
        if tmux.pane_dead(name):
            if sess["status"] != store.EXITED:
                raw = tmux.capture_pane(name, self.capture_lines, with_history=True)
                last = provider.extract_last_message(clean_frame(raw))
                store.update_status(sid, store.EXITED,
                                    last or sess.get("last_output", ""), activity=False)
            self._frames.pop(sid, None)
            self._active_streak.pop(sid, None)
            return

        raw = tmux.capture_pane(name, self.capture_lines)
        curr = clean_frame(raw)
        prev = self._frames.get(sid)
        status, changed = compute_status(provider, prev, curr)
        last_output = provider.extract_last_message(curr)
        store.update_status(sid, status, last_output, activity=changed)
        self._frames[sid] = curr

        # Edge-triggered on transitions (sess["status"] is the previous stored
        # value, so each fires once per transition, not every sample):
        #  - seat starts WAITING            -> "等待输入"
        #  - seat finishes working (ACTIVE→IDLE) -> "已完成"; the streak guard
        #    (≥2 consecutive active samples) filters TUI redraw flicker.
        # The transition is recorded to the DB push trail regardless of
        # notify_enabled (so the dashboard's recent-pushes strip traces it even
        # with OS banners off); only the desktop banner itself is gated.
        old = sess["status"]
        kind = None
        if status == store.WAITING and old != store.WAITING:
            kind = "waiting"
        elif (status == store.IDLE and old == store.ACTIVE
              and self._active_streak.get(sid, 0) >= 2):
            kind = "done"
        if kind:
            store.record_notification(sid, old, status, kind)
            if self.notify_enabled:
                proj = store.get_project(sess["project_id"])
                where = f"{proj['name']} / {sess['name']}" if proj else sess["name"]
                tail = "等待输入" if kind == "waiting" else "已完成,回到空闲"
                notify_mod.notify("Agent Hub", f"{where} {tail}", self.notify_url)
        self._active_streak[sid] = self._active_streak.get(sid, 0) + 1 \
            if status == store.ACTIVE else 0


def reconcile_on_startup() -> None:
    """After a service restart: mark gone sessions exited, and live-rename any
    session still carrying an old hash-style name to the readable scheme
    (rename-session is a pure relabel — the running agent isn't touched)."""
    for sess in store.list_live_sessions():
        name = sess["tmux_session"]
        try:
            alive = tmux.has_session(name)
        except tmux.TmuxError:
            alive = False
        if not alive:
            if sess["status"] != store.EXITED:
                store.update_status(sess["id"], store.EXITED, sess.get("last_output", ""), activity=False)
            continue
        proj = store.get_project(sess["project_id"])
        desired = tmux.make_session_name(proj["name"] if proj else "", sess["name"], sess["id"])
        if (desired != name and not store.tmux_name_exists(desired)
                and not tmux.has_session(desired) and tmux.rename_session(name, desired)):
            store.update_tmux_session(sess["id"], desired)
            log.info("renamed tmux session %s -> %s", name, desired)
