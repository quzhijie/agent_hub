"""Optional real Gateway-to-runtime contract test.

Agent Hub stays deployable without importing Project Core. The test uses its
public Python surface only when the separately installed integration is present;
the production adapter still talks exclusively through signed loopback HTTP.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

from app import project_core as project_core_client, store
from app.project_core_runtime import ProjectCoreRuntime, submit_checkpoint


_SIBLING_PROJECT_CORE = Path(__file__).resolve().parents[2] / "project-core"
if (_SIBLING_PROJECT_CORE / "project_core" / "__init__.py").is_file():
    sys.path.insert(0, str(_SIBLING_PROJECT_CORE))
project_core = pytest.importorskip("project_core")


def test_signed_registration_discovers_manual_and_checkpoint_round_trips(
    store_db, settings, tmp_path
):
    from project_core.model import AccessContext
    from project_core.service import ProjectCore
    from project_core.store import Store
    from project_core.workflow import WorkflowService
    from project_core.workflow_ui import create_workflow_server
    from project_brief import projects as brief_projects, seat as brief_seat

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    core = ProjectCore(Store(tmp_path / "core.db"))
    core.initialize()
    project = core.create_project(slug="agent-manual-e2e", title="Agent Manual E2E")
    owner = core.add_actor(
        project_id=project["id"], name="Owner", kind="human", role="owner",
    )
    access = AccessContext(project["id"], owner["id"], "owner")
    core.add_actor(
        project_id=project["id"], name="Agent Hub", kind="agent",
        role="researcher", external_ref="agent-hub-runtime", access=access,
    )
    resource = core.register_resource(
        project_id=project["id"], actor_id=owner["id"],
        canonical_uri="project-core://agent-manual-e2e", kind="directory",
        owner_project_id=project["id"], local_path=str(workspace),
        machine_key="agent-manual-e2e-machine",
    )
    binding = core.bind_resource(
        project_id=project["id"], actor_id=owner["id"],
        resource_id=resource["id"], alias="workspace", role="owner",
        access_mode="read-write", version_policy="live", visibility="team",
    )
    proposed_resource = core.register_resource(
        project_id=project["id"], actor_id=owner["id"],
        canonical_uri="project-core://agent-manual-e2e/proposed", kind="dataset",
        owner_project_id=project["id"],
    )
    workstream = core.create_record(
        project_id=project["id"], actor_id=owner["id"], record_type="workstream",
        title="Verify manual discovery", payload={"goal": "exercise the real boundary"},
        visibility="team",
    )
    core.attach_to_node(
        project_id=project["id"], actor_id=owner["id"],
        node_record_id=workstream["record_id"],
        node_revision_id=workstream["revision_id"], slot="workspace",
        target_kind="resource", target_ref=resource["id"],
        resource_binding_id=binding["id"], relation="context",
        authority="working", version_policy="live", visibility="team",
    )
    workflow = WorkflowService(core, machine_key="agent-manual-e2e-machine")
    workflow.bootstrap_defaults()
    runtime_file = tmp_path / "workflow.json"
    gateway = create_workflow_server(
        workflow, project_id=project["id"], access=access, port=0,
        runtime_file=runtime_file,
        agent_principal_external_id="agent-hub-runtime",
        agent_principal_kind="agent",
    )
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    try:
        settings.enable_project_core = True
        settings.project_core_runtime_file = runtime_file
        hub_project = store.create_project("E2E", str(workspace), "on")
        session = store.create_session(
            hub_project["id"], "manual-e2e", "codex", str(workspace),
            "Exercise the registered path.", project_core_tracking="on",
            agent_role="implement",
        )
        startup_context = brief_seat.tracked_startup_context(
            brief_projects.Project(
                slug="agent-manual-e2e", title="Agent Manual E2E",
                project_id=project["id"],
            ),
            "# Agent Manual E2E\n\n## 下一步\n- Exercise the registered path.",
            "Exercise the registered path.", role="implement", execute=True,
        )
        registration = project_core_client.auto_register_session(
            session, runtime_file=runtime_file, data_dir=settings.data_dir,
            tracking_mode="on", selected_target={
                "project_id": project["id"], "project_title": "Agent Manual E2E",
                "record_id": workstream["record_id"],
                "workstream_title": "Verify manual discovery",
            },
            startup_context=startup_context,
        )
        metadata = registration["project_core"]
        assert metadata["registration_status"] == "registered"
        assert Path(metadata["manual_index_path"]).is_file()
        assert "PROJECT_CORE_AGENT_STARTUP_V1" in registration["initial_prompt"]
        assert "Opening mode: execute" in registration["initial_prompt"]
        assert "Opening assignment: Exercise the registered path." in registration["initial_prompt"]
        assert "PROJECT_BRIEF_" not in registration["initial_prompt"]
        assert "PROJECT_CORE_REPORT_CONTRACT_V3" not in registration["initial_prompt"]
        assert Path(metadata["startup_bundle_path"]).is_file()
        startup = json.loads(Path(metadata["startup_bundle_path"]).read_text())
        assert startup["source"] == "project-brief"
        assert startup["brief_snapshot"]["content"].startswith("# Agent Manual E2E")
        stored = store.update_session_project_core(
            session["id"], project_core=metadata,
            initial_prompt=registration["initial_prompt"],
            project_core_tracking="on",
        )
        runtime = ProjectCoreRuntime(settings)
        installed = runtime.install_contract(stored)
        assert "PROJECT_CORE_RUNTIME_BINDINGS_V1" in installed["initial_prompt"]
        assert '"report_schema_version":1' not in installed["initial_prompt"]
        assert "change_proposal_command:" in installed["initial_prompt"]

        installed_metadata = json.loads(installed["project_core_json"])
        config_path = Path(installed_metadata["report_config_path"])
        proposal_tool = Path(__file__).resolve().parents[1] / "backend" / "manage_change_proposal.py"
        contract = subprocess.run(
            [sys.executable, str(proposal_tool), "--config", str(config_path),
             "--action", "schema"],
            input="{}", text=True, capture_output=True, check=True,
        )
        assert json.loads(contract.stdout)["proposal_schema"] == (
            "project-core.agent-change-proposal/v1"
        )
        expectation = {
            "kind": "resource.binding", "resource_id": proposed_resource["id"],
            "alias": "proposed_data", "role": "consumer",
            "access_mode": "read-only", "version_policy": "live",
            "selector": {}, "scope": "", "visibility": "team",
        }
        proposal = {
            "schema": "project-core.agent-change-proposal/v1",
            "schema_version": 1,
            "title": "Bind proposed E2E data",
            "rationale": "Exercise the installed association-bound command.",
            "idempotency_key": "agent-hub:e2e:proposal:v1",
            "operations": [{
                "operation_id": "bind_data", "type": "resource.bind",
                "rationale": "Add one exact read-only data binding.",
                **{key: value for key, value in expectation.items() if key != "kind"},
                "preconditions": [{
                    "kind": "resource.owner", "resource_id": proposed_resource["id"],
                    "owner_project_id": project["id"],
                }, {"kind": "resource.alias_absent", "alias": "proposed_data"}],
                "verification": [expectation],
            }],
        }
        submitted_process = subprocess.run(
            [sys.executable, str(proposal_tool), "--config", str(config_path),
             "--action", "submit"],
            input=json.dumps(proposal), text=True, capture_output=True, check=True,
        )
        submitted = json.loads(submitted_process.stdout)
        assert submitted["association_id"] == metadata["association_id"]
        assert submitted["status"] == "pending_review"
        queue = core.list_change_proposal_review_queue(access=access)
        assert [item["proposal_id"] for item in queue] == [submitted["proposal_id"]]
        base = gateway.public_info["url"].rstrip("/")
        browser = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor()
        )
        page = browser.open(base + "/?token=" + gateway.browser_token).read().decode()
        assert "Approve exact scope" in page
        review_center = json.loads(browser.open(
            base + f"/v1/projects/{project['id']}/change-proposals"
        ).read())
        assert review_center["review_queue"][0]["proposal_id"] == submitted["proposal_id"]

        def browser_post(path: str, payload: dict) -> dict:
            request = urllib.request.Request(
                base + path,
                data=json.dumps(payload).encode(), method="POST",
                headers={"Content-Type": "application/json", "Origin": base},
            )
            return json.loads(browser.open(request).read())

        reviewed = browser_post(
            f"/v1/change-proposals/{submitted['proposal_id']}/review",
            {
                "reviewer_project_id": project["id"],
                "proposal_revision_id": submitted["proposal_revision_id"],
                "proposal_sha256": submitted["proposal_sha256"],
                "review_scope_sha256": submitted["proposal_sha256"],
                "verdict": "approve", "note": "",
            },
        )
        assert reviewed["status"] == "approved"
        history_response = json.loads(browser.open(
            base + f"/v1/change-proposals/{submitted['proposal_id']}/history"
            f"?project_id={project['id']}"
        ).read())
        assert history_response["revisions"][0]["reviews"][0]["verdict"] == "approve"
        capability = browser_post(
            f"/v1/change-proposals/{submitted['proposal_id']}/authorize-execution",
            {
                "project_id": project["id"],
                "proposal_revision_id": submitted["proposal_revision_id"],
                "proposal_sha256": submitted["proposal_sha256"],
                "association_id": metadata["association_id"], "ttl_seconds": 600,
            },
        )
        executed_process = subprocess.run(
            [sys.executable, str(proposal_tool), "--config", str(config_path),
             "--action", "execute"],
            input=json.dumps({
                "proposal_id": submitted["proposal_id"],
                "proposal_revision_id": submitted["proposal_revision_id"],
                "proposal_sha256": submitted["proposal_sha256"],
                "capability_token": capability["capability_token"],
            }),
            text=True, capture_output=True, check=True,
        )
        executed = json.loads(executed_process.stdout)
        assert executed["status"] == "applied"
        refresh_path = Path(executed["context_refresh"]["path"])
        assert refresh_path.is_file() and refresh_path.stat().st_mode & 0o077 == 0
        refresh = json.loads(refresh_path.read_text())
        assert refresh["association_id"] == metadata["association_id"]
        assert refresh["context_pack"]["sha256"] == executed["context_refresh"]["sha256"]
        history = core.list_change_proposal_revisions(
            submitted["proposal_id"], access=access,
        )
        assert len(history) == 1 and history[0]["current"]
        assert core.list_change_proposal_review_queue(access=access) == []
        with core.store.connection() as connection:
            assert connection.execute(
                "SELECT 1 FROM project_resource_bindings WHERE project_id=? AND alias=?",
                (project["id"], "proposed_data"),
            ).fetchone() is not None

        runtime.session_started(installed, first_start=True)
        report = submit_checkpoint(
            Path(installed_metadata["report_config_path"]),
            {
                "report_schema_version": 1, "status": "completed",
                "summary": "Verified the pinned Agent Manual discovery path.",
                "accomplished": ["Registered through the signed Gateway"],
                "decisions_proposed": [], "blockers": [], "next_steps": [], "io": [],
            },
        )
        assert report["outbox_state"] == "pending"
        assert runtime.flush_outbox()["delivered"] == 1
        with core.store.connection() as connection:
            association = connection.execute(
                "SELECT * FROM agent_session_associations"
            ).fetchone()
            report_count = connection.execute(
                "SELECT count(*) AS n FROM agent_session_turn_reports"
            ).fetchone()["n"]
            pending_review_count = connection.execute(
                "SELECT count(*) AS n FROM agent_session_report_reviews "
                "WHERE state='pending'"
            ).fetchone()["n"]
        assert association["agent_manual_sha256"] == metadata["agent_manual_sha256"]
        assert report_count == 1
        assert pending_review_count == 1
    finally:
        gateway.shutdown()
        thread.join(timeout=2)
        gateway.server_close()
