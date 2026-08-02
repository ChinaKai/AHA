from __future__ import annotations

import hashlib
from pathlib import Path
import secrets
import threading
import time

from aha_cli.domain.models import utc_now
from aha_cli.locking import exclusive_lock
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import aha_home_path

MAX_HANDOFFS = 1024
_state_lock = threading.RLock()


def service_handoffs_path(root: Path) -> Path:
    return aha_home_path(root) / "feishu" / "service_handoffs.json"


def _load(root: Path) -> dict[str, dict]:
    try:
        payload = read_json(service_handoffs_path(root))
    except (FileNotFoundError, OSError, ValueError):
        payload = {}
    handoffs = payload.get("handoffs") if isinstance(payload.get("handoffs"), dict) else {}
    return {str(key): value for key, value in handoffs.items() if isinstance(value, dict)}


def _save(root: Path, handoffs: dict[str, dict]) -> None:
    path = service_handoffs_path(root)
    write_json(path, {"version": 1, "handoffs": handoffs, "updated_at": utc_now()})
    try:
        path.chmod(0o600)
        path.parent.chmod(0o700)
    except OSError:
        pass


def _with_lock(root: Path):
    path = service_handoffs_path(root).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a+b")


def register_service_handoff(
    root: Path,
    *,
    assistant_run_id: str,
    assistant_task_id: str,
    session_key: str,
    chat_id: str,
    open_id: str,
    target_run_id: str,
    target_task_id: str,
    request_message: str,
) -> dict:
    handoff_id = secrets.token_urlsafe(18)
    record = {
        "id": handoff_id,
        "assistant_run_id": str(assistant_run_id or ""),
        "assistant_task_id": str(assistant_task_id or ""),
        "session_key": str(session_key or ""),
        "chat_id": str(chat_id or ""),
        "open_id": str(open_id or ""),
        "target_run_id": str(target_run_id or ""),
        "target_task_id": str(target_task_id or ""),
        "request_fingerprint": hashlib.sha256(str(request_message or "").encode("utf-8")).hexdigest(),
        "request_preview": " ".join(str(request_message or "").split())[:500],
        "status": "pending",
        "created_at": utc_now(),
        "created_at_epoch": time.time(),
    }
    with _state_lock, _with_lock(root) as handle, exclusive_lock(handle):
        handoffs = _load(root)
        handoffs[handoff_id] = record
        if len(handoffs) > MAX_HANDOFFS:
            handoffs = dict(
                sorted(handoffs.items(), key=lambda item: (float(item[1].get("created_at_epoch") or 0), item[0]))[
                    -MAX_HANDOFFS:
                ]
            )
        _save(root, handoffs)
    return dict(record)


def pending_handoff_for_reply(root: Path, run_id: str, task_id: str) -> dict | None:
    with _state_lock, _with_lock(root) as handle, exclusive_lock(handle):
        matches = [
            record
            for record in _load(root).values()
            if str(record.get("status") or "") == "pending"
            and str(record.get("target_run_id") or "") == str(run_id or "")
            and str(record.get("target_task_id") or "") == str(task_id or "")
        ]
    if not matches:
        return None
    return dict(min(matches, key=lambda item: (float(item.get("created_at_epoch") or 0), str(item.get("id") or ""))))


def mark_service_handoff(root: Path, handoff_id: str, status: str, *, error: str = "") -> dict | None:
    with _state_lock, _with_lock(root) as handle, exclusive_lock(handle):
        handoffs = _load(root)
        record = handoffs.get(str(handoff_id or ""))
        if not isinstance(record, dict):
            return None
        record["status"] = str(status or "")
        record["updated_at"] = utc_now()
        if status == "delivered":
            record["delivered_at"] = record["updated_at"]
            record["suppress_next_status"] = True
        if error:
            record["error"] = str(error)[:1000]
        _save(root, handoffs)
    return dict(record)


def consume_status_suppressions(root: Path, run_id: str, task_id: str) -> set[str]:
    """Consume origin chats that already received a directed handoff result."""
    chats: set[str] = set()
    with _state_lock, _with_lock(root) as handle, exclusive_lock(handle):
        handoffs = _load(root)
        changed = False
        for record in handoffs.values():
            if (
                record.get("suppress_next_status")
                and str(record.get("target_run_id") or "") == str(run_id or "")
                and str(record.get("target_task_id") or "") == str(task_id or "")
            ):
                chat_id = str(record.get("chat_id") or "")
                if chat_id:
                    chats.add(chat_id)
                record["suppress_next_status"] = False
                changed = True
        if changed:
            _save(root, handoffs)
    return chats


__all__ = [
    "consume_status_suppressions",
    "mark_service_handoff",
    "pending_handoff_for_reply",
    "register_service_handoff",
    "service_handoffs_path",
]
