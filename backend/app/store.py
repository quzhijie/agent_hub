"""Repository functions over the SQLite tables. Rows returned as plain dicts."""
from __future__ import annotations

import json
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

def create_project(
    name: str, root_dir: str, project_core_tracking: str = "off",
    project_core_project_id: str = "", project_core_project_title: str = "",
) -> dict:
    pid = new_id()
    ts = now_iso()
    with db.writing() as c:
        c.execute(
            "INSERT INTO projects (id, name, root_dir, created_at, updated_at, is_removed,"
            " project_core_tracking, project_core_project_id, project_core_project_title,"
            " sort_order)"
            " VALUES (?,?,?,?,?,0,?,?,?,"
            " COALESCE((SELECT MAX(sort_order)+1 FROM projects), 0))",
            (
                pid, name, root_dir, ts, ts, project_core_tracking,
                project_core_project_id, project_core_project_title,
            ),
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
                   notes: str | None = None, root_dir: str | None = None,
                   project_core_tracking: str | None = None,
                   project_core_project_id: str | None = None,
                   project_core_project_title: str | None = None) -> dict | None:
    fields, vals = [], []
    if name is not None:
        fields.append("name=?"); vals.append(name)
    if is_removed is not None:
        fields.append("is_removed=?"); vals.append(1 if is_removed else 0)
    if notes is not None:
        fields.append("notes=?"); vals.append(notes)
    if root_dir is not None:
        fields.append("root_dir=?"); vals.append(root_dir)
    if project_core_tracking is not None:
        fields.append("project_core_tracking=?"); vals.append(project_core_tracking)
    if project_core_project_id is not None:
        fields.append("project_core_project_id=?"); vals.append(project_core_project_id)
    if project_core_project_title is not None:
        fields.append("project_core_project_title=?"); vals.append(project_core_project_title)
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
                   launch_command: str, model: str = "", orchestrated: bool = False,
                   initial_prompt: str = "",
                   project_core: dict[str, Any] | None = None,
                   project_core_tracking: str = "off",
                   agent_role: str = "general",
                   permission_mode: str = "default") -> dict:
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
            "INSERT INTO sessions (id, project_id, name, provider, model, permission_mode, launch_command,"
            " working_dir, tmux_session, status, last_output, created_at, orchestrated,"
            " initial_prompt, project_core_json, project_core_tracking, agent_role, sort_order)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            " COALESCE((SELECT MAX(sort_order)+1 FROM sessions WHERE project_id=?), 0))",
            (sid, project_id, name, provider, model, permission_mode, launch_command, working_dir,
             tmux_session, UNKNOWN, "", ts, 1 if orchestrated else 0,
             initial_prompt, json.dumps(project_core or {}, sort_keys=True),
             project_core_tracking, agent_role, project_id),
        )
        _add_event(c, sid, "created", None, UNKNOWN)
    return get_session(sid)


def get_session(sid: str) -> dict | None:
    with db.connect() as c:
        return _row(c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())


def update_session_project_core(
    sid: str, *, project_core: dict[str, Any], initial_prompt: str,
    project_core_tracking: str | None = None,
) -> dict | None:
    """Persist adapter-owned registration metadata before a seat is started."""
    with db.writing() as c:
        fields = ["project_core_json=?", "initial_prompt=?"]
        values: list[Any] = [
            json.dumps(project_core, ensure_ascii=False, sort_keys=True), initial_prompt,
        ]
        if project_core_tracking is not None:
            fields.append("project_core_tracking=?")
            values.append(project_core_tracking)
        values.append(sid)
        c.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE id=?", values)
    return get_session(sid)


def update_project_core_runtime(
    sid: str, *, lifecycle: str | None = None, warning: str | None = None,
) -> dict | None:
    fields: list[str] = []
    values: list[Any] = []
    if lifecycle is not None:
        fields.append("project_core_lifecycle=?")
        values.append(lifecycle)
    if warning is not None:
        fields.append("project_core_report_warning=?")
        values.append(warning)
    if not fields:
        return get_session(sid)
    values.append(sid)
    with db.writing() as c:
        c.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE id=?", values)
    return get_session(sid)


