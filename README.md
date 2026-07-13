# Agent Hub

A **local, read-only dashboard** for your native multi-agent terminal sessions
(Hermes / Claude Code / Codex). It does not embed terminals and does not stream
them — that is what makes it fast. Instead:

- The web page shows **status at a glance** for every agent, grouped by project:
  working / waiting-for-input / idle / exited / unknown, plus a preview of each
  agent's last output.
- When you want to act, you click **跳到终端 (Jump)** and your *one* native
  terminal window switches to that agent. You keep using your fast, native
  terminal. No lag, ever — even with dozens of agents.

tmux is the invisible plumbing (agents survive window closes; the backend reads
their output read-only). You never type a tmux command yourself.

## Design boundaries

- Binds `127.0.0.1` only. Token + loopback-Host + same-Origin checks guard the
  API against DNS-rebinding. No remote access, no tunnels, no email/calendar.
- Seats live on tmux's shared default socket, so they also show up in your
  normal `tmux` and in handmux on your phone. kill/switch stay safe: the backend
  only ever kills sessions it registered (named `hub-<project>-<seat>-<id>`).
- The backend **never sends keystrokes** to any session. Status comes only from
  read-only `capture-pane`; jumping only points a client via `switch-client`.
- Workbench state lives under `data/` here; nothing is written to project repos.

## Run

```sh
cd /Users/quzhijie/tools/agent_hub
./run.sh
```

It prints a URL with the token — open it. To get a viewer terminal that the web
can drive, run once in any terminal (after you've started at least one seat):

```sh
tmux attach
```

Then create a project (its root dir), add seats (agents), click **启动** to
launch each into tmux, and use **跳到终端** to jump.

For the full desktop + mobile (handmux) walkthrough, see **[USAGE.md](USAGE.md)**.

## Develop / test

```sh
./run.sh test     # runs pytest in the venv
```

## Status detection

The hard part. TUI agents redraw a full screen each frame, so status is
heuristic: the main signal is "did the pane change between samples", refined by
provider-specific patterns (`backend/app/providers/*.py`). Rules fall back to
`unknown` rather than guessing `idle`. Refine them against real captured frames
— see `tests/test_providers_status.py`.
