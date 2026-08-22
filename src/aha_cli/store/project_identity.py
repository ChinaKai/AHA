"""Stable project identity manifests stored inside the synchronized knowledge base."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from aha_cli import platform
from aha_cli.domain.models import utc_now
from aha_cli.store.io import read_json, write_json


def _normalize_workspace_path(workspace: Path) -> Path:
    """Return a workspace path usable for git/manifest lookup on this host.

    A task workspace may be stored as a WSL UNC path
    (``\\\\wsl.localhost\\<distro>\\...``). Inside a WSL backend process the
    Linux ``Path`` treats that as a relative path and can neither read ``.git``
    nor produce a stable project key; convert it to the distro-native ``/...``
    path. On Windows (Web service) the UNC is already resolvable, so it is left
    untouched unless ``AHA_WSL_DISTRO`` says we are inside the distro.
    """
    workspace = Path(workspace).expanduser()
    if not os.environ.get("AHA_WSL_DISTRO"):
        return workspace
    text = str(workspace)
    if not text.startswith(("\\\\wsl.localhost\\", "\\\\wsl$\\")):
        return workspace
    from aha_cli.store.ws_target import wsl_workspace_native_path

    native = wsl_workspace_native_path(text)
    return Path(native) if native else workspace

PROJECT_IDENTITY_SCHEMA_VERSION = 3
LOCAL_PROJECT_BINDINGS_SCHEMA_VERSION = 2
PROJECT_MANIFEST_FILE = "project.json"
LOCAL_PROJECT_BINDINGS_FILE = "project_identity_bindings.json"
PROJECTS_DIR = "projects"
PROJECT_RELATION_TYPES = ("upstream", "sdk", "fork", "reference", "other")
MAX_PROJECT_RELATIONS = 5
MAX_PROJECT_RELATION_NOTE_LENGTH = 240
_PROJECT_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_BINDING_ID_RE = re.compile(r"^bind_[a-f0-9]{20}$")


class ProjectIdentityError(ValueError):
    """Raised when a project identity manifest or binding is invalid."""


class ProjectIdentityConflict(ProjectIdentityError):
    """Raised when one Git identity is already bound to another project."""


def slugify(text: str, *, max_length: int = 60) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    if not normalized:
        normalized = "kb-" + hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:10]
    return normalized[:max_length].strip("-")


def normalize_git_remote(remote: str) -> str:
    """Normalize equivalent Git remote URLs to one portable identity."""
    value = (remote or "").strip()
    if not value:
        return ""
    value = re.sub(r"\.git$", "", value)
    scp = re.match(r"^[\w.+-]+@([^:]+):(.+)$", value)
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        stripped = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value)
        stripped = re.sub(r"^[^@/]+@", "", stripped)
        host, _, path = stripped.partition("/")
    host = host.lower().strip("/")
    if host in {"github.com:443", "ssh.github.com", "ssh.github.com:443"}:
        host = "github.com"
    path = path.strip("/").lower()
    return f"{host}/{path}" if path else host


def git_remote_for(workspace: Path) -> str:
    """Best-effort origin lookup, including worktrees whose .git is a file."""
    facts = git_workspace_facts(workspace)
    if facts.get("remote"):
        return str(facts["remote"])
    return _legacy_git_remote_for(workspace)


def _legacy_git_remote_for(workspace: Path) -> str:
    """Fallback parser for environments where the git executable is unavailable."""
    config_file = Path(workspace).expanduser() / ".git" / "config"
    if not config_file.is_file():
        return ""
    try:
        text = config_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    in_origin = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_origin = line.replace(" ", "").lower() == '[remote"origin"]'
            continue
        if in_origin and line.lower().startswith("url"):
            _, _, value = line.partition("=")
            return value.strip()
    return ""


def _run_git(workspace: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(workspace), *args],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
            **platform.hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def git_workspace_facts(workspace: Path) -> dict:
    """Return portable Git facts for ordinary repos, worktrees, and submodules."""
    workspace = _normalize_workspace_path(workspace)
    root_text = _run_git(workspace, "rev-parse", "--show-toplevel")
    if not root_text:
        remote = _legacy_git_remote_for(workspace)
        return {
            "is_git": bool(remote),
            "workspace_path": _workspace_binding_key(workspace),
            "repo_root": str(workspace.resolve()),
            "git_dir": "",
            "remote": remote,
            "git_identity": normalize_git_remote(remote),
            "repository_fingerprint": "",
            "subpath": ".",
        }
    repo_root = Path(root_text).expanduser().resolve()
    git_dir = _run_git(workspace, "rev-parse", "--absolute-git-dir")
    remote = _run_git(workspace, "remote", "get-url", "origin")
    roots = sorted(
        line.strip()
        for line in _run_git(workspace, "rev-list", "--max-parents=0", "HEAD").splitlines()
        if line.strip()
    )
    fingerprint = (
        "roots:" + hashlib.sha256("\n".join(roots).encode("utf-8")).hexdigest()[:20]
        if roots
        else ""
    )
    try:
        subpath = workspace.resolve().relative_to(repo_root).as_posix() or "."
    except (OSError, ValueError):
        subpath = _run_git(workspace, "rev-parse", "--show-prefix").strip("/") or "."
    return {
        "is_git": True,
        "workspace_path": _workspace_binding_key(workspace),
        "repo_root": str(repo_root),
        "git_dir": git_dir,
        "remote": remote,
        "git_identity": normalize_git_remote(remote),
        "repository_fingerprint": fingerprint,
        "subpath": subpath,
    }


def _derived_project_key_aliases_from_facts(
    workspace: Path,
    facts: dict,
    goal: str | None = None,
) -> list[str]:
    workspace = Path(workspace).expanduser()
    remote = str(facts.get("git_identity") or "")
    if remote:
        subpath = str(facts.get("subpath") or ".")
        digest = hashlib.sha1(f"{remote}\0{subpath}".encode("utf-8")).hexdigest()[:12]
        repo_name = remote.rsplit("/", 1)[-1] or "repo"
        subpath_name = "" if subpath == "." else "-" + slugify(subpath.rsplit("/", 1)[-1], max_length=24)
        preferred = f"{slugify(repo_name, max_length=32)}{subpath_name}-git-{digest}"
        legacy_digest = hashlib.sha1(remote.encode("utf-8")).hexdigest()[:12]
        aliases = [
            preferred,
            f"git-{digest}",
            f"{slugify(repo_name, max_length=40)}-git-{legacy_digest}",
            f"git-{legacy_digest}",
        ]
        return _string_list(aliases)
    basis = "-".join(part for part in [(goal or "").strip(), workspace.name] if part)
    if not basis:
        basis = "workspace"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:8]
    return [f"ws-{slugify(basis)}-{digest}"]


def derived_project_key_aliases(workspace: Path, goal: str | None = None) -> list[str]:
    """Return the current remote/workspace-derived key and legacy aliases."""
    workspace = Path(workspace).expanduser()
    return _derived_project_key_aliases_from_facts(
        workspace,
        git_workspace_facts(workspace),
        goal=goal,
    )


def validate_project_key(project_key: str) -> str:
    key = str(project_key or "").strip()
    if not _PROJECT_KEY_RE.fullmatch(key):
        raise ProjectIdentityError(
            "project_key must use 1-128 letters, numbers, dots, underscores, or hyphens"
        )
    return key


def project_id_for_key(project_key: str) -> str:
    key = validate_project_key(project_key)
    return "prj_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _binding_id(*parts: object) -> str:
    payload = "\0".join(str(part or "") for part in parts)
    return "bind_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _git_binding(
    *,
    remote: str,
    repository_fingerprint: str = "",
    subpath: str = ".",
    active: bool = True,
    removed_at: str = "",
    created_at: str = "",
    updated_at: str = "",
) -> dict:
    normalized_remote = normalize_git_remote(remote)
    normalized_subpath = str(subpath or ".").strip().strip("/") or "."
    binding_id = _binding_id("git", normalized_remote, repository_fingerprint, normalized_subpath)
    return {
        "binding_id": binding_id,
        "kind": "git",
        "remote": normalized_remote,
        "repository_fingerprint": str(repository_fingerprint or ""),
        "subpath": normalized_subpath,
        "branch": None,
        "active": bool(active),
        "removed_at": str(removed_at or ""),
        "created_at": str(created_at or ""),
        "updated_at": str(updated_at or ""),
    }


def _normalize_binding(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "").strip().lower()
    if kind != "git":
        return None
    binding = _git_binding(
        remote=str(value.get("remote") or ""),
        repository_fingerprint=str(value.get("repository_fingerprint") or ""),
        subpath=str(value.get("subpath") or "."),
        active=value.get("active") is not False,
        removed_at=str(value.get("removed_at") or ""),
        created_at=str(value.get("created_at") or ""),
        updated_at=str(value.get("updated_at") or ""),
    )
    supplied_id = str(value.get("binding_id") or "").strip()
    if _BINDING_ID_RE.fullmatch(supplied_id):
        binding["binding_id"] = supplied_id
    return binding


def _merge_bindings(*groups: object) -> list[dict]:
    merged: dict[str, dict] = {}
    for group in groups:
        for raw in group if isinstance(group, list) else []:
            binding = _normalize_binding(raw)
            if binding is None:
                continue
            existing = merged.get(binding["binding_id"])
            binding_updated = str(binding.get("updated_at") or "")
            existing_updated = str((existing or {}).get("updated_at") or "")
            newer = binding_updated > existing_updated
            tombstone_wins_tie = (
                binding_updated == existing_updated
                and binding.get("active") is False
                and (existing or {}).get("active") is not False
            )
            if existing is None or newer or tombstone_wins_tie:
                merged[binding["binding_id"]] = binding
    return sorted(merged.values(), key=lambda item: item["binding_id"])


def project_manifest_path(kb_root: Path, project_key: str) -> Path:
    return Path(kb_root) / PROJECTS_DIR / validate_project_key(project_key) / PROJECT_MANIFEST_FILE


def local_project_bindings_path(aha_root: Path) -> Path:
    return Path(aha_root).expanduser() / "runtime" / LOCAL_PROJECT_BINDINGS_FILE


def _workspace_binding_key(workspace: Path) -> str:
    return str(_normalize_workspace_path(workspace).resolve())


def _read_local_project_bindings(aha_root: Path | None) -> list[dict]:
    if aha_root is None:
        return []
    path = local_project_bindings_path(aha_root)
    if not path.is_file():
        return []
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return []
    raw_bindings = data.get("bindings") if isinstance(data, dict) else []
    if not isinstance(raw_bindings, list):
        return []
    bindings: list[dict] = []
    seen: set[str] = set()
    for item in raw_bindings:
        if not isinstance(item, dict):
            continue
        workspace_path = str(item.get("workspace_path") or "").strip()
        if not workspace_path or workspace_path in seen:
            continue
        try:
            project_key = validate_project_key(str(item.get("project_key") or ""))
        except ProjectIdentityError:
            continue
        bindings.append({
            "binding_id": str(item.get("binding_id") or _binding_id("local", workspace_path)),
            "workspace_path": workspace_path,
            "project_key": project_key,
            "project_id": str(item.get("project_id") or project_id_for_key(project_key)),
            "binding_mode": (
                "explicit" if str(item.get("binding_mode") or "").lower() == "explicit"
                else "fallback"
            ),
            "created_at": str(item.get("created_at") or ""),
            "updated_at": str(item.get("updated_at") or ""),
        })
        seen.add(workspace_path)
    return bindings


def _write_local_project_binding(
    aha_root: Path,
    workspace: Path,
    project_key: str,
    *,
    explicit: bool = False,
) -> Path:
    path = local_project_bindings_path(aha_root)
    workspace_path = _workspace_binding_key(workspace)
    target_key = validate_project_key(project_key)
    now = utc_now()
    existing_bindings = _read_local_project_bindings(aha_root)
    existing = next(
        (item for item in existing_bindings if item["workspace_path"] == workspace_path),
        None,
    )
    bindings = [
        item for item in existing_bindings if item["workspace_path"] != workspace_path
    ]
    bindings.append({
        "binding_id": str((existing or {}).get("binding_id") or _binding_id("local", workspace_path)),
        "workspace_path": workspace_path,
        "project_key": target_key,
        "project_id": str((existing or {}).get("project_id") or project_id_for_key(target_key)),
        "binding_mode": "explicit" if explicit else "fallback",
        "created_at": str((existing or {}).get("created_at") or now),
        "updated_at": now,
    })
    write_json(path, {
        "schema_version": LOCAL_PROJECT_BINDINGS_SCHEMA_VERSION,
        "bindings": sorted(bindings, key=lambda item: item["workspace_path"]),
    })
    return path


def _remove_local_project_binding(aha_root: Path, workspace: Path) -> bool:
    path = local_project_bindings_path(aha_root)
    workspace_path = _workspace_binding_key(workspace)
    existing_bindings = _read_local_project_bindings(aha_root)
    bindings = [
        item for item in existing_bindings if item["workspace_path"] != workspace_path
    ]
    if len(bindings) == len(existing_bindings):
        return False
    write_json(path, {
        "schema_version": LOCAL_PROJECT_BINDINGS_SCHEMA_VERSION,
        "bindings": sorted(bindings, key=lambda item: item["workspace_path"]),
    })
    return True


def _string_list(value: object) -> list[str]:
    items = value if isinstance(value, list) else []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def normalize_project_relations(
    value: object,
    *,
    current_project_key: str,
    strict: bool = False,
) -> list[dict]:
    current_key = validate_project_key(current_project_key)
    if not isinstance(value, list):
        if strict and value is not None:
            raise ProjectIdentityError("related_projects must be an array")
        return []
    if strict and len(value) > MAX_PROJECT_RELATIONS:
        raise ProjectIdentityError(
            f"related_projects supports at most {MAX_PROJECT_RELATIONS} projects"
        )
    relations: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            if strict:
                raise ProjectIdentityError("each related project must be an object")
            continue
        try:
            target_key = validate_project_key(str(item.get("project_key") or ""))
        except ProjectIdentityError:
            if strict:
                raise
            continue
        if target_key == current_key:
            if strict:
                raise ProjectIdentityError("a project cannot reference itself")
            continue
        if target_key in seen:
            if strict:
                raise ProjectIdentityError(f"duplicate related project: {target_key}")
            continue
        relation = str(item.get("relation") or "reference").strip().lower()
        if relation not in PROJECT_RELATION_TYPES:
            if strict:
                raise ProjectIdentityError(f"unknown project relation: {relation}")
            relation = "reference"
        note = " ".join(str(item.get("note") or "").split())
        if len(note) > MAX_PROJECT_RELATION_NOTE_LENGTH:
            if strict:
                raise ProjectIdentityError(
                    f"project relation note must be at most {MAX_PROJECT_RELATION_NOTE_LENGTH} characters"
                )
            note = note[:MAX_PROJECT_RELATION_NOTE_LENGTH].rstrip()
        relations.append({
            "project_key": target_key,
            "relation": relation,
            "note": note,
        })
        seen.add(target_key)
        if len(relations) >= MAX_PROJECT_RELATIONS:
            break
    return relations


def _normalized_manifest(data: dict, *, directory_key: str) -> dict:
    key = validate_project_key(str(data.get("project_key") or directory_key))
    if key != directory_key:
        raise ProjectIdentityError(
            f"project manifest key {key!r} does not match directory {directory_key!r}"
        )
    schema_version = int(data.get("schema_version") or 0)
    legacy_bindings = (
        [
            _git_binding(remote=value, created_at=str(data.get("created_at") or ""), updated_at=str(data.get("updated_at") or ""))
            for value in _string_list(data.get("git_identities"))
            if normalize_git_remote(value)
        ]
        if schema_version < PROJECT_IDENTITY_SCHEMA_VERSION
        else []
    )
    bindings = _merge_bindings(data.get("bindings"), legacy_bindings)
    identities = _string_list([
        binding.get("remote")
        for binding in bindings
        if binding.get("active") and binding.get("remote")
    ])
    aliases: list[str] = []
    for value in _string_list([
        *_string_list(data.get("aliases")),
        *_string_list(data.get("legacy_keys")),
    ]):
        try:
            legacy = validate_project_key(value)
        except ProjectIdentityError:
            continue
        if legacy != key and legacy not in aliases:
            aliases.append(legacy)
    redirect_to = str(data.get("redirect_to") or "").strip()
    if redirect_to:
        redirect_to = validate_project_key(redirect_to)
    return {
        "schema_version": PROJECT_IDENTITY_SCHEMA_VERSION,
        "project_id": str(data.get("project_id") or project_id_for_key(key)),
        "project_key": key,
        "slug": str(data.get("slug") or key).strip() or key,
        "display_name": str(data.get("display_name") or key).strip() or key,
        "bindings": bindings,
        "aliases": aliases,
        "git_identities": identities,
        "legacy_keys": aliases,
        "related_projects": normalize_project_relations(
            data.get("related_projects"),
            current_project_key=key,
        ),
        "revision": max(1, int(data.get("revision") or 1)),
        "redirect_to": redirect_to,
        "created_at": str(data.get("created_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def read_project_manifest(kb_root: Path, project_key: str) -> dict | None:
    path = project_manifest_path(kb_root, project_key)
    if not path.is_file():
        return None
    data = read_json(path)
    if not isinstance(data, dict):
        raise ProjectIdentityError(f"project manifest must contain an object: {path}")
    manifest = _normalized_manifest(data, directory_key=validate_project_key(project_key))
    manifest["path"] = str(path)
    return manifest


def write_project_manifest(kb_root: Path, project_key: str, manifest: dict) -> Path:
    key = validate_project_key(project_key)
    normalized = _normalized_manifest({**manifest, "project_key": key}, directory_key=key)
    path = project_manifest_path(kb_root, key)
    write_json(path, {key: value for key, value in normalized.items() if key != "path"})
    return path


def resolve_project_manifest(kb_root: Path, project_key: str) -> dict | None:
    current = validate_project_key(project_key)
    seen: set[str] = set()
    for _ in range(8):
        if current in seen:
            raise ProjectIdentityConflict("project redirect cycle detected")
        seen.add(current)
        manifest = read_project_manifest(kb_root, current)
        if manifest is None:
            return None
        redirect = str(manifest.get("redirect_to") or "")
        if not redirect:
            manifest["redirected_from"] = [
                key for key in seen if key != manifest["project_key"]
            ]
            return manifest
        current = validate_project_key(redirect)
    raise ProjectIdentityConflict("project redirect chain is too deep")


def canonical_project_key(kb_root: Path, project_key: str) -> str:
    manifest = resolve_project_manifest(kb_root, project_key)
    return str((manifest or {}).get("project_key") or validate_project_key(project_key))


def list_project_manifests(kb_root: Path) -> list[dict]:
    projects_root = Path(kb_root) / PROJECTS_DIR
    if not projects_root.is_dir():
        return []
    manifests: list[dict] = []
    for directory in sorted(path for path in projects_root.iterdir() if path.is_dir()):
        try:
            manifest = read_project_manifest(kb_root, directory.name)
        except (OSError, ValueError):
            continue
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def project_identity_migration_plan(kb_root: Path) -> list[dict]:
    plan: list[dict] = []
    projects_root = Path(kb_root) / PROJECTS_DIR
    if not projects_root.is_dir():
        return plan
    for directory in sorted(path for path in projects_root.iterdir() if path.is_dir()):
        path = directory / PROJECT_MANIFEST_FILE
        if not path.is_file():
            continue
        try:
            raw = read_json(path)
            normalized = _normalized_manifest(raw, directory_key=directory.name)
        except (OSError, ValueError):
            continue
        payload = {key: value for key, value in normalized.items() if key != "path"}
        plan.append({
            "project_key": directory.name,
            "path": str(path),
            "relative_path": path.relative_to(kb_root).as_posix(),
            "from_schema": int(raw.get("schema_version") or 0),
            "to_schema": PROJECT_IDENTITY_SCHEMA_VERSION,
            "changed": raw != payload,
            "project_id": payload["project_id"],
            "binding_count": len(payload.get("bindings") or []),
            "payload": payload,
        })
    return plan


def backup_project_identity_manifests(
    kb_root: Path,
    backup_dir: Path,
    *,
    plan: list[dict] | None = None,
) -> list[Path]:
    copied: list[Path] = []
    for item in plan if isinstance(plan, list) else project_identity_migration_plan(kb_root):
        source = Path(str(item.get("path") or ""))
        if not source.is_file():
            continue
        target = Path(backup_dir) / str(item.get("relative_path") or source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(target)
    return copied


def migrate_project_identity_manifests(kb_root: Path) -> list[Path]:
    migrated: list[Path] = []
    for item in project_identity_migration_plan(kb_root):
        if not item.get("changed"):
            continue
        path = Path(str(item["path"]))
        write_json(path, dict(item["payload"]))
        migrated.append(path)
    return migrated


def _binding_match(binding: dict, facts: dict) -> tuple[int, list[str]]:
    if not binding.get("active") or binding.get("kind") != "git":
        return 0, []
    binding_subpath = str(binding.get("subpath") or ".")
    workspace_subpath = str(facts.get("subpath") or ".")
    if binding_subpath not in {".", workspace_subpath}:
        return 0, []
    matched_by: list[str] = []
    score = 0
    remote = str(binding.get("remote") or "")
    if remote and remote == str(facts.get("git_identity") or ""):
        score += 55
        matched_by.append("remote")
    fingerprint = str(binding.get("repository_fingerprint") or "")
    if fingerprint and fingerprint == str(facts.get("repository_fingerprint") or ""):
        score += 40
        matched_by.append("repository_fingerprint")
    if not matched_by:
        return 0, []
    if binding_subpath == workspace_subpath:
        score += 30
        matched_by.append("subpath")
    elif binding_subpath == ".":
        score += 10
        matched_by.append("repository_root")
    return min(score, 100), matched_by


def _identity_result(
    *,
    manifest: dict | None,
    derived_aliases: list[str],
    facts: dict,
    source: str,
    confidence: float,
    matched_by: list[str],
    binding: dict | None = None,
    alternatives: list[dict] | None = None,
    ambiguous_project_keys: list[str] | None = None,
) -> dict:
    project_key = str((manifest or {}).get("project_key") or derived_aliases[0])
    aliases = _string_list([
        project_key,
        *((manifest or {}).get("aliases") or []),
        *((manifest or {}).get("legacy_keys") or []),
        *derived_aliases,
    ])
    return {
        "project_id": str((manifest or {}).get("project_id") or project_id_for_key(project_key)),
        "project_key": project_key,
        "aliases": aliases,
        "source": source,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "matched_by": list(matched_by),
        "binding": dict(binding or {}),
        "git_identity": str(facts.get("git_identity") or ""),
        "workspace_identity": dict(facts),
        "manifest": manifest,
        "alternatives": list(alternatives or []),
        "ambiguous_project_keys": list(ambiguous_project_keys or []),
    }


def resolve_project_identity(
    kb_root: Path,
    workspace: Path,
    goal: str | None = None,
    *,
    aha_root: Path | None = None,
) -> dict:
    """Resolve a workspace with explainable local/shared binding precedence."""
    workspace = _normalize_workspace_path(workspace)
    facts = git_workspace_facts(workspace)
    derived_aliases = _derived_project_key_aliases_from_facts(
        workspace,
        facts,
        goal=goal,
    )
    workspace_path = str(facts.get("workspace_path") or _workspace_binding_key(workspace))
    local_binding = next(
        (
            item
            for item in _read_local_project_bindings(aha_root)
            if item["workspace_path"] == workspace_path
        ),
        None,
    )
    if local_binding is not None and (
        not facts.get("is_git") or local_binding.get("binding_mode") == "explicit"
    ):
        manifest = resolve_project_manifest(kb_root, local_binding["project_key"])
        if manifest is not None:
            return _identity_result(
                manifest=manifest,
                derived_aliases=derived_aliases,
                facts=facts,
                source="local_binding",
                confidence=1.0,
                matched_by=["local_workspace_path"],
                binding=local_binding,
            )

    scored: list[dict] = []
    for raw_manifest in list_project_manifests(kb_root):
        if raw_manifest.get("redirect_to"):
            continue
        for binding in raw_manifest.get("bindings") or []:
            score, matched_by = _binding_match(binding, facts)
            if score:
                scored.append({
                    "score": score,
                    "matched_by": matched_by,
                    "manifest": raw_manifest,
                    "binding": binding,
                })
    scored.sort(
        key=lambda item: (
            -int(item["score"]),
            str((item["manifest"] or {}).get("project_key") or ""),
        )
    )
    if scored:
        top_score = int(scored[0]["score"])
        top = [item for item in scored if int(item["score"]) == top_score]
        unique_projects = {item["manifest"]["project_key"] for item in top}
        alternatives: list[dict] = []
        alternative_projects: set[str] = set()
        for item in scored:
            project_key = str(item["manifest"]["project_key"])
            if project_key in alternative_projects:
                continue
            alternatives.append({
                "project_id": item["manifest"]["project_id"],
                "project_key": project_key,
                "display_name": item["manifest"]["display_name"],
                "confidence": round(int(item["score"]) / 100, 3),
                "matched_by": item["matched_by"],
                "binding_id": item["binding"]["binding_id"],
            })
            alternative_projects.add(project_key)
            if len(alternatives) >= 8:
                break
        if len(unique_projects) == 1:
            selected = top[0]
            return _identity_result(
                manifest=selected["manifest"],
                derived_aliases=derived_aliases,
                facts=facts,
                source="manifest",
                confidence=top_score / 100,
                matched_by=selected["matched_by"],
                binding=selected["binding"],
                alternatives=alternatives,
            )
        return _identity_result(
            manifest=None,
            derived_aliases=derived_aliases,
            facts=facts,
            source="ambiguous",
            confidence=top_score / 100,
            matched_by=top[0]["matched_by"],
            alternatives=alternatives,
            ambiguous_project_keys=sorted(unique_projects),
        )

    return _identity_result(
        manifest=None,
        derived_aliases=derived_aliases,
        facts=facts,
        source="derived_git" if facts.get("is_git") else "workspace_fallback",
        confidence=0.4 if facts.get("is_git") else 0.2,
        matched_by=["derived_remote_subpath"] if facts.get("is_git") else ["derived_workspace"],
    )


def _default_display_name(project_key: str) -> str:
    marker = project_key.find("-git-")
    return project_key[:marker] if marker > 0 else project_key


def create_project_identity(
    kb_root: Path,
    project_key: str,
    *,
    display_name: str | None = None,
) -> dict:
    key = validate_project_key(project_key)
    target_dir = Path(kb_root) / PROJECTS_DIR / key
    target_dir.mkdir(parents=True, exist_ok=True)
    existing = read_project_manifest(kb_root, key)
    if existing is not None:
        return existing
    now = utc_now()
    write_project_manifest(kb_root, key, {
        "project_id": project_id_for_key(key),
        "slug": key,
        "display_name": str(display_name or "").strip() or _default_display_name(key),
        "bindings": [],
        "aliases": [],
        "related_projects": [],
        "revision": 1,
        "created_at": now,
        "updated_at": now,
    })
    return read_project_manifest(kb_root, key) or {"project_key": key}


def bind_project_identity(
    kb_root: Path,
    workspace: Path,
    target_project_key: str,
    *,
    display_name: str | None = None,
    aha_root: Path | None = None,
    binding_scope: str = "shared",
    resolve_conflicts: bool = False,
) -> dict:
    """Bind a Git identity or local workspace path to an existing KB project."""
    target_key = validate_project_key(target_project_key)
    target_dir = Path(kb_root) / PROJECTS_DIR / target_key
    if not target_dir.is_dir():
        raise FileNotFoundError(f"knowledge project not found: {target_key}")
    workspace = _normalize_workspace_path(Path(workspace).expanduser())
    facts = git_workspace_facts(workspace)
    git_identity = str(facts.get("git_identity") or "")
    requested_scope = str(binding_scope or "shared").strip().lower()
    if requested_scope not in {"shared", "local"}:
        raise ProjectIdentityError("binding_scope must be shared or local")

    if requested_scope == "local" or not git_identity:
        if aha_root is None:
            raise ProjectIdentityError("aha_root is required for a local binding")
        existing = read_project_manifest(kb_root, target_key)
        if existing is None:
            create_project_identity(kb_root, target_key, display_name=display_name)
        _write_local_project_binding(
            aha_root,
            workspace,
            target_key,
            explicit=bool(facts.get("is_git")),
        )
        result = resolve_project_identity(
            kb_root,
            workspace,
            aha_root=aha_root,
        )
        result["path"] = str(project_manifest_path(kb_root, target_key))
        result["local_binding_path"] = str(local_project_bindings_path(aha_root))
        return result

    existing = read_project_manifest(kb_root, target_key)
    now = utc_now()
    new_binding = _git_binding(
        remote=git_identity,
        repository_fingerprint=str(facts.get("repository_fingerprint") or ""),
        subpath=str(facts.get("subpath") or "."),
        created_at=now,
        updated_at=now,
    )
    existing_bindings = list((existing or {}).get("bindings") or [])
    prior_binding = next(
        (
            binding
            for binding in existing_bindings
            if binding.get("binding_id") == new_binding["binding_id"]
        ),
        None,
    )
    if prior_binding is not None:
        new_binding["created_at"] = str(prior_binding.get("created_at") or now)
    for manifest in list_project_manifests(kb_root):
        if manifest.get("redirect_to"):
            continue
        for binding in manifest.get("bindings") or []:
            if not binding.get("active"):
                continue
            same_remote_scope = (
                binding.get("remote") == new_binding["remote"]
                and binding.get("subpath") == new_binding["subpath"]
            )
            same_fingerprint_scope = (
                bool(new_binding.get("repository_fingerprint"))
                and binding.get("repository_fingerprint") == new_binding["repository_fingerprint"]
                and binding.get("subpath") == new_binding["subpath"]
            )
            if (same_remote_scope or same_fingerprint_scope) and manifest["project_key"] != target_key:
                if not resolve_conflicts:
                    raise ProjectIdentityConflict(
                        f"Workspace identity is already bound to project {manifest['project_key']}"
                    )
                conflict_now = utc_now()
                updated_bindings = []
                for existing_binding in manifest.get("bindings") or []:
                    item = dict(existing_binding)
                    if item.get("binding_id") == binding.get("binding_id") and item.get("active"):
                        item.update({
                            "active": False,
                            "removed_at": conflict_now,
                            "updated_at": conflict_now,
                        })
                    updated_bindings.append(item)
                write_project_manifest(kb_root, manifest["project_key"], {
                    **manifest,
                    "bindings": updated_bindings,
                    "revision": int(manifest.get("revision") or 0) + 1,
                    "updated_at": conflict_now,
                })

    derived_aliases = derived_project_key_aliases(workspace)
    known_directory_aliases = [
        key
        for key in derived_aliases
        if key != target_key and (Path(kb_root) / PROJECTS_DIR / key).is_dir()
    ]
    manifest = {
        "project_id": str((existing or {}).get("project_id") or project_id_for_key(target_key)),
        "project_key": target_key,
        "slug": str((existing or {}).get("slug") or target_key),
        "display_name": (
            str(display_name or "").strip()
            or str((existing or {}).get("display_name") or "").strip()
            or _default_display_name(target_key)
        ),
        # An explicit bind is also the supported way to reactivate a tombstoned
        # binding. Remove the prior record first so equal second-resolution
        # timestamps cannot make the tombstone win the local user action.
        "bindings": _merge_bindings(
            [
                binding
                for binding in existing_bindings
                if binding.get("binding_id") != new_binding["binding_id"]
            ],
            [new_binding],
        ),
        "aliases": _string_list(
            [*((existing or {}).get("legacy_keys", [])), *known_directory_aliases]
        ),
        "related_projects": list((existing or {}).get("related_projects") or []),
        "revision": int((existing or {}).get("revision") or 0) + 1,
        "created_at": str((existing or {}).get("created_at") or now),
        "updated_at": now,
    }
    path = write_project_manifest(kb_root, target_key, manifest)
    result = resolve_project_identity(kb_root, workspace, aha_root=aha_root)
    result["path"] = str(path)
    return result


def unbind_project_identity(
    kb_root: Path,
    workspace: Path,
    *,
    aha_root: Path | None = None,
    binding_scope: str = "auto",
    binding_id: str | None = None,
) -> dict:
    """Remove the current workspace binding without deleting project knowledge."""
    workspace = _normalize_workspace_path(Path(workspace).expanduser())
    identity = resolve_project_identity(
        kb_root,
        workspace,
        aha_root=aha_root,
    )
    source = str(identity.get("source") or "")
    if source == "ambiguous":
        raise ProjectIdentityConflict(
            "Git identity matches multiple knowledge projects; resolve the conflict before unbinding"
        )
    if source not in {"manifest", "local_binding"}:
        raise ProjectIdentityError("the current workspace is not bound to a knowledge project")

    project_key = validate_project_key(str(identity.get("project_key") or ""))
    requested_scope = str(binding_scope or "auto").strip().lower()
    if requested_scope not in {"auto", "local", "shared"}:
        raise ProjectIdentityError("binding_scope must be auto, local, or shared")
    resolved_scope = (
        "local" if requested_scope == "local"
        else "shared" if requested_scope == "shared"
        else "shared" if source == "manifest"
        else "local"
    )
    synced_changed = False
    if resolved_scope == "shared":
        if source != "manifest":
            raise ProjectIdentityError("the current workspace has no shared Git binding")
        existing = read_project_manifest(kb_root, project_key)
        if existing is None:
            raise ProjectIdentityError(f"project manifest not found: {project_key}")
        selected_id = str(binding_id or (identity.get("binding") or {}).get("binding_id") or "")
        now = utc_now()
        changed = False
        bindings: list[dict] = []
        for binding in existing.get("bindings") or []:
            item = dict(binding)
            if item.get("binding_id") == selected_id and item.get("active"):
                item.update({"active": False, "removed_at": now, "updated_at": now})
                changed = True
            bindings.append(item)
        if not changed:
            raise ProjectIdentityError("shared binding is not present in the project manifest")
        write_project_manifest(kb_root, project_key, {
            **existing,
            "bindings": bindings,
            "revision": int(existing.get("revision") or 0) + 1,
            "updated_at": now,
        })
        synced_changed = True
    elif aha_root is None or not _remove_local_project_binding(aha_root, workspace):
        raise ProjectIdentityError("local workspace binding is not present")

    result = resolve_project_identity(
        kb_root,
        workspace,
        aha_root=aha_root,
    )
    result.update({
        "unbound_project_key": project_key,
        "binding_scope": resolved_scope,
        "synced_changed": synced_changed,
    })
    return result


def update_project_relations(
    kb_root: Path,
    project_key: str,
    related_projects: object,
    *,
    create_manifest: bool = False,
) -> dict:
    key = validate_project_key(project_key)
    existing = read_project_manifest(kb_root, key)
    if existing is None:
        if not create_manifest:
            raise ProjectIdentityError("bind the current repository before editing related projects")
        if not (Path(kb_root) / PROJECTS_DIR / key).is_dir():
            raise FileNotFoundError(f"knowledge project not found: {key}")
        now = utc_now()
        existing = {
            "project_id": project_id_for_key(key),
            "slug": key,
            "display_name": _default_display_name(key),
            "bindings": [],
            "aliases": [],
            "related_projects": [],
            "revision": 0,
            "created_at": now,
        }
    relations = normalize_project_relations(
        related_projects,
        current_project_key=key,
        strict=True,
    )
    for relation in relations:
        target_key = relation["project_key"]
        if target_key in {key, *list(existing.get("legacy_keys") or [])}:
            raise ProjectIdentityError("a project cannot reference itself or one of its legacy keys")
        if not (Path(kb_root) / PROJECTS_DIR / target_key).is_dir():
            raise FileNotFoundError(f"knowledge project not found: {target_key}")
    manifest = {
        "project_id": existing.get("project_id") or project_id_for_key(key),
        "project_key": key,
        "slug": existing.get("slug") or key,
        "display_name": existing["display_name"],
        "bindings": list(existing.get("bindings") or []),
        "aliases": list(existing.get("aliases") or existing.get("legacy_keys") or []),
        "related_projects": relations,
        "revision": int(existing.get("revision") or 0) + 1,
        "created_at": existing.get("created_at") or utc_now(),
        "updated_at": utc_now(),
    }
    path = write_project_manifest(kb_root, key, manifest)
    result = read_project_manifest(kb_root, key) or manifest
    result["path"] = str(path)
    return result


def project_merge_plan(kb_root: Path, source_project_key: str, target_project_key: str) -> dict:
    source_key = validate_project_key(source_project_key)
    target_key = validate_project_key(target_project_key)
    if source_key == target_key:
        raise ProjectIdentityError("source and target project must differ")
    source_dir = Path(kb_root) / PROJECTS_DIR / source_key
    target_dir = Path(kb_root) / PROJECTS_DIR / target_key
    if not source_dir.is_dir():
        raise FileNotFoundError(f"knowledge project not found: {source_key}")
    if not target_dir.is_dir():
        raise FileNotFoundError(f"knowledge project not found: {target_key}")
    moves: list[str] = []
    duplicates: list[str] = []
    conflicts: list[str] = []
    for source in sorted(path for path in source_dir.rglob("*") if path.is_file()):
        relative = source.relative_to(source_dir)
        if relative.as_posix() == PROJECT_MANIFEST_FILE:
            continue
        target = target_dir / relative
        if not target.exists():
            moves.append(relative.as_posix())
            continue
        try:
            same = source.read_bytes() == target.read_bytes()
        except OSError:
            same = False
        (duplicates if same else conflicts).append(relative.as_posix())
    return {
        "source_project_key": source_key,
        "target_project_key": target_key,
        "moves": moves,
        "duplicates": duplicates,
        "conflicts": conflicts,
        "move_count": len(moves),
        "duplicate_count": len(duplicates),
        "conflict_count": len(conflicts),
    }


def _rewrite_markdown_project_key(path: Path, source_key: str, target_key: str) -> None:
    if path.suffix.lower() != ".md":
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    if not text.startswith("---\n"):
        return
    end = text.find("\n---", 4)
    if end < 0:
        return
    try:
        meta = json.loads(text[4:end])
    except (TypeError, ValueError):
        return
    if not isinstance(meta, dict) or str(meta.get("project_key") or "") != source_key:
        return
    meta["project_key"] = target_key
    path.write_text(
        "---\n" + json.dumps(meta, indent=2, ensure_ascii=False) + text[end:],
        encoding="utf-8",
    )


def _rebind_local_project_key(aha_root: Path | None, source_key: str, target_key: str) -> None:
    if aha_root is None:
        return
    path = local_project_bindings_path(aha_root)
    bindings = _read_local_project_bindings(aha_root)
    changed = False
    now = utc_now()
    for binding in bindings:
        if binding.get("project_key") != source_key:
            continue
        binding["project_key"] = target_key
        binding["project_id"] = project_id_for_key(target_key)
        binding["updated_at"] = now
        changed = True
    if changed:
        write_json(path, {
            "schema_version": LOCAL_PROJECT_BINDINGS_SCHEMA_VERSION,
            "bindings": sorted(bindings, key=lambda item: item["workspace_path"]),
        })


def merge_project_identities(
    kb_root: Path,
    source_project_key: str,
    target_project_key: str,
    *,
    aha_root: Path | None = None,
    dry_run: bool = True,
) -> dict:
    plan = project_merge_plan(kb_root, source_project_key, target_project_key)
    if dry_run:
        return {**plan, "applied": False}
    source_key = plan["source_project_key"]
    target_key = plan["target_project_key"]
    source_dir = Path(kb_root) / PROJECTS_DIR / source_key
    target_dir = Path(kb_root) / PROJECTS_DIR / target_key
    source_manifest = read_project_manifest(kb_root, source_key)
    target_manifest = read_project_manifest(kb_root, target_key)
    if target_manifest is None:
        target_manifest = create_project_identity(kb_root, target_key)
    archive_root = target_dir / ".merge_conflicts" / source_key

    for relative_text in plan["moves"]:
        relative = Path(relative_text)
        source = source_dir / relative
        target = target_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        _rewrite_markdown_project_key(target, source_key, target_key)
    for relative_text in plan["conflicts"]:
        relative = Path(relative_text)
        source = source_dir / relative
        archive = archive_root / relative
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, archive)
        _rewrite_markdown_project_key(archive, source_key, target_key)

    source_relations = list((source_manifest or {}).get("related_projects") or [])
    target_relations = list((target_manifest or {}).get("related_projects") or [])
    combined_relations = normalize_project_relations(
        [*target_relations, *source_relations],
        current_project_key=target_key,
    )
    now = utc_now()
    write_project_manifest(kb_root, target_key, {
        **target_manifest,
        "bindings": _merge_bindings(
            target_manifest.get("bindings"),
            (source_manifest or {}).get("bindings"),
        ),
        "aliases": _string_list([
            *(target_manifest.get("aliases") or []),
            source_key,
            *((source_manifest or {}).get("aliases") or []),
            *((source_manifest or {}).get("legacy_keys") or []),
        ]),
        "related_projects": combined_relations,
        "revision": int(target_manifest.get("revision") or 0) + 1,
        "updated_at": now,
    })

    for manifest in list_project_manifests(kb_root):
        if manifest["project_key"] in {source_key, target_key} or manifest.get("redirect_to"):
            continue
        changed = False
        relations: list[dict] = []
        for relation in manifest.get("related_projects") or []:
            item = dict(relation)
            if item.get("project_key") == source_key:
                item["project_key"] = target_key
                changed = True
            relations.append(item)
        if changed:
            write_project_manifest(kb_root, manifest["project_key"], {
                **manifest,
                "related_projects": normalize_project_relations(
                    relations,
                    current_project_key=manifest["project_key"],
                ),
                "revision": int(manifest.get("revision") or 0) + 1,
                "updated_at": now,
            })

    _rebind_local_project_key(aha_root, source_key, target_key)
    shutil.rmtree(source_dir)
    source_dir.mkdir(parents=True, exist_ok=True)
    write_project_manifest(kb_root, source_key, {
        "project_id": str((source_manifest or {}).get("project_id") or project_id_for_key(source_key)),
        "slug": str((source_manifest or {}).get("slug") or source_key),
        "display_name": str((source_manifest or {}).get("display_name") or source_key),
        "bindings": [],
        "aliases": list((source_manifest or {}).get("aliases") or []),
        "related_projects": [],
        "revision": int((source_manifest or {}).get("revision") or 0) + 1,
        "redirect_to": target_key,
        "created_at": str((source_manifest or {}).get("created_at") or now),
        "updated_at": now,
    })
    return {
        **plan,
        "applied": True,
        "target_project_id": target_manifest.get("project_id"),
        "redirect_project_key": source_key,
        "archive_path": str(archive_root) if plan["conflicts"] else "",
    }
