from __future__ import annotations

import urllib.error

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import paths, project_core as project_core_client, store, tmux

router = APIRouter()
_PROJECT_CORE_TRACKING = {"suggest", "on", "off"}


class ProjectCreate(BaseModel):
    name: str
    root_dir: str
    project_core_tracking: str = "off"
    project_core_project_id: str = ""
    project_core_project_title: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = None
    is_removed: bool | None = None
    notes: str | None = None
    root_dir: str | None = None
    project_core_tracking: str | None = None
    project_core_project_id: str | None = None
    project_core_project_title: str | None = None


class ProjectCoreTargetsBody(BaseModel):
    working_dir: str


class ReorderBody(BaseModel):
    ids: list[str]


@router.get("/projects")
def get_projects(include_removed: bool = False):
    return store.list_projects(include_removed=include_removed)


def _project_core_binding(project_id: str, title: str) -> tuple[str, str]:
    project_id = project_id.strip()
    title = title.strip()
    if len(project_id) > 200 or len(title) > 500 or "\x00" in project_id + title:
        raise HTTPException(400, "invalid Project Core Project binding")
    if project_id and not title:
        title = project_id
    if not project_id:
        title = ""
    return project_id, title


@router.post("/project-core/targets")
def resolve_project_core_targets(body: ProjectCoreTargetsBody, request: Request):
    settings = request.app.state.settings
    if not settings.enable_project_core:
        raise HTTPException(503, "Project Core integration is disabled")
    try:
        working_dir = paths.validate_dir(body.working_dir)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        return project_core_client.resolve_targets(
            working_dir=working_dir,
            runtime_file=settings.project_core_runtime_file,
        )
    except (KeyError, OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        raise HTTPException(503, "Project Core target discovery is unavailable") from exc


@router.post("/projects")
def create_project(body: ProjectCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "project name is required")
    try:
        root = paths.validate_dir(body.root_dir)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.project_core_tracking not in _PROJECT_CORE_TRACKING:
        raise HTTPException(400, "invalid Project Core tracking policy")
    pc_id, pc_title = _project_core_binding(
        body.project_core_project_id, body.project_core_project_title
    )
    tracking = "on" if pc_id else "off"
    return store.create_project(
        name, root, tracking,
        project_core_project_id=pc_id, project_core_project_title=pc_title,
    )


@router.post("/projects/reorder")
def reorder_projects(body: ReorderBody):
    store.reorder_projects(body.ids)
    return {"ok": True}


@router.patch("/projects/{pid}")
def update_project(pid: str, body: ProjectUpdate):
    current = store.get_project(pid)
    if current is None:
        raise HTTPException(404, "project not found")
    name = body.name.strip() if body.name is not None else None
    if name == "":
        raise HTTPException(400, "project name cannot be empty")
    root = None
    if body.root_dir is not None:
        try:
            root = paths.validate_dir(body.root_dir)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if (
        body.project_core_tracking is not None
        and body.project_core_tracking not in _PROJECT_CORE_TRACKING
    ):
        raise HTTPException(400, "invalid Project Core tracking policy")
    pc_id = pc_title = None
    if body.project_core_project_id is not None:
        pc_id, pc_title = _project_core_binding(
            body.project_core_project_id,
            body.project_core_project_title or "",
        )
    elif body.project_core_project_title is not None:
        raise HTTPException(400, "Project Core Project ID is required with its title")
    tracking = None
    if pc_id:
        tracking = "on"
    elif pc_id == "":
        tracking = "off"
    elif body.project_core_tracking is not None:
        tracking = "on" if current.get("project_core_project_id") else "off"
    return store.update_project(pid, name=name, is_removed=body.is_removed,
                                notes=body.notes, root_dir=root,
                                project_core_tracking=tracking,
                                project_core_project_id=pc_id,
                                project_core_project_title=pc_title)


@router.delete("/projects/{pid}")
def delete_project(pid: str, request: Request):
    """Permanently delete a project and all its seats. Kills any live tmux seats
    we own first so none orphan (mirrors purge_session)."""
    if store.get_project(pid) is None:
        raise HTTPException(404, "project not found")
    sessions = store.list_sessions(pid, include_removed=True)
    for s in sessions:
        request.app.state.project_core_runtime.close_session(
            s, abandoned=False, reason="project-purged"
        )
        name = s["tmux_session"]
        if store.tmux_name_exists(name) and tmux.has_session(name):
            tmux.kill_session(name)
        if not s.get("removed_at"):
            store.mark_removed(s["id"])
    pending = sum(
        store.project_core_metrics(session["id"]).get("outbox_pending", 0)
        for session in sessions
    )
    if pending:
        raise HTTPException(
            409,
            "project seats were stopped and archived; Project Core events are still "
            "pending, retry purge after delivery",
        )
    store.purge_project(pid)
    return {"ok": True, "purged": pid}
