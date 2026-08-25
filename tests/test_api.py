import hashlib
import json


def _manual_bundle():
    document = "Read the exact handoff.\n"
    digest = lambda value: hashlib.sha256(  # noqa: E731 - compact test fixture
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    documents = {"tools/context-handoff.md": document}
    index = {
        "schema": "project-core.agent-manual-index/v1",
        "manual_version": "test-1", "read_first": "Read context first.",
        "bootstrap_guardrails": [
            "Treat scientific conclusions as provisional until human review.",
            "Context is not execution authorization.",
            "Do not submit a Project Core checkpoint without explicit user opt-in.",
        ],
        "tools": [{
            "id": "context-handoff", "version": 1, "title": "Context",
            "document_path": "tools/context-handoff.md", "availability": "available",
            "required_authority": "registered association",
            "when_to_read": "at startup", "sha256": digest(document),
        }],
    }
    unsigned = {
        "schema_version": 1, "manual_version": "test-1", "index": index,
        "index_sha256": digest(index), "documents": documents,
    }
    bundle_digest = digest(unsigned)
    return {"id": f"manual_{bundle_digest[:24]}", **unsigned, "sha256": bundle_digest}


def _make_project(client, tmp_path):
    return client.post("/api/projects", json={"name": "Proj", "root_dir": str(tmp_path)})


def test_empty_state(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    assert r.json()["projects"] == []
    assert r.json()["tmux_clients"] == []
    assert r.json()["instance_id"]
    assert r.json()["restart_available"] is False


def test_restart_api_schedules_the_managed_service_once(client):
    class FakeRestartController:
        available = True

        def __init__(self):
            self.calls = 0

        def request(self):
            self.calls += 1
            return self.calls == 1

    controller = FakeRestartController()
    client.app.state.restart_controller = controller

    first = client.post("/api/system/restart", json={})
    second = client.post("/api/system/restart", json={})

    assert first.status_code == 202
    assert first.json()["accepted"] is True
    assert first.json()["already_requested"] is False
    assert first.json()["instance_id"] == client.app.state.instance_id
    assert second.status_code == 202
    assert second.json()["already_requested"] is True


def test_provider_options_advertise_native_models(client):
    options = {
        item["name"]: item["models"]
        for item in client.get("/api/provider-options").json()
    }
    assert "opus" in options["claude"]
    assert "gpt-5.6-sol" in options["codex"]
    assert options["custom"] == []


def test_jump_api_passes_selected_client(client, tmp_path, monkeypatch):
    from app.routes import sessions as sessions_route

    pid = _make_project(client, tmp_path).json()["id"]
    seat = client.post(
        f"/api/projects/{pid}/sessions",
        json={"name": "seat", "provider": "claude", "working_dir": str(tmp_path)},
    ).json()
    seen = {}

    def fake_jump(sess, client_name=None):
        seen.update(session=sess["id"], client=client_name)
        return {"ok": True, "jumped": True, "focused": False}

    monkeypatch.setattr(sessions_route.jump_mod, "jump_to", fake_jump)
    r = client.post(
        f"/api/sessions/{seat['id']}/jump",
        json={"client": "/dev/ttys009"},
    )

    assert r.status_code == 200
    assert seen == {"session": seat["id"], "client": "/dev/ttys009"}


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


def test_project_focus_publishes_navigation_intent(client, tmp_path, monkeypatch):
    from app.routes import projects as projects_route

    pid = _make_project(client, tmp_path).json()["id"]
    seen = {}

    def fake_focus(settings, project_id, *, open_if_missing):
        seen.update(
            token=settings.token, project_id=project_id,
            open_if_missing=open_if_missing,
        )
        return {"handled": True, "status": "focused"}

    monkeypatch.setattr(projects_route.dashboard, "focus_project", fake_focus)
    response = client.post(f"/api/projects/{pid}/focus", json={})

    assert response.json() == {"handled": True, "status": "focused"}
    assert seen == {
        "token": "testtoken", "project_id": pid, "open_if_missing": False,
    }
    intent = client.get("/api/state").json()["navigation_intent"]
    assert intent["project_id"] == pid
    assert intent["id"]


def test_active_dashboard_receives_focus_intent_without_opening_fallback(
    client, tmp_path, monkeypatch,
):
    from app.routes import projects as projects_route

    pid = _make_project(client, tmp_path).json()["id"]
    monkeypatch.setattr(
        projects_route.dashboard,
        "focus_project",
        lambda _settings, _project_id, *, open_if_missing: {
            "handled": False, "status": "native-focus-unavailable"
        },
    )
    client.get("/api/state", headers={"X-Agent-Hub-Dashboard": "1"})

    response = client.post(f"/api/projects/{pid}/focus", json={})

    assert response.json() == {
        "handled": True,
        "status": "intent-delivered",
    }


def test_focus_leaves_the_only_open_fallback_to_its_caller(
    client, tmp_path, monkeypatch,
):
    from app.routes import projects as projects_route

    pid = _make_project(client, tmp_path).json()["id"]
    seen = {}

    def fake_focus(_settings, _project_id, *, open_if_missing):
        seen["open_if_missing"] = open_if_missing
        return {"handled": False, "status": "no-dashboard-found"}

    monkeypatch.setattr(projects_route.dashboard, "focus_project", fake_focus)

    response = client.post(f"/api/projects/{pid}/focus", json={})

    assert response.json() == {
        "handled": False,
        "status": "no-dashboard-found",
    }
    assert seen == {"open_if_missing": False}


def test_project_validation(client):
    assert client.post("/api/projects", json={"name": "x", "root_dir": "relative"}).status_code == 400
    assert client.post("/api/projects", json={"name": "", "root_dir": "/tmp"}).status_code == 400


def test_project_edit_root_dir_relocates_seats(client, tmp_path):
    # A project's folder was reorganized: old -> new. Seats at/under the old root
    # must follow; a seat pointing elsewhere must be left alone.
    old = tmp_path / "old"; old.mkdir()
    (old / "pkg").mkdir()
    new = tmp_path / "new"; new.mkdir()
    other = tmp_path / "other"; other.mkdir()

    pid = client.post("/api/projects", json={"name": "P", "root_dir": str(old)}).json()["id"]
    def seat(name, wd):
        return client.post(f"/api/projects/{pid}/sessions",
                           json={"name": name, "provider": "claude", "working_dir": wd}).json()["id"]
    s_root = seat("a", str(old))
    s_sub = seat("b", str(old / "pkg"))
    s_other = seat("c", str(other))

    r = client.patch(f"/api/projects/{pid}", json={"root_dir": str(new)})
    assert r.status_code == 200 and r.json()["root_dir"] == str(new)

    wd = {s["id"]: s["working_dir"] for s in client.get(f"/api/projects/{pid}/sessions").json()}
    assert wd[s_root] == str(new)             # seat at the old root -> new root
    assert wd[s_sub] == str(new / "pkg")      # sub-directory seat follows the prefix
    assert wd[s_other] == str(other)          # unrelated seat untouched


def test_project_edit_root_dir_rejects_missing_dir(client, tmp_path):
    pid = _make_project(client, tmp_path).json()["id"]
    r = client.patch(f"/api/projects/{pid}", json={"root_dir": str(tmp_path / "nope")})
    assert r.status_code == 400
    assert client.get("/api/projects").json()[0]["root_dir"] == str(tmp_path)  # unchanged


def test_project_delete_purges_seats(client, tmp_path):
    from app import store
    pid = _make_project(client, tmp_path).json()["id"]
    client.post(f"/api/projects/{pid}/sessions",
                json={"name": "a", "provider": "claude", "working_dir": str(tmp_path)})
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get("/api/projects?include_removed=true").json() == []
    assert store.get_project(pid) is None
    assert store.list_sessions(pid, include_removed=True) == []       # seat rows purged too
    assert client.delete(f"/api/projects/{pid}").status_code == 404   # idempotent 404


def test_project_delete_preserves_pending_project_core_outbox(client, tmp_path):
    from app import db, store

    pid = _make_project(client, tmp_path).json()["id"]
    seat = client.post(
        f"/api/projects/{pid}/sessions",
        json={"name": "a", "provider": "claude", "working_dir": str(tmp_path)},
    ).json()
    now = store.now_iso()
    with db.writing() as connection:
        connection.execute(
            """INSERT INTO project_core_outbox(
                   event_id, session_id, association_id, turn_id, event_type,
                   idempotency_key, envelope_json, envelope_sha256, state,
                   created_at, updated_at
               ) VALUES ('evt-pending', ?, 'asoc-pending', NULL,
                         'agent.session_finished', 'pending-delete', '{}', ?,
                         'pending', ?, ?)""",
            (seat["id"], "0" * 64, now, now),
        )
    blocked = client.delete(f"/api/projects/{pid}")
    assert blocked.status_code == 409
    assert store.get_project(pid) is not None
    assert store.get_session(seat["id"])["removed_at"] is not None
    assert store.project_core_metrics(seat["id"])["outbox_pending"] == 1

    with db.writing() as connection:
        connection.execute(
            "UPDATE project_core_outbox SET state='delivered' WHERE event_id='evt-pending'"
        )
    assert client.delete(f"/api/projects/{pid}").status_code == 200


def test_dead_letter_is_visible_and_owner_can_retry_immutable_event(
    client, tmp_path, monkeypatch
):
    import hashlib
    from app import db, project_core_runtime, store

    pid = _make_project(client, tmp_path).json()["id"]
    seat = client.post(
        f"/api/projects/{pid}/sessions",
        json={"name": "dead letter", "provider": "claude", "working_dir": str(tmp_path)},
    ).json()
    envelope = "{}"
    digest = hashlib.sha256(envelope.encode()).hexdigest()
    now = store.now_iso()
    with db.writing() as connection:
        connection.execute(
            """INSERT INTO project_core_outbox(
                   event_id, session_id, association_id, turn_id, event_type,
                   idempotency_key, envelope_json, envelope_sha256, state,
                   attempts, last_error, created_at, updated_at
               ) VALUES ('evt-dead', ?, 'asoc-dead', NULL,
                         'agent.turn_reported', 'dead-letter-retry', ?, ?,
                         'dead_letter', 1, 'HTTP 400: evidence too large', ?, ?)""",
            (seat["id"], envelope, digest, now, now),
        )
    monkeypatch.setattr(
        project_core_runtime.project_core, "deliver_event",
        lambda _event, **_kwargs: {"state": "applied"},
    )
    client.app.state.settings.enable_project_core = True

    visible = client.get("/api/state").json()["projects"][0]["sessions"][0]
    assert visible["project_core_dead_letter_count"] == 1
    assert visible["project_core_dead_letter_error"] == "HTTP 400: evidence too large"

    response = client.post(
        f"/api/sessions/{seat['id']}/project-core/deliveries/retry"
    )

    assert response.status_code == 200
    assert response.json()["requeued"] == 1
    assert response.json()["remaining_dead_letters"] == 0
    with db.connect() as connection:
        delivered = connection.execute(
            "SELECT state, envelope_json, envelope_sha256 FROM project_core_outbox "
            "WHERE event_id='evt-dead'"
        ).fetchone()
    assert delivered["state"] == "delivered"
    assert delivered["envelope_json"] == envelope
    assert delivered["envelope_sha256"] == digest


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
    assert seat["agent_role"] == "general"

    role_seat = client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "planner", "provider": "claude", "working_dir": str(tmp_path),
            "agent_role": "plan", "initial_prompt": "Find the current planning gap.",
        },
    )
    assert role_seat.status_code == 200
    assert role_seat.json()["agent_role"] == "plan"
    assert role_seat.json()["initial_prompt"] == "Find the current planning gap."
    model_seat = client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "selected model", "provider": "codex", "model": "gpt-5.6-sol",
            "working_dir": str(tmp_path),
        },
    )
    assert model_seat.status_code == 200
    assert model_seat.json()["model"] == "gpt-5.6-sol"
    assert client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "bad model", "provider": "codex", "model": "gpt; nope",
            "working_dir": str(tmp_path),
        },
    ).status_code == 400
    assert client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "bad role", "provider": "claude", "working_dir": str(tmp_path),
            "agent_role": "manager",
        },
    ).status_code == 400
    bound = client.post(
        "/api/projects",
        json={
            "name": "Bound", "root_dir": str(tmp_path),
            "project_core_project_id": "prj_1",
            "project_core_project_title": "Research",
        },
    ).json()
    assert client.post(
        f"/api/projects/{bound['id']}/sessions",
        json={
            "name": "custom tracked", "provider": "custom",
            "working_dir": str(tmp_path), "launch_command": "custom-agent",
            "project_core_workstream_id": "rec_1",
            "project_core_workstream_title": "Node",
        },
    ).status_code == 400

    # bad provider / custom-without-command
    assert client.post(f"/api/projects/{pid}/sessions",
                       json={"name": "x", "provider": "nope", "working_dir": str(tmp_path)}).status_code == 400
    assert client.post(f"/api/projects/{pid}/sessions",
                       json={"name": "x", "provider": "custom", "working_dir": str(tmp_path)}).status_code == 400
    # bad working dir
    assert client.post(f"/api/projects/{pid}/sessions",
                       json={"name": "x", "provider": "claude", "working_dir": "rel"}).status_code == 400

    seats = client.get(f"/api/projects/{pid}/sessions").json()
    assert len(seats) == 3


