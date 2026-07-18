"""Status detection logic over synthetic frames.

These validate the decision LOGIC (precedence + diff-driven active), not
real-world TUI accuracy — that needs real captured samples. No tmux involved.
"""
from app import store
from app.providers.registry import get_provider
from app.status import compute_status, new_seat_state, next_status


def test_waiting_beats_change_permission_prompt():
    p = get_provider("claude")
    frame = "editing config.py\nDo you want to proceed?\n❯ 1. Yes\n  2. No"
    status, changed = compute_status(p, "editing config.py", frame)
    assert status == store.WAITING


def test_generic_yes_no_is_waiting():
    p = get_provider("custom")
    status, _ = compute_status(p, None, "Overwrite file? (y/n)")
    assert status == store.WAITING


def test_generating_spinner_is_active():
    p = get_provider("codex")
    status, _ = compute_status(p, "same", "same\n⠹ working…  (esc to interrupt)")
    assert status == store.ACTIVE


def test_changed_frame_is_active_even_without_patterns():
    p = get_provider("hermes")
    status, changed = compute_status(p, "line one", "line one\nline two arrived")
    assert changed is True
    assert status == store.ACTIVE


def test_stable_prompt_is_idle():
    p = get_provider("claude")
    frame = "assistant: all done.\n╭──────────╮\n│ >        │\n╰──────────╯"
    status, changed = compute_status(p, frame, frame)  # identical -> stable
    assert changed is False
    assert status == store.IDLE


def test_stable_unrecognized_is_unknown_not_idle():
    p = get_provider("hermes")
    frame = "some opaque final output with no known prompt marker"
    status, _ = compute_status(p, frame, frame)
    assert status == store.UNKNOWN


def test_detection_is_pure_no_tmux_calls(monkeypatch):
    # If detection ever shelled out to tmux, this would blow up.
    import app.tmux as tmux_mod

    def boom(*a, **k):
        raise AssertionError("status detection must not touch tmux")

    monkeypatch.setattr(tmux_mod, "_run", boom)
    p = get_provider("claude")
    compute_status(p, "a", "b")


# --- real frames captured 2026-07 (de-identified) ---------------------------

CLAUDE_IDLE_2026 = (
    "────────────────────────────\n"
    "❯\xa0Try \"how does <filepath> work?\"\n"
    "────────────────────────────\n"
    "  PROJ · Opus 4.8 (1M context)\n"
    "  ← for agents"
)

CODEX_IDLE_PLACEHOLDER = (
    "• Some earlier answer text.\n"
    "\n"
    "› Implement {feature}\n"
    "\n"
    "  gpt-x xhigh · ~/somewhere"
)

CODEX_WORKING = (
    "• 我会继续沿用现有分支,先核对目标命令树。\n"
    "\n"
    "• Working (8s • esc to interrupt)\n"
    "\n"
    "› Find and fix a bug in @filename\n"
    "\n"
    "  gpt-x xhigh · ~/somewhere"
)


def test_claude_new_ui_idle_hint_with_nbsp():
    p = get_provider("claude")
    status, _ = compute_status(p, CLAUDE_IDLE_2026, CLAUDE_IDLE_2026)
    assert status == store.IDLE


def test_codex_idle_with_placeholder_suggestion():
    p = get_provider("codex")
    status, _ = compute_status(p, CODEX_IDLE_PLACEHOLDER, CODEX_IDLE_PLACEHOLDER)
    assert status == store.IDLE


def test_codex_working_beats_idle_input_line():
    p = get_provider("codex")
    status, _ = compute_status(p, CODEX_WORKING, CODEX_WORKING)
    assert status == store.ACTIVE


def test_rotating_tip_on_claude_idle_screen_stays_idle():
    """Idle TUIs rotate their hint text; a repaint is NOT work. This used to
    flap idle→active→idle and fire spurious '已完成' notifications."""
    p = get_provider("claude")
    f1 = CLAUDE_IDLE_2026
    f2 = CLAUDE_IDLE_2026.replace("how does <filepath> work?", "fix the bug in <file>")
    status, changed = compute_status(p, f1, f2)
    assert changed is True          # the frame did change...
    assert status == store.IDLE     # ...but the idle marker wins


