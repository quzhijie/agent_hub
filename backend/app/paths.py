"""Path validation for project roots and session working dirs."""
from __future__ import annotations

from pathlib import Path


def validate_dir(path_str: str) -> str:
    if path_str is None or not path_str.strip():
        raise ValueError("path is empty")
    p = Path(path_str.strip()).expanduser()
    if not p.is_absolute():
        raise ValueError(f"path must be absolute: {path_str!r}")
    if not p.exists():
        raise ValueError(f"path does not exist: {path_str!r}")
    if not p.is_dir():
        raise ValueError(f"path is not a directory: {path_str!r}")
    return str(p)
