#!/usr/bin/env python3
"""Association-bound CLI for Project Core forward change proposals."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

from app.project_core import agent_change_proposal_request


_MAX_INPUT_BYTES = 6_000_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXECUTION_ID_RE = re.compile(r"^chgexec_[A-Za-z0-9]+$")


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_private_once(directory: Path, filename: str, content: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    directory_fd = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary = f".{filename}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(
                temporary, filename,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = os.open(
                filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                value = os.fstat(existing)
                if not stat.S_ISREG(value.st_mode) or value.st_mode & 0o077:
                    raise PermissionError("context refresh path is not a private regular file")
                if _read_descriptor(existing) != content:
                    raise RuntimeError("context refresh path already contains other content")
            finally:
                os.close(existing)
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _load_config(path: Path) -> tuple[Path, str, Path]:
    resolved = path.expanduser().resolve(strict=True)
    if resolved.stat().st_mode & 0o077:
        raise PermissionError("change proposal config must not be group/world readable")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("unsupported change proposal config")
    runtime_file = value.get("runtime_file")
    association_id = value.get("association_id")
    if (
        not isinstance(runtime_file, str) or not runtime_file
        or not isinstance(association_id, str) or not association_id
    ):
        raise ValueError("change proposal config is incomplete")
    return Path(runtime_file), association_id, resolved


def _persist_context_refresh(
    result: dict, *, config_path: Path, association_id: str,
) -> None:
    refresh = result.get("context_refresh")
    if refresh is None:
        return
    if (
        not isinstance(refresh, dict)
        or set(refresh) != {"id", "sha256", "content"}
        or not isinstance(refresh.get("id"), str)
        or not refresh["id"].startswith("ctx_")
        or not isinstance(refresh.get("sha256"), str)
        or not _SHA256_RE.fullmatch(refresh["sha256"])
        or not isinstance(refresh.get("content"), dict)
    ):
        raise RuntimeError("Project Core context refresh envelope is invalid")
    execution_id = result.get("execution_id")
    if not isinstance(execution_id, str) or not _EXECUTION_ID_RE.fullmatch(execution_id):
        raise RuntimeError("Project Core execution ID is invalid")
    canonical = json.dumps(
        refresh["content"], ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    if hashlib.sha256(canonical.encode()).hexdigest() != refresh.get("sha256"):
        raise RuntimeError("Project Core context refresh hash does not match")
    directory = config_path.parent.parent / "project_core_handoffs"
    filename = f"execution_{execution_id}_{refresh['sha256'][:24]}.json"
    target = directory / filename
    envelope = {
        "schema": "project-core.agent-change-execution-context/v1",
        "association_id": association_id,
        "execution_id": result["execution_id"],
        "context_pack": refresh,
    }
    _write_private_once(
        directory,
        filename,
        (json.dumps(envelope, ensure_ascii=False, indent=0, sort_keys=True) + "\n").encode(),
    )
    result["context_refresh"] = {
        "id": refresh["id"], "sha256": refresh["sha256"],
        "path": str(target),
    }


def _read_payload(action: str) -> dict:
    raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValueError("change proposal input is too large")
    if not raw.strip():
        value = {}
    else:
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("change proposal input must be a JSON object")
    if action == "submit":
        return {"proposal": value}
    return value


def main() -> int:
    parser = argparse.ArgumentParser(prog="manage_change_proposal")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--action",
        required=True,
        choices=("schema", "submit", "revise", "get", "list", "execute"),
    )
    args = parser.parse_args()
    try:
        runtime_file, association_id, config_path = _load_config(args.config)
        result = agent_change_proposal_request(
            args.action, _read_payload(args.action),
            association_id=association_id, runtime_file=runtime_file,
        )
        if args.action == "execute":
            _persist_context_refresh(
                result, config_path=config_path, association_id=association_id,
            )
    except Exception as exc:  # noqa: BLE001 - narrow CLI boundary
        print(f"manage_change_proposal failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
