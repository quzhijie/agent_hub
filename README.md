# Agent Hub

A **local dashboard** for your native multi-agent terminal sessions
(Hermes / Claude Code / Codex), **read-only toward the seats you drive yourself**.
It does not embed terminals and does not stream them — that is what makes it fast.
Instead:

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
- The backend **never sends keystrokes to any seat**. Status comes only from
  read-only `capture-pane`; jumping only points a client via `switch-client`.
  The optional **pipeline orchestrator** doesn't type into terminals either: it
  runs each step **headless** (`claude -p` / `codex exec`) with the prompt fed on
  stdin from a file, so a step can never stall on a permission/trust dialog and
  needs no TTY. See **Pipelines** below.
- Workbench state lives under `data/` here; the dashboard itself writes nothing
  into project repos. Pipelines are the deliberate exception: each creates a
  `git` branch + a sibling worktree and its agents commit *there*, never onto
  your checked-out branch.

## Install & run

Requirements: **Python ≥ 3.11** and **tmux ≥ 3.0** (`brew install python tmux` on macOS).

```sh
git clone https://github.com/quzhijie/agent_hub.git
cd agent_hub
./run.sh                # first run creates a venv, then starts the server
```

It prints a URL with a token — open it. The token is generated locally on first
run and stored in `data/token` (gitignored); the server binds `127.0.0.1` only.
Nothing is hard-coded to a machine — paths derive from wherever you cloned it.

Want it to auto-start on login (and restart if it crashes)? Register a per-user
launchd service — the plist is generated from your clone location, no personal
paths baked in:

```sh
./run.sh install        # start now + on every login (macOS)
./run.sh uninstall      # remove the service (repo, venv and data/ untouched)
```

To get a viewer terminal that the web can drive, run once in any terminal (after
you've started at least one seat):

```sh
tmux attach
```

Then create a project (its root dir), add seats (agents), click **启动** to
launch each into tmux, and use **跳到终端** to jump.

For the full desktop + mobile (handmux) walkthrough, see **[USAGE.md](USAGE.md)**.

## Pipelines (optional)

Beyond watching seats, the dashboard can **orchestrate** a multi-step task as a
linear pipeline (e.g. `plan → implement → review`). Each step runs one agent
**headless** (`claude -p` / `codex exec`, prompt on stdin, no TTY, no prompts) in
a dedicated `git` worktree + branch, tee'ing its output to a per-step log under
`data/pipelines/<id>/`. By default it **stops for your approval** after each step
(you review the log, click 继续); tick the **全自动** box at create time to run
straight through instead — a step that exits non-zero always stops for you. The
whole run is isolated in the worktree, so it never touches your working tree.

There is **no LLM in charge** — the orchestrator (`backend/app/orchestrator.py`)
is a deterministic state machine that never types into a terminal; it launches
headless steps and reads their logs. It's on by default but idle until you
create a pipeline. Build the steps from a built-in template or by parsing an
`OUTLINE.md` (split into steps by the one repeating heading level / numbering /
checkboxes), then edit/reorder them before launch. To get an outline that splits
cleanly and encodes the worktree/path decisions up front, write it with the
`/pipeline-outline` Claude Code skill — a copy is **bundled in [`skills/`](skills/)**
so the clone is self-contained; run `./run.sh link-skills` to install it into
`~/.claude/skills/` (or hand-write the outline — the skill is optional). Full
walkthrough in **[USAGE.md](USAGE.md)**.

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
