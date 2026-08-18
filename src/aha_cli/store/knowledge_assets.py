"""Image and general attachment storage for approved knowledge entries.

Small text/pdf/docx images live inside the KB repo's ``assets/<slug>/`` dir and
sync with git. Large media (video/audio) and oversized files are stored in a
local-only store outside the KB repo — the KB keeps only the reference and a
sha256 checksum so the git repo never bloats.
"""

from __future__ import annotations

import hashlib
import io
import re
import uuid
import zipfile
from pathlib import Path

from aha_cli.store.knowledge import (
    find_entry,
    kind_for_type,
    normalize_entry_slug,
    read_entry,
    write_entry,
)
from aha_cli.store.paths import aha_home_path

ENTRY_ASSETS_DIR = "assets"
LOCAL_ASSETS_DIR = "knowledge_local_assets"
ALLOWED_ENTRY_IMAGE_MIME = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}
MAX_ENTRY_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ENTRY_IMAGE_TOTAL_BYTES = 20 * 1024 * 1024

# General attachment whitelist.
ALLOWED_ENTRY_ATTACHMENT_MIME = {
    *ALLOWED_ENTRY_IMAGE_MIME,
    "application/pdf",
    "text/plain",
    "text/markdown",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "video/mp4",
    "video/webm",
    "audio/mpeg",
    "audio/ogg",
    "application/zip",
}
# Attachments at or under this size sync to the KB git repo.
MAX_SYNCED_ATTACHMENT_BYTES = 5 * 1024 * 1024
# Hard cap for local-only media/large attachments.
MAX_LOCAL_ATTACHMENT_BYTES = 200 * 1024 * 1024
# Media types are always local-only (never pushed into the KB git repo).
LOCAL_ONLY_MIME_PREFIXES = ("video/", "audio/")
_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".zip": "application/zip",
}
_KIND_BY_MIME = {
    "image/png": "image",
    "image/jpeg": "image",
    "image/svg+xml": "image",
    "image/webp": "image",
    "application/pdf": "pdf",
    "text/plain": "text",
    "text/markdown": "text",
    "application/msword": "office",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "office",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "office",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "office",
    "video/mp4": "video",
    "video/webm": "video",
    "audio/mpeg": "audio",
    "audio/ogg": "audio",
    "application/zip": "archive",
    "application/octet-stream": "other",
}


def attachment_mime_for(filename: str) -> str:
    """Best-effort mime from a filename extension (fallback octet-stream)."""
    ext = Path(str(filename or "")).suffix.lower()
    return _EXT_MIME.get(ext, "application/octet-stream")


def attachment_kind(mime: str) -> str:
    return _KIND_BY_MIME.get(str(mime or ""), "other")


def is_local_only_attachment(mime: str, size: int) -> bool:
    """Media types and oversized files stay out of the KB git repo."""
    if str(mime or "").startswith(LOCAL_ONLY_MIME_PREFIXES):
        return True
    return int(size or 0) > MAX_SYNCED_ATTACHMENT_BYTES


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


# --------------------------------------------------------------------------- #
# General attachments (pdf / office / text / video / audio / archives)
# --------------------------------------------------------------------------- #
def local_assets_root(root: Path) -> Path:
    """Directory outside the KB git repo where local-only attachments live."""
    return aha_home_path(root) / LOCAL_ASSETS_DIR


def _local_attachment_path(root: Path, sha256: str, name: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "file")).strip("._") or "file"
    return local_assets_root(root) / sha256 / safe_name


def _safe_attachment_name(filename: str, mime: str) -> str:
    ext = {v: k for k, v in _EXT_MIME.items()}.get(mime, "")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (filename or "").rsplit("/", 1)[-1]).strip("._") or "attachment"
    stem = stem.rsplit(".", 1)[0][:60] or "attachment"
    return f"{stem}{ext}"


def _sniff_attachment_mime(data: bytes, filename: str) -> str:
    from_magic = _sniff_entry_image_mime(data)
    if from_magic:
        return from_magic
    if data[:4] == b"%PDF":
        return "application/pdf"
    # docx/pptx/xlsx are zip containers.
    if data[:4] == b"PK\x03\x04":
        if len(data) > 32:
            lowered = data[:64].lower()
            if b"[content_types].xml" in lowered or b"word/" in lowered:
                return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if b"ppt/" in lowered:
                return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            if b"xl/" in lowered:
                return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return "application/zip"
    return attachment_mime_for(filename)


