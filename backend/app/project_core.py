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
_MANUAL_FIELDS = {
    "id", "schema_version", "manual_version", "index", "index_sha256",
    "documents", "sha256",
}
_STARTUP_FIELDS = {
    "id", "schema", "schema_version", "target", "seat_role", "source",
    "assignment", "brief_snapshot", "context_pack", "agent_manual",
    "policies", "created_at", "sha256",
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


def fetch_agent_manual(*, runtime_file: Path) -> dict[str, Any]:
    """Fetch and independently verify Project Core's current handbook release."""
    discovery = _read_discovery(runtime_file)
    response = _signed_post(
        discovery["agent_manual_url"], {},
        secret=discovery["integration_secrets"]["agent"],
    )
    return _validate_agent_manual_bundle(response)


def render_agent_startup(
    *,
    runtime_file: Path,
    association_id: str,
    startup_sha256: str,
    startup_bundle_path: Path | str,
    handoff_path: Path | str,
    manual_index_path: Path | str,
    delivery_mode: str = "initial",
) -> str:
    """Ask Project Core—not the transport runtime—to author the bootstrap."""
    discovery = _read_discovery(runtime_file)
    response = _signed_post(
        discovery["agent_startup_render_url"],
        {
            "association_id": association_id,
            "startup_sha256": startup_sha256,
            "startup_bundle_path": str(startup_bundle_path),
            "handoff_path": str(handoff_path),
            "manual_index_path": str(manual_index_path),
            "delivery_mode": delivery_mode,
        },
        secret=discovery["integration_secrets"]["agent"],
    )
    bootstrap = response.get("bootstrap")
    if (
        response.get("association_id") != association_id
        or response.get("startup_sha256") != startup_sha256
        or response.get("delivery_mode") != delivery_mode
        or not isinstance(bootstrap, str) or not bootstrap.strip()
        or len(bootstrap) > 20_000 or "\x00" in bootstrap
    ):
        raise RuntimeError("Project Core Agent startup rendering is invalid")
    return bootstrap.strip()


def preview_agent_startup(
    *,
    runtime_file: Path,
    project_ref: str,
    workstream_ref: str,
    startup_context: dict[str, Any],
) -> dict[str, Any]:
    discovery = _read_discovery(runtime_file)
    response = _signed_post(
        discovery["agent_startup_preview_url"],
        {
            "project_ref": project_ref,
            "workstream_ref": workstream_ref,
            "startup_context": startup_context,
        },
        secret=discovery["integration_secrets"]["agent"],
    )
    if response.get("preview") is not True:
        raise RuntimeError("Project Core Agent startup preview is invalid")
    bootstrap = response.get("bootstrap")
    bundle = _validate_agent_startup_bundle(response.get("startup_bundle"))
    if not isinstance(bootstrap, str) or not bootstrap.strip():
        raise RuntimeError("Project Core Agent startup preview omitted its bootstrap")
    return {"bootstrap": bootstrap.strip(), "startup_bundle": bundle}


def auto_register_session(
    session: dict[str, Any],
    *,
    runtime_file: Path,
    data_dir: Path,
    tracking_mode: str = "suggest",
    association_segment: int = 1,
    selected_target: dict[str, str] | None = None,
    startup_context: dict[str, Any] | None = None,
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
                    startup_context=startup_context,
                )
            registered = _signed_post(
                discovery["agent_sessions_register_url"],
                {
                    "preparation_id": prepared["preparation_id"],
                    "candidate_id": matches[0]["candidate_id"],
                    **({"startup_context": startup_context} if startup_context else {}),
                },
                secret=discovery["integration_secrets"]["agent"],
            )
            return _registered_result(
                registered, data_dir=data_dir,
                existing_prompt=session.get("initial_prompt", ""),
                principal=principal, agent_role=session.get("agent_role", "general"),
                runtime_file=runtime_file,
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
                        **_startup_metadata(startup_context),
                    },
                    "initial_prompt": session.get("initial_prompt", ""),
                }
            registered = _signed_post(
                discovery["agent_sessions_register_url"],
                {
                    "preparation_id": prepared["preparation_id"],
                    "candidate_id": candidates[0]["candidate_id"],
                    **({"startup_context": startup_context} if startup_context else {}),
                },
                secret=discovery["integration_secrets"]["agent"],
            )
            return _registered_result(
                registered, data_dir=data_dir,
                existing_prompt=session.get("initial_prompt", ""),
                principal=principal, agent_role=session.get("agent_role", "general"),
                runtime_file=runtime_file,
            )
        if status in {"registered", "already_registered"}:
            return _registered_result(
                prepared, data_dir=data_dir,
                existing_prompt=session.get("initial_prompt", ""),
                principal=principal, agent_role=session.get("agent_role", "general"),
                runtime_file=runtime_file,
            )
        if status == "ambiguous":
            return {
                "project_core": {
                    "registration_status": "ambiguous",
                    "preparation_id": prepared.get("preparation_id", ""),
                    "expires_at": prepared.get("expires_at", ""),
                    "candidates": prepared.get("candidates") or [],
                    **_startup_metadata(startup_context),
                },
                "initial_prompt": session.get("initial_prompt", ""),
            }
        if status == "no_match":
            if selected_target:
                return _unresolved_selected_target(
                    session, prepared=prepared, selected_target=selected_target,
                    startup_context=startup_context,
                )
            return {
                "project_core": {
                    "registration_status": "unassigned",
                    "preparation_id": prepared.get("preparation_id", ""),
                    "expires_at": prepared.get("expires_at", ""),
                    **_startup_metadata(startup_context),
                },
                "initial_prompt": session.get("initial_prompt", ""),
            }
        raise RuntimeError("Project Core returned an unknown preparation status")
    except (KeyError, OSError, ValueError, RuntimeError, urllib.error.URLError):
        return {
            "project_core": {
                "registration_status": "unavailable",
                **(_selected_target_metadata(selected_target) if selected_target else {}),
                **_startup_metadata(startup_context),
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


def _startup_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    return {"startup_context": value} if isinstance(value, dict) else {}


def _unresolved_selected_target(
    session: dict[str, Any], *, prepared: dict[str, Any],
    selected_target: dict[str, str],
    startup_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "project_core": {
            "registration_status": "target_unavailable",
            "preparation_id": str(prepared.get("preparation_id") or ""),
            "expires_at": str(prepared.get("expires_at") or ""),
            **_selected_target_metadata(selected_target),
            **_startup_metadata(startup_context),
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
                **({
                    "startup_context": metadata["startup_context"],
                } if isinstance(metadata.get("startup_context"), dict) else {}),
            },
            secret=discovery["integration_secrets"]["agent"],
        )
        return _registered_result(
            registered, data_dir=data_dir,
            existing_prompt=session.get("initial_prompt", ""),
            principal=discovery["integration_principals"]["agent"],
            agent_role=session.get("agent_role", "general"),
            runtime_file=runtime_file,
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
    runtime_file: Path | None = None,
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

    manual_value = response.get("agent_manual")
    manual = _validate_agent_manual_bundle(manual_value) if manual_value is not None else None
    startup_value = response.get("agent_startup")
    startup = _validate_agent_startup_bundle(startup_value) if startup_value is not None else None

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

    manual_index_path: Path | None = None
    manual_guardrails: list[str] | None = None
    if manual is not None:
        manual_index_path = _write_manual_bundle(manual, data_dir=data_dir)
        manual_guardrails = list(manual["index"]["bootstrap_guardrails"])

    brief_path: Path | None = None
    startup_path: Path | None = None
    if startup is not None:
        if manual is None or runtime_file is None:
            raise RuntimeError("modern Project Core startup requires its manual and renderer")
        if (
            startup["target"]["association_id"] != association_id
            or startup["target"]["project_ref"] != association.get("project_ref")
            or startup["target"]["workstream_ref"] != association.get("workstream_ref")
            or startup["context_pack"]["id"] != context_pack.get("id")
            or startup["context_pack"]["sha256"] != context_pack.get("sha256")
            or startup["agent_manual"]["id"] != manual["id"]
            or startup["agent_manual"]["sha256"] != manual["sha256"]
            or startup["agent_manual"]["index_sha256"] != manual["index_sha256"]
        ):
            raise RuntimeError("Project Core Agent startup dependencies do not match")
        startup_dir = data_dir / "project_core_startups"
        startup_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(startup_dir, 0o700)
        except OSError:
            pass
        startup_path = startup_dir / f"{startup['id']}.json"
        _atomic_private_write(
            startup_path,
            json.dumps(startup, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    else:
        # Compatibility only for a pre-startup-bundle Project Core. Modern
        # tracked seats never let Agent Hub read or reinterpret Project Brief.
        brief = _project_brief(str(association.get("project_ref") or ""))
        if brief:
            brief_dir = data_dir / "project_core_briefs"
            brief_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(brief_dir, 0o700)
            except OSError:
                pass
            brief_path = brief_dir / f"{association_id}.md"
            _atomic_private_write(brief_path, brief.rstrip() + "\n")

    if startup is not None:
        instruction = render_agent_startup(
            runtime_file=runtime_file,
            association_id=association_id,
            startup_sha256=startup["sha256"],
            startup_bundle_path=startup_path,
            handoff_path=handoff_path,
            manual_index_path=manual_index_path,
        )
        # A modern tracked seat has one opening author: Project Core. Any
        # assignment supplied by Brief/Hub is already inside startup_context.
        prompt = instruction
    else:
        instruction = _context_bootstrap(
            content, association=association, handoff_path=handoff_path,
            agent_role=agent_role, manual_index_path=manual_index_path,
            manual_index_sha256=(manual or {}).get("index_sha256"),
            manual_guardrails=manual_guardrails, brief_path=brief_path,
        )
        prompt = f"{existing_prompt}\n\n{instruction}".strip()
    return {
        "project_core": {
            "registration_status": "registered",
            "association_id": association_id,
            "association_segment": int(association.get("association_segment") or 1),
            "resource_binding_id": str(association.get("resource_binding_id") or ""),
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
            **({
                "agent_manual_id": manual["id"],
                "agent_manual_version": manual["manual_version"],
                "agent_manual_sha256": manual["sha256"],
                "manual_index_path": str(manual_index_path),
                "manual_index_sha256": manual["index_sha256"],
            } if manual is not None else {}),
            **({"brief_path": str(brief_path)} if brief_path is not None else {}),
            **({
                "agent_startup_id": startup["id"],
                "agent_startup_sha256": startup["sha256"],
                "startup_bundle_path": str(startup_path),
                "seat_role": startup["seat_role"],
                "opening_mode": startup["assignment"]["mode"],
                "startup_source": startup["source"],
            } if startup is not None else {}),
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
            runtime_file=runtime_file,
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


def _brief_index_path() -> Path:
    """Same location convention the brief tool uses, override included."""
    root = os.environ.get("PROJECT_BRIEF_HOME")
    base = Path(root).expanduser() if root else Path.home() / ".local" / "state" / "project-brief"
    return base / "index.json"

_BRIEF_SECTIONS = ("现在在做", "待决 · 卡住", "下一步")
_BRIEF_MAX_CHARS = 4000
_PINNED_BEGIN = "<!-- pinned:begin -->"
_PINNED_END = "<!-- pinned:end -->"


def _brief_sections(text: str) -> dict[str, str]:
    body = text.split(_PINNED_BEGIN)[0]
    found: dict[str, str] = {}
    title: str | None = None
    buffer: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if title is not None:
                found[title] = "\n".join(buffer).strip()
            title = line[3:].strip()
            buffer = []
        elif title is not None:
            buffer.append(line)
    if title is not None:
        found[title] = "\n".join(buffer).strip()
    return found


def _project_brief(project_ref: str) -> str:
    """The Project's reviewed state summary, if the brief tool has one.

    Deliberately mutable and read at seat-creation time, so a seat starts from
    the newest approved state rather than whatever a Workstream record happened
    to say when someone last edited it.  It is not part of the immutable Context
    Pack and is never authoritative; failures here only cost the seat a hint.
    """
    if not project_ref:
        return ""
    try:
        index = json.loads(_brief_index_path().read_text(encoding="utf-8"))
        entry = (index.get("projects") or {}).get(project_ref)
        if not isinstance(entry, dict):
            return ""
        path = Path(str(entry.get("brief_path") or ""))
        if not path.is_file() or path.stat().st_size > 200_000:
            return ""
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    sections = _brief_sections(text)
    parts: list[str] = []
    for name in _BRIEF_SECTIONS:
        body = sections.get(name, "").strip()
        if body:
            parts.append(f"## {name}\n{body}")
    if _PINNED_BEGIN in text and _PINNED_END in text:
        pinned = text[
            text.index(_PINNED_BEGIN) + len(_PINNED_BEGIN):text.index(_PINNED_END)
        ].strip()
        if pinned:
            parts.append(pinned)
    if not parts:
        return ""
    rendered = "\n\n".join(parts)
    if len(rendered) > _BRIEF_MAX_CHARS:
        rendered = rendered[:_BRIEF_MAX_CHARS].rstrip() + "\n…（简报已截断，全文见 brief show）"
    return rendered


def _context_bootstrap(
    content: dict[str, Any], *, association: dict[str, Any],
    handoff_path: Path | str, agent_role: str,
    manual_index_path: Path | str | None = None,
    manual_index_sha256: str | None = None,
    manual_guardrails: list[str] | None = None,
    brief_path: Path | str | None = None,
) -> str:
    """Small deterministic prompt; complete context and tool rules stay on disk."""
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
    if manual_index_path is not None:
        lines = [
            "[PROJECT_CORE_AGENT_BOOTSTRAP_V2]",
            f"Work target: {project_title} > {workstream_title}",
            f"Seat role: {_ROLE_LABELS.get(agent_role, 'general')}",
            f"Exact handoff: {handoff_path}",
            (
                f"Agent manual index: {manual_index_path} "
                f"(canonical sha256 {manual_index_sha256})"
            ),
        ]
        if brief_path is not None:
            lines.append(
                f"Mutable Project state summary: {brief_path} "
                "(orientation only; prefer observed evidence on disagreement)"
            )
        lines.append(
            "Read the exact handoff and the manual index before substantive work; "
            "then read only the tool cards relevant to the task."
        )
        lines.extend(manual_guardrails or [])
        return "\n".join(lines)

    # Compatibility path for a pre-handbook Project Core runtime. It remains
    # deliberately isolated from the v2 path and can be removed after all live
    # gateways publish a pinned manual release.
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
    brief = _project_brief(str(association.get("project_ref") or ""))
    if brief:
        lines.extend([
            "",
            "Project state summary (mutable working memory, drafted from earlier "
            "sessions and approved by the owner; prefer evidence you observe "
            "yourself and say so when they disagree):",
            brief,
            "",
        ])
    lines.extend([
        "Read the exact immutable Context Pack before substantive work:",
        str(handoff_path),
        "Treat conclusions as provisional until human review. The short bootstrap may "
        "omit context; the handoff file and its hash are authoritative.",
        "Context is not execution authorization. Follow the opening request's execution "
        "mode; if it says context-only or assigns no task, do not call tools or change "
        "anything and wait for the user's next message.",
    ])
    return "\n".join(lines)


PREVIEW_HANDOFF_PATH = "(the Context Pack is written when the seat is registered)"


def context_bootstrap_preview(
    content: dict[str, Any], *, association: dict[str, Any], agent_role: str,
    manual: dict[str, Any] | None = None,
) -> str:
    """The seat prompt as it would be, for a seat that has not been created.

    Same composer as the real thing on purpose: a preview that is assembled
    separately is a second definition, and the one thing a preview must not do
    is disagree with what actually gets injected.
    """
    checked = _validate_agent_manual_bundle(manual) if manual is not None else None
    return _context_bootstrap(
        content, association=association,
        handoff_path=PREVIEW_HANDOFF_PATH, agent_role=agent_role,
        manual_index_path=(
            "(the Agent Manual index is written when the seat is registered)"
            if checked is not None else None
        ),
        manual_index_sha256=(checked or {}).get("index_sha256"),
        manual_guardrails=(
            list(checked["index"]["bootstrap_guardrails"])
            if checked is not None else None
        ),
    )


def _validate_agent_manual_bundle(value: Any) -> dict[str, Any]:
    """Verify Project Core's handbook without importing Project Core code."""
    if not isinstance(value, dict) or set(value) != _MANUAL_FIELDS:
        raise RuntimeError("Project Core Agent Manual bundle fields are invalid")
    if value.get("schema_version") != 1:
        raise RuntimeError("Project Core Agent Manual bundle schema is unsupported")
    index = value.get("index")
    documents = value.get("documents")
    if not isinstance(index, dict) or not isinstance(documents, dict):
        raise RuntimeError("Project Core Agent Manual bundle is incomplete")
    if _json_digest(index) != value.get("index_sha256"):
        raise RuntimeError("Project Core Agent Manual index hash does not match")
    tools = index.get("tools")
    guardrails = index.get("bootstrap_guardrails")
    if not isinstance(tools, list) or not tools or not isinstance(guardrails, list):
        raise RuntimeError("Project Core Agent Manual index is incomplete")
    if any(not isinstance(item, str) or not item.strip() for item in guardrails):
        raise RuntimeError("Project Core Agent Manual guardrail is invalid")
    seen: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            raise RuntimeError("Project Core Agent Manual tool entry is invalid")
        tool_id = tool.get("id")
        path = tool.get("document_path")
        if not isinstance(tool_id, str) or not tool_id or tool_id in seen:
            raise RuntimeError("Project Core Agent Manual tool identity is invalid")
        if (
            not isinstance(path, str) or path.startswith("/") or ".." in Path(path).parts
            or path not in documents or not isinstance(documents[path], str)
        ):
            raise RuntimeError("Project Core Agent Manual document path is invalid")
        if _json_digest(documents[path]) != tool.get("sha256"):
            raise RuntimeError("Project Core Agent Manual document hash does not match")
        seen.add(tool_id)
    unsigned = {
        key: value[key] for key in (
            "schema_version", "manual_version", "index", "index_sha256", "documents",
        )
    }
    digest = _json_digest(unsigned)
    if value.get("sha256") != digest or value.get("id") != f"manual_{digest[:24]}":
        raise RuntimeError("Project Core Agent Manual bundle hash does not match")
    return json.loads(json.dumps(value))


def _validate_agent_startup_bundle(value: Any) -> dict[str, Any]:
    """Verify the Core-authored startup envelope without importing Core."""
    if not isinstance(value, dict) or set(value) != _STARTUP_FIELDS:
        raise RuntimeError("Project Core Agent startup bundle fields are invalid")
    if (
        value.get("schema") != "project-core.agent-startup-bundle/v1"
        or value.get("schema_version") != 1
    ):
        raise RuntimeError("Project Core Agent startup bundle schema is unsupported")
    target = value.get("target")
    assignment = value.get("assignment")
    context = value.get("context_pack")
    manual = value.get("agent_manual")
    policies = value.get("policies")
    if (
        not isinstance(target, dict)
        or set(target) != {
            "association_id", "project_ref", "project_title",
            "workstream_ref", "workstream_title",
        }
        or not all(isinstance(target.get(key), str) and target[key] for key in target)
        or value.get("seat_role") not in {"general", "plan", "implement", "review"}
        or not isinstance(value.get("source"), str) or not value["source"]
        or not isinstance(assignment, dict) or set(assignment) != {"mode", "task"}
        or assignment.get("mode") not in {"context_only", "execute"}
        or not isinstance(assignment.get("task"), str)
        or (assignment.get("mode") == "execute" and not assignment["task"])
        or not isinstance(context, dict) or set(context) != {"id", "sha256"}
        or not isinstance(manual, dict)
        or set(manual) != {"id", "manual_version", "sha256", "index_sha256"}
        or policies != {
            "scientific_conclusions": "provisional_until_human_review",
            "context_is_execution_authority": False,
            "checkpoint_requires_explicit_user_opt_in": True,
        }
        or not isinstance(value.get("created_at"), str) or not value["created_at"]
    ):
        raise RuntimeError("Project Core Agent startup bundle is invalid")
    for digest in (
        context.get("sha256"), manual.get("sha256"), manual.get("index_sha256"),
    ):
        if not isinstance(digest, str) or len(digest) != 64:
            raise RuntimeError("Project Core Agent startup dependency hash is invalid")
    brief = value.get("brief_snapshot")
    if brief is not None:
        if (
            not isinstance(brief, dict)
            or set(brief) != {
                "schema", "project_ref", "accepted_at", "content", "sha256",
            }
            or brief.get("schema") != "project-core.project-brief-snapshot/v1"
            or brief.get("project_ref") != target["project_ref"]
            or not isinstance(brief.get("content"), str)
            or _json_digest(brief["content"]) != brief.get("sha256")
        ):
            raise RuntimeError("Project Core Project Brief snapshot is invalid")
    unsigned = {key: value[key] for key in (
        "schema", "schema_version", "target", "seat_role", "source",
        "assignment", "brief_snapshot", "context_pack", "agent_manual",
        "policies", "created_at",
    )}
    digest = _json_digest(unsigned)
    if value.get("sha256") != digest or value.get("id") != f"startup_{digest[:24]}":
        raise RuntimeError("Project Core Agent startup bundle hash does not match")
    return json.loads(json.dumps(value))


def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _atomic_private_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _write_manual_bundle(manual: dict[str, Any], *, data_dir: Path) -> Path:
    release_dir = data_dir / "project_core_manuals" / manual["id"]
    release_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(release_dir.parent, 0o700)
        os.chmod(release_dir, 0o700)
    except OSError:
        pass
    index_path = release_dir / "index.json"
    _atomic_private_write(
        index_path,
        json.dumps(manual["index"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    for relative, document in manual["documents"].items():
        target = release_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            pass
        _atomic_private_write(target, document)
    _atomic_private_write(
        release_dir / "bundle.json",
        json.dumps(manual, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return index_path


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
    for key in (
        "agent_targets_url", "agent_sessions_adopt_url", "agent_manual_url",
        "agent_startup_render_url", "agent_startup_preview_url", "event_url",
    ):
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
