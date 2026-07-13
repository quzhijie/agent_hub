import uuid

import pytest

from app import jump, store, tmux


def test_jump_reports_gone_for_unstarted_seat(store_db, tmp_path):
    p = store.create_project("P", str(tmp_path))
    s = store.create_session(p["id"], "seat", "claude", str(tmp_path), "")
    r = jump.jump_to(s)
    assert r["ok"] is False
    assert "gone" in r["reason"] or "exited" in r["reason"]


@pytest.mark.skipif(not tmux.available(), reason="tmux not installed")
def test_jump_without_client_offers_attach_command(store_db, tmp_path):
    p = store.create_project("P", str(tmp_path))
    s = store.create_session(p["id"], "seat", "custom", str(tmp_path), "sleep 600")
    name = s["tmux_session"]
    try:
        tmux.new_session(name, str(tmp_path), "sleep 600")
        r = jump.jump_to(store.get_session(s["id"]))
        assert r["ok"] is True
        assert r["session"] == name
        if not r["jumped"]:  # no viewer attached during tests (the common case)
            assert name in r["attach_command"]
    finally:
        tmux.kill_session(name)
