"""Provider lookup."""
from __future__ import annotations

from .base import Provider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .custom import CustomProvider
from .hermes import HermesProvider

_PROVIDERS: dict[str, Provider] = {
    "hermes": HermesProvider(),
    "claude": ClaudeProvider(),
    "codex": CodexProvider(),
    "custom": CustomProvider(),
}

PROVIDER_NAMES = tuple(_PROVIDERS.keys())


def get_provider(name: str) -> Provider:
    p = _PROVIDERS.get(name)
    if p is None:
        raise ValueError(f"unknown provider: {name!r}")
    return p


def is_valid_provider(name: str) -> bool:
    return name in _PROVIDERS
