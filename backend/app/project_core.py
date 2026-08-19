"""Narrow Agent Hub client for Project Core session registration.

This module knows only the loopback discovery/HTTP contract.  It never imports
Project Core code or opens its database, so Agent Hub can be replaced without
turning either application's private schema into an integration API.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


_MAX_DISCOVERY_BYTES = 128_000
_MAX_RESPONSE_BYTES = 2_000_000
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "ip6-localhost"}
_ROLE_LABELS = {
    "general": "general", "plan": "planning",
    "implement": "implementation", "review": "review",
}


def resolve_targets(*, working_dir: str, runtime_file: Path) -> dict[str, Any]:
    """Resolve authorized picker choices without preparing or registering a seat."""
    discovery = _read_discovery(runtime_file)
    response = _signed_post(
        discovery["agent_targets_url"],
        {"cwd": working_dir},
        secret=discovery["integration_secrets"]["agent"],
    )
    status = response.get("status")
    candidates = response.get("candidates")
    if status not in {"resolved", "no_match"} or not isinstance(candidates, list):
        raise RuntimeError("Project Core target response is invalid")
    return {"status": status, "candidates": candidates}


def auto_register_session(
    session: dict[str, Any],
    *,
    runtime_file: Path,
    data_dir: Path,
    tracking_mode: str = "suggest",
    association_segment: int = 1,
    selected_target: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Prepare a seat and register only its explicitly selected target.

    Failure is deliberately non-fatal to seat creation.  The returned metadata
    records an unassigned/unavailable state for later recovery without placing
    Project Core IDs or credentials in the model-visible request.
    """

    if tracking_mode not in {"suggest", "on", "off"}:
        raise ValueError("invalid Project Core tracking mode")
    if tracking_mode == "off":
        return {
            "project_core": {"registration_status": "off"},
            "initial_prompt": session.get("initial_prompt", ""),
        }
    try:
        discovery = _read_discovery(runtime_file)
        principal = discovery["integration_principals"]["agent"]
        provider = principal["provider"]
        provider_instance = principal["provider_instance"]
        prepared = _signed_post(
            discovery["agent_sessions_prepare_url"],
            {
                "provider": provider,
                "provider_instance": provider_instance,
                "external_session_id": session["id"],
                "cwd": session["working_dir"],
                "association_segment": association_segment,
            },
            secret=discovery["integration_secrets"]["agent"],
        )
        status = prepared.get("status")
        if selected_target and status in {"one", "ambiguous"}:
            candidates = prepared.get("candidates") or []
            matches = [
                item for item in candidates
                if item.get("project_ref") == selected_target.get("project_id")
                and item.get("workstream_ref") == selected_target.get("record_id")
            ]
            if not matches:
                return _unresolved_selected_target(
                    session, prepared=prepared, selected_target=selected_target,
                )
            registered = _signed_post(
                discovery["agent_sessions_register_url"],
                {
                    "preparation_id": prepared["preparation_id"],
                    "candidate_id": matches[0]["candidate_id"],
                },
                secret=discovery["integration_secrets"]["agent"],
            )
            return _registered_result(
                registered, data_dir=data_dir,
                existing_prompt=session.get("initial_prompt", ""),
                principal=principal, agent_role=session.get("agent_role", "general"),
            )
        if status == "one":
            candidates = prepared.get("candidates") or []
            if len(candidates) != 1:
                raise RuntimeError("Project Core returned an invalid single candidate")
            if tracking_mode == "suggest":
                return {
                    "project_core": {
                        "registration_status": "suggested",
                        "preparation_id": prepared.get("preparation_id", ""),
                        "expires_at": prepared.get("expires_at", ""),
                        "candidates": candidates,
                    },
                    "initial_prompt": session.get("initial_prompt", ""),
                }
            registered = _signed_post(
                discovery["agent_sessions_register_url"],
                {
                    "preparation_id": prepared["preparation_id"],
                    "candidate_id": candidates[0]["candidate_id"],
                },
                secret=discovery["integration_secrets"]["agent"],
            )
            return _registered_result(
                registered, data_dir=data_dir,
                existing_prompt=session.get("initial_prompt", ""),
                principal=principal, agent_role=session.get("agent_role", "general"),
            )
        if status in {"registered", "already_registered"}:
            return _registered_result(
                prepared, data_dir=data_dir,
                existing_prompt=session.get("initial_prompt", ""),
                principal=principal, agent_role=session.get("agent_role", "general"),
            )
        if status == "ambiguous":
            return {
                "project_core": {
                    "registration_status": "ambiguous",
                    "preparation_id": prepared.get("preparation_id", ""),
                    "expires_at": prepared.get("expires_at", ""),
                    "candidates": prepared.get("candidates") or [],
                },
                "initial_prompt": session.get("initial_prompt", ""),
            }
        if status == "no_match":
            if selected_target:
                return _unresolved_selected_target(
                    session, prepared=prepared, selected_target=selected_target,
                )
            return {
                "project_core": {
                    "registration_status": "unassigned",
                    "preparation_id": prepared.get("preparation_id", ""),
                    "expires_at": prepared.get("expires_at", ""),
                },
                "initial_prompt": session.get("initial_prompt", ""),
            }
        raise RuntimeError("Project Core returned an unknown preparation status")
    except (KeyError, OSError, ValueError, RuntimeError, urllib.error.URLError):
        return {
            "project_core": {
                "registration_status": "unavailable",
                **(_selected_target_metadata(selected_target) if selected_target else {}),
            },
            "initial_prompt": session.get("initial_prompt", ""),
        }


