import json
import sqlite3
from pathlib import Path

import pytest

from app import transcripts


def _line(path: Path, *rows: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _codex_message(role: str, text: str, *, phase: str = "") -> dict:
    kind = "input_text" if role in {"user", "developer"} else "output_text"
    payload = {
        "type": "message", "id": f"msg-{role}-{text[:4]}", "role": role,
        "content": [{"type": kind, "text": text}],
    }
    if phase:
        payload["phase"] = phase
    return {"timestamp": "2026-08-25T10:00:00Z", "type": "response_item", "payload": payload}


def test_codex_transcript_is_visible_dialogue_with_stable_older_pages(
    tmp_path, monkeypatch,
):
    native_id = "01a03725-d1b9-7683-9c4d-0f84f7e4754a"
    home = tmp_path / "codex"
    rollout = home / "sessions" / "rollout.jsonl"
    _line(
        rollout,
        _codex_message("user", "First question"),
        _codex_message("developer", "Internal instruction"),
        _codex_message("assistant", "Short update", phase="commentary"),
        {"timestamp": "2026-08-25T10:00:01Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "secret_tool"}},
        _codex_message("user", "Second question"),
        _codex_message("assistant", "Answer with ```py\nprint(1)\n```", phase="final_answer"),
    )
    state = home / "state_5.sqlite"
    connection = sqlite3.connect(state)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, created_at INTEGER)"
    )
    connection.execute(
        "INSERT INTO threads VALUES (?,?,?,?)",
        (native_id, str(rollout), str(tmp_path), 1_777_000_000),
    )
    connection.commit(); connection.close()
    monkeypatch.setenv("CODEX_HOME", str(home))
    session = {
        "id": "seat-1", "provider": "codex", "provider_session_id": native_id,
        "working_dir": str(tmp_path), "started_at": "2026-08-25T10:00:00",
    }

    newest = transcripts.read_session_transcript(session, limit=2)
    assert newest["status"] == "available"
    assert newest["total_messages"] == 4
    assert newest["internal_content_omitted"] is True
    assert [item["seq"] for item in newest["messages"]] == [5, 6]
    assert [item["role"] for item in newest["messages"]] == ["user", "assistant"]
    assert newest["messages"][-1]["phase"] == "final_answer"
    assert "print(1)" in newest["messages"][-1]["content"]
    assert newest["next_before"] == 5
    assert "Internal instruction" not in repr(newest)
    assert "secret_tool" not in repr(newest)

    older = transcripts.read_session_transcript(
        session, before=newest["next_before"], limit=2,
    )
    assert [item["seq"] for item in older["messages"]] == [1, 3]
    assert older["next_before"] is None
    assert older["messages"][0]["content"] == "First question"


def test_claude_transcript_reads_text_but_not_thinking_or_tool_results(
    tmp_path, monkeypatch,
):
    native_id = "3505ebb5-bc89-4586-bd7c-f2cc338e7d5e"
    home = tmp_path / "claude"
    cwd = tmp_path / "workspace"
    project_dir = home / "projects" / str(cwd.resolve()).replace("/", "-")
    _line(
        project_dir / f"{native_id}.jsonl",
        {"type": "user", "uuid": "u1", "timestamp": "2026-08-25T10:00:00Z",
         "message": {"role": "user", "content": "Please review this"}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-08-25T10:00:01Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "private chain"},
             {"type": "text", "text": "Review complete"},
             {"type": "tool_use", "name": "Read", "input": {"file": "secret"}},
         ]}},
        {"type": "user", "uuid": "tool", "timestamp": "2026-08-25T10:00:02Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "content": "secret result"},
         ]}},
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    session = {
        "id": "seat-2", "provider": "claude", "provider_session_id": native_id,
        "working_dir": str(cwd), "started_at": "2026-08-25T10:00:00",
    }

    result = transcripts.read_session_transcript(session)
    assert result["status"] == "available"
    assert [item["content"] for item in result["messages"]] == [
        "Please review this", "Review complete",
    ]
    assert "private chain" not in repr(result)
    assert "secret result" not in repr(result)


