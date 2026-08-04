#!/usr/bin/env python3
"""Loopback-only macOS helper that brings the SSH client's iTerm2 forward.

Agent Hub itself runs on the SSH server, so its AppleScript cannot manipulate
applications on the Mac that owns the browser and SSH window.  This tiny HTTP
service runs on that client Mac instead.  It accepts exactly one action from a
loopback Agent Hub page: activate iTerm2 through LaunchServices.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

HOST = "127.0.0.1"
DEFAULT_PORT = 18788
ITERM2_BUNDLE_ID = "com.googlecode.iterm2"
MAX_BODY_BYTES = 1024

log = logging.getLogger("agent_hub.client_focus")


def _origin_allowed(origin: Optional[str]) -> bool:
    """Only browser pages guaranteed to resolve to this same Mac may call us."""
    if not origin:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and (
        host in {"127.0.0.1", "::1", "localhost", "agent-hub.localhost"}
    )


def _host_allowed(host_header: Optional[str]) -> bool:
    if not host_header:
        return False
    value = host_header.strip().lower()
    if value.startswith("["):
        host = value[1:value.find("]")] if "]" in value else ""
    else:
        host = value.rsplit(":", 1)[0] if ":" in value else value
    return host in {"127.0.0.1", "::1", "localhost"}


def focus_iterm2() -> Tuple[bool, str]:
    """Activate iTerm2 without accepting any command or app name from HTTP."""
    try:
        result = subprocess.run(
            ["/usr/bin/open", "-b", ITERM2_BUNDLE_ID],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, ""
    detail = (result.stderr or result.stdout).strip()
    return False, detail or f"open exited {result.returncode}"


class FocusHandler(BaseHTTPRequestHandler):
    server_version = "AgentHubClientFocus/1"

    def _cors_origin(self) -> Optional[str]:
        origin = self.headers.get("Origin")
        return origin if _origin_allowed(origin) else None

    def _send_json(
        self, status: int, payload: Dict[str, object], origin: Optional[str] = None
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _request_is_local(self) -> bool:
        return self.client_address[0] in {"127.0.0.1", "::1"} and _host_allowed(
            self.headers.get("Host")
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/health" or not self._request_is_local():
            self._send_json(404, {"ok": False})
            return
        origin = self._cors_origin()
        self._send_json(200, {"ok": True, "app": "iTerm2"}, origin)

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        origin = self._cors_origin()
        if self.path != "/focus" or not self._request_is_local() or not origin:
            self._send_json(403, {"ok": False, "error": "origin rejected"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        origin = self._cors_origin()
        if self.path != "/focus" or not self._request_is_local() or not origin:
            self._send_json(403, {"ok": False, "error": "origin rejected"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = MAX_BODY_BYTES + 1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"ok": False, "error": "request too large"}, origin)
            return
        if length:
            self.rfile.read(length)  # payload is intentionally ignored

        ok, error = focus_iterm2()
        if ok:
            self._send_json(200, {"ok": True, "app": "iTerm2"}, origin)
        else:
            log.warning("could not activate iTerm2: %s", error)
            self._send_json(503, {"ok": False, "error": error}, origin)

    def log_message(self, fmt: str, *args: object) -> None:
        log.info("%s - %s", self.client_address[0], fmt % args)


class FocusServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Hub iTerm2 focus helper")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = FocusServer((HOST, args.port), FocusHandler)
    log.info("listening on http://%s:%d for iTerm2 focus requests", HOST, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
