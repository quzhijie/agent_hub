"""Custom provider: user supplies the full launch command. Generic rules only."""
from __future__ import annotations

from .base import Provider


class CustomProvider(Provider):
    name = "custom"
    default_binary = None
