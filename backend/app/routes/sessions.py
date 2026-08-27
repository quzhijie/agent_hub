from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import jump as jump_mod
from .. import paths, project_core as project_core_client, store, tmux, transcripts
from ..providers.registry import PROVIDER_NAMES, get_provider, is_valid_provider

router = APIRouter()
_PROJECT_CORE_TRACKING = {"inherit", "suggest", "on", "off"}
_AGENT_ROLES = {"general", "plan", "implement", "review"}
_PERMISSION_MODES = {"default", "unrestricted"}


class SessionCreate(BaseModel):
    name: str
    provider: str
    model: str = ""
    reasoning_effort: str = ""
    permission_mode: str = "default"
    working_dir: str
    launch_command: str = ""
    initial_prompt: str = ""
    project_core_startup_context: dict[str, Any] = Field(default_factory=dict)
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


class RestoreBody(BaseModel):
    conversation_mode: Literal["fresh", "resume"] = "fresh"


class ProjectCoreTranscriptSearchTarget(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    association_id: str = Field(min_length=1, max_length=200)


class ProjectCoreTranscriptSearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=transcripts.MAX_SEARCH_QUERY_CHARS)
    targets: list[ProjectCoreTranscriptSearchTarget] = Field(
        min_length=1, max_length=100,
    )


def _ensure_provider_session_id(
    session: dict[str, Any], provider=None,
) -> str:
    """Persist a provider-native conversation id while launch evidence exists."""
    native_session_id = str(session.get("provider_session_id") or "")
    if native_session_id or not session.get("started_at"):
        return native_session_id
    provider = provider or get_provider(session["provider"])
    native_session_id = provider.find_native_session_id(
        session["working_dir"], session["started_at"],
    )
    if native_session_id:
        store.update_provider_session_id(session["id"], native_session_id)
    return native_session_id


