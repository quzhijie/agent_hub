"""Resume-on-restart command selection per provider."""
import json
import sqlite3

import pytest

from app.providers.registry import get_provider


def test_claude_default_resume_appends_continue():
    cmd = get_provider("claude").resolve_resume_command("")
    assert cmd.endswith("--continue")
    assert "claude" in cmd


def test_claude_initial_and_resume_commands_pin_exact_session():
    provider = get_provider("claude")
    native_id = "f220b703-6a77-4161-adc6-6046d09dfbd2"

    initial = provider.resolve_initial_command(
        "", "Initial context", native_session_id=native_id,
    )
    resumed = provider.resolve_resume_with_prompt_command(
        "", "Latest context", native_session_id=native_id,
    )

    assert f"--session-id {native_id}" in initial
    assert f"--resume {native_id}" in resumed
    assert "--continue" not in resumed
    assert resumed.endswith("'Latest context'")


def test_codex_default_resume_uses_resume_last():
    cmd = get_provider("codex").resolve_resume_command("")
    assert cmd.endswith("resume --last")


def test_native_model_selection_survives_initial_and_resume_commands():
    codex = get_provider("codex")
    initial = codex.resolve_initial_command("", "context", model="gpt-5.6-sol")
    resumed = codex.resolve_resume_command("", model="gpt-5.6-sol")
    assert "--model gpt-5.6-sol" in initial
    assert "--model gpt-5.6-sol" in resumed
    assert resumed.endswith("resume --last")

    claude = get_provider("claude").resolve_initial_command(
        "", "context", model="opus",
    )
    assert "--model opus" in claude


def test_model_selection_rejects_shell_syntax_and_custom_commands():
    provider = get_provider("codex")
    with pytest.raises(ValueError, match="invalid model"):
        provider.resolve_command("", model="gpt; touch /tmp/no")
    with pytest.raises(ValueError, match="custom launch command"):
        provider.resolve_command("codex --flag", model="gpt-5.6-sol")


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
    native_id = "01a02cfd-7f18-77e0-aab9-3e70717a882c"
    command = get_provider("codex").resolve_resume_with_prompt_command(
        "", "Latest brief; $(touch /tmp/should-not-run)",
        native_session_id=native_id,
    )
    assert f"resume {native_id}" in command
    assert "resume --last" not in command
    assert "'Latest brief; $(touch /tmp/should-not-run)'" in command


def test_codex_context_resume_refuses_ambiguous_resume_last():
    with pytest.raises(ValueError, match="exact Codex conversation"):
        get_provider("codex").resolve_resume_with_prompt_command("", "Latest brief")


def test_codex_finds_native_thread_by_cwd_and_first_start(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    state = sqlite3.connect(codex_home / "state_5.sqlite")
    state.execute("CREATE TABLE threads (id TEXT, cwd TEXT, created_at INTEGER)")
    expected = "01a02cfd-7f18-77e0-aab9-3e70717a882c"
    state.executemany(
        "INSERT INTO threads VALUES (?,?,?)",
        [
            ("01a02ce7-72de-7b92-8dea-b51282aacd6f", str(tmp_path), 1787461100),
            # Native thread creation can lag while a trust/login screen waits.
            (expected, str(tmp_path), 1787461314),
            ("01a02d01-98d4-7081-8c89-49f107a98885", str(tmp_path / "other"), 1787461205),
        ],
    )
    state.commit()
    state.close()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    found = get_provider("codex").find_native_session_id(
        str(tmp_path), "2026-08-23T13:00:05+08:00",
    )

    assert found == expected


def test_claude_finds_native_session_by_cwd_and_first_start(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    working_dir = tmp_path / "work"
    working_dir.mkdir()
    project_dir = claude_home / "projects" / str(working_dir).replace("/", "-")
    project_dir.mkdir(parents=True)
    expected = "f220b703-6a77-4161-adc6-6046d09dfbd2"
    events = [
        {"type": "mode", "sessionId": expected},
        {
            "type": "user", "sessionId": expected, "cwd": str(working_dir),
            "timestamp": "2026-08-23T05:28:43.820Z",
        },
    ]
    (project_dir / f"{expected}.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))

    found = get_provider("claude").find_native_session_id(
        str(working_dir), "2026-08-23T13:28:42+08:00",
    )

    assert found == expected


def test_resume_with_context_refuses_unknown_command_contracts():
    with pytest.raises(ValueError, match="cannot resume"):
        get_provider("custom").resolve_resume_with_prompt_command(
            "mytool --resume", "Latest brief",
        )
