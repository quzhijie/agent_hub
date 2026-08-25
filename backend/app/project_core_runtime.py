"""Agent Hub side of the provider-neutral Project Core session protocol.

The runtime owns completion gating and a durable local outbox.  It deliberately
does not import Project Core packages or inspect provider transcript databases.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import urllib.error
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from . import db, project_core, store, tmux


_REPORT_STATUSES = {"completed", "partial", "blocked", "waiting_user", "no_change"}
_ARRAY_FIELDS = ("accomplished", "decisions_proposed", "blockers", "next_steps")
_CONTRACT_MARKER = "PROJECT_CORE_REPORT_CONTRACT_V3"
_RUNTIME_BINDING_MARKER = "PROJECT_CORE_RUNTIME_BINDINGS_V1"
_MAX_REPORT_BYTES = 4_096
_MAX_SUMMARY_CHARS = 500
_MAX_ITEMS = 5
_MAX_ITEM_CHARS = 300
_MAX_IO_ITEMS = 10
_MAX_IO_ITEM_BYTES = 1_024
# Must match Project Core's v1 per-evidence ceiling.  The runtime additionally
# trims its own Git path sample to the encoded-byte boundary so a repository
# with deep paths cannot turn an otherwise valid checkpoint into dead letter.
_MAX_EVIDENCE_ITEM_BYTES = 4_096
_IO_FIELDS = {
    "relation", "resource_binding_id", "path", "slot", "label", "required",
    "sha256",
}
_IO_SLOT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _metadata(session: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(session.get("project_core_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_registered(session: dict[str, Any]) -> bool:
    metadata = _metadata(session)
    return (
        session.get("project_core_tracking") == "on"
        and metadata.get("registration_status") == "registered"
        and bool(metadata.get("association_id"))
    )


def _validate_io(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_IO_ITEMS:
        raise ValueError(f"checkpoint io must contain at most {_MAX_IO_ITEMS} items")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        field = f"checkpoint io[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{field} must be a JSON object")
        unknown = sorted(set(item) - _IO_FIELDS)
        if unknown:
            raise ValueError(f"{field} has unsupported fields: {', '.join(unknown)}")
        relation = item.get("relation")
        if relation not in {"input", "output"}:
            raise ValueError(f"{field} relation must be input or output")
        slot = item.get("slot")
        if not isinstance(slot, str) or not _IO_SLOT_RE.fullmatch(slot):
            raise ValueError(f"{field} slot is not a valid attachment slot")
        raw_path = item.get("path")
        if (
            not isinstance(raw_path, str) or not raw_path.strip()
            or raw_path != raw_path.strip()
            or any(ord(character) < 32 for character in raw_path)
        ):
            raise ValueError(f"{field} path must be a non-empty relative path")
        path = posixpath.normpath(raw_path.replace("\\", "/"))
        portable = PurePosixPath(path)
        if (
            portable.is_absolute() or path in {".", ".."}
            or path.startswith("../") or re.match(r"^[A-Za-z]:/", path)
        ):
            raise ValueError(f"{field} path must stay inside its Resource")
        if len(path) > 1_000:
            raise ValueError(f"{field} path exceeds 1000 characters")
        binding_id = item.get("resource_binding_id")
        if binding_id is not None and (
            not isinstance(binding_id, str) or not binding_id.strip()
            or binding_id != binding_id.strip() or len(binding_id) > 200
        ):
            raise ValueError(f"{field} resource_binding_id is invalid")
        required = item.get("required", relation == "input")
        if not isinstance(required, bool):
            raise ValueError(f"{field} required must be a boolean")
        declaration: dict[str, Any] = {
            "relation": relation, "path": path, "slot": slot,
            "required": required,
        }
        if binding_id:
            declaration["resource_binding_id"] = binding_id
        label = item.get("label")
        if label is not None:
            if (
                not isinstance(label, str) or not label.strip()
                or label != label.strip() or len(label) > 200
                or any(ord(character) < 32 for character in label)
            ):
                raise ValueError(f"{field} label must contain 1-200 characters")
            declaration["label"] = label
        digest = item.get("sha256")
        if digest is not None:
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise ValueError(f"{field} sha256 must be a SHA-256")
            declaration["sha256"] = digest
        normalized.append(declaration)
    return normalized


def _validate_report(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise ValueError("checkpoint report must be a JSON object")
    allowed = {"report_schema_version", "status", "summary", "io", *_ARRAY_FIELDS}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unsupported checkpoint fields: {', '.join(unknown)}")
    if value.get("report_schema_version", 1) != 1:
        raise ValueError("unsupported checkpoint report schema")
    status = value.get("status")
    if status not in _REPORT_STATUSES:
        raise ValueError("invalid checkpoint status")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary.strip()) > _MAX_SUMMARY_CHARS:
        raise ValueError("checkpoint summary must contain 1-500 characters")
    normalized: dict[str, Any] = {
        "report_schema_version": 1,
        "status": status,
        "summary": summary.strip(),
    }
    for field in _ARRAY_FIELDS:
        items = value.get(field, [])
        if not isinstance(items, list) or len(items) > _MAX_ITEMS:
            raise ValueError(f"checkpoint {field} must contain at most {_MAX_ITEMS} items")
        clean: list[str] = []
        for item in items:
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > _MAX_ITEM_CHARS:
                raise ValueError(f"checkpoint {field} items must contain 1-300 characters")
            if any(ord(character) < 32 for character in item):
                raise ValueError(f"checkpoint {field} contains control characters")
            clean.append(item.strip())
        normalized[field] = clean
    if status == "no_change" and any(normalized[field] for field in _ARRAY_FIELDS):
        raise ValueError("no_change checkpoint arrays must be empty")
    if len(_canonical(normalized).encode("utf-8")) > _MAX_REPORT_BYTES:
        raise ValueError("checkpoint report exceeds 4096 bytes")
    io = _validate_io(value.get("io"))
    if status == "no_change" and io:
        raise ValueError("no_change checkpoint cannot declare Resource I/O")
    return normalized, io


def _run_git(working_dir: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", working_dir, *args], capture_output=True, text=True,
            timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def collect_git_evidence(working_dir: str) -> dict[str, Any]:
    """Collect bounded machine facts without file bodies, diffs, or commands."""
    try:
        scope = Path(working_dir).resolve(strict=True)
    except (OSError, RuntimeError):
        return {}
    root_text = _run_git(str(scope), "rev-parse", "--show-toplevel")
    head = _run_git(str(scope), "rev-parse", "HEAD")
    if not root_text or not head or not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
        return {}
    try:
        root = Path(root_text).resolve(strict=True)
    except (OSError, RuntimeError):
        return {}
    raw = _run_git(str(scope), "status", "--porcelain=v1", "-z", "--untracked-files=all")
    changed: list[str] = []
    if raw:
        for entry in raw.split("\0"):
            if len(entry) < 4 or entry[2] != " ":
                continue
            relative = entry[3:]
            try:
                resolved = (root / relative).resolve(strict=False)
                if resolved == scope or resolved.is_relative_to(scope):
                    changed.append(resolved.relative_to(scope).as_posix())
            except (OSError, RuntimeError, ValueError):
                continue
    changed = sorted(set(changed))
    return {
        "commit": head.lower(),
        "dirty": bool(raw),
        "changed_count": len(changed),
        "changed_paths": changed[:20],
        "changed_paths_sha256": _digest(changed),
    }


def _host_evidence(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    if not before and not after:
        return []
    commit = str(after.get("commit") or before.get("commit") or "")
    paths = list(after.get("changed_paths") or [])
    while True:
        observation = {
            "before_commit": before.get("commit"),
            "after_commit": after.get("commit"),
            "dirty_after": bool(after.get("dirty")),
            "changed_count": int(after.get("changed_count") or 0),
            "changed_paths": paths,
            # This digest is over the complete path set collected at the turn
            # boundary, not merely the display sample retained below.
            "changed_paths_sha256": after.get("changed_paths_sha256") or _digest([]),
        }
        item = {
            "kind": "git_worktree",
            "uri": f"urn:agent-hub:git:{commit or _digest(observation)}",
            "sha256": _digest(observation),
            **observation,
        }
        if len(_canonical(item).encode("utf-8")) <= _MAX_EVIDENCE_ITEM_BYTES:
            return [item]
        if not paths:
            raise ValueError("bounded Git evidence exceeds the Project Core contract")
        paths = paths[:-1]


def _required_identity(metadata: dict[str, Any]) -> dict[str, str]:
    fields = {
        "project_ref": "project_id", "workstream_ref": "record_id",
        "correlation_id": "correlation_id", "context_pack_id": "context_pack_id",
        "context_pack_sha256": "context_pack_sha256", "provider": "provider",
        "producer_instance": "provider_instance",
        "actor_external_id": "principal_external_id",
        "actor_kind": "principal_kind",
    }
    result: dict[str, str] = {}
    for output, source in fields.items():
        value = metadata.get(source)
        if not isinstance(value, str) or not value:
            raise ValueError(f"registered session is missing {source}")
        result[output] = value
    return result


def _event_base(session: dict[str, Any], metadata: dict[str, Any], event_type: str) -> dict[str, Any]:
    identity = _required_identity(metadata)
    return {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": _utc_now(),
        "producer": "agent",
        "producer_instance": identity["producer_instance"],
        "project_ref": identity["project_ref"],
        "workstream_ref": identity["workstream_ref"],
        "correlation_id": identity["correlation_id"],
        "actor": {
            "external_id": identity["actor_external_id"],
            "kind": identity["actor_kind"],
        },
        "subject": {
            "provider": identity["provider"],
            "session_id": session["id"],
            "context_pack_id": identity["context_pack_id"],
            "context_pack_sha256": identity["context_pack_sha256"],
        },
    }


def _report_contract(config_path: Path) -> str:
    script = Path(__file__).resolve().parents[1] / "report_checkpoint.py"
    return f"""[{_CONTRACT_MARKER}]
