import pytest

from app import tmux


def test_make_session_name_is_readable():
    name = tmux.make_session_name("My Proj", "executor", "0987654321fedcba")
    assert name == "hub-My-Proj-executor-0987"
    tmux.validate_name(name)  # must not raise


def test_make_session_name_longer_id_on_demand():
    name = tmux.make_session_name("p", "s", "0987654321fedcba", id_len=8)
    assert name == "hub-p-s-09876543"


def test_make_session_name_cjk_falls_back_to_id():
    name = tmux.make_session_name("项目", "执行者", "0987654321fedcba")
    assert name == "hub-0987"
    tmux.validate_name(name)


def test_slug_strips_unsafe_and_truncates():
    assert tmux.slug("My Proj!") == "My-Proj"
    assert tmux.slug("  a b/c  ") == "a-b-c"
    assert tmux.slug("x" * 40) == "x" * 12
    assert tmux.slug("！？") == ""


@pytest.mark.parametrize("bad", ["has.dot", "has:colon", "has space", "", "tab\t", "quote'"])
def test_validate_name_rejects_unsafe(bad):
    with pytest.raises(tmux.TmuxError):
        tmux.validate_name(bad)


@pytest.mark.parametrize("good", ["agent-hub-1", "abc_DEF-09"])
def test_validate_name_accepts_safe(good):
    assert tmux.validate_name(good) == good


def test_attach_command_reflects_configured_socket():
    from app.config import TMUX_SOCKET
    cmd = tmux.attach_command("agent-hub-x-y")
    if TMUX_SOCKET:
        assert f"-L {TMUX_SOCKET} " in cmd
    else:
        assert " -L " not in cmd
    assert "=agent-hub-x-y" in cmd


def test_attach_command_quotes_target_for_zsh():
    # A bare leading '=' triggers zsh EQUALS expansion; the target must be quoted
    # so a pasted attach command doesn't fail with "... not found".
    cmd = tmux.attach_command("agent-hub-x-y")
    assert "'=agent-hub-x-y'" in cmd
    assert "-t =agent-hub-x-y" not in cmd
