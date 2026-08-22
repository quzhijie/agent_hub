from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import jump as jump_mod
from .. import paths, project_core as project_core_client, store, tmux
from ..providers.registry import PROVIDER_NAMES, get_provider, is_valid_provider

router = APIRouter()
_PROJECT_CORE_TRACKING = {"inherit", "suggest", "on", "off"}
_AGENT_ROLES = {"general", "plan", "implement", "review"}


class SessionCreate(BaseModel):
    name: str
    provider: str
    working_dir: str
    launch_command: str = ""
    initial_prompt: str = ""
    project_core: dict[str, Any] = Field(default_factory=dict)
    project_core_tracking: str = "inherit"
    agent_role: str = "general"
    project_core_workstream_id: str = ""
    project_core_workstream_title: str = ""
    # A directory can be bound to several Project Core Projects, so the seat
    # carries the Project the chosen Workstream actually belongs to.  Empty
    # means "the one this Agent Hub project is bound to", which is what every
    # seat used to be limited to.
    project_core_project_id: str = ""
    project_core_project_title: str = ""


class ReorderBody(BaseModel):
    ids: list[str]


class JumpBody(BaseModel):
    client: str | None = None


class ProjectCoreRegisterBody(BaseModel):
    candidate_id: str | None = None


@router.get("/providers")
def list_providers():
    return list(PROVIDER_NAMES)


@router.get("/projects/{pid}/sessions")
def get_sessions(pid: str, include_removed: bool = False):
    if store.get_project(pid) is None:
        raise HTTPException(404, "project not found")
    return store.list_sessions(pid, include_removed=include_removed)


@router.post("/projects/{pid}/sessions")
def create_session(pid: str, body: SessionCreate, request: Request):
    project = store.get_project(pid)
    if project is None:
        raise HTTPException(404, "project not found")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "seat name is required")
    if not is_valid_provider(body.provider):
        raise HTTPException(400, f"unknown provider: {body.provider}")
    if body.provider == "custom" and not body.launch_command.strip():
        raise HTTPException(400, "custom provider requires a launch command")
    initial_prompt = body.initial_prompt.strip()
    if len(initial_prompt) > 20_000:
        raise HTTPException(400, "initial prompt is too long")
    if "\x00" in initial_prompt:
        raise HTTPException(400, "initial prompt contains NUL")
    if initial_prompt and body.launch_command.strip():
        raise HTTPException(400, "initial_prompt cannot be combined with a custom launch command")
    allowed_context = {
        "project_id", "record_id", "context_pack_id", "context_pack_sha256",
        "correlation_id",
    }
    unknown_context = sorted(set(body.project_core) - allowed_context)
    if unknown_context:
        raise HTTPException(400, f"unknown Project Core fields: {', '.join(unknown_context)}")
    project_core = {}
    for key, value in body.project_core.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise HTTPException(400, f"invalid Project Core field: {key}")
        project_core[key] = value.strip()
    if body.project_core_tracking not in _PROJECT_CORE_TRACKING:
        raise HTTPException(400, "invalid Project Core tracking mode")
    if body.agent_role not in _AGENT_ROLES:
        raise HTTPException(400, "invalid agent role")
    workstream_id = body.project_core_workstream_id.strip()
    workstream_title = body.project_core_workstream_title.strip()
    if (
        len(workstream_id) > 200 or len(workstream_title) > 500
        or "\x00" in workstream_id + workstream_title
    ):
        raise HTTPException(400, "invalid Project Core Workstream target")
    target_project_id = (
        body.project_core_project_id.strip()
        or (project.get("project_core_project_id") or "")
    )
    target_project_title = (
        body.project_core_project_title.strip()
        or (project.get("project_core_project_title") or "")
        or target_project_id
    )
    if len(target_project_id) > 200 or len(target_project_title) > 500:
        raise HTTPException(400, "invalid Project Core Project target")
    if workstream_id and not target_project_id:
        raise HTTPException(400, "no Project Core Project for this Workstream")
    if workstream_id and (body.provider == "custom" or body.launch_command.strip()):
        raise HTTPException(
            400,
            "Project Core tracking requires a native provider with its default launch command",
        )
    if workstream_id and not workstream_title:
        workstream_title = workstream_id
    selected_target = None
    if workstream_id:
        selected_target = {
            "project_id": target_project_id,
            "project_title": target_project_title,
            "record_id": workstream_id,
            "workstream_title": workstream_title,
        }
    # Explicit Workstream selection is tracking consent. No selection means an
    # ordinary seat; cwd discovery may suggest UI choices but cannot opt in.
    tracking_mode = "on" if selected_target else "off"
    if project_core:
        tracking_mode = "on"
    try:
        wd = paths.validate_dir(body.working_dir)
    except ValueError as e:
        raise HTTPException(400, str(e))
    session = store.create_session(
        pid, name, body.provider, wd, body.launch_command.strip(),
        initial_prompt=initial_prompt, project_core=project_core,
        project_core_tracking=tracking_mode, agent_role=body.agent_role,
    )
    settings = request.app.state.settings
    runtime = request.app.state.project_core_runtime
    if project_core and settings.enable_project_core:
        registration = project_core_client.adopt_handoff_session(
            session, runtime_file=settings.project_core_runtime_file,
            data_dir=settings.data_dir,
        )
        session = store.update_session_project_core(
            session["id"], project_core=registration["project_core"],
            initial_prompt=registration["initial_prompt"], project_core_tracking="on",
        )
        if registration["project_core"].get("registration_status") == "registered":
            session = runtime.install_contract(session)
    elif (
        settings.enable_project_core and tracking_mode != "off" and not project_core
        and body.provider != "custom" and not body.launch_command.strip()
    ):
        registration = project_core_client.auto_register_session(
            session, runtime_file=settings.project_core_runtime_file,
            data_dir=settings.data_dir, tracking_mode=tracking_mode,
            selected_target=selected_target,
        )
        session = store.update_session_project_core(
            session["id"], project_core=registration["project_core"],
            initial_prompt=registration["initial_prompt"],
        )
        if registration["project_core"].get("registration_status") == "registered":
            session = runtime.install_contract(session)
    elif not project_core:
        status = "off" if not selected_target else "unavailable"
        session = store.update_session_project_core(
            session["id"], project_core={
                "registration_status": status,
                **(
                    {
                        "desired_project_id": selected_target["project_id"],
                        "desired_project_title": selected_target["project_title"],
                        "desired_record_id": selected_target["record_id"],
                        "desired_workstream_title": selected_target["workstream_title"],
                    }
                    if selected_target else {}
                ),
            },
            initial_prompt=session.get("initial_prompt", ""),
        )
    return session


