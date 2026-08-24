import hashlib
import io
import json
import urllib.error
from pathlib import Path

from app import store
from app.project_core_runtime import (
    _MAX_EVIDENCE_ITEM_BYTES,
    ProjectCoreRuntime,
    _host_evidence,
    submit_checkpoint,
)
from app.status import new_seat_state, next_status


def _registered_session(tmp_path):
    project = store.create_project("Tracked", str(tmp_path), "on")
    metadata = {
        "registration_status": "registered",
        "association_id": "asoc_runtime1", "association_segment": 1,
        "resource_binding_id": "bind_runtime_workspace",
        "project_id": "prj_runtime", "record_id": "rec_runtime",
        "context_pack_id": "ctx_runtime", "context_pack_sha256": "a" * 64,
        "correlation_id": "corr_runtime", "provider": "agent-hub",
        "provider_instance": "agent-hub-test", "principal_external_id": "agent-hub:runtime",
        "principal_kind": "service",
        "handoff_path": str(tmp_path / "handoff.json"),
        "seat_role": "plan",
    }
    return store.create_session(
        project["id"], "seat", "codex", str(tmp_path), "",
        project_core=metadata, project_core_tracking="on", agent_role="plan",
    )


def _report(summary="Implemented the runtime outbox"):
    return {
        "report_schema_version": 1, "status": "completed", "summary": summary,
        "accomplished": ["Persisted the report before delivery"],
        "decisions_proposed": [], "blockers": [], "next_steps": [],
    }


def _no_change_report(summary="No project change in this turn"):
    return {
        "report_schema_version": 1, "status": "no_change", "summary": summary,
        "accomplished": [], "decisions_proposed": [], "blockers": [],
        "next_steps": [],
    }


def test_report_contract_persists_turn_and_lifecycle_outbox(
    store_db, settings, tmp_path
):
    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    assert session["project_core_lifecycle"] == "registered"
    assert "PROJECT_CORE_REPORT_CONTRACT_V3" in session["initial_prompt"]
    assert "checkpoints are explicit opt-in" in session["initial_prompt"]
    assert "does not authorize a report" in session["initial_prompt"]
    assert "do not claim Project Core received" in session["initial_prompt"]
    assert "An output requires\na read-write binding" in session["initial_prompt"]
    metadata = json.loads(session["project_core_json"])
    config = Path(metadata["report_config_path"])
    assert config.stat().st_mode & 0o077 == 0

    runtime.session_started(session, first_start=True)
    result = submit_checkpoint(config, _report())
    assert result["turn_seq"] == 1
    turn = store.list_project_core_turns(session["id"])[0]
    assert turn["state"] == "reported"
    assert turn["report_bytes"] > 0

    runtime.observe_status(session, store.ACTIVE, store.DONE, "done")
    turn = store.list_project_core_turns(session["id"])[0]
    assert turn["settle_kind"] == "completed"
    runtime.close_session(store.get_session(session["id"]), abandoned=False)
    closed = store.get_session(session["id"])
    assert closed["project_core_lifecycle"] == "finished"
    metrics = store.project_core_metrics(session["id"])
    assert metrics["turn_reported"] == 1
    assert metrics["outbox_pending"] == 3  # started, turn report, finished


def test_pinned_manual_uses_only_a_short_runtime_binding(
    store_db, settings, tmp_path
):
    runtime = ProjectCoreRuntime(settings)
    session = _registered_session(tmp_path)
    metadata = json.loads(session["project_core_json"])
    manual_index = tmp_path / "manual" / "index.json"
    manual_index.parent.mkdir()
    manual_index.write_text("{}\n", encoding="utf-8")
    metadata.update({
        "agent_manual_id": "manual_test",
        "agent_manual_version": "1.0",
        "agent_manual_sha256": "b" * 64,
        "manual_index_path": str(manual_index),
        "manual_index_sha256": "c" * 64,
    })
    session = store.update_session_project_core(
        session["id"], project_core=metadata,
        initial_prompt="[PROJECT_CORE_AGENT_BOOTSTRAP_V2]\nRead the pinned index.",
        project_core_tracking="on",
    )

    installed = runtime.install_contract(session)

    assert "PROJECT_CORE_RUNTIME_BINDINGS_V1" in installed["initial_prompt"]
    assert "checkpoint_command:" in installed["initial_prompt"]
    assert str(manual_index.parent / "tools" / "checkpoint.md") in installed["initial_prompt"]
    assert "PROJECT_CORE_REPORT_CONTRACT_V3" not in installed["initial_prompt"]
    assert '"report_schema_version":1' not in installed["initial_prompt"]