def test_project_core_context_reinjection_calls_same_seat_runtime(
    client, tmp_path, monkeypatch
):
    pid = _make_project(client, tmp_path).json()["id"]
    seat = client.post(
        f"/api/projects/{pid}/sessions",
        json={"name": "seat", "provider": "codex", "working_dir": str(tmp_path)},
    ).json()
    seen = []
    monkeypatch.setattr(
        client.app.state.project_core_runtime, "reinject_context",
        lambda session: seen.append(session["id"]),
    )
    response = client.post(f"/api/sessions/{seat['id']}/project-core/reinject")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert seen == [seat["id"]]


def test_selected_model_reaches_the_native_launch_command(
    client, tmp_path, monkeypatch
):
    from app.routes import sessions as sessions_route

    pid = _make_project(client, tmp_path).json()["id"]
    seat = client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "model seat", "provider": "codex", "model": "gpt-5.6-sol",
            "working_dir": str(tmp_path), "initial_prompt": "context only",
        },
    ).json()
    launched = {}
    monkeypatch.setattr(sessions_route.tmux, "has_session", lambda _name: False)
    monkeypatch.setattr(
        sessions_route.tmux, "new_session",
        lambda name, working_dir, command: launched.update(command=command),
    )
    monkeypatch.setattr(sessions_route.tmux, "require_live_pane", lambda _name: None)

    response = client.post(f"/api/sessions/{seat['id']}/start")

    assert response.status_code == 200
    assert "--model gpt-5.6-sol" in launched["command"]
    assert "context only" in launched["command"]


