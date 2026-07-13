"""Pipeline templates — DATA, not code.

Each template is a named linear sequence of phases. A phase carries a default
provider and a prompt template with slots: {task}, {sentinel}, {base_branch}.
The generic runner in orchestrator.py executes ANY template; adding a new task
type means adding an entry here, never touching the runner.
"""
from __future__ import annotations

# Every phase's prompt asks the agent to end with this line. v1 uses it as a
# human-visible "I'm done" marker (completion is detected from the seat going
# idle); later versions can also grep for it to auto-confirm.
SENTINEL = "<<<PHASE-DONE>>>"


TEMPLATES: dict[str, dict] = {
    "code": {
        "label": "写代码：plan → implement → review",
        "phases": [
            {"role": "plan", "provider": "claude", "prompt":
                "任务：\n{task}\n\n先不要写代码。请制定实现方案并写入仓库根目录的 "
                "PLAN.md（要点、涉及文件、步骤、风险）。完成后在最后单独输出一行：{sentinel}"},
            {"role": "implement", "provider": "claude", "prompt":
                "请阅读仓库根目录的 PLAN.md 并实现它，用 git 提交到当前分支（可多次提交）。"
                "只做 PLAN.md 涉及的改动。完成后在最后单独输出一行：{sentinel}"},
            {"role": "review", "provider": "claude", "prompt":
                "请 review 当前分支相对基线 {base_branch} 的全部改动"
                "（git diff {base_branch}...HEAD）。把结论写入 REVIEW.md，第一行只写 "
                "PASS 或 FAIL，其后列出问题。完成后在最后单独输出一行：{sentinel}"},
        ],
    },
    "writing": {
        "label": "写文章：起草 → 查证 → 修订",
        "phases": [
            {"role": "draft", "provider": "claude", "prompt":
                "任务：\n{task}\n\n请起草文章，写入仓库根目录的 DRAFT.md。"
                "完成后在最后单独输出一行：{sentinel}"},
            {"role": "factcheck", "provider": "claude", "prompt":
                "请核对 DRAFT.md 中的事实、引用与数据，把发现的问题与修改建议写入 "
                "CHECK.md。完成后在最后单独输出一行：{sentinel}"},
            {"role": "revise", "provider": "claude", "prompt":
                "请根据 CHECK.md 的意见修订 DRAFT.md（直接改 DRAFT.md）。"
                "完成后在最后单独输出一行：{sentinel}"},
        ],
    },
    "discussion": {
        "label": "讨论：主张 → 反驳 → 综合",
        "phases": [
            {"role": "propose", "provider": "claude", "prompt":
                "议题：\n{task}\n\n请给出你的主张与论据，写入仓库根目录的 DISCUSS.md"
                "（## 主张 一节）。完成后在最后单独输出一行：{sentinel}"},
            {"role": "critique", "provider": "claude", "prompt":
                "请阅读 DISCUSS.md 的主张，尽力反驳、找出漏洞与反例，追加写入 DISCUSS.md"
                "（## 反驳 一节）。完成后在最后单独输出一行：{sentinel}"},
            {"role": "synthesize", "provider": "claude", "prompt":
                "请综合 DISCUSS.md 的主张与反驳，给出结论与理由，追加写入 DISCUSS.md"
                "（## 结论 一节）。完成后在最后单独输出一行：{sentinel}"},
        ],
    },
}


def get(template_id: str) -> dict | None:
    return TEMPLATES.get(template_id)


def catalog() -> list[dict]:
    """For the UI to PREFILL the editable step list: id, label, and each phase's
    role/provider/prompt. Templates are just starting points now — the dialog
    lets you add/remove/reorder/edit steps before launch."""
    return [
        {"id": tid, "label": t["label"],
         "phases": [{"role": p["role"], "provider": p["provider"], "prompt": p["prompt"]}
                    for p in t["phases"]]}
        for tid, t in TEMPLATES.items()
    ]


def resolve_prompt(phase: dict, *, task: str, base_branch: str) -> str:
    return phase["prompt"].format(task=task, sentinel=SENTINEL, base_branch=base_branch)


def wrap_outline_step(title: str, body: str) -> str:
    """Turn one parsed outline step into the prompt its cold agent receives. The
    full outline is copied into the worktree as OUTLINE.md, so the agent gets the
    whole plan as context while focusing on just this step."""
    extra = f"\n\n{body}" if body.strip() else ""
    return (f"按仓库根目录 OUTLINE.md 的整体计划，现在**只做这一步**：「{title}」。{extra}\n\n"
            f"完成后（若涉及代码请 git 提交），在最后单独输出一行：{SENTINEL}")