def test_rotating_placeholder_on_codex_idle_screen_stays_idle():
    p = get_provider("codex")
    f1 = CODEX_IDLE_PLACEHOLDER
    f2 = CODEX_IDLE_PLACEHOLDER.replace("Implement {feature}", "Find and fix a bug")
    status, _ = compute_status(p, f1, f2)
    assert status == store.IDLE


def test_codex_real_work_still_active_despite_idle_input_line():
    # working frame contains BOTH the '›' input line and the Working indicator
    p = get_provider("codex")
    status, _ = compute_status(p, CODEX_IDLE_PLACEHOLDER, CODEX_WORKING)
    assert status == store.ACTIVE


def test_ds4_headless_matches_claude_flags():
    # ds4 is Claude Code with a DeepSeek backend — same headless contract,
    # launched through the DeepSeek env wrapper.
    cmd = get_provider("ds4").resolve_headless_command()
    assert "-p" in cmd and "--dangerously-skip-permissions" in cmd
    assert cmd.split()[0].endswith("ds4_launch.sh")


def test_ds4_reuses_claude_detection():
    # Same TUI as Claude Code, so Claude's idle/active frames classify identically.
    ds4 = get_provider("ds4")
    idle = "assistant: done.\n╭────────╮\n│ >      │\n╰────────╯"
    active = "✳ Cogitating… (5s · ↓ 1.2k tokens)"
    assert compute_status(ds4, idle, idle)[0] == store.IDLE
    assert compute_status(ds4, active, active)[0] == store.ACTIVE


def test_hermes_changed_frame_still_counts_as_active():
    # hermes has no provider-specific idle patterns: keep change-driven detection
    p = get_provider("hermes")
    status, _ = compute_status(p, "old output", "new output arrived")
    assert status == store.ACTIVE


# --- current Claude UI frames captured 2026-07 (de-identified) ---------------
# Idle input is now a bare "❯" between two ──── rules (no │ > │ box, no "Try"
# hint). Working shows a rotating verb "<Verb>…" + a live "(elapsed · tokens)"
# footer. Finished shows a PAST-tense verb ("Brewed for 0s") with neither.

_RULE = "─" * 40

CLAUDE_IDLE_BARE_2026 = (
    f"{_RULE}\n"
    "❯ \n"
    f"{_RULE}\n"
    "  PROJ · Opus 4.8 (1M context) · Ctx 6% · 5h 28% · Wk 37%\n"
    "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"
)

CLAUDE_WORKING_2026 = (
    "⏺ Reorganized the draft files.\n"
    "\n"
    "✳ Whatchamacalliting… (5m 11s · ↓ 22.0k tokens)\n"
    "  ⎿  Tip: Use /btw to ask a quick side question without interrupting Claude\n"
    f"{_RULE}\n"
    "❯ \n"
    f"{_RULE}\n"
    "  PROJ · Opus 4.8 (1M context) · Ctx 6% · 5h 28% · Wk 37%"
)

CLAUDE_RUNNING_2026 = (
    "⏺ Running 1 shell command…\n"
    "  ⎿  $ git status --short\n"
    f"{_RULE}\n"
    "❯ \n"
    f"{_RULE}\n"
    "  PROJ · Opus 4.8 (1M context) · Ctx 6% · 5h 28% · Wk 37%"
)

CLAUDE_FINISHED_VERB_2026 = (
    "  ⎿  all done.\n"
    "✻ Brewed for 0s\n"
    f"{_RULE}\n"
    "❯ \n"
    f"{_RULE}\n"
    "  PROJ · Opus 4.8 (1M context) · Ctx 6% · 5h 28% · Wk 37%"
)

CLAUDE_WORKING_WITH_LIST_2026 = (
    "⏺ Here's the plan:\n"
    "  1. Refactor the sampler\n"
    "  2. Add tests\n"
    "  3. Ship it\n"
    "✳ Forging… (12s · ↓ 3.1k tokens)\n"
    f"{_RULE}\n"
    "❯ \n"
    f"{_RULE}\n"
    "  PROJ · Opus 4.8 (1M context) · Ctx 6% · 5h 28% · Wk 37%"
)

CODEX_FINISHED_2026 = (
    "  改动保留在工作区,尚未 commit。\n"
    "─ Worked for 7m 47s ─────────────\n"
    "› Find and fix a bug in @filename\n"
    "  gpt-x xhigh · ~/somewhere"
)


def test_claude_current_ui_bare_prompt_is_idle():
    p = get_provider("claude")
    status, _ = compute_status(p, CLAUDE_IDLE_BARE_2026, CLAUDE_IDLE_BARE_2026)
    assert status == store.IDLE