def test_start_rejects_an_immediately_dead_provider_pane(
    client, tmp_path, monkeypatch,
):
    from app import store, tmux
    from app.routes import sessions as sessions_route

    pid = _make_project(client, tmp_path).json()["id"]
    seat = client.post(
        f"/api/projects/{pid}/sessions",
        json={"name": "crash", "provider": "codex", "working_dir": str(tmp_path)},
    ).json()
    live = {"value": False}
    monkeypatch.setattr(
        sessions_route.tmux, "has_session", lambda _name: live["value"],
    )
    monkeypatch.setattr(sessions_route.tmux, "pane_dead", lambda _name: True)
    monkeypatch.setattr(
        sessions_route.tmux, "new_session",
        lambda _name, _working_dir, _command: live.update(value=True),
    )
    monkeypatch.setattr(
        sessions_route.tmux, "require_live_pane",
        lambda _name: (_ for _ in ()).throw(
            tmux.TmuxError("provider exited during startup (status 2)")
        ),
    )
    monkeypatch.setattr(
        sessions_route.tmux, "kill_session",
        lambda _name: live.update(value=False),
    )

    response = client.post(f"/api/sessions/{seat['id']}/start")

    assert response.status_code == 400
    assert "status 2" in response.json()["detail"]
    assert live["value"] is False
    stored = store.get_session(seat["id"])
    assert stored["started_at"] is None
    assert stored["status"] == "unknown"


def test_first_claude_start_allocates_and_passes_native_session_id(
    client, tmp_path, monkeypatch,
):
    from app import store
    from app.routes import sessions as sessions_route

    pid = _make_project(client, tmp_path).json()["id"]
    seat = client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "claude exact", "provider": "claude",
            "working_dir": str(tmp_path), "initial_prompt": "context",
        },
    ).json()
    native_id = "f220b703-6a77-4161-adc6-6046d09dfbd2"
    monkeypatch.setattr(
        sessions_route.get_provider("claude"), "new_native_session_id",
        lambda: native_id,
    )
    launched = {}
    monkeypatch.setattr(sessions_route.tmux, "has_session", lambda _name: False)
    monkeypatch.setattr(
        sessions_route.tmux, "new_session",
        lambda _name, _working_dir, command: launched.update(command=command),
    )
    monkeypatch.setattr(sessions_route.tmux, "require_live_pane", lambda _name: None)

    response = client.post(f"/api/sessions/{seat['id']}/start")

    assert response.status_code == 200
    assert f"--session-id {native_id}" in launched["command"]
    assert store.get_session(seat["id"])["provider_session_id"] == native_id


def test_project_core_handoff_is_validated_and_persisted(client, tmp_path):
    pid = _make_project(client, tmp_path).json()["id"]
    context = {
        "project_id": "prj_1", "record_id": "rec_1",
        "context_pack_id": "ctx_1", "context_pack_sha256": "a" * 64,
        "correlation_id": "corr_1",
    }
    r = client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "Project Core", "provider": "codex",
            "working_dir": str(tmp_path), "initial_prompt": "Read the handoff file.",
            "project_core": context,
        },
    )
    assert r.status_code == 200
    seat = r.json()
    assert seat["initial_prompt"] == "Read the handoff file."
    assert seat["project_core_tracking"] == "on"
    assert json.loads(seat["project_core_json"]) == context

    bad = client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "bad", "provider": "codex", "working_dir": str(tmp_path),
            "initial_prompt": "prompt", "launch_command": "codex --other",
        },
    )
    assert bad.status_code == 400
    unknown = client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "bad metadata", "provider": "codex", "working_dir": str(tmp_path),
            "project_core": {"local_path": "/secret"},
        },
    )
    assert unknown.status_code == 400


