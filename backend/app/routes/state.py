from __future__ import annotations

import time

from fastapi import APIRouter, Request

from .. import store, tmux

router = APIRouter()


@router.get("/state")
def get_state(request: Request):
    """Full snapshot for the dashboard poll: projects with their seats."""
    if request.headers.get("X-Agent-Hub-Dashboard") == "1":
        request.app.state.dashboard_seen_at = time.monotonic()
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
    intent = getattr(request.app.state, "navigation_intent", None)
    if intent and intent["expires_at"] <= time.monotonic():
        intent = None
        request.app.state.navigation_intent = None
    return {
        "projects": out,
        "tmux_available": tmux.available(),
        "tmux_clients": tmux.client_details(),
        "instance_id": request.app.state.instance_id,
        "restart_available": request.app.state.restart_controller.available,
        "navigation_intent": (
            {"id": intent["id"], "project_id": intent["project_id"]}
            if intent else None
        ),
    }