def test_claude_just_cleared_stays_idle_despite_full_repaint():
    """/clear wipes the whole screen -> a huge frame change. It must stay idle,
    not flip to 工作中 on the change alone (the original bug)."""
    p = get_provider("claude")
    # a busy conversation, then cleared to a fresh empty prompt
    status, changed = compute_status(p, CLAUDE_WORKING_2026, CLAUDE_IDLE_BARE_2026)
    assert changed is True
    assert status == store.IDLE


def test_claude_working_verb_footer_beats_idle_input_box():
    """The empty ❯ box is drawn even mid-run; the live "<Verb>… (elapsed·tokens)"
    footer must win so a busy seat isn't read as idle -> "已完成"."""
    p = get_provider("claude")
    status, _ = compute_status(p, CLAUDE_WORKING_2026, CLAUDE_WORKING_2026)
    assert status == store.ACTIVE


def test_claude_running_shell_command_is_active_even_when_static():
    """A long shell command holds a static "Running …command…" frame across
    samples. Without a strong signal it read idle -> spurious "已完成"."""
    p = get_provider("claude")
    status, changed = compute_status(p, CLAUDE_RUNNING_2026, CLAUDE_RUNNING_2026)
    assert changed is False          # frame did not move between samples...
    assert status == store.ACTIVE    # ...but it's still working


def test_claude_finished_verb_is_idle_not_active():
    """'✻ Brewed for 0s' is the FINISHED footer (past tense, no '…', no live
    timer). It must not be mistaken for a working '<Verb>…'."""
    p = get_provider("claude")
    status, _ = compute_status(p, CLAUDE_FINISHED_VERB_2026, CLAUDE_FINISHED_VERB_2026)
    assert status == store.IDLE


def test_claude_working_beats_numbered_list_in_output():
    """A busy agent streaming a '1. 2. 3.' list must stay active, not be read as
    a numbered selection menu (等待输入)."""
    p = get_provider("claude")
    status, _ = compute_status(p, CLAUDE_WORKING_WITH_LIST_2026, CLAUDE_WORKING_WITH_LIST_2026)
    assert status == store.ACTIVE


def test_codex_finished_divider_is_idle_not_active():
    """'─ Worked for 7m 47s ─' is past-tense/finished; must not match 'Working ('."""
    p = get_provider("codex")
    status, _ = compute_status(p, CODEX_FINISHED_2026, CODEX_FINISHED_2026)
    assert status == store.IDLE


# The footer that slipped through and fired a false 已完成: a "thinking …" screen
# rendered with ASCII dots and NO token count yet — the only live-work marker is
# the parenthesised 读秒 timer.
CLAUDE_THINKING_ASCII_2026 = (
    "⏺ Let me work through this.\n"
    "\n"
    "✳ Thinking... (12s)\n"
    f"{_RULE}\n"
    "❯ \n"
    f"{_RULE}\n"
    "  PROJ · Opus 4.8 (1M context) · Ctx 6% · 5h 28% · Wk 37%"
)


def test_claude_thinking_ascii_dots_timer_only_is_active():
    """'✳ Thinking... (12s)' — ASCII dots, no '…', no token count. The bare
    parenthesised timer alone must read active, else it flips to idle -> 已完成."""
    p = get_provider("claude")
    status, _ = compute_status(p, CLAUDE_THINKING_ASCII_2026, CLAUDE_THINKING_ASCII_2026)
    assert status == store.ACTIVE


def test_claude_idle_context_paren_is_not_a_working_timer():
    """The idle footer's '(1M context)' (uppercase M) must NOT be read as a live
    timer — the '读秒' pattern is case-sensitive on [hms] precisely to exclude it."""
    p = get_provider("claude")
    assert p.is_generating_strong(CLAUDE_IDLE_BARE_2026) is False


# --- the sticky state machine (next_status) ----------------------------------
# compute_status is the per-FRAME verdict; next_status turns a stream of those
# verdicts into a STABLE, latching status. These drive the machine by hand.

def _drive(seq, prev=store.ACTIVE, st=None):
    """Feed (raw, changed) pairs through next_status, threading one seat's state.
    Returns (final_status, list_of_push_kinds)."""
    st = st if st is not None else new_seat_state()
    pushes, status = [], prev
    for raw, changed in seq:
        status, kind = next_status(status, raw, changed, st)
        if kind:
            pushes.append(kind)
    return status, pushes