def test_selected_workstream_uses_project_core_registration(
    client, settings, tmp_path, monkeypatch
):
    from app.routes import sessions as sessions_route

    settings.enable_project_core = True
    observed = {}

    def fake_register(
        session, *, runtime_file, data_dir, tracking_mode, selected_target,
        startup_context,
    ):
        observed.update(
            session_id=session["id"], working_dir=session["working_dir"],
            runtime_file=runtime_file, data_dir=data_dir,
            tracking_mode=tracking_mode,
            selected_target=selected_target,
            startup_context=startup_context,
        )
        return {
            "project_core": {
                "registration_status": "registered",
                "association_id": "asoc_1", "project_id": "prj_1",
                "record_id": "rec_1", "context_pack_id": "ctx_1",
                "context_pack_sha256": "a" * 64, "correlation_id": "corr_1",
                "provider": "agent-hub", "provider_instance": "agent-hub-test",
                "principal_external_id": "agent-hub-runtime",
                "principal_kind": "service", "association_segment": 1,
            },
            "initial_prompt": "Read the registered Context Pack.",
        }

    monkeypatch.setattr(
        sessions_route.project_core_client, "auto_register_session", fake_register
    )
    pid = client.post(
        "/api/projects",
        json={
            "name": "Proj", "root_dir": str(tmp_path),
            "project_core_project_id": "prj_1",
            "project_core_project_title": "Research",
        },
    ).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "native", "provider": "codex", "working_dir": str(tmp_path),
            "project_core_workstream_id": "rec_1",
            "project_core_workstream_title": "Relevant work",
        },
    )
    assert response.status_code == 200
    session = response.json()
    assert json.loads(session["project_core_json"])["association_id"] == "asoc_1"
    assert session["initial_prompt"].startswith("Read the registered Context Pack.")
    assert "PROJECT_CORE_REPORT_CONTRACT_V3" in session["initial_prompt"]
    assert session["project_core_lifecycle"] == "registered"
    assert observed["session_id"] == session["id"]
    assert observed["working_dir"] == str(tmp_path)
    assert observed["data_dir"] == settings.data_dir
    assert observed["tracking_mode"] == "on"
    assert observed["selected_target"] == {
        "project_id": "prj_1", "project_title": "Research",
        "record_id": "rec_1", "workstream_title": "Relevant work",
    }
    assert observed["startup_context"]["assignment"] == {
        "mode": "context_only", "task": "",
    }


def test_context_preview_uses_the_same_composer_as_a_real_seat(
    client, settings, tmp_path, monkeypatch
):
    """A preview assembled separately is a preview that can disagree."""
    settings.enable_project_core = True
    from app.routes import projects as projects_route

    observed = {}

    def preview(**kwargs):
        observed.update(kwargs)
        return {
            "bootstrap": (
                "[PROJECT_CORE_AGENT_STARTUP_V1]\n"
                "Work target: Project Core > Context handling\n"
                "Seat role: implementation\n"
                "Exact Context Pack handoff: /PROJECT_CORE_PREVIEW/handoff.json\n"
                "Context is not execution authorization"
            ),
            "startup_bundle": {"brief_snapshot": None},
        }

    monkeypatch.setattr(
        projects_route.project_core_client, "preview_agent_startup", preview,
    )
    response = client.post(
        "/api/project-core/context-preview",
        json={
            "project_id": "prj_1", "project_title": "Project Core",
            "workstream_id": "rec_1", "workstream_title": "Context handling",
            "agent_role": "implement", "current_state": "four layers deep",
            "next_steps": ["map briefs to directories"],
        },
    )
    assert response.status_code == 200
    text = response.json()["bootstrap"]
    assert text.startswith("[PROJECT_CORE_AGENT_STARTUP_V1]")
    assert "Work target: Project Core > Context handling" in text
    assert "Seat role: implementation" in text
    assert "four layers deep" not in text
    assert "map briefs to directories" not in text
    assert "Context is not execution authorization" in text
    # The pack does not exist yet, and the preview says so rather than naming a
    # path that nothing will ever write.
    assert response.json()["pack_path_placeholder"] in text
    assert observed["startup_context"]["assignment"]["mode"] == "context_only"
    assert client.post(
        "/api/project-core/context-preview",
        json={"agent_role": "director"},
    ).status_code == 400


def test_a_seat_may_track_another_project_bound_to_the_same_directory(
    client, settings, tmp_path, monkeypatch
):
    """One directory, several Project Core Projects, all of them pickable."""
    from app.routes import sessions as sessions_route

    settings.enable_project_core = True
    observed = {}

    def fake_register(
        session, *, runtime_file, data_dir, tracking_mode, selected_target,
        startup_context,
    ):
        observed["selected_target"] = selected_target
        return {
            "project_core": {"registration_status": "unavailable"},
            "initial_prompt": session.get("initial_prompt", ""),
        }

    monkeypatch.setattr(
        sessions_route.project_core_client, "auto_register_session", fake_register
    )
    pid = client.post(
        "/api/projects",
        json={
            "name": "Toolchain", "root_dir": str(tmp_path),
            "project_core_project_id": "prj_tool",
            "project_core_project_title": "Toolchain",
        },
    ).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "release work", "provider": "codex", "working_dir": str(tmp_path),
            "project_core_workstream_id": "rec_release",
            "project_core_workstream_title": "Measure and release",
            "project_core_project_id": "prj_release",
            "project_core_project_title": "Measurement Release",
        },
    )
    assert response.status_code == 200
    # The seat carries the Project its Workstream belongs to, not the one the
    # Agent Hub project happens to be bound to.
    assert observed["selected_target"] == {
        "project_id": "prj_release", "project_title": "Measurement Release",
        "record_id": "rec_release", "workstream_title": "Measure and release",
    }


