#!/usr/bin/env bash
# Agent Hub launcher. Creates a dedicated venv on first run.
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"

export PYTHONPATH="$PWD/backend${PYTHONPATH:+:$PYTHONPATH}"

if [ ! -x "$PY" ]; then
  echo "==> creating venv"
  python3 -m venv "$VENV"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet fastapi "uvicorn[standard]" pydantic pytest httpx
fi

PLIST="$HOME/Library/LaunchAgents/com.agent-hub.plist"

case "${1:-run}" in
  run)
    exec "$PY" -m app.main
    ;;
  test)
    exec "$PY" -m pytest
    ;;
  install)
    # Register a per-user launchd service (auto-start on login, restart on crash).
    # Everything is derived from where this repo lives + your current $HOME/$PATH,
    # so it's portable — no hard-coded paths. Re-run after moving the repo.
    mkdir -p "$HOME/Library/LaunchAgents"
    # launchd starts with a bare PATH, but tmux seats need to find their agent
    # binaries (tmux, claude, codex, …). Bake in the PATH from the shell you're
    # installing from, plus the usual Homebrew / user-bin locations.
    SVC_PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/bin:$PATH"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.agent-hub</string>
  <key>ProgramArguments</key><array><string>$PWD/run.sh</string></array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$SVC_PATH</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PWD/data/hub.log</string>
  <key>StandardErrorPath</key><string>$PWD/data/hub.err.log</string>
</dict>
</plist>
PLIST
    launchctl bootout "gui/$(id -u)/com.agent-hub" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "==> installed + started: $PLIST"
    echo "    open the URL that data/hub.log prints, or run ./run.sh once to see it"
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/com.agent-hub" 2>/dev/null || true
    rm -f "$PLIST"
    echo "==> uninstalled (your repo, venv and data/ are untouched)"
    ;;
  link-skills)
    # Symlink the bundled Claude Code skill(s) into ~/.claude/skills/ so
    # /pipeline-outline works. Symlink (not copy) so repo edits propagate.
    # Refuses to clobber an existing real directory — only replaces its own link.
    dst="$HOME/.claude/skills"
    mkdir -p "$dst"
    for s in skills/*/; do
      name="$(basename "$s")"
      target="$dst/$name"
      if [ -L "$target" ]; then
        rm -f "$target"
      elif [ -e "$target" ]; then
        echo "==> skip $name: $target already exists (not a symlink) — remove it yourself to relink" >&2
        continue
      fi
      ln -s "$PWD/skills/$name" "$target"
      echo "==> linked $name -> $target"
    done
    echo "    restart Claude Code to pick up the skill(s) (e.g. /pipeline-outline)"
    ;;
  *)
    echo "usage: ./run.sh [run|test|install|uninstall|link-skills]" >&2
    exit 2
    ;;
esac
