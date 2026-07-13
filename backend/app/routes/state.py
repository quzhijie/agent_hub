from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import store, tmux

router = APIRouter()


@router.get("/state")
def get_state():
    """Full snapshot for the dashboard poll: projects with their seats."""
    out = []
    for p in store.list_projects():
        active = store.list_sessions(p["id"], include_removed=False)
        allrows = store.list_sessions(p["id"], include_removed=True)
        removed = [s for s in allrows if s["removed_at"]]
        out.append({
            **p,
            "sessions": [s for s in active if not s["removed_at"]],
            "removed_sessions": removed,
            "attention": sum(1 for s in active if s["status"] == store.WAITING),
            "active_count": sum(1 for s in active if s["status"] in (store.ACTIVE, store.WAITING)),
        })
    return {"projects": out, "tmux_available": tmux.available(),
            "events": store.recent_notifications()}


@router.post("/events/{eid}/archive")
def archive_event(eid: str):
    """Soft-dismiss one push-trail row so it drops out of the strip."""
    if not store.archive_notification(eid):
        raise HTTPException(404, "event not found or already archived")
    return {"ok": True}


@router.post("/events/archive_all")
def archive_all_events():
    """Clear the whole strip (archive every currently-shown row)."""
    return {"ok": True, "archived": store.archive_all_notifications()}
