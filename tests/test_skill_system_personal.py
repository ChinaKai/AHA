from __future__ import annotations

import json
from pathlib import Path
import tempfile

from aha_cli.domain.models import default_knowledge_config
from aha_cli.services.skill_management import (
    classify_skill_source,
    create_managed_skill,
    delete_managed_skill,
    get_managed_skill,
    list_managed_skills,
    save_managed_skill,
    skill_frontmatter,
    write_skills_moc,
)
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import init_knowledge_base
from aha_cli.store.paths import config_path
from aha_cli.web.task_command_format import format_aha_skill_command


def _cfg() -> dict:
    kb = default_knowledge_config()
    kb["enabled"] = True
    return {"knowledge": kb}


def _home(tmp_path: Path) -> tuple[Path, dict]:
    home = tmp_path / ".aha"
    cfg = _cfg()
    write_json(config_path(home), cfg)
    init_knowledge_base(home, cfg)
    return home, cfg


def _write_skill(home: Path, skill_id: str, skill_md: str) -> Path:
    path = home / "knowledge" / "skills" / skill_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(skill_md, encoding="utf-8")
    return path


def test_classify_skill_source_frontmatter_wins():
    assert classify_skill_source("---\nsource: system\n---\n# x\n", fallback_source="knowledge") == "system"
    assert classify_skill_source("---\nsource: personal\n---\n# x\n", fallback_source="aha_home") == "personal"
    # No source frontmatter -> defaults personal regardless of storage.
    assert classify_skill_source("# x\n", fallback_source="knowledge") == "personal"
    assert classify_skill_source("---\nname: x\n---\n", fallback_source="aha_home") == "personal"


def test_skill_frontmatter_parses_source():
    assert skill_frontmatter("---\nname: a\nsource: system\n---\n")["source"] == "system"


def test_list_classifies_system_and_personal(tmp_path: Path):
    home, cfg = _home(tmp_path)
    _write_skill(home, "sys-skill", "---\nname: sys-skill\nsource: system\n---\n# Sys\n")
    _write_skill(home, "personal-skill", "---\nname: personal-skill\n---\n# Pers\n")
    skills = list_managed_skills(home, None, cfg)
    by_id = {s["id"]: s for s in skills}
    assert by_id["sys-skill"]["source"] == "system"
    assert by_id["sys-skill"]["system"] is True
    assert by_id["personal-skill"]["source"] == "personal"
    assert by_id["personal-skill"]["system"] is False
    assert by_id["personal-skill"]["storage_source"] == "knowledge"


def test_directory_classified_layout_read(tmp_path: Path):
    home, cfg = _home(tmp_path)
    # New layout: <skills_root>/personal/<id> and <skills_root>/system/<id>.
    for category, skill_id, skill_md in (
        ("personal", "pers", "---\nname: pers\n---\n# P\n"),
        ("system", "sys", "---\nname: sys\nsource: system\n---\n# S\n"),
    ):
        d = home / "knowledge" / "skills" / category / skill_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(skill_md, encoding="utf-8")
    skills = list_managed_skills(home, None, cfg)
    by_id = {s["id"]: s for s in skills}
    assert "pers" in by_id and "sys" in by_id
    assert by_id["pers"]["source"] == "personal"
    assert by_id["sys"]["source"] == "system"
    # get_managed_skill finds them too.
    assert get_managed_skill(home, "sys", None, cfg)["id"] == "sys"


def test_system_skill_is_read_only(tmp_path: Path):
    home, cfg = _home(tmp_path)
    _write_skill(home, "sys", "---\nname: sys\nsource: system\n---\n# S\n")
    from aha_cli.services.skill_management import SkillManagementError

    try:
        save_managed_skill(home, "sys", {"skill_md": "# Overwrite\n"}, None, cfg)
        raise AssertionError("expected system skill save to fail")
    except SkillManagementError as exc:
        assert "read-only" in str(exc)
        assert exc.status == "403 Forbidden"

    try:
        delete_managed_skill(home, "sys", None, cfg)
        raise AssertionError("expected system skill delete to fail")
    except SkillManagementError as exc:
        assert "read-only" in str(exc)
    # Still present.
    assert (home / "knowledge" / "skills" / "sys" / "SKILL.md").is_file()


def test_personal_skill_editable_and_deletable(tmp_path: Path):
    home, cfg = _home(tmp_path)
    skill = create_managed_skill(home, {"id": "board-debug", "skill_md": "---\nname: board-debug\n---\n# BD\n"}, None, cfg)
    assert skill["source"] == "personal"
    updated = save_managed_skill(home, "board-debug", {"skill_md": "# BD v2\n"}, None, cfg)
    assert "BD v2" in updated["skill_md"]
    delete_managed_skill(home, "board-debug", None, cfg)
    from aha_cli.services.skill_management import SkillManagementError

    try:
        get_managed_skill(home, "board-debug", None, cfg)
        raise AssertionError("expected deleted skill to be gone")
    except SkillManagementError:
        pass


def test_write_skills_moc(tmp_path: Path):
    home, cfg = _home(tmp_path)
    _write_skill(home, "sys", "---\nname: sys\nsource: system\n---\n# Sys\n")
    _write_skill(home, "pers", "---\nname: pers\n---\n# Pers\n")
    moc = write_skills_moc(home, cfg)
    assert moc is not None
    text = moc.read_text(encoding="utf-8")
    assert "系统技能" in text and "个人技能" in text
    assert "sys" in text and "pers" in text


def test_aha_skill_slash_command_renders_creation_guide():
    handled, agent_message, reply = format_aha_skill_command("/aha skill board-debug 板卡调试技能")
    assert handled is False
    assert agent_message is not None
    assert "skill_creation_guide" not in agent_message  # rendered, not raw name
    assert "SKILL.md" in agent_message
    assert "board-debug" in agent_message
    assert reply is None
    # Empty usage.
    handled, agent_message, reply = format_aha_skill_command("/aha skill")
    assert handled is True
    assert "Usage:" in reply


def test_legacy_skill_layout_still_read(tmp_path: Path):
    # Backward compat: a legacy flat skill with no source frontmatter is read as
    # personal (user-installed), not rejected by the new classification. The
    # legacy dir is migrated into the knowledge skills dir, then listed.
    home, cfg = _home(tmp_path)
    legacy = home / "skills" / "aha-hardware-debug"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("# AHA Hardware Debug\n\n## Core Rules\n- x\n", encoding="utf-8")
    skills = list_managed_skills(home, None, cfg)
    by_id = {s["id"]: s for s in skills}
    assert "aha-hardware-debug" in by_id
    assert by_id["aha-hardware-debug"]["source"] == "personal"
    assert (home / "knowledge" / "skills" / "aha-hardware-debug" / "SKILL.md").is_file()
