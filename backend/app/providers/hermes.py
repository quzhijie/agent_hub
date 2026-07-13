"""Hermes provider rules. Interface not yet sampled — relies on generic rules
plus a couple of safe guesses; falls back to `unknown` when unsure."""
from __future__ import annotations

import re

from .base import Provider


class HermesProvider(Provider):
    name = "hermes"
    default_binary = "hermes"

    waiting_patterns = [
        re.compile(r"\byour (?:input|response|answer)\b", re.I),
    ]
    generating_patterns = [
        re.compile(r"esc to interrupt", re.I),
    ]
    idle_patterns = []
