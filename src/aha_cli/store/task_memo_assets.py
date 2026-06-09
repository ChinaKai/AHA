from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import Path

from aha_cli.store.paths import run_dir
from aha_cli.store.runs import require_plan

TASK_MEMO_ASSET_DIR = "task_memo_assets"
MAX_TASK_MEMO_ASSET_BYTES = 25 * 1024 * 1024
IMAGE_TYPES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,(.*)$", re.DOTALL)
SAFE_ASSET_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def task_memo_assets_dir(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / TASK_MEMO_ASSET_DIR


def normalize_image_type(value: object) -> str:
    content_type = str(value or "").strip().lower()
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    if content_type not in IMAGE_TYPES:
        raise ValueError(f"unsupported memo image type: {content_type or 'unknown'}")
    return content_type


def parse_image_data_url(value: object) -> tuple[str, bytes]:
    text = str(value or "").strip()
    match = DATA_URL_RE.match(text)
    if not match:
        raise ValueError("image data_url must be a base64 data URL")
    content_type = normalize_image_type(match.group(1))
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image data_url is invalid base64") from exc
    if not data:
        raise ValueError("memo image cannot be empty")
    if len(data) > MAX_TASK_MEMO_ASSET_BYTES:
        raise ValueError("memo image is too large")
    return content_type, data


def safe_original_stem(filename: object) -> str:
    stem = Path(str(filename or "")).stem.strip().lower()
    stem = re.sub(r"[^a-z0-9._-]+", "-", stem).strip(".-_")
    return stem[:36] or "image"


def markdown_alt_text(value: object) -> str:
    alt = Path(str(value or "")).stem.replace("[", "").replace("]", "").strip() or "memo image"
    return alt


def asset_content_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    for content_type, image_suffix in IMAGE_TYPES.items():
        if suffix == image_suffix:
            return content_type
    raise ValueError(f"unsupported memo image asset: {filename}")


def validate_asset_filename(filename: object) -> str:
    name = str(filename or "").strip()
    if not name or "/" in name or "\\" in name or name in {".", ".."} or not SAFE_ASSET_RE.match(name):
        raise ValueError("invalid memo image asset filename")
    asset_content_type(name)
    return name


def create_task_memo_asset(root: Path, run_id: str, payload: dict) -> dict:
    require_plan(root, run_id)
    if not isinstance(payload, dict):
        raise ValueError("memo image payload must be an object")
    content_type, data = parse_image_data_url(payload.get("data_url"))
    digest = hashlib.sha256(data).hexdigest()
    suffix = IMAGE_TYPES[content_type]
    filename = f"memo-{digest[:16]}{suffix}"
    alt_text = markdown_alt_text(safe_original_stem(payload.get("filename")))
    directory = task_memo_assets_dir(root, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    if not path.exists():
        path.write_bytes(data)
    relative_path = f"{TASK_MEMO_ASSET_DIR}/{filename}"
    return {
        "filename": filename,
        "path": relative_path,
        "url": f"/api/task-memo-assets/{filename}",
        "content_type": content_type,
        "bytes": len(data),
        "sha256": digest,
        "markdown": f"![{alt_text}]({relative_path})",
    }


def read_task_memo_asset(root: Path, run_id: str, filename: str) -> tuple[bytes, str, str]:
    require_plan(root, run_id)
    safe_name = validate_asset_filename(filename)
    path = task_memo_assets_dir(root, run_id) / safe_name
    if not path.is_file():
        raise FileNotFoundError(safe_name)
    return path.read_bytes(), asset_content_type(safe_name), safe_name
