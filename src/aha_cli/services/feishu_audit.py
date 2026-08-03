from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any

from aha_cli.domain.models import utc_now
from aha_cli.locking import exclusive_lock
from aha_cli.store.paths import aha_home_path

MAX_AUDIT_TEXT_CHARS = 500
_AUTH_RE = re.compile(r"(?i)authorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+")
_SECRET_RE = re.compile(
    r"(?i)(app[_-]?secret|access[_-]?token|refresh[_-]?token|cookie|api[_-]?key)\s*[:=]\s*([^\s,;]+)"
)
_FEISHU_AT_RE = re.compile(r"<at\s+user_id=(['\"])[^'\"]+\1\s*></at>", re.IGNORECASE)


def feishu_audit_dir(root: Path) -> Path:
    return aha_home_path(root) / "logs" / "feishu"


def feishu_audit_path(root: Path, day: str | None = None) -> Path:
    stamp = str(day or utc_now()[:10]).strip()
    return feishu_audit_dir(root) / f"{stamp}.jsonl"


def _digest(value: object) -> str:
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""


def _safe_text(value: object, limit: int = MAX_AUDIT_TEXT_CHARS) -> str:
    text = " ".join(str(value or "").split())
    text = _AUTH_RE.sub("Authorization=[REDACTED]", text)
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _FEISHU_AT_RE.sub("<at user_id=[REDACTED]></at>", text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def feishu_content_summary(message: object) -> str:
    if isinstance(message, str):
        return _safe_text(message)
    if not isinstance(message, dict):
        return _safe_text(type(message).__name__ if message is not None else "")
    if "text" in message:
        return _safe_text(message.get("text"))
    card = message.get("card") if isinstance(message.get("card"), dict) else message
    header = card.get("header") if isinstance(card.get("header"), dict) else {}
    title = header.get("title") if isinstance(header.get("title"), dict) else {}
    title_text = _safe_text(title.get("content"))
    if title_text:
        return f"card: {title_text}"
    return "card" if card.get("schema") or isinstance(message.get("card"), dict) else "structured message"


def _append_private_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "ab", closefd=False) as output, exclusive_lock(output):
            raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            output.write(raw)
            output.flush()
    finally:
        os.close(descriptor)


def audit_feishu_channel(
    root: Path,
    *,
    direction: str,
    kind: str,
    status: str,
    transport: str,
    message_id: str = "",
    chat_id: str = "",
    open_id: str = "",
    session_key: str = "",
    run_id: str = "",
    task_id: str = "",
    content: object = None,
    error: object = None,
    reason: str = "",
    decision: str = "",
) -> bool:
    """Append a sanitized Channel boundary record without exposing raw payloads or credentials."""
    payload = {
        "version": 1,
        "ts": utc_now(),
        "direction": _safe_text(direction, 32),
        "kind": _safe_text(kind, 48),
        "status": _safe_text(status, 48),
        "transport": _safe_text(transport, 32),
        "pid": os.getpid(),
        "thread_id": threading.get_ident(),
    }
    optional = {
        "message_id": _safe_text(message_id, 160),
        "chat_hash": _digest(chat_id),
        "open_id_hash": _digest(open_id),
        "session_hash": _digest(session_key),
        "run_id": _safe_text(run_id, 160),
        "task_id": _safe_text(task_id, 160),
        "content_summary": feishu_content_summary(content),
        "error": _safe_text(error),
        "reason": _safe_text(reason, 160),
        "decision": _safe_text(decision, 32),
    }
    payload.update({key: value for key, value in optional.items() if value})
    try:
        _append_private_jsonl(feishu_audit_path(root), payload)
    except OSError:
        return False
    return True


__all__ = [
    "audit_feishu_channel",
    "feishu_audit_dir",
    "feishu_audit_path",
    "feishu_content_summary",
]
