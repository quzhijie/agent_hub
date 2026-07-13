"""Repository functions over the SQLite tables. Rows returned as plain dicts."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from . import db, tmux

# Session status values.
#   active   工作中     — generating right now (sticky: won't drop on a blip)
#   waiting  等待输入   — asked you a question; LATCHING until you look at it
#   done     已完成     — finished a turn you haven't seen yet; LATCHING
#   idle     空闲       — resting / acknowledged; nothing wants you
#   exited   已退出     — the process is gone
#   unknown  状态未知   — the fallback when the frame is unreadable
ACTIVE = "active"
WAITING = "waiting"
DONE = "done"
IDLE = "idle"
EXITED = "exited"
UNKNOWN = "unknown"
STATUSES = {ACTIVE, WAITING, DONE, IDLE, EXITED, UNKNOWN}

# The two "look at me" states that don't clear on their own — only a genuine
# resumption (raw active) or a view-acknowledge moves a seat out of them.
ATTENTION = {WAITING, DONE}


def is_settled(status: str) -> bool:
    """True if the seat is not working and not asking — parked at its prompt.

    Both 空闲 and 已完成 mean "the turn is over"; the orchestrator treats them
    the same (a phase that finished is settled whether or not you've seen it)."""
    return status in (IDLE, DONE)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def _row(r) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


# --- projects ---------------------------------------------------------------

def create_project(name: str, root_dir: str) -> dict:
    pid = new_id()
    ts = now_iso()
    with db.writing() as c:
        c.execute(
            "INSERT INTO projects (id, name, root_dir, created_at, updated_at, is_removed, sort_order)"
            " VALUES (?,?,?,?,?,0, COALESCE((SELECT MAX(sort_order)+1 FROM projects), 0))",
            (pid, name, root_dir, ts, ts),
        )
    return get_project(pid)


def get_project(pid: str) -> dict | None:
    with db.connect() as c:
        return _row(c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone())


def list_projects(include_removed: bool = False) -> list[dict]:
    q = "SELECT * FROM projects"
    if not include_removed:
        q += " WHERE is_removed=0"
    q += " ORDER BY sort_order, created_at"
    with db.connect() as c:
        return [dict(r) for r in c.execute(q).fetchall()]


def update_project(pid: str, *, name: str | None = None, is_removed: bool | None = None,
                   notes: str | None = None, root_dir: str | None = None) -> dict | None:
    fields, vals = [], []
    if name is not None:
        fields.append("name=?"); vals.append(name)
    if is_removed is not None:
        fields.append("is_removed=?"); vals.append(1 if is_removed else 0)
    if notes is not None:
        fields.append("notes=?"); vals.append(notes)
    if root_dir is not None:
        fields.append("root_dir=?"); vals.append(root_dir)
    if not fields:
        return get_project(pid)
    fields.append("updated_at=?"); vals.append(now_iso())
    vals.append(pid)
    with db.writing() as c:
        old = c.execute("SELECT root_dir FROM projects WHERE id=?", (pid,)).fetchone()
        c.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id=?", vals)
        if root_dir is not None and old and old["root_dir"] != root_dir:
            _relocate_sessions(c, pid, old["root_dir"], root_dir)
    return get_project(pid)


def _relocate_sessions(c, project_id: str, old_root: str, new_root: str) -> None:
    """A project's root moved (folder was reorganized): repoint every seat whose
    working_dir lived at/under the OLD root to the NEW root (prefix swap, so
    sub-directory seats follow too). Seats pointing elsewhere are left alone.

    Metadata only: a RUNNING tmux session already has its cwd and is untouched —
    this just fixes where the seat's NEXT start will launch.
    """
    old = old_root.rstrip("/")
    new = new_root.rstrip("/")
    for r in c.execute("SELECT id, working_dir FROM sessions WHERE project_id=?",
                       (project_id,)).fetchall():
        wd = r["working_dir"] or ""
        if wd == old:
            new_wd = new
        elif wd.startswith(old + "/"):
            new_wd = new + wd[len(old):]
        else:
            continue
        c.execute("UPDATE sessions SET working_dir=? WHERE id=?", (new_wd, r["id"]))


# --- sessions ---------------------------------------------------------------

def create_session(project_id: str, name: str, provider: str, working_dir: str,
                   launch_command: str, orchestrated: bool = False) -> dict:
    sid = new_id()
    proj = get_project(project_id)
    pname = proj["name"] if proj else ""
    tmux_session = tmux.make_session_name(pname, name, sid)
    if tmux_name_exists(tmux_session):     # same project+seat names: longer id
        tmux_session = tmux.make_session_name(pname, name, sid, id_len=8)
    tmux.validate_name(tmux_session)
    ts = now_iso()
    with db.writing() as c:
        c.execute(
            "INSERT INTO sessions (id, project_id, name, provider, launch_command,"
            " working_dir, tmux_session, status, last_output, created_at, orchestrated,"
            " sort_order)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,"
            " COALESCE((SELECT MAX(sort_order)+1 FROM sessions WHERE project_id=?), 0))",
            (sid, project_id, name, provider, launch_command, working_dir,
             tmux_session, UNKNOWN, "", ts, 1 if orchestrated else 0, project_id),
        )
        _add_event(c, sid, "created", None, UNKNOWN)
    return get_session(sid)


def get_session(sid: str) -> dict | None:
    with db.connect() as c:
        return _row(c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())


def list_sessions(project_id: str | None = None, include_removed: bool = False) -> list[dict]:
    q = "SELECT * FROM sessions"
    conds, vals = [], []
    if project_id is not None:
        conds.append("project_id=?"); vals.append(project_id)
    if not include_removed:
        conds.append("removed_at IS NULL")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY sort_order, created_at"
    with db.connect() as c:
        return [dict(r) for r in c.execute(q, vals).fetchall()]


def list_live_sessions() -> list[dict]:
    """Registered, not manually removed — candidates for status sampling."""
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM sessions WHERE removed_at IS NULL AND started_at IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_started(sid: str) -> dict | None:
    ts = now_iso()
    with db.writing() as c:
        old = c.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
        c.execute(
            "UPDATE sessions SET started_at=?, status=?, last_activity_at=? WHERE id=?",
            (ts, ACTIVE, ts, sid),
        )
        _add_event(c, sid, "started", old["status"] if old else None, ACTIVE)
    return get_session(sid)


def update_status(sid: str, status: str, last_output: str, activity: bool) -> None:
    with db.writing() as c:
        row = c.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            return
        old = row["status"]
        sets = ["status=?", "last_output=?"]
        vals: list[Any] = [status, last_output]
        if activity:
            sets.append("last_activity_at=?"); vals.append(now_iso())
        vals.append(sid)
        c.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", vals)
        if status != old:
            _add_event(c, sid, "status_changed", old, status)


# --- notification / push trail ----------------------------------------------

def record_notification(sid: str, old_status: str | None, new_status: str, kind: str) -> None:
    """Persist a push-worthy status transition so the dashboard can show a
    recent-pushes strip — lets you trace which agent pinged even if you missed
    the OS banner. kind is 'waiting' (needs input) or 'done' (back to idle)."""
    with db.writing() as c:
        _add_event(c, sid, f"notify_{kind}", old_status, new_status)


def recent_notifications(limit: int = 30) -> list[dict]:
    """Newest-first push trail for /api/state: each row carries the seat it came
    from (still jumpable unless removed) and a ready-to-render line."""
    with db.connect() as c:
        rows = c.execute(
            "SELECT e.id AS id, e.created_at AS ts, e.kind AS kind, e.session_id AS seat_id,"
            "       s.name AS seat, s.removed_at AS seat_removed, p.name AS project"
            "  FROM session_events e"
            "  JOIN sessions s ON s.id = e.session_id"
            "  LEFT JOIN projects p ON p.id = s.project_id"
            " WHERE e.kind LIKE 'notify_%' AND e.archived_at IS NULL"
            " ORDER BY e.created_at DESC, e.rowid DESC"
            " LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        waiting = r["kind"] == "notify_waiting"
        where = f"{r['project']} / {r['seat']}" if r["project"] else r["seat"]
        out.append({
            "id": r["id"],
            "ts": r["ts"],
            "kind": "waiting" if waiting else "done",
            "seat_id": r["seat_id"],
            "seat_removed": bool(r["seat_removed"]),
            "text": f"{where} {'等待输入' if waiting else '已完成'}",
        })
    return out


def archive_notification(eid: str) -> bool:
    """Soft-dismiss one push-trail row — non-destructive: it just stamps
    archived_at so the row drops out of the strip (history stays in the DB).
    Returns True if a not-yet-archived notify_* row matched."""
    with db.writing() as c:
        cur = c.execute(
            "UPDATE session_events SET archived_at=?"
            " WHERE id=? AND kind LIKE 'notify_%' AND archived_at IS NULL",
            (now_iso(), eid),
        )
        return cur.rowcount > 0


def archive_all_notifications() -> int:
    """Clear the whole strip in one go. Returns how many rows were archived."""
    with db.writing() as c:
        cur = c.execute(
            "UPDATE session_events SET archived_at=?"
            " WHERE kind LIKE 'notify_%' AND archived_at IS NULL",
            (now_iso(),),
        )
        return cur.rowcount


def mark_removed(sid: str) -> dict | None:
    ts = now_iso()
    with db.writing() as c:
        row = c.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
        old = row["status"] if row else None
        c.execute(
            "UPDATE sessions SET removed_at=?, status=? WHERE id=?", (ts, EXITED, sid)
        )
        _add_event(c, sid, "manually_removed", old, EXITED)
    return get_session(sid)


def restore_session(sid: str) -> dict | None:
    with db.writing() as c:
        c.execute(
            "UPDATE sessions SET removed_at=NULL, started_at=NULL, status=? WHERE id=?",
            (UNKNOWN, sid),
        )
    return get_session(sid)


def update_tmux_session(sid: str, tmux_session: str) -> dict | None:
    """Rename a seat's tmux name (only safe while its session isn't running)."""
    tmux.validate_name(tmux_session)
    with db.writing() as c:
        c.execute("UPDATE sessions SET tmux_session=? WHERE id=?", (tmux_session, sid))
    return get_session(sid)


def reorder_projects(ids: list[str]) -> None:
    """Assign sort_order 0..n-1 following the given id order. Unknown ids no-op."""
    with db.writing() as c:
        for i, pid in enumerate(ids):
            c.execute("UPDATE projects SET sort_order=? WHERE id=?", (i, pid))


def reorder_sessions(project_id: str, ids: list[str]) -> None:
    """Same, scoped to one project so the ids can't touch another project's seats."""
    with db.writing() as c:
        for i, sid in enumerate(ids):
            c.execute("UPDATE sessions SET sort_order=? WHERE id=? AND project_id=?",
                      (i, sid, project_id))


def purge_session(sid: str) -> bool:
    """Permanently delete a seat and its events. Returns True if a row was removed.

    Events must go first: session_events references sessions(id) and foreign
    keys are enforced, so deleting the session while events remain would fail.
    """
    with db.writing() as c:
        c.execute("DELETE FROM session_events WHERE session_id=?", (sid,))
        cur = c.execute("DELETE FROM sessions WHERE id=?", (sid,))
        return cur.rowcount > 0


def purge_project(pid: str) -> bool:
    """Permanently delete a project and ALL its seats (+ their events). Returns
    True if the project row was removed. Caller should kill any live tmux seats
    first so none orphan (see the delete route)."""
    with db.writing() as c:
        sids = [r["id"] for r in
                c.execute("SELECT id FROM sessions WHERE project_id=?", (pid,)).fetchall()]
        for sid in sids:                       # events first: FK to sessions(id)
            c.execute("DELETE FROM session_events WHERE session_id=?", (sid,))
        c.execute("DELETE FROM sessions WHERE project_id=?", (pid,))
        cur = c.execute("DELETE FROM projects WHERE id=?", (pid,))
        return cur.rowcount > 0


def tmux_name_exists(tmux_session: str) -> bool:
    with db.connect() as c:
        return c.execute(
            "SELECT 1 FROM sessions WHERE tmux_session=?", (tmux_session,)
        ).fetchone() is not None


def is_registered_tmux_name(tmux_session: str) -> bool:
    """True if this tmux name belongs to a non-removed workbench seat."""
    with db.connect() as c:
        return c.execute(
            "SELECT 1 FROM sessions WHERE tmux_session=? AND removed_at IS NULL",
            (tmux_session,),
        ).fetchone() is not None


def _add_event(c, sid: str, kind: str, old: str | None, new: str | None) -> None:
    c.execute(
        "INSERT INTO session_events (id, session_id, kind, old_status, new_status, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (new_id(), sid, kind, old, new, now_iso()),
    )


# --- pipelines --------------------------------------------------------------
# Pipeline statuses: 'running' | 'done' | 'aborted' | 'failed'.
# Phase statuses:    'pending' | 'running' | 'awaiting_approval' | 'done'.

def create_pipeline(pid: str, project_id: str, name: str, task: str, template: str,
                    worktree_path: str, branch: str, base_branch: str,
                    phases: list[dict]) -> dict:
    """phases: [{role, seat_id, prompt}] in order. All rows in one transaction.
    `pid` is supplied by the caller so the worktree/branch can be derived from it
    before the row exists."""
    ts = now_iso()
    with db.writing() as c:
        c.execute(
            "INSERT INTO pipelines (id, project_id, name, task, template, worktree_path,"
            " branch, base_branch, status, phase_index, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?, 'running', 0, ?, ?)",
            (pid, project_id, name, task, template, worktree_path, branch, base_branch, ts, ts),
        )
        for i, ph in enumerate(phases):
            c.execute(
                "INSERT INTO pipeline_phases (id, pipeline_id, idx, role, seat_id, prompt,"
                " status, saw_active, created_at) VALUES (?,?,?,?,?,?, 'pending', 0, ?)",
                (new_id(), pid, i, ph["role"], ph["seat_id"], ph["prompt"], ts),
            )
    return get_pipeline(pid)


def get_pipeline(pid: str) -> dict | None:
    with db.connect() as c:
        return _row(c.execute("SELECT * FROM pipelines WHERE id=?", (pid,)).fetchone())


def list_pipelines(status: str | None = None) -> list[dict]:
    q = "SELECT * FROM pipelines"
    vals: list = []
    if status is not None:
        q += " WHERE status=?"; vals.append(status)
    q += " ORDER BY created_at DESC"
    with db.connect() as c:
        return [dict(r) for r in c.execute(q, vals).fetchall()]


def pipeline_phases(pid: str) -> list[dict]:
    with db.connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM pipeline_phases WHERE pipeline_id=? ORDER BY idx", (pid,)).fetchall()]


def get_phase(phase_id: str) -> dict | None:
    with db.connect() as c:
        return _row(c.execute("SELECT * FROM pipeline_phases WHERE id=?", (phase_id,)).fetchone())


def update_pipeline(pid: str, **fields) -> dict | None:
    allowed = {"status", "phase_index", "name", "task"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if sets:
        cols = ", ".join(f"{k}=?" for k in sets) + ", updated_at=?"
        vals = list(sets.values()) + [now_iso(), pid]
        with db.writing() as c:
            c.execute(f"UPDATE pipelines SET {cols} WHERE id=?", vals)
    return get_pipeline(pid)


def update_phase(phase_id: str, **fields) -> dict | None:
    allowed = {"status", "saw_active", "prompt"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if sets:
        cols = ", ".join(f"{k}=?" for k in sets)
        vals = list(sets.values()) + [phase_id]
        with db.writing() as c:
            c.execute(f"UPDATE pipeline_phases SET {cols} WHERE id=?", vals)
    return get_phase(phase_id)


def pipeline_member_seat_ids(pid: str) -> set[str]:
    """The seats this pipeline owns — the ONLY seats the orchestrator may write
    to. Used by the hardcoded send allowlist."""
    return {ph["seat_id"] for ph in pipeline_phases(pid)}


def purge_pipeline(pid: str) -> bool:
    """Delete the pipeline + its phase rows. Member SEATS are handled separately
    by the abort route (killed/removed); this only drops the orchestration rows."""
    with db.writing() as c:
        c.execute("DELETE FROM pipeline_phases WHERE pipeline_id=?", (pid,))
        cur = c.execute("DELETE FROM pipelines WHERE id=?", (pid,))
        return cur.rowcount > 0