def test_provider_machine_user_envelopes_are_not_rendered(tmp_path, monkeypatch):
    codex_id = "01a03725-d1b9-7683-9c4d-0f84f7e4754a"
    codex_home = tmp_path / "codex"
    rollout = codex_home / "sessions" / "rollout.jsonl"
    _line(
        rollout,
        _codex_message(
            "user", "# AGENTS.md instructions for /work\n"
            "<environment_context><cwd>/secret</cwd></environment_context>",
        ),
        _codex_message("user", "<environment_context>private</environment_context>"),
        _codex_message(
            "user", "[Agent Hub Project Core protocol] The user requested context "
            "reinjection.\n\n[PROJECT_CORE_RUNTIME_BINDINGS_V1]\nprivate runtime",
        ),
        _codex_message(
            "user", "[PROJECT_CORE_REPORT_CONTRACT_V3]\nprivate reporting contract",
        ),
        _codex_message("user", "Human question"),
        _codex_message("assistant", "Human answer"),
    )
    state = codex_home / "state_5.sqlite"
    connection = sqlite3.connect(state)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, created_at INTEGER)"
    )
    connection.execute(
        "INSERT INTO threads VALUES (?,?,?,?)",
        (codex_id, str(rollout), str(tmp_path), 1_777_000_000),
    )
    connection.commit(); connection.close()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    codex = transcripts.read_session_transcript({
        "id": "codex-envelope", "provider": "codex",
        "provider_session_id": codex_id, "working_dir": str(tmp_path),
        "started_at": "2026-08-25T10:00:00",
    })

    claude_id = "3505ebb5-bc89-4586-bd7c-f2cc338e7d5e"
    claude_home = tmp_path / "claude"
    cwd = tmp_path / "workspace"
    project_dir = claude_home / "projects" / str(cwd.resolve()).replace("/", "-")
    _line(
        project_dir / f"{claude_id}.jsonl",
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": (
                "<system-reminder>private reminder</system-reminder>\nHuman request"
            )},
        ]}},
        {"type": "assistant", "message": {
            "role": "assistant", "content": "Human response",
        }},
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    claude = transcripts.read_session_transcript({
        "id": "claude-envelope", "provider": "claude",
        "provider_session_id": claude_id, "working_dir": str(cwd),
        "started_at": "2026-08-25T10:00:00",
    })

    assert [item["content"] for item in codex["messages"]] == [
        "Human question", "Human answer",
    ]
    assert [item["seq"] for item in codex["messages"]] == [5, 6]
    assert [item["content"] for item in claude["messages"]] == [
        "Human request", "Human response",
    ]
    assert "environment_context" not in repr(codex)
    assert "system-reminder" not in repr(claude)


def test_raw_event_cursor_keeps_segment_boundary_stable_when_filters_change(
    tmp_path, monkeypatch,
):
    native_id = "01a03725-d1b9-7683-9c4d-0f84f7e4754a"
    home = tmp_path / "codex"
    rollout = home / "sessions" / "rollout.jsonl"
    initial_rows = (
        _codex_message("user", "Old question"),
        _codex_message("developer", "Old internal instruction"),
        _codex_message("assistant", "Old answer"),
    )
    _line(rollout, *initial_rows)
    state = home / "state_5.sqlite"
    connection = sqlite3.connect(state)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, created_at INTEGER)"
    )
    connection.execute(
        "INSERT INTO threads VALUES (?,?,?,?)",
        (native_id, str(rollout), str(tmp_path), 1_777_000_000),
    )
    connection.commit(); connection.close()
    monkeypatch.setenv("CODEX_HOME", str(home))
    session = {
        "id": "stable-boundary", "provider": "codex",
        "provider_session_id": native_id, "working_dir": str(tmp_path),
        "started_at": "2026-08-25T10:00:00",
    }
    boundary = transcripts.session_transcript_cursor(session)
    _line(
        rollout,
        *initial_rows,
        _codex_message("user", "<environment_context>new internal</environment_context>"),
        _codex_message("assistant", "New segment answer"),
    )

    old_segment = transcripts.read_session_transcript(session, after=0, through=boundary)
    new_segment = transcripts.read_session_transcript(session, after=boundary)

    assert boundary == 3
    assert [item["content"] for item in old_segment["messages"]] == [
        "Old question", "Old answer",
    ]
    assert [item["seq"] for item in old_segment["messages"]] == [1, 3]
    assert [item["content"] for item in new_segment["messages"]] == [
        "New segment answer",
    ]
    assert [item["seq"] for item in new_segment["messages"]] == [5]


