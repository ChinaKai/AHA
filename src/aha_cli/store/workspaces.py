from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import _normalized_path, workspaces_dir


def list_workspaces(root: Path) -> list[dict]:
    base = workspaces_dir(root)
    if not base.is_dir():
        return []
    workspaces: list[dict] = []
    for path in sorted(base.glob("*.json")):
        try:
            workspace = read_json(path)
        except (OSError, ValueError):
            continue
        if workspace.get("id") and workspace.get("path"):
            workspaces.append(workspace)
    return sorted(workspaces, key=lambda item: (str(item.get("name") or ""), str(item.get("id") or "")))


def get_workspace(root: Path, workspace_id: str) -> dict | None:
    if not workspace_id:
        return None
    path = workspaces_dir(root) / f"{workspace_id}.json"
    if path.exists():
        return read_json(path)
    return next((workspace for workspace in list_workspaces(root) if workspace.get("id") == workspace_id), None)


def _next_workspace_id(root: Path) -> str:
    used: set[int] = set()
    for workspace in list_workspaces(root):
        workspace_id = str(workspace.get("id") or "")
        if workspace_id.startswith("ws-") and workspace_id[3:].isdigit():
            used.add(int(workspace_id[3:]))
    index = 1
    while index in used:
        index += 1
    return f"ws-{index:03d}"


def add_workspace(root: Path, workspace_path: str | Path, name: str | None = None) -> dict:
    path = _normalized_path(Path(workspace_path))
    if not path.is_dir():
        raise ValueError(f"workspace path is not a directory: {path}")
    now = utc_now()
    for workspace in list_workspaces(root):
        if _normalized_path(Path(str(workspace.get("path")))) == path:
            if name and workspace.get("name") != name:
                workspace["name"] = name
            workspace["last_used_at"] = now
            write_json(workspaces_dir(root) / f"{workspace['id']}.json", workspace)
            return workspace
    workspace = {
        "id": _next_workspace_id(root),
        "name": name or path.name,
        "path": str(path),
        "created_at": now,
        "last_used_at": now,
    }
    write_json(workspaces_dir(root) / f"{workspace['id']}.json", workspace)
    return workspace


def resolve_workspace_path(
    root: Path,
    workspace_id: str | None = None,
    workspace_path: str | Path | None = None,
    default: str | Path | None = None,
) -> tuple[str, str | None]:
    if workspace_path:
        resolved = _normalized_path(Path(workspace_path))
        if workspace_id:
            workspace = get_workspace(root, workspace_id)
            if workspace is None:
                raise ValueError(f"workspace not found: {workspace_id}")
            if _normalized_path(Path(str(workspace["path"]))) != resolved:
                raise ValueError(f"workspace path does not match registered workspace: {workspace_id}")
        return str(resolved), workspace_id
    if workspace_id:
        workspace = get_workspace(root, workspace_id)
        if workspace is None:
            raise ValueError(f"workspace not found: {workspace_id}")
        return str(workspace["path"]), str(workspace["id"])
    fallback = _normalized_path(Path(default)) if default is not None else _normalized_path(Path.cwd())
    return str(fallback), None