def test_a_workstream_without_any_project_is_refused(client, settings, tmp_path):
    settings.enable_project_core = True
    pid = client.post(
        "/api/projects", json={"name": "Unbound", "root_dir": str(tmp_path)},
    ).json()["id"]
    assert client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "no project", "provider": "codex", "working_dir": str(tmp_path),
            "project_core_workstream_id": "rec_1",
            "project_core_workstream_title": "Node",
        },
    ).status_code == 400


def test_project_core_origin_handoff_is_adopted_and_gets_report_contract(
    client, settings, tmp_path, monkeypatch
):
    from app.routes import sessions as sessions_route

    settings.enable_project_core = True
    observed = {}

    def fake_adopt(session, **_kwargs):
        observed["session_id"] = session["id"]
        return {
            "project_core": {
                "registration_status": "registered", "association_id": "asoc_pc",
                "association_segment": 1, "project_id": "prj_pc", "record_id": "rec_pc",
                "context_pack_id": "ctx_pc", "context_pack_sha256": "b" * 64,
                "correlation_id": "corr_pc", "provider": "agent-hub",
                "provider_instance": "agent-hub-test",
                "principal_external_id": "agent-hub:runtime", "principal_kind": "service",
                "handoff_path": str(tmp_path / "handoff.json"),
            },
            "initial_prompt": "Read the exact Project Core handoff.",
        }

    monkeypatch.setattr(
        sessions_route.project_core_client, "adopt_handoff_session", fake_adopt
    )
    pid = _make_project(client, tmp_path).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "pc-origin", "provider": "codex", "working_dir": str(tmp_path),
            "initial_prompt": "Original handoff",
            "project_core": {
                "project_id": "prj_pc", "record_id": "rec_pc",
                "context_pack_id": "ctx_pc", "context_pack_sha256": "b" * 64,
                "correlation_id": "corr_pc",
            },
        },
    )
    assert response.status_code == 200
    seat = response.json()
    assert observed["session_id"] == seat["id"]
    assert seat["project_core_lifecycle"] == "registered"
    assert "PROJECT_CORE_REPORT_CONTRACT_V3" in seat["initial_prompt"]


def test_project_core_origin_handoff_retry_preserves_exact_authorization(
    client, settings, tmp_path, monkeypatch
):
    from app.routes import sessions as sessions_route

    settings.enable_project_core = True
    calls = []

    def fake_adopt(session, **_kwargs):
        metadata = json.loads(session["project_core_json"])
        calls.append(metadata["context_pack_id"])
        if len(calls) == 1:
            return {
                "project_core": {**metadata, "registration_status": "unavailable"},
                "initial_prompt": session["initial_prompt"],
            }
        return {
            "project_core": {
                **metadata, "registration_status": "registered",
                "association_id": "asoc_retry", "association_segment": 1,
                "maximum_visibility": "private", "provider": "agent-hub",
                "provider_instance": "agent-hub-test",
                "principal_external_id": "agent-hub-runtime",
                "principal_kind": "service",
                "handoff_path": str(tmp_path / "handoff.json"),
            },
            "initial_prompt": session["initial_prompt"],
        }

    monkeypatch.setattr(
        sessions_route.project_core_client, "adopt_handoff_session", fake_adopt
    )
    monkeypatch.setattr(
        sessions_route.project_core_client, "auto_register_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an exact handoff retry must not fall back to cwd discovery")
        ),
    )
    pid = _make_project(client, tmp_path).json()["id"]
    context = {
        "project_id": "prj_pc", "record_id": "rec_pc",
        "context_pack_id": "ctx_exact", "context_pack_sha256": "c" * 64,
        "correlation_id": "corr_pc",
    }
    created = client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "pc-retry", "provider": "codex",
            "working_dir": str(tmp_path), "initial_prompt": "Exact handoff",
            "project_core": context,
        },
    ).json()
    assert json.loads(created["project_core_json"])["registration_status"] == "unavailable"
    retried = client.post(
        f"/api/sessions/{created['id']}/project-core/retry"
    )
    assert retried.status_code == 200
    assert json.loads(retried.json()["project_core_json"])["association_id"] == "asoc_retry"
    assert calls == ["ctx_exact", "ctx_exact"]


def test_no_workstream_selection_never_discovers_or_injects(
    client, settings, tmp_path, monkeypatch
):
    from app.routes import sessions as sessions_route

    settings.enable_project_core = True

    monkeypatch.setattr(
        sessions_route.project_core_client, "auto_register_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cwd must not opt an unselected seat into tracking")
        ),
    )
    pid = client.post(
        "/api/projects",
        json={
            "name": "Proj", "root_dir": str(tmp_path),
            "project_core_project_id": "prj_1",
            "project_core_project_title": "Research",
        },
    ).json()["id"]
    created = client.post(
        f"/api/projects/{pid}/sessions",
        json={"name": "suggest", "provider": "codex", "working_dir": str(tmp_path)},
    ).json()
    assert created["project_core_tracking"] == "off"
    assert json.loads(created["project_core_json"])["registration_status"] == "off"
    assert created["initial_prompt"] == ""


def test_project_core_project_binding_roundtrip(client, tmp_path):
    created = client.post(
        "/api/projects",
        json={
            "name": "Tracked", "root_dir": str(tmp_path),
            "project_core_project_id": "prj_research",
            "project_core_project_title": "Research",
        },
    )
    assert created.status_code == 200
    assert created.json()["project_core_tracking"] == "on"
    assert created.json()["project_core_project_id"] == "prj_research"
    assert created.json()["project_core_project_title"] == "Research"
    updated = client.patch(
        f"/api/projects/{created.json()['id']}",
        json={"project_core_project_id": "", "project_core_project_title": ""},
    )
    assert updated.status_code == 200
    assert updated.json()["project_core_tracking"] == "off"
    assert updated.json()["project_core_project_id"] == ""
    assert client.patch(
        f"/api/projects/{created.json()['id']}",
        json={"project_core_tracking": "always"},
    ).status_code == 400