def test_active_holds_across_a_single_idle_blip():
    """The core bug: one markerless frame between a tool result and the next
    spinner must NOT flip 工作中 to 已完成/空闲."""
    status, pushes = _drive([(store.IDLE, True)], prev=store.ACTIVE)
    assert status == store.ACTIVE and pushes == []


def test_active_becomes_done_after_confirmed_settle_when_worked():
    # two real working frames arm "worked", then two settled frames confirm 已完成
    status, pushes = _drive(
        [(store.ACTIVE, True), (store.ACTIVE, True), (store.IDLE, True), (store.IDLE, True)],
        prev=store.ACTIVE)
    assert status == store.DONE and pushes == ["done"]


def test_booted_but_never_worked_settles_to_idle_not_done():
    """mark_started sets ACTIVE, but a seat that only ever showed its empty prompt
    never worked — it must land 空闲 and NEVER push 已完成 (protects the
    orchestrator's boot wait and avoids a spurious startup notification)."""
    status, pushes = _drive([(store.IDLE, True), (store.IDLE, False)], prev=store.ACTIVE)
    assert status == store.IDLE and pushes == []


def test_waiting_latches_over_settled_frames():
    """等待输入 whose prompt scrolled out of the capture window must NOT decay to
    空闲 on its own — the whole point of the redesign."""
    status, pushes = _drive([(store.IDLE, True), (store.IDLE, False)], prev=store.WAITING)
    assert status == store.WAITING and pushes == []


def test_done_latches_over_settled_frames():
    status, pushes = _drive([(store.IDLE, False), (store.IDLE, False)], prev=store.DONE)
    assert status == store.DONE and pushes == []


def test_active_to_waiting_needs_two_reads():
    st = new_seat_state()
    # a lone waiting frame (a '?' or '1.' in streamed output) HOLDS 工作中...
    status, kind = next_status(store.ACTIVE, store.WAITING, True, st)
    assert status == store.ACTIVE and kind is None
    # ...only a second consecutive waiting read commits and pushes 等待输入
    status, kind = next_status(status, store.WAITING, True, st)
    assert status == store.WAITING and kind == "waiting"


def test_waiting_resumes_to_active_on_real_work():
    # you answered the prompt directly in tmux; the agent generates again
    status, kind = next_status(store.WAITING, store.ACTIVE, True, new_seat_state())
    assert status == store.ACTIVE and kind is None


def test_done_resumes_to_active_on_real_work():
    status, kind = next_status(store.DONE, store.ACTIVE, True, new_seat_state())
    assert status == store.ACTIVE and kind is None


def test_entering_waiting_from_idle_pushes():
    st = new_seat_state()
    next_status(store.IDLE, store.WAITING, True, st)          # first read holds idle
    status, kind = next_status(store.IDLE, store.WAITING, True, st)
    assert status == store.WAITING and kind == "waiting"


def test_active_holds_through_unreadable_but_changing_frames():
    # opaque output that keeps repainting is still work — never drop to 空闲
    status, pushes = _drive(
        [(store.UNKNOWN, True), (store.UNKNOWN, True), (store.UNKNOWN, True)], prev=store.ACTIVE)
    assert status == store.ACTIVE and pushes == []


def test_active_falls_to_unknown_only_when_stably_unreadable():
    from app.status import STUCK_LIMIT
    # a long run of IDENTICAL unreadable frames (no repaint) is not a live agent
    # — fall to the 保底 未知 rather than claim 工作中 forever.
    status, pushes = _drive([(store.UNKNOWN, False)] * STUCK_LIMIT, prev=store.ACTIVE)
    assert status == store.UNKNOWN and pushes == []


def test_idle_stays_idle_on_unreadable_frame():
    status, kind = next_status(store.IDLE, store.UNKNOWN, False, new_seat_state())
    assert status == store.IDLE and kind is None


