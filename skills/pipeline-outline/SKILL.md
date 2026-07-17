---
name: pipeline-outline
description: Write a pipeline-ready OUTLINE.md for the agent_hub multi-agent pipeline runner — one executable step per `##` heading, non-step context folded into a marked section, worktree/output-path decision made explicit, no over-claims. Use whenever asked to write an outline, a phased plan, or an OUTLINE.md that a fan-out of agents (agent_hub) will split into steps and execute one at a time.
user-invocable: true
allowed-tools:
  - Read
  - Grep
  - Glob
  - Write
  - Edit
  - Bash
---

# Writing a pipeline-ready OUTLINE

You are writing an outline that **agent_hub** (a local multi-agent pipeline
runner) will feed through a *dumb, deterministic splitter* and then execute:
each step is handed to a **separate cold agent** with no conversation context.
Write so the split is clean and each cold agent can act safely. If you follow
this, the outline needs **no post-hoc reframing**.

## The runtime you're writing for (why the rules exist)
- The splitter cuts the outline into steps on **one repeating heading level**
  (`##`). Whatever you make a `##` becomes a pipeline step.
- Each step launches **one agent in its own tmux seat**, in a **git worktree +
  branch** created for the run. Steps run **one at a time**; a step finishes,
  you approve, the next starts.
- The **whole outline is copied into the worktree as `OUTLINE.md`**, so every
  agent can read the global plan — but each is told to *do only its step*.
- Orchestrated agents run with **autonomy flags on (no permission prompts)**.
  So anything a step's actions imply, the agent will just *do*, unattended —
  write your steps with that in mind.

## Hard rules

**R1 — Step delimitation.** Exactly one `#` document title (not a step). Every
**executable step is a `##`** heading, all at the same level. Use `###` only for
structure *inside* a step (never as its own step). Don't let a sub-point,
preface, or appendix become a `##`.

**R2 — Non-step content goes in one marked section.** Global conventions, env
setup, path anchors, philosophy, gotchas, appendix — anything that is *not an
executable step* — folds into a single first section:
`## 0. Global conventions (non-step — every agent reads this)`. Do **not** leave
a separate `## Appendix` or `## Setup` floating as its own step. Keep the
parenthesised **`(non-step …)`** marker in that heading verbatim (or `(非步骤…)`)
— the splitter drops any `##` section whose heading carries it, so it becomes
shared context (still copied into OUTLINE.md) rather than a spawned step. Any
other reference-only `##` section you must keep separate: give it a `(non-step)`
marker too.

**R3 — Every step is self-contained** (its agent starts cold). Give each `##`
step this skeleton:
- **Must-read:** the exact files it must read first (absolute or
  worktree-relative paths).
- **Start condition:** which prior step(s) must be done.
- **Actions:** what to do.
- **Outputs:** exact paths the products land at.
- **Review gate:** PASS / REVISE criteria a skeptical reviewer applies.
- **Done when:** the completion test.

**R4 — Make the output-path / isolation decision explicit.** This is the one that
bites. Decide, per project, and state it in §0 *and* per step:
- **Products belong in the real repo/archive** (durable science outputs, MCMC
  chains, figures): keep the **absolute** paths, and on each such step add a line
  `⚠ this step writes OUTSIDE the worktree to <path>` — the worktree does **not**
  isolate these, and with autonomy flags there is **no undo and no prompt**.
- **You want isolation / a dry-run:** write products to **worktree-relative**
  paths (a designated `out/…` dir), and copy them to their real home only after
  review.
- Mark read-only inputs `⚠ read-only`.
Pick one deliberately — don't leave it implicit.

**R5 — No over-claims.** Any statement stronger than the evidence carries an
explicit uncertainty or limit. Avoid bare "detected / proven / = <value> / the
first …". If a phrasing resembles a claim that was previously retracted in the
project, flag it in the step rather than asserting it.

**R6 — Fan-out & barriers.** Note which steps are embarrassingly parallel and
which are **barriers** (need all prior results). Size each step so **one agent
can finish it in one sitting** — split the too-big, merge the too-small.

**R7 — The final review step also writes the report (don't spawn a separate
one).** The last review/meta-review step's agent already reads every prior
output in order to review it — so make **that same step** also emit the
human-facing report in the same pass. **Do not add a fresh `##` step for the
report**: a cold report agent would re-read every output from scratch, paying a
full second read pass for context the reviewer already has. Instead extend the
final review step's **Actions** and **Outputs**:
- After completing its review, the agent writes **`REPORT.md`** covering, in
  order: (a) **what was done & how** — the methods and which code/scripts
  produced each result; (b) **results / findings**, each tied to the specific
  output that supports it; (c) **every figure produced** — path + one line on
  what it shows and its takeaway; (d) **limitations / caveats / open questions**,
  including anything the review just flagged (honor R5 — no claim beyond the
  data). It *summarizes existing outputs* — it does not re-run analysis or
  generate new products.
- **Outputs:** the review verdict **plus** `REPORT.md` — a review artifact, so
  **worktree-relative by default** per R4 (worktree root), unless §0 says
  reports belong in the repo.
- The report thus costs only extra **output** tokens, not a second full read.

## Skeleton to copy

```markdown
# <Title of the whole plan>

## 0. Global conventions (non-step — every agent reads this)
- Environment / how to run, absolute path anchors, systemic constants, etc.
- OUTPUT-PATH DECISION: <real-repo absolute + ⚠, or worktree-relative>.
- Read-only inputs: <paths> (⚠ read-only).
- Known over-claim red lines: <…>.

## Step 1 — <verb-phrase>
**Must-read:** <paths>
**Start condition:** none (entry) | Step N PASS
**Actions:** …
**Outputs:** <paths>   (⚠ outside-worktree write, if R4 says so)
**Review gate:** PASS iff … ; else REVISE …
**Done when:** …

## Step 2 — <…>   (BARRIER: needs all Step-1 fan-out)
…

## Step N — Meta-review + write the report   (BARRIER: needs all prior steps)
**Must-read:** every prior step's Outputs — figures/, results/notes, scripts/
**Start condition:** all prior steps PASS
**Actions:** (1) review the whole project for correctness/consistency/over-claims.
(2) In the **same pass** (context already loaded), write REPORT.md — (a) methods
& which code produced each result; (b) results, each tied to its supporting
output; (c) every figure (path + what it shows + takeaway); (d) limitations/
caveats, incl. anything the review flagged (R5). Summarize only; don't re-run analysis.
**Outputs:** review verdict + REPORT.md   (worktree root; review artifact per R4)
**Review gate:** PASS iff every claim traces to a listed output; nothing missing or over-claimed.
**Done when:** REPORT.md covers every prior step's Outputs.
```

## Before you finish — self-check
1. Would a dumb splitter cutting on `##` produce **exactly** your intended steps,
   and nothing else? (Title is `#`; sub-points are `###`; no stray `##`.)
2. Is **every** non-step thing inside `## 0.`? (No lone Appendix/Setup step.)
3. Did you **state the output-path/isolation decision** in §0 and mark every
   outside-worktree write with `⚠`?
4. Any claim stronger than the data? Add its uncertainty/limit.
5. Does each step list its own Must-read, Outputs, and a Review gate?
6. Does the **final review/meta-review step** also write `REPORT.md` in the same
   pass (methods/code + results + every figure; no over-claims) — rather than a
   separate cold report step that re-reads everything?

Deliver the outline as a `.md` file (or inline if asked). Don't add prose outside
the outline structure.
