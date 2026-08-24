# Agent Hub

A **local dashboard** for your native multi-agent terminal sessions
(Hermes / Claude Code / Codex), **read-only toward the seats you drive yourself**.
It does not embed terminals and does not stream them — that is what makes it fast.
Instead:

- The web page shows **status at a glance** for every agent, grouped by project:
  working / waiting-for-input / idle / exited / unknown, plus a preview of each
  agent's last output.
- When you want to act, you click **跳到终端 (Jump)** and your selected native
  terminal switches to that agent. Each browser remembers its own tmux client,
  so a dashboard reached over SSH can drive the remote viewer without switching
  the terminal left open on the Mac. You keep using your fast, native terminal.

tmux is the invisible plumbing (agents survive window closes; the backend reads
their output read-only). You never type a tmux command yourself.

## Design boundaries

- Binds `127.0.0.1` only. A persistent owner-only token bootstraps a long-lived
  `HttpOnly`, `SameSite=Strict` browser cookie; loopback-Host and same-Origin
  checks guard the API against DNS-rebinding. No remote access, no email/calendar.
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

It prints a URL with a token — open it once per browser profile. The token is
generated locally on first run and stored in the owner-only `data/token`
(gitignored); the bootstrap writes a persistent browser cookie, so Agent Hub,
the browser, or an SSH tunnel may restart without another token. The token is
never embedded in the page JavaScript. The server binds `127.0.0.1` only.
Nothing is hard-coded to a machine — paths derive from wherever you cloned it.

Want it to auto-start on login (and restart if it crashes)? Register a per-user
launchd service — the plist is generated from your clone location, no personal
paths baked in:

```sh
./run.sh install        # start now + on every login (macOS)
./run.sh uninstall      # remove the service (repo, venv and data/ untouched)
```

To get a viewer terminal that the web can drive, run once in any terminal (after
you've started at least one seat), then select that tty from the dashboard's
**取景器** menu:

```sh
tmux attach
```

With multiple attached terminals, the menu shows each tty, dimensions, and
current session. Its selection is stored per browser. **自动（最宽终端）** keeps
the old widest-client behavior when no explicit viewer is needed.

When the dashboard and viewer are on another Mac over SSH, the server can
switch that tmux client but cannot raise applications on the client Mac. Install
the bundled loopback-only iTerm2 helper **on the client Mac** to restore one-click
foregrounding (Python 3.8+; no particular macOS version needs to be configured):

```sh
curl -fsSL https://raw.githubusercontent.com/quzhijie/agent_hub/main/client-focus/install.sh | bash
```

The helper is a user LaunchAgent listening only on `127.0.0.1:18788`; it exposes
one fixed action—activate iTerm2—and accepts browser requests only from loopback
Agent Hub origins. No SSH command changes are required. To remove it:

```sh
curl -fsSL https://raw.githubusercontent.com/quzhijie/agent_hub/main/client-focus/uninstall.sh | bash
```

Then create a project (its root dir), add seats (agents), click **启动** to
launch each into tmux, and use **跳到终端** to jump.

## Project Core registration (optional)

When Project Core's workflow gateway is running, an ordinary Agent Hub seat can
be explicitly associated without copying Project/Workstream IDs. In the Agent
Hub Project dialog, **根据根目录查找** uses the local Resource binding to list
authorized Project Core Projects; the user binds exactly one or chooses no
binding. That binding is a default for manual seats, not a gate. When creating
a seat, the Workstream picker lists every authorized candidate for its actual
working directory, including candidates from another Project sharing that
directory. The user chooses exactly one Workstream or **不追踪 Project Core**.
The cwd is only a discovery hint and never constitutes tracking consent. No
candidate is auto-selected merely because it is the only match.

After registration, the exact Context Pack is saved under
`data/project_core_handoffs/` with mode `0600` and injected into the seat prompt
as a file reference. The create-seat dialog also accepts a lightweight seat role
(`general`, `plan`, `implement`, or `review`) and an optional current task/gap.
The registered card shows the readable `Project › Workstream` target; the role
is a coordination hint, not Project Core authority. Registration always
re-resolves and authorizes the selected stable Project/Workstream IDs, and never
falls back to a different candidate. A temporarily unavailable target remains
visible and can be retried against the same IDs. A session launched by Project
Core itself is adopted into the same durable association and always tracked.

Provider-internal `/new` (Codex) and `/clear` (Claude) do not create a new tmux
seat, so Agent Hub cannot detect those context resets. After one, use the seat
card's **重新注入当前快照** action. It resends that seat's already-registered
immutable Context Pack and report contract into the live conversation; it does
not fetch newer Project Core state. To hand work from plan to implementation to
review without a pipeline, associate/start each downstream seat after the
upstream result has been adopted into the Workstream. Multiple independent
seats may target the same Workstream, but packs created earlier remain exact
historical snapshots.

Removing a seat archives its card and stops tmux. Restoring it defaults to
**最新上下文重开**: Agent Hub clears the stale opening assignment, creates a
new Project Core association segment, reads the latest accepted brief, and
starts a fresh provider conversation in context-only mode. **继续旧对话** is
an explicit alternative for native Claude/Codex providers; it resumes the
provider's most recent conversation and submits the same refreshed context as
its first turn. Because the CLIs expose “continue/latest” rather than an exact
Agent Hub seat ID, this option inherits their most-recent-conversation caveat.

No selection leaves a completely ordinary seat. Project Core downtime never
prevents creating or starting that untracked seat. An explicitly selected but
unavailable target is retained as an unstarted seat for retry; Agent Hub refuses
to start it until registration succeeds or the user explicitly turns tracking
off. Registered seats receive a session-scoped
`report_checkpoint` CLI contract. Its small JSON report and immutable event are
committed to the local SQLite outbox before the command succeeds; Gateway
downtime is retried in FIFO order. Checkpoints are opt-in: the agent sends one
only when the user explicitly says `check` or `checkpoint` as an instruction to
record it. Ordinary completion edges neither remind the agent nor create a
missing-report warning; one bounded evidence window remains open until a
checkpoint is requested or the seat closes. No second summarizer agent reads
the transcript. Git evidence is bounded to commit/dirty state and relative
changed paths; file contents, diffs, logs, and secrets are excluded.

Closing or unexpectedly losing a tracked seat emits a lifecycle event. Project
Core aggregates reported turns deterministically and exposes pending reports
for human adoption; Agent Hub keeps only an opaque transcript URI. Set
`AGENT_HUB_PROJECT_CORE=0` to disable the integration globally, or
`PROJECT_CORE_WORKFLOW_FILE=/path/to/workflow.json` to use another runtime file.

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

## Authors

Built by **Qu Zhijie** ([@quzhijie](https://github.com/quzhijie)) together with
**Claude** (Anthropic's Claude Code, Opus 4.8).
