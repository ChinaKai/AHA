from __future__ import annotations

import re
import textwrap


CONVENTIONAL_TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")
SUBJECT_RE = re.compile(r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([A-Za-z0-9_./-]+\))?(!)?: .+")


def format_commit_message(
    commit_type: str,
    summary: str,
    task_id: str,
    agent_id: str,
    scope: str | None = None,
    aha_scope: str | None = None,
) -> str:
    normalized_type = commit_type.strip()
    normalized_scope = (scope or "").strip()
    normalized_summary = summary.strip()
    normalized_task_id = task_id.strip()
    normalized_agent_id = agent_id.strip()
    normalized_aha_scope = (aha_scope or "").strip()
    if normalized_type not in CONVENTIONAL_TYPES:
        raise ValueError(f"Invalid conventional commit type: {commit_type}")
    if not normalized_summary:
        raise ValueError("Commit summary is required")
    if not normalized_task_id:
        raise ValueError("AHA task id is required")
    if not normalized_agent_id:
        raise ValueError("AHA agent id is required")
    subject_scope = f"({normalized_scope})" if normalized_scope else ""
    lines = [
        f"{normalized_type}{subject_scope}: {normalized_summary}",
        "",
        f"AHA-Task: {normalized_task_id}",
        f"AHA-Agent: {normalized_agent_id}",
    ]
    if normalized_aha_scope:
        lines.append(f"AHA-Scope: {normalized_aha_scope}")
    return "\n".join(lines) + "\n"


def validate_commit_message(message: str) -> list[str]:
    lines = message.splitlines()
    subject = lines[0].strip() if lines else ""
    errors: list[str] = []
    if not SUBJECT_RE.match(subject):
        errors.append("first line must be a Conventional Commit subject, e.g. feat(web): add lazy loading")
    trailers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.startswith("AHA-"):
            trailers[key] = value.strip()
    if not trailers.get("AHA-Task"):
        errors.append("commit body must include AHA-Task: <task-id>")
    if not trailers.get("AHA-Agent"):
        errors.append("commit body must include AHA-Agent: <agent-id>")
    return errors


def commit_message_policy_prompt(task_id: str, agent_id: str) -> str:
    return textwrap.dedent(
        f"""\
        Commit message policy:
        - Use a Conventional Commit subject: `<type>(<scope>): <summary>`.
        - Include AHA trailers in the commit body:
          `AHA-Task: {task_id}`
          `AHA-Agent: {agent_id}`
          `AHA-Scope: <short-scope>`
        - Prefer `aha commit --type <type> --scope <scope> --summary <summary> --task-id {task_id} --agent {agent_id} --aha-scope <short-scope>` over raw `git commit`.
        - Validate hand-written commit messages with `aha commit-check <message-file>` before committing.
        """
    )
