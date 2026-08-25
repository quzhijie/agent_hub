"""Navigate and focus the local Agent Hub dashboard without exposing its token."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlencode

from .config import Settings

log = logging.getLogger("agent_hub.dashboard")

_OSASCRIPT = shutil.which("osascript")
_FOCUS_SCRIPT = Path(__file__).resolve().parent / "dashboard_focus.applescript"
_HANDLED_STATUSES = {"focused", "opened", "opened-default"}


def project_url(settings: Settings, project_id: str) -> str:
    host = f"[{settings.host}]" if ":" in settings.host else settings.host
    query = urlencode({"token": settings.token})
    fragment = urlencode({"project": project_id})
    return f"http://{host}:{settings.port}/?{query}#{fragment}"


def focus_project(
    settings: Settings,
    project_id: str,
    *,
    open_if_missing: bool = True,
) -> dict[str, object]:
    """Focus an existing Chrome Hub tab and navigate it, opening only as fallback."""
    if not _OSASCRIPT or not _FOCUS_SCRIPT.exists():
        return {"handled": False, "status": "native-focus-unavailable"}
    try:
        result = subprocess.run(
            [
                _OSASCRIPT,
                str(_FOCUS_SCRIPT),
                project_url(settings, project_id),
                str(settings.port),
                "1" if open_if_missing else "0",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.warning("dashboard focus failed", exc_info=True)
        return {"handled": False, "status": "native-focus-failed"}
    status = result.stdout.strip()
    if result.returncode == 0 and status == "not-found":
        return {"handled": False, "status": "no-dashboard-found"}
    if result.returncode != 0 or status not in _HANDLED_STATUSES:
        log.warning(
            "dashboard focus returned an unexpected result: code=%s status=%r",
            result.returncode,
            status,
        )
        return {"handled": False, "status": "native-focus-failed"}
    return {"handled": True, "status": status}
