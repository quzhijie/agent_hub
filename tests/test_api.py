def _make_project(client, tmp_path):
    return client.post("/api/projects", json={"name": "Proj", "root_dir": str(tmp_path)})


def test_empty_state(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    assert r.json()["projects"] == []
    assert r.json()["events"] == []


def test_recent_notifications_roundtrip(store_db, tmp_path):
    from app import store
    proj = store.create_project("P", str(tmp_path))
    seat = store.create_session(proj["id"], "executor", "claude", str(tmp_path), "")
    sid = seat["id"]
    assert store.recent_notifications() == []

    store.record_notification(sid, "active", "idle", "done")
    store.record_notification(sid, "idle", "waiting", "waiting")
    evs = store.recent_notifications()
    assert len(evs) == 2
    # newest first: the WAITING push was recorded last, so it leads.
    assert evs[0]["kind"] == "waiting"
    assert evs[0]["seat_id"] == sid
    assert evs[0]["seat_removed"] is False
    assert evs[0]["text"] == "P / executor 等待输入"
    assert evs[1]["kind"] == "done" and "已完成" in evs[1]["text"]

    # Once the seat is removed, its trail rows are flagged non-jumpable; the
    # 'manually_removed' lifecycle event is not a notify_* row, so count holds.
    store.mark_removed(sid)
    evs2 = store.recent_notifications()
    assert len(evs2) == 2 and all(e["seat_removed"] for e in evs2)


def test_archive_notifications(store_db, tmp_path):
    from app import store
    proj = store.create_project("P", str(tmp_path))
    seat = store.create_session(proj["id"], "executor", "claude", str(tmp_path), "")
    sid = seat["id"]
    store.record_notification(sid, "active", "idle", "done")
    store.record_notification(sid, "idle", "waiting", "waiting")
    evs = store.recent_notifications()
    assert [e["kind"] for e in evs] == ["waiting", "done"]

    # Archiving the top row drops just that one; the other stays.
    assert store.archive_notification(evs[0]["id"]) is True
    assert [e["kind"] for e in store.recent_notifications()] == ["done"]
    # Archiving again is a no-op (already archived).
    assert store.archive_notification(evs[0]["id"]) is False

    # A brand-new ping still appears — archiving never suppresses future events.
    store.record_notification(sid, "idle", "waiting", "waiting")
    assert any(e["kind"] == "waiting" for e in store.recent_notifications())

    # Clear-all archives whatever's left; the strip goes empty.
    assert store.archive_all_notifications() == 2
    assert store.recent_notifications() == []
    assert store.archive_all_notifications() == 0


def test_archive_event_endpoint(client, tmp_path):
    from app import store
    pid = _make_project(client, tmp_path).json()["id"]
    seat = client.post(f"/api/projects/{pid}/sessions",
                       json={"name": "s", "provider": "claude",
                             "working_dir": str(tmp_path)}).json()
    store.record_notification(seat["id"], "active", "idle", "done")
    ev = client.get("/api/state").json()["events"][0]
    assert client.post(f"/api/events/{ev['id']}/archive").status_code == 200
    assert client.get("/api/state").json()["events"] == []
    # Second archive of the same id 404s; archive_all now clears nothing.
    assert client.post(f"/api/events/{ev['id']}/archive").status_code == 404
    assert client.post("/api/events/archive_all").json()["archived"] == 0


def test_project_crud(client, tmp_path):
    r = _make_project(client, tmp_path)
    assert r.status_code == 200
    pid = r.json()["id"]

    assert client.get("/api/projects").json()[0]["id"] == pid

    r = client.patch(f"/api/projects/{pid}", json={"name": "Renamed"})
    assert r.json()["name"] == "Renamed"

    client.patch(f"/api/projects/{pid}", json={"is_removed": True})
    assert client.get("/api/projects").json() == []
    assert len(client.get("/api/projects?include_removed=true").json()) == 1


def test_project_validation(client):
    assert client.post("/api/projects", json={"name": "x", "root_dir": "relative"}).status_code == 400
    assert client.post("/api/projects", json={"name": "", "root_dir": "/tmp"}).status_code == 400


def test_session_lifecycle_registry(client, tmp_path):
    pid = _make_project(client, tmp_path).json()["id"]

    r = client.post(f"/api/projects/{pid}/sessions",
                    json={"name": "exec", "provider": "claude", "working_dir": str(tmp_path)})
    assert r.status_code == 200
    seat = r.json()
    assert seat["status"] == "unknown"
    assert seat["tmux_session"].startswith("hub-")
    assert "exec" in seat["tmux_session"]      # seat name is visible in tmux/handmux
    assert seat["started_at"] is None

    # bad provider / custom-without-command
    assert client.post(f"/api/projects/{pid}/sessions",
                       json={"name": "x", "provider": "nope", "working_dir": str(tmp_path)}).status_code == 400
    assert client.post(f"/api/projects/{pid}/sessions",
                       json={"name": "x", "provider": "custom", "working_dir": str(tmp_path)}).status_code == 400
    # bad working dir
    assert client.post(f"/api/projects/{pid}/sessions",
                       json={"name": "x", "provider": "claude", "working_dir": "rel"}).status_code == 400

    seats = client.get(f"/api/projects/{pid}/sessions").json()
    assert len(seats) == 1


def test_remove_restore_and_purge(client, tmp_path):
    pid = _make_project(client, tmp_path).json()["id"]
    sid = client.post(f"/api/projects/{pid}/sessions",
                      json={"name": "exec", "provider": "claude", "working_dir": str(tmp_path)}).json()["id"]

    # remove -> lands in removed_sessions, out of the active list
    client.post(f"/api/sessions/{sid}/remove")
    state = client.get("/api/state").json()["projects"][0]
    assert state["sessions"] == []
    assert [s["id"] for s in state["removed_sessions"]] == [sid]

    # restore -> back to active, unstarted
    client.post(f"/api/sessions/{sid}/restore")
    state = client.get("/api/state").json()["projects"][0]
    assert [s["id"] for s in state["sessions"]] == [sid]
    assert state["removed_sessions"] == []

    # purge -> gone for good, and idempotent 404 afterwards
    client.post(f"/api/sessions/{sid}/remove")
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    state = client.get("/api/state").json()["projects"][0]
    assert state["sessions"] == [] and state["removed_sessions"] == []
    assert client.delete(f"/api/sessions/{sid}").status_code == 404


def test_project_notes_roundtrip(client, tmp_path):
    pid = _make_project(client, tmp_path).json()["id"]
    assert client.get("/api/state").json()["projects"][0]["notes"] == ""
    r = client.patch(f"/api/projects/{pid}", json={"notes": "做到第三步了"})
    assert r.status_code == 200 and r.json()["notes"] == "做到第三步了"
    assert client.get("/api/state").json()["projects"][0]["notes"] == "做到第三步了"
    # a name-only update must not wipe notes
    client.patch(f"/api/projects/{pid}", json={"name": "Renamed"})
    assert client.get("/api/state").json()["projects"][0]["notes"] == "做到第三步了"


def test_reorder_projects_and_seats(client, tmp_path):
    p1 = client.post("/api/projects", json={"name": "A", "root_dir": str(tmp_path)}).json()
    p2 = client.post("/api/projects", json={"name": "B", "root_dir": str(tmp_path)}).json()
    assert [p["id"] for p in client.get("/api/projects").json()] == [p1["id"], p2["id"]]

    r = client.post("/api/projects/reorder", json={"ids": [p2["id"], p1["id"]]})
    assert r.status_code == 200
    assert [p["id"] for p in client.get("/api/projects").json()] == [p2["id"], p1["id"]]

    s1 = client.post(f"/api/projects/{p1['id']}/sessions",
                     json={"name": "a", "provider": "claude", "working_dir": str(tmp_path)}).json()
    s2 = client.post(f"/api/projects/{p1['id']}/sessions",
                     json={"name": "b", "provider": "claude", "working_dir": str(tmp_path)}).json()
    assert [s["id"] for s in client.get(f"/api/projects/{p1['id']}/sessions").json()] == [s1["id"], s2["id"]]

    r = client.post(f"/api/projects/{p1['id']}/sessions/reorder", json={"ids": [s2["id"], s1["id"]]})
    assert r.status_code == 200
    assert [s["id"] for s in client.get(f"/api/projects/{p1['id']}/sessions").json()] == [s2["id"], s1["id"]]

    # a seat id passed to the wrong project must not be re-scoped
    assert client.post(f"/api/projects/{p2['id']}/sessions/reorder",
                       json={"ids": [s1["id"]]}).status_code == 200
    assert [s["id"] for s in client.get(f"/api/projects/{p1['id']}/sessions").json()] == [s2["id"], s1["id"]]


def test_start_migrates_old_hash_name_to_readable(client, tmp_path):
    import pytest as _pytest
    from app import store, tmux
    if not tmux.available():
        _pytest.skip("tmux not installed")
    pid = _make_project(client, tmp_path).json()["id"]
    seat = client.post(f"/api/projects/{pid}/sessions",
                       json={"name": "exec", "provider": "custom",
                             "working_dir": str(tmp_path), "launch_command": "sleep 600"}).json()
    # Forge a pre-rename hash-style name, then start: it must come back readable.
    store.update_tmux_session(seat["id"], "agent-hub-deadbeef-cafebabe")
    started = client.post(f"/api/sessions/{seat['id']}/start").json()
    try:
        assert started["tmux_session"].startswith("hub-")
        assert "exec" in started["tmux_session"]
        assert tmux.has_session(started["tmux_session"])
    finally:
        tmux.kill_session(started["tmux_session"])


def test_providers_endpoint(client):
    provs = client.get("/api/providers").json()
    assert set(provs) >= {"hermes", "claude", "codex", "custom"}


def test_index_injects_token_without_clobbering_var(client, settings):
    body = client.get("/").text
    assert "window.__AUTH_TOKEN__" in body   # JS variable name must survive
    assert settings.token in body            # real token injected
    assert "%%AUTH_TOKEN%%" not in body       # placeholder consumed


def test_auth_requires_token(client):
    assert client.get("/api/state", headers={"X-Auth-Token": "wrong"}).status_code == 401


def test_rejects_non_loopback_host(client):
    r = client.get("/api/state", headers={"Host": "evil.example.com"})
    assert r.status_code == 403


def test_accepts_dot_localhost_host(client):
    # Browsers resolve *.localhost to loopback themselves (RFC 6761), so the
    # memorable http://agent-hub.localhost:8787 must pass the Host guard.
    r = client.get("/api/state", headers={"Host": "agent-hub.localhost:8787"})
    assert r.status_code == 200
    # but a lookalike public domain must not
    r = client.get("/api/state", headers={"Host": "agent-hub.localhost.evil.com"})
    assert r.status_code == 403


def test_rejects_cross_origin(client):
    r = client.get("/api/state", headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403
