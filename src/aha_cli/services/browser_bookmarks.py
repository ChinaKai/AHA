from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

from aha_cli.domain.models import utc_now
from aha_cli.services.browser_runtime import (
    browser_named_profile_id,
    browser_named_profiles_dir,
    ensure_named_browser_profile,
)
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import aha_home_path

MAX_BROWSER_BOOKMARKS = 200


def _scope_key(value: object) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in str(value or ""))


def _bookmark_scope(
    root: Path,
    run_id: str,
    task_id: str,
    browser_config: dict,
) -> tuple[Path, dict]:
    profile = str(browser_config.get("profile") or "ephemeral")
    if profile == "named":
        named = ensure_named_browser_profile(root, browser_config.get("profile_name"))
        profile_root = browser_named_profiles_dir(root) / browser_named_profile_id(named["name"])
        return profile_root / "bookmarks.json", {
            "kind": "named",
            "id": named["id"],
            "name": named["name"],
        }
    path = (
        aha_home_path(root)
        / "browser"
        / "bookmarks"
        / "tasks"
        / _scope_key(run_id)
        / f"{_scope_key(task_id)}.json"
    )
    return path, {"kind": "task", "run_id": run_id, "task_id": task_id}


def _normalize_bookmark_url(value: object) -> str:
    url = str(value or "").strip()
    if not url or len(url) > 4096:
        raise ValueError("bookmark URL must be 1-4096 characters")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("bookmark URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("bookmark URL must not contain credentials")
    return url


def _normalize_bookmark_title(value: object, url: str) -> str:
    title = " ".join(str(value or "").split())
    if not title:
        title = urlsplit(url).hostname or url
    return title[:160]


def _bookmark_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def _normalized_items(payload: object) -> list[dict]:
    raw_items = payload.get("items") if isinstance(payload, dict) else []
    items: list[dict] = []
    seen: set[str] = set()
    for raw in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(raw, dict):
            continue
        try:
            url = _normalize_bookmark_url(raw.get("url"))
        except ValueError:
            continue
        item_id = _bookmark_id(url)
        if item_id in seen:
            continue
        seen.add(item_id)
        items.append(
            {
                "id": item_id,
                "title": _normalize_bookmark_title(raw.get("title"), url),
                "url": url,
                "created_at": str(raw.get("created_at") or ""),
                "updated_at": str(raw.get("updated_at") or raw.get("created_at") or ""),
            }
        )
        if len(items) >= MAX_BROWSER_BOOKMARKS:
            break
    return items


@contextmanager
def _bookmark_lock(path: Path):
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _read_items(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        return _normalized_items(read_json(path))
    except (OSError, ValueError):
        return []


def browser_bookmarks_snapshot(
    root: Path,
    run_id: str,
    task_id: str,
    browser_config: dict,
) -> dict:
    path, scope = _bookmark_scope(root, run_id, task_id, browser_config)
    with _bookmark_lock(path):
        items = _read_items(path)
    return {"scope": scope, "items": items, "count": len(items)}


def update_browser_bookmarks(
    root: Path,
    run_id: str,
    task_id: str,
    browser_config: dict,
    *,
    action: object,
    url: object = "",
    title: object = "",
    bookmark_id: object = "",
) -> dict:
    command = str(action or "").strip().lower()
    if command not in {"add", "remove", "toggle"}:
        raise ValueError("bookmark action must be add, remove, or toggle")
    path, scope = _bookmark_scope(root, run_id, task_id, browser_config)
    normalized_url = _normalize_bookmark_url(url) if url else ""
    normalized_id = str(bookmark_id or "").strip()
    if command in {"add", "toggle"} and not normalized_url:
        raise ValueError("bookmark URL is required")
    if command == "remove" and not (normalized_url or normalized_id):
        raise ValueError("bookmark id or URL is required")
    changed = False
    added = False
    removed = False
    with _bookmark_lock(path):
        items = _read_items(path)
        target_id = _bookmark_id(normalized_url) if normalized_url else normalized_id
        existing_index = next(
            (index for index, item in enumerate(items) if item["id"] == target_id),
            None,
        )
        if command == "toggle" and existing_index is not None:
            items.pop(existing_index)
            changed = removed = True
        elif command == "remove":
            if existing_index is not None:
                items.pop(existing_index)
                changed = removed = True
        else:
            now = utc_now()
            item = {
                "id": target_id,
                "title": _normalize_bookmark_title(title, normalized_url),
                "url": normalized_url,
                "created_at": now,
                "updated_at": now,
            }
            if existing_index is None:
                if len(items) >= MAX_BROWSER_BOOKMARKS:
                    raise ValueError(f"browser bookmarks are limited to {MAX_BROWSER_BOOKMARKS}")
                items.append(item)
                added = changed = True
            else:
                item["created_at"] = items[existing_index].get("created_at") or now
                changed = item != items[existing_index]
                items[existing_index] = item
                added = True
        if changed:
            write_json(
                path,
                {
                    "version": 1,
                    "scope": scope,
                    "updated_at": utc_now(),
                    "items": items,
                },
            )
    return {
        "scope": scope,
        "items": items,
        "count": len(items),
        "added": added,
        "removed": removed,
        "changed": changed,
    }


__all__ = [
    "MAX_BROWSER_BOOKMARKS",
    "browser_bookmarks_snapshot",
    "update_browser_bookmarks",
]
