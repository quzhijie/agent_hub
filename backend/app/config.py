"""Runtime settings. Enforces loopback-only binding; generates a local token."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

# Repo root, derived from this file's location (…/agent_hub/backend/app/config.py)
# so the app is portable — it runs from wherever it's cloned, no absolute paths.
BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "web"

# Which tmux server the workbench uses. Empty = tmux's DEFAULT socket — the same
# one your normal `tmux` and handmux use — so seats show up in handmux on your
# phone. kill/switch stay safe: the backend only ever kills sessions it
# registered (all named `hub-*`). Set AGENT_HUB_TMUX_SOCKET to a name
# (passed as `tmux -L <name>`) for a dedicated, isolated server instead; the
# test suite does exactly that so it never touches your real tmux.
TMUX_SOCKET = os.environ.get("AGENT_HUB_TMUX_SOCKET", "")

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    data_dir: Path = DATA_DIR
    web_dir: Path = WEB_DIR
    db_path: Path = field(default_factory=lambda: DATA_DIR / "agent_hub.db")
    token: str = ""
    sample_interval: float = 3.0      # seconds between status samples
    capture_lines: int = 60           # lines of pane tail to analyse
    tmux_socket: str = TMUX_SOCKET
    enable_sampler: bool = True       # background status loop (off in tests)
    enable_notify: bool = True        # macOS notification when a seat starts waiting
    enable_orchestrator: bool = True  # drive pipelines each sample cycle (off in tests)


def _load_or_create_token(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    tf = data_dir / "token"
    if tf.exists():
        existing = tf.read_text().strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(24)
    tf.write_text(token)
    try:
        os.chmod(tf, 0o600)
    except OSError:
        pass
    return token


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("AGENT_HUB_DATA", str(DATA_DIR)))
    host = os.environ.get("AGENT_HUB_HOST", "127.0.0.1")
    if host not in _LOOPBACK:
        raise SystemExit(f"refusing to bind non-loopback host: {host!r}")
    port = int(os.environ.get("AGENT_HUB_PORT", "8787"))
    s = Settings(
        host=host,
        port=port,
        data_dir=data_dir,
        db_path=data_dir / "agent_hub.db",
        enable_notify=os.environ.get("AGENT_HUB_NOTIFY", "1") not in ("0", "false", "no"),
    )
    s.token = _load_or_create_token(data_dir)
    return s
