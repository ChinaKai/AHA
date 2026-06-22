from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from aha_cli.cli import main
from aha_cli.domain.models import default_config
from aha_cli.store.config import load_config
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import (
    init_knowledge_base,
    knowledge_root,
    knowledge_status,
    list_entries,
    normalize_git_remote,
    parse_entry,
    project_key,
    project_key_aliases,
    read_entry,
    serialize_entry,
    slugify,
    write_entry,
)
from aha_cli.store.paths import config_path


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_default_config_has_knowledge_block():
    cfg = default_config()
    assert cfg["knowledge"]["enabled"] is False
    assert cfg["knowledge"]["git"]["auto_pull"] is True
    assert cfg["knowledge"]["git"]["auto_commit"] is True
    assert cfg["knowledge"]["git"]["auto_push"] is False
    assert cfg["knowledge"]["curation"]["gate"] == "manual"
    assert cfg["knowledge"]["project_nav"]["enabled"] is True
    assert cfg["knowledge"]["project_nav"]["maintain_during_task"] is True
    assert cfg["knowledge"]["retrieval"]["inject_mode"] == "references"
    assert cfg["knowledge"]["retrieval"]["summary_chars"] == 120


def test_load_config_deep_merges_partial_knowledge(tmp_path: Path):
    root = tmp_path / ".aha"
    write_json(
        config_path(root),
        {"knowledge": {"enabled": True, "git": {"remote": "git@github.com:u/kb.git"}}},
    )
    cfg = load_config(root)
    kb = cfg["knowledge"]
    # overridden values
    assert kb["enabled"] is True
    assert kb["git"]["remote"] == "git@github.com:u/kb.git"
    # untouched defaults preserved through the deep merge
    assert kb["git"]["branch"] == "main"
    assert kb["git"]["auto_push"] is False
    assert kb["curation"]["gate"] == "manual"
    assert kb["project_nav"]["enabled"] is True
    assert kb["retrieval"]["inject_mode"] == "references"
    assert kb["retrieval"]["max_entries"] == 5


# --------------------------------------------------------------------------- #
# Identity helpers
# --------------------------------------------------------------------------- #
def test_slugify_ascii_and_non_ascii():
    assert slugify("Serial Bridge Lifecycle!") == "serial-bridge-lifecycle"
    # Non-ASCII collapses to a stable hash-based slug, never empty.
    slug = slugify("知识库沉淀")
    assert slug.startswith("kb-")
    assert slug == slugify("知识库沉淀")


def test_normalize_git_remote_equivalence():
    ssh = normalize_git_remote("git@github.com:user/repo.git")
    https = normalize_git_remote("https://github.com/user/repo")
    assert ssh == https == "github.com/user/repo"


def _make_git_workspace(path: Path, remote: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git_dir = path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        f'[core]\n[remote "origin"]\n\turl = {remote}\n', encoding="utf-8"
    )
    return path


def test_project_key_stable_across_paths_for_same_remote(tmp_path: Path):
    ws_a = _make_git_workspace(tmp_path / "a", "git@github.com:user/repo.git")
    ws_b = _make_git_workspace(tmp_path / "b", "https://github.com/user/repo")
    key_a = project_key(ws_a)
    key_b = project_key(ws_b)
    assert key_a == key_b
    assert key_a.startswith("repo-git-")
    aliases = project_key_aliases(ws_a)
    assert aliases[0] == key_a
    assert aliases[1].startswith("git-")


def test_project_key_falls_back_without_git(tmp_path: Path):
    ws = tmp_path / "plain"
    ws.mkdir()
    key = project_key(ws, goal="my goal")
    assert key.startswith("ws-")
    # Deterministic for the same workspace.
    assert key == project_key(ws, goal="my goal")


def test_project_key_fallback_is_migratable(tmp_path: Path):
    # Same project (dir name + goal) at two different absolute paths -> same key,
    # because the fallback must not encode the absolute path.
    (tmp_path / "loc-a" / "proj").mkdir(parents=True)
    (tmp_path / "loc-b" / "proj").mkdir(parents=True)
    key_a = project_key(tmp_path / "loc-a" / "proj", goal="g")
    key_b = project_key(tmp_path / "loc-b" / "proj", goal="g")
    assert key_a == key_b
    # Different dir name yields a different key.
    (tmp_path / "other").mkdir()
    assert project_key(tmp_path / "other", goal="g") != key_a


