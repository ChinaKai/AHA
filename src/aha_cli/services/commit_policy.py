from __future__ import annotations

import re

from aha_cli.backends.registry import resolve_model
from aha_cli.services.prompt_templates import render_prompt_template


CONVENTIONAL_TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")
SUBJECT_RE = re.compile(r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([A-Za-z0-9_./-]+\))?(!)?: .+")
BACKEND_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "command": "Command",
    "stub": "Stub",
}
DEFAULT_GENERATED_BY = "AHA Codex GPT-5.5"
FORBIDDEN_TRAILER_KEYS = {
    "aha-agent",
    "aha-scope",
    "aha-task",
    "co-authored-by",
}


def _model_label(model: str | None) -> str:
    normalized = str(model or "default").strip() or "default"
    if normalized.startswith("gpt-"):
        return f"GPT-{normalized[4:]}"
    return normalized


def generated_by_for_backend_model(backend: str | None, model: str | None) -> str:
    normalized_backend = str(backend or "codex").strip() or "codex"
    label = BACKEND_LABELS.get(normalized_backend, normalized_backend.title())
    resolved_model = resolve_model(normalized_backend, model)
    model_label = _model_label(resolved_model)
    return f"AHA {label} {model_label}"


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

def validate_commit_message(message: str, expected_generated_by: str | None = None) -> list[str]:
    lines = message.splitlines()
    subject = lines[0].strip() if lines else ""
    errors: list[str] = []
    if not SUBJECT_RE.match(subject):
        errors.append("first line must be a Conventional Commit subject, e.g. feat(web): add lazy loading")
    generated_by_values: list[str] = []
    legacy_aha_trailers: list[str] = []
    forbidden_trailers: list[str] = []
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized_key = key.strip()
        normalized_key_lower = normalized_key.lower()
        normalized_value = value.strip()
        if normalized_key == "Generated-by":
            generated_by_values.append(normalized_value)
        if normalized_key in {"AHA-Task", "AHA-Agent", "AHA-Scope"}:
            legacy_aha_trailers.append(normalized_key)
        if normalized_key_lower in FORBIDDEN_TRAILER_KEYS:
            forbidden_trailers.append(normalized_key)
    if len(generated_by_values) != 1:
        errors.append("commit body must include exactly one Generated-by trailer")
    elif expected_generated_by and generated_by_values[0] != expected_generated_by:
        errors.append(f"commit body Generated-by value must be exactly: {expected_generated_by}")
    if legacy_aha_trailers:
        errors.append("commit body should not include AHA task/agent/scope trailers; keep that tracking in the AHA journal")
    extra_forbidden_trailers = sorted({key for key in forbidden_trailers if key not in {"AHA-Task", "AHA-Agent", "AHA-Scope"}})
    if extra_forbidden_trailers:
        errors.append(f"commit body should not include unsupported trailers: {', '.join(extra_forbidden_trailers)}")
    return errors


def commit_message_policy_prompt(task_id: str, agent_id: str, backend: str | None = None, model: str | None = None) -> str:
    generated_by = generated_by_for_backend_model(backend, model)
    return render_prompt_template("commit_policy.md", task_id=task_id, agent_id=agent_id, generated_by=generated_by)
