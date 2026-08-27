from app.providers.registry import get_provider


def _project(client, tmp_path):
    return client.post(
        "/api/projects", json={"name": "permissions", "root_dir": str(tmp_path)},
    ).json()


def test_provider_options_expose_only_supported_permission_modes(client):
    options = {
        item["name"]: item for item in client.get("/api/provider-options").json()
    }
    assert options["codex"]["permission_modes"] == ["default", "unrestricted"]
    assert options["claude"]["permission_modes"] == ["default", "unrestricted"]
    assert options["hermes"]["permission_modes"] == ["default"]


def test_provider_options_expose_reasoning_effort_only_when_supported(client):
    options = {
        item["name"]: item for item in client.get("/api/provider-options").json()
    }
    expected = ["low", "medium", "high", "xhigh", "max"]
    assert options["codex"]["reasoning_efforts"] == expected
    assert options["claude"]["reasoning_efforts"] == expected
    assert options["hermes"]["reasoning_efforts"] == []


def test_unrestricted_mode_is_persisted_and_used_on_first_start(
    client, tmp_path, monkeypatch,
):
    from app.routes import sessions as sessions_route

    project = _project(client, tmp_path)
    seat = client.post(
        f"/api/projects/{project['id']}/sessions",
        json={
            "name": "autonomous", "provider": "codex",
            "working_dir": str(tmp_path), "initial_prompt": "Do the task",
            "permission_mode": "unrestricted",
        },
    ).json()
    assert seat["permission_mode"] == "unrestricted"

    launched = {}
    monkeypatch.setattr(sessions_route.tmux, "has_session", lambda _name: False)
    monkeypatch.setattr(
        sessions_route.tmux, "new_session",
        lambda name, working_dir, command: launched.update(command=command),
    )
    monkeypatch.setattr(sessions_route.tmux, "require_live_pane", lambda _name: None)
    response = client.post(f"/api/sessions/{seat['id']}/start")
    assert response.status_code == 200
    assert "--dangerously-bypass-approvals-and-sandbox" in launched["command"]
    assert launched["command"].endswith("'Do the task'")


def test_unrestricted_mode_survives_native_resume_commands():
    codex = get_provider("codex").resolve_resume_command(
        "", permission_mode="unrestricted",
    )
    assert "--dangerously-bypass-approvals-and-sandbox" in codex
    assert codex.endswith("resume --last")

    claude = get_provider("claude").resolve_resume_command(
        "", permission_mode="unrestricted",
    )
    assert "--dangerously-skip-permissions" in claude
    assert claude.endswith("--continue")


def test_reasoning_effort_is_persisted_and_applied_to_native_commands(
    client, tmp_path, monkeypatch,
):
    from app.routes import sessions as sessions_route

    project = _project(client, tmp_path)
    seat = client.post(
        f"/api/projects/{project['id']}/sessions",
        json={
            "name": "deep", "provider": "codex", "working_dir": str(tmp_path),
            "initial_prompt": "Do the task", "reasoning_effort": "xhigh",
        },
    ).json()
    assert seat["reasoning_effort"] == "xhigh"

    launched = {}
    monkeypatch.setattr(sessions_route.tmux, "has_session", lambda _name: False)
    monkeypatch.setattr(
        sessions_route.tmux, "new_session",
        lambda _name, _working_dir, command: launched.update(command=command),
    )
    monkeypatch.setattr(sessions_route.tmux, "require_live_pane", lambda _name: None)
    assert client.post(f"/api/sessions/{seat['id']}/start").status_code == 200
    assert "model_reasoning_effort=\"xhigh\"" in launched["command"]

    claude = get_provider("claude").resolve_initial_command(
        "", "task", reasoning_effort="high",
    )
    assert "--effort high" in claude


def test_unknown_or_unsupported_permission_mode_is_rejected(client, tmp_path):
    project = _project(client, tmp_path)
    base = {
        "name": "bad", "provider": "hermes", "working_dir": str(tmp_path),
    }
    unsupported = client.post(
        f"/api/projects/{project['id']}/sessions",
        json={**base, "permission_mode": "unrestricted"},
    )
    assert unsupported.status_code == 400
    assert "does not support" in unsupported.json()["detail"]

    invalid = client.post(
        f"/api/projects/{project['id']}/sessions",
        json={**base, "name": "invalid", "permission_mode": "root"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "invalid permission mode"
