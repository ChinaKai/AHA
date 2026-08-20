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
- **Agent conflict resolution.** When ``knowledge.sync.resolve_conflicts ==
  "agent"``, ``sync`` can leave the rebase in progress so a KB maintenance
  agent can resolve the unmerged paths (user-priority), then ``rebase
  --continue`` and push. The maintenance plumbing lives here; the agent
  orchestration lives in :mod:`aha_cli.services.knowledge_maintenance`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from aha_cli import platform
from aha_cli.services.proxy import apply_proxy_environment, core_proxy_config
from aha_cli.store.knowledge import PROJECTS_DIR, init_knowledge_base, knowledge_config, knowledge_root

_GIT_TIMEOUT = 120
_PROJECT_APPROVED_KINDS = ("navigation", "solutions", "worklog")
_LOCAL_CHANGES_MAX_FILES = 100
_LOCAL_CHANGES_MAX_DIFF_CHARS = 12_000
_LOCAL_CHANGES_MAX_TOTAL_DIFF_CHARS = 60_000
_LOCAL_CHANGES_READ_BYTES = 256_000

# Porcelain XY codes for unmerged paths (index and worktree both non-space).
_UNMERGED_CODES = {"AA", "DD", "AU", "UA", "DU", "UD", "UU"}


def git_available() -> bool:
    return shutil.which("git") is not None


def _git_cfg(config: dict | None) -> dict:
    cfg = knowledge_config(config)
    git = cfg.get("git")
    return git if isinstance(git, dict) else {}


def _sync_cfg(config: dict | None) -> dict:
    cfg = knowledge_config(config)
    sync = cfg.get("sync")
    return sync if isinstance(sync, dict) else {}


def _resolve_conflicts_mode(config: dict | None, explicit: str | None = None) -> str:
    value = explicit or _sync_cfg(config).get("resolve_conflicts", "manual")
    return str(value or "manual").strip()


def _author_flags(git_cfg: dict) -> list[str]:
    name = git_cfg.get("author_name") or "AHA"
    email = git_cfg.get("author_email") or "aha@local"
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def _run_git(
    repo: Path,
    args: list[str],
    *,
    author: dict | None = None,
    env: dict[str, str] | None = None,
) -> dict:
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
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
            env=env,
            **platform.hidden_subprocess_kwargs(),
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
        "stdout": proc.stdout.rstrip("\n"),
        "stderr": proc.stderr.strip(),
        "args": args,
    }


def _network_git_env(config: dict | None) -> dict[str, str]:
    env = os.environ.copy()
    if not _git_cfg(config).get("proxy_enabled", False):
        return apply_proxy_environment(env, {})
    proxy = core_proxy_config(config)
    values = {
        "HTTP_PROXY": proxy.get("http_proxy"),
        "HTTPS_PROXY": proxy.get("https_proxy"),
        "NO_PROXY": proxy.get("no_proxy"),
    }
    proxy_env: dict[str, str] = {}
    for key, value in values.items():
        text = str(value or "").strip()
        if text:
            proxy_env[key] = text
            proxy_env[key.lower()] = text
    return apply_proxy_environment(env, proxy_env)


def is_repo(repo: Path) -> bool:
    return (repo / ".git").is_dir()


