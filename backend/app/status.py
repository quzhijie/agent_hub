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
      1. footer_state == 'active': the LOWEST status line is a live-work marker
         -> active  (position-aware: beats a "1." menu / "proceed?" in streamed
          output, and the empty input box drawn mid-run.)
      2. explicit waiting prompt (question / permission)  -> waiting
      3. footer_state == 'idle': the LOWEST status line is a turn-finished marker
         ("Cogitated for 9m 48s" / end-of-session survey) -> idle.  This beats
         the STALE 'Running…'/'<Verb>…' lines still sitting ABOVE it in the
         captured window (which used to make a just-finished seat read 工作中).
      4. STRONG generating signal, whole-window (for providers with no per-line
         footer markers, e.g. hermes: spinner / "esc to interrupt")  -> active
      5. WEAK generating indicator (bare verb words)      -> active
      6. provider-SPECIFIC idle marker visible            -> idle
         (beats "frame changed": idle TUIs rotate tips and repaint on /clear —
          a change that doesn't mean work.)
      7. frame changed since last sample                  -> active
      8. stable and a generic idle prompt is visible      -> idle
      9. otherwise                                          -> unknown

    The key idea: only the most-recent (lowest) status line reflects the current
    state. A working agent's lowest marker is a live "<Verb>… (读秒)"; a finished
    agent's is a past-tense "…ed for <time>" — even though both frames also carry
    older working lines higher up.
    """
    changed = curr_frame.strip() != (prev_frame or "").strip()
    # Position-aware first: the LOWEST status line reflects the current state, so
    # a finished footer beats stale 'Running…'/'<Verb>…' lines still in the window.
    footer = provider.footer_state(curr_frame)
    if footer == "active":
        return store.ACTIVE, changed
    if provider.is_waiting(curr_frame):
        return store.WAITING, changed
    if footer == "idle":
        return store.IDLE, changed
    if provider.is_generating_strong(curr_frame):
        return store.ACTIVE, changed
    if provider.is_generating(curr_frame):
        return store.ACTIVE, changed
    if provider.is_idle_prompt_specific(curr_frame):
        return store.IDLE, changed
    if changed:
        return store.ACTIVE, changed
    if provider.is_idle_prompt(curr_frame):
        return store.IDLE, changed
    return store.UNKNOWN, changed


# --- the sticky state machine ------------------------------------------------
# compute_status above is a per-FRAME classifier: given one screen it guesses
# active/waiting/idle/unknown. That guess jitters (a tool-result gap paints a
# markerless frame; streamed output flashes a '?' or a '1.' menu). Believing
# every frame is what made 工作中 flap to 等待输入/已完成. So the sampler never
# stores the raw guess directly — it feeds it through next_status(), which holds
# a per-seat memory and only moves on CONFIRMED, meaningful transitions.

WORK_CONFIRM = 2     # consecutive raw-active reads before we trust "really working"
SETTLE_CONFIRM = 2   # consecutive settled reads before 工作中 -> 已完成/空闲
WAIT_CONFIRM = 2     # consecutive waiting reads before entering 等待输入
STUCK_LIMIT = 20     # 工作中 held over this many STABLE unreadable frames -> 未知


def new_seat_state() -> dict:
    """Per-seat debounce memory carried across samples by the sampler."""
    return {"active_run": 0, "wait_run": 0, "settle_run": 0,
            "worked": False, "stuck_run": 0, "acked": False}


def _reset_episode(st: dict) -> None:
    """A turn ended (settled / entered waiting): forget the run-length counters
    and the 'really worked' flag so the NEXT turn has to earn them again."""
    st["worked"] = False
    st["active_run"] = st["wait_run"] = st["settle_run"] = st["stuck_run"] = 0


def _hold(prev: str) -> str:
    """While a candidate transition is still debouncing, keep the seat's current
    meaning rather than jump to the not-yet-confirmed target."""
    return prev if prev in (store.ACTIVE, store.WAITING, store.DONE, store.IDLE) else store.UNKNOWN


def next_status(prev: str, raw: str, changed: bool, st: dict) -> tuple[str, str | None]:
    """Sticky transition. Return (new_status, push_kind).

    `prev` is the last STORED status; `raw` is compute_status()'s verdict for THIS
    frame; `st` is this seat's mutable memory (new_seat_state). push_kind is
    'waiting' / 'done' / None — set only on the edge that should notify.

    Invariants (the whole point of this module):
      * 工作中 never falls to 空闲/等待输入 on a single jittery frame. Leaving it
        needs SETTLE_CONFIRM (→已完成/空闲) or WAIT_CONFIRM (→等待输入) consecutive
        agreeing reads; an unreadable frame keeps it 工作中.
      * 等待输入 and 已完成 LATCH: settled or unreadable frames leave them
        unchanged. Only genuine new work (raw active) or a view-acknowledge (done
        by the sampler, not here) moves a seat out of them.
      * 工作中 -> 已完成 only if the seat genuinely worked (`worked`); a seat that
        merely booted to its input box settles to 空闲 and never pushes.
    """
    st["active_run"] = st["active_run"] + 1 if raw == store.ACTIVE else 0
    st["wait_run"] = st["wait_run"] + 1 if raw == store.WAITING else 0
    st["settle_run"] = st["settle_run"] + 1 if raw == store.IDLE else 0
    if st["active_run"] >= WORK_CONFIRM:
        st["worked"] = True

    # Already acknowledged this screen (you looked and left): stay quiet on the
    # SAME still-present prompt/finished frame — re-alerting the thing you just
    # dismissed is the annoyance the view-ack was meant to end. Only genuine new
    # work (raw active) re-arms alerts, and it clears the flag just below.
    if st.get("acked") and raw != store.ACTIVE:
        return store.IDLE, None

    # 1) generating right now — always wins, resumes 工作中 from any state.
    if raw == store.ACTIVE:
        st["acked"] = False
        st["stuck_run"] = 0
        return store.ACTIVE, None

    # 2) a question / permission prompt — enter 等待输入 once confirmed.
    if raw == store.WAITING:
        st["stuck_run"] = 0
        if prev == store.WAITING:
            return store.WAITING, None
        if st["wait_run"] >= WAIT_CONFIRM:
            _reset_episode(st)
            return store.WAITING, "waiting"
        return _hold(prev), None

    # 3) a settled / finished-looking frame.
    if raw == store.IDLE:
        st["stuck_run"] = 0
        if prev == store.ACTIVE:
            if st["settle_run"] < SETTLE_CONFIRM:
                return store.ACTIVE, None          # hold across a one-frame blip
            worked = st["worked"]
            _reset_episode(st)
            return (store.DONE, "done") if worked else (store.IDLE, None)
        if prev in store.ATTENTION:
            return prev, None                       # LATCH 等待输入/已完成
        return store.IDLE, None

    # 4) unreadable frame.
    if prev == store.ACTIVE:
        # A working seat must not silently fall to 空闲 (the spec). Keep it 工作中
        # — unless it has sat on a STABLE unreadable screen for a long time, which
        # a real generating agent never does (it repaints a spinner/timer every
        # frame), so fall through to the 保底 未知.
        if changed:
            st["stuck_run"] = 0
        else:
            st["stuck_run"] += 1
            if st["stuck_run"] >= STUCK_LIMIT:
                _reset_episode(st)
                return store.UNKNOWN, None
        return store.ACTIVE, None
    if prev in store.ATTENTION:
        return prev, None                           # LATCH
    if prev == store.IDLE:
        return store.IDLE, None
    return store.UNKNOWN, None


class StatusSampler:
    def __init__(self, interval: float = 3.0, capture_lines: int = 60,
                 notify_enabled: bool = False, notify_url: str | None = None,
                 on_cycle=None):
        self.interval = interval
        self.capture_lines = capture_lines
        self.notify_enabled = notify_enabled
        self.notify_url = notify_url            # clicking a banner opens this
        self.on_cycle = on_cycle                # ran after each sample (orchestrator tick)
        self._seat: dict[str, dict] = {}    # sid -> per-seat debounce memory (new_seat_state)
        self._viewed: set[str] = set()      # sids you've had in the viewer this attention episode
        self._focused: str | None = None    # tmux session the viewer is showing this cycle
        self._frames: dict[str, str] = {}   # session_id -> last cleaned frame
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def mark_viewed(self, sid: str) -> None:
        """Record that you've looked at this seat (called by the jump route). The
        seat's 等待输入/已完成 then clears to 空闲 as soon as the sampler sees the
        viewer move off it — even if the glance was shorter than one sample."""
        self._viewed.add(sid)

    def _forget(self, sid: str) -> None:
        self._frames.pop(sid, None)
        self._seat.pop(sid, None)
        self._viewed.discard(sid)

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
            if self.on_cycle is not None:
                # runs after statuses are fresh; blocking (tmux/db) so off-thread
                try:
                    await asyncio.to_thread(self.on_cycle)
                except Exception:  # pragma: no cover - defensive
                    log.exception("on_cycle (orchestrator tick) failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def sample_once(self) -> None:
        # One read of "where is the viewer looking" per cycle, shared by every
        # seat's view-acknowledge check below.
        try:
            self._focused = tmux.viewer_focus_session()
        except Exception:  # pragma: no cover - defensive
            self._focused = None
        for sess in store.list_live_sessions():
            self._sample_session(sess)

    def _sample_session(self, sess: dict) -> None:
        sid = sess["id"]
        name = sess["tmux_session"]
        if not tmux.has_session(name):
            if sess["status"] != store.EXITED:
                store.update_status(sid, store.EXITED, sess.get("last_output", ""), activity=False)
            self._forget(sid)
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
            self._forget(sid)
            return

        raw = tmux.capture_pane(name, self.capture_lines)
        curr = clean_frame(raw)
        prev = self._frames.get(sid)
        # A restart clears _frames, so the first sample has no baseline. Don't let
        # a None->frame diff read as "changed" — that phantom change flips
        # markerless providers (hermes) to active for one cycle, which the idle
        # debounce would then turn into a spurious 已完成. No baseline => no change.
        baseline = curr if prev is None else prev
        raw_status, changed = compute_status(provider, baseline, curr)
        # Feed the raw per-frame verdict through the sticky machine: 工作中 holds
        # across blips, 等待输入/已完成 latch, and only confirmed edges push.
        old = sess["status"]
        st = self._seat.setdefault(sid, new_seat_state())
        status, kind = next_status(old, raw_status, changed, st)

        # View-acknowledge: an attention state (等待输入/已完成) clears to 空闲 only
        # after you DELIBERATELY went to look — you hit "跳到终端" (jump ->
        # mark_viewed) and then moved the viewer off the seat. Being merely PARKED
        # on a seat you never opened does NOT count (that was the bug: a finished
        # seat you hadn't looked at cleared the instant the viewer drifted away).
        # So jump is the only thing that arms this; leaving is what fires it.
        focused = (self._focused == name)
        if status in store.ATTENTION and sid in self._viewed and not focused:
            status, kind = store.IDLE, None
            self._viewed.discard(sid)
            _reset_episode(st)
            st["acked"] = True   # keep quiet on this same screen until real work resumes
        elif status not in store.ATTENTION and not focused:
            self._viewed.discard(sid)   # episode over — a future attention state re-arms

        last_output = provider.extract_last_message(curr)
        store.update_status(sid, status, last_output, activity=changed)
        self._frames[sid] = curr

        # Push once, on the confirmed edge next_status flagged (kind is set only
        # then, and stickiness stops a settled seat re-firing — so no coalesce is
        # needed). Recorded to the DB push trail regardless of notify_enabled (the
        # dashboard strip traces it even with OS banners off); only the desktop
        # banner itself is gated.
        if kind:
            store.record_notification(sid, old, status, kind)
            if self.notify_enabled:
                proj = store.get_project(sess["project_id"])
                where = f"{proj['name']} / {sess['name']}" if proj else sess["name"]
                tail = "等待输入" if kind == "waiting" else "已完成"
                notify_mod.notify("Agent Hub", f"{where} {tail}", self.notify_url)


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
