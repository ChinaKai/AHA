from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import threading
import uuid

from aha_cli.store.config import load_config
from aha_cli.store.knowledge import init_knowledge_base, knowledge_root
from aha_cli.store.paths import aha_home_path

SKILL_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$|^[a-z0-9]$")
SKILL_MD = "SKILL.md"
OPENAI_YAML = "openai.yaml"
KNOWLEDGE_SKILL_SOURCE = "knowledge"
LEGACY_SKILL_SOURCE = "aha_home"


class SkillManagementError(ValueError):
    def __init__(self, message: str, status: str = "400 Bad Request") -> None:
        super().__init__(message)
        self.status = status


def _config(root: Path, config: dict | None = None) -> dict:
    return config if isinstance(config, dict) else load_config(root)


def skills_root(root: Path, workspace: Path | str | None = None, config: dict | None = None) -> Path:
    del workspace
    cfg = _config(root, config)
    return knowledge_root(root, cfg) / "skills"


def legacy_skills_root(root: Path) -> Path:
    return aha_home_path(root) / "skills"


def validate_skill_id(skill_id: str) -> str:
    value = str(skill_id or "").strip()
    if not value:
        raise SkillManagementError("skill id is required")
    if not SKILL_ID_PATTERN.fullmatch(value):
        raise SkillManagementError("skill id must use lowercase letters, digits, and hyphens")
    return value


def _safe_skill_dir(base: Path, skill_id: str) -> Path:
    safe_id = validate_skill_id(skill_id)
    base = base.resolve(strict=False)
    path = (base / safe_id).resolve(strict=False)
    if path != base / safe_id:
        raise SkillManagementError("invalid skill path")
    return path


def _skill_dir(root: Path, skill_id: str, workspace: Path | str | None = None, config: dict | None = None) -> Path:
    return _safe_skill_dir(skills_root(root, workspace, config), skill_id)