@router.post("/sessions/{sid}/project-core/reinject")
def reinject_project_core_context(sid: str, request: Request):
    """Resend the registered immutable snapshot after an in-provider /new or /clear."""
    session = store.get_session(sid)
    if session is None:
        raise HTTPException(404, "seat not found")
    if session["removed_at"]:
        raise HTTPException(400, "restore the seat before reinjecting context")
    try:
        request.app.state.project_core_runtime.reinject_context(session)
    except (ValueError, tmux.TmuxError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.post("/sessions/{sid}/project-core/register")
def register_project_core_session(
    sid: str, body: ProjectCoreRegisterBody, request: Request
):
    session = store.get_session(sid)
    if session is None:
        raise HTTPException(404, "seat not found")
    if session["removed_at"]:
        raise HTTPException(400, "removed seat cannot be associated")
    settings = request.app.state.settings
    if not settings.enable_project_core:
        raise HTTPException(503, "Project Core integration is disabled")
    result = project_core_client.register_prepared_session(
        session, candidate_id=body.candidate_id,
        runtime_file=settings.project_core_runtime_file, data_dir=settings.data_dir,
    )
    if result["project_core"].get("registration_status") != "registered":
        raise HTTPException(503, "Project Core suggestion could not be registered")
    session = store.update_session_project_core(
        sid, project_core=result["project_core"],
        initial_prompt=result["initial_prompt"], project_core_tracking="on",
    )
    runtime = request.app.state.project_core_runtime
    session = runtime.install_contract(session)
    if session.get("started_at"):
        runtime.notify_late_association(session)
    return session


@router.post("/sessions/{sid}/project-core/retry")
def retry_project_core_session(sid: str, request: Request):
    session = store.get_session(sid)
    if session is None:
        raise HTTPException(404, "seat not found")
    if session["removed_at"]:
        raise HTTPException(400, "restore the seat before associating it")
    settings = request.app.state.settings
    if not settings.enable_project_core:
        raise HTTPException(503, "Project Core integration is disabled")
    try:
        metadata = json.loads(session.get("project_core_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    handoff_fields = {
        "project_id", "record_id", "context_pack_id", "context_pack_sha256",
        "correlation_id",
    }
    if isinstance(metadata, dict) and handoff_fields <= set(metadata):
        # A failed Project Core-origin handoff must be retried against its exact
        # immutable authorization, not silently replaced by cwd discovery.
        result = project_core_client.adopt_handoff_session(
            session, runtime_file=settings.project_core_runtime_file,
            data_dir=settings.data_dir,
        )
    else:
        desired_target = None
        if isinstance(metadata, dict) and metadata.get("desired_record_id"):
            desired_target = {
                "project_id": str(metadata.get("desired_project_id") or ""),
                "project_title": str(metadata.get("desired_project_title") or ""),
                "record_id": str(metadata.get("desired_record_id") or ""),
                "workstream_title": str(metadata.get("desired_workstream_title") or ""),
            }
        mode = "on" if session["project_core_tracking"] == "on" else "suggest"
        result = project_core_client.auto_register_session(
            session, runtime_file=settings.project_core_runtime_file,
            data_dir=settings.data_dir, tracking_mode=mode,
            selected_target=desired_target,
        )
    session = store.update_session_project_core(
        sid, project_core=result["project_core"],
        initial_prompt=result["initial_prompt"],
    )
    if result["project_core"].get("registration_status") == "registered":
        runtime = request.app.state.project_core_runtime
        session = runtime.install_contract(session)
        if session.get("started_at"):
            runtime.notify_late_association(session)
    return session


@router.get("/sessions/{sid}/project-core/metrics")
def get_project_core_metrics(sid: str):
    if store.get_session(sid) is None:
        raise HTTPException(404, "seat not found")
    return store.project_core_metrics(sid)


@router.post("/sessions/{sid}/project-core/ignore")
def ignore_project_core_session(sid: str):
    session = store.get_session(sid)
    if session is None:
        raise HTTPException(404, "seat not found")
    try:
        metadata = json.loads(session.get("project_core_json") or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(500, "seat Project Core metadata is invalid") from exc
    if metadata.get("registration_status") == "registered":
        raise HTTPException(400, "registered association must be closed, not ignored")
    return store.update_session_project_core(
        sid, project_core={"registration_status": "off"},
        initial_prompt=session.get("initial_prompt", ""), project_core_tracking="off",
    )


@router.post("/projects/{pid}/sessions/reorder")
def reorder_sessions(pid: str, body: ReorderBody):
    if store.get_project(pid) is None:
        raise HTTPException(404, "project not found")
    store.reorder_sessions(pid, body.ids)
    return {"ok": True}


@router.post("/sessions/{sid}/start")
def start_session(sid: str, request: Request):
    sess = store.get_session(sid)
    if sess is None:
        raise HTTPException(404, "seat not found")
    if sess["removed_at"]:
        raise HTTPException(400, "seat was removed; restore it before starting")
    name = sess["tmux_session"]
    first_start = not bool(sess["started_at"])
    provider = get_provider(sess["provider"])
    if tmux.has_session(name):
        if tmux.pane_dead(name):
            # remain-on-exit corpse: clear it and fall through to a relaunch.
            tmux.kill_session(name)
        else:
            # Already running (e.g. survived a service restart) — just record it.
            started = store.mark_started(sid)
            request.app.state.project_core_runtime.session_started(
                started, first_start=first_start
            )
            return started
    # Seat is down — good moment to migrate an old hash-style tmux name (or a
    # stale one after a project/seat rename) to the readable scheme, so
    # handmux/tmux lists show hub-<project>-<seat>-<id> instead of hex noise.
    proj = store.get_project(sess["project_id"])
    desired = tmux.make_session_name(proj["name"] if proj else "", sess["name"], sid)
    if desired != name and not store.tmux_name_exists(desired) and not tmux.has_session(desired):
        store.update_tmux_session(sid, desired)
        name = desired
    try:
        # RE-start (ran before) → resume command, so the agent picks its last
        # conversation back up after an exit/reboot. First start → fresh.
        if sess["started_at"]:
            command = provider.resolve_resume_command(sess["launch_command"])
        else:
            command = provider.resolve_initial_command(
                sess["launch_command"], sess.get("initial_prompt", "")
            )
        tmux.new_session(name, sess["working_dir"], command)
    except (ValueError, tmux.TmuxError) as e:
        raise HTTPException(400, str(e))
    started = store.mark_started(sid)
    request.app.state.project_core_runtime.session_started(
        started, first_start=first_start
    )
    return started


@router.post("/sessions/{sid}/remove")
def remove_session(sid: str, request: Request):
    sess = store.get_session(sid)
    if sess is None:
        raise HTTPException(404, "seat not found")
    name = sess["tmux_session"]
    request.app.state.project_core_runtime.close_session(
        sess, abandoned=False, reason="user-closed"
    )
    # Only ever kill a session that is registered to this seat and actually ours.
    if store.is_registered_tmux_name(name) and tmux.has_session(name):
        tmux.kill_session(name)
    return store.mark_removed(sid)


@router.post("/sessions/{sid}/restore")
def restore_session(sid: str, request: Request):
    sess = store.get_session(sid)
    if sess is None:
        raise HTTPException(404, "seat not found")
    restored = store.restore_session(sid)
    metadata = json.loads(sess.get("project_core_json") or "{}")
    if (
        sess.get("project_core_tracking") == "on"
        and metadata.get("registration_status") == "registered"
        and sess.get("project_core_lifecycle") in {"finished", "abandoned"}
        and request.app.state.settings.enable_project_core
    ):
        result = project_core_client.auto_register_session(
            restored,
            runtime_file=request.app.state.settings.project_core_runtime_file,
            data_dir=request.app.state.settings.data_dir,
            tracking_mode="on",
            association_segment=int(metadata.get("association_segment") or 1) + 1,
        )
        restored = store.update_session_project_core(
            sid, project_core=result["project_core"],
            initial_prompt=result["initial_prompt"],
        )
        if result["project_core"].get("registration_status") == "registered":
            restored = request.app.state.project_core_runtime.install_contract(restored)
        else:
            restored = store.update_project_core_runtime(
                sid, lifecycle="untracked",
                warning="Project Core reassociation is pending",
            )
    return restored


@router.delete("/sessions/{sid}")
def purge_session(sid: str, request: Request):
    """Permanently delete a seat record (no restore afterwards)."""
    sess = store.get_session(sid)
    if sess is None:
        raise HTTPException(404, "seat not found")
    name = sess["tmux_session"]
    request.app.state.project_core_runtime.close_session(
        sess, abandoned=False, reason="user-purged"
    )
    # The user asked to purge, so stop and archive the seat even when its final
    # lifecycle event still needs delivery.  Keep the DB row until that durable
    # outbox has drained, otherwise ON DELETE CASCADE would erase the event.
    if store.tmux_name_exists(name) and tmux.has_session(name):
        tmux.kill_session(name)
    if not sess.get("removed_at"):
        store.mark_removed(sid)
    metrics = store.project_core_metrics(sid)
    if metrics.get("outbox_pending", 0):
        raise HTTPException(
            409,
            "seat was stopped and archived; Project Core events are still pending, "
            "retry purge after delivery",
        )
    store.purge_session(sid)
    return {"ok": True, "purged": sid}


@router.post("/sessions/{sid}/jump")
def jump_session(sid: str, request: Request, body: JumpBody | None = None):
    sess = store.get_session(sid)
    if sess is None:
        raise HTTPException(404, "seat not found")
    result = jump_mod.jump_to(sess, client_name=body.client if body else None)
    # Jumping IS looking: tell the sampler you've now seen this seat, so its
    # 等待输入/已完成 clears to 空闲 the moment you switch the viewer away — even
    # if that glance was shorter than one sample interval.
    if result.get("ok") and result.get("jumped"):
        sampler = getattr(request.app.state, "sampler", None)
        if sampler is not None:
            sampler.mark_viewed(sid, result.get("client"))
    return result