This session is tracked by Project Core, but checkpoints are explicit opt-in. Do not submit
one automatically. Submit exactly one only when the user explicitly asks to save or send a
Project Core checkpoint using "check" or "checkpoint" as that instruction. Merely discussing
checkpoints, asking how they work, or ending an ordinary turn does not authorize a report.
This contract supersedes any PROJECT_CORE_REPORT_CONTRACT_V1 or
PROJECT_CORE_REPORT_CONTRACT_V2 text earlier in the session.

When explicitly requested, submit one small checkpoint from this same agent context before
returning control. Do not repeat the final answer, file list, diff, or test logs. Run this
local tool with JSON on stdin:

python3 {shlex.quote(str(script))} --config {shlex.quote(str(config_path))} <<'JSON'
{{"report_schema_version":1,"status":"completed","summary":"What changed",\
"accomplished":[],"decisions_proposed":[],"blockers":[],"next_steps":[],\
"io":[{{"relation":"output","path":"outputs/summary.csv",\
"slot":"summary-output","label":"Summary table"}}]}}
JSON

Allowed status: completed, partial, blocked, waiting_user, no_change. Summary <=500 chars;
each report array <=5 short items. For no_change all arrays and io must be empty. Use io only
for stable scientific inputs or outputs established or changed by this Workstream. Paths must
be relative to a Project Resource, never machine-absolute; omit resource_binding_id to use the
seat's workspace Resource, or copy another binding ID from the Context Pack. An output requires
a read-write binding; a read-only seat Resource can only be declared as input. Keep slot stable
when a path is replaced. Do not enumerate routine source changes, diffs, secrets, or logs. The
host adds bounded Git evidence and durably queues delivery. The tool's pending outbox state
means only Agent Hub has persisted it; do not claim Project Core received the checkpoint
unless the returned state is delivered.
After the tool confirms the local write, return your normal final response. Without an
explicit request, return normally without calling this tool."""


def _runtime_bindings(config_path: Path, metadata: dict[str, Any]) -> str:
    """Bind host-local commands to Core-authored semantics without copying them."""
    script = Path(__file__).resolve().parents[1] / "report_checkpoint.py"
    proposal_script = Path(__file__).resolve().parents[1] / "manage_change_proposal.py"
    manual_index = str(metadata.get("manual_index_path") or "")
    checkpoint_manual = (
        str(Path(manual_index).parent / "tools" / "checkpoint.md")
        if manual_index else ""
    )
    proposal_manual = (
        str(Path(manual_index).parent / "tools" / "change-proposal.md")
        if manual_index else ""
    )
    execution_manual = (
        str(Path(manual_index).parent / "tools" / "restricted-execution.md")
        if manual_index else ""
    )
    return "\n".join([
        f"[{_RUNTIME_BINDING_MARKER}]",
        "Project Core tool semantics are in the pinned Agent Manual index; this block only binds host-local paths.",
        f"checkpoint_manual: {checkpoint_manual}",
        "checkpoint_command: "
        f"python3 {shlex.quote(str(script))} --config {shlex.quote(str(config_path))}",
        f"change_proposal_manual: {proposal_manual}",
        "change_proposal_command: "
        f"python3 {shlex.quote(str(proposal_script))} "
        f"--config {shlex.quote(str(config_path))} --action",
        f"restricted_execution_manual: {execution_manual}",
    ])


def _restore_registration_binding(
    metadata: dict[str, Any], *, data_dir: Path
) -> dict[str, Any]:
    """Upgrade pre-V3 session metadata from its exact private handoff."""
    if metadata.get("resource_binding_id"):
        return metadata
    association_id = str(metadata.get("association_id") or "")
    if not association_id:
        return metadata
    handoff_path = data_dir / "project_core_handoffs" / f"{association_id}.json"
    try:
        response = json.loads(handoff_path.read_text(encoding="utf-8"))
        association = response.get("association")
    except (OSError, TypeError, json.JSONDecodeError):
        return metadata
    if not isinstance(association, dict):
        return metadata
    expected = {
        "id": metadata.get("association_id"),
        "project_ref": metadata.get("project_id"),
        "workstream_ref": metadata.get("record_id"),
        "context_pack_id": metadata.get("context_pack_id"),
        "context_pack_sha256": metadata.get("context_pack_sha256"),
    }
    if any(association.get(field) != value for field, value in expected.items()):
        return metadata
    binding_id = association.get("resource_binding_id")
    if isinstance(binding_id, str) and binding_id:
        return {**metadata, "resource_binding_id": binding_id}
    return metadata


def install_reporting_contract(
    session: dict[str, Any], *, data_dir: Path, db_path: Path,
    runtime_file: Path,
) -> dict[str, Any]:
    if not _is_registered(session):
        return session
    metadata = _restore_registration_binding(_metadata(session), data_dir=data_dir)
    config_dir = data_dir / "project_core_reports"
    config_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(config_dir, 0o700)
    except OSError:
        pass
    config_path = config_dir / f"{session['id']}.json"
    temporary = config_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({
            "schema_version": 1, "db_path": str(db_path), "session_id": session["id"],
            "runtime_file": str(runtime_file),
            "association_id": str(metadata["association_id"]),
        }, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(config_path)
    metadata["report_config_path"] = str(config_path)
    prompt = session.get("initial_prompt", "")
    if metadata.get("manual_index_path"):
        if _RUNTIME_BINDING_MARKER not in prompt:
            prompt = f"{prompt}\n\n{_runtime_bindings(config_path, metadata)}".strip()
    elif _CONTRACT_MARKER not in prompt:
        # Compatibility for associations created by a pre-handbook Core.
        prompt = f"{prompt}\n\n{_report_contract(config_path)}".strip()
    updated = store.update_session_project_core(
        session["id"], project_core=metadata, initial_prompt=prompt,
        project_core_tracking="on",
    )
    return store.update_project_core_runtime(
        session["id"], lifecycle="registered", warning=""
    ) or updated


def _load_report_config(path: Path) -> tuple[Path, str]:
    resolved = path.expanduser().resolve(strict=True)
    stat = resolved.stat()
    if stat.st_mode & 0o077:
        raise PermissionError("report config must not be group/world readable")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("unsupported report config")
    db_path = value.get("db_path")
    session_id = value.get("session_id")
    if not isinstance(db_path, str) or not isinstance(session_id, str) or not session_id:
        raise ValueError("report config is incomplete")
    return Path(db_path), session_id


def _settle_reported_turn_from_history(
    connection,
    turn,
) -> bool:
    """Repair a missed turn-boundary callback from durable status history.

    Builds affected by the old ``completed``/``done`` mismatch still recorded
    the ACTIVE -> DONE session event even though the Project Core turn remained
    unsettled.  A later settled/waiting -> ACTIVE edge is equally conclusive:
    the provider has begun another turn, so a report written before that edge
    cannot still belong to the current one.  Without either boundary we leave
    the row untouched and keep rejecting a second report.
    """
    if (
        turn is None
        or turn["state"] != "reported"
        or turn["settle_kind"]
        or not turn["reported_at"]
    ):
        return False
    edge = connection.execute(
        """SELECT old_status,new_status FROM session_events
           WHERE session_id=? AND kind='status_changed'
             AND created_at>=?
             AND (
                 (old_status=? AND new_status IN (?, ?))
                 OR
                 (new_status=? AND old_status IN (?, ?, ?))
             )
           ORDER BY created_at, rowid LIMIT 1""",
        (
            turn["session_id"], turn["reported_at"],
            store.ACTIVE, store.WAITING, store.DONE,
            store.ACTIVE, store.IDLE, store.WAITING, store.DONE,
        ),
    ).fetchone()
    if edge is None:
        return False
    settle = (
        "waiting_user"
        if edge["new_status"] == store.WAITING or edge["old_status"] == store.WAITING
        else "completed"
    )
    connection.execute(
        """UPDATE project_core_turns SET settle_kind=?, updated_at=?
           WHERE id=? AND state='reported' AND settle_kind=''""",
        (settle, store.now_iso(), turn["id"]),
    )
    return True


def submit_checkpoint(config_path: Path, report: Any) -> dict[str, Any]:
    db_path, session_id = _load_report_config(config_path)
    db.init_db(db_path)
    session = store.get_session(session_id)
    if session is None or not _is_registered(session):
        raise ValueError("session is not registered with Project Core")
    metadata = _metadata(session)
    normalized, declarations = _validate_report(report)
    default_binding_id = str(metadata.get("resource_binding_id") or "")
    io_evidence = []
    for declaration in declarations:
        binding_id = str(
            declaration.get("resource_binding_id") or default_binding_id
        )
        if not binding_id:
            raise ValueError(
                "checkpoint io requires resource_binding_id because the session "
                "has no workspace Resource binding"
            )
        io_evidence.append({
            "kind": "project_resource_io",
            **declaration,
            "resource_binding_id": binding_id,
        })
        if len(_canonical(io_evidence[-1]).encode("utf-8")) > _MAX_IO_ITEM_BYTES:
            raise ValueError(
                f"checkpoint io[{len(io_evidence) - 1}] exceeds "
                f"{_MAX_IO_ITEM_BYTES} bytes"
            )
    association_id = str(metadata["association_id"])
    after = collect_git_evidence(session["working_dir"])
    report_sha = (
        _digest(normalized) if not io_evidence
        else _digest({"report": normalized, "io": io_evidence})
    )
    now = store.now_iso()
    with db.transaction() as connection:
        latest = connection.execute(
            """SELECT * FROM project_core_turns WHERE session_id=? AND association_id=?
               ORDER BY turn_seq DESC LIMIT 1""",
            (session_id, association_id),
        ).fetchone()
        if latest is not None and latest["state"] == "reported" and not latest["settle_kind"]:
            if latest["report_sha256"] != report_sha:
                if not _settle_reported_turn_from_history(connection, latest):
                    raise ValueError(
                        "the current turn already has a different checkpoint report"
                    )
                latest = connection.execute(
                    "SELECT * FROM project_core_turns WHERE id=?", (latest["id"],)
                ).fetchone()
            else:
                outbox = connection.execute(
                    "SELECT event_id, state FROM project_core_outbox WHERE turn_id=?",
                    (latest["id"],),
                ).fetchone()
                return {
                    "ok": True, "duplicate": True, "turn_id": latest["id"],
                    "turn_seq": latest["turn_seq"], "outbox_state": outbox["state"],
                }
        if latest is not None and latest["state"] in {"open", "missing"}:
            turn = latest
        else:
            turn_id = f"turn_{uuid.uuid4().hex}"
            turn_seq = (latest["turn_seq"] + 1) if latest is not None else 1
            before = collect_git_evidence(session["working_dir"])
            connection.execute(
                """INSERT INTO project_core_turns(
                       id, session_id, association_id, turn_seq, state,
                       git_before_json, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?)""",
                (
                    turn_id, session_id, association_id, turn_seq,
                    _canonical(before), now, now,
                ),
            )
            turn = connection.execute(
                "SELECT * FROM project_core_turns WHERE id=?", (turn_id,)
            ).fetchone()
        before = json.loads(turn["git_before_json"] or "{}")
        evidence = [*_host_evidence(before, after), *io_evidence]
        envelope = _event_base(session, metadata, "agent.turn_reported")
        envelope["causation_id"] = turn["id"]
        envelope["subject"] = {
            **envelope["subject"], "turn_id": turn["id"], "turn_seq": turn["turn_seq"],
        }
        envelope["payload"] = normalized
        envelope["evidence"] = evidence
        envelope["idempotency_key"] = (
            f"{envelope['producer_instance']}:{session_id}:{turn['id']}:report:v1"
        )
        event_json = _canonical(envelope)
        event_sha = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
        connection.execute(
            """UPDATE project_core_turns SET
                   state='reported', report_json=?, report_sha256=?, evidence_json=?,
                   report_bytes=?, git_after_json=?, updated_at=?, reported_at=?
               WHERE id=?""",
            (
                _canonical(normalized), report_sha, _canonical(evidence),
                len(_canonical(normalized).encode("utf-8")), _canonical(after),
                now, now, turn["id"],
            ),
        )
        connection.execute(
            """INSERT INTO project_core_outbox(
                   event_id, session_id, association_id, turn_id, event_type, idempotency_key,
                   envelope_json, envelope_sha256, state, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'agent.turn_reported', ?, ?, ?, 'pending', ?, ?)""",
            (
                envelope["event_id"], session_id, association_id, turn["id"],
                envelope["idempotency_key"], event_json, event_sha, now, now,
            ),
        )
    store.update_project_core_runtime(session_id, warning="")
    return {
        "ok": True, "duplicate": False, "turn_id": turn["id"],
        "turn_seq": turn["turn_seq"], "outbox_state": "pending",
        "report_bytes": len(_canonical(normalized).encode("utf-8")),
    }


class ProjectCoreRuntime:
    def __init__(self, settings):
        self.settings = settings

    def install_contract(self, session: dict[str, Any]) -> dict[str, Any]:
        return install_reporting_contract(
            session, data_dir=self.settings.data_dir, db_path=self.settings.db_path,
            runtime_file=self.settings.project_core_runtime_file,
        )

    def _begin_turn(
        self, session: dict[str, Any], *, settle_previous: str = "",
    ) -> dict[str, Any] | None:
        if not _is_registered(session):
            return None
        before = collect_git_evidence(session["working_dir"])
        association_id = str(_metadata(session)["association_id"])
        now = store.now_iso()
        with db.transaction() as connection:
            latest = connection.execute(
                """SELECT * FROM project_core_turns
                   WHERE session_id=? AND association_id=?
                   ORDER BY turn_seq DESC LIMIT 1""",
                (session["id"], association_id),
            ).fetchone()
            if latest is not None and latest["state"] == "open":
                return dict(latest)
            if (
                latest is not None
                and latest["state"] == "reported"
                and not latest["settle_kind"]
            ):
                # A new ACTIVE episode after idle/done/waiting is itself a
                # provider-neutral turn boundary.  The sampler can miss the
                # preceding completion edge (notably when an attention state
                # was acknowledged to idle), but it must not then reuse the
                # old reported row for this new request.
                if not settle_previous:
                    return dict(latest)
                connection.execute(
                    """UPDATE project_core_turns SET settle_kind=?, updated_at=?
                       WHERE id=? AND state='reported' AND settle_kind=''""",
                    (settle_previous, now, latest["id"]),
                )
            turn_id = f"turn_{uuid.uuid4().hex}"
            turn_seq = latest["turn_seq"] + 1 if latest is not None else 1
            connection.execute(
                """INSERT INTO project_core_turns(
                       id, session_id, association_id, turn_seq, state,
                       git_before_json, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?)""",
                (
                    turn_id, session["id"], association_id, turn_seq,
                    _canonical(before), now, now,
                ),
            )
        return store.get_project_core_turn(turn_id)

    def _enqueue_lifecycle(
        self, session: dict[str, Any], event_type: str, payload: dict[str, Any],
    ) -> None:
        metadata = _metadata(session)
        association_id = str(metadata["association_id"])
        envelope = _event_base(session, metadata, event_type)
        envelope["payload"] = payload
        envelope["evidence"] = []
        suffix = event_type.rsplit(".", 1)[-1]
        envelope["idempotency_key"] = (
            f"{envelope['producer_instance']}:{association_id}:{suffix}:v1"
        )
        event_json = _canonical(envelope)
        now = store.now_iso()
        with db.transaction() as connection:
            exists = connection.execute(
                "SELECT event_id FROM project_core_outbox WHERE idempotency_key=?",
                (envelope["idempotency_key"],),
            ).fetchone()
            if exists is not None:
                return
            connection.execute(
                """INSERT INTO project_core_outbox(
                       event_id, session_id, association_id, turn_id, event_type, idempotency_key,
                       envelope_json, envelope_sha256, state, created_at, updated_at
                   ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    envelope["event_id"], session["id"], association_id, event_type,
                    envelope["idempotency_key"], event_json,
                    hashlib.sha256(event_json.encode("utf-8")).hexdigest(), now, now,
                ),
            )

    def session_started(self, session: dict[str, Any], *, first_start: bool) -> None:
        if not _is_registered(session):
            return
        self._enqueue_lifecycle(
            session, "agent.session_started", {"lifecycle_schema_version": 1},
        )
        store.update_project_core_runtime(session["id"], lifecycle="active", warning="")
        if first_start:
            self._begin_turn(session)
        self.flush_outbox()

    def observe_status(
        self, session: dict[str, Any], old_status: str, new_status: str,
        edge_kind: str | None, *, unexpected_exit: bool = False,
    ) -> None:
        current = store.get_session(session["id"])
        if current is None or not _is_registered(current):
            return
        if unexpected_exit or new_status == store.EXITED:
            self.close_session(current, abandoned=True, reason="runtime-exited")
            return
        if new_status == store.ACTIVE and old_status != store.ACTIVE:
            settle_previous = (
                "waiting_user" if old_status == store.WAITING
                else "completed" if old_status in {store.IDLE, store.DONE}
                else ""
            )
            self._begin_turn(current, settle_previous=settle_previous)
            return
        # A reported checkpoint is settled at the next provider-neutral
        # completion edge. An unreported row is only an evidence window: it
        # deliberately stays open across ordinary turns until the user asks
        # for a checkpoint. Completion is not reporting consent.
        if edge_kind not in {"waiting", "done"}:
            return
        association_id = str(_metadata(current)["association_id"])
        with db.transaction() as connection:
            turn = connection.execute(
                """SELECT * FROM project_core_turns
                   WHERE session_id=? AND association_id=?
                   ORDER BY turn_seq DESC LIMIT 1""",
                (current["id"], association_id),
            ).fetchone()
            if turn is None:
                return
            settle = "waiting_user" if edge_kind == "waiting" else "completed"
            if turn["state"] == "reported":
                connection.execute(
                    "UPDATE project_core_turns SET settle_kind=?, updated_at=? WHERE id=?",
                    (settle, store.now_iso(), turn["id"]),
                )
        store.update_project_core_runtime(current["id"], warning="")

    def notify_late_association(self, session: dict[str, Any]) -> None:
        if not session.get("started_at") or not tmux.has_session(session["tmux_session"]):
            return
        metadata = _metadata(session)
        config_path = Path(str(metadata["report_config_path"]))
        if metadata.get("startup_bundle_path"):
            message = (
                project_core.render_agent_startup(
                    runtime_file=self.settings.project_core_runtime_file,
                    association_id=str(metadata["association_id"]),
                    startup_sha256=str(metadata["agent_startup_sha256"]),
                    startup_bundle_path=str(metadata["startup_bundle_path"]),
                    handoff_path=str(metadata["handoff_path"]),
                    manual_index_path=str(metadata["manual_index_path"]),
                    delivery_mode="late_association",
                )
                + "\n\n" + _runtime_bindings(config_path, metadata)
            )
        elif metadata.get("manual_index_path"):
            message = (
                "[Agent Hub Project Core protocol] The user explicitly associated this running "
                "seat with Project Core. Read the exact handoff at "
                f"{metadata['handoff_path']} and the pinned Agent Manual index at "
                f"{metadata['manual_index_path']} before substantive work.\n\n"
                + _runtime_bindings(config_path, metadata)
            )
        else:
            message = (
                "[Agent Hub Project Core protocol] The user explicitly associated this running "
                "seat with Project Core. Read the exact Context Pack handoff at "
                f"{metadata['handoff_path']}, then use this reporting contract for future turns:\n\n"
                + _report_contract(config_path)
            )
        tmux.send_protocol_message(session["tmux_session"], message)

    def reinject_context(self, session: dict[str, Any]) -> None:
        """Resend one seat's current immutable handoff into its live provider context."""
        if not _is_registered(session):
            raise ValueError("seat is not registered with Project Core")
        if not session.get("started_at"):
            raise ValueError("start the seat before reinjecting context")
        if not tmux.has_session(session["tmux_session"]):
            raise ValueError("seat is not currently running")
        metadata = _metadata(session)
        handoff_path = metadata.get("handoff_path")
        report_path = metadata.get("report_config_path")
        if not isinstance(handoff_path, str) or not handoff_path:
            raise ValueError("registered seat is missing its Context Pack handoff")
        if not isinstance(report_path, str) or not report_path:
            raise ValueError("registered seat is missing its reporting contract")
        role = str(metadata.get("seat_role") or session.get("agent_role") or "general")
        if metadata.get("startup_bundle_path"):
            message = (
                project_core.render_agent_startup(
                    runtime_file=self.settings.project_core_runtime_file,
                    association_id=str(metadata["association_id"]),
                    startup_sha256=str(metadata["agent_startup_sha256"]),
                    startup_bundle_path=str(metadata["startup_bundle_path"]),
                    handoff_path=handoff_path,
                    manual_index_path=str(metadata["manual_index_path"]),
                    delivery_mode="reinject",
                )
                + "\n\n" + _runtime_bindings(Path(report_path), metadata)
            )
        elif metadata.get("manual_index_path"):
            message = (
                "[Agent Hub Project Core protocol] The user requested context reinjection, "
                "typically after an in-provider /new or /clear. "
                f"Your seat role is {role}. Read the exact handoff at {handoff_path} and "
                f"the pinned Agent Manual index at {metadata['manual_index_path']} before "
                "further substantive work. This resends the immutable snapshot registered "
                "to this seat; it does not refresh Project Core state.\n\n"
                + _runtime_bindings(Path(report_path), metadata)
            )
        else:
            message = (
                "[Agent Hub Project Core protocol] The user requested context reinjection, "
                "typically after an in-provider /new or /clear. "
                f"Your seat role is {role}. Read the exact current Context Pack snapshot at "
                f"{handoff_path} before further substantive work. This resends the immutable "
                "snapshot registered to this seat; it does not refresh Project Core state. "
                "Continue using this reporting contract for future turns:\n\n"
                + _report_contract(Path(report_path))
            )
        tmux.send_protocol_message(session["tmux_session"], message)

    def close_session(
        self, session: dict[str, Any], *, abandoned: bool, reason: str = "",
    ) -> None:
        if not _is_registered(session):
            return
        if session.get("project_core_lifecycle") in {"finished", "abandoned"}:
            return
        association_id = str(_metadata(session)["association_id"])
        with db.transaction() as connection:
            # An open row is an unrequested evidence window, not a missed
            # report. Discard it when the seat closes; only explicit reports
            # belong in lifecycle counts and Project Core history.
            connection.execute(
                """DELETE FROM project_core_turns
                   WHERE session_id=? AND association_id=? AND state='open'""",
                (session["id"], association_id),
            )
            counts = connection.execute(
                """SELECT COALESCE(max(turn_seq), 0) AS last_seq,
                          sum(CASE WHEN state='reported' THEN 1 ELSE 0 END) AS reports,
                          sum(CASE WHEN state='missing' THEN 1 ELSE 0 END) AS missing
                   FROM project_core_turns WHERE session_id=? AND association_id=?""",
                (session["id"], association_id),
            ).fetchone()
        event_type = "agent.session_abandoned" if abandoned else "agent.session_finished"
        payload = {
            "lifecycle_schema_version": 1,
            "last_turn_seq": int(counts["last_seq"] or 0),
            "report_count": int(counts["reports"] or 0),
            "report_missing_count": int(counts["missing"] or 0),
            "transcript_uri": (
                f"agent-hub://session/{session['id']}"
                f"/association/{association_id}"
            ),
        }
        if abandoned:
            payload["reason"] = (reason or "runtime-exited")[:200]
        self._enqueue_lifecycle(session, event_type, payload)
        store.update_project_core_runtime(
            session["id"], lifecycle="abandoned" if abandoned else "finished",
            warning=(
                "Project Core session closed with missing checkpoint reports"
                if payload["report_missing_count"] else ""
            ),
        )
        self.flush_outbox()

    def flush_outbox(
        self, *, limit: int = 50, session_id: str | None = None,
    ) -> dict[str, int]:
        if not self.settings.enable_project_core:
            return {"processed": 0, "delivered": 0, "pending": 0, "dead_letter": 0}
        result = {"processed": 0, "delivered": 0, "pending": 0, "dead_letter": 0}
        for item in store.pending_project_core_outbox(
            limit=limit, session_id=session_id,
        ):
            result["processed"] += 1
            try:
                envelope = json.loads(item["envelope_json"])
                if hashlib.sha256(item["envelope_json"].encode("utf-8")).hexdigest() != item["envelope_sha256"]:
                    raise ValueError("outbox envelope integrity check failed")
                response = project_core.deliver_event(
                    envelope, runtime_file=self.settings.project_core_runtime_file,
                )
                # Any signed 2xx response is a durable Core inbox receipt.
                # `dead_letter` here is the *application* state of that accepted
                # event, owned and surfaced by Project Core; treating it as an
                # outbound delivery failure duplicates one problem in both UIs
                # and makes producer Retry repeat a permanent semantic error.
                store.mark_project_core_outbox_delivered(item["event_id"])
                result["delivered"] += 1
            except urllib.error.HTTPError as exc:
                terminal = 400 <= exc.code < 500 and exc.code not in {408, 429}
                self._delivery_failed(
                    item, self._http_error_message(exc), terminal=terminal,
                )
                result["dead_letter" if terminal else "pending"] += 1
                if not terminal:
                    break
            except ValueError:
                self._delivery_failed(item, "invalid local outbox event", terminal=True)
                result["dead_letter"] += 1
            except (OSError, RuntimeError, urllib.error.URLError):
                self._delivery_failed(item, "Project Core is unavailable", terminal=False)
                result["pending"] += 1
                break
        return result

    @staticmethod
    def _http_error_message(exc: urllib.error.HTTPError) -> str:
        """Keep the bounded Gateway reason that makes a dead letter actionable."""
        message = f"HTTP {exc.code}"
        try:
            raw = exc.read(4_097)
        except (OSError, ValueError):
            return message
        if not raw or len(raw) > 4_096:
            return message
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = raw.decode("utf-8", errors="replace")
        else:
            detail = value
            if isinstance(value, dict):
                error = value.get("error")
                if isinstance(error, dict):
                    detail = error.get("message") or error.get("type")
                else:
                    detail = value.get("detail") or error
        if isinstance(detail, str) and detail.strip():
            return f"{message}: {' '.join(detail.split())}"[:500]
        return message

    @staticmethod
    def _delivery_failed(item: dict[str, Any], message: str, *, terminal: bool) -> None:
        attempts = int(item.get("attempts") or 0) + 1
        retry_at = None
        if not terminal:
            retry_at = (
                datetime.now() + timedelta(seconds=min(300, 2 ** min(attempts, 8)))
            ).isoformat(timespec="seconds")
        store.mark_project_core_outbox_failed(
            item["event_id"], error=message, next_attempt_at=retry_at, terminal=terminal,
        )

    def reconcile_on_startup(self) -> None:
        with db.transaction() as connection:
            stale = connection.execute(
                """SELECT * FROM project_core_turns
                   WHERE state='reported' AND settle_kind=''"""
            ).fetchall()
            for turn in stale:
                _settle_reported_turn_from_history(connection, turn)
        for session in store.list_project_core_runtime_sessions():
            # Upgrade the stored prompt/config for seats created under an older
            # contract. Live provider contexts are only changed by the user's
            # explicit reinjection action; an unstarted seat receives V3 on its
            # first launch.
            session = self.install_contract(session)
            if not session.get("started_at"):
                continue
            try:
                alive = tmux.has_session(session["tmux_session"])
            except tmux.TmuxError:
                alive = False
            if not alive:
                self.close_session(session, abandoned=True, reason="runtime-missing-at-startup")
        self.flush_outbox()
