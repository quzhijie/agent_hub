"""Resume-on-restart command selection per provider."""
import pytest

from app.providers.registry import get_provider


def test_claude_default_resume_appends_continue():
    cmd = get_provider("claude").resolve_resume_command("")
    assert cmd.endswith("--continue")
    assert "claude" in cmd


def test_codex_default_resume_uses_resume_last():
    cmd = get_provider("codex").resolve_resume_command("")
    assert cmd.endswith("resume --last")


def test_hermes_resume_falls_back_to_fresh_launch():
    p = get_provider("hermes")
    assert p.resolve_resume_command("") == p.resolve_command("")


def test_user_launch_command_is_never_mutated():
    for name in ("claude", "codex", "hermes", "custom"):
        p = get_provider(name)
        assert p.resolve_resume_command("mytool --flag") == "mytool --flag"


def test_custom_without_command_still_raises():
    with pytest.raises(ValueError):
        get_provider("custom").resolve_resume_command("")
