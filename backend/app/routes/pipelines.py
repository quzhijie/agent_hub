from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import orchestrator, outline as outline_mod, store, templates, tmux, worktree

router = APIRouter()


class Step(BaseModel):
    role: str = ""
    prompt: str
    provider: str = "claude"


class PipelineCreate(BaseModel):
    project_id: str
    steps: list[Step]                 # arbitrary length; from a preset, an outline, or edits
    name: str | None = None
    outline_path: str | None = None   # if set, copied into the worktree as OUTLINE.md


class OutlineParse(BaseModel):
    path: str | None = None           # a file on this machine (the architect's outline)
    text: str | None = None           # or pasted text


def _view(pl: dict) -> dict:
    """Pipeline + its phases, each with the bound seat's live status, for the UI."""
    phases = []
    for ph in store.pipeline_phases(pl["id"]):
        seat = store.get_session(ph["seat_id"])
        phases.append({
            "id": ph["id"], "idx": ph["idx"], "role": ph["role"],
            "status": ph["status"], "prompt": ph["prompt"],
            "seat": None if not seat else {
                "id": seat["id"], "name": seat["name"], "provider": seat["provider"],
                "status": seat["status"], "last_output": seat["last_output"],
            },
        })
    return {**pl, "phases": phases}


@router.get("/pipeline-templates")
def list_templates():
    return templates.catalog()


@router.post("/parse-outline")
def parse_outline(body: OutlineParse):
    """Deterministically split an outline into editable steps (no LLM). Returns
    the resolved path (so the client can pass it back to copy into the worktree)."""
    text, path = body.text, None
    if body.path:
        p = Path(body.path).expanduser()
        if not p.is_file():
            raise HTTPException(400, f"文件不存在: {body.path}")
        text = p.read_text()
        path = str(p)
    if not (text or "").strip():
        raise HTTPException(400, "大纲为空")
    steps = [{
        "role": (s["title"] or f"step-{i + 1}")[:24],
        "prompt": templates.wrap_outline_step(s["title"], s["body"]),
        "provider": "claude",
    } for i, s in enumerate(outline_mod.parse_steps(text))]
    return {"steps": steps, "outline_path": path}


@router.get("/pipelines")
def list_pipelines():
    return [_view(pl) for pl in store.list_pipelines()]


@router.get("/pipelines/{pid}")
def get_pipeline(pid: str):
    pl = store.get_pipeline(pid)
    if pl is None:
        raise HTTPException(404, "pipeline not found")
    return _view(pl)


@router.post("/pipelines")
def create_pipeline(body: PipelineCreate):
    steps = [s.model_dump() for s in body.steps]
    if not steps:
        raise HTTPException(400, "流水线至少要有一步")
    first = (steps[0].get("role") or "").strip()
    name = (body.name or first or "pipeline")[:60].strip() or "pipeline"
    source = "outline" if body.outline_path else "custom"
    try:
        pl = orchestrator.launch_pipeline(body.project_id, name, steps,
                                          outline_path=body.outline_path, source=source)
    except (orchestrator.OrchestratorError, worktree.WorktreeError) as e:
        raise HTTPException(400, str(e))
    return _view(pl)


@router.post("/pipelines/{pid}/approve")
def approve(pid: str):
    try:
        pl = orchestrator.approve_phase(pid)
    except orchestrator.OrchestratorError as e:
        raise HTTPException(400, str(e))
    return _view(pl)


@router.post("/pipelines/{pid}/abort")
def abort(pid: str):
    try:
        pl = orchestrator.abort_pipeline(pid)
    except orchestrator.OrchestratorError as e:
        raise HTTPException(400, str(e))
    return _view(pl)


@router.delete("/pipelines/{pid}")
def delete_pipeline(pid: str):
    """Full cleanup: abort if still running, kill+purge its seats, remove the
    worktree (the branch is kept), then drop the pipeline rows."""
    pl = store.get_pipeline(pid)
    if pl is None:
        raise HTTPException(404, "pipeline not found")
    if pl["status"] == "running":
        orchestrator.abort_pipeline(pid)
    seat_ids = [ph["seat_id"] for ph in store.pipeline_phases(pid)]
    for sid in seat_ids:
        seat = store.get_session(sid)
        if seat and store.tmux_name_exists(seat["tmux_session"]) and tmux.has_session(seat["tmux_session"]):
            tmux.kill_session(seat["tmux_session"])
    # Drop the pipeline (+ its phase rows) BEFORE the seats: pipeline_phases has a
    # FK to sessions(id), so purging a seat first would violate it.
    store.purge_pipeline(pid)
    for sid in seat_ids:
        store.purge_session(sid)
    proj = store.get_project(pl["project_id"])
    if proj and pl["worktree_path"]:
        worktree.remove(proj["root_dir"], pl["worktree_path"])
    return {"ok": True, "deleted": pid}
