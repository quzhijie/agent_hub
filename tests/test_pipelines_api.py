import subprocess
from pathlib import Path


def _git_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "init"], cwd=root, check=True)
    return root


def _steps(*roles):
    return [{"role": r, "prompt": f"do {r}", "provider": "claude"} for r in roles]


def _project(client, root):
    return client.post("/api/projects", json={"name": "P", "root_dir": str(root)}).json()["id"]


def test_templates_endpoint_prefill(client):
    cat = client.get("/api/pipeline-templates").json()
    assert {"code", "writing", "discussion"} <= {t["id"] for t in cat}
    code = next(t for t in cat if t["id"] == "code")
    assert [p["role"] for p in code["phases"]] == ["plan", "implement", "review"]
    assert all(p["prompt"] for p in code["phases"])       # dialog prefills from these


def test_create_and_list_pipeline(client, tmp_path):
    pid = _project(client, _git_repo(tmp_path))
    r = client.post("/api/pipelines", json={"project_id": pid, "steps": _steps("plan", "implement", "review")})
    assert r.status_code == 200
    pl = r.json()
    assert pl["status"] == "running"
    assert [ph["role"] for ph in pl["phases"]] == ["plan", "implement", "review"]
    assert client.get("/api/pipelines").json()[0]["id"] == pl["id"]
    assert client.post(f"/api/pipelines/{pl['id']}/approve").status_code == 400  # nothing ready


def test_arbitrary_length_and_per_step_provider(client, tmp_path):
    pid = _project(client, _git_repo(tmp_path))
    pl = client.post("/api/pipelines", json={"project_id": pid, "steps": _steps("a", "b", "c", "d", "e")}).json()
    assert len(pl["phases"]) == 5                          # not fixed to 3 anymore
    steps = _steps("x", "y")
    steps[1]["provider"] = "codex"
    pl2 = client.post("/api/pipelines", json={"project_id": pid, "steps": steps}).json()
    assert pl2["phases"][1]["seat"]["provider"] == "codex"


def test_empty_steps_rejected(client, tmp_path):
    pid = _project(client, _git_repo(tmp_path))
    assert client.post("/api/pipelines", json={"project_id": pid, "steps": []}).status_code == 400


def test_create_pipeline_requires_git_repo(client, tmp_path):
    pid = _project(client, tmp_path)                       # tmp_path is not a git repo
    assert client.post("/api/pipelines", json={"project_id": pid, "steps": _steps("a")}).status_code == 400


def test_parse_outline_headings(client):
    md = "## 重构\n拆分模块\n\n## 测试\n补单测\n\n## review\n看 diff"
    steps = client.post("/api/parse-outline", json={"text": md}).json()["steps"]
    assert [s["role"] for s in steps] == ["重构", "测试", "review"]
    assert "重构" in steps[0]["prompt"] and "OUTLINE.md" in steps[0]["prompt"]


def test_parse_outline_numbered(client):
    steps = client.post("/api/parse-outline", json={"text": "1. 先做A\n2. 再做B\n3. 最后C"}).json()["steps"]
    assert [s["role"] for s in steps] == ["先做A", "再做B", "最后C"]


def test_parse_outline_nested_splits_on_one_level(client):
    """A nested outline (a `# Title`, `## Phase` steps, `### sub-points`) must cut
    only on the repeating level (`##`) — not on every heading. The title and the
    sub-points stay inside their step's body; a `# comment` in a code fence is not
    a heading."""
    md = (
        "# 大标题\n介绍文字\n\n"
        "## 阶段一\n### 子点A\n干活\n### 子点B\n```sh\n# 这不是标题\necho hi\n```\n\n"
        "## 阶段二\n收尾\n"
    )
    steps = client.post("/api/parse-outline", json={"text": md}).json()["steps"]
    assert [s["role"] for s in steps] == ["阶段一", "阶段二"]
    # the ### sub-points and the fenced `# comment` land in 阶段一's prompt body
    assert "子点A" in steps[0]["prompt"] and "这不是标题" in steps[0]["prompt"]


def test_parse_outline_from_file_then_launch(client, tmp_path, monkeypatch):
    from app import tmux
    monkeypatch.setattr(tmux, "has_session", lambda n: False)
    root = _git_repo(tmp_path)
    outline = root / "OUTLINE.md"
    outline.write_text("## 步骤一\n做一件事\n\n## 步骤二\n做另一件")
    pid = _project(client, root)

    parsed = client.post("/api/parse-outline", json={"path": str(outline)}).json()
    assert len(parsed["steps"]) == 2 and parsed["outline_path"] == str(outline)

    pl = client.post("/api/pipelines", json={
        "project_id": pid, "steps": parsed["steps"], "outline_path": parsed["outline_path"]}).json()
    assert [ph["role"] for ph in pl["phases"]] == ["步骤一", "步骤二"]
    # the outline was copied into the isolated worktree for the agents to read
    assert (Path(pl["worktree_path"]) / "OUTLINE.md").is_file()


def test_parse_outline_missing_file(client):
    assert client.post("/api/parse-outline", json={"path": "/no/such/outline.md"}).status_code == 400


def test_delete_pipeline_cleans_up(client, tmp_path, monkeypatch):
    from app import tmux
    monkeypatch.setattr(tmux, "has_session", lambda n: False)
    pid = _project(client, _git_repo(tmp_path))
    pl = client.post("/api/pipelines", json={"project_id": pid, "steps": _steps("a", "b")}).json()
    assert client.delete(f"/api/pipelines/{pl['id']}").status_code == 200
    assert client.get("/api/pipelines").json() == []