def test_project_core_target_picker_uses_read_only_resolver(
    client, settings, tmp_path, monkeypatch
):
    from app.routes import projects as projects_route

    settings.enable_project_core = True
    seen = {}

    def fake_resolve(*, working_dir, runtime_file):
        seen.update(working_dir=working_dir, runtime_file=runtime_file)
        return {
            "status": "resolved",
            "candidates": [{
                "candidate_id": "cand_1", "project_ref": "prj_1",
                "project_title": "Research", "workstream_ref": "rec_1",
                "workstream_title": "Parallel node", "horizon": "now",
            }],
        }

    monkeypatch.setattr(projects_route.project_core_client, "resolve_targets", fake_resolve)
    response = client.post(
        "/api/project-core/targets", json={"working_dir": str(tmp_path)}
    )
    assert response.status_code == 200
    assert response.json()["candidates"][0]["workstream_ref"] == "rec_1"
    assert seen == {
        "working_dir": str(tmp_path),
        "runtime_file": settings.project_core_runtime_file,
    }


def test_selected_target_retry_preserves_project_and_workstream(
    client, settings, tmp_path, monkeypatch
):
    from app.routes import sessions as sessions_route

    settings.enable_project_core = True
    calls = []

    def fake_register(session, **kwargs):
        target = kwargs["selected_target"]
        calls.append(target)
        if len(calls) == 1:
            return {
                "project_core": {
                    "registration_status": "target_unavailable",
                    "desired_project_id": target["project_id"],
                    "desired_project_title": target["project_title"],
                    "desired_record_id": target["record_id"],
                    "desired_workstream_title": target["workstream_title"],
                },
                "initial_prompt": session["initial_prompt"],
            }
        return {
            "project_core": {
                "registration_status": "registered", "association_id": "asoc_retry",
                "association_segment": 1, "project_id": target["project_id"],
                "project_title": target["project_title"], "record_id": target["record_id"],
                "workstream_title": target["workstream_title"],
                "context_pack_id": "ctx_retry", "context_pack_sha256": "d" * 64,
                "correlation_id": "corr_retry", "maximum_visibility": "team",
                "provider": "agent-hub", "provider_instance": "agent-hub-test",
                "principal_external_id": "agent-hub-runtime", "principal_kind": "service",
                "handoff_path": str(tmp_path / "handoff.json"), "seat_role": "general",
            },
            "initial_prompt": session["initial_prompt"],
        }

    monkeypatch.setattr(
        sessions_route.project_core_client, "auto_register_session", fake_register
    )
    project = client.post(
        "/api/projects",
        json={
            "name": "Tracked", "root_dir": str(tmp_path),
            "project_core_project_id": "prj_1",
            "project_core_project_title": "Research",
        },
    ).json()
    seat = client.post(
        f"/api/projects/{project['id']}/sessions",
        json={
            "name": "worker", "provider": "codex", "working_dir": str(tmp_path),
            "project_core_workstream_id": "rec_parallel",
            "project_core_workstream_title": "Parallel node",
        },
    ).json()
    assert json.loads(seat["project_core_json"])["registration_status"] == "target_unavailable"

    blocked = client.post(f"/api/sessions/{seat['id']}/start")
    assert blocked.status_code == 409
    assert "retry the association or turn off tracking" in blocked.json()["detail"]
    assert sessions_route.store.get_session(seat["id"])["started_at"] is None

    retried = client.post(f"/api/sessions/{seat['id']}/project-core/retry")
    assert retried.status_code == 200
    assert json.loads(retried.json()["project_core_json"])["association_id"] == "asoc_retry"
    assert calls == [calls[0], calls[0]]


def test_tracking_off_skips_project_core_discovery(
    client, settings, tmp_path, monkeypatch
):
    from app.routes import sessions as sessions_route

    settings.enable_project_core = True
    monkeypatch.setattr(
        sessions_route.project_core_client, "auto_register_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("tracking=off must not call Project Core")
        ),
    )
    project = client.post(
        "/api/projects",
        json={
            "name": "Ordinary", "root_dir": str(tmp_path),
            "project_core_tracking": "off",
        },
    ).json()
    response = client.post(
        f"/api/projects/{project['id']}/sessions",
        json={"name": "local", "provider": "codex", "working_dir": str(tmp_path)},
    )
    assert response.status_code == 200
    seat = response.json()
    assert seat["project_core_tracking"] == "off"
    assert json.loads(seat["project_core_json"])["registration_status"] == "off"


def test_remove_restore_and_purge(client, tmp_path):
    from app import store

    pid = _make_project(client, tmp_path).json()["id"]
    sid = client.post(f"/api/projects/{pid}/sessions",
                      json={"name": "exec", "provider": "claude", "working_dir": str(tmp_path)}).json()["id"]
    store.update_provider_session_id(
        sid, "f220b703-6a77-4161-adc6-6046d09dfbd2",
    )

    # remove -> lands in removed_sessions, out of the active list
    client.post(f"/api/sessions/{sid}/remove")
    state = client.get("/api/state").json()["projects"][0]
    assert state["sessions"] == []
    assert [s["id"] for s in state["removed_sessions"]] == [sid]

    # restore defaults to a fresh conversation and a context-only prompt
    client.post(f"/api/sessions/{sid}/restore")
    state = client.get("/api/state").json()["projects"][0]
    assert [s["id"] for s in state["sessions"]] == [sid]
    assert state["removed_sessions"] == []
    assert state["sessions"][0]["started_at"] is None
    assert state["sessions"][0]["resume_prompt_pending"] == 0
    assert state["sessions"][0]["provider_session_id"] == ""
    assert "AGENT_HUB_RESTORE_CONTEXT_ONLY_V1" in state["sessions"][0]["initial_prompt"]

    # purge -> gone for good, and idempotent 404 afterwards
    client.post(f"/api/sessions/{sid}/remove")
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    state = client.get("/api/state").json()["projects"][0]
    assert state["sessions"] == [] and state["removed_sessions"] == []
    assert client.delete(f"/api/sessions/{sid}").status_code == 404


