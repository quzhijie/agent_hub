"""Local-only auth: loopback Host, matching Origin, and a shared token.

Together these block DNS-rebinding from a malicious web page (which would carry
a non-loopback Host/Origin and cannot read our token due to the same-origin
policy) while keeping the localhost single-user flow frictionless.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

from .config import Settings

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _is_loopback_name(host: str | None) -> bool:
    # RFC 6761: browsers (Chrome/Firefox) resolve *.localhost to loopback
    # themselves, so a memorable name like agent-hub.localhost is still
    # guaranteed-local and safe to accept.
    return host is not None and (host in _LOOPBACK_HOSTS or host.endswith(".localhost"))


def _host_only(value: str) -> str:
    # strip any :port ; handle bracketed IPv6 like [::1]:8787
    v = value.strip()
    if v.startswith("["):
        return v[: v.index("]") + 1]
    if ":" in v:
        return v.rsplit(":", 1)[0]
    return v


def _is_loopback_host(header_value: str | None) -> bool:
    if not header_value:
        return False
    return _is_loopback_name(_host_only(header_value))


def _origin_is_loopback(origin: str | None) -> bool:
    if not origin:
        return True  # non-browser clients (curl, tests) may omit Origin
    return _is_loopback_name(urlsplit(origin).hostname)


def make_guard(settings: Settings):
    """Build a FastAPI dependency enforcing the local-only policy."""

    async def guard(
        request: Request,
        x_auth_token: str | None = Header(default=None),
    ) -> None:
        if not _is_loopback_host(request.headers.get("host")):
            raise HTTPException(status_code=403, detail="non-loopback host rejected")
        if not _origin_is_loopback(request.headers.get("origin")):
            raise HTTPException(status_code=403, detail="cross-origin request rejected")
        token = x_auth_token or request.query_params.get("token")
        if token != settings.token:
            raise HTTPException(status_code=401, detail="bad or missing token")

    return guard
