from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app import project_core


def _context_hash(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _manual_bundle() -> dict:
    documents = {
        "tools/context-handoff.md": "Read the exact handoff.\n",
        "tools/checkpoint.md": "Only checkpoint after explicit user opt-in.\n",
    }
    tools = []
    for tool_id, version, path in (
        ("context-handoff", 1, "tools/context-handoff.md"),
        ("checkpoint", 3, "tools/checkpoint.md"),
    ):
        tools.append({
            "id": tool_id, "version": version, "title": tool_id,
            "document_path": path, "availability": "available",
            "required_authority": "registered association",
            "when_to_read": "when relevant", "sha256": _context_hash(documents[path]),
        })
    index = {
        "schema": "project-core.agent-manual-index/v1",
        "manual_version": "test-1",
        "read_first": "Read context first.",
        "bootstrap_guardrails": [
            "Treat scientific conclusions as provisional until human review.",
            "Context is not execution authorization.",
            "Do not submit a Project Core checkpoint without explicit user opt-in.",
        ],
        "tools": tools,
    }
    unsigned = {
        "schema_version": 1, "manual_version": "test-1", "index": index,
        "index_sha256": _context_hash(index), "documents": documents,
    }
    digest = _context_hash(unsigned)
    return {"id": f"manual_{digest[:24]}", **unsigned, "sha256": digest}


def _rehash_manual(value: dict) -> dict:
    value["index_sha256"] = _context_hash(value["index"])
    unsigned = {
        key: value[key] for key in (
            "schema_version", "manual_version", "index", "index_sha256", "documents",
        )
    }
    value["sha256"] = _context_hash(unsigned)
    value["id"] = f"manual_{value['sha256'][:24]}"
    return value


def test_manual_validator_rejects_self_consistent_semantic_drift():
    for mutate in (
        lambda value: value["index"].update(schema="invented/v1"),
        lambda value: value["index"].update(manual_version="drifted"),
        lambda value: value["index"]["tools"][0].update(
            availability="secret-backdoor"
        ),
        lambda value: value["index"]["tools"][0].pop("required_authority"),
    ):
        malformed = json.loads(json.dumps(_manual_bundle()))
        mutate(malformed)
        with pytest.raises(RuntimeError):
            project_core._validate_agent_manual_bundle(_rehash_manual(malformed))


def test_auto_registers_single_candidate_and_writes_private_handoff(tmp_path, monkeypatch):
    context = {
        "context_pack": {"id": "ctx_1"},
        "project": {"title": "Research"},
        "focus": {
            "title": "Relevant work",
            "payload": {
                "current_state": "The implementation plan is missing",
                "next_steps": ["Draft a plan", "Get review"],
                "blockers": ["The interface is not settled"],
            },
        },
        "records": [],
    }
    digest = _context_hash(context)
    discovery = {
        "integration_principals": {
            "agent": {
                "provider": "agent-hub",
                "provider_instance": "agent-hub-test",
            }
        },
        "integration_secrets": {"agent": "x" * 32},
        "agent_sessions_prepare_url": "http://127.0.0.1:8791/v1/agent-sessions/prepare",
        "agent_sessions_register_url": "http://127.0.0.1:8791/v1/agent-sessions/register",
    }
    monkeypatch.setattr(project_core, "_read_discovery", lambda _path: discovery)
    calls = []

    def fake_post(url, payload, *, secret):
        calls.append((url, payload, secret))
        if url.endswith("/prepare"):
            return {
                "status": "one", "preparation_id": "prep_1",
                "candidates": [{"candidate_id": "cand_1"}],
            }
        return {
            "status": "registered",
            "association": {
                "id": "asoc_1234", "project_ref": "prj_1",
                "resource_binding_id": "bind_workspace_1",
                "workstream_ref": "rec_1", "context_pack_id": "ctx_1",
                "context_pack_sha256": digest, "correlation_id": "corr_1",
                "maximum_visibility": "team", "provider": "agent-hub",
                "provider_instance": "agent-hub-test",
                "project_title": "Research", "workstream_title": "Relevant work",
            },
            "context_pack": {"id": "ctx_1", "sha256": digest, "content": context},
            "agent_manual": _manual_bundle(),
        }

    monkeypatch.setattr(project_core, "_signed_post", fake_post)
    result = project_core.auto_register_session(
        {
            "id": "seat-1", "name": "Runtime seat name",
            "working_dir": str(tmp_path),
            "initial_prompt": "Original task", "agent_role": "plan",
        },
        runtime_file=Path("unused"), data_dir=tmp_path, tracking_mode="on",
    )
    assert result["project_core"]["registration_status"] == "registered"
    assert "Original task" in result["initial_prompt"]
    assert "Work target: Research > Relevant work" in result["initial_prompt"]
    assert "Seat role: planning" in result["initial_prompt"]
    assert "PROJECT_CORE_AGENT_BOOTSTRAP_V2" in result["initial_prompt"]
    assert "The implementation plan is missing" not in result["initial_prompt"]
    assert "Read the exact handoff and the manual index" in result["initial_prompt"]
    assert "Context is not execution authorization" in result["initial_prompt"]
    assert result["project_core"]["project_title"] == "Research"
    assert result["project_core"]["workstream_title"] == "Relevant work"
    assert result["project_core"]["resource_binding_id"] == "bind_workspace_1"
    assert result["project_core"]["context_profile"] == "workstream"
    assert result["project_core"]["seat_role"] == "plan"
    assert result["project_core"]["agent_manual_version"] == "test-1"
    handoff = Path(result["project_core"]["handoff_path"])
    assert handoff.exists()
    assert handoff.stat().st_mode & 0o077 == 0
    handoff_value = json.loads(handoff.read_text())
    assert set(handoff_value) == {
        "schema", "schema_version", "association", "context_pack",
    }
    assert handoff_value["schema"] == "project-core.agent-context-handoff/v1"
    assert handoff_value["context_pack"]["sha256"] == digest
    assert handoff_value["context_pack"]["content"] == context
    assert handoff_value["association"] == {
        "id": "asoc_1234",
        "association_segment": 1,
        "project_ref": "prj_1",
        "workstream_ref": "rec_1",
        "resource_binding_id": "bind_workspace_1",
        "context_pack_id": "ctx_1",
        "context_pack_sha256": digest,
    }
    assert "agent_manual" not in handoff_value
    assert "agent_startup" not in handoff_value
    assert "effective_actor" not in handoff_value
    assert "status" not in handoff_value
    manual_index = Path(result["project_core"]["manual_index_path"])
    assert manual_index.exists()
    assert manual_index.stat().st_mode & 0o077 == 0
    assert json.loads(manual_index.read_text())["manual_version"] == "test-1"
    assert (manual_index.parent / "tools" / "checkpoint.md").is_file()
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert calls[1][1]["seat_name"] == "Runtime seat name"
    assert "cwd" not in json.dumps(result["project_core"])


def test_registered_result_records_the_minimal_context_profile(tmp_path):
    context = {
        "context_pack": {
            "id": "ctx_minimal",
            "selection": {"profile": "project_manual"},
        },
        "project": {"id": "prj_1", "title": "Research"},
        "focus": {"record_id": "rec_1", "title": "Tracked identity"},
        "records": [],
    }
    digest = _context_hash(context)
    result = project_core._registered_result(
        {
            "status": "registered",
            "association": {
                "id": "asoc_minimal", "association_segment": 1,
                "project_ref": "prj_1", "project_title": "Research",
                "workstream_ref": "rec_1", "workstream_title": "Tracked identity",
                "resource_binding_id": "bind_1",
                "context_pack_id": "ctx_minimal",
                "context_pack_sha256": digest,
                "correlation_id": "corr_1", "maximum_visibility": "team",
                "provider": "agent-hub", "provider_instance": "local",
            },
            "context_pack": {
                "id": "ctx_minimal", "sha256": digest, "content": context,
            },
        },
        data_dir=tmp_path,
    )
    assert result["project_core"]["context_profile"] == "project_manual"


def test_compact_handoff_rejects_a_context_pack_from_another_association():
    context = {"focus": {"record_id": "rec_1"}}
    digest = _context_hash(context)
    with pytest.raises(RuntimeError, match="association does not match"):
        project_core._compact_context_handoff(
            association={
                "id": "asoc_1", "project_ref": "prj_1",
                "workstream_ref": "rec_1", "resource_binding_id": "bind_1",
                "context_pack_id": "ctx_other", "context_pack_sha256": digest,
            },
            context_pack={"id": "ctx_1", "sha256": digest, "content": context},
        )


def test_auto_registration_keeps_ambiguous_session_unassigned(tmp_path, monkeypatch):
    monkeypatch.setattr(project_core, "_read_discovery", lambda _path: {
        "integration_principals": {
            "agent": {"provider": "agent-hub", "provider_instance": "agent-hub-test"}
        },
        "integration_secrets": {"agent": "x" * 32},
        "agent_sessions_prepare_url": "http://127.0.0.1:8791/v1/agent-sessions/prepare",
        "agent_sessions_register_url": "http://127.0.0.1:8791/v1/agent-sessions/register",
    })
    monkeypatch.setattr(project_core, "_signed_post", lambda *_args, **_kwargs: {
        "status": "ambiguous", "preparation_id": "prep_1",
        "expires_at": "2026-08-19T00:15:00+00:00",
        "candidates": [{"candidate_id": "one"}, {"candidate_id": "two"}],
    })
    result = project_core.auto_register_session(
        {"id": "seat-2", "working_dir": str(tmp_path), "initial_prompt": ""},
        runtime_file=Path("unused"), data_dir=tmp_path,
    )
    assert result["project_core"]["registration_status"] == "ambiguous"
    assert len(result["project_core"]["candidates"]) == 2
    assert result["initial_prompt"] == ""


def test_handoff_adoption_sends_the_durable_seat_name(tmp_path, monkeypatch):
    discovery = {
        "integration_principals": {
            "agent": {
                "provider": "agent-hub",
                "provider_instance": "agent-hub-test",
            }
        },
        "integration_secrets": {"agent": "x" * 32},
        "agent_sessions_adopt_url": "http://127.0.0.1:8791/v1/agent-sessions/adopt",
    }
    seen = {}
    monkeypatch.setattr(project_core, "_read_discovery", lambda _path: discovery)

    def fake_post(url, payload, *, secret):
        seen.update(url=url, payload=payload, secret=secret)
        return {"status": "registered"}

    monkeypatch.setattr(project_core, "_signed_post", fake_post)
    monkeypatch.setattr(
        project_core, "_registered_result",
        lambda *_args, **_kwargs: {"project_core": {"registration_status": "registered"}},
    )
    result = project_core.adopt_handoff_session(
        {
            "id": "seat-adopted", "name": "Exact historical seat",
            "project_core": {
                "project_id": "prj_1", "record_id": "rec_1",
                "correlation_id": "corr_1", "context_pack_id": "ctx_1",
                "context_pack_sha256": "a" * 64,
            },
        },
        runtime_file=Path("unused"), data_dir=tmp_path,
    )
    assert result["project_core"]["registration_status"] == "registered"
    assert seen["url"].endswith("/v1/agent-sessions/adopt")
    assert seen["payload"]["external_session_id"] == "seat-adopted"
    assert seen["payload"]["seat_name"] == "Exact historical seat"


def test_default_suggest_does_not_register_single_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(project_core, "_read_discovery", lambda _path: {
        "integration_principals": {
            "agent": {"provider": "agent-hub", "provider_instance": "agent-hub-test"}
        },
        "integration_secrets": {"agent": "x" * 32},
        "agent_sessions_prepare_url": "http://127.0.0.1:8791/v1/agent-sessions/prepare",
        "agent_sessions_register_url": "http://127.0.0.1:8791/v1/agent-sessions/register",
    })
    calls = []

    def fake_post(url, payload, *, secret):
        calls.append(url)
        return {
            "status": "one", "preparation_id": "prep_1",
            "expires_at": "2026-08-19T00:15:00+00:00",
            "candidates": [{
                "candidate_id": "cand_1", "project_title": "Research",
                "workstream_title": "Relevant work",
            }],
        }

    monkeypatch.setattr(project_core, "_signed_post", fake_post)
    result = project_core.auto_register_session(
        {"id": "seat-3", "working_dir": str(tmp_path), "initial_prompt": ""},
        runtime_file=Path("unused"), data_dir=tmp_path,
    )
    assert result["project_core"]["registration_status"] == "suggested"
    assert calls == ["http://127.0.0.1:8791/v1/agent-sessions/prepare"]
    assert not (tmp_path / "project_core_handoffs").exists()


def test_tracking_off_does_not_read_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(
        project_core, "_read_discovery",
        lambda _path: (_ for _ in ()).throw(AssertionError("must not discover")),
    )
    result = project_core.auto_register_session(
        {"id": "seat-4", "working_dir": str(tmp_path), "initial_prompt": "ordinary"},
        runtime_file=Path("unused"), data_dir=tmp_path, tracking_mode="off",
    )
    assert result == {
        "project_core": {"registration_status": "off"},
        "initial_prompt": "ordinary",
    }


def test_target_picker_uses_read_only_discovery_endpoint(tmp_path, monkeypatch):
    discovery = {
        "integration_principals": {
            "agent": {"provider": "agent-hub", "provider_instance": "agent-hub-test"}
        },
        "integration_secrets": {"agent": "x" * 32},
        "agent_sessions_prepare_url": "http://127.0.0.1:8791/v1/agent-sessions/prepare",
        "agent_sessions_register_url": "http://127.0.0.1:8791/v1/agent-sessions/register",
        "agent_targets_url": "http://127.0.0.1:8791/v1/agent-targets/resolve",
    }
    monkeypatch.setattr(project_core, "_read_discovery", lambda _path: discovery)
    seen = {}

    def fake_post(url, payload, *, secret):
        seen.update(url=url, payload=payload, secret=secret)
        return {"status": "resolved", "candidates": [{"candidate_id": "cand_1"}]}

    monkeypatch.setattr(project_core, "_signed_post", fake_post)
    result = project_core.resolve_targets(
        working_dir=str(tmp_path), runtime_file=Path("unused")
    )
    assert result["status"] == "resolved"
    assert seen["url"].endswith("/v1/agent-targets/resolve")
    assert seen["payload"] == {"cwd": str(tmp_path)}


def test_explicit_workstream_selects_only_that_candidate(tmp_path, monkeypatch):
    context = {
        "project": {"title": "Research"},
        "focus": {"title": "Node B", "payload": {"goal": "Implement B"}},
    }
    digest = _context_hash(context)
    discovery = {
        "integration_principals": {
            "agent": {"provider": "agent-hub", "provider_instance": "agent-hub-test"}
        },
        "integration_secrets": {"agent": "x" * 32},
        "agent_sessions_prepare_url": "http://127.0.0.1:8791/v1/agent-sessions/prepare",
        "agent_sessions_register_url": "http://127.0.0.1:8791/v1/agent-sessions/register",
    }
    monkeypatch.setattr(project_core, "_read_discovery", lambda _path: discovery)
    registered_candidate = []

    def fake_post(url, payload, *, secret):
        if url.endswith("/prepare"):
            return {
                "status": "ambiguous", "preparation_id": "prep_1",
                "candidates": [
                    {
                        "candidate_id": "cand_a", "project_ref": "prj_1",
                        "workstream_ref": "rec_a",
                    },
                    {
                        "candidate_id": "cand_b", "project_ref": "prj_1",
                        "workstream_ref": "rec_b",
                    },
                ],
            }
        registered_candidate.append(payload["candidate_id"])
        return {
            "status": "registered",
            "association": {
                "id": "asoc_selected", "project_ref": "prj_1",
                "resource_binding_id": "bind_workspace_b",
                "project_title": "Research", "workstream_ref": "rec_b",
                "workstream_title": "Node B", "context_pack_id": "ctx_b",
                "context_pack_sha256": digest, "correlation_id": "corr_b",
                "maximum_visibility": "team", "provider": "agent-hub",
                "provider_instance": "agent-hub-test",
            },
            "context_pack": {"id": "ctx_b", "sha256": digest, "content": context},
        }

    monkeypatch.setattr(project_core, "_signed_post", fake_post)
    result = project_core.auto_register_session(
        {"id": "seat-b", "working_dir": str(tmp_path), "initial_prompt": ""},
        runtime_file=Path("unused"), data_dir=tmp_path, tracking_mode="on",
        selected_target={
            "project_id": "prj_1", "project_title": "Research",
            "record_id": "rec_b", "workstream_title": "Node B",
        },
    )
    assert registered_candidate == ["cand_b"]
    assert result["project_core"]["record_id"] == "rec_b"


def test_explicit_workstream_never_falls_back_to_another_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(project_core, "_read_discovery", lambda _path: {
        "integration_principals": {
            "agent": {"provider": "agent-hub", "provider_instance": "agent-hub-test"}
        },
        "integration_secrets": {"agent": "x" * 32},
        "agent_sessions_prepare_url": "http://127.0.0.1:8791/v1/agent-sessions/prepare",
        "agent_sessions_register_url": "http://127.0.0.1:8791/v1/agent-sessions/register",
    })
    calls = []

    def fake_post(url, payload, *, secret):
        calls.append(url)
        if url.endswith("/register"):
            raise AssertionError("a different candidate must never be registered")
        return {
            "status": "one", "preparation_id": "prep_1", "expires_at": "later",
            "candidates": [{
                "candidate_id": "cand_a", "project_ref": "prj_1",
                "workstream_ref": "rec_a",
            }],
        }

    monkeypatch.setattr(project_core, "_signed_post", fake_post)
    result = project_core.auto_register_session(
        {"id": "seat-b", "working_dir": str(tmp_path), "initial_prompt": ""},
        runtime_file=Path("unused"), data_dir=tmp_path, tracking_mode="on",
        selected_target={
            "project_id": "prj_1", "project_title": "Research",
            "record_id": "rec_b", "workstream_title": "Node B",
        },
    )
    assert calls == ["http://127.0.0.1:8791/v1/agent-sessions/prepare"]
    assert result["project_core"]["registration_status"] == "target_unavailable"
    assert result["project_core"]["desired_record_id"] == "rec_b"


def _brief_index(tmp_path: Path, project_ref: str, body: str) -> None:
    brief = tmp_path / "briefs" / "demo.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(body, encoding="utf-8")
    (tmp_path / "index.json").write_text(
        json.dumps({
            "schema_version": 1,
            "projects": {project_ref: {"slug": "demo", "brief_path": str(brief)}},
        }),
        encoding="utf-8",
    )


_BRIEF_BODY = """# demo

## 现在在做

- 正在改 buffer 合并。

## 最近完成

- 合并了 14 个星系。

## 待决 · 卡住

- 8 个疑似重复未判定。

## 下一步

- 逐一核定坐标来源。

<!-- pinned:begin -->
## 钉住的

- 归档不要手改。

<!-- pinned:end -->
"""


def test_bootstrap_includes_the_approved_project_brief(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_BRIEF_HOME", str(tmp_path))
    _brief_index(tmp_path, "prj_demo", _BRIEF_BODY)
    instruction = project_core._context_bootstrap(
        {"project": {"title": "Demo"}, "focus": {"title": "WS", "payload": {}}},
        association={"project_ref": "prj_demo", "project_title": "Demo", "workstream_title": "WS"},
        handoff_path=tmp_path / "handoff.json",
        agent_role="general",
    )
    assert "Project state summary" in instruction
    assert "正在改 buffer 合并" in instruction
    assert "8 个疑似重复未判定" in instruction
    assert "逐一核定坐标来源" in instruction
    # The human-owned pinned block travels with the brief.
    assert "归档不要手改" in instruction
    # `最近完成` is history, not orientation for the next turn; it stays out.
    assert "合并了 14 个星系" not in instruction


def test_bootstrap_without_a_brief_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_BRIEF_HOME", str(tmp_path / "missing"))
    instruction = project_core._context_bootstrap(
        {"project": {"title": "Demo"}, "focus": {"title": "WS", "payload": {}}},
        association={"project_ref": "prj_demo", "project_title": "Demo", "workstream_title": "WS"},
        handoff_path=tmp_path / "handoff.json",
        agent_role="general",
    )
    assert "Project state summary" not in instruction
    assert "Work target: Demo > WS" in instruction


def test_bootstrap_survives_a_corrupt_brief_index(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_BRIEF_HOME", str(tmp_path))
    (tmp_path / "index.json").write_text("{not json", encoding="utf-8")
    instruction = project_core._context_bootstrap(
        {"project": {"title": "Demo"}, "focus": {"title": "WS", "payload": {}}},
        association={"project_ref": "prj_demo", "project_title": "Demo", "workstream_title": "WS"},
        handoff_path=tmp_path / "handoff.json",
        agent_role="general",
    )
    assert "Project state summary" not in instruction
