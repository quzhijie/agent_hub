"""Read the human-visible conversation from provider-native session records.

Agent Hub keeps the stable seat-to-provider-session binding in its own database,
while Codex and Claude remain authoritative for the actual conversation.  This
module projects only user/assistant text: system instructions, reasoning, tool
arguments, and tool results stay out of the browser transcript.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

DEFAULT_LIMIT = 40
MAX_LIMIT = 100
MAX_MESSAGE_CHARS = 300_000
MAX_PAGE_MESSAGE_BYTES = 2_400_000
MAX_TRANSCRIPT_BYTES = 128 * 1024 * 1024
MAX_RECOVERY_FILES = 5_000
RECOVERY_SCAN_LINES = 128
MAX_SEARCH_QUERY_CHARS = 200
MAX_SEARCH_EXCERPT_CHARS = 420
_MACHINE_USER_PREFIXES = (
    "Continue Project Core workstream ",
    "[PROJECT_CORE_AGENT_STARTUP_V1]",
    "[PROJECT_CORE_AGENT_BOOTSTRAP_V2]",
    "[PROJECT_CORE_CONTEXT_BOOTSTRAP_V1]",
    "[AGENT_HUB_RESTORE_CONTEXT_ONLY_V1]",
    "[Agent Hub Project Core protocol]",
    "[PROJECT_CORE_RUNTIME_BINDINGS_V1]",
    "[PROJECT_CORE_REPORT_CONTRACT_V1]",
    "[PROJECT_CORE_REPORT_CONTRACT_V2]",
    "[PROJECT_CORE_REPORT_CONTRACT_V3]",
)
_INTERNAL_USER_BLOCK_RE = re.compile(
    r"<(environment_context|system-reminder)>.*?</\1>", re.DOTALL,
)
_UUID_IN_FILENAME_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
)
_SEAT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ASSOCIATION_ID_RE = re.compile(r"^asoc_[0-9a-f]{32}$")
_ASSOCIATION_HANDOFF_RE = re.compile(
    r"project_core_handoffs/(asoc_[0-9a-f]{32})\.json"
    r"(?![0-9A-Za-z_.-])",
)
_SEAT_REPORT_RE = re.compile(
    r"project_core_reports/([0-9a-f]{32})\.json"
    r"(?![0-9A-Za-z_.-])",
)


def read_session_transcript(
    session: dict[str, Any], *, before: int | None = None, limit: int = DEFAULT_LIMIT,
    after: int = 0, through: int | None = None,
) -> dict[str, Any]:
    """Return one newest-first page boundary, rendered oldest-to-newest.

    ``before`` is an exclusive, stable message sequence.  The response itself
    is chronological so it can be appended to a normal chat timeline.
    """
    if isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"transcript limit must be between 1 and {MAX_LIMIT}")
    if before is not None and (isinstance(before, bool) or before < 1):
        raise ValueError("transcript cursor must be a positive integer")
    if isinstance(after, bool) or after < 0:
        raise ValueError("transcript association start must be non-negative")
    if through is not None and (
        isinstance(through, bool) or through < after
    ):
        raise ValueError("transcript association end is invalid")

    provider = str(session.get("provider") or "")
    messages, source, _cursor = _visible_messages(session)
    if source == "unsupported_provider":
        return _unavailable(session, "unsupported_provider")
    if messages is None:
        return _unavailable(session, source)

    conversation_total = len(messages)
    segment = [
        item for item in messages
        if item["seq"] > after and (through is None or item["seq"] <= through)
    ]
    eligible = [
        item for item in segment if before is None or item["seq"] < before
    ]
    page = _bounded_page(eligible, limit=limit)
    next_before = (
        page[0]["seq"] if page and len(eligible) > len(page) else None
    )
    return {
        "schema_version": 1,
        "status": "available",
        "session_id": str(session.get("id") or ""),
        "provider": provider,
        "provider_session_id": str(session.get("provider_session_id") or ""),
        "source": source,
        "roles": ["user", "assistant"],
        "internal_content_omitted": True,
        "total_messages": len(segment),
        "conversation_total_messages": conversation_total,
        "returned": len(page),
        "cursor": before,
        "next_before": next_before,
        "has_earlier": next_before is not None,
        "messages": page,
    }


def search_session_transcript(
    session: dict[str, Any], query: str, *, after: int = 0,
    through: int | None = None,
) -> dict[str, Any]:
    """Search one exact association segment of human-visible dialogue.

    Matching mirrors Project Core history search: whitespace-separated terms
    are case-insensitive and every term must occur somewhere in the bounded
    conversation. When one user message contains the complete query, its
    newest match is preferred so a repeated assistant answer does not hide the
    owner's original question. Otherwise the newest partially matching message
    supplies the short excerpt; the full provider transcript remains owned by
    the provider.
    """
    if not isinstance(query, str):
        raise ValueError("transcript search query must be a string")
    query = " ".join(query.split())
    if not query or len(query) > MAX_SEARCH_QUERY_CHARS or "\x00" in query:
        raise ValueError("transcript search query is invalid")
    if isinstance(after, bool) or after < 0:
        raise ValueError("transcript association start must be non-negative")
    if through is not None and (
        isinstance(through, bool) or through < after
    ):
        raise ValueError("transcript association end is invalid")

    provider = str(session.get("provider") or "")
    messages, source, _cursor = _visible_messages(session)
    if messages is None:
        return {
            "schema_version": 1, "status": "unavailable", "reason": source,
            "session_id": str(session.get("id") or ""), "provider": provider,
            "provider_session_id": str(session.get("provider_session_id") or ""),
            "internal_content_omitted": True, "searched_messages": 0,
            "matched": False, "matching_message_count": 0, "match": None,
        }

    segment = [
        item for item in messages
        if item["seq"] > after and (through is None or item["seq"] <= through)
    ]
    terms = tuple(part.casefold() for part in query.split())
    folded_messages = [item["content"].casefold() for item in segment]
    matched = all(any(term in text for text in folded_messages) for term in terms)
    matching_messages = [
        item for item, text in zip(segment, folded_messages)
        if any(term in text for term in terms)
    ] if matched else []
    complete_user_matches = [
        item for item, text in zip(segment, folded_messages)
        if item["role"] == "user" and all(term in text for term in terms)
    ] if matched else []
    selected = (
        complete_user_matches[-1] if complete_user_matches
        else matching_messages[-1] if matching_messages else None
    )
    match = None
    if selected is not None:
        match = {
            "message_seq": selected["seq"],
            "role": selected["role"],
            "occurred_at": selected["created_at"],
            "excerpt": _search_excerpt(selected["content"], terms),
        }
    return {
        "schema_version": 1, "status": "available",
        "session_id": str(session.get("id") or ""), "provider": provider,
        "provider_session_id": str(session.get("provider_session_id") or ""),
        "source": source, "internal_content_omitted": True,
        "searched_messages": len(segment), "matched": matched,
        "matching_message_count": len(matching_messages), "match": match,
    }


def session_transcript_cursor(session: dict[str, Any]) -> int | None:
    """Return the append-only provider event boundary, or ``None`` if unreadable.

    This deliberately counts raw JSONL lines rather than visible messages. A
    later parser rule may hide or reveal a provider envelope without moving a
    historical Project Core association boundary.
    """
    messages, _source, cursor = _visible_messages(session)
    return cursor if messages is not None else None


def _visible_messages(
    session: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, str, int]:
    provider = str(session.get("provider") or "")
    if provider in {"codex", "ds4-co"}:
        return _codex_messages(session)
    if provider in {"claude", "ds4"}:
        return _claude_messages(session)
    return None, "unsupported_provider", 0


def _search_excerpt(content: str, terms: tuple[str, ...]) -> str:
    text = " ".join(content.split())
    folded = text.casefold()
    positions = [folded.find(term) for term in terms if term in folded]
    position = min(positions) if positions else 0
    start = max(0, position - 120)
    end = min(len(text), start + MAX_SEARCH_EXCERPT_CHARS - 2)
    snippet = text[start:end]
    if start:
        snippet = "…" + snippet
    if end < len(text):
        snippet += "…"
    return snippet[:MAX_SEARCH_EXCERPT_CHARS]


def recover_project_core_session(
    session_id: str, association_id: str,
) -> dict[str, Any] | None:
    """Recover a purged legacy seat from its exact injected Core identities.

    Older Agent Hub purge deleted the seat row before transcript bindings
    existed. The provider transcript remains authoritative and its initial
    machine bootstrap contains both the globally unique Project Core
    association and the Agent Hub seat ID. Admit only one exact match; an
    absent or ambiguous match stays unavailable.
    """
    return recover_project_core_sessions(
        [(session_id, association_id)]
    ).get((session_id, association_id))


def recover_project_core_sessions(
    identities: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Recover exact purged seats with one bounded provider-history scan."""
    targets = {
        (session_id, association_id)
        for session_id, association_id in identities
        if (
            _SEAT_ID_RE.fullmatch(session_id) is not None
            and _ASSOCIATION_ID_RE.fullmatch(association_id) is not None
        )
    }
    if not targets:
        return {}
    candidates: dict[tuple[str, str], list[dict[str, Any]]] = {
        identity: [] for identity in targets
    }
    roots = (
        ("codex", _provider_home({"provider": "codex"}, "codex") / "sessions"),
        ("ds4-co", _provider_home({"provider": "ds4-co"}, "codex") / "sessions"),
        ("claude", _provider_home({"provider": "claude"}, "claude") / "projects"),
        ("ds4", _provider_home({"provider": "ds4"}, "claude") / "projects"),
    )
    seen_files = 0
    for provider, root in roots:
        try:
            paths = root.rglob("*.jsonl")
            for path in paths:
                seen_files += 1
                if seen_files > MAX_RECOVERY_FILES:
                    return {}
                found = _project_core_recovery_candidates(
                    path, root=root, provider=provider,
                    targets=targets,
                )
                for identity, candidate in found.items():
                    candidates[identity].append(candidate)
        except OSError:
            continue
    return {
        identity: values[0]
        for identity, values in candidates.items()
        if len(values) == 1
    }