def rebase_in_progress(repo: Path) -> bool:
    """True when the repo is mid-rebase (a pull left it in a conflict state)."""
    git_dir = repo / ".git"
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def unmerged_paths(repo: Path) -> list[str]:
    """Return the paths with unresolved merge conflicts (porcelain XY conflict codes)."""
    if not is_repo(repo):
        return []
    res = _run_git(repo, ["status", "--porcelain"])
    if not res["ok"]:
        return []
    paths: list[str] = []
    for raw in (res["stdout"] or "").splitlines():
        if len(raw) < 4:
            continue
        code = raw[:2].replace(" ", "")
        if code not in _UNMERGED_CODES:
            continue
        path = raw[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        paths.append(path)
    return _dedupe(paths)


# Rebase conflict stages: while replaying a local commit onto the upstream,
# git stages the upstream tip as :2: and the replayed (local) commit as :3:.
# The semantics are the reverse of a merge, so we label sides semantically as
# ``local`` (this device's work, :3:) and ``remote`` (upstream, :2:).
_REBASE_STAGES = {"local": "3", "remote": "2"}


def _take_stage(repo: Path, path: str, side: str) -> dict:
    """Resolve an unmerged path by taking a side (``local`` or ``remote``)."""
    stage = _REBASE_STAGES.get(str(side).strip())
    if stage is None:
        stage = "3"
    res = _run_git(repo, ["show", f":{stage}:{path}"])
    if res["ok"]:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(res["stdout"], encoding="utf-8")
        return _run_git(repo, ["add", "--", path])
    # The side is a deletion (file absent there): record the removal.
    removed = _run_git(repo, ["rm", "--", path])
    if removed["ok"]:
        return removed
    return _run_git(repo, ["rm", "--cached", "--", path])


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
    if status.get("unmerged") or status.get("rebase_in_progress"):
        return "conflict"
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

    dirty = _changed_paths(repo)
    if dirty["ok"]:
        paths = dirty["paths"]
        status["dirty"] = bool(paths)
        status["changed_count"] = len(paths)
        status["changed_paths"] = paths[:20]
    else:
        status["ok"] = False
        status["remote_error"] = f"git status failed: {dirty['stderr']}"

    # Conflict state: a prior agent-resolution pull may have left a rebase in
    # progress with unmerged paths. Report them so the UI can surface them and
    # the maintenance flow can resume.
    status["rebase_in_progress"] = rebase_in_progress(repo)
    unmerged = unmerged_paths(repo)
    status["unmerged"] = unmerged
    status["conflict_files"] = unmerged

    local = _run_git(repo, ["rev-parse", "HEAD"])
    if local["ok"]:
        status["local_head"] = local["stdout"]

    if check_remote and remote:
        fetch = _run_git(repo, ["fetch", "origin", branch], env=_network_git_env(config))
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


def _changed_paths_from_status_z(stdout: str) -> list[str]:
    paths: list[str] = []
    parts = (stdout or "").split("\0")
    index = 0
    while index < len(parts):
        raw = parts[index]
        index += 1
        if len(raw) < 4:
            continue
        status = raw[:2]
        path = raw[3:]
        if path:
            paths.append(path)
        if "R" in status or "C" in status:
            index += 1
    return paths


def _changed_paths(repo: Path, pathspecs: list[str] | None = None) -> dict:
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if pathspecs:
        args.extend(["--", *pathspecs])
    status = _run_git(repo, args)
    if not status["ok"]:
        return {"ok": False, "paths": [], "stderr": status["stderr"]}
    return {"ok": True, "paths": _changed_paths_from_status_z(status["stdout"]), "stderr": ""}


def changed_paths(root: Path, config: dict | None = None) -> list[str]:
    """Return dirty paths in the knowledge git repository without remote I/O."""
    repo = knowledge_root(root, config)
    if not git_available() or not is_repo(repo):
        return []
    status = _changed_paths(repo)
    if not status["ok"]:
        return []
    return list(status["paths"])


def _changed_entries(repo: Path) -> dict:
    status = _run_git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if not status["ok"]:
        return {"ok": False, "entries": [], "error": status["stderr"] or "git status failed"}
    entries: list[dict] = []
    parts = (status["stdout"] or "").split("\0")
    index = 0
    while index < len(parts):
        raw = parts[index]
        index += 1
        if len(raw) < 4:
            continue
        code = raw[:2]
        path = raw[3:]
        original_path = None
        if "R" in code or "C" in code:
            if index < len(parts) and parts[index]:
                original_path = parts[index]
            index += 1
        if path:
            entries.append({"status": code, "path": path, "original_path": original_path})
    return {"ok": True, "entries": entries, "error": ""}


def _untracked_change(repo: Path, entry: dict, max_chars: int) -> dict:
    path = str(entry.get("path") or "")
    item = {
        **entry,
        "additions": None,
        "deletions": None,
        "binary": False,
        "diff": "",
        "truncated": False,
        "no_diff": False,
        "error": "",
    }
    try:
        target = (repo / path).resolve()
        target.relative_to(repo.resolve())
        if not target.is_file():
            item["no_diff"] = True
            return item
        with target.open("rb") as handle:
            raw = handle.read(_LOCAL_CHANGES_READ_BYTES + 1)
        if b"\0" in raw:
            item["binary"] = True
            return item
        content_truncated = len(raw) > _LOCAL_CHANGES_READ_BYTES
        if not raw:
            item["additions"] = 0
            item["deletions"] = 0
            item["no_diff"] = True
            return item
        try:
            text = raw[:_LOCAL_CHANGES_READ_BYTES].decode("utf-8", errors="ignore" if content_truncated else "strict")
        except UnicodeDecodeError:
            item["binary"] = True
            return item
        lines = text.splitlines()
        item["additions"] = len(lines)
        item["deletions"] = 0
        header = f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
        diff = header + "".join(f"+{line}\n" for line in lines)
        if len(diff) > max_chars:
            diff = diff[:max_chars].rstrip() + "\n…"
            content_truncated = True
        item["diff"] = diff
        item["truncated"] = content_truncated
        item["no_diff"] = not bool(diff)
    except (OSError, ValueError) as exc:
        item["error"] = str(exc)
    return item


def _tracked_change(repo: Path, entry: dict, max_chars: int, *, has_head: bool) -> dict:
    path = str(entry.get("path") or "")
    item = {
        **entry,
        "additions": None,
        "deletions": None,
        "binary": False,
        "diff": "",
        "truncated": False,
        "no_diff": False,
        "error": "",
    }
    base = ["HEAD"] if has_head else ["--cached"]
    numstat = _run_git(repo, ["diff", "--no-ext-diff", "--no-renames", "--numstat", *base, "--", path])
    if not numstat["ok"]:
        item["error"] = numstat["stderr"] or "git diff --numstat failed"
        return item
    additions = 0
    deletions = 0
    saw_stats = False
    for raw_line in (numstat["stdout"] or "").splitlines():
        added, separator, remainder = raw_line.partition("\t")
        removed, separator2, _changed_path = remainder.partition("\t")
        if not separator or not separator2:
            continue
        saw_stats = True
        if added == "-" or removed == "-":
            item["binary"] = True
            break
        try:
            additions += int(added)
            deletions += int(removed)
        except ValueError:
            continue
    if saw_stats and not item["binary"]:
        item["additions"] = additions
        item["deletions"] = deletions
    if item["binary"]:
        return item
    diff_result = _run_git(repo, ["diff", "--no-ext-diff", "--no-color", "--no-renames", "--unified=3", *base, "--", path])
    if not diff_result["ok"]:
        item["error"] = diff_result["stderr"] or "git diff failed"
        return item
    diff = diff_result["stdout"] or ""
    if len(diff) > max_chars:
        diff = diff[:max_chars].rstrip() + "\n…"
        item["truncated"] = True
    item["diff"] = diff
    item["no_diff"] = not bool(diff)
    return item


def local_changes(root: Path, config: dict | None = None) -> dict:
    """Return bounded, read-only details for local KB git changes."""
    repo = knowledge_root(root, config)
    if not git_available():
        return {"ok": False, "state": "git_unavailable", "error": "git is not available on PATH", "changes": []}
    if not is_repo(repo):
        return {"ok": False, "state": "not_initialized", "error": "knowledge git repository is not initialized", "changes": []}
    status = _changed_entries(repo)
    if not status["ok"]:
        return {"ok": False, "state": "error", "error": status["error"], "changes": []}
    entries = status["entries"]
    has_head = _run_git(repo, ["rev-parse", "--verify", "HEAD"])["ok"]
    changes: list[dict] = []
    used_chars = 0
    content_truncated = False
    for entry in entries[:_LOCAL_CHANGES_MAX_FILES]:
        remaining = max(0, _LOCAL_CHANGES_MAX_TOTAL_DIFF_CHARS - used_chars)
        max_chars = min(_LOCAL_CHANGES_MAX_DIFF_CHARS, remaining)
        if entry["status"] == "??":
            item = _untracked_change(repo, entry, max_chars)
        else:
            item = _tracked_change(repo, entry, max_chars, has_head=has_head)
        if max_chars <= 0 and not item["binary"] and not item["error"]:
            item["diff"] = ""
            item["truncated"] = True
            item["no_diff"] = True
        used_chars += len(item["diff"])
        content_truncated = content_truncated or bool(item["truncated"])
        changes.append(item)
    return {
        "ok": True,
        "state": "dirty" if entries else "clean",
        "count": len(entries),
        "returned": len(changes),
        "truncated": len(entries) > len(changes) or content_truncated,
        "changes": changes,
    }


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


def pull(root: Path, config: dict | None = None, *, keep_rebase_on_conflict: bool = False) -> dict:
    """Rebase the local branch onto the remote. Aborts cleanly on conflict.

    When ``keep_rebase_on_conflict`` is true and a rebase conflict occurs, the
    rebase is left in progress (with unmerged paths) so a KB maintenance agent
    can resolve it and ``rebase --continue``. This is used by the agent conflict
    resolution flow; plain pulls keep the historic abort-on-conflict behavior.
    """
    git_cfg = _git_cfg(config)
    remote = (git_cfg.get("remote") or "").strip()
    if not remote:
        return {"ok": True, "pulled": False, "reason": "no remote configured"}
    ensured = ensure_repo(root, config)
    if not ensured["ok"]:
        return {"ok": False, "pulled": False, "error": ensured.get("error")}
    repo = knowledge_root(root, config)
    branch = git_cfg.get("branch") or "main"
    network_env = _network_git_env(config)

    # A rebase left in progress by a previous agent-resolution pull must not
    # block a fresh pull: abort it (local commits are intact) and retry.
    if rebase_in_progress(repo):
        _run_git(repo, ["rebase", "--abort"])

    # Distinguish an unreachable remote (a real failure) from an empty remote
    # that simply has no branch yet (safe to skip). `ls-remote` exits 0 with
    # empty output when the remote is reachable but the branch is absent, and
    # exits non-zero when the remote cannot be reached at all.
    ls = _run_git(repo, ["ls-remote", "--heads", "origin", branch], env=network_env)
    if not ls["ok"]:
        return {"ok": False, "pulled": False, "error": f"remote unreachable: {ls['stderr']}"}
    if not ls["stdout"]:
        return {"ok": True, "pulled": False, "reason": "remote has no such branch yet"}

    fetch = _run_git(repo, ["fetch", "origin", branch], env=network_env)
    if not fetch["ok"]:
        return {"ok": False, "pulled": False, "error": f"fetch failed: {fetch['stderr']}"}

    onto = f"origin/{branch}"
    rebase = _run_git(repo, ["rebase", onto], author=git_cfg)
    if not rebase["ok"]:
        # Agent resolution: leave the rebase in progress so a maintenance agent
        # can resolve the unmerged paths. Only when there are actual unmerged
        # paths — otherwise fall through to the abort/adopt path below.
        if keep_rebase_on_conflict and unmerged_paths(repo):
            return {
                "ok": False,
                "pulled": False,
                "conflict": True,
                "rebase_in_progress": True,
                "unmerged": unmerged_paths(repo),
                "error": "rebase conflict with remote; left in progress for agent resolution",
            }
        _run_git(repo, ["rebase", "--abort"])
        # Unrelated histories (e.g. a prior sync committed the local skeleton
        # before the remote existed): rebase has no common base to replay onto.
        # When the working tree is clean, adopt the remote as the authoritative
        # base; otherwise surface the conflict so uncommitted work is not lost.
        base = _run_git(repo, ["merge-base", "HEAD", onto])
        dirty = _run_git(repo, ["status", "--porcelain"])
        if not base["ok"] and not dirty["stdout"]:
            reset = _run_git(repo, ["reset", "--hard", onto])
            if reset["ok"]:
                return {"ok": True, "pulled": True, "adopted_remote": True}
            return {"ok": False, "pulled": False, "error": f"failed to adopt remote: {reset['stderr']}"}
        return {
            "ok": False,
            "pulled": False,
            "conflict": True,
            "error": "rebase conflict with remote; aborted to keep repo clean",
        }
    return {"ok": True, "pulled": True}


def _adopt_remote_base(root: Path, config: dict | None = None) -> dict | None:
    """On a freshly-created repo, fetch and reset onto the remote branch.

    Adopting the remote as the base before the first local commit avoids a
    divergent local history with no common ancestor, which would make the
    subsequent ``rebase`` fail. Returns ``None`` when there is nothing to adopt
    (no remote, unreachable, or the branch does not exist yet).
    """
    git_cfg = _git_cfg(config)
    remote = (git_cfg.get("remote") or "").strip()
    if not remote:
        return None
    branch = git_cfg.get("branch") or "main"
    repo = knowledge_root(root, config)
    network_env = _network_git_env(config)
    ls = _run_git(repo, ["ls-remote", "--heads", "origin", branch], env=network_env)
    if not ls["ok"] or not ls["stdout"]:
        return None
    fetch = _run_git(repo, ["fetch", "origin", branch], env=network_env)
    if not fetch["ok"]:
        return {"ok": False, "error": f"fetch failed: {fetch['stderr']}"}
    reset = _run_git(repo, ["reset", "--hard", f"origin/{branch}"])
    if not reset["ok"]:
        return {"ok": False, "error": f"reset failed: {reset['stderr']}"}
    return {"ok": True, "adopted": True}


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

    res = _run_git(repo, ["push", "-u", "origin", branch], env=_network_git_env(config))
    if not res["ok"]:
        return {"ok": False, "pushed": False, "error": f"git push failed: {res['stderr']}"}
    return {"ok": True, "pushed": True}


# --------------------------------------------------------------------------- #
# Conflict resolution plumbing (used by the KB maintenance agent)
# --------------------------------------------------------------------------- #
_AGENT_VALUE_MARKERS = {
    "agent", "aha", "auto", "assistant", "generated", "llm", "distill",
    "claude", "codex", "gpt",
}


def _frontmatter_marker(marker: str, text: str, agent_values: set[str] | None = None) -> bool:
    """Case-insensitive frontmatter key=value or key: value marker match.

    When ``agent_values`` is None, presence of the key with a non-false value
    matches (used for ``distilled_by``, which AHA only writes on agent output).
    Otherwise the value must be one of ``agent_values``.
    """
    if not text:
        return False
    needle = marker.lower()
    for line in text.splitlines()[:80]:
        lowered = line.strip().lower()
        if not (lowered.startswith(f"{needle}:") or lowered.startswith(f"{needle}=")):
            continue
        value = lowered.split(":", 1)[-1].split("=", 1)[-1].strip().strip("'\"")
        if value in {"", "false", "0", "no", "none", "null", "undefined", "unknown"}:
            return False
        if agent_values is None:
            return True
        if value in agent_values:
            return True
    return False


def _agent_authored(content: str) -> bool:
    """Best-effort detection that an entry body/frontmatter is agent-distilled."""
    if _frontmatter_marker("distilled_by", content):
        return True
    for marker in ("created_by", "source", "origin"):
        if _frontmatter_marker(marker, content, agent_values=_AGENT_VALUE_MARKERS):
            return True
    return False


def conflict_detail(root: Path, config: dict | None = None) -> dict:
    """Return base/local/remote content for each unmerged path.

    During a rebase conflict ``local`` is the replayed commit (this device's
    work) and ``remote`` is the upstream tip. Used to build the maintenance
    agent's resolution prompt. Contents are returned in full so a small KB fits
    in the prompt; callers may truncate.
    """
    repo = knowledge_root(root, config)
    unmerged = unmerged_paths(repo)
    conflicts: list[dict] = []
    for path in unmerged:
        entry: dict = {"path": path}
        for stage, label in (("1", "base"), ("2", "remote"), ("3", "local")):
            res = _run_git(repo, ["show", f":{stage}:{path}"])
            content = res["stdout"] if res["ok"] else ""
            entry[label] = content
            entry[f"{label}_len"] = len(content)
            entry[f"{label}_agent"] = _agent_authored(content)
        entry["base_agent"] = bool(entry.get("base_agent"))
        conflicts.append(entry)
    return {"ok": True, "repo": str(repo), "unmerged": unmerged, "conflicts": conflicts}


def resolve_unmerged(
    root: Path,
    config: dict | None = None,
    *,
    decisions: dict | None = None,
) -> dict:
    """Resolve every unmerged path and stage the result.

    ``decisions`` maps path -> {"action": "local"|"remote"|"merge", "content": str}
    (legacy "ours"/"theirs" are mapped to local/remote). Default for a path
    without a decision honors ``knowledge.sync.user_priority``: take the side
    that is not agent-authored when exactly one side is, otherwise keep the
    local (this device's) side.
    """
    repo = knowledge_root(root, config)
    unmerged = unmerged_paths(repo)
    if not unmerged:
        return {"ok": True, "resolved": []}
    cfg = _sync_cfg(config)
    user_priority = bool(cfg.get("user_priority", True))
    decisions = decisions or {}
    detail = conflict_detail(root, config)
    by_path = {c["path"]: c for c in detail.get("conflicts", [])}
    resolved: list[str] = []
    for path in unmerged:
        dec = decisions.get(path)
        if isinstance(dec, dict):
            action = _normalize_action(str(dec.get("action") or "merge"))
            content = dec.get("content")
            # A merge decision without content (e.g. an agent produced a plan
            # object without body) is unsafe: fall back to the default rather
            # than clobber the file with an empty string.
            if action == "merge" and content is None:
                action, content = _default_resolution(by_path.get(path), user_priority)
        else:
            action, content = _default_resolution(by_path.get(path), user_priority)
        if action == "local":
            result = _take_stage(repo, path, "local")
        elif action == "remote":
            result = _take_stage(repo, path, "remote")
        else:
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content or ""), encoding="utf-8")
            result = _run_git(repo, ["add", "--", path])
        if result["ok"]:
            resolved.append(path)
    return {"ok": True, "resolved": resolved}


