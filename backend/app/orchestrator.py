"""Deterministic pipeline runner (the "conductor").

There is NO LLM in charge here — this is a plain linear state machine. Each tick
it looks at the current phase of every running pipeline and advances it. The one
and only place that types INTO a seat is `_send`, which first enforces a
hardcoded allowlist: a pipeline may only ever write to its OWN seats, and only
seats explicitly flagged `orchestrated`. Your interactive seats can never be
reached from here.

Phase lifecycle:
    pending  --launch seat-->  starting  --seat idle/ready, send prompt-->  running
    running  --seat went active then idle-->  awaiting_approval  --you approve-->  done
Every phase is gated: nothing advances to the next phase without your approval.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from . import store, templates, tmux, worktree
from .providers.registry import get_provider, is_valid_provider

log = logging.getLogger("agent_hub.orchestrator")


class OrchestratorError(RuntimeError):
    pass


# --- creation ---------------------------------------------------------------

def launch_pipeline(project_id: str, name: str, steps: list[dict],
                    outline_path: str | None = None, source: str = "custom") -> dict:
    """Create the isolated worktree, one orchestrated seat per step, and the
    pipeline rows. `steps` is an arbitrary-length ordered list of
    {role, prompt, provider} — from a template preset, a parsed outline, or hand
    edits, all the same to the runner. Does NOT start any agent; the tick loop
    drives step 0.

    `{sentinel}`/`{base_branch}` tokens in a prompt are substituted here (safe
    literal replace, so braces in your text never break). If `outline_path` is
    given it's copied into the worktree as OUTLINE.md for the agents to read.
    """
    proj = store.get_project(project_id)
    if proj is None:
        raise OrchestratorError("project not found")
    if not steps:
        raise OrchestratorError("流水线至少要有一步")
    root = proj["root_dir"]
    if not worktree.is_git_repo(root):
        raise OrchestratorError("v1 隔离需要 git 仓库；该项目根目录不是 git 仓库")
    for st in steps:
        if not st.get("prompt", "").strip():
            raise OrchestratorError("每一步都需要一段 prompt")
        prov = st.get("provider") or "claude"
        if not is_valid_provider(prov):
            raise OrchestratorError(f"未知 provider: {prov}")

    pid = store.new_id()
    branch = f"agent-hub/pipe-{pid[:8]}"
    path = worktree.default_path(root, pid)
    base = worktree.current_branch(root)

    worktree.create(root, branch, path, base)
    created_seats: list[str] = []
    try:
        if outline_path:
            src = Path(outline_path).expanduser()
            if src.is_file():
                shutil.copy(src, Path(path) / "OUTLINE.md")
        phases: list[dict] = []
        for i, st in enumerate(steps):
            role = (st.get("role") or f"step-{i + 1}").strip()
            provider = st.get("provider") or "claude"
            prompt = (st["prompt"].replace("{sentinel}", templates.SENTINEL)
                                  .replace("{base_branch}", base))
            seat = store.create_session(
                project_id, name=role, provider=provider,
                working_dir=path, launch_command="", orchestrated=True)
            created_seats.append(seat["id"])
            phases.append({"role": role, "seat_id": seat["id"], "prompt": prompt})
        return store.create_pipeline(pid, project_id, name, name, source,
                                     path, branch, base, phases)
    except Exception:
        # roll back the half-built pipeline so a failure never leaves an orphan
        for sid in created_seats:
            store.purge_session(sid)
        worktree.remove(root, path)
        raise


# --- the guarded write path (the ONLY writer) -------------------------------

def _send(pl: dict, phase: dict, seat: dict) -> None:
    members = store.pipeline_member_seat_ids(pl["id"])
    if phase["seat_id"] not in members:
        raise OrchestratorError("refusing to write: seat is not a member of this pipeline")
    if not seat or not seat.get("orchestrated"):
        raise OrchestratorError("refusing to write: seat is not orchestrated")
    log.info("pipeline %s: sending phase %s prompt to %s", pl["id"][:8],
             phase["role"], seat["tmux_session"])
    tmux.send_text(seat["tmux_session"], phase["prompt"])


# --- the state machine ------------------------------------------------------

def _ensure_seat_started(seat: dict) -> None:
    name = seat["tmux_session"]
    if tmux.has_session(name):
        if tmux.pane_dead(name):
            tmux.kill_session(name)
        else:
            if not seat["started_at"]:
                store.mark_started(seat["id"])
            return
    provider = get_provider(seat["provider"])
    cmd = provider.resolve_command(seat["launch_command"])
    tmux.new_session(name, seat["working_dir"], cmd)
    store.mark_started(seat["id"])


def _advance(pl: dict) -> None:
    phases = store.pipeline_phases(pl["id"])
    i = pl["phase_index"]
    if i >= len(phases):
        store.update_pipeline(pl["id"], status="done")
        return
    ph = phases[i]
    st = ph["status"]
    if st == "pending":
        _ensure_seat_started(store.get_session(ph["seat_id"]))
        store.update_phase(ph["id"], status="starting")
    elif st == "starting":
        seat = store.get_session(ph["seat_id"])
        # wait until the agent has booted and its input box is ready (settled),
        # so the prompt lands in the box instead of a half-painted screen.
        if seat and store.is_settled(seat["status"]):
            _send(pl, ph, seat)
            store.update_phase(ph["id"], status="running", saw_active=0)
    elif st == "running":
        seat = store.get_session(ph["seat_id"])
        s = seat["status"] if seat else store.UNKNOWN
        if s == store.ACTIVE and not ph["saw_active"]:
            store.update_phase(ph["id"], saw_active=1)
        elif store.is_settled(s) and ph["saw_active"]:
            # phase produced its output and settled (空闲/已完成) — hand to the gate.
            store.update_phase(ph["id"], status="awaiting_approval")
        # WAITING (a prompt), EXITED, or not-yet-active: leave as running; the UI
        # surfaces the live seat status so you can go look. We never auto-answer.


def tick() -> None:
    """Called once per sampler cycle (statuses are fresh). Best-effort per
    pipeline: one failing pipeline never blocks the others."""
    for pl in store.list_pipelines(status="running"):
        try:
            _advance(pl)
        except Exception:
            log.exception("pipeline %s advance failed", pl["id"][:8])


# --- user actions -----------------------------------------------------------

def approve_phase(pid: str) -> dict:
    """Approve the current awaiting-approval phase and advance to the next
    (or finish). This is the only thing that moves a pipeline between phases."""
    pl = store.get_pipeline(pid)
    if pl is None:
        raise OrchestratorError("pipeline not found")
    if pl["status"] != "running":
        raise OrchestratorError(f"pipeline is {pl['status']}, not running")
    phases = store.pipeline_phases(pid)
    i = pl["phase_index"]
    ph = phases[i]
    if ph["status"] != "awaiting_approval":
        raise OrchestratorError("当前 phase 还没完成，无法批准")
    store.update_phase(ph["id"], status="done")
    if i + 1 >= len(phases):
        store.update_pipeline(pid, status="done")
    else:
        store.update_pipeline(pid, phase_index=i + 1)   # next phase pending; tick starts it
    return store.get_pipeline(pid)


def abort_pipeline(pid: str) -> dict:
    """Kill this pipeline's seats and mark it aborted. The worktree + branch are
    LEFT in place so you can still inspect or merge the work."""
    pl = store.get_pipeline(pid)
    if pl is None:
        raise OrchestratorError("pipeline not found")
    for ph in store.pipeline_phases(pid):
        seat = store.get_session(ph["seat_id"])
        if not seat:
            continue
        name = seat["tmux_session"]
        if store.tmux_name_exists(name) and tmux.has_session(name):
            tmux.kill_session(name)
        store.mark_removed(seat["id"])
    return store.update_pipeline(pid, status="aborted")
