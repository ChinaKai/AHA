from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import re
from pathlib import Path

from aha_cli.store.paths import run_dir
from aha_cli.store.runs import require_plan

TASK_MEMO_ASSET_DIR = "task_memo_assets"
MAX_TASK_MEMO_ASSET_BYTES = 64 * 1024 * 1024
IMAGE_TYPES = {
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heic-sequence": ".heic",
    "image/heif": ".heif",
    "image/heif-sequence": ".heif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}
DATA_URL_RE = re.compile(r"^data:([^;,]*);base64,(.*)$", re.DOTALL)
SAFE_ASSET_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_SUFFIX_RE = re.compile(r"^[A-Za-z0-9]+$")


def task_memo_assets_dir(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / TASK_MEMO_ASSET_DIR


def normalize_image_type(value: object) -> str:
    content_type = str(value or "").strip().lower()
    if content_type in {"image/jpg", "image/pjpeg"}:
        content_type = "image/jpeg"
    if content_type == "image/x-png":
        content_type = "image/png"
    if content_type == "image/x-heic":
        content_type = "image/heic"
    if content_type == "image/x-heif":
        content_type = "image/heif"
    if content_type in {"image/svg", "image/x-svg+xml"}:
        content_type = "image/svg+xml"
    if content_type not in IMAGE_TYPES:
        raise ValueError(f"unsupported memo image type: {content_type or 'unknown'}")
    return content_type


def image_type_from_filename(value: object) -> str:
    suffix = Path(str(value or "")).suffix.strip().lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".svg":
        return "image/svg+xml"
    if suffix == ".gif":
        return "image/gif"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".avif":
        return "image/avif"
    if suffix == ".bmp":
        return "image/bmp"
    if suffix == ".heic":
        return "image/heic"
    if suffix == ".heif":
        return "image/heif"
    return ""


def is_image_content_type(content_type: object) -> bool:
    try:
        normalize_image_type(content_type)
    except ValueError:
        return False
    return True


def normalize_asset_type(content_type: object, filename: object = None) -> str:
    try:
        return normalize_image_type(content_type)
    except ValueError:
        filename_type = image_type_from_filename(filename)
        if filename_type:
            return normalize_image_type(filename_type)
        guessed_type, _ = mimetypes.guess_type(str(filename or ""))
        clean_type = str(content_type or guessed_type or "application/octet-stream").split(";", 1)[0].strip().lower()
        if not clean_type or "/" not in clean_type:
            return "application/octet-stream"
        return clean_type


def parse_asset_data_url(value: object, fallback_content_type: object = None, filename: object = None) -> tuple[str, bytes]:
    text = str(value or "").strip()
    match = DATA_URL_RE.match(text)
    if not match:
        raise ValueError("memo asset data_url must be a base64 data URL")
    raw_content_type = match.group(1)
    content_type = normalize_asset_type(raw_content_type or fallback_content_type, filename)
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("memo asset data_url is invalid base64") from exc
    if not data:
        raise ValueError("memo attachment cannot be empty")
    if len(data) > MAX_TASK_MEMO_ASSET_BYTES:
        raise ValueError("memo attachment is too large")
    return content_type, data


def parse_asset_upload(content_type: object, filename: object, data: object) -> tuple[str, bytes]:
    if not isinstance(data, bytes):
        raise ValueError("memo attachment file is required")
    if not data:
        raise ValueError("memo attachment cannot be empty")
    if len(data) > MAX_TASK_MEMO_ASSET_BYTES:
        raise ValueError("memo attachment is too large")
    return normalize_asset_type(content_type, filename), data


def safe_original_stem(filename: object) -> str:
    stem = Path(str(filename or "")).stem.strip().lower()
    stem = re.sub(r"[^a-z0-9._-]+", "-", stem).strip(".-_")
    return stem[:36] or "image"


def markdown_alt_text(value: object) -> str:
    alt = Path(str(value or "")).stem.replace("[", "").replace("]", "").strip() or "memo image"
    return alt


def markdown_attachment_text(value: object) -> str:
    label = Path(str(value or "")).name.replace("[", "").replace("]", "").replace("\r", " ").replace("\n", " ").strip()
    return label or "attachment"


def asset_suffix(filename: object, content_type: str) -> str:
    if is_image_content_type(content_type):
        return IMAGE_TYPES[normalize_image_type(content_type)]
    suffix = Path(str(filename or "")).suffix.strip().lower()
    if suffix and 2 <= len(suffix) <= 16 and SAFE_SUFFIX_RE.match(suffix[1:]):
        return suffix
    guessed_suffix = mimetypes.guess_extension(content_type) or ".bin"
    guessed_suffix = guessed_suffix.split("/", 1)[0].strip().lower()
    if not guessed_suffix.startswith(".") or not SAFE_SUFFIX_RE.match(guessed_suffix[1:]):
        return ".bin"
    return guessed_suffix[:16]


def asset_content_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    for content_type, image_suffix in IMAGE_TYPES.items():
        if suffix == image_suffix:
            return content_type
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def validate_asset_filename(filename: object) -> str:
    name = str(filename or "").strip()
    if not name or "\\" in name:
        raise ValueError("invalid memo image asset filename")
    parts = name.split("/")
    if len(parts) > 8:
        raise ValueError("invalid memo image asset filename")
    safe_parts = []
    for part in parts:
        if not part or part in {".", ".."} or not SAFE_ASSET_RE.match(part):
            raise ValueError("invalid memo image asset filename")
        safe_parts.append(part)
    return "/".join(safe_parts)


def create_task_memo_asset_from_bytes(root: Path, run_id: str, *, filename: object, content_type: object, data: bytes) -> dict:
    require_plan(root, run_id)
    original_filename = filename
    content_type, data = parse_asset_upload(content_type, filename, data)
    kind = "image" if is_image_content_type(content_type) else "attachment"
    digest = hashlib.sha256(data).hexdigest()
    asset_prefix = digest[:2]
    suffix = asset_suffix(filename, content_type)
    asset_name = f"memo-{digest[:16]}{suffix}"
    filename = f"{asset_prefix}/{asset_name}"
    alt_text = markdown_alt_text(safe_original_stem(original_filename))
    attachment_text = markdown_attachment_text(original_filename)
    directory = task_memo_assets_dir(root, run_id) / asset_prefix
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / asset_name
    if not path.exists():
        path.write_bytes(data)
    relative_path = f"{TASK_MEMO_ASSET_DIR}/{filename}"
    markdown = f"![{alt_text}]({relative_path})" if kind == "image" else f"[Attachment: {attachment_text}]({relative_path})"
    return {
        "filename": filename,
        "path": relative_path,
        "url": f"/api/task-memo-assets/{filename}",
        "content_type": content_type,
        "kind": kind,
        "original_filename": markdown_attachment_text(original_filename),
        "bytes": len(data),
        "sha256": digest,
        "markdown": markdown,
    }


def create_task_memo_asset(root: Path, run_id: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("memo attachment payload must be an object")
    fallback_content_type = payload.get("content_type") or image_type_from_filename(payload.get("filename"))
    content_type, data = parse_asset_data_url(payload.get("data_url"), fallback_content_type, payload.get("filename"))
    return create_task_memo_asset_from_bytes(root, run_id, filename=payload.get("filename"), content_type=content_type, data=data)


def read_task_memo_asset(root: Path, run_id: str, filename: str) -> tuple[bytes, str, str]:
    require_plan(root, run_id)
    safe_name = validate_asset_filename(filename)
    path = task_memo_assets_dir(root, run_id) / safe_name
    if not path.is_file():
        raise FileNotFoundError(safe_name)
    return path.read_bytes(), asset_content_type(safe_name), safe_name
