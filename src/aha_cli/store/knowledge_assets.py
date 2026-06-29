"""Image asset storage for approved knowledge entries."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from aha_cli.store.knowledge import (
    find_entry,
    kind_for_type,
    normalize_entry_slug,
    read_entry,
    write_entry,
)

ENTRY_ASSETS_DIR = "assets"
ALLOWED_ENTRY_IMAGE_MIME = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}
MAX_ENTRY_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ENTRY_IMAGE_TOTAL_BYTES = 20 * 1024 * 1024


class EntryImageRejected(ValueError):
    """Raised when an uploaded entry image violates an asset guardrail."""


def _sniff_entry_image_mime(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if _looks_like_svg(data):
        return "image/svg+xml"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _looks_like_svg(data: bytes) -> bool:
    sample = bytes(data[:4096]).lstrip()
    if sample.startswith(b"\xef\xbb\xbf"):
        sample = sample[3:].lstrip()
    lowered = sample.lower()
    if lowered.startswith(b"<?xml"):
        end = lowered.find(b"?>")
        if end >= 0:
            lowered = lowered[end + 2:].lstrip()
    return lowered.startswith(b"<svg") and (len(lowered) == 4 or lowered[4] in b" \t\r\n>/")


def _safe_entry_asset_name(filename: str, mime: str) -> str:
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/svg+xml": ".svg", "image/webp": ".webp"}.get(mime, "")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (filename or "").rsplit("/", 1)[-1]).strip("._") or "image"
    stem = stem.rsplit(".", 1)[0][:60] or "image"
    return f"{stem}{ext}"


def _entry_asset_slug(entry: dict) -> str:
    meta = entry.get("meta", {})
    return normalize_entry_slug(str(meta.get("slug") or Path(entry.get("path") or "entry").stem))


def _entry_asset_dir(entry: dict) -> Path:
    return Path(entry["path"]).parent / ENTRY_ASSETS_DIR / _entry_asset_slug(entry)


def _entry_asset_path(entry: dict, path_text: str) -> Path | None:
    rel = str(path_text or "").strip().replace("\\", "/").lstrip("/")
    parts = [part for part in rel.split("/") if part]
    if not parts or parts[0] != ENTRY_ASSETS_DIR or any(part in {".", ".."} for part in parts):
        return None
    entry_dir_path = Path(entry["path"]).parent
    asset_root = (entry_dir_path / ENTRY_ASSETS_DIR).resolve()
    candidate = entry_dir_path.joinpath(*parts).resolve()
    try:
        candidate.relative_to(asset_root)
    except ValueError:
        return None
    return candidate


def _entry_asset_record(entry: dict, path_or_name: str) -> dict | None:
    text = str(path_or_name or "").strip().replace("\\", "/")
    if not text:
        return None
    for image in entry.get("meta", {}).get("assets") or []:
        if not isinstance(image, dict):
            continue
        if text in {str(image.get("path") or ""), str(image.get("name") or "")}:
            return dict(image)
    if text.startswith(f"{ENTRY_ASSETS_DIR}/"):
        return {"path": text, "name": text.rsplit("/", 1)[-1]}
    return {"path": f"{ENTRY_ASSETS_DIR}/{_entry_asset_slug(entry)}/{text}", "name": text}


def add_entry_image(
    root: Path,
    config: dict | None,
    identifier: str,
    *,
    data: bytes,
    filename: str,
) -> tuple[dict, dict]:
    """Validate and persist an image for a tracked entry.

    The body is left unchanged; callers insert the returned Markdown where the
    user requested it.
    """
    entry = find_entry(root, config, identifier)
    if entry is None:
        raise FileNotFoundError(f"entry not found: {identifier}")
    mime = _sniff_entry_image_mime(data or b"")
    if mime not in ALLOWED_ENTRY_IMAGE_MIME:
        raise EntryImageRejected("unsupported image type (allowed: png, jpeg, svg, webp)")
    if len(data) > MAX_ENTRY_IMAGE_BYTES:
        raise EntryImageRejected(f"image exceeds {MAX_ENTRY_IMAGE_BYTES // (1024 * 1024)}MB limit")
    meta = dict(entry.get("meta") or {})
    images = [dict(item) for item in (meta.get("assets") or []) if isinstance(item, dict)]
    current_total = sum(int(img.get("size") or 0) for img in images)
    if current_total + len(data) > MAX_ENTRY_IMAGE_TOTAL_BYTES:
        raise EntryImageRejected(f"entry image total exceeds {MAX_ENTRY_IMAGE_TOTAL_BYTES // (1024 * 1024)}MB limit")

    assets = _entry_asset_dir(entry)
    assets.mkdir(parents=True, exist_ok=True)
    name = _safe_entry_asset_name(filename, mime)
    if (assets / name).exists():
        name = f"{uuid.uuid4().hex[:8]}-{name}"
    (assets / name).write_bytes(data)
    original = (filename or "").rsplit("/", 1)[-1]
    rel = f"{ENTRY_ASSETS_DIR}/{_entry_asset_slug(entry)}/{name}"
    image = {
        "name": name,
        "original": original,
        "mime": mime,
        "size": len(data),
        "path": rel,
    }
    meta["assets"] = images + [image]
    path = write_entry(
        root,
        config=config,
        scope=str(meta.get("scope") or "project"),
        kind=kind_for_type(meta.get("type")),
        project_key_value=meta.get("project_key"),
        title=str(meta.get("title") or meta.get("slug") or "entry"),
        body=str(entry.get("body") or ""),
        meta=meta,
        slug=meta.get("slug") or _entry_asset_slug(entry),
    )
    response_image = dict(image)
    response_image["markdown"] = f"![{original or name}]({rel})"
    return read_entry(path), response_image


def read_entry_image(root: Path, config: dict | None, identifier: str, path_or_name: str) -> tuple[bytes, str] | None:
    """Return (bytes, mime) for a stored entry image, or None if absent."""
    entry = find_entry(root, config, identifier)
    if entry is None:
        return None
    record = _entry_asset_record(entry, path_or_name)
    if record is None:
        return None
    candidate = _entry_asset_path(entry, str(record.get("path") or ""))
    if candidate is None or not candidate.is_file():
        return None
    try:
        return candidate.read_bytes(), str(record.get("mime") or "application/octet-stream")
    except OSError:
        return None
