"""Git management for the knowledge base (Phase 2).

The knowledge base directory is an AHA-managed git repository. This module
wraps the git plumbing needed to keep it synced with an optional remote:

- ``ensure_repo``     — idempotent ``git init`` + branch + author + remote wiring
- ``commit_all``      — stage everything and commit (no-op when clean)
- ``pull`` / ``push`` — sync with the configured remote/branch
- ``auto_pull_before_task`` / ``auto_commit_after_change`` / ``auto_push`` —
  config-gated high-level hooks used by the task lifecycle

Design rules:
- **Failure isolation.** Every public call returns a result dict and never
  raises for an expected git/IO failure, so a broken remote can never abort a
  task. Results carry ``ok`` and a human-readable ``error`` / ``reason``.
- **No global state mutation.** Author identity is injected per-command via
  ``git -c user.name=… -c user.email=…``; we never touch the user's global git
  config.
- **Conflict safety.** A pull that hits a rebase conflict is aborted so the
  repo is never left mid-rebase; the conflict is reported for the UI to surface.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from aha_cli.store.knowledge import PROJECTS_DIR, init_knowledge_base, knowledge_config, knowledge_root

_GIT_TIMEOUT = 120
_PROJECT_APPROVED_KINDS = ("navigation", "solutions", "worklog")


def git_available() -> bool:
    return shutil.which("git") is not None


def _git_cfg(config: dict | None) -> dict:
    cfg = knowledge_config(config)
    git = cfg.get("git")
    return git if isinstance(git, dict) else {}


def _author_flags(git_cfg: dict) -> list[str]:
    name = git_cfg.get("author_name") or "AHA"
    email = git_cfg.get("author_email") or "aha@local"
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def _run_git(repo: Path, args: list[str], *, author: dict | None = None) -> dict:
    """Run a git command in ``repo``. Returns a result dict; never raises."""
    cmd = ["git", "-C", str(repo)]
    if author is not None:
        cmd += _author_flags(author)
    cmd += args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except FileNotFoundError:
        return {"ok": False, "rc": 127, "stdout": "", "stderr": "git not found", "args": args}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": 124, "stdout": "", "stderr": "git timed out", "args": args}
    except OSError as exc:  # pragma: no cover - defensive
        return {"ok": False, "rc": 1, "stdout": "", "stderr": str(exc), "args": args}
    return {
        "ok": proc.returncode == 0,
        "rc": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "args": args,
    }


def is_repo(repo: Path) -> bool:
    return (repo / ".git").is_dir()


def _current_remote(repo: Path) -> str | None:
    res = _run_git(repo, ["remote", "get-url", "origin"])
    return res["stdout"] if res["ok"] and res["stdout"] else None


def ensure_repo(root: Path, config: dict | None = None) -> dict:
    """Make sure the knowledge base is an initialized git repo. Idempotent."""
    if not git_available():
        return {"ok": False, "error": "git is not available on PATH"}
    git_cfg = _git_cfg(config)
    branch = git_cfg.get("branch") or "main"
    # Guarantee the on-disk skeleton exists before touching git.
    init_knowledge_base(root, config)
    repo = knowledge_root(root, config)

    created = False
    if not is_repo(repo):
        res = _run_git(repo, ["init", "-b", branch])
        if not res["ok"]:
            return {"ok": False, "error": f"git init failed: {res['stderr']}"}
        created = True

    # Wire (or update) the origin remote when configured.
    remote = (git_cfg.get("remote") or "").strip()
    remote_state = "unset"
    if remote:
        current = _current_remote(repo)
        if current is None:
            add = _run_git(repo, ["remote", "add", "origin", remote])
            remote_state = "added" if add["ok"] else f"error: {add['stderr']}"
        elif current != remote:
            upd = _run_git(repo, ["remote", "set-url", "origin", remote])
            remote_state = "updated" if upd["ok"] else f"error: {upd['stderr']}"
        else:
            remote_state = "unchanged"

    return {
        "ok": True,
        "repo": str(repo),
        "created": created,
        "branch": branch,
        "remote": remote or None,
        "remote_state": remote_state,
    }


def _has_changes(repo: Path) -> bool:
    res = _run_git(repo, ["status", "--porcelain"])
    return bool(res["ok"] and res["stdout"])


def _sync_status_state(status: dict) -> str:
    if not status.get("git_available", True):
        return "git_unavailable"
    if not status.get("is_repo"):
        return "not_initialized"
    if not status.get("remote"):
        return "no_remote"
    if status.get("remote_error"):
        return "remote_error"
    if status.get("ahead", 0) and status.get("behind", 0):
        return "diverged"
    if status.get("dirty"):
        return "dirty"
    if status.get("ahead", 0):
        return "ahead"
    if status.get("behind", 0):
        return "behind"
    return "clean"


def sync_status(root: Path, config: dict | None = None, *, check_remote: bool = False) -> dict:
    """Return KB git sync state without committing, rebasing, or pushing."""
    git_cfg = _git_cfg(config)
    branch = git_cfg.get("branch") or "main"
    remote = (git_cfg.get("remote") or "").strip()
    repo = knowledge_root(root, config)
    status: dict = {
        "ok": True,
        "git_available": git_available(),
        "repo": str(repo),
        "is_repo": is_repo(repo),
        "remote": remote or None,
        "branch": branch,
        "dirty": False,
        "changed_count": 0,
        "changed_paths": [],
        "ahead": 0,
        "behind": 0,
        "local_head": None,
        "remote_head": None,
        "remote_error": "",
        "needs_sync": False,
    }
    if not status["git_available"]:
        status["ok"] = False
        status["state"] = _sync_status_state(status)
        return status
    if not status["is_repo"]:
        status["state"] = _sync_status_state(status)
        return status

    dirty = _run_git(repo, ["status", "--porcelain"])
    if dirty["ok"]:
        paths = _changed_paths_from_status(dirty["stdout"])
        status["dirty"] = bool(paths)
        status["changed_count"] = len(paths)
        status["changed_paths"] = paths[:20]
    else:
        status["ok"] = False
        status["remote_error"] = f"git status failed: {dirty['stderr']}"

    local = _run_git(repo, ["rev-parse", "HEAD"])
    if local["ok"]:
        status["local_head"] = local["stdout"]

    if check_remote and remote:
        fetch = _run_git(repo, ["fetch", "origin", branch])
        if not fetch["ok"]:
            status["ok"] = False
            status["remote_error"] = f"fetch failed: {fetch['stderr']}"
        else:
            remote_head = _run_git(repo, ["rev-parse", f"origin/{branch}"])
            if remote_head["ok"]:
                status["remote_head"] = remote_head["stdout"]
                if status["local_head"]:
                    counts = _run_git(repo, ["rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"])
                    if counts["ok"] and counts["stdout"]:
                        left, _, right = counts["stdout"].partition("\t")
                        if not right:
                            left, _, right = counts["stdout"].partition(" ")
                        try:
                            status["ahead"] = int(left or 0)
                            status["behind"] = int(right or 0)
                        except ValueError:
                            status["ok"] = False
                            status["remote_error"] = f"rev-list returned invalid counts: {counts['stdout']}"
                    elif not counts["ok"]:
                        status["ok"] = False
                        status["remote_error"] = f"rev-list failed: {counts['stderr']}"

    status["state"] = _sync_status_state(status)
    status["needs_sync"] = status["state"] in {"dirty", "ahead", "behind", "diverged"}
    return status


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _safe_project_keys(project_keys: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    safe: list[str] = []
    for value in project_keys:
        key = str(value or "").strip()
        if not key or key in {".", ".."} or "/" in key or "\\" in key:
            continue
        safe.append(key)
    return _dedupe(safe)


def _project_approved_pathspecs(project_keys: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    pathspecs: list[str] = []
    for key in _safe_project_keys(project_keys):
        for kind in _PROJECT_APPROVED_KINDS:
            pathspecs.append(f"{PROJECTS_DIR}/{key}/{kind}")
    return pathspecs


def _usable_pathspecs(repo: Path, pathspecs: list[str]) -> list[str]:
    usable: list[str] = []
    for pathspec in pathspecs:
        if (repo / pathspec).exists():
            usable.append(pathspec)
            continue
        tracked = _run_git(repo, ["ls-files", "--", pathspec])
        if tracked["ok"] and tracked["stdout"]:
            usable.append(pathspec)
    return usable


def _status_for_pathspecs(repo: Path, pathspecs: list[str]) -> dict:
    if not pathspecs:
        return {"ok": True, "stdout": ""}
    return _run_git(repo, ["status", "--porcelain", "--", *pathspecs])


def _changed_paths_from_status(stdout: str) -> list[str]:
    paths: list[str] = []
    for raw in (stdout or "").splitlines():
        if len(raw) < 4:
            continue
        path = raw[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        if path:
            paths.append(path)
    return paths


def changed_paths(root: Path, config: dict | None = None) -> list[str]:
    """Return dirty paths in the knowledge git repository without remote I/O."""
    repo = knowledge_root(root, config)
    if not git_available() or not is_repo(repo):
        return []
    status = _run_git(repo, ["status", "--porcelain"])
    if not status["ok"]:
        return []
    return _changed_paths_from_status(status["stdout"])


def commit_all(root: Path, message: str, config: dict | None = None) -> dict:
    """Stage all knowledge changes and commit. No-op when the tree is clean."""
    ensured = ensure_repo(root, config)
    if not ensured["ok"]:
        return {"ok": False, "committed": False, "error": ensured.get("error")}
    repo = knowledge_root(root, config)
    git_cfg = _git_cfg(config)

    if not _has_changes(repo):
        return {"ok": True, "committed": False, "reason": "nothing to commit"}

    add = _run_git(repo, ["add", "-A"])
    if not add["ok"]:
        return {"ok": False, "committed": False, "error": f"git add failed: {add['stderr']}"}
    commit = _run_git(repo, ["commit", "-m", message], author=git_cfg)
    if not commit["ok"]:
        return {"ok": False, "committed": False, "error": f"git commit failed: {commit['stderr']}"}
    head = _run_git(repo, ["rev-parse", "--short", "HEAD"])
    return {"ok": True, "committed": True, "commit": head["stdout"] if head["ok"] else None}


def commit_project_approved_entries(root: Path, message: str, project_keys: list[str], config: dict | None = None) -> dict:
    """Commit only approved project KB entry changes for the given project keys.

    This deliberately excludes pending candidates, general/personal entries,
    and other projects. It also uses ``git commit --only`` so pre-existing
    staged changes outside these pathspecs cannot be swept into the commit.
    """
    ensured = ensure_repo(root, config)
    if not ensured["ok"]:
        return {"ok": False, "committed": False, "error": ensured.get("error")}
    repo = knowledge_root(root, config)
    git_cfg = _git_cfg(config)
    project_keys = _safe_project_keys(project_keys)
    pathspecs = _usable_pathspecs(repo, _project_approved_pathspecs(project_keys))
    if not pathspecs:
        return {"ok": True, "committed": False, "reason": "no project pathspecs"}

    status = _status_for_pathspecs(repo, pathspecs)
    if not status["ok"]:
        return {"ok": False, "committed": False, "error": f"git status failed: {status['stderr']}"}
    changed_paths = _changed_paths_from_status(status["stdout"])
    if not changed_paths:
        return {"ok": True, "committed": False, "reason": "nothing to commit", "project_keys": project_keys}

    add = _run_git(repo, ["add", "-A", "--", *pathspecs])
    if not add["ok"]:
        return {"ok": False, "committed": False, "error": f"git add failed: {add['stderr']}"}
    commit = _run_git(repo, ["commit", "-m", message, "--only", "--", *pathspecs], author=git_cfg)
    if not commit["ok"]:
        return {"ok": False, "committed": False, "error": f"git commit failed: {commit['stderr']}"}
    head = _run_git(repo, ["rev-parse", "--short", "HEAD"])
    return {
        "ok": True,
        "committed": True,
        "commit": head["stdout"] if head["ok"] else None,
        "project_keys": project_keys,
        "paths": changed_paths,
    }


def pull(root: Path, config: dict | None = None) -> dict:
    """Rebase the local branch onto the remote. Aborts cleanly on conflict."""
    git_cfg = _git_cfg(config)
    remote = (git_cfg.get("remote") or "").strip()
    if not remote:
        return {"ok": True, "pulled": False, "reason": "no remote configured"}
    ensured = ensure_repo(root, config)
    if not ensured["ok"]:
        return {"ok": False, "pulled": False, "error": ensured.get("error")}
    repo = knowledge_root(root, config)
    branch = git_cfg.get("branch") or "main"

    # Distinguish an unreachable remote (a real failure) from an empty remote
    # that simply has no branch yet (safe to skip). `ls-remote` exits 0 with
    # empty output when the remote is reachable but the branch is absent, and
    # exits non-zero when the remote cannot be reached at all.
    ls = _run_git(repo, ["ls-remote", "--heads", "origin", branch])
    if not ls["ok"]:
        return {"ok": False, "pulled": False, "error": f"remote unreachable: {ls['stderr']}"}
    if not ls["stdout"]:
        return {"ok": True, "pulled": False, "reason": "remote has no such branch yet"}

    fetch = _run_git(repo, ["fetch", "origin", branch])
    if not fetch["ok"]:
        return {"ok": False, "pulled": False, "error": f"fetch failed: {fetch['stderr']}"}

    rebase = _run_git(repo, ["rebase", f"origin/{branch}"], author=git_cfg)
    if not rebase["ok"]:
        _run_git(repo, ["rebase", "--abort"])
        return {
            "ok": False,
            "pulled": False,
            "conflict": True,
            "error": "rebase conflict with remote; aborted to keep repo clean",
        }
    return {"ok": True, "pulled": True}


def push(root: Path, config: dict | None = None) -> dict:
    git_cfg = _git_cfg(config)
    remote = (git_cfg.get("remote") or "").strip()
    if not remote:
        return {"ok": True, "pushed": False, "reason": "no remote configured"}
    ensured = ensure_repo(root, config)
    if not ensured["ok"]:
        return {"ok": False, "pushed": False, "error": ensured.get("error")}
    repo = knowledge_root(root, config)
    branch = git_cfg.get("branch") or "main"

    res = _run_git(repo, ["push", "-u", "origin", branch])
    if not res["ok"]:
        return {"ok": False, "pushed": False, "error": f"git push failed: {res['stderr']}"}
    return {"ok": True, "pushed": True}


# --------------------------------------------------------------------------- #
# Config-gated lifecycle hooks
# --------------------------------------------------------------------------- #
def _enabled(config: dict | None) -> bool:
    cfg = knowledge_config(config)
    return bool(cfg.get("enabled")) and bool(_git_cfg(config).get("enabled"))


def auto_pull_before_task(root: Path, config: dict | None = None) -> dict:
    """Pull before a task starts, if knowledge.git.auto_pull is on."""
    if not _enabled(config):
        return {"ok": True, "skipped": "git sync disabled"}
    if not _git_cfg(config).get("auto_pull", False):
        return {"ok": True, "skipped": "auto_pull disabled"}
    return pull(root, config)


def auto_commit_after_change(root: Path, message: str, config: dict | None = None) -> dict:
    """Commit (and optionally push) after knowledge is written."""
    if not _enabled(config):
        return {"ok": True, "skipped": "git sync disabled"}
    git_cfg = _git_cfg(config)
    if not git_cfg.get("auto_commit", False):
        return {"ok": True, "skipped": "auto_commit disabled"}
    result = commit_all(root, message, config)
    if result.get("committed") and git_cfg.get("auto_push", False):
        result["push"] = push(root, config)
    return result


def auto_commit_project_approved_entries_after_feedback(
    root: Path,
    message: str,
    project_keys: list[str],
    config: dict | None = None,
) -> dict:
    """Commit approved project KB edits reported by task KB feedback."""
    if not _enabled(config):
        return {"ok": True, "skipped": "git sync disabled"}
    git_cfg = _git_cfg(config)
    if not git_cfg.get("auto_commit", False):
        return {"ok": True, "skipped": "auto_commit disabled"}
    result = commit_project_approved_entries(root, message, project_keys, config)
    if result.get("committed") and git_cfg.get("auto_push", False):
        result["push"] = push(root, config)
    return result


def sync(
    root: Path,
    config: dict | None = None,
    *,
    message: str,
    do_pull: bool = True,
    do_push: bool | None = None,
) -> dict:
    """Manual end-to-end sync: ensure repo -> pull -> commit -> push.

    Each step is failure-isolated; ``do_push=None`` defers to ``git.auto_push``.
    """
    steps: dict[str, dict] = {}
    steps["ensure"] = ensure_repo(root, config)
    if not steps["ensure"]["ok"]:
        return {"ok": False, "steps": steps}
    # Commit local changes BEFORE rebasing: a dirty work tree makes
    # `rebase` fail outright when the remote has new commits, so the local
    # work must land as a commit first, then rebase onto the remote, then push.
    steps["commit"] = commit_all(root, message, config)
    if not steps["commit"]["ok"]:
        return {"ok": False, "steps": steps}
    if do_pull:
        steps["pull"] = pull(root, config)
        if not steps["pull"]["ok"]:
            # Conflict (already aborted) or unreachable remote: stop before push
            # so we never push a half-synced or divergent history.
            return {"ok": False, "steps": steps}
    want_push = _git_cfg(config).get("auto_push", False) if do_push is None else do_push
    if want_push:
        steps["push"] = push(root, config)
    ok = all(step.get("ok", True) for step in steps.values())
    return {"ok": ok, "steps": steps}