def _normalize_action(action: str) -> str:
    normalized = str(action or "merge").strip().lower()
    if normalized in {"local", "remote", "merge"}:
        return normalized
    if normalized in {"ours", "keep_local"}:
        return "local"
    if normalized in {"theirs", "keep_remote"}:
        return "remote"
    return "merge"


def default_resolutions(root: Path, config: dict | None = None, *, safe_only: bool = False) -> dict:
    """Map every unmerged path to its default user-priority resolution.

    Used as the deterministic baseline the maintenance agent plan is layered
    on top of, so paths the agent does not mention still resolve sanely.
    """
    cfg = _sync_cfg(config)
    user_priority = bool(cfg.get("user_priority", True))
    detail = conflict_detail(root, config)
    decisions: dict[str, dict] = {}
    for conflict in detail.get("conflicts", []):
        if safe_only and bool(conflict.get("local_agent")) == bool(conflict.get("remote_agent")):
            continue
        action, _ = _default_resolution(conflict, user_priority)
        decisions[conflict["path"]] = {"action": action}
    return decisions


def _default_resolution(conflict: dict | None, user_priority: bool) -> tuple[str, str | None]:
    """Pick a deterministic resolution honoring user-priority when no agent plan exists."""
    if not conflict:
        return "local", None
    local_agent = bool(conflict.get("local_agent"))
    remote_agent = bool(conflict.get("remote_agent"))
    if user_priority and local_agent and not remote_agent:
        # The remote side is user-authored and the local side is agent-written.
        return "remote", None
    if user_priority and remote_agent and not local_agent:
        # The local side is user-authored and the remote side is agent-written.
        return "local", None
    return "local", None


