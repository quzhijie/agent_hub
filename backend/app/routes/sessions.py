from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import jump as jump_mod
from .. import paths, store, tmux
from ..providers.registry import PROVIDER_NAMES, get_provider, is_valid_provider

router = APIRouter()


class SessionCreate(BaseModel):
    name: str
    provider: str
    working_dir: str
    launch_command: str = ""


class ReorderBody(BaseModel):
    ids: list[str]


@router.get("/providers")
def list_providers():
    return list(PROVIDER_NAMES)


@router.get("/projects/{pid}/sessions")
def get_sessions(pid: str, include_removed: bool = False):
    if store.get_project(pid) is None:
        raise HTTPException(404, "project not found")
    return store.list_sessions(pid, include_removed=include_removed)


@router.post("/projects/{pid}/sessions")
def create_session(pid: str, body: SessionCreate):
    if store.get_project(pid) is None:
        raise HTTPException(404, "project not found")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "seat name is required")
    if not is_valid_provider(body.provider):
        raise HTTPException(400, f"unknown provider: {body.provider}")
    if body.provider == "custom" and not body.launch_command.strip():
        raise HTTPException(400, "custom provider requires a launch command")
    try:
        wd = paths.validate_dir(body.working_dir)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return store.create_session(pid, name, body.provider, wd, body.launch_command.strip())


@router.post("/projects/{pid}/sessions/reorder")
def reorder_sessions(pid: str, body: ReorderBody):
    if store.get_project(pid) is None:
        raise HTTPException(404, "project not found")
    store.reorder_sessions(pid, body.ids)
    return {"ok": True}


@router.post("/sessions/{sid}/start")
def start_session(sid: str):
    sess = store.get_session(sid)
    if sess is None:
        raise HTTPException(404, "seat not found")
    if sess["removed_at"]:
        raise HTTPException(400, "seat was removed; restore it before starting")
    name = sess["tmux_session"]
    provider = get_provider(sess["provider"])
    if tmux.has_session(name):
        if tmux.pane_dead(name):
            # remain-on-exit corpse: clear it and fall through to a relaunch.
            tmux.kill_session(name)
        else:
            # Already running (e.g. survived a service restart) — just record it.
            return store.mark_started(sid)
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
            command = provider.resolve_command(sess["launch_command"])
        tmux.new_session(name, sess["working_dir"], command)
    except (ValueError, tmux.TmuxError) as e:
        raise HTTPException(400, str(e))
    return store.mark_started(sid)


@router.post("/sessions/{sid}/remove")
def remove_session(sid: str):
    sess = store.get_session(sid)
    if sess is None:
        raise HTTPException(404, "seat not found")
    name = sess["tmux_session"]
    # Only ever kill a session that is registered to this seat and actually ours.
    if store.is_registered_tmux_name(name) and tmux.has_session(name):
        tmux.kill_session(name)
    return store.mark_removed(sid)


@router.post("/sessions/{sid}/restore")
def restore_session(sid: str):
    sess = store.get_session(sid)
    if sess is None:
        raise HTTPException(404, "seat not found")
    return store.restore_session(sid)


@router.delete("/sessions/{sid}")
def purge_session(sid: str):
    """Permanently delete a seat record (no restore afterwards)."""
    sess = store.get_session(sid)
    if sess is None:
        raise HTTPException(404, "seat not found")
    name = sess["tmux_session"]
    # Defensive: kill a still-live tmux session that belongs to this seat so we
    # never orphan one. tmux_name_exists confirms the name is ours before we do.
    if store.tmux_name_exists(name) and tmux.has_session(name):
        tmux.kill_session(name)
    store.purge_session(sid)
    return {"ok": True, "purged": sid}


@router.post("/sessions/{sid}/jump")
def jump_session(sid: str):
    sess = store.get_session(sid)
    if sess is None:
        raise HTTPException(404, "seat not found")
    return jump_mod.jump_to(sess)
