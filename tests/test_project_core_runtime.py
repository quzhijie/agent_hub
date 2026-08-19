import hashlib
import json
import urllib.error
from pathlib import Path

from app import store
from app.project_core_runtime import ProjectCoreRuntime, submit_checkpoint


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
    assert "PROJECT_CORE_REPORT_CONTRACT_V1" in session["initial_prompt"]
    metadata = json.loads(session["project_core_json"])
    config = Path(metadata["report_config_path"])
    assert config.stat().st_mode & 0o077 == 0

    runtime.session_started(session, first_start=True)
    result = submit_checkpoint(config, _report())
    assert result["turn_seq"] == 1
    turn = store.list_project_core_turns(session["id"])[0]
    assert turn["state"] == "reported"
    assert turn["report_bytes"] > 0

    runtime.observe_status(session, store.ACTIVE, store.DONE, "completed")
    turn = store.list_project_core_turns(session["id"])[0]
    assert turn["settle_kind"] == "completed"
    runtime.close_session(store.get_session(session["id"]), abandoned=False)
    closed = store.get_session(session["id"])
    assert closed["project_core_lifecycle"] == "finished"
    metrics = store.project_core_metrics(session["id"])
    assert metrics["turn_reported"] == 1
    assert metrics["outbox_pending"] == 3  # started, turn report, finished


def test_completion_gate_reminds_once_then_records_missing(
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
    runtime.observe_status(session, store.ACTIVE, store.DONE, "completed")
    runtime.observe_status(session, store.ACTIVE, store.DONE, "completed")
    turn = store.list_project_core_turns(session["id"])[0]
    assert len(messages) == 1
    assert turn["reminder_count"] == 1
    assert turn["state"] == "missing"
    assert "missing" in store.get_session(session["id"])["project_core_report_warning"]


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
    assert "PROJECT_CORE_REPORT_CONTRACT_V1" in messages[0][1]


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
