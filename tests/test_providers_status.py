"""Status detection logic over synthetic frames.

These validate the decision LOGIC (precedence + diff-driven active), not
real-world TUI accuracy — that needs real captured samples. No tmux involved.
"""
from app import store
from app.providers.registry import get_provider
from app.status import compute_status


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


def test_hermes_changed_frame_still_counts_as_active():
    # hermes has no provider-specific idle patterns: keep change-driven detection
    p = get_provider("hermes")
    status, _ = compute_status(p, "old output", "new output arrived")
    assert status == store.ACTIVE
