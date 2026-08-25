from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit


def test_project_url_carries_auth_and_project_fragment(settings):
    from app import dashboard

    parsed = urlsplit(dashboard.project_url(settings, "project with spaces"))

    assert parse_qs(parsed.query) == {"token": ["testtoken"]}
    assert parse_qs(parsed.fragment) == {"project": ["project with spaces"]}


def test_focus_project_navigates_through_existing_dashboard_helper(
    settings, monkeypatch,
):
    from app import dashboard

    seen = {}

    def fake_run(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout="focused\n")

    monkeypatch.setattr(dashboard, "_OSASCRIPT", "/usr/bin/osascript")
    monkeypatch.setattr(dashboard.subprocess, "run", fake_run)

    assert dashboard.focus_project(settings, "project-id") == {
        "handled": True,
        "status": "focused",
    }
    assert seen["args"][0] == "/usr/bin/osascript"
    assert "#project=project-id" in seen["args"][2]
    assert seen["args"][3] == "8787"
    assert seen["args"][4] == "1"
    assert seen["kwargs"]["timeout"] == 3


def test_focus_project_can_refuse_to_open_a_new_dashboard(settings, monkeypatch):
    from app import dashboard

    seen = {}

    def fake_run(args, **_kwargs):
        seen["args"] = args
        return SimpleNamespace(returncode=0, stdout="not-found\n")

    monkeypatch.setattr(dashboard, "_OSASCRIPT", "/usr/bin/osascript")
    monkeypatch.setattr(dashboard.subprocess, "run", fake_run)

    assert dashboard.focus_project(
        settings, "project-id", open_if_missing=False
    ) == {"handled": False, "status": "no-dashboard-found"}
    assert seen["args"][4] == "0"


def test_focus_script_navigates_a_matching_chrome_tab():
    from app import dashboard

    script = dashboard._FOCUS_SCRIPT.read_text(encoding="utf-8")

    assert "set URL of t to target" in script
    assert 'return "not-found"' in script
    assert 'return "focused"' in script