def _project_core_recovery_candidate(
    path: Path,
    *,
    root: Path,
    provider: str,
    session_id: str,
    association_id: str,
) -> dict[str, Any] | None:
    return _project_core_recovery_candidates(
        path, root=root, provider=provider,
        targets={(session_id, association_id)},
    ).get((session_id, association_id))


def _project_core_recovery_candidates(
    path: Path,
    *,
    root: Path,
    provider: str,
    targets: set[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not _readable_transcript(path, root=root):
        return {}
    matches = _UUID_IN_FILENAME_RE.findall(path.name)
    native_id = _valid_native_id(matches[-1] if matches else "")
    if not native_id:
        return {}
    try:
        verified_path = path.expanduser().resolve(strict=True)
    except OSError:
        return {}
    cwd = ""
    started_at = ""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        for _line_number, row in _indexed_json_lines(
            path, maximum=RECOVERY_SCAN_LINES,
        ):
            if row is None:
                continue
            payload = row.get("payload")
            if isinstance(payload, dict) and row.get("type") == "session_meta":
                cwd = str(payload.get("cwd") or cwd)
            cwd = str(row.get("cwd") or cwd)
            started_at = str(row.get("timestamp") or started_at)
            text = _recovery_user_text(row, provider=provider)
            stripped = text.lstrip()
            if not stripped.startswith(_MACHINE_USER_PREFIXES):
                continue
            association_ids = set(_ASSOCIATION_HANDOFF_RE.findall(stripped))
            session_ids = set(_SEAT_REPORT_RE.findall(stripped))
            if len(association_ids) != 1 or len(session_ids) != 1:
                continue
            identity = (next(iter(session_ids)), next(iter(association_ids)))
            if identity in targets and identity not in found:
                found[identity] = {
                    "id": identity[0],
                    "provider": provider,
                    "provider_session_id": native_id,
                    "working_dir": cwd,
                    "started_at": started_at,
                    "_verified_transcript_path": str(verified_path),
                }
    except OSError:
        return {}
    return found


def _recovery_user_text(row: dict[str, Any], *, provider: str) -> str:
    if provider in {"codex", "ds4-co"}:
        payload = row.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("type") != "message"
            or payload.get("role") != "user"
        ):
            return ""
        return _content_text(payload.get("content"), allowed={"input_text"})
    if row.get("type") != "user":
        return ""
    payload = row.get("message")
    if not isinstance(payload, dict) or payload.get("role") != "user":
        return ""
    return _claude_content_text(payload.get("content"))


def _unavailable(session: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "unavailable",
        "reason": reason,
        "session_id": str(session.get("id") or ""),
        "provider": str(session.get("provider") or ""),
        "provider_session_id": str(session.get("provider_session_id") or ""),
        "roles": ["user", "assistant"],
        "internal_content_omitted": True,
        "total_messages": 0,
        "returned": 0,
        "cursor": None,
        "next_before": None,
        "has_earlier": False,
        "messages": [],
    }


def _provider_home(session: dict[str, Any], family: str) -> Path:
    provider = str(session.get("provider") or "")
    if family == "codex":
        if provider == "ds4-co":
            return Path.home() / ".codex-ds4"
        return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    if provider == "ds4":
        return Path.home() / ".claude-ds4"
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def _valid_native_id(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except ValueError:
        return ""


def _codex_state_db(home: Path) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    for path in home.glob("state_*.sqlite"):
        suffix = path.stem.removeprefix("state_")
        if suffix.isdigit() and _readable_file(path, root=home):
            candidates.append((int(suffix), path))
    return max(candidates)[1] if candidates else None


def _codex_rollout_path(
    state_db: Path, session: dict[str, Any], native_id: str,
) -> tuple[str, Path | None]:
    try:
        connection = sqlite3.connect(
            f"{state_db.resolve().as_uri()}?mode=ro", uri=True,
        )
        connection.row_factory = sqlite3.Row
        if native_id:
            row = connection.execute(
                "SELECT id,rollout_path FROM threads WHERE id=?", (native_id,)
            ).fetchone()
        else:
            row = _nearest_codex_thread(connection, session)
        connection.close()
    except (OSError, sqlite3.Error):
        return "provider_index_unavailable", None
    if row is None:
        return "provider_session_not_found", None
    recovered_id = _valid_native_id(row["id"])
    if not recovered_id:
        return "provider_session_not_found", None
    path = Path(str(row["rollout_path"])).expanduser()
    return recovered_id, path


def _nearest_codex_thread(
    connection: sqlite3.Connection, session: dict[str, Any],
) -> sqlite3.Row | None:
    started_at = str(session.get("started_at") or "")
    try:
        launch_time = datetime.fromisoformat(started_at).timestamp()
    except ValueError:
        return None
    expected_cwd = os.path.realpath(str(session.get("working_dir") or ""))
    rows = connection.execute(
        """SELECT id,rollout_path,cwd,created_at FROM threads
           WHERE created_at BETWEEN ? AND ?""",
        (int(launch_time) - 5, int(launch_time) + 600),
    ).fetchall()
    matches = [
        (abs(float(row["created_at"]) - launch_time), row)
        for row in rows if os.path.realpath(str(row["cwd"])) == expected_cwd
    ]
    matches.sort(key=lambda item: (item[0], str(item[1]["id"])))
    if not matches or (len(matches) > 1 and matches[0][0] == matches[1][0]):
        return None
    return matches[0][1]


def _codex_messages(
    session: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, str, int]:
    home = _provider_home(session, "codex")
    recorded_native_id = str(session.get("provider_session_id") or "").strip()
    native_id = _valid_native_id(recorded_native_id)
    if recorded_native_id and not native_id:
        return None, "invalid_provider_session_id", 0
    verified_path = session.get("_verified_transcript_path")
    if verified_path is not None:
        if not native_id:
            return None, "invalid_provider_session_id", 0
        recovered_id, path = native_id, Path(str(verified_path))
    else:
        state_db = _codex_state_db(home)
        if state_db is None:
            return None, "provider_index_unavailable", 0
        recovered_id, path = _codex_rollout_path(state_db, session, native_id)
        if path is None:
            return None, recovered_id, 0
    if not _readable_transcript(path, root=home):
        return None, "provider_transcript_unavailable", 0

    messages: list[dict[str, Any]] = []
    cursor = 0
    try:
        for cursor, row in _indexed_json_lines(path):
            if row is None:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "message":
                continue
            role = str(payload.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            text = _content_text(
                payload.get("content"),
                allowed={"input_text"} if role == "user" else {"output_text"},
            )
            if role == "user":
                text = _visible_user_text(text)
            if not text:
                continue
            messages.append(_message(
                cursor, role, text,
                message_id=str(payload.get("id") or ""),
                created_at=str(row.get("timestamp") or ""),
                phase=str(payload.get("phase") or ""),
            ))
    except OSError:
        return None, "provider_transcript_unavailable", 0
    return messages, f"codex:{recovered_id}", cursor


def _claude_messages(
    session: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, str, int]:
    recorded_native_id = str(session.get("provider_session_id") or "").strip()
    native_id = _valid_native_id(recorded_native_id)
    if recorded_native_id and not native_id:
        return None, "invalid_provider_session_id", 0
    home = _provider_home(session, "claude")
    verified_path = session.get("_verified_transcript_path")
    if verified_path is not None:
        if not native_id:
            return None, "invalid_provider_session_id", 0
        path = Path(str(verified_path))
        if not _readable_transcript(path, root=home):
            return None, "provider_transcript_unavailable", 0
    else:
        project_dir = home / "projects" / os.path.realpath(
            str(session.get("working_dir") or "")
        ).replace(os.sep, "-")
        if native_id:
            path = project_dir / f"{native_id}.jsonl"
            if not _readable_transcript(path, root=home):
                return None, "provider_session_not_found", 0
        else:
            native_id, path = _nearest_claude_transcript(
                project_dir, session, provider_home=home,
            )
    if path is None:
        return None, "provider_session_not_found", 0

    messages: list[dict[str, Any]] = []
    cursor = 0
    try:
        for cursor, row in _indexed_json_lines(path):
            if row is None:
                continue
            if row.get("isSidechain") is True or row.get("type") not in {"user", "assistant"}:
                continue
            payload = row.get("message")
            if not isinstance(payload, dict):
                continue
            role = str(payload.get("role") or row.get("type") or "")
            if role not in {"user", "assistant"}:
                continue
            text = _claude_content_text(payload.get("content"))
            if role == "user":
                text = _visible_user_text(text)
            if not text:
                continue
            messages.append(_message(
                cursor, role, text,
                message_id=str(row.get("uuid") or payload.get("id") or ""),
                created_at=str(row.get("timestamp") or ""),
                phase="",
            ))
    except OSError:
        return None, "provider_transcript_unavailable", 0
    return messages, f"claude:{native_id}", cursor


def _nearest_claude_transcript(
    project_dir: Path, session: dict[str, Any], *, provider_home: Path,
) -> tuple[str, Path | None]:
    try:
        launch_time = datetime.fromisoformat(str(session.get("started_at") or "")).timestamp()
    except ValueError:
        return "", None
    expected_cwd = os.path.realpath(str(session.get("working_dir") or ""))
    matches: list[tuple[float, str, Path]] = []
    for path in project_dir.glob("*.jsonl"):
        native_id = _valid_native_id(path.stem)
        if not native_id or not _readable_transcript(path, root=provider_home):
            continue
        try:
            first = next(_json_lines(path, maximum=64), None)
        except OSError:
            continue
        if not first or os.path.realpath(str(first.get("cwd") or "")) != expected_cwd:
            continue
        try:
            created = datetime.fromisoformat(
                str(first.get("timestamp") or "").replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            continue
        if launch_time - 5 <= created <= launch_time + 600:
            matches.append((abs(created - launch_time), native_id, path))
    matches.sort(key=lambda item: (item[0], item[1]))
    if not matches or (len(matches) > 1 and matches[0][0] == matches[1][0]):
        return "", None
    return matches[0][1], matches[0][2]


def _readable_transcript(path: Path, *, root: Path) -> bool:
    return _readable_file(path, root=root, max_bytes=MAX_TRANSCRIPT_BYTES)


def _readable_file(
    path: Path, *, root: Path, max_bytes: int | None = None,
) -> bool:
    try:
        resolved_root = root.expanduser().resolve(strict=True)
        resolved = path.expanduser().resolve(strict=True)
        return (
            resolved.is_file()
            and (resolved == resolved_root or resolved_root in resolved.parents)
            and (max_bytes is None or resolved.stat().st_size <= max_bytes)
        )
    except OSError:
        return False


def _json_lines(path: Path, maximum: int | None = None) -> Iterable[dict[str, Any]]:
    for _line_number, row in _indexed_json_lines(path, maximum=maximum):
        if row is not None:
            yield row


def _indexed_json_lines(
    path: Path, maximum: int | None = None,
) -> Iterable[tuple[int, dict[str, Any] | None]]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            if maximum is not None and line_number > maximum:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                row = None
            yield line_number, row if isinstance(row, dict) else None


def _content_text(content: Any, *, allowed: set[str]) -> str:
    if not isinstance(content, list):
        return ""
    parts = [
        str(item.get("text") or "").strip()
        for item in content
        if isinstance(item, dict) and item.get("type") in allowed
    ]
    return "\n\n".join(part for part in parts if part).strip()


def _claude_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n\n".join(
        str(item.get("text") or "").strip()
        for item in content
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
    ).strip()


def _visible_user_text(text: str) -> str:
    """Remove known provider/host envelopes from user-role transcript rows."""
    stripped = text.strip()
    if stripped.startswith(_MACHINE_USER_PREFIXES):
        return ""
    if (
        stripped.startswith("# AGENTS.md instructions for ")
        and "<environment_context>" in stripped
    ):
        return ""
    return _INTERNAL_USER_BLOCK_RE.sub("", text).strip()


def _message(
    seq: int, role: str, content: str, *, message_id: str, created_at: str, phase: str,
) -> dict[str, Any]:
    truncated = len(content) > MAX_MESSAGE_CHARS
    return {
        "seq": seq,
        "id": (message_id or f"message-{seq}")[:200],
        "role": role,
        "phase": phase if phase in {"commentary", "final_answer"} else "",
        "created_at": created_at[:100],
        "content": content[:MAX_MESSAGE_CHARS],
        "truncated": truncated,
    }


def _bounded_page(
    eligible: list[dict[str, Any]], *, limit: int,
) -> list[dict[str, Any]]:
    """Keep one page below Project Core's stricter proxy response ceiling."""
    newest_first: list[dict[str, Any]] = []
    used = 0
    for item in reversed(eligible):
        if len(newest_first) >= limit:
            break
        item_bytes = len(json.dumps(
            item, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8"))
        if newest_first and used + item_bytes > MAX_PAGE_MESSAGE_BYTES:
            break
        newest_first.append(item)
        used += item_bytes
    return list(reversed(newest_first))