def _selected_target_metadata(target: dict[str, str] | None) -> dict[str, str]:
    if not target:
        return {}
    return {
        "desired_project_id": str(target.get("project_id") or ""),
        "desired_project_title": str(target.get("project_title") or ""),
        "desired_record_id": str(target.get("record_id") or ""),
        "desired_workstream_title": str(target.get("workstream_title") or ""),
    }


def _unresolved_selected_target(
    session: dict[str, Any], *, prepared: dict[str, Any],
    selected_target: dict[str, str],
) -> dict[str, Any]:
    return {
        "project_core": {
            "registration_status": "target_unavailable",
            "preparation_id": str(prepared.get("preparation_id") or ""),
            "expires_at": str(prepared.get("expires_at") or ""),
            **_selected_target_metadata(selected_target),
        },
        "initial_prompt": session.get("initial_prompt", ""),
    }


def register_prepared_session(
    session: dict[str, Any],
    *,
    candidate_id: str | None,
    runtime_file: Path,
    data_dir: Path,
) -> dict[str, Any]:
    """Complete a user-approved suggestion without re-resolving its cwd."""

    try:
        pending_metadata = json.loads(session.get("project_core_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        pending_metadata = {}
    if not isinstance(pending_metadata, dict):
        pending_metadata = {}
    try:
        metadata = pending_metadata
        if not metadata:
            raise ValueError("session Project Core metadata is invalid")
        if metadata.get("registration_status") not in {"suggested", "ambiguous"}:
            raise ValueError("session has no pending Project Core suggestion")
        candidates = metadata.get("candidates") or []
        if candidate_id is None:
            if len(candidates) != 1:
                raise ValueError("candidate_id is required for multiple suggestions")
            candidate_id = candidates[0].get("candidate_id")
        if not any(item.get("candidate_id") == candidate_id for item in candidates):
            raise ValueError("candidate_id is not part of this session suggestion")
        discovery = _read_discovery(runtime_file)
        registered = _signed_post(
            discovery["agent_sessions_register_url"],
            {
                "preparation_id": metadata["preparation_id"],
                "candidate_id": candidate_id,
            },
            secret=discovery["integration_secrets"]["agent"],
        )
        return _registered_result(
            registered, data_dir=data_dir,
            existing_prompt=session.get("initial_prompt", ""),
            principal=discovery["integration_principals"]["agent"],
            agent_role=session.get("agent_role", "general"),
        )
    except (KeyError, OSError, ValueError, RuntimeError, urllib.error.URLError):
        return {
            "project_core": {
                **pending_metadata,
                "registration_status": "unavailable",
            },
            "initial_prompt": session.get("initial_prompt", ""),
        }


def _registered_result(
    response: dict[str, Any], *, data_dir: Path, existing_prompt: str = "",
    principal: dict[str, Any] | None = None, agent_role: str = "general",
) -> dict[str, Any]:
    if response.get("status") not in {"registered", "already_registered"}:
        raise RuntimeError("Project Core did not register the session")
    association = response.get("association")
    context_pack = response.get("context_pack")
    if not isinstance(association, dict) or not isinstance(context_pack, dict):
        raise RuntimeError("Project Core registration response is incomplete")
    content = context_pack.get("content")
    if not isinstance(content, dict):
        raise RuntimeError("Project Core registration omitted Context Pack content")
    digest = hashlib.sha256(
        json.dumps(content, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    if digest != context_pack.get("sha256"):
        raise RuntimeError("Project Core Context Pack hash does not match its content")

    association_id = str(association.get("id") or "")
    if not association_id.startswith("asoc_") or not association_id[5:].isalnum():
        raise RuntimeError("Project Core association ID is invalid")
    handoff_dir = data_dir / "project_core_handoffs"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(handoff_dir, 0o700)
    except OSError:
        pass
    handoff_path = handoff_dir / f"{association_id}.json"
    temporary = handoff_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(handoff_path)

    instruction = _context_bootstrap(
        content, association=association, handoff_path=handoff_path,
        agent_role=agent_role,
    )
    prompt = f"{existing_prompt}\n\n{instruction}".strip()
    return {
        "project_core": {
            "registration_status": "registered",
            "association_id": association_id,
            "association_segment": int(association.get("association_segment") or 1),
            "project_id": association["project_ref"],
            "project_title": str(
                association.get("project_title") or association["project_ref"]
            ),
            "record_id": association["workstream_ref"],
            "workstream_title": str(
                association.get("workstream_title") or association["workstream_ref"]
            ),
            "context_pack_id": association["context_pack_id"],
            "context_pack_sha256": association["context_pack_sha256"],
            "correlation_id": association["correlation_id"],
            "maximum_visibility": association["maximum_visibility"],
            "provider": association["provider"],
            "provider_instance": association["provider_instance"],
            "principal_external_id": str((principal or {}).get("external_id") or ""),
            "principal_kind": str((principal or {}).get("kind") or ""),
            "seat_role": agent_role,
            "handoff_path": str(handoff_path),
        },
        "initial_prompt": prompt,
    }


def adopt_handoff_session(
    session: dict[str, Any], *, runtime_file: Path, data_dir: Path,
) -> dict[str, Any]:
    """Turn a Project Core-origin handoff into the same durable association."""
    metadata = session.get("project_core")
    if not isinstance(metadata, dict):
        try:
            metadata = json.loads(session.get("project_core_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
    try:
        discovery = _read_discovery(runtime_file)
        url = discovery["agent_sessions_adopt_url"]
        principal = discovery["integration_principals"]["agent"]
        response = _signed_post(
            url,
            {
                "provider": principal["provider"],
                "provider_instance": principal["provider_instance"],
                "external_session_id": session["id"],
                "project_ref": metadata["project_id"],
                "workstream_ref": metadata["record_id"],
                "correlation_id": metadata["correlation_id"],
                "context_pack_id": metadata["context_pack_id"],
                "context_pack_sha256": metadata["context_pack_sha256"],
            },
            secret=discovery["integration_secrets"]["agent"],
        )
        return _registered_result(
            response, data_dir=data_dir,
            existing_prompt=session.get("initial_prompt", ""), principal=principal,
            agent_role=session.get("agent_role", "general"),
        )
    except (KeyError, OSError, ValueError, RuntimeError, urllib.error.URLError):
        return {
            "project_core": {**metadata, "registration_status": "unavailable"},
            "initial_prompt": session.get("initial_prompt", ""),
        }


def deliver_event(event: dict[str, Any], *, runtime_file: Path) -> dict[str, Any]:
    """Deliver one already-canonical outbox envelope without changing it."""
    discovery = _read_discovery(runtime_file)
    url = discovery.get("event_url")
    if not isinstance(url, str) or not url:
        raise RuntimeError("Project Core discovery does not publish an event endpoint")
    return _signed_post(
        url, event,
        secret=discovery["integration_secrets"]["agent"],
    )


def _context_bootstrap(
    content: dict[str, Any], *, association: dict[str, Any],
    handoff_path: Path, agent_role: str,
) -> str:
    """Small deterministic prompt; the complete immutable pack stays on disk."""
    focus = content.get("focus") if isinstance(content.get("focus"), dict) else {}
    payload = focus.get("payload") if isinstance(focus.get("payload"), dict) else {}
    project = content.get("project") if isinstance(content.get("project"), dict) else {}
    project_title = str(
        association.get("project_title")
        or project.get("title")
        or association.get("project_ref")
        or "Project"
    )
    workstream_title = str(
        association.get("workstream_title")
        or focus.get("title")
        or association.get("workstream_ref")
        or "Workstream"
    )
    current = next((
        str(payload[key]).strip() for key in (
            "current_state", "summary", "goal", "outcome", "status",
        ) if payload.get(key)
    ), "")

    def short_items(field: str) -> list[str]:
        value = payload.get(field)
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:180] for item in value[:3] if str(item).strip()]

    next_steps = short_items("next_steps")
    blockers = short_items("blockers")
    lines = [
        "[PROJECT_CORE_CONTEXT_BOOTSTRAP_V1]",
        f"Work target: {project_title} > {workstream_title}",
        f"Seat role: {_ROLE_LABELS.get(agent_role, 'general')}",
    ]
    if current:
        lines.append(f"Current state/gap: {current[:500]}")
    if next_steps:
        lines.append("Current next steps: " + "; ".join(next_steps))
    if blockers:
        lines.append("Current blockers: " + "; ".join(blockers))
    lines.extend([
        "Read the exact immutable Context Pack before substantive work:",
        str(handoff_path),
        "Treat conclusions as provisional until human review. The short bootstrap may "
        "omit context; the handoff file and its hash are authoritative.",
    ])
    return "\n".join(lines)


def _read_discovery(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    stat = resolved.stat()
    if stat.st_size > _MAX_DISCOVERY_BYTES:
        raise ValueError("Project Core discovery file is too large")
    if stat.st_mode & 0o077:
        raise PermissionError("Project Core discovery file must not be group/world readable")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise ValueError("unsupported Project Core discovery schema")
    for key in ("agent_sessions_prepare_url", "agent_sessions_register_url"):
        _loopback_url(value.get(key, ""))
    for key in ("agent_targets_url", "agent_sessions_adopt_url", "event_url"):
        if key in value:
            _loopback_url(value.get(key, ""))
    return value


def _signed_post(url: str, payload: dict[str, Any], *, secret: str) -> dict[str, Any]:
    _loopback_url(url)
    if not isinstance(secret, str) or len(secret) < 20:
        raise ValueError("Project Core agent credential is invalid")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    timestamp = str(time.time())
    nonce = uuid.uuid4().hex
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + nonce.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-PC-Producer": "agent",
            "X-PC-Timestamp": timestamp,
            "X-PC-Nonce": nonce,
            "X-PC-Signature": signature,
        },
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise RuntimeError("Project Core response is too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("Project Core response must be a JSON object")
    return value


def _loopback_url(value: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        raise ValueError("Project Core registration URL must be loopback HTTP")