def test_claude_exact_identity_never_falls_back_to_a_nearby_transcript(
    tmp_path, monkeypatch,
):
    requested_id = "3505ebb5-bc89-4586-bd7c-f2cc338e7d5e"
    nearby_id = "f220b703-6a77-4161-adc6-6046d09dfbd2"
    home = tmp_path / "claude"
    cwd = tmp_path / "workspace"
    project_dir = home / "projects" / str(cwd.resolve()).replace("/", "-")
    _line(
        project_dir / f"{nearby_id}.jsonl",
        {
            "type": "user", "uuid": "wrong",
            "cwd": str(cwd.resolve()), "timestamp": "2026-08-25T10:00:00Z",
            "message": {"role": "user", "content": "Wrong conversation"},
        },
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))

    result = transcripts.read_session_transcript({
        "id": "seat-exact", "provider": "claude",
        "provider_session_id": requested_id, "working_dir": str(cwd),
        "started_at": "2026-08-25T10:00:00+00:00",
    })

    assert result["status"] == "unavailable"
    assert result["reason"] == "provider_session_not_found"
    assert "Wrong conversation" not in repr(result)

    malformed = transcripts.read_session_transcript({
        "id": "seat-malformed", "provider": "claude",
        "provider_session_id": "bad-id", "working_dir": str(cwd),
        "started_at": "2026-08-25T10:00:00+00:00",
    })
    assert malformed["status"] == "unavailable"
    assert malformed["reason"] == "invalid_provider_session_id"
    assert "Wrong conversation" not in repr(malformed)


def test_codex_malformed_exact_identity_never_uses_nearest_thread(
    tmp_path, monkeypatch,
):
    from datetime import datetime

    nearby_id = "01a03725-d1b9-7683-9c4d-0f84f7e4754a"
    home = tmp_path / "codex"
    rollout = home / "sessions" / "nearby.jsonl"
    _line(rollout, _codex_message("user", "Wrong conversation"))
    state = home / "state_5.sqlite"
    connection = sqlite3.connect(state)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, created_at INTEGER)"
    )
    connection.execute(
        "INSERT INTO threads VALUES (?,?,?,?)",
        (
            nearby_id, str(rollout), str(tmp_path),
            int(datetime.fromisoformat("2026-08-25T10:00:00").timestamp()),
        ),
    )
    connection.commit(); connection.close()
    monkeypatch.setenv("CODEX_HOME", str(home))

    result = transcripts.read_session_transcript({
        "id": "codex-malformed", "provider": "codex",
        "provider_session_id": "bad-id", "working_dir": str(tmp_path),
        "started_at": "2026-08-25T10:00:00",
    })

    assert result["status"] == "unavailable"
    assert result["reason"] == "invalid_provider_session_id"
    assert "Wrong conversation" not in repr(result)


def test_claude_transcript_rejects_symlink_escape(tmp_path, monkeypatch):
    native_id = "3505ebb5-bc89-4586-bd7c-f2cc338e7d5e"
    home = tmp_path / "claude"
    cwd = tmp_path / "workspace"
    project_dir = home / "projects" / str(cwd.resolve()).replace("/", "-")
    project_dir.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    _line(
        outside,
        {"type": "user", "message": {"role": "user", "content": "escape"}},
    )
    (project_dir / f"{native_id}.jsonl").symlink_to(outside)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))

    result = transcripts.read_session_transcript({
        "id": "seat-symlink", "provider": "claude",
        "provider_session_id": native_id, "working_dir": str(cwd),
        "started_at": "2026-08-25T10:00:00+00:00",
    })

    assert result["status"] == "unavailable"
    assert "escape" not in repr(result)


