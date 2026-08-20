"""Capture inbox: raw user notes awaiting agent distillation.

This is the first stage of the third knowledge ingestion channel:

    raw note (capture/) --[agent distill]--> pending candidate --[approve]--> entry

A capture note is unstructured raw material the user dumps in to deal with
later (pasted logs, half-formed ideas, screenshots). It is neither a candidate
nor a tracked entry. Notes live as normal Markdown files under ``capture/``
with attachments under ``capture/assets/``. These are user materials and stay
syncable across machines; only generated distill logs under
``capture/distill/`` are ignored.

Phase 2 owns storage + CRUD only; the distill trigger is wired in Phase 3.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from aha_cli.domain.models import utc_now
from aha_cli.store.io import read_json, write_json
from aha_cli.store.knowledge import (
    KNOWLEDGE_GITIGNORE_FILE,
    PENDING_DIR,
    knowledge_root,
    parse_entry,
    serialize_entry,
)

CAPTURE_DIR = "capture"
LEGACY_CAPTURE_DIR = ".capture"
CAPTURE_INBOX_DIR = "inbox"
CAPTURE_ASSETS_DIR = "assets"
CAPTURE_DISTILL_DIR = "distill"
CAPTURE_SCOPES = ("personal", "project", "general")

# Image guardrails (no new dependency): allow only these types, sniffed from the
# bytes (not the filename), and bound per-image / per-note total size.
ALLOWED_IMAGE_MIME = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_NOTE_IMAGE_TOTAL_BYTES = 20 * 1024 * 1024


class ImageRejected(ValueError):
    """Raised when an uploaded image violates a capture guardrail."""


def sniff_image_mime(data: bytes) -> str | None:
    """Detect supported image types from bytes; None if unrecognized."""
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


def _safe_asset_name(filename: str, mime: str) -> str:
    """Filesystem-safe asset filename with an extension matching the sniffed mime."""
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/svg+xml": ".svg", "image/webp": ".webp"}.get(mime, "")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (filename or "").rsplit("/", 1)[-1]).strip("._") or "image"
    stem = stem.rsplit(".", 1)[0][:60] or "image"
    return f"{stem}{ext}"


def _legacy_capture_dir(root: Path, config: dict | None = None) -> Path:
    return knowledge_root(root, config) / LEGACY_CAPTURE_DIR


def capture_dir(root: Path, config: dict | None = None) -> Path:
    kb_root = knowledge_root(root, config)
    current = kb_root / CAPTURE_DIR
    legacy = kb_root / LEGACY_CAPTURE_DIR
    if legacy.is_dir() and not current.exists():
        shutil.move(str(legacy), str(current))
    return current


def _capture_dirs(root: Path, config: dict | None = None) -> list[Path]:
    current = capture_dir(root, config)
    dirs = [current]
    legacy = _legacy_capture_dir(root, config)
    if legacy.exists():
        dirs.append(legacy)
    return dirs


def _ensure_capture_gitignored(kb_root: Path) -> None:
    """Make sure .gitignore excludes process state, not raw capture material."""
    gitignore = kb_root / KNOWLEDGE_GITIGNORE_FILE
    wanted = {
        f"{PENDING_DIR}/",
        f"{CAPTURE_DIR}/{CAPTURE_DISTILL_DIR}/",
        f"{LEGACY_CAPTURE_DIR}/{CAPTURE_DISTILL_DIR}/",
    }
    obsolete = {f"{CAPTURE_DIR}/", f"{LEGACY_CAPTURE_DIR}/"}
    existing: set[str] = set()
    lines: list[str] = []
    if gitignore.exists():
        try:
            lines = [
                line
                for line in gitignore.read_text(encoding="utf-8").splitlines()
                if line.strip() not in obsolete
            ]
            existing = {line.strip() for line in lines}
        except OSError:
            lines = []
    missing = [entry for entry in sorted(wanted) if entry not in existing]
    if missing or not gitignore.exists():
        out = [line for line in lines if line.strip()] + missing
        gitignore.write_text("\n".join(out) + "\n", encoding="utf-8")


def _safe_note_filename(title: str, note_id: str, created_at: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", str(title or "").strip())
    clean = re.sub(r"\s+", "-", clean).strip(" .-")[:80] or note_id
    stamp = re.sub(r"[^0-9]", "", str(created_at or "")[:19])[:14]
    return f"{stamp + '-' if stamp else ''}{clean}-{note_id[-6:]}.md"


def _new_note_path(target: Path, record: dict) -> Path:
    inbox = target / CAPTURE_INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / _safe_note_filename(
        str(record.get("title") or ""),
        str(record.get("id") or "capture"),
        str(record.get("created_at") or ""),
    )
    if not path.exists():
        return path
    return inbox / f"{record.get('id') or uuid.uuid4().hex}.md"


def _iter_markdown_note_paths(target: Path):
    if not target.is_dir():
        return
    for path in target.rglob("*.md"):
        try:
            relative = path.relative_to(target)
        except ValueError:
            continue
        if any(
            part in {CAPTURE_ASSETS_DIR, CAPTURE_DISTILL_DIR} or part.startswith(".")
            for part in relative.parts[:-1]
        ):
            continue
        yield path


def _fallback_note_id(target: Path, path: Path) -> str:
    try:
        identity = path.relative_to(target).as_posix()
    except ValueError:
        identity = str(path)
    return "cap_" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]


def _fallback_note_title(body: str, path: Path) -> str:
    for line in str(body or "").splitlines():
        clean = line.strip()
        if clean.startswith("# ") and clean[2:].strip():
            return clean[2:].strip()
    return path.stem


def _path_timestamp(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return utc_now()


def _read_markdown_note(target: Path, path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    try:
        meta, body = parse_entry(raw)
    except (TypeError, ValueError):
        meta, body = {}, raw.strip("\n")
    now = _path_timestamp(path)
    record = dict(meta)
    record["id"] = str(record.get("id") or _fallback_note_id(target, path))
    record["type"] = "capture"
    record["title"] = str(record.get("title") or _fallback_note_title(body, path)).strip()
    record["text"] = body
    record["scope_hint"] = record.get("scope_hint") if record.get("scope_hint") in CAPTURE_SCOPES else "personal"
    record["images"] = list(record.get("images") or [])
    record["status"] = str(record.get("status") or "raw")
    record["candidate_ids"] = list(record.get("candidate_ids") or [])
    record["created_at"] = str(record.get("created_at") or now)
    record["updated_at"] = str(record.get("updated_at") or now)
    record["_path"] = str(path)
    return record


def _markdown_note_meta(record: dict) -> dict:
    return {
        key: value
        for key, value in record.items()
        if key not in {"_path", "text", "render_text"} and value is not None
    }


def _write_markdown_note(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        serialize_entry(_markdown_note_meta(record), str(record.get("text") or "")),
        encoding="utf-8",
    )


def _normalize_note_image_refs(record: dict, note_path: Path, target: Path) -> None:
    note_id = str(record.get("id") or "")
    text = str(record.get("text") or "")
    normalized: list[dict] = []
    for raw_image in list(record.get("images") or []):
        image = dict(raw_image)
        path_text = str(image.get("path") or "")
        if path_text.startswith(f"{LEGACY_CAPTURE_DIR}/"):
            path_text = f"{CAPTURE_DIR}/{path_text[len(LEGACY_CAPTURE_DIR) + 1:]}"
            image["path"] = path_text
        name = str(image.get("name") or "")
        if name:
            asset_path = target / CAPTURE_ASSETS_DIR / note_id / name
            relative = Path(os.path.relpath(asset_path, note_path.parent)).as_posix()
            api_ref = f"/api/kb/capture/image?id={note_id}&name={name}"
            text = text.replace(f"]({api_ref})", f"]({relative})")
        normalized.append(image)
    record["images"] = normalized
    record["text"] = text


def migrate_legacy_capture_notes(root: Path, config: dict | None = None) -> list[Path]:
    target = capture_dir(root, config)
    target.mkdir(parents=True, exist_ok=True)
    existing_ids: set[str] = set()
    for path in _iter_markdown_note_paths(target) or []:
        try:
            existing_ids.add(str(_read_markdown_note(target, path).get("id") or ""))
        except (OSError, ValueError):
            continue
    migrated: list[Path] = []
    for base in _capture_dirs(root, config):
        if not base.is_dir():
            continue
        for path in base.glob("*.json"):
            try:
                record = read_json(path)
            except (OSError, ValueError):
                continue
            note_id = str(record.get("id") or path.stem)
            if note_id in existing_ids:
                continue
            record["id"] = note_id
            record.setdefault("type", "capture")
            source_assets = base / CAPTURE_ASSETS_DIR / note_id
            target_assets = target / CAPTURE_ASSETS_DIR / note_id
            if base != target and source_assets.is_dir():
                target_assets.mkdir(parents=True, exist_ok=True)
                for asset in source_assets.iterdir():
                    if asset.is_file() and not (target_assets / asset.name).exists():
                        shutil.copy2(asset, target_assets / asset.name)
            note_path = _new_note_path(target, record)
            _normalize_note_image_refs(record, note_path, target)
            _write_markdown_note(note_path, record)
            path.unlink()
            migrated.append(note_path)
            existing_ids.add(note_id)
    return migrated


def _note_paths(root: Path, config: dict | None, note_id: str) -> list[Path]:
    paths: list[Path] = []
    for target in _capture_dirs(root, config):
        for path in _iter_markdown_note_paths(target) or []:
            try:
                if str(_read_markdown_note(target, path).get("id") or "") == note_id:
                    paths.append(path)
            except (OSError, ValueError):
                continue
        paths.append(target / f"{note_id}.json")
    return paths


def _existing_note_path(root: Path, config: dict | None, note_id: str) -> Path | None:
    return next((path for path in _note_paths(root, config, note_id) if path.exists()), None)


def _write_note_record(root: Path, config: dict | None, note_id: str, record: dict) -> None:
    target = capture_dir(root, config)
    record["id"] = note_id
    record["type"] = "capture"
    raw_path = record.get("_path")
    old_path = Path(str(raw_path)) if raw_path else None
    path = old_path if old_path and old_path.suffix.lower() == ".md" else _new_note_path(target, record)
    _normalize_note_image_refs(record, path, target)
    _write_markdown_note(path, record)
    if old_path and old_path != path and old_path.exists():
        old_path.unlink()
    record["_path"] = str(path)


def _distill_log_dir(root: Path, config: dict | None, note_id: str) -> Path:
    return capture_dir(root, config) / CAPTURE_DISTILL_DIR / note_id


def _distill_log_dirs(root: Path, config: dict | None, note_id: str) -> list[Path]:
    return [base / CAPTURE_DISTILL_DIR / note_id for base in _capture_dirs(root, config)]


def _safe_log_id(log_id: str) -> str:
    clean = str(log_id or "").strip()
    if not clean or not re.fullmatch(r"[A-Za-z0-9._-]+", clean):
        raise ValueError("invalid distill log id")
    return clean


def _distill_log_path(root: Path, config: dict | None, note_id: str, log_id: str) -> Path:
    return _distill_log_dir(root, config, note_id) / f"{_safe_log_id(log_id)}.json"


def _distill_log_paths(root: Path, config: dict | None, note_id: str, log_id: str) -> list[Path]:
    filename = f"{_safe_log_id(log_id)}.json"
    return [base / filename for base in _distill_log_dirs(root, config, note_id)]


def _existing_distill_log_path(root: Path, config: dict | None, note_id: str, log_id: str) -> Path | None:
    return next((path for path in _distill_log_paths(root, config, note_id, log_id) if path.exists()), None)


def create_note(
    root: Path,
    config: dict | None,
    *,
    text: str,
    scope_hint: str = "personal",
    title: str | None = None,
    images: list[dict] | None = None,
) -> dict:
    """Persist a new raw capture note and return its record."""
    target = capture_dir(root, config)
    target.mkdir(parents=True, exist_ok=True)
    _ensure_capture_gitignored(knowledge_root(root, config))
    scope_hint = scope_hint if scope_hint in CAPTURE_SCOPES else "personal"
    now = utc_now()
    note_id = "cap_" + uuid.uuid4().hex[:12]
    record = {
        "id": note_id,
        "type": "capture",
        "title": (title or "").strip(),
        "text": text or "",
        "scope_hint": scope_hint,
        "images": images or [],
        "status": "raw",
        "candidate_ids": [],
        "created_at": now,
        "updated_at": now,
    }
    path = _new_note_path(target, record)
    _write_note_record(root, config, note_id, {**record, "_path": str(path)})
    return record


def list_notes(root: Path, config: dict | None = None) -> list[dict]:
    migrate_legacy_capture_notes(root, config)
    notes: list[dict] = []
    seen: set[str] = set()
    for target in _capture_dirs(root, config):
        if not target.is_dir():
            continue
        for path in _iter_markdown_note_paths(target) or []:
            try:
                record = _read_markdown_note(target, path)
                note_id = str(record.get("id") or "")
                if note_id in seen:
                    continue
                seen.add(note_id)
                notes.append(record)
            except (OSError, ValueError):
                continue
    notes.sort(key=lambda r: str(r.get("created_at") or ""))
    return notes


def read_note(root: Path, config: dict | None, note_id: str) -> dict | None:
    migrate_legacy_capture_notes(root, config)
    path = _existing_note_path(root, config, note_id)
    if path is None:
        return None
    try:
        target = next(
            (base for base in _capture_dirs(root, config) if path.is_relative_to(base)),
            capture_dir(root, config),
        )
        record = _read_markdown_note(target, path) if path.suffix.lower() == ".md" else read_json(path)
    except (OSError, ValueError):
        return None
    record["_path"] = str(path)
    return record


def update_note(
    root: Path,
    config: dict | None,
    note_id: str,
    *,
    text: str | None = None,
    scope_hint: str | None = None,
    title: str | None = None,
    status: str | None = None,
    candidate_ids: list[str] | None = None,
    last_error: str | None = None,
    management_run_id: str | None = None,
    management_task_id: str | None = None,
) -> dict:
    """Update a raw note in place, preserving id/created_at."""
    record = read_note(root, config, note_id)
    if record is None:
        raise FileNotFoundError(f"capture note not found: {note_id}")
    if text is not None:
        record["text"] = text
    if scope_hint is not None and scope_hint in CAPTURE_SCOPES:
        record["scope_hint"] = scope_hint
    if title is not None:
        record["title"] = title.strip()
    if status is not None:
        record["status"] = status
    if candidate_ids is not None:
        record["candidate_ids"] = list(candidate_ids)
    if last_error is not None:
        record["last_error"] = last_error
    if management_run_id is not None:
        record["management_run_id"] = management_run_id
    if management_task_id is not None:
        record["management_task_id"] = management_task_id
    record["updated_at"] = utc_now()
    _write_note_record(root, config, note_id, record)
    return record


def delete_note(root: Path, config: dict | None, note_id: str) -> bool:
    migrate_legacy_capture_notes(root, config)
    deleted = False
    for path in _note_paths(root, config, note_id):
        if not path.exists():
            continue
        path.unlink()
        deleted = True
    if deleted:
        for assets in _note_asset_dirs(root, config, note_id):
            if assets.is_dir():
                shutil.rmtree(assets, ignore_errors=True)
        for logs in _distill_log_dirs(root, config, note_id):
            if logs.is_dir():
                shutil.rmtree(logs, ignore_errors=True)
    return deleted


# --------------------------------------------------------------------------- #
# Distill agent logs: debug-only sidecars under capture/distill/<note-id>/.
# They intentionally stay out of approved knowledge entries.
# --------------------------------------------------------------------------- #
def create_distill_log(root: Path, config: dict | None, note_id: str, data: dict) -> dict:
    target = _distill_log_dir(root, config, note_id)
    target.mkdir(parents=True, exist_ok=True)
    _ensure_capture_gitignored(knowledge_root(root, config))
    now = utc_now()
    log_id = str(data.get("id") or f"log_{uuid.uuid4().hex[:12]}")
    record = dict(data)
    record.update({
        "id": _safe_log_id(log_id),
        "note_id": note_id,
        "created_at": data.get("created_at") or now,
        "updated_at": now,
    })
    record.setdefault("started_at", now)
    write_json(_distill_log_path(root, config, note_id, record["id"]), record)
    return record


def update_distill_log(root: Path, config: dict | None, note_id: str, log_id: str, **updates) -> dict:
    path = _existing_distill_log_path(root, config, note_id, log_id)
    if not path:
        raise FileNotFoundError(f"distill log not found: {log_id}")
    record = read_json(path)
    record.update(updates)
    record["updated_at"] = utc_now()
    write_json(path, record)
    return record


def list_distill_logs(root: Path, config: dict | None, note_id: str) -> list[dict]:
    logs: list[dict] = []
    seen: set[str] = set()
    for target in _distill_log_dirs(root, config, note_id):
        if not target.is_dir():
            continue
        for path in target.glob("*.json"):
            try:
                record = read_json(path)
                log_id = str(record.get("id") or path.stem)
                if log_id in seen:
                    continue
                seen.add(log_id)
                record["_path"] = str(path)
                logs.append(record)
            except (OSError, ValueError):
                continue
    logs.sort(key=lambda r: str(r.get("started_at") or r.get("created_at") or ""))
    return logs


def read_distill_log(root: Path, config: dict | None, note_id: str, log_id: str | None = None) -> dict | None:
    if log_id:
        try:
            path = _existing_distill_log_path(root, config, note_id, log_id)
        except ValueError:
            return None
        if not path:
            return None
        try:
            record = read_json(path)
        except (OSError, ValueError):
            return None
        record["_path"] = str(path)
        return record
    logs = list_distill_logs(root, config, note_id)
    return logs[-1] if logs else None


# --------------------------------------------------------------------------- #
# Image assets (Phase 5a): stored as files under capture/assets/<note-id>/.
# The note keeps only lightweight metadata, never base64.
# --------------------------------------------------------------------------- #
def _note_assets_dir(root: Path, config: dict | None, note_id: str) -> Path:
    return capture_dir(root, config) / CAPTURE_ASSETS_DIR / note_id


def _note_asset_dirs(root: Path, config: dict | None, note_id: str) -> list[Path]:
    return [base / CAPTURE_ASSETS_DIR / note_id for base in _capture_dirs(root, config)]


def _asset_path_for_image(root: Path, config: dict | None, note_id: str, image: dict) -> Path | None:
    name = str(image.get("name") or "").strip()
    path_text = str(image.get("path") or "").strip()
    kb_root = knowledge_root(root, config)
    candidates: list[Path] = []
    if path_text:
        candidates.append(kb_root / path_text)
        legacy_prefix = f"{LEGACY_CAPTURE_DIR}/"
        current_prefix = f"{CAPTURE_DIR}/"
        if path_text.startswith(legacy_prefix):
            candidates.append(kb_root / CAPTURE_DIR / path_text[len(legacy_prefix):])
        if path_text.startswith(current_prefix):
            candidates.append(kb_root / LEGACY_CAPTURE_DIR / path_text[len(current_prefix):])
    if name:
        candidates.extend(assets / name for assets in _note_asset_dirs(root, config, note_id))
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate
    return None


def add_note_image(
    root: Path,
    config: dict | None,
    note_id: str,
    *,
    data: bytes,
    filename: str,
    append_ref: bool = True,
) -> dict:
    """Validate and persist an image for a note; return its metadata record.

    Raises FileNotFoundError if the note is missing, ImageRejected on a
    guardrail violation (type / size).
    """
    record = read_note(root, config, note_id)
    if record is None:
        raise FileNotFoundError(f"capture note not found: {note_id}")
    mime = sniff_image_mime(data or b"")
    if mime not in ALLOWED_IMAGE_MIME:
        raise ImageRejected("unsupported image type (allowed: png, jpeg, svg, webp)")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageRejected(f"image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)}MB limit")
    images = list(record.get("images") or [])
    current_total = sum(int(img.get("size") or 0) for img in images)
    if current_total + len(data) > MAX_NOTE_IMAGE_TOTAL_BYTES:
        raise ImageRejected(f"note image total exceeds {MAX_NOTE_IMAGE_TOTAL_BYTES // (1024 * 1024)}MB limit")

    assets = _note_assets_dir(root, config, note_id)
    assets.mkdir(parents=True, exist_ok=True)
    name = _safe_asset_name(filename, mime)
    if (assets / name).exists():
        name = f"{uuid.uuid4().hex[:8]}-{name}"
    (assets / name).write_bytes(data)
    original = (filename or "").rsplit("/", 1)[-1]
    stored_image = {
        "name": name,
        "original": original,
        "mime": mime,
        "size": len(data),
        "path": f"{CAPTURE_DIR}/{CAPTURE_ASSETS_DIR}/{note_id}/{name}",
    }
    note_path = Path(str(record.get("_path") or ""))
    src = Path(os.path.relpath(assets / name, note_path.parent)).as_posix()
    markdown = f"![{original or name}]({src})"
    record["images"] = images + [stored_image]
    if append_ref:
        record["text"] = (str(record.get("text") or "").rstrip() + f"\n\n{markdown}\n").lstrip("\n")
    record["updated_at"] = utc_now()
    _write_note_record(root, config, note_id, record)
    return {**stored_image, "src": src, "markdown": markdown}


def remove_note_image(root: Path, config: dict | None, note_id: str, name: str) -> bool:
    record = read_note(root, config, note_id)
    if record is None:
        return False
    images = list(record.get("images") or [])
    kept = [img for img in images if img.get("name") != name]
    if len(kept) == len(images):
        return False
    asset = _asset_path_for_image(root, config, note_id, {"name": name})
    if asset and asset.exists():
        asset.unlink()
    record["images"] = kept
    record["updated_at"] = utc_now()
    _write_note_record(root, config, note_id, record)
    return True


def _find_source_note_id(root: Path, config: dict | None, candidate: dict) -> str | None:
    """Compat reverse lookup: a note whose candidate_ids contains this candidate."""
    cid = str(candidate.get("id") or candidate.get("identity") or "").strip()
    if not cid:
        return None
    for note in list_notes(root, config):
        if cid in (note.get("candidate_ids") or []):
            return str(note.get("id"))
    return None


def promote_assets_for_entry(
    root: Path,
    config: dict | None,
    candidate: dict,
    *,
    scope: str,
    kind: str,
    project_key: str | None,
    slug: str,
) -> dict | None:
    """Copy a source capture note's images into a knowledge entry's assets dir.

    Phase 5b: called from ``approve_candidate``. Copy-only (no git), idempotent
    (skips files that already exist, never overwrites), and the raw
    ``capture/assets`` is left intact. Returns ``{source_note_id, assets,
    body_suffix}`` to splice into the entry, or ``None`` when there is nothing to
    promote.
    """
    note_id = str(candidate.get("source_note_id") or "").strip() or _find_source_note_id(root, config, candidate)
    if not note_id:
        return None
    note = read_note(root, config, note_id)
    if note is None or not note.get("images"):
        return None

    # Lazy import to avoid a store import cycle (knowledge imports nothing here).
    from aha_cli.store.knowledge import entry_dir, knowledge_root

    kb_root = knowledge_root(root, config)
    try:
        dest = entry_dir(kb_root, scope, kind, project_key) / CAPTURE_ASSETS_DIR / slug
    except ValueError:
        return None
    dest.mkdir(parents=True, exist_ok=True)

    assets_meta: list[dict] = []
    refs: list[str] = []
    for img in note["images"]:
        name = img.get("name")
        src = _asset_path_for_image(root, config, note_id, img)
        if not name or src is None or not src.is_file():
            continue
        target = dest / name
        if not target.exists():  # idempotent: never overwrite an existing asset
            shutil.copy2(src, target)
        rel = f"{CAPTURE_ASSETS_DIR}/{slug}/{name}"
        assets_meta.append({"name": name, "mime": img.get("mime"), "size": img.get("size"), "path": rel})
        refs.append(f"![{img.get('original') or name}]({rel})")
    if not assets_meta:
        return None
    body_suffix = "\n\n## 附图\n" + "\n".join(refs) + "\n"
    return {"source_note_id": note_id, "assets": assets_meta, "body_suffix": body_suffix}


def read_note_image(
    root: Path,
    config: dict | None,
    note_id: str,
    name: str = "",
    relative_path: str = "",
) -> tuple[bytes, str] | None:
    """Return (bytes, mime) for a stored note image, or None if absent."""
    record = read_note(root, config, note_id)
    if record is None:
        return None
    if relative_path:
        raw_path = str(record.get("_path") or "")
        if not raw_path:
            return None
        try:
            capture_root = capture_dir(root, config).resolve()
            asset = (Path(raw_path).parent / unquote(relative_path)).resolve()
            if not asset.is_relative_to(capture_root) or not asset.is_file():
                return None
            data = asset.read_bytes()
        except OSError:
            return None
        mime = sniff_image_mime(data)
        return (data, mime) if mime else None
    image = next((img for img in (record.get("images") or []) if img.get("name") == name), None)
    if image is None:
        return None
    asset = _asset_path_for_image(root, config, note_id, image)
    if asset is None or not asset.is_file():
        return None
    try:
        return asset.read_bytes(), str(image.get("mime") or "application/octet-stream")
    except OSError:
        return None


def note_text_for_web(root: Path, config: dict | None, note: dict) -> str:
    text = str(note.get("text") or "")
    raw_path = str(note.get("_path") or "")
    if not raw_path:
        return text
    note_path = Path(raw_path)
    note_id = str(note.get("id") or "")
    capture_root = capture_dir(root, config).resolve()

    def replace(match: re.Match[str]) -> str:
        target = match.group(2).strip()
        if not target or re.match(r"^(?:data:|https?:|/api/)", target, re.IGNORECASE):
            return match.group(0)
        clean = target[1:-1] if target.startswith("<") and target.endswith(">") else target
        try:
            asset = (note_path.parent / unquote(clean)).resolve()
            if not asset.is_relative_to(capture_root) or not asset.is_file():
                return match.group(0)
        except OSError:
            return match.group(0)
        api_ref = f"/api/kb/capture/image?id={quote(note_id)}&path={quote(clean, safe='')}"
        return f"{match.group(1)}{api_ref}{match.group(3)}"

    return re.sub(r"(!\[[^\]]*\]\()([^)]+)(\))", replace, text)
