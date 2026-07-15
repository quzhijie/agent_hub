"""Orchestrator: the linear state machine that runs each step HEADLESS.

tmux is fully mocked so no real sessions/processes happen; step completion is
simulated by writing the DONE sentinel into the step's log file (what the real
runner script does). worktree/git is a throwaway repo under tmp_path.
"""
import subprocess


def _git_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "init"], cwd=root, check=True)
    return root


def _mock_tmux(monkeypatch, launched):
    from app import tmux
    live = set()
    monkeypatch.setattr(tmux, "has_session", lambda n: n in live)
    monkeypatch.setattr(tmux, "pane_dead", lambda n: False)
    monkeypatch.setattr(tmux, "new_session",
                        lambda name, wd, cmd, **k: (live.add(name), launched.__setitem__(name, cmd)))
    monkeypatch.setattr(tmux, "kill_session", lambda n: live.discard(n))
    return live


def _finish(orchestrator, pid, idx, code=0):
    """Simulate the headless runner finishing: stamp the log's exit sentinel."""
    orchestrator.step_log_path(pid, idx).write_text(f"...output...\n<<<AGENT-HUB-STEP-DONE exit={code}>>>\n")


def test_pipeline_gated_runs_headless_and_waits_at_each_gate(store_db, tmp_path, monkeypatch):
    from app import orchestrator, store
    orchestrator.set_log_root(tmp_path / "logs")
    proj = store.create_project("P", str(_git_repo(tmp_path)))
    launched = {}
    _mock_tmux(monkeypatch, launched)

    steps = [{"role": r, "prompt": f"do {r}"} for r in ("plan", "implement", "review")]
    pl = orchestrator.launch_pipeline(proj["id"], "task", steps)   # gated (auto_advance defaults off)
    phases = store.pipeline_phases(pl["id"])
    assert [p["role"] for p in phases] == ["plan", "implement", "review"]

    def phase(i):
        return store.get_phase(phases[i]["id"])

    # phase 0: pending -> running, launched HEADLESS (claude -p), prompt via FILE not keystrokes
    orchestrator.tick()
    assert phase(0)["status"] == "running"
    name0 = store.get_session(phases[0]["seat_id"])["tmux_session"]
    assert "-p --dangerously-skip-permissions" in launched[name0]
    assert (orchestrator._pipe_dir(pl["id"]) / "step-0.prompt").read_text() == "do plan"

    # step finishes (sentinel in log) -> gated -> awaiting_approval, does NOT advance
    _finish(orchestrator, pl["id"], 0)
    orchestrator.tick()
    assert phase(0)["status"] == "awaiting_approval"
    assert store.get_pipeline(pl["id"])["phase_index"] == 0        # gate holds

    # approve -> phase 1 launches on the next tick
    orchestrator.approve_phase(pl["id"])
    assert store.get_pipeline(pl["id"])["phase_index"] == 1
    orchestrator.tick()
    assert phase(1)["status"] == "running"


def test_pipeline_auto_advance_runs_all_steps(store_db, tmp_path, monkeypatch):
    from app import orchestrator, store
    orchestrator.set_log_root(tmp_path / "logs")
    proj = store.create_project("P", str(_git_repo(tmp_path)))
    _mock_tmux(monkeypatch, {})

    steps = [{"role": r, "prompt": f"do {r}"} for r in ("a", "b", "c")]
    pl = orchestrator.launch_pipeline(proj["id"], "t", steps, auto_advance=True)

    # each step: one tick launches it, finishing + one tick auto-advances (no approval)
    for i in range(3):
        orchestrator.tick()                       # launch step i
        assert store.pipeline_phases(pl["id"])[i]["status"] == "running"
        _finish(orchestrator, pl["id"], i)
        orchestrator.tick()                       # auto: mark done + advance
    assert store.get_pipeline(pl["id"])["status"] == "done"
    assert all(p["status"] == "done" for p in store.pipeline_phases(pl["id"]))


def test_auto_advance_stops_on_nonzero_exit(store_db, tmp_path, monkeypatch):
    """A failed step must halt for a human even in auto mode — no blind cascade."""
    from app import orchestrator, store
    orchestrator.set_log_root(tmp_path / "logs")
    proj = store.create_project("P", str(_git_repo(tmp_path)))
    _mock_tmux(monkeypatch, {})

    pl = orchestrator.launch_pipeline(proj["id"], "t",
                                      [{"role": "a", "prompt": "x"}, {"role": "b", "prompt": "y"}],
                                      auto_advance=True)
    orchestrator.tick()                           # launch step 0
    _finish(orchestrator, pl["id"], 0, code=1)    # it FAILED
    orchestrator.tick()
    assert store.get_phase(store.pipeline_phases(pl["id"])[0]["id"])["status"] == "awaiting_approval"
    assert store.get_pipeline(pl["id"])["phase_index"] == 0   # did not advance


def test_seats_launch_headless_per_provider(store_db, tmp_path, monkeypatch):
    """Each step launches its provider's headless command (prompt on stdin),
    never an interactive TUI (which would hang on the trust dialog)."""
    from app import orchestrator, store
    orchestrator.set_log_root(tmp_path / "logs")
    proj = store.create_project("P", str(_git_repo(tmp_path)))
    launched = {}
    _mock_tmux(monkeypatch, launched)

    steps = [{"role": "a", "prompt": "x", "provider": "claude"},
             {"role": "b", "prompt": "y", "provider": "codex"}]
    pl = orchestrator.launch_pipeline(proj["id"], "t", steps)
    for ph in store.pipeline_phases(pl["id"]):
        orchestrator._launch_step(ph, store.get_session(ph["seat_id"]))
    byname = {store.get_session(ph["seat_id"])["provider"]:
              store.get_session(ph["seat_id"])["tmux_session"] for ph in store.pipeline_phases(pl["id"])}
    assert "-p --dangerously-skip-permissions" in launched[byname["claude"]]
    assert "exec --dangerously-bypass-approvals-and-sandbox" in launched[byname["codex"]]


def test_abort_kills_seats_and_marks_aborted(store_db, tmp_path, monkeypatch):
    from app import orchestrator, store, tmux
    orchestrator.set_log_root(tmp_path / "logs")
    proj = store.create_project("P", str(_git_repo(tmp_path)))
    killed = []
    monkeypatch.setattr(tmux, "has_session", lambda n: True)
    monkeypatch.setattr(tmux, "kill_session", lambda n: killed.append(n))

    steps = [{"role": r, "prompt": f"do {r}"} for r in ("plan", "implement", "review")]
    pl = orchestrator.launch_pipeline(proj["id"], "n", steps)
    orchestrator.abort_pipeline(pl["id"])
    assert store.get_pipeline(pl["id"])["status"] == "aborted"
    assert len(killed) == 3                                    # all three seats killed
    assert all(store.get_session(p["seat_id"])["removed_at"]
               for p in store.pipeline_phases(pl["id"]))


def test_interactive_launch_stays_bare():
    """Hand-driven (non-orchestrated) seats launch the plain binary — the bypass
    flags live only on the headless pipeline path, never the interactive one."""
    from app.providers.registry import get_provider
    claude, codex = get_provider("claude"), get_provider("codex")
    assert "--dangerously-skip-permissions" not in claude.resolve_command("")
    assert "--dangerously-bypass-approvals-and-sandbox" not in codex.resolve_command("")
    # …but the headless command carries them
    assert "-p --dangerously-skip-permissions" in claude.resolve_headless_command()
