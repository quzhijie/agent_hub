"""Orchestrator: the hardcoded write allowlist + the linear state machine.

tmux is fully mocked so no real sessions/keystrokes happen. worktree/git is only
touched by the end-to-end test, which builds a throwaway repo under tmp_path.
"""
import subprocess

import pytest


def _seat(store, pid, name, orchestrated=False):
    return store.create_session(pid, name, "claude", "/tmp", "", orchestrated=orchestrated)


def _pipeline_with(store, pid, seat_ids):
    phases = [{"role": f"r{i}", "seat_id": sid, "prompt": f"prompt-{i}"}
              for i, sid in enumerate(seat_ids)]
    return store.create_pipeline(store.new_id(), pid, "n", "task", "code",
                                 "/tmp/wt", "branch", "base", phases)


# --- the safety line: _send only ever writes to the pipeline's own seats -----

def test_send_writes_only_to_member_orchestrated_seat(store_db, monkeypatch):
    from app import orchestrator, store, tmux
    sent = []
    monkeypatch.setattr(tmux, "send_text", lambda n, t, submit=True: sent.append((n, t)))

    proj = store.create_project("P", "/tmp")
    member = _seat(store, proj["id"], "plan", orchestrated=True)
    pl = _pipeline_with(store, proj["id"], [member["id"]])
    phase = store.pipeline_phases(pl["id"])[0]

    orchestrator._send(pl, phase, store.get_session(member["id"]))
    assert sent == [(member["tmux_session"], "prompt-0")]


def test_send_refuses_a_non_member_seat(store_db, monkeypatch):
    from app import orchestrator, store, tmux
    monkeypatch.setattr(tmux, "send_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote!")))

    proj = store.create_project("P", "/tmp")
    member = _seat(store, proj["id"], "plan", orchestrated=True)
    outsider = _seat(store, proj["id"], "my-own-agent", orchestrated=True)  # NOT in this pipeline
    pl = _pipeline_with(store, proj["id"], [member["id"]])
    phase = store.pipeline_phases(pl["id"])[0]

    # a phase pointed at a seat outside the pipeline must be refused, even though
    # that seat is itself orchestrated — membership is checked structurally.
    with pytest.raises(orchestrator.OrchestratorError):
        orchestrator._send(pl, {**phase, "seat_id": outsider["id"]},
                           store.get_session(outsider["id"]))


def test_send_refuses_a_non_orchestrated_seat(store_db, monkeypatch):
    from app import orchestrator, store, tmux
    monkeypatch.setattr(tmux, "send_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote!")))

    proj = store.create_project("P", "/tmp")
    interactive = _seat(store, proj["id"], "interactive")   # orchestrated=0
    pl = _pipeline_with(store, proj["id"], [interactive["id"]])  # member, but not orchestrated
    phase = store.pipeline_phases(pl["id"])[0]

    with pytest.raises(orchestrator.OrchestratorError):
        orchestrator._send(pl, phase, store.get_session(interactive["id"]))


# --- the linear state machine, end to end (mock tmux, real git worktree) -----

def test_pipeline_runs_phases_with_gates(store_db, tmp_path, monkeypatch):
    from app import orchestrator, store, tmux

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "init"], cwd=root, check=True)
    proj = store.create_project("P", str(root))

    live, sent = set(), []
    monkeypatch.setattr(tmux, "has_session", lambda n: n in live)
    monkeypatch.setattr(tmux, "pane_dead", lambda n: False)
    monkeypatch.setattr(tmux, "new_session", lambda name, wd, cmd, **k: live.add(name))
    monkeypatch.setattr(tmux, "kill_session", lambda n: live.discard(n))
    monkeypatch.setattr(tmux, "send_text", lambda n, t, submit=True: sent.append((n, t)))

    steps = [{"role": r, "prompt": f"do {r}"} for r in ("plan", "implement", "review")]
    pl = orchestrator.launch_pipeline(proj["id"], "task name", steps)
    phases = store.pipeline_phases(pl["id"])
    assert [p["role"] for p in phases] == ["plan", "implement", "review"]
    assert all(store.get_session(p["seat_id"])["orchestrated"] for p in phases)

    def phase(i):
        return store.get_phase(phases[i]["id"])

    # phase 0: pending -> starting (seat launched, no prompt yet)
    orchestrator.tick()
    assert phase(0)["status"] == "starting" and not sent

    # agent boots -> idle -> prompt is sent, phase running
    store.update_status(phases[0]["seat_id"], store.IDLE, "", False)
    orchestrator.tick()
    assert phase(0)["status"] == "running"
    assert sent and sent[-1][1] == phases[0]["prompt"]

    # agent works then settles -> awaiting_approval (does NOT auto-advance)
    store.update_status(phases[0]["seat_id"], store.ACTIVE, "", False)
    orchestrator.tick()
    store.update_status(phases[0]["seat_id"], store.IDLE, "", False)
    orchestrator.tick()
    assert phase(0)["status"] == "awaiting_approval"
    assert store.get_pipeline(pl["id"])["phase_index"] == 0   # gate holds

    # approve -> advance to phase 1; phase 0 sends only once
    sent_after_p0 = len(sent)
    orchestrator.approve_phase(pl["id"])
    assert store.get_pipeline(pl["id"])["phase_index"] == 1
    orchestrator.tick()                                        # phase 1 pending -> starting
    store.update_status(phases[1]["seat_id"], store.IDLE, "", False)
    orchestrator.tick()                                        # -> running, sends phase-1
    assert phase(1)["status"] == "running"
    assert sent[-1][1] == phases[1]["prompt"] and len(sent) == sent_after_p0 + 1


def test_abort_kills_seats_and_marks_aborted(store_db, tmp_path, monkeypatch):
    from app import orchestrator, store, tmux
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "init"], cwd=root, check=True)
    proj = store.create_project("P", str(root))
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
