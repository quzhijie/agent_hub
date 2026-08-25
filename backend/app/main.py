from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db, orchestrator, status
from .config import Settings, load_settings
from .project_core_runtime import ProjectCoreRuntime
from .routes import pipelines, projects, sessions, state
from .security import BROWSER_COOKIE, BROWSER_COOKIE_MAX_AGE, make_guard

log = logging.getLogger("agent_hub")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    orchestrator.set_log_root(settings.data_dir / "pipelines")
    guard = make_guard(settings)
    project_core_runtime = ProjectCoreRuntime(settings)

    def cycle() -> None:
        if settings.enable_orchestrator:
            orchestrator.tick()
        project_core_runtime.flush_outbox()

    sampler = status.StatusSampler(
        interval=settings.sample_interval, capture_lines=settings.capture_lines,
        notify_enabled=settings.enable_notify,
        notify_url=f"http://{settings.host}:{settings.port}/",
        on_cycle=cycle, on_status=project_core_runtime.observe_status,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db(settings.db_path)
        status.reconcile_on_startup()
        project_core_runtime.reconcile_on_startup()
        if settings.enable_sampler:
            sampler.start()
        log.info("agent-hub ready on http://%s:%s", settings.host, settings.port)
        yield
        if settings.enable_sampler:
            await sampler.stop()

    app = FastAPI(title="Agent Hub", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.sampler = sampler
    app.state.project_core_runtime = project_core_runtime
    app.state.navigation_intent = None
    app.state.dashboard_seen_at = 0.0

    guarded = [Depends(guard)]
    app.include_router(projects.router, prefix="/api", dependencies=guarded)
    app.include_router(sessions.router, prefix="/api", dependencies=guarded)
    app.include_router(state.router, prefix="/api", dependencies=guarded)
    app.include_router(pipelines.router, prefix="/api", dependencies=guarded)

    @app.get("/", response_class=HTMLResponse, dependencies=guarded)
    def index(request: Request):
        if request.query_params.get("token") == settings.token:
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                BROWSER_COOKIE, settings.token, max_age=BROWSER_COOKIE_MAX_AGE,
                httponly=True, samesite="strict", path="/",
            )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response
        html = (settings.web_dir / "index.html").read_text()
        # Cache-bust the static assets by their mtime: StaticFiles sends no
        # Cache-Control, so a normal browser refresh otherwise serves edited
        # JS/CSS stale from disk cache. The ?v=<mtime> changes only when the file
        # does, so unchanged assets still cache.
        for asset in ("app.js", "styles.css"):
            try:
                v = int((settings.web_dir / asset).stat().st_mtime)
            except OSError:
                continue
            html = html.replace(f"/static/{asset}", f"/static/{asset}?v={v}")
        response = HTMLResponse(html)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    if settings.web_dir.exists():
        app.mount("/static", StaticFiles(directory=str(settings.web_dir)), name="static")

    return app


app = create_app()


class _QuietPolls(logging.Filter):
    """Drop the every-2.5s dashboard-poll access lines; keep jumps/starts/errors."""
    _NOISY = ("/api/state", "/api/providers")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        path = args[2] if isinstance(args, tuple) and len(args) >= 3 else ""
        return not (isinstance(path, str) and path.startswith(self._NOISY))


def main() -> None:  # `python -m app.main` / console entry
    import uvicorn

    logging.getLogger("uvicorn.access").addFilter(_QuietPolls())
    s = app.state.settings
    sock = f"-L {s.tmux_socket} " if s.tmux_socket else ""
    banner = (
        "\n  Agent Hub — local multi-agent dashboard\n"
        f"  → open   http://{s.host}:{s.port}/?token={s.token}\n"
        f"  → viewer tmux {sock}attach   (one terminal, driven by the web)\n"
    )
    print(banner)
    uvicorn.run(app, host=s.host, port=s.port, log_level="info")


if __name__ == "__main__":
    main()
