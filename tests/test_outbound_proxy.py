"""Outbound-proxy launch behavior for launchd/tmux providers."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

from app.providers.base import _PROXY_LAUNCHER
from app.providers.registry import get_provider


def test_proxy_launcher_is_only_used_for_default_international_providers():
    for name in ("codex", "claude"):
        command = get_provider(name).resolve_command("")
        assert shlex.split(command)[0] == str(_PROXY_LAUNCHER)
        assert "HTTPS_PROXY=" not in command  # never store credentials in tmux metadata

    for name in ("ds4", "ds4-co", "hermes"):
        command = get_provider(name).resolve_command("")
        assert str(_PROXY_LAUNCHER) not in command


def test_user_launch_command_is_not_routed_through_proxy_launcher():
    assert get_provider("codex").resolve_command("my-codex --flag") == "my-codex --flag"
    assert get_provider("claude").resolve_command("my-claude --flag") == "my-claude --flag"


def test_proxy_launcher_sources_exports_and_expands_references(tmp_path):
    config_dir = tmp_path / ".config"
    config_dir.mkdir()
    (config_dir / "proxy.env").write_text(
        "export HTTPS_PROXY='http://proxy.test:8888'\n"
        'export HTTP_PROXY="$HTTPS_PROXY"\n'
        "export NO_PROXY='localhost,.ts.net'\n"
    )
    probe = (
        "import json, os; "
        "print(json.dumps({k: os.environ.get(k) for k in "
        "('HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY')}))"
    )
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    result = subprocess.run(
        [str(_PROXY_LAUNCHER), sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    values = json.loads(result.stdout)
    assert values == {
        "HTTPS_PROXY": "http://proxy.test:8888",
        "HTTP_PROXY": "http://proxy.test:8888",
        "NO_PROXY": "localhost,.ts.net",
    }
