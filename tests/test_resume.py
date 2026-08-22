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


def test_ds4_default_resume_appends_continue():
    # ds4 is Claude Code (DeepSeek backend) -> same --continue resume.
    cmd = get_provider("ds4").resolve_resume_command("")
    assert cmd.endswith("--continue")


def test_ds4co_default_resume_uses_resume_last():
    # ds4-co is Codex CLI (DeepSeek backend) -> same resume --last contract.
    cmd = get_provider("ds4-co").resolve_resume_command("")
    assert cmd.endswith("resume --last")


def test_user_launch_command_is_never_mutated():
    for name in ("claude", "codex", "ds4", "ds4-co", "hermes", "custom"):
        p = get_provider(name)
        assert p.resolve_resume_command("mytool --flag") == "mytool --flag"


def test_custom_without_command_still_raises():
    with pytest.raises(ValueError):
        get_provider("custom").resolve_resume_command("")


def test_initial_prompt_is_one_shell_quoted_argument():
    cmd = get_provider("codex").resolve_initial_command(
        "", "Read /tmp/context; $(touch /tmp/should-not-run)"
    )
    assert "'Read /tmp/context; $(touch /tmp/should-not-run)'" in cmd


def test_initial_prompt_refuses_custom_launch_semantics():
    with pytest.raises(ValueError, match="custom launch command"):
        get_provider("codex").resolve_initial_command("codex --flag", "prompt")


def test_resumed_native_conversation_can_receive_fresh_context():
    command = get_provider("codex").resolve_resume_with_prompt_command(
        "", "Latest brief; $(touch /tmp/should-not-run)",
    )
    assert "resume --last" in command
    assert "'Latest brief; $(touch /tmp/should-not-run)'" in command


def test_resume_with_context_refuses_unknown_command_contracts():
    with pytest.raises(ValueError, match="cannot resume"):
        get_provider("custom").resolve_resume_with_prompt_command(
            "mytool --resume", "Latest brief",
        )
