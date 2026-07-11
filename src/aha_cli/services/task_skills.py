from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import normalize_task_skills
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.services.skill_management import list_managed_skills


def discover_task_skill_options(root: Path, cwd: Path | None = None) -> list[dict[str, str]]:
    return [
        {
            "id": str(skill.get("id") or ""),
            "label": str(skill.get("label") or skill.get("id") or skill.get("path") or ""),
            "path": str(skill.get("path") or ""),
            "source": str(skill.get("source") or ""),
        }
        for skill in list_managed_skills(root, cwd)
        if skill.get("path")
    ]


def task_skills_context_for_prompt(task: dict) -> str:
    config = normalize_task_skills(task.get("task_skills"))
    paths = config.get("enabled_paths") or []
    if not paths:
        return ""
    return render_prompt_template(
        "task_skills_context.md",
        enabled_paths="\n".join(f"  - {path}" for path in paths),
    )


__all__ = ["discover_task_skill_options", "task_skills_context_for_prompt"]