def test_transcript_reports_unsupported_and_missing_provider_records(tmp_path, monkeypatch):
    unsupported = transcripts.read_session_transcript({
        "id": "custom", "provider": "custom", "provider_session_id": "",
    })
    assert unsupported["status"] == "unavailable"
    assert unsupported["reason"] == "unsupported_provider"

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex"))
    missing = transcripts.read_session_transcript({
        "id": "missing", "provider": "codex", "provider_session_id": "",
        "working_dir": str(tmp_path), "started_at": "",
    })
    assert missing["status"] == "unavailable"
    assert missing["reason"] == "provider_index_unavailable"


def test_transcript_page_budget_keeps_the_newest_messages_and_a_stable_cursor():
    messages = [
        transcripts._message(
            seq, "assistant", "\0" * (transcripts.MAX_MESSAGE_CHARS + 10),
            message_id=f"message-{seq}", created_at="2026-08-25T10:00:00Z",
            phase="final_answer",
        )
        for seq in range(1, 4)
    ]

    page = transcripts._bounded_page(messages, limit=100)

    assert [item["seq"] for item in page] == [3]
    assert page[0]["truncated"] is True
    assert len(page[0]["content"]) == transcripts.MAX_MESSAGE_CHARS
    assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) < 3_000_000


def test_session_transcript_api_uses_the_authenticated_seat_record(
    client, tmp_path, monkeypatch,
):
    from app.routes import sessions as session_routes

    project = client.post(
        "/api/projects", json={"name": "Transcript", "root_dir": str(tmp_path)},
    ).json()
    seat = client.post(
        f"/api/projects/{project['id']}/sessions",
        json={"name": "History seat", "provider": "codex", "working_dir": str(tmp_path)},
    ).json()
    seen = {}

    association_id = "asoc_transcript"
    from app import store
    store.update_session_project_core(
        seat["id"],
        project_core={
            "registration_status": "registered",
            "association_id": association_id,
            "association_segment": 1,
        },
        initial_prompt="",
        project_core_tracking="on",
    )
    native_id = "01a03725-d1b9-7683-9c4d-0f84f7e4754a"
    store.update_provider_session_id(seat["id"], native_id)
    store.bind_session_conversation(
        seat["id"], provider="codex", provider_session_id=native_id,
        association_id=association_id, association_segment=1,
        start_message_seq=0,
    )

    def fake_transcript(session, *, before, limit, after, through):
        seen.update(
            id=session["id"], before=before, limit=limit,
            after=after, through=through,
        )
        return {"schema_version": 1, "status": "available", "messages": []}

    monkeypatch.setattr(
        session_routes.transcripts, "read_session_transcript", fake_transcript,
    )
    response = client.get(
        f"/api/sessions/{seat['id']}/project-core/associations/"
        f"{association_id}/transcript?before=9&limit=7"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "available"
    assert seen == {
        "id": seat["id"], "before": 9, "limit": 7,
        "after": 0, "through": None,
    }


def test_conversation_generations_and_resumed_association_bounds_are_immutable(
    client, tmp_path,
):
    from app import store

    project = client.post(
        "/api/projects", json={"name": "Bounds", "root_dir": str(tmp_path)},
    ).json()
    seat = client.post(
        f"/api/projects/{project['id']}/sessions",
        json={"name": "Bounded", "provider": "codex", "working_dir": str(tmp_path)},
    ).json()
    first_native = "01a03725-d1b9-7683-9c4d-0f84f7e4754a"
    second_native = "01a02cfd-7f18-77e0-aab9-3e70717a882c"

    first = store.bind_session_conversation(
        seat["id"], provider="codex", provider_session_id=first_native,
        association_id="asoc_first", association_segment=1, start_message_seq=0,
    )
    resumed = store.bind_session_conversation(
        seat["id"], provider="codex", provider_session_id=first_native,
        association_id="asoc_resumed", association_segment=2, start_message_seq=4,
    )
    fresh = store.bind_session_conversation(
        seat["id"], provider="codex", provider_session_id=second_native,
        association_id="asoc_fresh", association_segment=3, start_message_seq=0,
    )
    bindings = {
        item["association_id"]: item
        for item in store.list_session_conversation_bindings(seat["id"])
    }

    assert first["conversation_id"] == resumed["conversation_id"]
    assert fresh["conversation_id"] != first["conversation_id"]
    assert bindings["asoc_first"]["end_message_seq"] == 4
    assert bindings["asoc_resumed"]["start_message_seq"] == 4
    assert bindings["asoc_first"]["generation"] == 1
    assert bindings["asoc_resumed"]["generation"] == 1
    assert bindings["asoc_fresh"]["generation"] == 2
    with pytest.raises(ValueError, match="must be a UUID"):
        store.bind_session_conversation(
            seat["id"], provider="codex", provider_session_id="bad-id",
            association_id="asoc_invalid", association_segment=4,
            start_message_seq=0,
        )


def test_old_association_route_keeps_old_native_identity_after_fresh_restore(
    client, tmp_path, monkeypatch,
):
    from app import store
    from app.routes import sessions as session_routes

    project = client.post(
        "/api/projects", json={"name": "Identity", "root_dir": str(tmp_path)},
    ).json()
    seat = client.post(
        f"/api/projects/{project['id']}/sessions",
        json={"name": "Identity", "provider": "codex", "working_dir": str(tmp_path)},
    ).json()
    other = client.post(
        f"/api/projects/{project['id']}/sessions",
        json={"name": "Other", "provider": "codex", "working_dir": str(tmp_path)},
    ).json()
    old_native = "01a03725-d1b9-7683-9c4d-0f84f7e4754a"
    new_native = "01a02cfd-7f18-77e0-aab9-3e70717a882c"
    store.bind_session_conversation(
        seat["id"], provider="codex", provider_session_id=old_native,
        association_id="asoc_old", association_segment=1, start_message_seq=0,
    )
    store.update_provider_session_id(seat["id"], new_native)
    seen = []

    def fake_transcript(session, **_kwargs):
        seen.append(session["provider_session_id"])
        return {
            "schema_version": 1, "status": "available", "messages": [],
            "total_messages": 0, "returned": 0, "next_before": None,
            "has_earlier": False,
        }

    monkeypatch.setattr(
        session_routes.transcripts, "read_session_transcript", fake_transcript,
    )
    path = (
        f"/api/sessions/{seat['id']}/project-core/associations/"
        "asoc_old/transcript"
    )

    assert client.get(path).status_code == 200
    assert seen == [old_native]
    cross_seat = client.get(
        f"/api/sessions/{other['id']}/project-core/associations/"
        "asoc_old/transcript"
    )
    assert cross_seat.status_code == 404
    assert seen == [old_native]


def test_failed_fresh_launch_does_not_publish_a_conversation_binding(
    client, tmp_path, monkeypatch,
):
    from app import store, tmux
    from app.routes import sessions as session_routes

    project = client.post(
        "/api/projects", json={"name": "Failed launch", "root_dir": str(tmp_path)},
    ).json()
    seat = client.post(
        f"/api/projects/{project['id']}/sessions",
        json={"name": "Fresh", "provider": "claude", "working_dir": str(tmp_path)},
    ).json()
    store.update_session_project_core(
        seat["id"],
        project_core={
            "registration_status": "registered",
            "association_id": "asoc_failed",
            "association_segment": 1,
        },
        initial_prompt="",
    )
    native_id = "f220b703-6a77-4161-adc6-6046d09dfbd2"
    provider = session_routes.get_provider("claude")
    monkeypatch.setattr(provider, "new_native_session_id", lambda: native_id)
    live = {"value": False}
    monkeypatch.setattr(
        session_routes.tmux, "has_session", lambda _name: live["value"],
    )
    monkeypatch.setattr(session_routes.tmux, "pane_dead", lambda _name: True)
    monkeypatch.setattr(
        session_routes.tmux, "new_session",
        lambda _name, _working_dir, _command: live.update(value=True),
    )
    monkeypatch.setattr(
        session_routes.tmux, "require_live_pane",
        lambda _name: (_ for _ in ()).throw(tmux.TmuxError("launch failed")),
    )
    monkeypatch.setattr(
        session_routes.tmux, "kill_session",
        lambda _name: live.update(value=False),
    )

    response = client.post(f"/api/sessions/{seat['id']}/start")

    assert response.status_code == 400
    assert store.get_session(seat["id"])["provider_session_id"] == ""
    assert store.get_session_conversation_binding("asoc_failed") is None
    assert store.list_session_conversation_bindings(seat["id"]) == []
