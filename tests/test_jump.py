import uuid
from types import SimpleNamespace

import pytest

from app import jump, store, tmux


def test_client_details_parses_tmux_format(monkeypatch):
    stdout = (
        "/dev/ttys001\t/dev/ttys001\t120\t38\thub-one\txterm-256color\n"
        "/dev/ttys009\t/dev/ttys009\twide\tbad\thub-two\tscreen\n"
    )
    monkeypatch.setattr(tmux, "_run", lambda args: SimpleNamespace(returncode=0, stdout=stdout))

    assert tmux.client_details() == [
        {"name": "/dev/ttys001", "tty": "/dev/ttys001", "width": 120,
         "height": 38, "session": "hub-one", "term": "xterm-256color"},
        {"name": "/dev/ttys009", "tty": "/dev/ttys009", "width": 0,
         "height": 0, "session": "hub-two", "term": "screen"},
    ]


def test_jump_uses_explicit_live_client(store_db, tmp_path, monkeypatch):
    p = store.create_project("P", str(tmp_path))
    s = store.create_session(p["id"], "seat", "claude", str(tmp_path), "")
    selected = "/dev/ttys009"
    switched = {}

    monkeypatch.setattr(tmux, "has_session", lambda name: True)
    monkeypatch.setattr(tmux, "pane_dead", lambda name: False)
    monkeypatch.setattr(tmux, "client_by_name", lambda name: (name, name) if name == selected else None)
    monkeypatch.setattr(tmux, "viewer_client", lambda: pytest.fail("widest fallback must not be used"))
    monkeypatch.setattr(tmux, "switch_client", lambda client, name: switched.update(client=client, session=name) or True)
    monkeypatch.setattr(jump.focus, "focus_terminal_by_tty", lambda tty: False)

    r = jump.jump_to(s, client_name=selected)

    assert r["ok"] is True and r["jumped"] is True
    assert r["client"] == selected
    assert r["explicit_client"] is True
    assert switched == {"client": selected, "session": s["tmux_session"]}


def test_jump_rejects_disconnected_selected_client(store_db, tmp_path, monkeypatch):
    p = store.create_project("P", str(tmp_path))
    s = store.create_session(p["id"], "seat", "claude", str(tmp_path), "")
    monkeypatch.setattr(tmux, "has_session", lambda name: True)
    monkeypatch.setattr(tmux, "pane_dead", lambda name: False)
    monkeypatch.setattr(tmux, "client_by_name", lambda name: None)

    r = jump.jump_to(s, client_name="/dev/ttys404")

    assert r["ok"] is False
    assert "no longer attached" in r["reason"]
    assert s["tmux_session"] in r["attach_command"]


def test_jump_reports_gone_for_unstarted_seat(store_db, tmp_path):
    p = store.create_project("P", str(tmp_path))
    s = store.create_session(p["id"], "seat", "claude", str(tmp_path), "")
    r = jump.jump_to(s)
    assert r["ok"] is False
    assert "gone" in r["reason"] or "exited" in r["reason"]


def test_jump_rejects_a_remain_on_exit_corpse(store_db, tmp_path, monkeypatch):
    p = store.create_project("P", str(tmp_path))
    s = store.create_session(p["id"], "seat", "codex", str(tmp_path), "")
    monkeypatch.setattr(tmux, "has_session", lambda _name: True)
    monkeypatch.setattr(tmux, "pane_dead", lambda _name: True)

    r = jump.jump_to(s)

    assert r["ok"] is False
    assert "restart" in r["reason"]


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
