from __future__ import annotations

import re

from aha_cli.services.prompt_templates import render_prompt_template


CONVENTIONAL_TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")
SUBJECT_RE = re.compile(r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([A-Za-z0-9_./-]+\))?(!)?: .+")
DEFAULT_GENERATED_BY = "AHA Codex GPT-5.5"


def format_commit_message(
    commit_type: str,
    summary: str,
    task_id: str | None = None,
    agent_id: str | None = None,
    scope: str | None = None,
    aha_scope: str | None = None,
    generated_by: str = DEFAULT_GENERATED_BY,
) -> str:
    normalized_type = commit_type.strip()
    normalized_scope = (scope or "").strip()
    normalized_summary = summary.strip()
    normalized_generated_by = generated_by.strip()
    if normalized_type not in CONVENTIONAL_TYPES:
        raise ValueError(f"Invalid conventional commit type: {commit_type}")
    if not normalized_summary:
        raise ValueError("Commit summary is required")
    if not normalized_generated_by:
        raise ValueError("Generated-by trailer value is required")
    subject_scope = f"({normalized_scope})" if normalized_scope else ""
    lines = [
        f"{normalized_type}{subject_scope}: {normalized_summary}",
        "",
        f"Generated-by: {normalized_generated_by}",
    ]
    return "\n".join(lines) + "\n"


def validate_commit_message(message: str) -> list[str]:
    lines = message.splitlines()
    subject = lines[0].strip() if lines else ""
    errors: list[str] = []
    if not SUBJECT_RE.match(subject):
        errors.append("first line must be a Conventional Commit subject, e.g. feat(web): add lazy loading")
    trailers: dict[str, str] = {}
    legacy_aha_trailers: list[str] = []
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized_key = key.strip()
        trailers[normalized_key] = value.strip()
        if normalized_key in {"AHA-Task", "AHA-Agent", "AHA-Scope"}:
            legacy_aha_trailers.append(normalized_key)
    if not trailers.get("Generated-by"):
        errors.append(f"commit body must include Generated-by: {DEFAULT_GENERATED_BY}")
    if legacy_aha_trailers:
        errors.append("commit body should not include AHA task/agent/scope trailers; keep that tracking in the AHA journal")
    return errors


def commit_message_policy_prompt(task_id: str, agent_id: str) -> str:
    return render_prompt_template("commit_policy.md", task_id=task_id, agent_id=agent_id)