def _project_core_metadata(session: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(session.get("project_core_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _bind_current_conversation(
    session: dict[str, Any], native_session_id: str, *, new_conversation: bool = False,
) -> dict[str, Any] | None:
    metadata = _project_core_metadata(session)
    association_id = str(metadata.get("association_id") or "")
    if (
        not native_session_id
        or metadata.get("registration_status") != "registered"
        or not association_id
    ):
        return None
    existing = store.get_session_conversation_binding(association_id)
    if existing is not None:
        return existing
    history = store.list_session_conversation_bindings(session["id"])
    same_conversation = any(
        item["provider"] == session["provider"]
        and item["provider_session_id"] == native_session_id
        for item in history
    )
    reader_session = {**session, "provider_session_id": native_session_id}
    if new_conversation or not history:
        start_message_seq = 0
    elif same_conversation:
        start_message_seq = transcripts.session_transcript_cursor(reader_session)
    else:
        # A newly discovered legacy conversation has no safe cross-generation
        # boundary. It is still exact for this current association from seq 0.
        start_message_seq = 0
    return store.bind_session_conversation(
        session["id"], provider=session["provider"],
        provider_session_id=native_session_id, association_id=association_id,
        association_segment=int(metadata.get("association_segment") or 1),
        start_message_seq=start_message_seq,
    )


def _snapshot_current_conversation(session: dict[str, Any]) -> None:
    """Freeze the visible-message end before a restore can change identity."""
    native_session_id = _ensure_provider_session_id(session)
    binding = _bind_current_conversation(session, native_session_id)
    if binding is None:
        return
    count = transcripts.session_transcript_cursor(
        {**session, "provider_session_id": native_session_id}
    )
    store.close_session_conversation_binding(
        binding["association_id"], end_message_seq=count,
    )


def _restored_context_prompt(session: dict[str, Any], *, resume: bool) -> str:
    """A restored seat gets orientation, never its stale original assignment."""
    try:
        metadata = json.loads(session.get("project_core_json") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    target = ""
    if isinstance(metadata, dict):
        project = str(metadata.get("project_title") or "").strip()
        workstream = str(metadata.get("workstream_title") or "").strip()
        target = " > ".join(part for part in (project, workstream) if part)
    lines = [
        "[AGENT_HUB_RESTORE_CONTEXT_ONLY_V1]",
        (
            "This archived seat is resuming its old provider conversation."
            if resume else
            "This archived seat is opening a fresh provider conversation."
        ),
    ]
    if target:
        lines.append(f"Work target: {target}")
    lines += [
        "The following Project Core bootstrap and accepted brief are the current context; "
        "they supersede stale project-status text from the earlier conversation.",
        "Context only: do not resume the prior task, call tools, or change files. Wait for "
        "the user's next message unless that message explicitly authorizes work.",
    ]
    return "\n".join(lines)


def _startup_context_from_prompt(
    prompt: str,
    *,
    agent_role: str,
    source: str = "agent-hub-user",
) -> dict[str, Any]:
    return {
        "schema": "project-core.agent-startup-request/v1",
        "schema_version": 1,
        "source": source,
        "seat_role": agent_role,
        "assignment": {
            "mode": "execute" if prompt else "context_only",
            "task": prompt,
        },
        "brief_snapshot": None,
    }


@router.get("/providers")
def list_providers():
    return list(PROVIDER_NAMES)


@router.get("/provider-options")
def provider_options():
    return [
        {
            "name": name,
            "models": list(get_provider(name).model_choices),
            "reasoning_efforts": list(get_provider(name).reasoning_effort_choices),
            "permission_modes": list(get_provider(name).permission_modes()),
        }
        for name in PROVIDER_NAMES
    ]


@router.get("/projects/{pid}/sessions")
def get_sessions(pid: str, include_removed: bool = False):
    if store.get_project(pid) is None:
        raise HTTPException(404, "project not found")
    return store.list_sessions(pid, include_removed=include_removed)


@router.get("/sessions/{sid}/project-core/associations/{association_id}/transcript")
def get_session_transcript(
    sid: str, association_id: str, before: int | None = None,
    limit: int = transcripts.DEFAULT_LIMIT,
):
    """Read the exact provider conversation segment for one Core association."""
    session = store.get_session(sid)
    binding = store.get_session_conversation_binding(association_id)
    if binding is None and session is not None:
        metadata = _project_core_metadata(session)
        if str(metadata.get("association_id") or "") == association_id:
            native_session_id = _ensure_provider_session_id(session)
            binding = _bind_current_conversation(session, native_session_id)
    if binding is None:
        recovered = transcripts.recover_project_core_session(sid, association_id)
        if recovered is not None:
            try:
                result = transcripts.read_session_transcript(
                    recovered, before=before, limit=limit, after=0, through=None,
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            result.update({
                "association_id": association_id,
                "association_segment": 1,
                "conversation_generation": 1,
                "legacy_recovered": True,
            })
            return result
        if session is None:
            return {
                "schema_version": 1, "status": "unavailable",
                "reason": "provider_session_not_found",
                "session_id": sid, "association_id": association_id,
                "messages": [], "total_messages": 0, "returned": 0,
                "next_before": None, "has_earlier": False,
                "internal_content_omitted": True,
            }
    if binding is None or binding["session_id"] != sid:
        raise HTTPException(404, "conversation association is not bound")
    if binding["start_message_seq"] is None:
        return {
            "schema_version": 1, "status": "unavailable",
            "reason": "association_boundary_unavailable",
            "session_id": sid, "association_id": association_id,
            "messages": [], "total_messages": 0, "returned": 0,
            "next_before": None, "has_earlier": False,
        }
    try:
        result = transcripts.read_session_transcript(
            {
                **session,
                "provider": binding["provider"],
                "provider_session_id": binding["provider_session_id"],
            },
            before=before, limit=limit,
            after=int(binding["start_message_seq"]),
            through=(
                int(binding["end_message_seq"])
                if binding["end_message_seq"] is not None else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    result.update({
        "association_id": association_id,
        "association_segment": int(binding["association_segment"]),
        "conversation_generation": int(binding["generation"]),
    })
    return result


@router.post("/project-core/transcripts/search")
def search_project_core_transcripts(body: ProjectCoreTranscriptSearchBody):
    """Search exact Core association segments without returning transcripts."""
    query = " ".join(body.query.split())
    if not query or "\x00" in query:
        raise HTTPException(400, "transcript search query is invalid")
    identities = [
        (target.session_id, target.association_id) for target in body.targets
    ]
    if len(set(identities)) != len(identities):
        raise HTTPException(400, "transcript search targets must be unique")

    searched_count = 0
    unavailable_count = 0
    matches: list[dict[str, Any]] = []
    resolved: list[tuple[Any, dict[str, Any] | None, dict[str, Any] | None]] = []
    recovery_targets: list[tuple[str, str]] = []
    for target in body.targets:
        session = store.get_session(target.session_id)
        binding = store.get_session_conversation_binding(target.association_id)
        if binding is None and session is not None:
            metadata = _project_core_metadata(session)
            if str(metadata.get("association_id") or "") == target.association_id:
                native_session_id = _ensure_provider_session_id(session)
                binding = _bind_current_conversation(session, native_session_id)
        if session is None and binding is None:
            recovery_targets.append((target.session_id, target.association_id))
        resolved.append((target, session, binding))
    recovered_sessions = transcripts.recover_project_core_sessions(recovery_targets)

    for target, session, binding in resolved:
        recovered = recovered_sessions.get(
            (target.session_id, target.association_id)
        )
        if (
            recovered is None and (
                session is None or binding is None
                or binding["session_id"] != target.session_id
                or binding["start_message_seq"] is None
            )
        ):
            unavailable_count += 1
            continue
        try:
            result = transcripts.search_session_transcript(
                recovered or {
                    **session,
                    "provider": binding["provider"],
                    "provider_session_id": binding["provider_session_id"],
                },
                query,
                after=(0 if recovered else int(binding["start_message_seq"])),
                through=(
                    None if recovered else (
                        int(binding["end_message_seq"])
                        if binding["end_message_seq"] is not None else None
                    )
                ),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if result["status"] != "available":
            unavailable_count += 1
            continue
        searched_count += 1
        if not result["matched"]:
            continue
        match = result["match"]
        matches.append({
            "session_id": target.session_id,
            "association_id": target.association_id,
            "message_seq": match["message_seq"],
            "role": match["role"],
            "occurred_at": match["occurred_at"],
            "excerpt": match["excerpt"],
            "matching_message_count": result["matching_message_count"],
        })
    return {
        "schema_version": 1,
        "status": "available",
        "target_count": len(body.targets),
        "searched_count": searched_count,
        "unavailable_count": unavailable_count,
        "matched_count": len(matches),
        "matches": matches,
        "internal_content_omitted": True,
    }


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
    provider = get_provider(body.provider)
    try:
        model = provider.normalize_model(body.model)
        reasoning_effort = provider.normalize_reasoning_effort(body.reasoning_effort)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if body.provider == "custom" and not body.launch_command.strip():
        raise HTTPException(400, "custom provider requires a launch command")
    if body.permission_mode not in _PERMISSION_MODES:
        raise HTTPException(400, "invalid permission mode")
    if body.permission_mode not in provider.permission_modes():
        raise HTTPException(
            400, f"provider {body.provider!r} does not support {body.permission_mode} permissions",
        )
    initial_prompt = body.initial_prompt.strip()
    if len(initial_prompt) > 20_000:
        raise HTTPException(400, "initial prompt is too long")
    if "\x00" in initial_prompt:
        raise HTTPException(400, "initial prompt contains NUL")
    if initial_prompt and body.launch_command.strip():
        raise HTTPException(400, "initial_prompt cannot be combined with a custom launch command")
    if (model or reasoning_effort) and body.launch_command.strip():
        raise HTTPException(
            400, "model or reasoning effort cannot be combined with a custom launch command",
        )
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
    supplied_startup = body.project_core_startup_context
    if not isinstance(supplied_startup, dict):
        raise HTTPException(400, "Project Core startup context must be an object")
    try:
        startup_bytes = json.dumps(
            supplied_startup, ensure_ascii=False, separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "Project Core startup context must be JSON") from exc
    if len(startup_bytes) > 32_000 or b"\x00" in startup_bytes:
        raise HTTPException(400, "Project Core startup context is too large")
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
    startup_context: dict[str, Any] | None = None
    if supplied_startup and not selected_target:
        raise HTTPException(400, "Project Core startup context requires a Workstream")
    if supplied_startup and project_core:
        raise HTTPException(400, "Project Core-origin handoff owns its startup context")
    if selected_target:
        if supplied_startup:
            if supplied_startup.get("seat_role") != body.agent_role:
                raise HTTPException(400, "startup seat role does not match the session role")
            startup_context = supplied_startup
        else:
            startup_context = _startup_context_from_prompt(
                initial_prompt, agent_role=body.agent_role,
            )
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
        model=model, reasoning_effort=reasoning_effort,
        # A tracked opening assignment is data inside Project Core's startup
        # bundle. Only an untracked seat receives caller-authored prompt text.
        initial_prompt=("" if selected_target else initial_prompt),
        project_core=project_core,
        project_core_tracking=tracking_mode, agent_role=body.agent_role,
        permission_mode=body.permission_mode,
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
            startup_context=startup_context,
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
            association_segment=int(metadata.get("association_segment") or 1),
            selected_target=desired_target,
            startup_context=(
                metadata.get("startup_context")
                if isinstance(metadata.get("startup_context"), dict) else None
            ),
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


@router.post("/sessions/{sid}/project-core/deliveries/retry")
def retry_project_core_deliveries(sid: str, request: Request):
    if store.get_session(sid) is None:
        raise HTTPException(404, "seat not found")
    if not request.app.state.settings.enable_project_core:
        raise HTTPException(503, "Project Core integration is disabled")
    requeued = store.requeue_project_core_dead_letters(sid)
    delivery = request.app.state.project_core_runtime.flush_outbox(
        limit=50, session_id=sid,
    )
    remaining = store.project_core_metrics(sid).get("outbox_dead_letter", 0)
    return {
        "ok": remaining == 0,
        "requeued": requeued,
        "remaining_dead_letters": remaining,
        "delivery": delivery,
    }


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
    if sess.get("project_core_tracking") == "on":
        try:
            project_core = json.loads(sess.get("project_core_json") or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(500, "seat Project Core metadata is invalid") from exc
        if not isinstance(project_core, dict):
            raise HTTPException(500, "seat Project Core metadata is invalid")
        registration_status = project_core.get("registration_status")
        if registration_status != "registered":
            raise HTTPException(
                409,
                "Project Core tracking is not registered "
                f"({registration_status or 'unknown'}); retry the association or "
                "turn off tracking before starting",
            )
    name = sess["tmux_session"]
    resume_prompt_pending = bool(sess.get("resume_prompt_pending"))
    first_start = not bool(sess["started_at"]) or resume_prompt_pending
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
    launch_started_at = store.now_iso()
    allocated_native_session_id = False
    try:
        native_session_id = _ensure_provider_session_id(sess, provider)
        if (
            not sess["started_at"] and not native_session_id
            and not sess.get("launch_command", "").strip()
        ):
            native_session_id = provider.new_native_session_id()
            if native_session_id:
                store.update_provider_session_id(sid, native_session_id)
                allocated_native_session_id = True
        if native_session_id and not allocated_native_session_id:
            _bind_current_conversation(
                {**sess, "provider_session_id": native_session_id},
                native_session_id,
            )
        # RE-start (ran before) → resume command, so the agent picks its last
        # conversation back up after an exit/reboot. First start → fresh.
        if sess["started_at"]:
            if resume_prompt_pending:
                command = provider.resolve_resume_with_prompt_command(
                    sess["launch_command"], sess.get("initial_prompt", ""),
                    model=sess.get("model", ""),
                    reasoning_effort=sess.get("reasoning_effort", ""),
                    permission_mode=sess.get("permission_mode", "default"),
                    native_session_id=native_session_id,
                )
            else:
                command = provider.resolve_resume_command(
                    sess["launch_command"], model=sess.get("model", ""),
                    reasoning_effort=sess.get("reasoning_effort", ""),
                    permission_mode=sess.get("permission_mode", "default"),
                    native_session_id=native_session_id,
                )
        else:
            command = provider.resolve_initial_command(
                sess["launch_command"], sess.get("initial_prompt", ""),
                model=sess.get("model", ""),
                reasoning_effort=sess.get("reasoning_effort", ""),
                permission_mode=sess.get("permission_mode", "default"),
                native_session_id=native_session_id,
            )
        tmux.new_session(name, sess["working_dir"], command)
        tmux.require_live_pane(name)
    except (ValueError, tmux.TmuxError) as e:
        if tmux.has_session(name) and tmux.pane_dead(name):
            tmux.kill_session(name)
        if allocated_native_session_id:
            store.update_provider_session_id(sid, "")
        raise HTTPException(400, str(e))
    if native_session_id and allocated_native_session_id:
        _bind_current_conversation(
            {**sess, "provider_session_id": native_session_id},
            native_session_id,
            new_conversation=True,
        )
    if not native_session_id:
        native_session_id = provider.find_native_session_id(
            sess["working_dir"], launch_started_at,
        )
        if native_session_id:
            store.update_provider_session_id(sid, native_session_id)
            _bind_current_conversation(
                {**sess, "provider_session_id": native_session_id},
                native_session_id,
                new_conversation=True,
            )
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
    _snapshot_current_conversation(sess)
    return store.mark_removed(sid)


@router.post("/sessions/{sid}/restore")
def restore_session(sid: str, request: Request, body: RestoreBody | None = None):
    sess = store.get_session(sid)
    if sess is None:
        raise HTTPException(404, "seat not found")
    if not sess.get("removed_at"):
        raise HTTPException(400, "seat is not archived; remove it before restoring")
    resume = bool(body and body.conversation_mode == "resume")
    if resume:
        provider = get_provider(sess["provider"])
        if (
            not sess.get("started_at") or sess.get("launch_command", "").strip()
            or not provider.resume_suffix
        ):
            raise HTTPException(400, "this seat has no resumable native conversation")
        native_session_id = _ensure_provider_session_id(sess, provider)
        if provider.requires_exact_resume_with_prompt and not native_session_id:
            raise HTTPException(
                409,
                "could not identify this seat's exact provider conversation; "
                "restore it as a fresh conversation instead",
            )
    _snapshot_current_conversation(sess)
    restored = store.restore_session(
        sid, resume_conversation=resume,
        initial_prompt=_restored_context_prompt(sess, resume=resume),
    )
    metadata = json.loads(sess.get("project_core_json") or "{}")
    tracked_closed = (
        sess.get("project_core_tracking") == "on"
        and metadata.get("registration_status") == "registered"
        and sess.get("project_core_lifecycle") in {"finished", "abandoned"}
    )
    if tracked_closed and request.app.state.settings.enable_project_core:
        selected_target = {
            "project_id": str(metadata.get("project_id") or ""),
            "project_title": str(metadata.get("project_title") or ""),
            "record_id": str(metadata.get("record_id") or ""),
            "workstream_title": str(metadata.get("workstream_title") or ""),
        }
        result = project_core_client.auto_register_session(
            restored,
            runtime_file=request.app.state.settings.project_core_runtime_file,
            data_dir=request.app.state.settings.data_dir,
            tracking_mode="on",
            association_segment=int(metadata.get("association_segment") or 1) + 1,
            selected_target=selected_target,
            startup_context=_startup_context_from_prompt(
                "", agent_role=str(restored.get("agent_role") or "general"),
                source="agent-hub-restore",
            ),
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
    elif tracked_closed:
        restored = store.update_session_project_core(
            sid,
            project_core={
                "registration_status": "unavailable",
                "association_segment": int(metadata.get("association_segment") or 1) + 1,
                "desired_project_id": str(metadata.get("project_id") or ""),
                "desired_project_title": str(metadata.get("project_title") or ""),
                "desired_record_id": str(metadata.get("record_id") or ""),
                "desired_workstream_title": str(metadata.get("workstream_title") or ""),
            },
            initial_prompt=restored.get("initial_prompt", ""),
        )
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
    _snapshot_current_conversation(sess)
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
