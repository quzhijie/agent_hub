"""Deterministic pipeline runner (the "conductor").

No LLM here — a plain linear state machine. Each tick it advances the current
phase of every running pipeline. It NEVER types into a terminal: each step runs
the agent HEADLESS (`claude -p` / `codex exec`), the step prompt fed on stdin
from a file, output tee'd to a per-step log, launched in its own tmux window so
it survives server restarts and you can optionally watch it stream. Running
headless also sidesteps the interactive "trust this folder" dialog that would
otherwise hang a fresh worktree forever.

A step is done when its log carries the DONE sentinel (or its pane dies). Between
steps the pipeline either stops for your approval (gated, the default) or
advances on its own (auto_advance) — either way every step is logged under
`<data>/pipelines/<pid>/step-N.log` for you to review afterwards.

Phase lifecycle:
    pending --launch headless--> running --sentinel / pane dead--> awaiting_approval
    awaiting_approval --you approve (or auto_advance on exit 0)--> done --> next
A step that exits non-zero always stops for you, even in auto mode.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from . import store, templates, tmux, worktree
from .config import DATA_DIR
from .providers.registry import get_provider, is_valid_provider

log = logging.getLogger("agent_hub.orchestrator")

# Where per-pipeline prompt/log files live. Set from settings at app startup;
# defaults to the repo data dir so scripts/tests work without wiring.
_LOG_ROOT = DATA_DIR / "pipelines"

_DONE_RE = re.compile(r"<<<AGENT-HUB-STEP-DONE exit=(\d+)")

# Wraps each headless step: log a header, run the agent with the prompt on stdin,
# tee output to the log, then stamp a sentinel carrying the exit code. Kept as a
# file (not an inline tmux command) so the large prompt never touches the shell.
_RUNNER_SH = r"""#!/usr/bin/env bash
# agent-hub pipeline step runner.  usage: run-step.sh <prompt> <log> <cmd...>
set -o pipefail
prompt="$1"; log="$2"; shift 2
{
  printf '=== agent-hub step start %s ===\n' "$(date '+%F %T')"
  printf 'cmd: %s  (prompt on stdin)\n\n--- prompt ---\n' "$*"
  cat "$prompt"
  printf '\n\n--- output ---\n'
} >>"$log" 2>&1
"$@" <"$prompt" 2>&1 | tee -a "$log"
code="${PIPESTATUS[0]}"
printf '\n<<<AGENT-HUB-STEP-DONE exit=%s at=%s>>>\n' "$code" "$(date '+%F %T')" | tee -a "$log"
"""


class OrchestratorError(RuntimeError):
    pass


def set_log_root(path) -> None:
    global _LOG_ROOT
    _LOG_ROOT = Path(path)


def _pipe_dir(pid: str) -> Path:
    d = _LOG_ROOT / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


def step_log_path(pid: str, idx: int) -> Path:
    return _pipe_dir(pid) / f"step-{idx}.log"


def purge_logs(pid: str) -> None:
    shutil.rmtree(_LOG_ROOT / pid, ignore_errors=True)


# --- creation ---------------------------------------------------------------

def launch_pipeline(project_id: str, name: str, steps: list[dict],
                    outline_path: str | None = None, source: str = "custom",
                    auto_advance: bool = False) -> dict:
    """Create the isolated worktree, one seat per step, and the pipeline rows.
    `steps` is an ordered list of {role, prompt, provider}. Does NOT start any
    agent; the tick loop drives step 0. `{sentinel}`/`{base_branch}` tokens are
    substituted here. `outline_path`, if given, is copied into the worktree as
    OUTLINE.md. `auto_advance` runs every step without stopping at each gate."""
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
        if get_provider(prov).resolve_headless_command() is None:
            raise OrchestratorError(f"provider {prov} 不支持无人值守流水线（无 headless 模式）")

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
        # write the shared runner once
        runner = _pipe_dir(pid) / "run-step.sh"
        runner.write_text(_RUNNER_SH)
        runner.chmod(0o755)
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
                                     path, branch, base, phases, auto_advance=auto_advance)
    except Exception:
        for sid in created_seats:
            store.purge_session(sid)
        worktree.remove(root, path)
        purge_logs(pid)
        raise


# --- the state machine ------------------------------------------------------

def _launch_step(ph: dict, seat: dict) -> None:
    """Start one step headless in its own tmux window. The prompt goes to a file
    and is fed on stdin — nothing is ever typed into the terminal."""
    name = seat["tmux_session"]
    if tmux.has_session(name):
        tmux.kill_session(name)          # clear any stale corpse before (re)launch
    headless = get_provider(seat["provider"]).resolve_headless_command()
    if headless is None:                 # guarded against at creation, belt-and-braces
        raise OrchestratorError(f"provider {seat['provider']} has no headless mode")
    d = _pipe_dir(ph["pipeline_id"])
    runner = d / "run-step.sh"
    if not runner.exists():
        runner.write_text(_RUNNER_SH); runner.chmod(0o755)
    prompt_f = d / f"step-{ph['idx']}.prompt"
    log_f = d / f"step-{ph['idx']}.log"
    prompt_f.write_text(ph["prompt"])
    # data-dir paths are space-free, so a plain argv is safe; tmux execs bash
    # with these args and the runner does the stdin redirect + tee itself.
    cmd = f"bash {runner} {prompt_f} {log_f} {headless}"
    log.info("pipeline %s: launching step %s headless in %s",
             ph["pipeline_id"][:8], ph["role"], name)
    tmux.new_session(name, seat["working_dir"], cmd)
    store.mark_started(seat["id"])


def _step_finished(ph: dict, seat: dict) -> tuple[bool, int | None]:
    """(finished?, exit_code). The log sentinel is authoritative (survives server
    restarts and vanished sessions); a dead pane with no sentinel means it died
    mid-run."""
    code = _read_exit(step_log_path(ph["pipeline_id"], ph["idx"]))
    if code is not None:
        return True, code
    name = seat["tmux_session"] if seat else None
    if name and tmux.has_session(name) and tmux.pane_dead(name):
        return True, None
    return False, None


def _read_exit(log_path: Path) -> int | None:
    try:
        text = log_path.read_text()
    except OSError:
        return None
    m = None
    for m in _DONE_RE.finditer(text):
        pass
    return int(m.group(1)) if m else None


def _goto_next(pid: str, i: int, n_phases: int) -> None:
    if i + 1 >= n_phases:
        store.update_pipeline(pid, status="done")
    else:
        store.update_pipeline(pid, phase_index=i + 1)   # next phase pending; tick starts it


def _advance(pl: dict) -> None:
    phases = store.pipeline_phases(pl["id"])
    i = pl["phase_index"]
    if i >= len(phases):
        store.update_pipeline(pl["id"], status="done")
        return
    ph = phases[i]
    seat = store.get_session(ph["seat_id"])
    st = ph["status"]
    if st == "pending":
        _launch_step(ph, seat)
        store.update_phase(ph["id"], status="running")
    elif st == "running":
        done, code = _step_finished(ph, seat)
        if done:
            if code == 0 and pl["auto_advance"]:
                store.update_phase(ph["id"], status="done")
                _goto_next(pl["id"], i, len(phases))
            else:
                # gated, or a non-zero/unknown exit even in auto mode -> stop for you.
                store.update_phase(ph["id"], status="awaiting_approval")
    # awaiting_approval: wait for approve_phase (or a fully-auto step never lands here).


def tick() -> None:
    """Called once per sampler cycle. Best-effort per pipeline: one failing
    pipeline never blocks the others."""
    for pl in store.list_pipelines(status="running"):
        try:
            _advance(pl)
        except Exception:
            log.exception("pipeline %s advance failed", pl["id"][:8])


# --- user actions -----------------------------------------------------------

def approve_phase(pid: str) -> dict:
    """Approve the current awaiting-approval phase and advance to the next."""
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
    _goto_next(pid, i, len(phases))
    return store.get_pipeline(pid)


def abort_pipeline(pid: str) -> dict:
    """Kill this pipeline's seats and mark it aborted. The worktree + branch +
    logs are LEFT in place so you can still inspect or merge the work."""
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
