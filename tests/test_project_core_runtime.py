import hashlib
import json
import urllib.error
from pathlib import Path

from app import store
from app.project_core_runtime import ProjectCoreRuntime, submit_checkpoint
from app.status import new_seat_state, next_status


def _registered_session(tmp_path):
    project = store.create_project("Tracked", str(tmp_path), "on")
    metadata = {
        "registration_status": "registered",
        "association_id": "asoc_runtime1", "association_segment": 1,
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


def test_report_contract_persists_turn_and_lifecycle_outbox(
    store_db, settings, tmp_path
):
    runtime = ProjectCoreRuntime(settings)
    session = runtime.install_contract(_registered_session(tmp_path))
    assert session["project_core_lifecycle"] == "registered"
    assert "PROJECT_CORE_REPORT_CONTRACT_V2" in session["initial_prompt"]
    assert "checkpoints are explicit opt-in" in session["initial_prompt"]
    assert "does not authorize a report" in session["initial_prompt"]
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


def test_installing_v2_supersedes_a_stored_v1_contract(
    store_db, settings, tmp_path
):
    runtime = ProjectCoreRuntime(settings)
    session = _registered_session(tmp_path)
    session = store.update_session_project_core(
        session["id"], project_core=json.loads(session["project_core_json"]),
        initial_prompt="[PROJECT_CORE_REPORT_CONTRACT_V1]\nOld mandatory policy.",
    )

    upgraded = runtime.install_contract(session)

    assert "PROJECT_CORE_REPORT_CONTRACT_V1" in upgraded["initial_prompt"]
    assert "PROJECT_CORE_REPORT_CONTRACT_V2" in upgraded["initial_prompt"]
    assert "supersedes any PROJECT_CORE_REPORT_CONTRACT_V1" in upgraded["initial_prompt"]


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
    assert "PROJECT_CORE_REPORT_CONTRACT_V2" in messages[0][1]


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
