"""Real tmux lifecycle tests on the dedicated agent-hub socket.

Uses only harmless shell commands; never launches a real agent. Skipped if
tmux is unavailable.
"""
import time
import uuid

import pytest

from app import tmux

pytestmark = pytest.mark.skipif(not tmux.available(), reason="tmux not installed")


def _name() -> str:
    return f"agent-hub-test-{uuid.uuid4().hex[:10]}"


def test_create_capture_kill(tmp_path):
    name = _name()
    try:
        assert not tmux.has_session(name)
        tmux.new_session(name, str(tmp_path), "sh -c 'echo HELLO_HUB; sleep 600'")
        assert tmux.has_session(name)

        out = ""
        for _ in range(10):
            out = tmux.capture_pane(name)
            if "HELLO_HUB" in out:
                break
            time.sleep(0.15)
        assert "HELLO_HUB" in out
    finally:
        tmux.kill_session(name)
    assert not tmux.has_session(name)


def test_kill_isolates_other_sessions(tmp_path):
    a, b = _name(), _name()
    try:
        tmux.new_session(a, str(tmp_path), "sleep 600")
        tmux.new_session(b, str(tmp_path), "sleep 600")
        assert tmux.has_session(a) and tmux.has_session(b)

        tmux.kill_session(a)
        assert not tmux.has_session(a)
        assert tmux.has_session(b)  # killing one must not touch the other
    finally:
        tmux.kill_session(a)
        tmux.kill_session(b)


def test_dead_pane_keeps_dying_output(tmp_path):
    """remain-on-exit: a crashing command leaves a dead pane whose last words
    are still capturable (this is how 'why did it die' reaches the card)."""
    name = _name()
    try:
        tmux.new_session(name, str(tmp_path), "sh -c 'echo BOOM_DYING_WORDS; exit 3'")
        deadline = time.time() + 5
        while time.time() < deadline and not tmux.pane_dead(name):
            time.sleep(0.1)
        assert tmux.has_session(name)          # corpse still registered
        assert tmux.pane_dead(name)
        # dying output scrolls into history; the sampler captures with history
        assert "BOOM_DYING_WORDS" in tmux.capture_pane(name, with_history=True)
    finally:
        tmux.kill_session(name)


def test_rename_session_relabels_live_session(tmp_path):
    old, new = _name(), _name()
    try:
        tmux.new_session(old, str(tmp_path), "sleep 600")
        assert tmux.rename_session(old, new)
        assert tmux.has_session(new)
        assert not tmux.has_session(old)
        assert not tmux.pane_dead(new)         # process untouched by the rename
    finally:
        tmux.kill_session(old)
        tmux.kill_session(new)
