# Bundled Claude Code skills

Agent Hub's **pipeline** feature is fully usable without anything in this folder:
you can always hand-write an `OUTLINE.md` and paste it in. This folder just makes
the *authoring* step nicer.

## `pipeline-outline`

A [Claude Code skill](https://docs.claude.com/en/docs/claude-code) that writes an
`OUTLINE.md` shaped for Agent Hub's pipeline splitter — one executable step per
`##` heading, non-step context folded into a marked `## 0.` section, the
worktree / output-path decision made explicit, no over-claims. The dashboard's
"create pipeline" box parses exactly this shape, so an outline written by the
skill splits cleanly into steps with no post-hoc reframing.

The repo references it in `README.md`, `USAGE.md`, and the web UI (`/pipeline-outline`),
so a copy lives here to keep the clone self-contained. **This is a snapshot/backup**
— the live skill is whatever sits in your `~/.claude/skills/`. If you edit one,
copy it over the other so they don't drift.

### Install (so `/pipeline-outline` works in Claude Code)

Claude Code discovers skills under `~/.claude/skills/<name>/SKILL.md`. Either
symlink (repo edits propagate) or copy:

```sh
# from the repo root — convenience wrapper, refuses to clobber an existing dir:
./run.sh link-skills

# …or do it by hand:
ln -s "$PWD/skills/pipeline-outline" ~/.claude/skills/pipeline-outline   # symlink
cp -R skills/pipeline-outline        ~/.claude/skills/pipeline-outline   # or copy
```

Restart / reopen Claude Code, then `/pipeline-outline` is available. Ask it to
turn a task into an outline, review the steps, paste into the dashboard's
pipeline box.

### Use without installing

Skip all of the above and just write `OUTLINE.md` yourself following the rules in
[`pipeline-outline/SKILL.md`](pipeline-outline/SKILL.md): exactly one `#` title,
every executable step a `##`, non-step material under
`## 0. … (non-step — every agent reads this)`, output-path/isolation decision
stated in §0 and per step. The splitter only cares about the heading shape, not
about whether a skill produced it.
