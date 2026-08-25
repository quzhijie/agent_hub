from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request


router = APIRouter()


@router.post("/restart", status_code=202)
def restart_service(request: Request):
    controller = request.app.state.restart_controller
    try:
        scheduled = controller.request()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {
        "accepted": True,
        "already_requested": not scheduled,
        "instance_id": request.app.state.instance_id,
    }
