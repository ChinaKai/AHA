from __future__ import annotations

import contextlib
import io
import json
import subprocess
from pathlib import Path

import pytest

from aha_cli.cli import main
from aha_cli.domain.models import default_config
from aha_cli.store.config import load_config
from aha_cli.store.io import read_json, write_json
from aha_cli.store.knowledge import (
    enqueue_candidate,
    init_knowledge_base,
    knowledge_root,
    knowledge_status,
    list_entries,
    list_pending,
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
from aha_cli.store.project_identity import (
    PROJECT_IDENTITY_SCHEMA_VERSION,
    ProjectIdentityConflict,
    ProjectIdentityError,
    bind_project_identity,
    create_project_identity,
    git_workspace_facts,
    local_project_bindings_path,
    merge_project_identities,
    migrate_project_identity_manifests,
    project_manifest_path,
    read_project_manifest,
    resolve_project_identity,
    unbind_project_identity,
    update_project_relations,
)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_default_config_has_knowledge_block():
    cfg = default_config()
    # Knowledge base is enabled by default (default-required core feature).
    assert cfg["knowledge"]["enabled"] is True
    assert cfg["knowledge"]["git"]["enabled"] is True
    assert cfg["knowledge"]["git"]["auto_pull"] is True
    assert cfg["knowledge"]["git"]["auto_commit"] is True
    assert cfg["knowledge"]["git"]["auto_push"] is True
    assert cfg["knowledge"]["curation"]["gate"] == "agent-auto"
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
    assert kb["git"]["auto_push"] is True
    assert kb["curation"]["gate"] == "agent-auto"
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
    ssh_443 = normalize_git_remote("ssh://git@ssh.github.com:443/user/repo.git")
    assert ssh == https == ssh_443 == "github.com/user/repo"


def _make_git_workspace(path: Path, remote: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git_dir = path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        f'[core]\n[remote "origin"]\n\turl = {remote}\n', encoding="utf-8"
    )
    return path


def _make_real_git_workspace(path: Path, remote: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    (path / "README.md").write_text("root\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)
    return path


def test_project_key_stable_across_paths_for_same_remote(tmp_path: Path):
    ws_a = _make_git_workspace(tmp_path / "a", "git@github.com:user/repo.git")
    ws_b = _make_git_workspace(tmp_path / "b", "https://github.com/user/repo")
    ws_c = _make_git_workspace(tmp_path / "c", "ssh://git@ssh.github.com:443/user/repo.git")
    key_a = project_key(ws_a)
    key_b = project_key(ws_b)
    key_c = project_key(ws_c)
    assert key_a == key_b == key_c
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


def test_non_git_workspace_can_bind_to_project_in_local_aha_state(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = load_config(home)
    init_knowledge_base(home, cfg)
    kb_root = knowledge_root(home, cfg)
    target_key = "stable-project"
    (kb_root / "projects" / target_key).mkdir(parents=True)
    workspace = tmp_path / "plain-workspace"
    workspace.mkdir()

    bound = bind_project_identity(
        kb_root,
        workspace,
        target_key,
        aha_root=home,
    )
    local_state = read_json(local_project_bindings_path(home))

    assert bound["source"] == "local_binding"
    assert bound["project_key"] == target_key
    assert bound["git_identity"] == ""
    assert local_state["schema_version"] == 2
    assert local_state["bindings"][0]["binding_mode"] == "fallback"
    assert local_state["bindings"][0]["workspace_path"] == str(workspace.resolve())
    assert local_state["bindings"][0]["project_key"] == target_key
    assert resolve_project_identity(
        kb_root,
        workspace,
        aha_root=home,
    )["project_key"] == target_key
    assert resolve_project_identity(kb_root, workspace)["source"] == "workspace_fallback"
    assert read_project_manifest(kb_root, target_key)["git_identities"] == []
    assert not (workspace / ".aha").exists()
    assert not (workspace / "project.json").exists()


def test_git_manifest_takes_priority_over_stale_local_workspace_binding(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = load_config(home)
    init_knowledge_base(home, cfg)
    kb_root = knowledge_root(home, cfg)
    for key in ("local-project", "git-project"):
        (kb_root / "projects" / key).mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bind_project_identity(
        kb_root,
        workspace,
        "local-project",
        aha_root=home,
    )

    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text(
        '[remote "origin"]\n  url = git@github.com:user/git-project.git\n',
        encoding="utf-8",
    )
    bind_project_identity(
        kb_root,
        workspace,
        "git-project",
        aha_root=home,
    )

    resolved = resolve_project_identity(kb_root, workspace, aha_root=home)
    assert resolved["source"] == "manifest"
    assert resolved["project_key"] == "git-project"


def test_v2_project_manifest_is_projected_as_v3(tmp_path: Path):
    kb_root = tmp_path / "knowledge"
    path = project_manifest_path(kb_root, "legacy-project")
    write_json(path, {
        "schema_version": 2,
        "project_key": "legacy-project",
        "display_name": "Legacy",
        "git_identities": ["git@github.com:user/legacy.git"],
        "legacy_keys": ["git-old"],
        "related_projects": [],
    })

    manifest = read_project_manifest(kb_root, "legacy-project")

    assert manifest["schema_version"] == 3
    assert manifest["project_id"].startswith("prj_")
    assert manifest["bindings"][0]["kind"] == "git"
    assert manifest["bindings"][0]["remote"] == "github.com/user/legacy"
    assert manifest["git_identities"] == ["github.com/user/legacy"]
    assert manifest["aliases"] == ["git-old"]
    assert migrate_project_identity_manifests(kb_root) == [path]
    migrated = read_json(path)
    assert migrated["schema_version"] == 3
    assert migrated["project_id"] == manifest["project_id"]
    assert migrated["bindings"][0]["binding_id"] == manifest["bindings"][0]["binding_id"]


def test_git_plumbing_supports_worktree_and_subpath(tmp_path: Path):
    repo = _make_real_git_workspace(
        tmp_path / "repo",
        "git@github.com:user/monorepo.git",
    )
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "feature", str(worktree)],
        check=True,
        capture_output=True,
    )
    nested = worktree / "products" / "camera"
    nested.mkdir(parents=True)

    facts = git_workspace_facts(nested)

    assert facts["is_git"] is True
    assert facts["repo_root"] == str(worktree.resolve())
    assert facts["git_dir"]
    assert facts["git_identity"] == "github.com/user/monorepo"
    assert facts["repository_fingerprint"].startswith("roots:")
    assert facts["subpath"] == "products/camera"


def test_monorepo_subpaths_can_bind_to_distinct_projects(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = load_config(home)
    init_knowledge_base(home, cfg)
    kb_root = knowledge_root(home, cfg)
    repo = _make_real_git_workspace(
        tmp_path / "repo",
        "git@github.com:user/monorepo.git",
    )
    camera = repo / "products" / "camera"
    cloud = repo / "products" / "cloud"
    camera.mkdir(parents=True)
    cloud.mkdir(parents=True)
    for key in ("camera-project", "cloud-project"):
        (kb_root / "projects" / key).mkdir(parents=True)

    bind_project_identity(kb_root, camera, "camera-project")
    bind_project_identity(kb_root, cloud, "cloud-project")

    camera_identity = resolve_project_identity(kb_root, camera)
    cloud_identity = resolve_project_identity(kb_root, cloud)
    assert camera_identity["project_key"] == "camera-project"
    assert cloud_identity["project_key"] == "cloud-project"
    assert camera_identity["matched_by"] == ["remote", "repository_fingerprint", "subpath"]
    assert [item["project_key"] for item in camera_identity["alternatives"]] == [
        "camera-project"
    ]


def test_explicit_local_binding_can_override_git_binding(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = load_config(home)
    init_knowledge_base(home, cfg)
    kb_root = knowledge_root(home, cfg)
    workspace = _make_git_workspace(
        tmp_path / "workspace",
        "git@github.com:user/repo.git",
    )
    for key in ("shared-project", "local-project"):
        (kb_root / "projects" / key).mkdir(parents=True)

    bind_project_identity(kb_root, workspace, "shared-project")
    bind_project_identity(
        kb_root,
        workspace,
        "local-project",
        aha_root=home,
        binding_scope="local",
    )

    resolved = resolve_project_identity(kb_root, workspace, aha_root=home)
    assert resolved["source"] == "local_binding"
    assert resolved["project_key"] == "local-project"
    assert resolved["binding"]["binding_mode"] == "explicit"


def test_unbind_non_git_workspace_removes_only_local_mapping(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = load_config(home)
    init_knowledge_base(home, cfg)
    kb_root = knowledge_root(home, cfg)
    (kb_root / "projects" / "stable-project").mkdir(parents=True)
    workspace = tmp_path / "plain-workspace"
    workspace.mkdir()
    bind_project_identity(
        kb_root,
        workspace,
        "stable-project",
        aha_root=home,
    )

    result = unbind_project_identity(kb_root, workspace, aha_root=home)

    assert result["source"] == "workspace_fallback"
    assert result["unbound_project_key"] == "stable-project"
    assert result["binding_scope"] == "local"
    assert result["synced_changed"] is False
    assert read_json(local_project_bindings_path(home))["bindings"] == []
    assert read_project_manifest(kb_root, "stable-project") is not None


def test_unbind_git_workspace_preserves_other_identities_and_project_metadata(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = load_config(home)
    init_knowledge_base(home, cfg)
    kb_root = knowledge_root(home, cfg)
    for key in ("stable-project", "related-project"):
        (kb_root / "projects" / key).mkdir(parents=True)
    workspace = _make_git_workspace(
        tmp_path / "workspace", "git@github.com:user/stable-project.git"
    )
    mirror = _make_git_workspace(
        tmp_path / "mirror", "git@gitlab.com:user/stable-project.git"
    )
    bind_project_identity(kb_root, workspace, "stable-project")
    bind_project_identity(kb_root, mirror, "stable-project")
    update_project_relations(
        kb_root,
        "stable-project",
        [{"project_key": "related-project", "relation": "reference", "note": "Docs"}],
    )

    result = unbind_project_identity(kb_root, workspace, aha_root=home)
    manifest = read_project_manifest(kb_root, "stable-project")

    assert result["source"] == "derived_git"
    assert result["binding_scope"] == "shared"
    assert result["synced_changed"] is True
    assert manifest is not None
    assert manifest["git_identities"] == ["gitlab.com/user/stable-project"]
    assert manifest["related_projects"] == [
        {"project_key": "related-project", "relation": "reference", "note": "Docs"}
    ]
    assert resolve_project_identity(kb_root, mirror)["source"] == "manifest"


def test_shared_binding_can_be_reactivated_at_same_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / ".aha"
    cfg = load_config(home)
    init_knowledge_base(home, cfg)
    kb_root = knowledge_root(home, cfg)
    (kb_root / "projects" / "stable-project").mkdir(parents=True)
    workspace = _make_git_workspace(
        tmp_path / "workspace", "git@github.com:user/stable-project.git"
    )
    monkeypatch.setattr(
        "aha_cli.store.project_identity.utc_now",
        lambda: "2026-08-21T15:01:07+00:00",
    )

    first = bind_project_identity(kb_root, workspace, "stable-project")
    unbind_project_identity(kb_root, workspace, aha_root=home)
    rebound = bind_project_identity(kb_root, workspace, "stable-project")

    assert rebound["source"] == "manifest"
    assert rebound["binding"]["binding_id"] == first["binding"]["binding_id"]
    assert rebound["binding"]["active"] is True
    assert rebound["binding"]["removed_at"] == ""


def test_synced_project_manifest_overrides_changed_remote(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = load_config(home)
    init_knowledge_base(home, cfg)
    kb_root = knowledge_root(home, cfg)
    target_key = "stable-project"
    (kb_root / "projects" / target_key).mkdir(parents=True)
    old_workspace = _make_git_workspace(
        tmp_path / "old-workspace", "git@github.com:user/old-name.git"
    )
    new_workspace = _make_git_workspace(
        tmp_path / "new-workspace", "git@github.com:user/new-name.git"
    )

    bind_project_identity(kb_root, old_workspace, target_key)
    bind_project_identity(kb_root, new_workspace, target_key)

    resolved = resolve_project_identity(kb_root, new_workspace)
    assert resolved["source"] == "manifest"
    assert resolved["project_key"] == target_key
    assert resolved["git_identity"] == "github.com/user/new-name"
    assert project_manifest_path(kb_root, target_key).is_file()
    # The original repositories contain only the test-created Git metadata;
    # project identity is written exclusively under the synchronized KB.
    assert not (old_workspace / "project.json").exists()
    assert not (new_workspace / "project.json").exists()


def test_git_identity_cannot_bind_two_knowledge_projects(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = load_config(home)
    init_knowledge_base(home, cfg)
    kb_root = knowledge_root(home, cfg)
    for key in ("project-a", "project-b"):
        (kb_root / "projects" / key).mkdir(parents=True)
    workspace = _make_git_workspace(
        tmp_path / "workspace", "https://github.com/user/repo"
    )

    bind_project_identity(kb_root, workspace, "project-a")
    with pytest.raises(ProjectIdentityConflict):
        bind_project_identity(kb_root, workspace, "project-b")


def test_explicit_bind_can_resolve_ambiguous_shared_identity(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = load_config(home)
    init_knowledge_base(home, cfg)
    kb_root = knowledge_root(home, cfg)
    for key in ("project-a", "project-b"):
        (kb_root / "projects" / key).mkdir(parents=True)
    workspace = _make_git_workspace(
        tmp_path / "workspace",
        "https://github.com/user/repo",
    )
    bind_project_identity(kb_root, workspace, "project-a")
    manifest_a = read_project_manifest(kb_root, "project-a")
    write_json(project_manifest_path(kb_root, "project-b"), {
        **{key: value for key, value in manifest_a.items() if key != "path"},
        "project_id": "prj_duplicate",
        "project_key": "project-b",
        "slug": "project-b",
        "display_name": "Project B",
    })

    ambiguous = resolve_project_identity(kb_root, workspace)
    rebound = bind_project_identity(
        kb_root,
        workspace,
        "project-a",
        resolve_conflicts=True,
    )

    assert ambiguous["source"] == "ambiguous"
    assert ambiguous["ambiguous_project_keys"] == ["project-a", "project-b"]
    assert rebound["source"] == "manifest"
    assert rebound["project_key"] == "project-a"
    assert all(
        binding["active"] is False
        for binding in read_project_manifest(kb_root, "project-b")["bindings"]
    )


def test_project_manifest_stores_validated_related_projects_and_preserves_them_on_rebind(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = load_config(home)
    init_knowledge_base(home, cfg)
    kb_root = knowledge_root(home, cfg)
    for key in ("project-a", "project-b", "project-c"):
        (kb_root / "projects" / key).mkdir(parents=True)
    workspace = _make_git_workspace(
        tmp_path / "workspace", "https://github.com/user/project-a"
    )
    mirror = _make_git_workspace(
        tmp_path / "mirror", "https://gitlab.com/user/project-a"
    )
    bind_project_identity(kb_root, workspace, "project-a")

    updated = update_project_relations(
        kb_root,
        "project-a",
        [
            {"project_key": "project-b", "relation": "upstream", "note": "Base implementation"},
            {"project_key": "project-c", "relation": "sdk", "note": "  Vendor   SDK  "},
        ],
    )
    bind_project_identity(kb_root, mirror, "project-a")
    manifest = read_project_manifest(kb_root, "project-a")

    assert updated["schema_version"] == PROJECT_IDENTITY_SCHEMA_VERSION
    assert manifest is not None
    assert manifest["related_projects"] == [
        {"project_key": "project-b", "relation": "upstream", "note": "Base implementation"},
        {"project_key": "project-c", "relation": "sdk", "note": "Vendor SDK"},
    ]
    assert len(manifest["git_identities"]) == 2

    with pytest.raises(ProjectIdentityError, match="cannot reference itself"):
        update_project_relations(
            kb_root,
            "project-a",
            [{"project_key": "project-a", "relation": "reference"}],
        )
    with pytest.raises(ProjectIdentityError, match="unknown project relation"):
        update_project_relations(
            kb_root,
            "project-a",
            [{"project_key": "project-b", "relation": "generated"}],
        )


def test_merge_projects_preserves_conflicts_and_leaves_redirect(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = load_config(home)
    init_knowledge_base(home, cfg)
    kb_root = knowledge_root(home, cfg)
    create_project_identity(kb_root, "source-project")
    create_project_identity(kb_root, "target-project")
    write_entry(
        home,
        config=cfg,
        scope="project",
        kind="solutions",
        project_key_value="source-project",
        title="Source only",
        body="source body",
        slug="source-only",
    )
    write_entry(
        home,
        config=cfg,
        scope="project",
        kind="solutions",
        project_key_value="source-project",
        title="Collision",
        body="source collision",
        slug="collision",
    )
    write_entry(
        home,
        config=cfg,
        scope="project",
        kind="solutions",
        project_key_value="target-project",
        title="Collision",
        body="target collision",
        slug="collision",
    )

    preview = merge_project_identities(
        kb_root,
        "source-project",
        "target-project",
        aha_root=home,
        dry_run=True,
    )
    applied = merge_project_identities(
        kb_root,
        "source-project",
        "target-project",
        aha_root=home,
        dry_run=False,
    )

    assert preview["move_count"] == 1
    assert preview["conflict_count"] == 1
    assert applied["applied"] is True
    assert read_project_manifest(kb_root, "source-project")["redirect_to"] == "target-project"
    moved = list_entries(
        home,
        config=cfg,
        scope="project",
        kind="solutions",
        project_key_value="source-project",
    )
    assert {entry["meta"]["slug"] for entry in moved} == {"source-only", "collision"}
    archive = kb_root / "projects" / "target-project" / ".merge_conflicts" / "source-project" / "solutions" / "collision.md"
    assert archive.is_file()


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
    assert not (kb_root / "general" / "navigation").exists()
    assert (kb_root / "personal" / "wiki").is_dir()
    assert (kb_root / "personal" / "solutions").is_dir()
    assert not (kb_root / "personal" / "navigation").exists()
    assert (kb_root / "projects").is_dir()
    assert (kb_root / "aha-knowledge.json").is_file()
    assert (kb_root / "README.md").is_file()
    assert (kb_root / ".gitattributes").read_text(encoding="utf-8").splitlines()[-1] == "* text=auto eol=lf"
    gitignore = (kb_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".pending/" in gitignore
    assert "capture/distill/" in gitignore
    assert ".capture/distill/" in gitignore
    assert ".capture/" not in gitignore
    assert "capture/" not in gitignore
    assert ".nav_drafts/" in gitignore

    index_before = json.loads((kb_root / "aha-knowledge.json").read_text())
    second = init_knowledge_base(root, cfg)
    assert second["created"] is False
    # Idempotent: index untouched on re-init.
    assert json.loads((kb_root / "aha-knowledge.json").read_text()) == index_before


def test_init_preserves_custom_gitattributes_and_keeps_aha_rule_last(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = load_config(root)
    kb_root = knowledge_root(root, cfg)
    kb_root.mkdir(parents=True)
    attributes = kb_root / ".gitattributes"
    attributes.write_text("*.cmd text eol=crlf\n* -text\n", encoding="utf-8")

    first = init_knowledge_base(root, cfg)
    second = init_knowledge_base(root, cfg)

    lines = attributes.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["*.cmd text eol=crlf", "* -text"]
    assert lines[-2:] == ["# AHA managed cross-platform text normalization", "* text=auto eol=lf"]
    assert first["gitattributes_updated"] is True
    assert second["gitattributes_updated"] is False


def test_init_updates_existing_knowledge_gitignore(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = load_config(root)
    kb_root = knowledge_root(root, cfg)
    kb_root.mkdir(parents=True)
    (kb_root / ".gitignore").write_text(".pending/\n.capture/\ncapture/\n", encoding="utf-8")

    init_knowledge_base(root, cfg)

    gitignore = (kb_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".pending/" in gitignore
    assert "capture/distill/" in gitignore
    assert ".capture/distill/" in gitignore
    assert ".capture/" not in gitignore
    assert "capture/" not in gitignore
    assert ".nav_drafts/" in gitignore


def test_navigation_entries_are_project_scoped(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = load_config(root)
    init_knowledge_base(root, cfg)

    assert list_entries(root, config=cfg, scope="general", kind="navigation") == []
    assert list_entries(root, config=cfg, scope="personal", kind="navigation") == []
    with pytest.raises(ValueError):
        write_entry(root, config=cfg, scope="personal", kind="navigation", title="Nav", body="x")

    path = write_entry(
        root,
        config=cfg,
        scope="project",
        kind="navigation",
        project_key_value="git-abc",
        title="Project nav",
        body="x",
        slug="index",
    )
    assert path.exists()


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
    assert status["curation_gate"] == "agent-auto"
    assert status["project_nav"]["enabled"] is True


# --------------------------------------------------------------------------- #
# Wikilinks (Obsidian [[...]])
# --------------------------------------------------------------------------- #
def test_extract_wikilinks_basic():
    from aha_cli.store.knowledge import extract_wikilinks

    body = "See [[cross-os-liveness]] and [[WSL 后端|alias]] plus [[cross-os-liveness]] again."
    targets = extract_wikilinks(body)
    assert targets == ["cross-os-liveness", "WSL 后端"]


def test_write_entry_maintains_links_and_backlinks(tmp_path: Path):
    from aha_cli.store.knowledge import extract_wikilinks, rebuild_wikilinks

    home = tmp_path / ".aha"
    cfg = {"knowledge": {"enabled": True}}
    init_knowledge_base(home, cfg)

    # Entry B links to entry A via wikilink.
    path_a = write_entry(
        home, config=cfg, scope="general", kind="wiki", title="跨 OS 存活",
        body="判断跨 OS 进程存活。",
        slug="cross-os-liveness",
    )
    path_b = write_entry(
        home, config=cfg, scope="general", kind="wiki", title="WSL 后端",
        body="参考 [[cross-os-liveness]] 判断进程。",
        slug="wsl-backend",
    )

    entry_a = read_entry(path_a)
    entry_b = read_entry(path_b)
    # B links to A: B.links contains A, and A.backlinks contains B.
    assert "cross-os-liveness" in entry_b["meta"].get("links", [])
    assert "wsl-backend" in entry_a["meta"].get("backlinks", [])

    # rebuild is idempotent: a second pass changes nothing.
    result = rebuild_wikilinks(home, cfg)
    assert result["total"] >= 2
    assert result["updated"] == 0


def test_kb_links_cli_rebuilds_index(tmp_path: Path):
    from aha_cli.store.knowledge import read_entry, write_entry

    home = tmp_path / ".aha"
    cfg = {"knowledge": {"enabled": True}}
    init_knowledge_base(home, cfg)
    write_entry(
        home, config=cfg, scope="general", kind="wiki", title="源笔记",
        body="链接到 [[target-note]]。",
        slug="source-note",
    )
    write_entry(
        home, config=cfg, scope="general", kind="wiki", title="目标笔记",
        body="正文。",
        slug="target-note",
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["--home", str(home), "kb", "links"])
    assert rc == 0
    assert "wikilink index rebuilt" in out.getvalue()
    # source entry should have target-note in its links.
    from aha_cli.store.knowledge import list_entries

    entries = {e["meta"]["slug"]: e for e in list_entries(home, config=cfg, scope="general", kind="wiki")}
    assert "target-note" in entries["source-note"]["meta"].get("links", [])


def test_enqueue_merges_similar_candidates_by_title(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = {"knowledge": {"enabled": True}}
    init_knowledge_base(home, cfg)

    def enq(title, tags, src):
        return enqueue_candidate(home, cfg, {
            "kind": "solutions", "scope": "project", "project_key": "git-abc",
            "title": title, "body": f"body {title}", "meta": {"tags": tags, "confidence": 0.7},
            "source": {"source_type": "task_final", **src},
        })

    p1 = enq("WSL python3 shim trap", ["wsl"], {"run_id": "r1", "task_id": "t1"})
    p2 = enq("WSL python3 shim 陷阱", ["wsl"], {"run_id": "r2", "task_id": "t2"})
    # Near-duplicate (title Jaccard 0.6 + shared tag) merges into the same file.
    assert p1 == p2
    pending = list_pending(home, cfg)
    assert len(pending) == 1
    merged = pending[0]
    assert len(merged.get("sources") or []) == 2
    # Bodies are preserved side by side with a merge marker.
    assert "WSL python3 shim trap" in merged["body"]
    assert "WSL python3 shim 陷阱" in merged["body"]


def test_enqueue_keeps_distinct_candidates_with_shared_tag(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = {"knowledge": {"enabled": True}}
    init_knowledge_base(home, cfg)

    def enq(title, src):
        return enqueue_candidate(home, cfg, {
            "kind": "solutions", "scope": "project", "project_key": "git-abc",
            "title": title, "body": f"body {title}", "meta": {"tags": ["wyze"], "confidence": 0.7},
            "source": {"source_type": "task_final", **src},
        })

    p1 = enq("Wyze 云存上传双路视频排查要点", {"run_id": "r1", "task_id": "t1"})
    p2 = enq("Wyze 云存业务层时间戳对齐逻辑", {"run_id": "r2", "task_id": "t2"})
    # Distinct solutions sharing one generic tag stay separate.
    assert p1 != p2
    assert len(list_pending(home, cfg)) == 2


def test_enqueue_merges_distinct_project_keys_never(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = {"knowledge": {"enabled": True}}
    init_knowledge_base(home, cfg)
    base = {
        "kind": "solutions", "scope": "project", "title": "WSL python3 shim trap",
        "body": "body", "meta": {"tags": ["wsl"], "confidence": 0.7},
        "source": {"source_type": "task_final", "run_id": "r1", "task_id": "t1"},
    }
    p1 = enqueue_candidate(home, cfg, {**base, "project_key": "git-a"})
    p2 = enqueue_candidate(home, cfg, {**base, "project_key": "git-b"})
    assert p1 != p2
    assert len(list_pending(home, cfg)) == 2
