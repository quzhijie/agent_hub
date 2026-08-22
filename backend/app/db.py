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
    project_core_tracking TEXT NOT NULL DEFAULT 'off'
        CHECK (project_core_tracking IN ('suggest', 'on', 'off')),
    project_core_project_id TEXT NOT NULL DEFAULT '',
    project_core_project_title TEXT NOT NULL DEFAULT '',
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
    resume_prompt_pending INTEGER NOT NULL DEFAULT 0
        CHECK (resume_prompt_pending IN (0, 1)),
    removed_at       TEXT,
    initial_prompt   TEXT NOT NULL DEFAULT '',
    project_core_json TEXT NOT NULL DEFAULT '{}',
    project_core_tracking TEXT NOT NULL DEFAULT 'off'
        CHECK (project_core_tracking IN ('suggest', 'on', 'off')),
    project_core_lifecycle TEXT NOT NULL DEFAULT 'untracked'
        CHECK (project_core_lifecycle IN (
            'untracked', 'registered', 'active', 'finished', 'abandoned'
        )),
    project_core_report_warning TEXT NOT NULL DEFAULT '',
    agent_role       TEXT NOT NULL DEFAULT 'general'
        CHECK (agent_role IN ('general', 'plan', 'implement', 'review')),
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

-- Provider-neutral checkpoint windows observed by the Agent Hub runtime.
-- The model report is stored separately from host evidence so neither can
-- silently rewrite the other. An open window may span ordinary turns because
-- a checkpoint exists only after the user explicitly requests one.
CREATE TABLE IF NOT EXISTS project_core_turns (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    association_id  TEXT NOT NULL,
    turn_seq        INTEGER NOT NULL CHECK (turn_seq > 0),
    state           TEXT NOT NULL CHECK (state IN ('open', 'reported', 'missing')),
    settle_kind     TEXT NOT NULL DEFAULT '',
    reminder_count  INTEGER NOT NULL DEFAULT 0 CHECK (reminder_count BETWEEN 0 AND 1),
    report_json     TEXT NOT NULL DEFAULT '{}',
    report_sha256   TEXT NOT NULL DEFAULT '',
    evidence_json   TEXT NOT NULL DEFAULT '[]',
    report_bytes    INTEGER NOT NULL DEFAULT 0,
    git_before_json TEXT NOT NULL DEFAULT '{}',
    git_after_json  TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    reported_at     TEXT,
    UNIQUE (session_id, association_id, turn_seq)
);

-- Exact canonical envelopes are durable before any callback. Retries only
-- replace delivery state; event identity and body never change.
CREATE TABLE IF NOT EXISTS project_core_outbox (
    event_id         TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    association_id   TEXT NOT NULL,
    turn_id          TEXT REFERENCES project_core_turns(id) ON DELETE CASCADE,
    event_type       TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    envelope_json    TEXT NOT NULL,
    envelope_sha256  TEXT NOT NULL,
    state            TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'dead_letter')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TEXT,
    last_error       TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    delivered_at     TEXT
);

CREATE INDEX IF NOT EXISTS project_core_turns_session_idx
    ON project_core_turns(session_id, association_id, turn_seq);
CREATE INDEX IF NOT EXISTS project_core_outbox_retry_idx
    ON project_core_outbox(state, next_attempt_at, created_at);

-- Orchestrated linear pipelines (plan→implement→review, etc.). The runner is
-- deterministic code and may type only into its OWN seats (orchestrator._send).
-- The separate Project Core gate may send only bounded, marker-prefixed protocol
-- handoff/reminder messages through tmux.send_protocol_message.
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
    auto_advance  INTEGER NOT NULL DEFAULT 0,
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
    if "project_core_tracking" not in cols:
        c.execute(
            "ALTER TABLE projects ADD COLUMN project_core_tracking "
            "TEXT NOT NULL DEFAULT 'off' "
            "CHECK (project_core_tracking IN ('suggest', 'on', 'off'))"
        )
    if "project_core_project_id" not in cols:
        c.execute(
            "ALTER TABLE projects ADD COLUMN project_core_project_id TEXT NOT NULL DEFAULT ''"
        )
        # Earlier builds stored only suggest/on/off and therefore had no
        # explicit Project target. Require the user to bind one under the new
        # model instead of interpreting an old policy as consent.
        c.execute("UPDATE projects SET project_core_tracking='off'")
    if "project_core_project_title" not in cols:
        c.execute(
            "ALTER TABLE projects ADD COLUMN project_core_project_title TEXT NOT NULL DEFAULT ''"
        )
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
    if "initial_prompt" not in scols:
        c.execute("ALTER TABLE sessions ADD COLUMN initial_prompt TEXT NOT NULL DEFAULT ''")
    if "resume_prompt_pending" not in scols:
        c.execute(
            "ALTER TABLE sessions ADD COLUMN resume_prompt_pending "
            "INTEGER NOT NULL DEFAULT 0 CHECK (resume_prompt_pending IN (0, 1))"
        )
    if "project_core_json" not in scols:
        c.execute("ALTER TABLE sessions ADD COLUMN project_core_json TEXT NOT NULL DEFAULT '{}'")
    if "project_core_tracking" not in scols:
        c.execute(
            "ALTER TABLE sessions ADD COLUMN project_core_tracking "
            "TEXT NOT NULL DEFAULT 'off' "
            "CHECK (project_core_tracking IN ('suggest', 'on', 'off'))"
        )
    if "project_core_lifecycle" not in scols:
        c.execute(
            "ALTER TABLE sessions ADD COLUMN project_core_lifecycle "
            "TEXT NOT NULL DEFAULT 'untracked' CHECK (project_core_lifecycle IN "
            "('untracked', 'registered', 'active', 'finished', 'abandoned'))"
        )
    if "project_core_report_warning" not in scols:
        c.execute(
            "ALTER TABLE sessions ADD COLUMN project_core_report_warning "
            "TEXT NOT NULL DEFAULT ''"
        )
    if "agent_role" not in scols:
        c.execute(
            "ALTER TABLE sessions ADD COLUMN agent_role TEXT NOT NULL DEFAULT 'general' "
            "CHECK (agent_role IN ('general', 'plan', 'implement', 'review'))"
        )
    # 'auto_advance' runs a pipeline through all steps without stopping at each
    # gate — steps still run headless with logs, you review after.
    plcols = {r["name"] for r in c.execute("PRAGMA table_info(pipelines)")}
    if "auto_advance" not in plcols:
        c.execute("ALTER TABLE pipelines ADD COLUMN auto_advance INTEGER NOT NULL DEFAULT 0")


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


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Cross-process-safe write transaction for report/outbox state changes."""
    with _WRITE_LOCK, connect() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except Exception:
            c.rollback()
            raise
        else:
            c.commit()
