from __future__ import annotations

import contextlib
import io
import json
import subprocess
from pathlib import Path

import pytest

from aha_cli.cli import main
from aha_cli.domain.models import default_knowledge_config
from aha_cli.services import knowledge_git as kg
from aha_cli.store.knowledge import knowledge_root, write_entry

pytestmark = pytest.mark.skipif(not kg.git_available(), reason="git not available")


def _config(remote: str | None = None, **git_overrides) -> dict:
    kb = default_knowledge_config()
    kb["enabled"] = True
    kb["git"]["enabled"] = True
    if remote is not None:
        kb["git"]["remote"] = remote
    kb["git"].update(git_overrides)
    return {"knowledge": kb}


def _bare_remote(path: Path) -> str:
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)], check=True, capture_output=True)
    return str(path)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


# --------------------------------------------------------------------------- #
def test_ensure_repo_inits_branch_and_remote(tmp_path: Path):
    root = tmp_path / ".aha"
    remote = _bare_remote(tmp_path / "remote.git")
    cfg = _config(remote)
    res = kg.ensure_repo(root, cfg)
    assert res["ok"] and res["created"] is True
    repo = knowledge_root(root, cfg)
    assert kg.is_repo(repo)
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(repo, "remote", "get-url", "origin") == remote
    # Idempotent second call.
    again = kg.ensure_repo(root, cfg)
    assert again["created"] is False
    assert again["remote_state"] == "unchanged"