def test_checkpoint_io_is_normalized_and_added_as_portable_evidence(
    store_db, settings, tmp_path
):
    from app import db

    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    config = Path(json.loads(session["project_core_json"])["report_config_path"])
    report = _report()
    report["io"] = [
        {
            "relation": "input", "path": "catalogs//targets.ecsv",
            "slot": "target-catalog", "label": "Target catalog",
        },
        {
            "relation": "output", "path": "outputs/summary.csv",
            "slot": "summary-output", "required": False,
            "resource_binding_id": "bind_shared_catalogs", "sha256": "b" * 64,
        },
    ]

    first = submit_checkpoint(config, report)
    duplicate = submit_checkpoint(config, report)

    assert not first["duplicate"]
    assert duplicate["duplicate"]
    with db.connect() as connection:
        turn = connection.execute(
            "SELECT * FROM project_core_turns WHERE id=?", (first["turn_id"],)
        ).fetchone()
        outbox = connection.execute(
            "SELECT envelope_json FROM project_core_outbox WHERE turn_id=?",
            (first["turn_id"],),
        ).fetchone()
    assert "io" not in json.loads(turn["report_json"])
    evidence = json.loads(turn["evidence_json"])
    declarations = [
        item for item in evidence if item["kind"] == "project_resource_io"
    ]
    assert declarations == [
        {
            "kind": "project_resource_io", "relation": "input",
            "resource_binding_id": "bind_runtime_workspace",
            "path": "catalogs/targets.ecsv", "slot": "target-catalog",
            "label": "Target catalog", "required": True,
        },
        {
            "kind": "project_resource_io", "relation": "output",
            "resource_binding_id": "bind_shared_catalogs",
            "path": "outputs/summary.csv", "slot": "summary-output",
            "required": False, "sha256": "b" * 64,
        },
    ]
    assert json.loads(outbox["envelope_json"])["evidence"] == evidence


