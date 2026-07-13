import os

# Run the real-tmux tests on a throwaway, isolated socket so they never create
# or kill sessions on your everyday tmux. Must be set before app.config is first
# imported (which reads this env var). Production leaves it unset → default socket.
os.environ.setdefault("AGENT_HUB_TMUX_SOCKET", "agent-hub-test")

import pytest


@pytest.fixture
def settings(tmp_path):
    from app.config import Settings
    return Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.db",
        token="testtoken",
        enable_sampler=False,
    )


@pytest.fixture
def store_db(settings):
    """Init a throwaway DB for tests that call store/* directly."""
    from app import db
    db.init_db(settings.db_path)
    yield
    db._DB_PATH = None


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient
    from app import db
    from app.main import create_app

    app = create_app(settings)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8787",
        headers={"X-Auth-Token": settings.token},
    ) as c:
        yield c
    db._DB_PATH = None