def rebase_abort(root: Path, config: dict | None = None) -> dict:
    """Abort an in-progress rebase, restoring the pre-rebase HEAD (no-op if idle)."""
    repo = knowledge_root(root, config)
    if not rebase_in_progress(repo):
        return {"ok": True, "aborted": False}
    res = _run_git(repo, ["rebase", "--abort"])
    return {"ok": res["ok"], "aborted": res["ok"]}


def rebase_continue(root: Path, config: dict | None = None, *, skip: bool = False) -> dict:
    """Stage any remaining changes and finish the in-progress rebase.

    Uses an editor override so the replayed commit message is kept as-is. On a
    hard failure the rebase is aborted so the repo is never left mid-rebase.
    """
    repo = knowledge_root(root, config)
    if not rebase_in_progress(repo):
        return {"ok": True, "continued": False, "reason": "no rebase in progress"}
    add = _run_git(repo, ["add", "-A"])
    if not add["ok"]:
        _run_git(repo, ["rebase", "--abort"])
        return {"ok": False, "continued": False, "error": f"git add failed: {add['stderr']}"}
    env = os.environ.copy()
    env["GIT_EDITOR"] = "true"
    cmd = ["rebase", "--skip"] if skip else ["rebase", "--continue"]
    cont = _run_git(repo, cmd, author=_git_cfg(config), env=env)
    if cont["ok"]:
        return {"ok": True, "continued": True}
    _run_git(repo, ["rebase", "--abort"])
    return {"ok": False, "continued": False, "error": f"rebase --continue failed: {cont['stderr']}"}


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
    resolve_conflicts: str | None = None,
) -> dict:
    """Manual end-to-end sync: ensure repo -> pull -> commit -> push.

    Each step is failure-isolated; ``do_push=None`` defers to ``git.auto_push``.
    When ``knowledge.sync.resolve_conflicts == "agent"`` and the pull rebase
    conflicts, the rebase is left in progress and the result carries
    ``conflict=True`` + ``unmerged`` so the caller can dispatch a KB maintenance
    agent to resolve it.
    """
    steps: dict[str, dict] = {}
    steps["ensure"] = ensure_repo(root, config)
    if not steps["ensure"]["ok"]:
        return {"ok": False, "steps": steps}
    # Fresh repo: adopt the remote base before committing any local work, so we
    # never create a local commit whose history is unrelated to the remote
    # (which would make the rebase below fail on first sync).
    if steps["ensure"].get("created"):
        adopted = _adopt_remote_base(root, config)
        if adopted is not None:
            steps["adopt_remote"] = adopted
            if not adopted["ok"]:
                return {"ok": False, "steps": steps}
    # Commit local changes BEFORE rebasing: a dirty work tree makes
    # `rebase` fail outright when the remote has new commits, so the local
    # work must land as a commit first, then rebase onto the remote, then push.
    steps["commit"] = commit_all(root, message, config)
    if not steps["commit"]["ok"]:
        return {"ok": False, "steps": steps}
    if do_pull:
        agent_resolve = _resolve_conflicts_mode(config, resolve_conflicts) == "agent"
        steps["pull"] = pull(root, config, keep_rebase_on_conflict=agent_resolve)
        if not steps["pull"]["ok"]:
            if steps["pull"].get("rebase_in_progress"):
                # Agent resolution path: leave the rebase in progress for the
                # maintenance job; do not push anything until it resolves.
                return {
                    "ok": False,
                    "conflict": True,
                    "unmerged": steps["pull"].get("unmerged", []),
                    "steps": steps,
                }
            # Conflict (already aborted) or unreachable remote: stop before push
            # so we never push a half-synced or divergent history.
            return {"ok": False, "steps": steps}
    want_push = _git_cfg(config).get("auto_push", False) if do_push is None else do_push
    if want_push:
        steps["push"] = push(root, config)
    ok = all(step.get("ok", True) for step in steps.values())
    return {"ok": ok, "steps": steps}