def test_commit_all_commits_then_noop(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _config()
    first = kg.commit_all(root, "init kb", cfg)
    assert first["ok"] and first["committed"] is True
    # Nothing changed -> no-op.
    second = kg.commit_all(root, "again", cfg)
    assert second["ok"] and second["committed"] is False
    # New entry -> commit again.
    write_entry(root, config=cfg, scope="general", kind="wiki", title="T", body="b")
    third = kg.commit_all(root, "add entry", cfg)
    assert third["committed"] is True


def test_push_then_clone_sees_content(tmp_path: Path):
    root = tmp_path / ".aha"
    remote = _bare_remote(tmp_path / "remote.git")
    cfg = _config(remote)
    write_entry(root, config=cfg, scope="general", kind="solutions", title="Sol", body="x")
    assert kg.commit_all(root, "init", cfg)["committed"] is True
    assert kg.push(root, cfg)["pushed"] is True

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", remote, str(clone)], check=True, capture_output=True)
    assert (clone / "general" / "solutions" / "sol.md").exists()


def test_pull_picks_up_remote_changes(tmp_path: Path):
    root = tmp_path / ".aha"
    remote = _bare_remote(tmp_path / "remote.git")
    cfg = _config(remote)
    kg.commit_all(root, "init", cfg)
    kg.push(root, cfg)

    # Another clone adds a file and pushes.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", remote, str(other)], check=True, capture_output=True)
    (other / "general" / "wiki").mkdir(parents=True, exist_ok=True)
    (other / "general" / "wiki" / "new.md").write_text("hi", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote change")
    _git(other, "push", "origin", "main")

    res = kg.pull(root, cfg)
    assert res["ok"] and res["pulled"] is True
    assert (knowledge_root(root, cfg) / "general" / "wiki" / "new.md").exists()


def test_pull_conflict_aborts_cleanly(tmp_path: Path):
    root = tmp_path / ".aha"
    remote = _bare_remote(tmp_path / "remote.git")
    cfg = _config(remote)
    repo = knowledge_root(root, cfg)
    kg.commit_all(root, "init", cfg)
    kg.push(root, cfg)

    # Remote diverges on README.md.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", remote, str(other)], check=True, capture_output=True)
    (other / "README.md").write_text("REMOTE VERSION\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote readme")
    _git(other, "push", "origin", "main")

    # Local diverges on the same file.
    (repo / "README.md").write_text("LOCAL VERSION\n", encoding="utf-8")
    kg.commit_all(root, "local readme", cfg)
    local_head = _git(repo, "rev-parse", "HEAD")

    res = kg.pull(root, cfg)
    assert res["ok"] is False and res.get("conflict") is True
    # Repo is not left mid-rebase, and local HEAD is intact.
    assert not (repo / ".git" / "rebase-merge").exists()
    assert not (repo / ".git" / "rebase-apply").exists()
    assert _git(repo, "rev-parse", "HEAD") == local_head


def test_no_remote_pull_push_are_noops(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _config()  # no remote
    assert kg.pull(root, cfg)["pulled"] is False
    assert kg.push(root, cfg)["pushed"] is False


def test_sync_commits_local_dirty_then_rebases_and_pushes(tmp_path: Path):
    # Key boundary: remote has new commits AND the local KB has uncommitted
    # changes. sync must commit local first, rebase onto remote, then push.
    root = tmp_path / ".aha"
    remote = _bare_remote(tmp_path / "remote.git")
    cfg = _config(remote)
    kg.commit_all(root, "init", cfg)
    kg.push(root, cfg)

    # Remote advances from another clone.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", remote, str(other)], check=True, capture_output=True)
    (other / "general" / "wiki").mkdir(parents=True, exist_ok=True)
    (other / "general" / "wiki" / "remote.md").write_text("remote", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote add")
    _git(other, "push", "origin", "main")

    # Local has an uncommitted (dirty) new entry.
    write_entry(root, config=cfg, scope="general", kind="solutions", title="Local", body="z")
    repo = knowledge_root(root, cfg)
    assert _git(repo, "status", "--porcelain") != ""  # dirty before sync

    res = kg.sync(root, cfg, message="sync local", do_push=True)
    assert res["ok"] is True, res
    assert res["steps"]["commit"]["committed"] is True
    assert res["steps"]["pull"]["pulled"] is True
    assert res["steps"]["push"]["pushed"] is True

    # Both the remote change and the local change coexist locally...
    assert (repo / "general" / "wiki" / "remote.md").exists()
    assert (repo / "general" / "solutions" / "local.md").exists()
    # ...and on the remote.
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", remote, str(clone)], check=True, capture_output=True)
    assert (clone / "general" / "wiki" / "remote.md").exists()
    assert (clone / "general" / "solutions" / "local.md").exists()


def test_pull_unreachable_remote_returns_failure(tmp_path: Path):
    root = tmp_path / ".aha"
    # Remote path that does not exist -> unreachable, not "empty".
    cfg = _config(str(tmp_path / "does-not-exist.git"))
    kg.commit_all(root, "init", cfg)
    res = kg.pull(root, cfg)
    assert res["ok"] is False
    assert res["pulled"] is False
    assert "unreachable" in res["error"]


def test_pull_empty_remote_is_skipped_ok(tmp_path: Path):
    root = tmp_path / ".aha"
    remote = _bare_remote(tmp_path / "remote.git")  # reachable but no branches
    cfg = _config(remote)
    kg.commit_all(root, "init", cfg)
    res = kg.pull(root, cfg)
    assert res["ok"] is True
    assert res["pulled"] is False
    # After the skip, the first push still establishes the branch.
    assert kg.push(root, cfg)["pushed"] is True


def test_sync_unreachable_remote_cli_fails(tmp_path: Path):
    from aha_cli.store.io import write_json
    from aha_cli.store.paths import config_path

    home = tmp_path / ".aha"
    write_json(config_path(home), _config(str(tmp_path / "nope.git")))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["--home", str(home), "kb", "sync", "--push", "--json"])
    assert rc == 1
    result = json.loads(out.getvalue())
    assert result["ok"] is False
    assert result["steps"]["pull"]["ok"] is False


# --------------------------------------------------------------------------- #
# Config-gated hooks
# --------------------------------------------------------------------------- #
def test_auto_hooks_respect_flags(tmp_path: Path):
    root = tmp_path / ".aha"
    # Disabled entirely.
    disabled = {"knowledge": default_knowledge_config()}
    assert "skipped" in kg.auto_commit_after_change(root, "m", disabled)
    assert "skipped" in kg.auto_pull_before_task(root, disabled)

    # Enabled, auto_commit on.
    cfg = _config()
    write_entry(root, config=cfg, scope="general", kind="wiki", title="A", body="b")
    res = kg.auto_commit_after_change(root, "auto", cfg)
    assert res.get("committed") is True

    # auto_commit explicitly off.
    cfg_off = _config(auto_commit=False)
    assert kg.auto_commit_after_change(root, "m", cfg_off).get("skipped") == "auto_commit disabled"


def test_auto_commit_pushes_when_auto_push_on(tmp_path: Path):
    root = tmp_path / ".aha"
    remote = _bare_remote(tmp_path / "remote.git")
    cfg = _config(remote, auto_push=True)
    write_entry(root, config=cfg, scope="general", kind="wiki", title="A", body="b")
    res = kg.auto_commit_after_change(root, "auto", cfg)
    assert res.get("committed") is True
    assert res.get("push", {}).get("pushed") is True


def test_auto_commit_project_feedback_only_commits_approved_project_paths(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _config()
    assert kg.commit_all(root, "init", cfg)["committed"] is True
    repo = knowledge_root(root, cfg)

    approved = repo / "projects" / "demo" / "navigation" / "index.md"
    approved.parent.mkdir(parents=True, exist_ok=True)
    approved.write_text("# Nav\n", encoding="utf-8")
    unrelated = repo / "general" / "wiki" / "unrelated.md"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("# Unrelated\n", encoding="utf-8")
    other_project = repo / "projects" / "other" / "navigation" / "index.md"
    other_project.parent.mkdir(parents=True, exist_ok=True)
    other_project.write_text("# Other\n", encoding="utf-8")
    pending = repo / ".pending" / "candidate.json"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("{}", encoding="utf-8")

    # Simulate a pre-existing staged change; the scoped commit must not sweep it in.
    _git(repo, "add", "general/wiki/unrelated.md")
    res = kg.auto_commit_project_approved_entries_after_feedback(root, "feedback", ["demo"], cfg)

    assert res.get("committed") is True
    committed = _git(repo, "show", "--name-only", "--pretty=format:", "HEAD").splitlines()
    assert committed == ["projects/demo/navigation/index.md"]
    status = _git(repo, "status", "--porcelain")
    assert "A  general/wiki/unrelated.md" in status
    assert "?? projects/other/" in status
    assert ".pending" not in status


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_kb_sync_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".aha"
    remote = _bare_remote(tmp_path / "remote.git")
    # Write a config.json so load_config picks up the remote.
    from aha_cli.store.io import write_json
    from aha_cli.store.paths import config_path

    write_json(config_path(home), _config(remote))

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["--home", str(home), "kb", "sync", "--push", "-m", "cli sync", "--json"])
    assert rc == 0
    result = json.loads(out.getvalue())
    assert result["ok"] is True
    assert result["steps"]["push"]["pushed"] is True

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", remote, str(clone)], check=True, capture_output=True)
    assert (clone / "aha-knowledge.json").exists()
