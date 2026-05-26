from __future__ import annotations

import json
import mimetypes
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from aha_cli.store.config import load_config
from aha_cli.store.paths import aha_home_path
from aha_cli.web.http_utils import http_response, json_response


GAME_ENTRY_KEYS = ("webgame_entry", "web_entry", "webEntry", "entry", "main")
GAME_META_KEYS = {
    "title": ("title", "name", "display_name", "displayName"),
    "description": ("description", "desc"),
}


def _webgame_workspace(root: Path) -> Path | None:
    raw = str(load_config(root).get("webgame_workspace") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        home = aha_home_path(root)
        base = home.parent if home.name == ".aha" else home
        path = base / path
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _public_name(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and not name.startswith(".") and "/" not in name


def _game_project(root: Path, game_id: str) -> Path | None:
    if not _public_name(game_id):
        return None
    workspace = _webgame_workspace(root)
    if not workspace:
        return None
    project = workspace / game_id
    try:
        root_path = workspace.resolve(strict=True)
        project_path = project.resolve(strict=True)
        project_path.relative_to(root_path)
    except (OSError, RuntimeError, ValueError):
        return None
    return project_path if project_path.is_dir() else None


def _safe_asset_path(project: Path, raw_path: str) -> Path | None:
    relative = PurePosixPath(unquote(raw_path))
    if relative.is_absolute() or not relative.parts:
        return None
    if any(part in {"", ".", ".."} or part.startswith(".") for part in relative.parts):
        return None
    try:
        root = project.resolve(strict=True)
        path = root.joinpath(*relative.parts).resolve(strict=True)
        path.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return path if path.is_file() else None


def _game_metadata(project: Path) -> dict:
    try:
        payload = json.loads((project / "game.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _metadata_string(payload: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _project_entry_path(project: Path, payload: dict | None = None) -> Path | None:
    metadata = payload if payload is not None else _game_metadata(project)
    configured = _metadata_string(metadata, GAME_ENTRY_KEYS)
    if configured:
        entry = _safe_asset_path(project, configured)
        if entry:
            return entry
    return _safe_asset_path(project, "index.html")


def _content_type(path: Path) -> str:
    known = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".wasm": "application/wasm",
    }.get(path.suffix)
    if known:
        return known
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _game_record(project: Path) -> dict:
    metadata = _game_metadata(project)
    game_id = project.name
    title = _metadata_string(metadata, GAME_META_KEYS["title"]) or game_id
    description = _metadata_string(metadata, GAME_META_KEYS["description"])
    return {
        "id": game_id,
        "title": title,
        "description": description,
        "href": f"/games/{game_id}/",
        "available": bool(_project_entry_path(project, metadata)),
    }


def _game_catalog(root: Path) -> list[dict]:
    workspace = _webgame_workspace(root)
    if not workspace:
        return []
    games = []
    for child in sorted(workspace.iterdir(), key=lambda item: item.name.lower()):
        if child.is_dir() and _public_name(child.name):
            games.append(_game_record(child))
    return games


def _split_game_path(path: str) -> tuple[str, str] | None:
    prefix = "/games/"
    if not path.startswith(prefix):
        return None
    suffix = path.removeprefix(prefix)
    raw_game_id, sep, raw_asset = suffix.partition("/")
    game_id = unquote(raw_game_id)
    if not _public_name(game_id):
        return None
    return game_id, raw_asset if sep else ""


def game_route_response(root: Path, run_id: str, method: str, path: str) -> bytes | None:
    if method not in {"GET", "HEAD"}:
        return None
    if path == "/api/games":
        return json_response({"games": _game_catalog(root)})

    split = _split_game_path(path)
    if not split:
        return None
    game_id, relative_path = split
    project = _game_project(root, game_id)
    if not project:
        return http_response("404 Not Found", b"not found\n")

    if relative_path in {"", "index.html"}:
        entry = _project_entry_path(project)
        if not entry:
            return http_response("404 Not Found", b"not found\n")
        body = b"" if method == "HEAD" else entry.read_bytes()
        return http_response("200 OK", body, _content_type(entry))

    asset_path = relative_path.removeprefix("assets/")
    asset = _safe_asset_path(project, asset_path)
    if not asset:
        return http_response("404 Not Found", b"not found\n")
    body = b"" if method == "HEAD" else asset.read_bytes()
    return http_response("200 OK", body, _content_type(asset))
