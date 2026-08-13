"""Stable project identity manifests stored inside the synchronized knowledge base."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

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

PROJECT_IDENTITY_SCHEMA_VERSION = 2
LOCAL_PROJECT_BINDINGS_SCHEMA_VERSION = 1
PROJECT_MANIFEST_FILE = "project.json"
LOCAL_PROJECT_BINDINGS_FILE = "project_identity_bindings.json"
PROJECTS_DIR = "projects"
PROJECT_RELATION_TYPES = ("upstream", "sdk", "fork", "reference", "other")
MAX_PROJECT_RELATIONS = 5
MAX_PROJECT_RELATION_NOTE_LENGTH = 240
_PROJECT_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


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
    """Best-effort read of a workspace's origin without invoking Git."""
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


def derived_project_key_aliases(workspace: Path, goal: str | None = None) -> list[str]:
    """Return the current remote/workspace-derived key and legacy aliases."""
    workspace = Path(workspace).expanduser()
    remote = normalize_git_remote(git_remote_for(workspace))
    if remote:
        digest = hashlib.sha1(remote.encode("utf-8")).hexdigest()[:12]
        repo_name = remote.rsplit("/", 1)[-1] or "repo"
        preferred = f"{slugify(repo_name, max_length=40)}-git-{digest}"
        legacy = f"git-{digest}"
        return [preferred, legacy] if preferred != legacy else [preferred]
    basis = "-".join(part for part in [(goal or "").strip(), workspace.name] if part)
    if not basis:
        basis = "workspace"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:8]
    return [f"ws-{slugify(basis)}-{digest}"]


def validate_project_key(project_key: str) -> str:
    key = str(project_key or "").strip()
    if not _PROJECT_KEY_RE.fullmatch(key):
        raise ProjectIdentityError(
            "project_key must use 1-128 letters, numbers, dots, underscores, or hyphens"
        )
    return key


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
            "workspace_path": workspace_path,
            "project_key": project_key,
            "created_at": str(item.get("created_at") or ""),
            "updated_at": str(item.get("updated_at") or ""),
        })
        seen.add(workspace_path)
    return bindings