# --- position-aware footer: only the LOWEST status line counts ---------------
# A just-finished turn still carries its 'Running…'/'<Verb>…' lines in the
# captured window, sitting ABOVE the finished footer. The finished line, being
# lower/more recent, must win — else the seat reads 工作中 after it's done.
CLAUDE_JUST_FINISHED_2026 = (
    "⏺ Running 1 shell command…\n"                 # stale tool line, still in window
    "  ⎿  $ pytest -q\n"
    "✽ Spelunking… (4m 12s · ↓ 18.0k tokens)\n"    # stale live footer from last step
    "\n"
    "✻ Cogitated for 9m 48s\n"                     # <-- the real, current state: DONE
    f"{_RULE}\n"
    "❯ \n"
    f"{_RULE}\n"
    "  PROJ · Opus 4.8 (1M context) · Ctx 6% · 5h 28% · Wk 37%"
)

CLAUDE_END_SURVEY_2026 = (
    "✻ Cogitated for 4m 12s\n"                     # finished (past tense) footer above
    "\n"
    "● How is Claude doing this session? (optional)\n"
    "  1: Bad    2: Fine   3: Good   0: Dismiss\n"
    f"{_RULE}\n"
    "❯ \n"
    f"{_RULE}\n"
    "  PROJ · Opus 4.8 (1M context) · Ctx 6% · 5h 28% · Wk 37%"
)

# The real bug: Claude popped the session survey WHILE still working — a live
# "<Verb>… (5m 11s · still thinking)" footer sits ABOVE the survey. The survey
# must NOT be read as "turn finished"; the live footer wins -> 工作中. (A seat
# stuck reading 空闲 while genuinely generating was the reported failure.)
CLAUDE_WORKING_WITH_SURVEY_2026 = (
    "⏺ 并行实现和设计高度吻合。23 测试通过。\n"
    "\n"
    "✻ Meandering… (5m 11s · ↓ 14.0k tokens · still thinking with xhigh effort)\n"
    "  ⎿  Tip: Use /btw to ask a quick side question without interrupting Claude\n"
    "● How is Claude doing this session? (optional)\n"
    "  1: Bad    2: Fine   3: Good   0: Dismiss\n"
    f"{_RULE}\n"
    "❯ \n"
    f"{_RULE}\n"
    "  code · Opus 4.8 (1M context) · Ctx 40% · 5h 39% · Wk 48%"
)

# The inverse: a NEW turn is working; a stale 'Cogitated for 5m' from the
# previous turn sits above the live footer. The live footer (lower) must win.
CLAUDE_RESUMED_WORK_2026 = (
    "✻ Cogitated for 5m 02s\n"                     # stale finished line from last turn
    "⏺ Now let me run the check.\n"
    "✶ Skedaddling… (12s · ↓ 900 tokens)\n"        # <-- current state: WORKING
    f"{_RULE}\n"
    "❯ \n"
    f"{_RULE}\n"
    "  PROJ · Opus 4.8 (1M context) · Ctx 6% · 5h 28% · Wk 37%"
)


def test_claude_just_finished_is_idle_despite_stale_working_lines():
    p = get_provider("claude")
    assert p.footer_state(CLAUDE_JUST_FINISHED_2026) == "idle"
    status, _ = compute_status(p, CLAUDE_JUST_FINISHED_2026, CLAUDE_JUST_FINISHED_2026)
    assert status == store.IDLE


def test_claude_end_of_session_survey_is_idle():
    p = get_provider("claude")
    assert p.footer_state(CLAUDE_END_SURVEY_2026) == "idle"   # via the past-tense footer
    status, _ = compute_status(p, CLAUDE_END_SURVEY_2026, CLAUDE_END_SURVEY_2026)
    assert status == store.IDLE


def test_claude_survey_below_live_footer_is_active():
    """The session survey can appear WHILE Claude is still working. A live footer
    above it must win — the seat is 工作中, not 空闲 (the reported bug)."""
    p = get_provider("claude")
    assert p.footer_state(CLAUDE_WORKING_WITH_SURVEY_2026) == "active"
    status, _ = compute_status(p, CLAUDE_WORKING_WITH_SURVEY_2026, CLAUDE_WORKING_WITH_SURVEY_2026)
    assert status == store.ACTIVE


def test_claude_resumed_work_beats_stale_finished_line():
    p = get_provider("claude")
    assert p.footer_state(CLAUDE_RESUMED_WORK_2026) == "active"
    status, _ = compute_status(p, CLAUDE_RESUMED_WORK_2026, CLAUDE_RESUMED_WORK_2026)
    assert status == store.ACTIVE