def add_entry_attachment(
    root: Path,
    config: dict | None,
    identifier: str,
    *,
    data: bytes,
    filename: str,
) -> tuple[dict, dict]:
    """Validate and persist a general attachment for a tracked entry.

    Small text/pdf/office/images sync into the KB repo; media and oversized
    files go to the local-only store (KB keeps only the reference + sha256).
    """
    entry = find_entry(root, config, identifier)
    if entry is None:
        raise FileNotFoundError(f"entry not found: {identifier}")
    data = data or b""
    mime = _sniff_attachment_mime(data, filename)
    if mime not in ALLOWED_ENTRY_ATTACHMENT_MIME:
        raise EntryImageRejected(
            f"unsupported attachment type: {mime or 'unknown'} (allowed: "
            + ", ".join(sorted(ALLOWED_ENTRY_ATTACHMENT_MIME))
            + ")"
        )
    if len(data) > MAX_LOCAL_ATTACHMENT_BYTES:
        raise EntryImageRejected(f"attachment exceeds {MAX_LOCAL_ATTACHMENT_BYTES // (1024 * 1024)}MB limit")
    sha256 = hashlib.sha256(data).hexdigest()
    meta = dict(entry.get("meta") or {})
    assets = [dict(item) for item in (meta.get("assets") or []) if isinstance(item, dict)]
    # Dedupe exact bytes already stored for this entry.
    if any(str(img.get("sha256") or "") == sha256 for img in assets):
        existing = next(img for img in assets if str(img.get("sha256") or "") == sha256)
        response = dict(existing)
        return entry, response

    local_only = is_local_only_attachment(mime, len(data))
    name = _safe_attachment_name(filename, mime)
    if local_only:
        target = _local_attachment_path(root, sha256, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        rel = f"{ENTRY_ASSETS_DIR}/{_entry_asset_slug(entry)}/{name}"
    else:
        assets_dir = _entry_asset_dir(entry)
        assets_dir.mkdir(parents=True, exist_ok=True)
        if (assets_dir / name).exists():
            name = f"{uuid.uuid4().hex[:8]}-{name}"
        (assets_dir / name).write_bytes(data)
        rel = f"{ENTRY_ASSETS_DIR}/{_entry_asset_slug(entry)}/{name}"
    original = (filename or "").rsplit("/", 1)[-1]
    attachment: dict = {
        "name": name,
        "original": original,
        "mime": mime,
        "kind": attachment_kind(mime),
        "size": len(data),
        "sha256": sha256,
        "path": rel,
        "local_only": local_only,
    }
    if local_only:
        attachment["local_path"] = str(target)
    meta["assets"] = assets + [attachment]
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
    response_attachment = dict(attachment)
    if attachment_kind(mime) == "image":
        response_attachment["markdown"] = f"![{original or name}]({rel})"
    return read_entry(path), response_attachment


def read_entry_attachment(
    root: Path,
    config: dict | None,
    identifier: str,
    path_or_name: str,
) -> tuple[bytes, dict] | None:
    """Return (bytes, record) for a stored entry attachment, or None.

    ``record`` includes ``mime`` and ``disposition`` (inline vs attachment).
    Path traversal is blocked: the resolved path must stay inside the entry
    asset dir (synced) or the local-only store (media).
    """
    entry = find_entry(root, config, identifier)
    if entry is None:
        return None
    record = _entry_asset_record(entry, path_or_name)
    if record is None:
        return None
    mime = str(record.get("mime") or "application/octet-stream")
    candidate: Path | None = None
    if record.get("local_only"):
        local = str(record.get("local_path") or "")
        if local:
            candidate = Path(local)
    else:
        candidate = _entry_asset_path(entry, str(record.get("path") or ""))
    if candidate is None or not candidate.is_file():
        return None
    try:
        candidate = candidate.resolve(strict=False)
    except OSError:
        return None
    if record.get("local_only"):
        root_dir = local_assets_root(root).resolve()
        try:
            candidate.relative_to(root_dir)
        except ValueError:
            return None
    try:
        data = candidate.read_bytes()
    except OSError:
        return None
    kind = attachment_kind(mime)
    disposition = "attachment" if kind in {"office", "archive", "other"} else "inline"
    return data, {
        "mime": mime,
        "kind": kind,
        "name": str(record.get("original") or record.get("name") or "attachment"),
        "size": len(data),
        "sha256": str(record.get("sha256") or ""),
        "disposition": disposition,
        "path": str(record.get("path") or ""),
    }


def extract_docx_media(data: bytes) -> list[dict]:
    """Extract embedded images from a docx/pptx zip container (stdlib only).

    Returns a list of ``{"name", "data", "mime"}`` for media entries. Graceful:
    a non-zip or missing-media document yields ``[]``.
    """
    if not data or data[:2] != b"PK":
        return []
    media_prefixes = ("word/media/", "ppt/media/")
    results: list[dict] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                name = info.filename or ""
                lowered = name.lower()
                if not any(lowered.startswith(prefix) for prefix in media_prefixes):
                    continue
                if info.is_dir():
                    continue
                try:
                    payload = zf.read(info)
                except (KeyError, zipfile.BadZipFile):
                    continue
                mime = attachment_mime_for(name)
                if mime not in ALLOWED_ENTRY_IMAGE_MIME:
                    continue
                results.append({"name": name.rsplit("/", 1)[-1], "data": payload, "mime": mime})
    except (zipfile.BadZipFile, OSError, ValueError):
        return []
    return results


def add_docx_media_as_images(
    root: Path,
    config: dict | None,
    identifier: str,
    *,
    data: bytes,
) -> dict:
    """Extract docx/pptx embedded images and attach them to the entry.

    Returns ``{"added": N, "images": [...]}``. Missing media or a non-zip
    document yields ``{"added": 0}`` — never raises.
    """
    extracted = extract_docx_media(data)
    added: list[dict] = []
    for media in extracted:
        try:
            _, image = add_entry_attachment(
                root,
                config,
                identifier,
                data=media["data"],
                filename=media["name"],
            )
            added.append(image)
        except (EntryImageRejected, FileNotFoundError, OSError):
            continue
    return {"added": len(added), "images": added}