# --------------------------------------------------------------------------- #
# Frontmatter codec
# --------------------------------------------------------------------------- #
def test_frontmatter_round_trip():
    meta = {"id": "kb_1", "type": "solution", "tags": ["a", "b"]}
    body = "## Problem\nsomething\n"
    text = serialize_entry(meta, body)
    parsed_meta, parsed_body = parse_entry(text)
    assert parsed_meta == meta
    assert parsed_body == "## Problem\nsomething"


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def test_init_is_idempotent_and_builds_layout(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = load_config(root)
    first = init_knowledge_base(root, cfg)
    assert first["created"] is True
    kb_root = knowledge_root(root, cfg)
    assert (kb_root / "general" / "wiki").is_dir()
    assert (kb_root / "general" / "solutions").is_dir()
    assert (kb_root / "projects").is_dir()
    assert (kb_root / "aha-knowledge.json").is_file()
    assert (kb_root / "README.md").is_file()
    gitignore = (kb_root / ".gitignore").read_text(encoding="utf-8")
    assert ".pending/" in gitignore
    assert ".capture/" in gitignore
    assert ".nav_drafts/" in gitignore

    index_before = json.loads((kb_root / "aha-knowledge.json").read_text())
    second = init_knowledge_base(root, cfg)
    assert second["created"] is False
    # Idempotent: index untouched on re-init.
    assert json.loads((kb_root / "aha-knowledge.json").read_text()) == index_before


def test_init_updates_existing_knowledge_gitignore(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = load_config(root)
    kb_root = knowledge_root(root, cfg)
    kb_root.mkdir(parents=True)
    (kb_root / ".gitignore").write_text(".pending/\n", encoding="utf-8")

    init_knowledge_base(root, cfg)

    gitignore = (kb_root / ".gitignore").read_text(encoding="utf-8")
    assert ".pending/" in gitignore
    assert ".capture/" in gitignore
    assert ".nav_drafts/" in gitignore


def test_write_read_list_entry(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = load_config(root)
    init_knowledge_base(root, cfg)
    path = write_entry(
        root,
        config=cfg,
        scope="project",
        kind="solutions",
        project_key_value="git-abc123",
        title="Fix zipapp ModuleNotFound",
        body="## Problem\n...\n## Fix\n...",
        meta={"outcome": "success", "tags": ["build"]},
    )
    assert path.exists()
    entry = read_entry(path)
    assert entry["meta"]["title"] == "Fix zipapp ModuleNotFound"
    assert entry["meta"]["scope"] == "project"
    assert entry["meta"]["project_key"] == "git-abc123"
    assert entry["meta"]["type"] == "solution"
    assert entry["meta"]["outcome"] == "success"

    entries = list_entries(
        root, config=cfg, scope="project", kind="solutions", project_key_value="git-abc123"
    )
    assert len(entries) == 1
    assert entries[0]["meta"]["slug"] == path.stem


def test_rewrite_preserves_created_at(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = load_config(root)
    init_knowledge_base(root, cfg)
    p1 = write_entry(
        root, config=cfg, scope="general", kind="wiki", title="Topic", body="v1"
    )
    created_at = read_entry(p1)["meta"]["created_at"]
    p2 = write_entry(
        root, config=cfg, scope="general", kind="wiki", title="Topic", body="v2"
    )
    assert p1 == p2  # same slug -> same file
    again = read_entry(p2)
    assert again["meta"]["created_at"] == created_at
    assert again["body"] == "v2"


def test_entry_has_stable_kb_id_preserved_on_rewrite(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = load_config(root)
    init_knowledge_base(root, cfg)
    p1 = write_entry(root, config=cfg, scope="general", kind="wiki", title="Topic", body="v1")
    first_id = read_entry(p1)["meta"]["id"]
    assert first_id.startswith("kb_")
    p2 = write_entry(root, config=cfg, scope="general", kind="wiki", title="Topic", body="v2")
    assert read_entry(p2)["meta"]["id"] == first_id  # preserved across rewrite
    # Different identity (scope) -> different id.
    p3 = write_entry(root, config=cfg, scope="project", kind="navigation",
                     project_key_value="git-x", title="Topic", body="v1", slug="index")
    assert read_entry(p3)["meta"]["id"] != first_id


def test_status_counts(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = load_config(root)
    init_knowledge_base(root, cfg)
    write_entry(root, config=cfg, scope="general", kind="wiki", title="A", body="x")
    write_entry(
        root, config=cfg, scope="project", kind="solutions",
        project_key_value="git-xyz", title="B", body="y",
    )
    status = knowledge_status(root, cfg)
    assert status["initialized"] is True
    assert status["general"]["wiki"] == 1
    assert status["total_entries"] == 2
    assert any(p["project_key"] == "git-xyz" for p in status["projects"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_kb_init_and_status(tmp_path: Path):
    home = str(tmp_path / ".aha")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["--home", home, "kb", "init", "--json"])
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["created"] is True
    assert Path(payload["path"]).is_dir()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["--home", home, "kb", "status", "--json"])
    assert rc == 0
    status = json.loads(out.getvalue())
    assert status["initialized"] is True
    assert status["total_entries"] == 0
    assert status["curation_gate"] == "manual"
    assert status["project_nav"]["enabled"] is True