def test_first_sample_baseline_is_not_read_as_changed():
    """The sampler passes baseline=curr on the first sample (no prior frame), so
    a markerless idle screen must resolve via its idle marker, NOT via 'changed'
    -> active. Otherwise every restart blips hermes active for a cycle."""
    p = get_provider("hermes")
    idle = "⚕ model │ ctx -- │ [░░░░] -- │ 6s │ ⏲ 0s\n────\n❯\n────"
    status, changed = compute_status(p, idle, idle)   # baseline == curr
    assert changed is False
    assert status != store.ACTIVE


# --- acknowledge suppression (next_status "acked") ---------------------------

def test_acked_screen_stays_quiet_until_work_resumes():
    """After a view-ack the SAME persistent prompt must not re-alert; only real
    work re-arms it. (You looked at a Claude permission prompt, chose to defer,
    switched away — it shouldn't keep nagging about the identical screen.)"""
    st = new_seat_state()
    st["acked"] = True
    # the prompt is still on screen every sample -> stays 空闲, no push
    status, pushes = _drive(
        [(store.WAITING, False), (store.WAITING, False), (store.IDLE, False)],
        prev=store.IDLE, st=st)
    assert status == store.IDLE and pushes == []
    # the agent finally does something new: alerts are re-armed
    status, kind = next_status(store.IDLE, store.ACTIVE, True, st)
    assert status == store.ACTIVE and st["acked"] is False


# --- view-acknowledge, driven through the real sampler -----------------------

def _mono_tmux(monkeypatch, name, focus):
    import app.tmux as tmux_mod
    monkeypatch.setattr(tmux_mod, "has_session", lambda n: True)
    monkeypatch.setattr(tmux_mod, "pane_dead", lambda n: False)
    monkeypatch.setattr(tmux_mod, "capture_pane", lambda n, *a, **k: "Proceed? (y/n)")
    monkeypatch.setattr(tmux_mod, "viewer_focus_session", focus)


def _waiting_seat(tmp="/tmp"):
    proj = store.create_project("proj", tmp)
    seat = store.create_session(proj["id"], "seat", "custom", tmp, "run")
    store.mark_started(seat["id"])
    store.update_status(seat["id"], store.WAITING, "", False)
    return seat["id"], seat["tmux_session"]


def test_view_ack_clears_waiting_after_you_jump_and_leave(store_db, monkeypatch):
    """The intended flow: you hit '跳到终端' (mark_viewed), the viewer lands on the
    seat, you look, then move the viewer away -> it acknowledges to 空闲."""
    from app.status import StatusSampler
    sid, name = _waiting_seat()

    focus = {"v": name}
    _mono_tmux(monkeypatch, name, lambda: focus["v"])
    sampler = StatusSampler()
    sampler.mark_viewed(sid)                 # the hub jump

    # viewer is ON the seat: 等待输入 persists (you're still looking)
    sampler.sample_once()
    assert store.get_session(sid)["status"] == store.WAITING

    # viewer moved away: having deliberately looked, the seat clears to 空闲
    focus["v"] = "hub-other-seat"
    sampler.sample_once()
    assert store.get_session(sid)["status"] == store.IDLE
    assert sid not in sampler._viewed


def test_parked_but_not_jumped_seat_never_auto_clears(store_db, monkeypatch):
    """The reported bug: the viewer happens to be PARKED on a seat you never
    opened via the hub. When it later drifts away the seat must STAY 等待输入 /
    已完成 — only a deliberate jump arms the auto-clear."""
    from app.status import StatusSampler
    sid, name = _waiting_seat()

    focus = {"v": name}                       # viewer passively parked on the seat
    _mono_tmux(monkeypatch, name, lambda: focus["v"])
    sampler = StatusSampler()                 # note: NO mark_viewed

    sampler.sample_once()
    assert store.get_session(sid)["status"] == store.WAITING
    assert sid not in sampler._viewed         # parking alone does not mark it viewed

    focus["v"] = "somewhere-else"             # viewer drifts off
    sampler.sample_once()
    assert store.get_session(sid)["status"] == store.WAITING   # still latched


def test_unlooked_waiting_never_auto_clears(store_db, monkeypatch):
    from app.status import StatusSampler
    sid, name = _waiting_seat()
    # viewer is never on this seat and you never jumped
    _mono_tmux(monkeypatch, name, lambda: "elsewhere")
    sampler = StatusSampler()
    for _ in range(5):
        sampler.sample_once()
    assert store.get_session(sid)["status"] == store.WAITING