def test_git_evidence_is_trimmed_by_canonical_bytes_not_only_path_count():
    paths = [f"nested/{index}/{'x' * 500}.csv" for index in range(20)]
    complete_digest = hashlib.sha256(
        json.dumps(paths, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    evidence = _host_evidence({}, {
        "commit": "a" * 40,
        "dirty": True,
        "changed_count": 2_119,
        "changed_paths": paths,
        "changed_paths_sha256": complete_digest,
    })[0]

    encoded = json.dumps(
        evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()
    assert len(encoded) <= _MAX_EVIDENCE_ITEM_BYTES
    assert len(evidence["changed_paths"]) < len(paths)
    assert evidence["changed_count"] == 2_119
    assert evidence["changed_paths_sha256"] == complete_digest


def test_checkpoint_io_rejects_unsafe_or_no_change_declarations(
    store_db, settings, tmp_path
):
    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    config = Path(json.loads(session["project_core_json"])["report_config_path"])
    base = {
        "relation": "input", "path": "catalogs/input.csv", "slot": "catalog-input",
    }
    invalid = [
        ({**base, "path": "/tmp/input.csv"}, "stay inside"),
        ({**base, "path": "../input.csv"}, "stay inside"),
        ({**base, "relation": "context"}, "input or output"),
        ({**base, "slot": "Bad Slot"}, "valid attachment slot"),
    ]
    import pytest
    for declaration, message in invalid:
        report = _report()
        report["io"] = [declaration]
        with pytest.raises(ValueError, match=message):
            submit_checkpoint(config, report)

    no_change = _no_change_report()
    no_change["io"] = [base]
    with pytest.raises(ValueError, match="no_change.*Resource I/O"):
        submit_checkpoint(config, no_change)


def test_unrequested_checkpoint_window_stays_open_without_a_reminder(
    store_db, settings, tmp_path, monkeypatch
):
    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    runtime.session_started(session, first_start=True)
    messages = []
    monkeypatch.setattr(
        "app.project_core_runtime.tmux.send_protocol_message",
        lambda name, message: messages.append((name, message)),
    )
    runtime.observe_status(session, store.ACTIVE, store.DONE, "done")
    runtime.observe_status(session, store.ACTIVE, store.DONE, "done")
    turn = store.list_project_core_turns(session["id"])[0]
    assert messages == []
    assert turn["reminder_count"] == 0
    assert turn["state"] == "open"
    assert turn["settle_kind"] == ""
    assert store.get_session(session["id"])["project_core_report_warning"] == ""


def test_installing_v3_supersedes_stored_older_contracts(
    store_db, settings, tmp_path
):
    runtime = ProjectCoreRuntime(settings)
    session = _registered_session(tmp_path)
    session = store.update_session_project_core(
        session["id"], project_core=json.loads(session["project_core_json"]),
        initial_prompt=(
            "[PROJECT_CORE_REPORT_CONTRACT_V1]\nOld mandatory policy.\n"
            "[PROJECT_CORE_REPORT_CONTRACT_V2]\nOld opt-in policy."
        ),
    )

    upgraded = runtime.install_contract(session)

    assert "PROJECT_CORE_REPORT_CONTRACT_V1" in upgraded["initial_prompt"]
    assert "PROJECT_CORE_REPORT_CONTRACT_V2" in upgraded["initial_prompt"]
    assert "PROJECT_CORE_REPORT_CONTRACT_V3" in upgraded["initial_prompt"]
    assert "PROJECT_CORE_REPORT_CONTRACT_V1 or" in upgraded["initial_prompt"]


def test_installing_v3_recovers_workspace_binding_from_private_handoff(
    store_db, settings, tmp_path
):
    runtime = ProjectCoreRuntime(settings)
    session = _registered_session(tmp_path)
    metadata = json.loads(session["project_core_json"])
    metadata.pop("resource_binding_id")
    session = store.update_session_project_core(
        session["id"], project_core=metadata, initial_prompt=session["initial_prompt"],
    )
    handoff_dir = settings.data_dir / "project_core_handoffs"
    handoff_dir.mkdir(parents=True)
    (handoff_dir / "asoc_runtime1.json").write_text(json.dumps({
        "association": {
            "id": "asoc_runtime1", "project_ref": "prj_runtime",
            "workstream_ref": "rec_runtime", "context_pack_id": "ctx_runtime",
            "context_pack_sha256": "a" * 64,
            "resource_binding_id": "bind_recovered_workspace",
        }
    }))

    upgraded = runtime.install_contract(session)

    assert json.loads(upgraded["project_core_json"])[
        "resource_binding_id"
    ] == "bind_recovered_workspace"


def test_closing_discards_an_unrequested_checkpoint_window(
    store_db, settings, tmp_path
):
    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    runtime.session_started(session, first_start=True)

    runtime.close_session(store.get_session(session["id"]), abandoned=False)

    assert store.list_project_core_turns(session["id"]) == []
    metrics = store.project_core_metrics(session["id"])
    assert metrics.get("turn_reported", 0) == 0
    assert metrics.get("turn_missing", 0) == 0


def test_two_substantive_turns_accept_different_checkpoint_reports(
    store_db, settings, tmp_path
):
    """The sampler's real DONE edge must close one report before the next turn."""
    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    runtime.session_started(session, first_start=True)
    config = Path(json.loads(session["project_core_json"])["report_config_path"])

    first = submit_checkpoint(config, _report("First substantive turn"))
    debounce = new_seat_state()
    status, _ = next_status(store.IDLE, store.ACTIVE, True, debounce)
    status, _ = next_status(status, store.ACTIVE, True, debounce)
    status, _ = next_status(status, store.IDLE, True, debounce)
    completed, completion_edge = next_status(status, store.IDLE, False, debounce)
    assert (completed, completion_edge) == (store.DONE, "done")
    runtime.observe_status(session, store.ACTIVE, completed, completion_edge)
    runtime.observe_status(session, completed, store.ACTIVE, None)
    second = submit_checkpoint(config, _report("Second substantive turn"))

    assert first["turn_seq"] == 1
    assert second["turn_seq"] == 2
    turns = store.list_project_core_turns(session["id"])
    assert [turn["state"] for turn in turns] == ["reported", "reported"]
    assert turns[0]["settle_kind"] == "completed"
    assert turns[1]["settle_kind"] == ""
    assert json.loads(turns[0]["report_json"])["summary"] == "First substantive turn"
    assert json.loads(turns[1]["report_json"])["summary"] == "Second substantive turn"


def test_checkpoint_repairs_a_missed_done_callback_from_status_history(
    store_db, settings, tmp_path
):
    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    session = store.mark_started(session["id"])
    runtime.session_started(session, first_start=True)
    config = Path(json.loads(session["project_core_json"])["report_config_path"])

    submit_checkpoint(config, _report("First report before the missed callback"))
    store.update_status(session["id"], store.DONE, "finished", activity=True)
    second = submit_checkpoint(config, _report("Second report after the done edge"))

    assert second["turn_seq"] == 2
    turns = store.list_project_core_turns(session["id"])
    assert turns[0]["settle_kind"] == "completed"
    assert [turn["state"] for turn in turns] == ["reported", "reported"]


def test_new_active_episode_does_not_reuse_a_reported_no_change_turn(
    store_db, settings, tmp_path
):
    """An acknowledged idle screen may hide DONE, but new work is a boundary."""
    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    session = store.mark_started(session["id"])
    runtime.session_started(session, first_start=True)
    config = Path(json.loads(session["project_core_json"])["report_config_path"])

    first = submit_checkpoint(config, _no_change_report("Nothing changed before idle"))
    store.update_status(session["id"], store.IDLE, "acknowledged", activity=False)
    runtime.observe_status(session, store.ACTIVE, store.IDLE, None)
    store.update_status(session["id"], store.ACTIVE, "new request", activity=True)
    runtime.observe_status(session, store.IDLE, store.ACTIVE, None)
    second = submit_checkpoint(config, _report("A later turn changed the project"))

    assert first["turn_seq"] == 1
    assert second["turn_seq"] == 2
    turns = store.list_project_core_turns(session["id"])
    assert turns[0]["settle_kind"] == "completed"
    assert turns[1]["settle_kind"] == ""
    assert json.loads(turns[0]["report_json"])["status"] == "no_change"


def test_checkpoint_repairs_missed_idle_to_active_boundary_from_history(
    store_db, settings, tmp_path
):
    """Submission can race the callback after the durable status event write."""
    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    session = store.mark_started(session["id"])
    runtime.session_started(session, first_start=True)
    config = Path(json.loads(session["project_core_json"])["report_config_path"])

    submit_checkpoint(config, _no_change_report("Old no-change report"))
    # Deliberately omit runtime.observe_status: this is the crash/race path in
    # which only the durable status history survives.
    store.update_status(session["id"], store.IDLE, "acknowledged", activity=False)
    store.update_status(session["id"], store.ACTIVE, "new request", activity=True)
    second = submit_checkpoint(config, _report("New report after active resumed"))

    assert second["turn_seq"] == 2
    turns = store.list_project_core_turns(session["id"])
    assert turns[0]["settle_kind"] == "completed"
    assert [turn["state"] for turn in turns] == ["reported", "reported"]


def test_startup_reconciles_reported_turns_left_unsettled(
    store_db, settings, tmp_path, monkeypatch
):
    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    session = store.mark_started(session["id"])
    runtime.session_started(session, first_start=True)
    config = Path(json.loads(session["project_core_json"])["report_config_path"])
    submit_checkpoint(config, _report())
    store.update_status(session["id"], store.WAITING, "question", activity=True)
    monkeypatch.setattr("app.project_core_runtime.tmux.has_session", lambda _name: True)

    runtime.reconcile_on_startup()

    turn = store.list_project_core_turns(session["id"])[0]
    assert turn["settle_kind"] == "waiting_user"


def test_manual_reinjection_resends_snapshot_after_new_or_clear(
    store_db, settings, tmp_path, monkeypatch
):
    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    session = store.mark_started(session["id"])
    messages = []
    monkeypatch.setattr("app.project_core_runtime.tmux.has_session", lambda _name: True)
    monkeypatch.setattr(
        "app.project_core_runtime.tmux.send_protocol_message",
        lambda name, message: messages.append((name, message)),
    )

    runtime.reinject_context(session)

    assert len(messages) == 1
    assert messages[0][0] == session["tmux_session"]
    assert "/new or /clear" in messages[0][1]
    assert "seat role is plan" in messages[0][1]
    assert str(tmp_path / "handoff.json") in messages[0][1]
    assert "does not refresh Project Core state" in messages[0][1]
    assert "PROJECT_CORE_REPORT_CONTRACT_V3" in messages[0][1]


def test_outbox_retries_the_exact_same_envelope(
    store_db, settings, tmp_path, monkeypatch
):
    from app import db
    from app import project_core_runtime as runtime_module

    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    runtime.session_started(session, first_start=True)
    config = Path(json.loads(session["project_core_json"])["report_config_path"])
    submit_checkpoint(config, _report())
    settings.enable_project_core = True
    seen = []

    def unavailable(event, **_kwargs):
        seen.append(json.dumps(event, ensure_ascii=False, sort_keys=True))
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(runtime_module.project_core, "deliver_event", unavailable)
    runtime.flush_outbox(limit=10)
    blocked = runtime.flush_outbox(limit=10)
    assert blocked["processed"] == 0
    assert len(seen) == 1  # a newer due event cannot leapfrog retry backoff
    with db.writing() as connection:
        connection.execute(
            "UPDATE project_core_outbox SET next_attempt_at=NULL WHERE state='pending'"
        )

    def delivered(event, **_kwargs):
        seen.append(json.dumps(event, ensure_ascii=False, sort_keys=True))
        return {"state": "applied"}

    monkeypatch.setattr(runtime_module.project_core, "deliver_event", delivered)
    runtime.flush_outbox(limit=10)
    assert len(seen) == 3  # FIFO stops at the first retryable failure
    assert seen[0] == seen[1]
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT envelope_json, envelope_sha256, state FROM project_core_outbox"
        ).fetchall()
    assert all(row["state"] == "delivered" for row in rows)
    assert all(
        hashlib.sha256(row["envelope_json"].encode()).hexdigest()
        == row["envelope_sha256"] for row in rows
    )


def test_terminal_http_error_keeps_gateway_reason_for_owner_recovery(
    store_db, settings, tmp_path, monkeypatch
):
    from app import db
    from app import project_core_runtime as runtime_module

    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    config = Path(json.loads(session["project_core_json"])["report_config_path"])
    report = submit_checkpoint(config, _report())
    settings.enable_project_core = True

    def rejected(_event, **_kwargs):
        body = json.dumps({
            "error": {
                "type": "invalid_request",
                "message": "agent turn evidence[0] exceeds 4096 bytes",
            }
        }).encode()
        raise urllib.error.HTTPError(
            "http://127.0.0.1/events", 400, "Bad Request", None,
            io.BytesIO(body),
        )

    monkeypatch.setattr(runtime_module.project_core, "deliver_event", rejected)
    result = runtime.flush_outbox(limit=10)

    assert result["dead_letter"] == 1
    with db.connect() as connection:
        row = connection.execute(
            "SELECT state, last_error FROM project_core_outbox WHERE turn_id=?",
            (report["turn_id"],),
        ).fetchone()
    assert row["state"] == "dead_letter"
    assert row["last_error"] == (
        "HTTP 400: agent turn evidence[0] exceeds 4096 bytes"
    )


def test_core_application_dead_letter_is_still_a_delivery_ack(
    store_db, settings, tmp_path, monkeypatch
):
    from app import db
    from app import project_core_runtime as runtime_module

    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    config = Path(json.loads(session["project_core_json"])["report_config_path"])
    report = submit_checkpoint(config, _report())
    settings.enable_project_core = True
    monkeypatch.setattr(
        runtime_module.project_core,
        "deliver_event",
        lambda _event, **_kwargs: {
            "id": "inbox-core-dead-letter",
            "state": "dead_letter",
            "error": "Resource output requires a read-write binding",
        },
    )

    result = runtime.flush_outbox(limit=10)

    assert result["delivered"] == 1
    assert result["dead_letter"] == 0
    with db.connect() as connection:
        row = connection.execute(
            "SELECT state, last_error, delivered_at FROM project_core_outbox WHERE turn_id=?",
            (report["turn_id"],),
        ).fetchone()
    assert row["state"] == "delivered"
    assert row["last_error"] == ""
    assert row["delivered_at"]
