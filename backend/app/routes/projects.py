from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import paths, store

router = APIRouter()


class ProjectCreate(BaseModel):
    name: str
    root_dir: str


class ProjectUpdate(BaseModel):
    name: str | None = None
    is_removed: bool | None = None
    notes: str | None = None


class ReorderBody(BaseModel):
    ids: list[str]


@router.get("/projects")
def get_projects(include_removed: bool = False):
    return store.list_projects(include_removed=include_removed)


@router.post("/projects")
def create_project(body: ProjectCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "project name is required")
    try:
        root = paths.validate_dir(body.root_dir)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return store.create_project(name, root)


@router.post("/projects/reorder")
def reorder_projects(body: ReorderBody):
    store.reorder_projects(body.ids)
    return {"ok": True}


@router.patch("/projects/{pid}")
def update_project(pid: str, body: ProjectUpdate):
    if store.get_project(pid) is None:
        raise HTTPException(404, "project not found")
    name = body.name.strip() if body.name is not None else None
    if name == "":
        raise HTTPException(400, "project name cannot be empty")
    return store.update_project(pid, name=name, is_removed=body.is_removed, notes=body.notes)