def _write_local_project_binding(
    aha_root: Path,
    workspace: Path,
    project_key: str,
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
        "workspace_path": workspace_path,
        "project_key": target_key,
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
    identities: list[str] = []
    for value in _string_list(data.get("git_identities")):
        normalized = normalize_git_remote(value)
        if normalized and normalized not in identities:
            identities.append(normalized)
    legacy_keys: list[str] = []
    for value in _string_list(data.get("legacy_keys")):
        try:
            legacy = validate_project_key(value)
        except ProjectIdentityError:
            continue
        if legacy != key and legacy not in legacy_keys:
            legacy_keys.append(legacy)
    return {
        "schema_version": int(data.get("schema_version") or PROJECT_IDENTITY_SCHEMA_VERSION),
        "project_key": key,
        "display_name": str(data.get("display_name") or key).strip() or key,
        "git_identities": identities,
        "legacy_keys": legacy_keys,
        "related_projects": normalize_project_relations(
            data.get("related_projects"),
            current_project_key=key,
        ),
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


def resolve_project_identity(
    kb_root: Path,
    workspace: Path,
    goal: str | None = None,
    *,
    aha_root: Path | None = None,
) -> dict:
    """Resolve a workspace through synced manifests before using derived keys."""
    workspace = _normalize_workspace_path(workspace)
    git_identity = normalize_git_remote(git_remote_for(workspace))
    derived_aliases = derived_project_key_aliases(workspace, goal=goal)
    matches = [
        manifest
        for manifest in list_project_manifests(kb_root)
        if git_identity and git_identity in manifest.get("git_identities", [])
    ]
    if len(matches) == 1:
        manifest = matches[0]
        aliases = _string_list(
            [manifest["project_key"], *manifest.get("legacy_keys", []), *derived_aliases]
        )
        return {
            "project_key": manifest["project_key"],
            "aliases": aliases,
            "source": "manifest",
            "git_identity": git_identity,
            "manifest": manifest,
            "ambiguous_project_keys": [],
        }
    if not git_identity:
        workspace_path = _workspace_binding_key(workspace)
        local_binding = next(
            (
                item
                for item in _read_local_project_bindings(aha_root)
                if item["workspace_path"] == workspace_path
            ),
            None,
        )
        if local_binding is not None:
            manifest = read_project_manifest(kb_root, local_binding["project_key"])
            if manifest is not None:
                aliases = _string_list(
                    [manifest["project_key"], *manifest.get("legacy_keys", []), *derived_aliases]
                )
                return {
                    "project_key": manifest["project_key"],
                    "aliases": aliases,
                    "source": "local_binding",
                    "git_identity": "",
                    "manifest": manifest,
                    "ambiguous_project_keys": [],
                }
    source = "ambiguous" if len(matches) > 1 else ("derived_git" if git_identity else "workspace_fallback")
    return {
        "project_key": derived_aliases[0],
        "aliases": derived_aliases,
        "source": source,
        "git_identity": git_identity,
        "manifest": None,
        "ambiguous_project_keys": [manifest["project_key"] for manifest in matches],
    }


def _default_display_name(project_key: str) -> str:
    marker = project_key.find("-git-")
    return project_key[:marker] if marker > 0 else project_key


def bind_project_identity(
    kb_root: Path,
    workspace: Path,
    target_project_key: str,
    *,
    display_name: str | None = None,
    aha_root: Path | None = None,
) -> dict:
    """Bind a Git identity or local workspace path to an existing KB project."""
    target_key = validate_project_key(target_project_key)
    target_dir = Path(kb_root) / PROJECTS_DIR / target_key
    if not target_dir.is_dir():
        raise FileNotFoundError(f"knowledge project not found: {target_key}")
    workspace = Path(workspace).expanduser()
    git_identity = normalize_git_remote(git_remote_for(workspace))
    if not git_identity:
        if aha_root is None:
            raise ProjectIdentityError(
                "workspace has no Git origin; aha_root is required for a local binding"
            )
        existing = read_project_manifest(kb_root, target_key)
        now = utc_now()
        if existing is None:
            path = project_manifest_path(kb_root, target_key)
            write_json(path, {
                "schema_version": PROJECT_IDENTITY_SCHEMA_VERSION,
                "project_key": target_key,
                "display_name": (
                    str(display_name or "").strip() or _default_display_name(target_key)
                ),
                "git_identities": [],
                "legacy_keys": [],
                "related_projects": [],
                "created_at": now,
                "updated_at": now,
            })
        _write_local_project_binding(aha_root, workspace, target_key)
        result = resolve_project_identity(
            kb_root,
            workspace,
            aha_root=aha_root,
        )
        result["path"] = str(project_manifest_path(kb_root, target_key))
        result["local_binding_path"] = str(local_project_bindings_path(aha_root))
        return result
    for manifest in list_project_manifests(kb_root):
        if git_identity not in manifest.get("git_identities", []):
            continue
        if manifest["project_key"] != target_key:
            raise ProjectIdentityConflict(
                f"Git identity is already bound to project {manifest['project_key']}"
            )

    existing = read_project_manifest(kb_root, target_key)
    now = utc_now()
    derived_aliases = derived_project_key_aliases(workspace)
    known_directory_aliases = [
        key
        for key in derived_aliases
        if key != target_key and (Path(kb_root) / PROJECTS_DIR / key).is_dir()
    ]
    manifest = {
        "schema_version": PROJECT_IDENTITY_SCHEMA_VERSION,
        "project_key": target_key,
        "display_name": (
            str(display_name or "").strip()
            or str((existing or {}).get("display_name") or "").strip()
            or _default_display_name(target_key)
        ),
        "git_identities": _string_list(
            [*((existing or {}).get("git_identities", [])), git_identity]
        ),
        "legacy_keys": _string_list(
            [*((existing or {}).get("legacy_keys", [])), *known_directory_aliases]
        ),
        "related_projects": list((existing or {}).get("related_projects") or []),
        "created_at": str((existing or {}).get("created_at") or now),
        "updated_at": now,
    }
    path = project_manifest_path(kb_root, target_key)
    write_json(path, manifest)
    result = resolve_project_identity(kb_root, workspace, aha_root=aha_root)
    result["path"] = str(path)
    return result


def unbind_project_identity(
    kb_root: Path,
    workspace: Path,
    *,
    aha_root: Path | None = None,
) -> dict:
    """Remove the current workspace binding without deleting project knowledge."""
    workspace = Path(workspace).expanduser()
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
    binding_scope = "git" if source == "manifest" else "local"
    synced_changed = False
    if source == "manifest":
        existing = read_project_manifest(kb_root, project_key)
        if existing is None:
            raise ProjectIdentityError(f"project manifest not found: {project_key}")
        git_identity = str(identity.get("git_identity") or "")
        remaining = [
            value
            for value in existing.get("git_identities", [])
            if value != git_identity
        ]
        if len(remaining) == len(existing.get("git_identities", [])):
            raise ProjectIdentityError("Git identity is not present in the project manifest")
        write_json(project_manifest_path(kb_root, project_key), {
            "schema_version": PROJECT_IDENTITY_SCHEMA_VERSION,
            "project_key": project_key,
            "display_name": existing["display_name"],
            "git_identities": remaining,
            "legacy_keys": list(existing.get("legacy_keys") or []),
            "related_projects": list(existing.get("related_projects") or []),
            "created_at": existing.get("created_at") or utc_now(),
            "updated_at": utc_now(),
        })
        synced_changed = True
    if aha_root is not None:
        _remove_local_project_binding(aha_root, workspace)

    result = resolve_project_identity(
        kb_root,
        workspace,
        aha_root=aha_root,
    )
    result.update({
        "unbound_project_key": project_key,
        "binding_scope": binding_scope,
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
            "display_name": _default_display_name(key),
            "git_identities": [],
            "legacy_keys": [],
            "related_projects": [],
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
        "schema_version": PROJECT_IDENTITY_SCHEMA_VERSION,
        "project_key": key,
        "display_name": existing["display_name"],
        "git_identities": list(existing.get("git_identities") or []),
        "legacy_keys": list(existing.get("legacy_keys") or []),
        "related_projects": relations,
        "created_at": existing.get("created_at") or utc_now(),
        "updated_at": utc_now(),
    }
    path = project_manifest_path(kb_root, key)
    write_json(path, manifest)
    result = read_project_manifest(kb_root, key) or manifest
    result["path"] = str(path)
    return result