def test_legacy_claude_restore_migrates_to_exact_native_session(
    client, tmp_path, monkeypatch,
):
    from datetime import datetime, timezone

    from app import store
    from app.routes import sessions as sessions_route

    working_dir = tmp_path / "work"
    working_dir.mkdir()
    pid = _make_project(client, working_dir).json()["id"]
    seat = client.post(
        f"/api/projects/{pid}/sessions",
        json={"name": "legacy claude", "provider": "claude", "working_dir": str(working_dir)},
    ).json()
    started = store.mark_started(seat["id"])
    native_id = "f220b703-6a77-4161-adc6-6046d09dfbd2"
    timestamp = datetime.fromisoformat(started["started_at"]).astimezone(
        timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    claude_home = tmp_path / "claude-home"
    project_dir = claude_home / "projects" / str(working_dir).replace("/", "-")
    project_dir.mkdir(parents=True)
    (project_dir / f"{native_id}.jsonl").write_text(
        json.dumps({"type": "mode", "sessionId": native_id}) + "\n"
        + json.dumps({
            "type": "user", "sessionId": native_id,
            "cwd": str(working_dir), "timestamp": timestamp,
        }) + "\n",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    store.mark_removed(seat["id"])

    restored = client.post(
        f"/api/sessions/{seat['id']}/restore",
        json={"conversation_mode": "resume"},
    )
    assert restored.status_code == 200
    assert restored.json()["provider_session_id"] == native_id

    launched = {}
    monkeypatch.setattr(sessions_route.tmux, "has_session", lambda _name: False)
    monkeypatch.setattr(
        sessions_route.tmux, "new_session",
        lambda _name, _working_dir, command: launched.update(command=command),
    )
    monkeypatch.setattr(sessions_route.tmux, "require_live_pane", lambda _name: None)

    response = client.post(f"/api/sessions/{seat['id']}/start")

    assert response.status_code == 200
    assert f"--resume {native_id}" in launched["command"]
    assert "--continue" not in launched["command"]
    assert "AGENT_HUB_RESTORE_CONTEXT_ONLY_V1" in launched["command"]


def test_remove_pins_codex_conversation_for_later_restore(
    client, tmp_path, monkeypatch,
):
    from app import store
    from app.routes import sessions as sessions_route

    pid = _make_project(client, tmp_path).json()["id"]
    seat = client.post(
        f"/api/projects/{pid}/sessions",
        json={"name": "pin", "provider": "codex", "working_dir": str(tmp_path)},
    ).json()
    store.mark_started(seat["id"])
    native_id = "01a02cfd-7f18-77e0-aab9-3e70717a882c"
    monkeypatch.setattr(
        sessions_route.get_provider("codex"), "find_native_session_id",
        lambda _working_dir, _started_at: native_id,
    )

    response = client.post(f"/api/sessions/{seat['id']}/remove")

    assert response.status_code == 200
    assert store.get_session(seat["id"])["provider_session_id"] == native_id


def test_codex_resume_restore_stays_archived_when_exact_thread_is_unknown(
    client, tmp_path, monkeypatch,
):
    from app import store
    from app.routes import sessions as sessions_route

    pid = _make_project(client, tmp_path).json()["id"]
    seat = client.post(
        f"/api/projects/{pid}/sessions",
        json={"name": "unknown", "provider": "codex", "working_dir": str(tmp_path)},
    ).json()
    store.mark_started(seat["id"])
    store.mark_removed(seat["id"])
    monkeypatch.setattr(
        sessions_route.get_provider("codex"), "find_native_session_id",
        lambda _working_dir, _started_at: "",
    )

    response = client.post(
        f"/api/sessions/{seat['id']}/restore",
        json={"conversation_mode": "resume"},
    )

    assert response.status_code == 409
    assert store.get_session(seat["id"])["removed_at"] is not None


def _tracked_restore_metadata(tmp_path, *, association="asoc_old", segment=1):
    return {
        "registration_status": "registered",
        "association_id": association, "association_segment": segment,
        "project_id": "prj_restore", "project_title": "Restore Project",
        "record_id": "rec_restore", "workstream_title": "Restore context",
        "context_pack_id": f"ctx_{association}", "context_pack_sha256": "a" * 64,
        "correlation_id": f"corr_{association}", "provider": "agent-hub",
        "provider_instance": "agent-hub-test",
        "principal_external_id": "agent-hub:runtime", "principal_kind": "service",
        "handoff_path": str(tmp_path / f"{association}.json"), "seat_role": "general",
    }


def _prepare_removed_tracked_seat(client, tmp_path):
    from app import store

    pid = _make_project(client, tmp_path).json()["id"]
    seat = client.post(
        f"/api/projects/{pid}/sessions",
        json={
            "name": "restore-context", "provider": "codex",
            "working_dir": str(tmp_path), "initial_prompt": "OLD ASSIGNMENT",
        },
    ).json()
    metadata = _tracked_restore_metadata(tmp_path)
    store.update_session_project_core(
        seat["id"], project_core=metadata, initial_prompt="OLD ASSIGNMENT",
        project_core_tracking="on",
    )
    store.update_project_core_runtime(seat["id"], lifecycle="active")
    store.mark_started(seat["id"])
    store.update_provider_session_id(
        seat["id"], "01a02cfd-7f18-77e0-aab9-3e70717a882c",
    )
    store.mark_removed(seat["id"])
    store.update_project_core_runtime(seat["id"], lifecycle="finished")
    return seat["id"]


def _mock_latest_restore_registration(monkeypatch, tmp_path):
    from app.routes import sessions as sessions_route

    def register(session, **kwargs):
        assert kwargs["association_segment"] == 2
        assert kwargs["selected_target"] == {
            "project_id": "prj_restore", "project_title": "Restore Project",
            "record_id": "rec_restore", "workstream_title": "Restore context",
        }
        assert "OLD ASSIGNMENT" not in session["initial_prompt"]
        return {
            "project_core": _tracked_restore_metadata(
                tmp_path, association="asoc_new", segment=2,
            ),
            "initial_prompt": session["initial_prompt"] + "\nLATEST ACCEPTED BRIEF",
        }

    monkeypatch.setattr(
        sessions_route.project_core_client, "auto_register_session", register,
    )


def test_tracked_restore_defaults_to_fresh_conversation_with_latest_context(
    client, settings, tmp_path, monkeypatch
):
    from app import store
    from app.routes import sessions as sessions_route

    settings.enable_project_core = True
    sid = _prepare_removed_tracked_seat(client, tmp_path)
    _mock_latest_restore_registration(monkeypatch, tmp_path)

    response = client.post(f"/api/sessions/{sid}/restore")

    assert response.status_code == 200
    restored = response.json()
    assert restored["started_at"] is None
    assert restored["resume_prompt_pending"] == 0
    assert "LATEST ACCEPTED BRIEF" in restored["initial_prompt"]
    assert "OLD ASSIGNMENT" not in restored["initial_prompt"]

    launched = {}
    monkeypatch.setattr(sessions_route.tmux, "has_session", lambda _name: False)
    monkeypatch.setattr(
        sessions_route.tmux, "new_session",
        lambda name, working_dir, command: launched.update(command=command),
    )
    monkeypatch.setattr(sessions_route.tmux, "require_live_pane", lambda _name: None)
    settings.enable_project_core = False
    assert client.post(f"/api/sessions/{sid}/start").status_code == 200
    assert "LATEST ACCEPTED BRIEF" in launched["command"]
    assert "resume --last" not in launched["command"]
    assert store.get_session(sid)["resume_prompt_pending"] == 0


def test_tracked_restore_can_explicitly_resume_old_conversation_with_latest_context(
    client, settings, tmp_path, monkeypatch
):
    from app import store
    from app.routes import sessions as sessions_route

    settings.enable_project_core = True
    sid = _prepare_removed_tracked_seat(client, tmp_path)
    _mock_latest_restore_registration(monkeypatch, tmp_path)

    response = client.post(
        f"/api/sessions/{sid}/restore", json={"conversation_mode": "resume"},
    )

    assert response.status_code == 200
    restored = response.json()
    assert restored["started_at"] is not None
    assert restored["status"] == "exited"
    assert restored["resume_prompt_pending"] == 1
    assert "LATEST ACCEPTED BRIEF" in restored["initial_prompt"]

    launched = {}
    monkeypatch.setattr(sessions_route.tmux, "has_session", lambda _name: False)
    monkeypatch.setattr(
        sessions_route.tmux, "new_session",
        lambda name, working_dir, command: launched.update(command=command),
    )
    monkeypatch.setattr(sessions_route.tmux, "require_live_pane", lambda _name: None)
    settings.enable_project_core = False
    assert client.post(f"/api/sessions/{sid}/start").status_code == 200
    assert "resume 01a02cfd-7f18-77e0-aab9-3e70717a882c" in launched["command"]
    assert "resume --last" not in launched["command"]
    assert "LATEST ACCEPTED BRIEF" in launched["command"]
    assert store.get_session(sid)["resume_prompt_pending"] == 0


def test_restore_ui_makes_fresh_the_default_and_resume_an_option(client):
    script = client.get("/static/app.js").text
    assert 'restore(seat, "fresh")' in script
    assert '"最新上下文重开"' in script
    assert 'restore(seat, "resume")' in script
    assert '"继续旧对话"' in script


def test_project_navigation_assets_support_bookmarkable_project_views(client):
    page = client.get("/").text
    script = client.get("/static/app.js").text
    styles = client.get("/static/styles.css").text

    assert 'id="project-sidebar"' in page
    assert 'id="project-nav"' in page
    assert 'function selectProject(projectId)' in script
    assert 'function applyNavigationIntent(intent, projects)' in script
    assert 'window.name = "agent-hub"' in script
    assert 'id="btn-restart"' in page
    assert '"/api/system/restart"' in script
    assert "state.instance_id !== accepted.instance_id" in script
    assert 'PROJECT_HASH_KEY = "project"' in script
    assert 'state.navigation_intent' in script
    assert 'pipeline.project_id === selectedId' in script
    assert 'selectProject(created.id)' in script
    assert '.project-sidebar' in styles
    assert '.project-nav-item.selected' in styles


def test_tracked_restore_waits_for_reassociation_when_project_core_is_off(
    client, settings, tmp_path
):
    sid = _prepare_removed_tracked_seat(client, tmp_path)
    assert settings.enable_project_core is False

    restored = client.post(f"/api/sessions/{sid}/restore").json()
    metadata = json.loads(restored["project_core_json"])

    assert metadata["registration_status"] == "unavailable"
    assert metadata["association_segment"] == 2
    assert metadata["desired_record_id"] == "rec_restore"
    assert client.post(f"/api/sessions/{sid}/start").status_code == 409


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


def test_index_and_script_do_not_expose_the_browser_token(client, settings):
    body = client.get("/").text
    script = client.get("/static/app.js").text
    assert settings.token not in body
    assert settings.token not in script
    assert "window.__AUTH_TOKEN__" not in body
    assert "X-Auth-Token" not in script


def test_browser_bootstrap_cookie_survives_an_app_restart(settings):
    from fastapi.testclient import TestClient
    from app import db
    from app.main import create_app

    first_app = create_app(settings)
    with TestClient(first_app, base_url="http://127.0.0.1:8787") as first:
        assert first.get("/", follow_redirects=False).status_code == 401
        bootstrap = first.get(
            f"/?token={settings.token}", follow_redirects=False,
        )
        assert bootstrap.status_code == 303
        cookie = bootstrap.headers["set-cookie"]
        assert "agent_hub=" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "Max-Age=315360000" in cookie
        assert first.get("/api/state").status_code == 200
        saved_cookies = first.cookies

    second_app = create_app(settings)
    with TestClient(second_app, base_url="http://127.0.0.1:8787") as second:
        second.cookies.update(saved_cookies)
        assert second.get("/").status_code == 200
        assert second.get("/api/state").status_code == 200
    db._DB_PATH = None


def test_auth_requires_token(client):
    assert client.get("/api/state", headers={"X-Auth-Token": "wrong"}).status_code == 401


def test_header_authenticated_api_migrates_the_browser_to_a_cookie(client):
    response = client.get("/api/state")
    cookie = response.headers["set-cookie"]
    assert "agent_hub=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Max-Age=315360000" in cookie


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
