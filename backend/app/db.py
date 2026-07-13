"""SQLite storage. One connection per operation; WAL for concurrent reads."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_WRITE_LOCK = threading.Lock()
_DB_PATH: Path | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    root_dir    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    is_removed  INTEGER NOT NULL DEFAULT 0,
    notes       TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    id               TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL REFERENCES projects(id),
    name             TEXT NOT NULL,
    provider         TEXT NOT NULL,
    launch_command   TEXT NOT NULL DEFAULT '',
    working_dir      TEXT NOT NULL,
    tmux_session     TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'unknown',
    last_output      TEXT NOT NULL DEFAULT '',
    last_activity_at TEXT,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    removed_at       TEXT,
    sort_order       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS session_events (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    kind        TEXT NOT NULL,
    old_status  TEXT,
    new_status  TEXT,
    created_at  TEXT NOT NULL,
    archived_at TEXT
);

-- Orchestrated linear pipelines (plan→implement→review, etc.). The runner is
-- deterministic code; the only thing that may type into a seat is a phase of a
-- pipeline, and only into that pipeline's OWN seats (see orchestrator._send).
CREATE TABLE IF NOT EXISTS pipelines (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES projects(id),
    name          TEXT NOT NULL,
    task          TEXT NOT NULL,
    template      TEXT NOT NULL,
    worktree_path TEXT NOT NULL DEFAULT '',
    branch        TEXT NOT NULL DEFAULT '',
    base_branch   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'running',
    phase_index   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_phases (
    id           TEXT PRIMARY KEY,
    pipeline_id  TEXT NOT NULL REFERENCES pipelines(id),
    idx          INTEGER NOT NULL,
    role         TEXT NOT NULL,
    seat_id      TEXT NOT NULL REFERENCES sessions(id),
    prompt       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    saw_active   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
"""


def init_db(path: Path | str) -> None:
    global _DB_PATH
    _DB_PATH = Path(path)
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def _migrate(c: sqlite3.Connection) -> None:
    """Additive migrations for DBs created before a column existed."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(projects)")}
    if "notes" not in cols:
        c.execute("ALTER TABLE projects ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
    if "sort_order" not in cols:
        c.execute("ALTER TABLE projects ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        # Backfill by creation time so the existing display order is preserved.
        for i, r in enumerate(c.execute("SELECT id FROM projects ORDER BY created_at").fetchall()):
            c.execute("UPDATE projects SET sort_order=? WHERE id=?", (i, r["id"]))
    scols = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
    if "sort_order" not in scols:
        c.execute("ALTER TABLE sessions ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        idx: dict[str, int] = {}
        for r in c.execute("SELECT id, project_id FROM sessions ORDER BY created_at").fetchall():
            i = idx.get(r["project_id"], 0)
            c.execute("UPDATE sessions SET sort_order=? WHERE id=?", (i, r["id"]))
            idx[r["project_id"]] = i + 1
    ecols = {r["name"] for r in c.execute("PRAGMA table_info(session_events)")}
    if "archived_at" not in ecols:
        c.execute("ALTER TABLE session_events ADD COLUMN archived_at TEXT")
    # 'orchestrated' marks a seat as pipeline-owned: the ONLY seats the
    # orchestrator is ever allowed to type into. Interactive seats stay 0.
    if "orchestrated" not in scols:
        c.execute("ALTER TABLE sessions ADD COLUMN orchestrated INTEGER NOT NULL DEFAULT 0")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    if _DB_PATH is None:
        raise RuntimeError("init_db() must be called before connect()")
    conn = sqlite3.connect(str(_DB_PATH), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def writing() -> Iterator[sqlite3.Connection]:
    """Serialise writers with a process-level lock (single-user localhost app)."""
    with _WRITE_LOCK, connect() as c:
        yield c