def list_project_core_runtime_sessions() -> list[dict]:
    with db.connect() as c:
        rows = c.execute(
            """SELECT * FROM sessions
               WHERE project_core_tracking='on'
                 AND project_core_lifecycle IN ('registered', 'active')"""
        ).fetchall()
    return [dict(row) for row in rows]


def list_project_core_turns(sid: str) -> list[dict]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM project_core_turns WHERE session_id=? ORDER BY turn_seq",
            (sid,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_project_core_turn(turn_id: str) -> dict | None:
    with db.connect() as c:
        return _row(c.execute(
            "SELECT * FROM project_core_turns WHERE id=?", (turn_id,)
        ).fetchone())


def pending_project_core_outbox(
    *, limit: int = 50, session_id: str | None = None,
) -> list[dict]:
    now = now_iso()
    where = "state='pending'"
    values: list[object] = []
    if session_id is not None:
        where += " AND session_id=?"
        values.append(session_id)
    values.append(limit)
    with db.connect() as c:
        rows = c.execute(
            f"""SELECT * FROM project_core_outbox
                WHERE {where}
                ORDER BY rowid LIMIT ?""",
            values,
        ).fetchall()
    # Return only the due prefix.  Filtering due rows in SQL would allow a
    # newer event to leapfrog an older event that is waiting on backoff.
    due: list[dict] = []
    for row in rows:
        item = dict(row)
        if item.get("next_attempt_at") and item["next_attempt_at"] > now:
            break
        due.append(item)
    return due


def mark_project_core_outbox_delivered(event_id: str) -> None:
    ts = now_iso()
    with db.writing() as c:
        c.execute(
            """UPDATE project_core_outbox
               SET state='delivered', attempts=attempts+1, next_attempt_at=NULL,
                   last_error='', updated_at=?, delivered_at=?
               WHERE event_id=? AND state='pending'""",
            (ts, ts, event_id),
        )


def mark_project_core_outbox_failed(
    event_id: str, *, error: str, next_attempt_at: str | None, terminal: bool,
) -> None:
    with db.writing() as c:
        c.execute(
            """UPDATE project_core_outbox
               SET state=?, attempts=attempts+1, next_attempt_at=?, last_error=?,
                   updated_at=? WHERE event_id=? AND state='pending'""",
            (
                "dead_letter" if terminal else "pending", next_attempt_at,
                error[:500], now_iso(), event_id,
            ),
        )


def requeue_project_core_dead_letters(sid: str) -> int:
    """Retry immutable envelopes for one owner-selected seat in FIFO order."""
    with db.writing() as c:
        result = c.execute(
            """UPDATE project_core_outbox
               SET state='pending', next_attempt_at=NULL, last_error='', updated_at=?
               WHERE session_id=? AND state='dead_letter'""",
            (now_iso(), sid),
        )
    return int(result.rowcount)


def project_core_metrics(sid: str) -> dict[str, int]:
    with db.connect() as c:
        turn_rows = c.execute(
            """SELECT state, count(*) AS n FROM project_core_turns
               WHERE session_id=? GROUP BY state""",
            (sid,),
        ).fetchall()
        outbox_rows = c.execute(
            """SELECT state, count(*) AS n FROM project_core_outbox
               WHERE session_id=? GROUP BY state""",
            (sid,),
        ).fetchall()
        reminders = c.execute(
            "SELECT COALESCE(sum(reminder_count), 0) FROM project_core_turns WHERE session_id=?",
            (sid,),
        ).fetchone()[0]
        report_bytes = c.execute(
            "SELECT COALESCE(sum(report_bytes), 0) FROM project_core_turns WHERE session_id=?",
            (sid,),
        ).fetchone()[0]
        reports = c.execute(
            "SELECT report_json FROM project_core_turns WHERE session_id=? AND state='reported'",
            (sid,),
        ).fetchall()
    result = {f"turn_{row['state']}": row["n"] for row in turn_rows}
    result.update({f"outbox_{row['state']}": row["n"] for row in outbox_rows})
    result["reminders"] = reminders
    result["report_bytes"] = report_bytes
    result["report_items"] = sum(
        sum(len((json.loads(row["report_json"]) or {}).get(field, []))
            for field in ("accomplished", "decisions_proposed", "blockers", "next_steps"))
        for row in reports
    )
    result["transcript_recoveries"] = 0
    return result


def list_sessions(project_id: str | None = None, include_removed: bool = False) -> list[dict]:
    q = """SELECT sessions.*,
                  (SELECT count(*) FROM project_core_outbox o
                   WHERE o.session_id=sessions.id AND o.state='dead_letter')
                    AS project_core_dead_letter_count,
                  COALESCE((SELECT o.last_error FROM project_core_outbox o
                            WHERE o.session_id=sessions.id AND o.state='dead_letter'
                            ORDER BY o.updated_at DESC, o.rowid DESC LIMIT 1), '')
                    AS project_core_dead_letter_error
           FROM sessions"""
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
            """UPDATE sessions SET started_at=?, status=?, last_activity_at=?,
                      resume_prompt_pending=0 WHERE id=?""",
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


def restore_session(
    sid: str, *, resume_conversation: bool = False, initial_prompt: str | None = None,
) -> dict | None:
    """Restore an archived seat, fresh by default.

    A fresh restore clears ``started_at`` so the next launch receives the new
    initial prompt. An explicit resume preserves it and marks that prompt for
    delivery as the first user turn in the resumed provider conversation.
    """
    with db.writing() as c:
        fields = ["removed_at=NULL", "status=?", "resume_prompt_pending=?"]
        # A preserved ``started_at`` means "resume is available", not that a
        # tmux process is already running. Keep it visibly exited so the card
        # offers the restart button immediately after restore.
        values: list[Any] = [
            EXITED if resume_conversation else UNKNOWN,
            1 if resume_conversation else 0,
        ]
        if not resume_conversation:
            fields.append("started_at=NULL")
            fields.append("provider_session_id=''")
        if initial_prompt is not None:
            fields.append("initial_prompt=?")
            values.append(initial_prompt)
        values.append(sid)
        c.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE id=?", values)
    return get_session(sid)


def update_tmux_session(sid: str, tmux_session: str) -> dict | None:
    """Rename a seat's tmux name (only safe while its session isn't running)."""
    tmux.validate_name(tmux_session)
    with db.writing() as c:
        c.execute("UPDATE sessions SET tmux_session=? WHERE id=?", (tmux_session, sid))
    return get_session(sid)


def update_provider_session_id(sid: str, provider_session_id: str) -> dict | None:
    """Persist the provider-native conversation identity for exact resumes."""
    with db.writing() as c:
        c.execute(
            "UPDATE sessions SET provider_session_id=? WHERE id=?",
            (provider_session_id, sid),
        )
    return get_session(sid)


def get_session_conversation_binding(association_id: str) -> dict | None:
    with db.connect() as c:
        return _row(c.execute(
            """SELECT binding.*,conversation.provider,
                      conversation.provider_session_id,conversation.generation
               FROM session_conversation_bindings binding
               JOIN session_conversations conversation
                 ON conversation.id=binding.conversation_id
               WHERE binding.association_id=?""",
            (association_id,),
        ).fetchone())


def list_session_conversation_bindings(sid: str) -> list[dict]:
    with db.connect() as c:
        rows = c.execute(
            """SELECT binding.*,conversation.provider,
                      conversation.provider_session_id,conversation.generation
               FROM session_conversation_bindings binding
               JOIN session_conversations conversation
                 ON conversation.id=binding.conversation_id
               WHERE binding.session_id=?
               ORDER BY conversation.generation,binding.association_segment,
                        binding.created_at,binding.association_id""",
            (sid,),
        ).fetchall()
    return [dict(row) for row in rows]


def bind_session_conversation(
    sid: str,
    *,
    provider: str,
    provider_session_id: str,
    association_id: str,
    association_segment: int,
    start_message_seq: int | None,
) -> dict:
    """Bind one Project Core association to an immutable native conversation."""
    provider = provider.strip()
    provider_session_id = provider_session_id.strip()
    if not provider or not provider_session_id or not association_id:
        raise ValueError("conversation binding requires provider and association identities")
    if provider in {"codex", "ds4-co", "claude", "ds4"}:
        try:
            provider_session_id = str(uuid.UUID(provider_session_id))
        except ValueError as exc:
            raise ValueError("provider conversation ID must be a UUID") from exc
    if association_segment < 1:
        raise ValueError("conversation association segment must be positive")
    if start_message_seq is not None and start_message_seq < 0:
        raise ValueError("conversation start sequence cannot be negative")
    ts = now_iso()
    with db.transaction() as c:
        existing = c.execute(
            "SELECT * FROM session_conversation_bindings WHERE association_id=?",
            (association_id,),
        ).fetchone()
        conversation = c.execute(
            """SELECT * FROM session_conversations
               WHERE session_id=? AND provider=? AND provider_session_id=?""",
            (sid, provider, provider_session_id),
        ).fetchone()
        if conversation is None:
            conversation_id = new_id()
            generation = int(c.execute(
                """SELECT COALESCE(max(generation),0)+1
                   FROM session_conversations WHERE session_id=?""",
                (sid,),
            ).fetchone()[0])
            c.execute(
                """INSERT INTO session_conversations(
                       id,session_id,generation,provider,provider_session_id,created_at
                   ) VALUES (?,?,?,?,?,?)""",
                (conversation_id, sid, generation, provider, provider_session_id, ts),
            )
        else:
            conversation_id = conversation["id"]
        if existing is not None:
            if existing["session_id"] != sid or existing["conversation_id"] != conversation_id:
                raise ValueError("association is already bound to another conversation")
            row = c.execute(
                "SELECT * FROM session_conversation_bindings WHERE association_id=?",
                (association_id,),
            ).fetchone()
            return dict(row)

        if start_message_seq is not None:
            # A resumed provider conversation starts a new association at this
            # exact boundary. Close any earlier segment that was still open.
            c.execute(
                """UPDATE session_conversation_bindings
                   SET end_message_seq=?
                   WHERE conversation_id=? AND end_message_seq IS NULL""",
                (start_message_seq, conversation_id),
            )
        c.execute(
            """INSERT INTO session_conversation_bindings(
                   association_id,session_id,conversation_id,association_segment,
                   start_message_seq,end_message_seq,created_at
               ) VALUES (?,?,?,?,?,NULL,?)""",
            (
                association_id, sid, conversation_id, association_segment,
                start_message_seq, ts,
            ),
        )
        row = c.execute(
            "SELECT * FROM session_conversation_bindings WHERE association_id=?",
            (association_id,),
        ).fetchone()
    return dict(row)


def close_session_conversation_binding(
    association_id: str, *, end_message_seq: int | None,
) -> dict | None:
    if end_message_seq is not None and end_message_seq < 0:
        raise ValueError("conversation end sequence cannot be negative")
    with db.transaction() as c:
        row = c.execute(
            "SELECT * FROM session_conversation_bindings WHERE association_id=?",
            (association_id,),
        ).fetchone()
        if row is None:
            return None
        if end_message_seq is not None:
            start = row["start_message_seq"]
            if start is None or end_message_seq < start:
                raise ValueError("conversation end precedes its association boundary")
            c.execute(
                """UPDATE session_conversation_bindings SET end_message_seq=?
                   WHERE association_id=? AND end_message_seq IS NULL""",
                (end_message_seq, association_id),
            )
        return _row(c.execute(
            "SELECT * FROM session_conversation_bindings WHERE association_id=?",
            (association_id,),
        ).fetchone())


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
                    phases: list[dict], auto_advance: bool = False) -> dict:
    """phases: [{role, seat_id, prompt}] in order. All rows in one transaction.
    `pid` is supplied by the caller so the worktree/branch can be derived from it
    before the row exists."""
    ts = now_iso()
    with db.writing() as c:
        c.execute(
            "INSERT INTO pipelines (id, project_id, name, task, template, worktree_path,"
            " branch, base_branch, status, phase_index, auto_advance, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?, 'running', 0, ?, ?, ?)",
            (pid, project_id, name, task, template, worktree_path, branch, base_branch,
             1 if auto_advance else 0, ts, ts),
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
