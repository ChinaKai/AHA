from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import normalize_task_skills
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.paths import aha_home_path


def _skill_label(skill_md: Path, fallback: str) -> str:
    try:
        for line in skill_md.read_text(encoding="utf-8").splitlines():
            title = line.strip()
            if title.startswith("# "):
                return title[2:].strip() or fallback
    except OSError:
        pass
    return fallback


def _skill_candidates(skills_root: Path, source: str) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    if not skills_root.is_dir():
        return options
    for item in sorted(skills_root.iterdir(), key=lambda path: path.name.lower()):
        if not item.is_dir():
            continue
        skill_md = item / "SKILL.md"
        if not skill_md.is_file():
            continue
        options.append(
            {
                "id": item.name,
                "label": _skill_label(skill_md, item.name),
                "path": str(skill_md),
                "source": source,
            }
        )
    return options


def discover_task_skill_options(root: Path, cwd: Path | None = None) -> list[dict[str, str]]:
    del cwd
    skills_root = aha_home_path(root) / "skills"
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for option in _skill_candidates(skills_root, "aha_home"):
        path = option["path"]
        if path in seen:
            continue
        seen.add(path)
        options.append(option)
    return options


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