def _legacy_skill_dir(root: Path, skill_id: str) -> Path:
    return _safe_skill_dir(legacy_skills_root(root), skill_id)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _frontmatter_value(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def skill_frontmatter(skill_md: str) -> dict[str, str]:
    lines = skill_md.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in {"name", "description"}:
            fields[key] = _frontmatter_value(value)
    return fields


def skill_title(skill_md: str, fallback: str) -> str:
    for line in skill_md.splitlines():
        title = line.strip()
        if title.startswith("# "):
            return title[2:].strip() or fallback
    metadata = skill_frontmatter(skill_md)
    return metadata.get("name") or fallback


def _interface_metadata(openai_yaml: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    in_interface = False
    for raw_line in openai_yaml.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not raw_line.startswith(" ") and line.endswith(":"):
            in_interface = line[:-1].strip() == "interface"
            continue
        if not in_interface or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in {"display_name", "short_description", "default_prompt"}:
            fields[key] = _frontmatter_value(value)
    return fields


def _skill_summary(path: Path, *, source: str) -> dict[str, object]:
    skill_md_path = path / SKILL_MD
    skill_md = _read_text(skill_md_path)
    openai_yaml_path = path / "agents" / OPENAI_YAML
    openai_yaml = _read_text(openai_yaml_path)
    frontmatter = skill_frontmatter(skill_md)
    interface = _interface_metadata(openai_yaml)
    stat = skill_md_path.stat()
    label = interface.get("display_name") or skill_title(skill_md, path.name)
    return {
        "id": path.name,
        "name": frontmatter.get("name") or path.name,
        "label": label,
        "description": frontmatter.get("description") or "",
        "short_description": interface.get("short_description") or "",
        "default_prompt": interface.get("default_prompt") or "",
        "path": str(skill_md_path),
        "agents_path": str(openai_yaml_path) if openai_yaml_path.exists() else "",
        "has_agent_metadata": openai_yaml_path.exists(),
        "source": source,
        "updated_at": stat.st_mtime,
        "size": stat.st_size,
    }


def _copy_skill_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        if item.is_symlink():
            continue
        rel = item.relative_to(source)
        dest = target / rel
        if item.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)


def sync_legacy_skills_to_knowledge(
    root: Path,
    workspace: Path | str | None = None,
    config: dict | None = None,
) -> list[dict[str, str]]:
    legacy_base = legacy_skills_root(root)
    if not legacy_base.is_dir():
        return []
    legacy_skills = [
        item for item in sorted(legacy_base.iterdir(), key=lambda candidate: candidate.name.lower())
        if item.is_dir() and (item / SKILL_MD).is_file()
    ]
    if not legacy_skills:
        return []

    cfg = _config(root, config)
    init_knowledge_base(root, cfg)
    primary = skills_root(root, workspace, cfg)
    migrated: list[dict[str, str]] = []
    for item in legacy_skills:
        try:
            target = _safe_skill_dir(primary, item.name)
        except SkillManagementError:
            continue
        if target.exists():
            continue
        _copy_skill_tree(item, target)
        migrated.append({"id": item.name, "from": str(item), "to": str(target)})
    return migrated


def list_managed_skills(
    root: Path,
    workspace: Path | str | None = None,
    config: dict | None = None,
) -> list[dict[str, object]]:
    cfg = _config(root, config)
    sync_legacy_skills_to_knowledge(root, workspace, cfg)
    skills: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    base = skills_root(root, workspace, cfg)
    if base.is_dir():
        for item in sorted(base.iterdir(), key=lambda candidate: candidate.name.lower()):
            if item.is_dir() and (item / SKILL_MD).is_file():
                seen_ids.add(item.name)
                skills.append(_skill_summary(item, source=KNOWLEDGE_SKILL_SOURCE))
    legacy_base = legacy_skills_root(root)
    if legacy_base.is_dir():
        for item in sorted(legacy_base.iterdir(), key=lambda candidate: candidate.name.lower()):
            if not item.is_dir() or not (item / SKILL_MD).is_file() or item.name in seen_ids:
                continue
            seen_ids.add(item.name)
            skills.append(_skill_summary(item, source=LEGACY_SKILL_SOURCE))
    return skills


def get_managed_skill(
    root: Path,
    skill_id: str,
    workspace: Path | str | None = None,
    config: dict | None = None,
) -> dict[str, object]:
    cfg = _config(root, config)
    sync_legacy_skills_to_knowledge(root, workspace, cfg)
    summary: dict[str, object] | None = None
    path: Path | None = None
    candidate = _skill_dir(root, skill_id, workspace, cfg)
    if (candidate / SKILL_MD).is_file():
        path = candidate
        summary = _skill_summary(candidate, source=KNOWLEDGE_SKILL_SOURCE)
    if summary is None:
        candidate = _legacy_skill_dir(root, skill_id)
        if (candidate / SKILL_MD).is_file():
            path = candidate
            summary = _skill_summary(candidate, source=LEGACY_SKILL_SOURCE)
    if summary is None or path is None:
        raise SkillManagementError(f"skill not found: {skill_id}", "404 Not Found")
    skill_md_path = path / SKILL_MD
    summary["skill_md"] = _read_text(skill_md_path)
    summary["openai_yaml"] = _read_text(path / "agents" / OPENAI_YAML)
    return summary


def default_skill_markdown(skill_id: str) -> str:
    safe_id = validate_skill_id(skill_id)
    title = " ".join(part.capitalize() for part in safe_id.split("-"))
    return "\n".join(
        [
            "---",
            f"name: {safe_id}",
            "description: Describe what this skill does and when Codex should use it.",
            "---",
            "",
            f"# {title}",
            "",
            "## Workflow",
            "",
            "- Add the concrete steps this skill should guide.",
            "",
        ]
    )


def save_managed_skill(
    root: Path,
    skill_id: str,
    payload: dict,
    workspace: Path | str | None = None,
    config: dict | None = None,
) -> dict[str, object]:
    cfg = _config(root, config)
    init_knowledge_base(root, cfg)
    path = _skill_dir(root, skill_id, workspace, cfg)
    skill_md = str(payload.get("skill_md", payload.get("content", "")) or "")
    if not skill_md.strip():
        skill_md = default_skill_markdown(skill_id)
    if len(skill_md.encode("utf-8")) > 512 * 1024:
        raise SkillManagementError("SKILL.md is too large")
    _write_text(path / SKILL_MD, skill_md if skill_md.endswith("\n") else f"{skill_md}\n")

    if "openai_yaml" in payload or "agent_metadata" in payload:
        openai_yaml = str(payload.get("openai_yaml", payload.get("agent_metadata", "")) or "")
        openai_path = path / "agents" / OPENAI_YAML
        if openai_yaml.strip():
            if len(openai_yaml.encode("utf-8")) > 128 * 1024:
                raise SkillManagementError("agents/openai.yaml is too large")
            _write_text(openai_path, openai_yaml if openai_yaml.endswith("\n") else f"{openai_yaml}\n")
        elif openai_path.exists():
            openai_path.unlink()

    return get_managed_skill(root, skill_id, workspace, cfg)


def create_managed_skill(
    root: Path,
    payload: dict,
    workspace: Path | str | None = None,
    config: dict | None = None,
) -> dict[str, object]:
    skill_id = validate_skill_id(str(payload.get("id", payload.get("name", "")) or ""))
    cfg = _config(root, config)
    init_knowledge_base(root, cfg)
    sync_legacy_skills_to_knowledge(root, workspace, cfg)
    path = _skill_dir(root, skill_id, workspace, cfg)
    if (path / SKILL_MD).exists():
        raise SkillManagementError(f"skill already exists: {skill_id}", "409 Conflict")
    return save_managed_skill(root, skill_id, payload, workspace, cfg)


def delete_managed_skill(
    root: Path,
    skill_id: str,
    workspace: Path | str | None = None,
    config: dict | None = None,
) -> None:
    cfg = _config(root, config)
    deleted = False
    path = _skill_dir(root, skill_id, workspace, cfg)
    if (path / SKILL_MD).is_file():
        shutil.rmtree(path)
        deleted = True
    legacy_path = _legacy_skill_dir(root, skill_id)
    if (legacy_path / SKILL_MD).is_file():
        shutil.rmtree(legacy_path)
        deleted = True
    if not deleted:
        raise SkillManagementError(f"skill not found: {skill_id}", "404 Not Found")


__all__ = [
    "SkillManagementError",
    "create_managed_skill",
    "delete_managed_skill",
    "get_managed_skill",
    "legacy_skills_root",
    "list_managed_skills",
    "save_managed_skill",
    "skills_root",
    "sync_legacy_skills_to_knowledge",
    "validate_skill_id",
]
