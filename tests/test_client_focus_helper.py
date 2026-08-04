import http.client
import importlib.util
import threading
from pathlib import Path


_HELPER = Path(__file__).parents[1] / "client-focus" / "helper.py"
_SPEC = importlib.util.spec_from_file_location("agent_hub_client_focus", _HELPER)
helper = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(helper)


def test_origin_and_host_guards():
    assert helper._origin_allowed("http://agent-hub.localhost:18787")
    assert helper._origin_allowed("http://127.0.0.1:8787")
    assert not helper._origin_allowed("https://example.com")
    assert not helper._origin_allowed(None)
    assert helper._host_allowed("127.0.0.1:18788")
    assert helper._host_allowed("localhost:18788")
    assert not helper._host_allowed("example.com:18788")


def test_focus_uses_only_iterm2_bundle_id(monkeypatch):
    seen = {}

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return Result()

    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    ok, error = helper.focus_iterm2()

    assert ok is True and error == ""
    assert seen["argv"] == ["/usr/bin/open", "-b", "com.googlecode.iterm2"]
    assert seen["kwargs"]["timeout"] == 5


def test_http_focus_and_cors(monkeypatch):
    calls = []
    monkeypatch.setattr(helper, "focus_iterm2", lambda: calls.append(True) or (True, ""))
    server = helper.FocusServer(("127.0.0.1", 0), helper.FocusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        conn.request(
            "POST",
            "/focus",
            body="focus",
            headers={"Origin": "http://agent-hub.localhost:18787", "Content-Type": "text/plain"},
        )
        response = conn.getresponse()
        assert response.status == 200
        assert response.getheader("Access-Control-Allow-Origin") == "http://agent-hub.localhost:18787"
        assert response.read() == b'{"ok":true,"app":"iTerm2"}'
        assert calls == [True]

        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        conn.request("POST", "/focus", body="focus", headers={"Origin": "https://example.com"})
        response = conn.getresponse()
        assert response.status == 403
        response.read()
        assert calls == [True]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
