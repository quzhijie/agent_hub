from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import db, status
from .config import Settings, load_settings
from .routes import projects, sessions, state
from .security import make_guard

log = logging.getLogger("agent_hub")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    guard = make_guard(settings)
    sampler = status.StatusSampler(
        interval=settings.sample_interval, capture_lines=settings.capture_lines,
        notify_enabled=settings.enable_notify,
        notify_url=f"http://{settings.host}:{settings.port}/",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db(settings.db_path)
        status.reconcile_on_startup()
        if settings.enable_sampler:
            sampler.start()
        log.info("agent-hub ready on http://%s:%s", settings.host, settings.port)
        yield
        if settings.enable_sampler:
            await sampler.stop()

    app = FastAPI(title="Agent Hub", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.sampler = sampler

    guarded = [Depends(guard)]
    app.include_router(projects.router, prefix="/api", dependencies=guarded)
    app.include_router(sessions.router, prefix="/api", dependencies=guarded)
    app.include_router(state.router, prefix="/api", dependencies=guarded)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (settings.web_dir / "index.html").read_text()
        return HTMLResponse(html.replace("%%AUTH_TOKEN%%", settings.token))

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
