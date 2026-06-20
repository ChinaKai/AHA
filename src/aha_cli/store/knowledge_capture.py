"""Capture inbox: raw user notes awaiting agent distillation.

This is the first stage of the third knowledge ingestion channel:

    raw note (.capture/) --[agent distill]--> pending candidate --[approve]--> entry

A capture note is unstructured raw material the user dumps in to deal with
later (pasted logs, half-formed ideas, screenshots). It is neither a candidate
nor a tracked entry. Notes live under ``.capture/`` (one JSON each) and, like
``.pending/``, are git-ignored: raw, unreviewed, possibly sensitive content
must not be committed/pushed. Only once a note is distilled into a candidate,
approved, and written as an entry does the knowledge enter the synced tree.

Phase 2 owns storage + CRUD only; the distill trigger is wired in Phase 3.
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.store.io import read_json, write_json
from aha_cli.store.knowledge import (
    KNOWLEDGE_GITIGNORE_FILE,
    PENDING_DIR,
    knowledge_root,
)

CAPTURE_DIR = ".capture"
CAPTURE_ASSETS_DIR = "assets"
CAPTURE_DISTILL_DIR = "distill"
CAPTURE_SCOPES = ("personal", "project", "general")

# Image guardrails (no new dependency): allow only these types, sniffed from the
# bytes (not the filename), and bound per-image / per-note total size.
ALLOWED_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_NOTE_IMAGE_TOTAL_BYTES = 20 * 1024 * 1024


class ImageRejected(ValueError):
    """Raised when an uploaded image violates a capture guardrail."""


def sniff_image_mime(data: bytes) -> str | None:
    """Detect png/jpeg/webp from magic bytes; None if unrecognized."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _safe_asset_name(filename: str, mime: str) -> str:
    """Filesystem-safe asset filename with an extension matching the sniffed mime."""
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, "")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (filename or "").rsplit("/", 1)[-1]).strip("._") or "image"
    stem = stem.rsplit(".", 1)[0][:60] or "image"
    return f"{stem}{ext}"


def capture_dir(root: Path, config: dict | None = None) -> Path:
    return knowledge_root(root, config) / CAPTURE_DIR


def _ensure_capture_gitignored(kb_root: Path) -> None:
    """Make sure the KB .gitignore excludes the capture inbox (and pending)."""
    gitignore = kb_root / KNOWLEDGE_GITIGNORE_FILE
    wanted = {f"{PENDING_DIR}/", f"{CAPTURE_DIR}/"}
    existing: set[str] = set()
    lines: list[str] = []
    if gitignore.exists():
        try:
            lines = gitignore.read_text(encoding="utf-8").splitlines()
            existing = {line.strip() for line in lines}
        except OSError:
            lines = []
    missing = [entry for entry in sorted(wanted) if entry not in existing]
    if missing or not gitignore.exists():
        out = [line for line in lines if line.strip()] + missing
        gitignore.write_text("\n".join(out) + "\n", encoding="utf-8")


def _note_path(root: Path, config: dict | None, note_id: str) -> Path:
    return capture_dir(root, config) / f"{note_id}.json"


def _distill_log_dir(root: Path, config: dict | None, note_id: str) -> Path:
    return capture_dir(root, config) / CAPTURE_DISTILL_DIR / note_id


def _safe_log_id(log_id: str) -> str:
    clean = str(log_id or "").strip()
    if not clean or not re.fullmatch(r"[A-Za-z0-9._-]+", clean):
        raise ValueError("invalid distill log id")
    return clean


def _distill_log_path(root: Path, config: dict | None, note_id: str, log_id: str) -> Path:
    return _distill_log_dir(root, config, note_id) / f"{_safe_log_id(log_id)}.json"


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
        "title": (title or "").strip(),
        "text": text or "",
        "scope_hint": scope_hint,
        "images": images or [],
        "status": "raw",
        "candidate_ids": [],
        "created_at": now,
        "updated_at": now,
    }
    write_json(_note_path(root, config, note_id), record)
    return record


def list_notes(root: Path, config: dict | None = None) -> list[dict]:
    target = capture_dir(root, config)
    if not target.is_dir():
        return []
    notes: list[dict] = []
    for path in target.glob("*.json"):
        try:
            record = read_json(path)
            record["_path"] = str(path)
            notes.append(record)
        except (OSError, ValueError):
            continue
    notes.sort(key=lambda r: str(r.get("created_at") or ""))
    return notes


def read_note(root: Path, config: dict | None, note_id: str) -> dict | None:
    path = _note_path(root, config, note_id)
    if not path.exists():
        return None
    try:
        record = read_json(path)
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
    record["updated_at"] = utc_now()
    record.pop("_path", None)
    write_json(_note_path(root, config, note_id), record)
    return record


def delete_note(root: Path, config: dict | None, note_id: str) -> bool:
    path = _note_path(root, config, note_id)
    if path.exists():
        path.unlink()
        assets = _note_assets_dir(root, config, note_id)
        if assets.is_dir():
            shutil.rmtree(assets, ignore_errors=True)
        logs = _distill_log_dir(root, config, note_id)
        if logs.is_dir():
            shutil.rmtree(logs, ignore_errors=True)
        return True
    return False


# --------------------------------------------------------------------------- #
# Distill agent logs: debug-only sidecars under .capture/distill/<note-id>/.
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
    path = _distill_log_path(root, config, note_id, log_id)
    if not path.exists():
        raise FileNotFoundError(f"distill log not found: {log_id}")
    record = read_json(path)
    record.update(updates)
    record["updated_at"] = utc_now()
    write_json(path, record)
    return record


def list_distill_logs(root: Path, config: dict | None, note_id: str) -> list[dict]:
    target = _distill_log_dir(root, config, note_id)
    if not target.is_dir():
        return []
    logs: list[dict] = []
    for path in target.glob("*.json"):
        try:
            record = read_json(path)
            record["_path"] = str(path)
            logs.append(record)
        except (OSError, ValueError):
            continue
    logs.sort(key=lambda r: str(r.get("started_at") or r.get("created_at") or ""))
    return logs


def read_distill_log(root: Path, config: dict | None, note_id: str, log_id: str | None = None) -> dict | None:
    if log_id:
        try:
            path = _distill_log_path(root, config, note_id, log_id)
        except ValueError:
            return None
        if not path.exists():
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
# Image assets (Phase 5a): stored as files under .capture/assets/<note-id>/,
# never committed (covered by the .capture/ gitignore). The note keeps only
# lightweight metadata, never base64.
# --------------------------------------------------------------------------- #
def _note_assets_dir(root: Path, config: dict | None, note_id: str) -> Path:
    return capture_dir(root, config) / CAPTURE_ASSETS_DIR / note_id


def add_note_image(
    root: Path,
    config: dict | None,
    note_id: str,
    *,
    data: bytes,
    filename: str,
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
        raise ImageRejected("unsupported image type (allowed: png, jpeg, webp)")
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
    image = {
        "name": name,
        "original": original,
        "mime": mime,
        "size": len(data),
        "path": f"{CAPTURE_DIR}/{CAPTURE_ASSETS_DIR}/{note_id}/{name}",
    }
    record["images"] = images + [image]
    # Level B: embed the image inline in the note body (memo-style), so it
    # renders in place. note.images stays as the asset registry for guardrails
    # and approve-time promotion.
    ref = f"![{original or name}](/api/kb/capture/image?id={note_id}&name={name})"
    record["text"] = (str(record.get("text") or "").rstrip() + f"\n\n{ref}\n").lstrip("\n")
    record.pop("_path", None)
    record["updated_at"] = utc_now()
    write_json(_note_path(root, config, note_id), record)
    return image


def remove_note_image(root: Path, config: dict | None, note_id: str, name: str) -> bool:
    record = read_note(root, config, note_id)
    if record is None:
        return False
    images = list(record.get("images") or [])
    kept = [img for img in images if img.get("name") != name]
    if len(kept) == len(images):
        return False
    asset = _note_assets_dir(root, config, note_id) / name
    if asset.exists():
        asset.unlink()
    record["images"] = kept
    record.pop("_path", None)
    record["updated_at"] = utc_now()
    write_json(_note_path(root, config, note_id), record)
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
    ``.capture/assets`` is left intact. Returns ``{source_note_id, assets,
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
        src = kb_root / str(img.get("path") or "")
        if not name or not src.is_file():
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


def read_note_image(root: Path, config: dict | None, note_id: str, name: str) -> tuple[bytes, str] | None:
    """Return (bytes, mime) for a stored note image, or None if absent."""
    record = read_note(root, config, note_id)
    if record is None:
        return None
    image = next((img for img in (record.get("images") or []) if img.get("name") == name), None)
    if image is None:
        return None
    asset = _note_assets_dir(root, config, note_id) / name
    if not asset.is_file():
        return None
    try:
        return asset.read_bytes(), str(image.get("mime") or "application/octet-stream")
    except OSError:
        return None
