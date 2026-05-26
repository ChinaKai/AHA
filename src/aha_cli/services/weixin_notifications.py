from __future__ import annotations

import json
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.weixin import load_account, send_test_notification
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import plan_path, run_dir

NOTIFICATION_EVENT_TYPES = {"message"}
ALLOWED_MESSAGE_ROUTES = {
    ("browser", "main"),
    ("main", "browser"),
    ("main", "host"),
    ("host", "main"),
    ("host", "browser"),
}
MAX_MESSAGE_CHARS = 1800
MESSAGE_PREVIEW_CHARS = 1200


def notification_state_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "weixin_notifications.json"


def load_notification_state(root: Path, run_id: str) -> dict:
    try:
        state = read_json(notification_state_path(root, run_id))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = {}
    sent = state.get("sent")
    if not isinstance(sent, dict):
        sent = {}
    return {
        "enabled": bool(state.get("enabled")),
        "sent": sent,
        "updated_at": str(state.get("updated_at") or ""),
        "last_sent_at": str(state.get("last_sent_at") or ""),
    }


def notification_status(root: Path, run_id: str) -> dict:
    state = load_notification_state(root, run_id)
    account = load_account(root)
    return {
        "enabled": state["enabled"],
        "ready": bool(account.get("token") and account.get("user_id")),
        "sent_count": len(state["sent"]),
        "updated_at": state["updated_at"],
        "last_sent_at": state["last_sent_at"],
    }


def set_notifications_enabled(root: Path, run_id: str, enabled: bool) -> dict:
    state = load_notification_state(root, run_id)
    state["enabled"] = bool(enabled)
    state["updated_at"] = utc_now()
    write_json(notification_state_path(root, run_id), state)
    return notification_status(root, run_id)


def _task_by_id(root: Path, run_id: str, task_id: str) -> dict:
    try:
        plan = read_json(plan_path(root, run_id))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        plan = {}
    for task in plan.get("tasks", []):
        if str(task.get("id") or "") == task_id:
            return task
    return {"id": task_id, "title": task_id}


def _run_label(root: Path, run_id: str) -> str:
    try:
        plan = read_json(plan_path(root, run_id))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        plan = {}
    goal = str(plan.get("goal") or "").strip()
    return f"{goal} ({run_id})" if goal else run_id


def _event_key(root: Path, run_id: str, event: dict) -> str:
    event_type = str(event.get("type") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    task_id = str(data.get("task_id") or "")
    if event_type == "message":
        event_id = event.get("event_id")
        if event_id not in (None, ""):
            return f"message_event:{event_id}"
        sender, target = _message_route(data)
        return ":".join(["message", task_id, sender, target, _compact_text(_message_text(data), 80)])
    return f"{event_type}:{event.get('event_id') or ''}"


def _truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _compact_text(text: object, limit: int = MESSAGE_PREVIEW_CHARS) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + "..."


def _message_endpoint(data: dict, *keys: str) -> str:
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def _message_route(data: dict) -> tuple[str, str]:
    sender = _message_endpoint(data, "display_sender", "sender", "from_agent").lower()
    target = _message_endpoint(data, "display_target", "to_agent", "target").lower()
    return sender, target


def _message_text(data: dict) -> str:
    return str(data.get("message") or data.get("text") or "").strip()


def _message_notification(root: Path, run_id: str, data: dict) -> str:
    sender, target = _message_route(data)
    if (sender, target) not in ALLOWED_MESSAGE_ROUTES:
        return ""
    task_id = str(data.get("task_id") or "")
    task = _task_by_id(root, run_id, task_id)
    title = str(task.get("title") or task_id)
    message = _compact_text(_message_text(data)) or "-"
    lines = [
        "AHA 消息通知",
        f"Run: {_run_label(root, run_id)}",
        f"Task: {title} ({task_id})",
        f"Route: {sender} -> {target}",
        f"内容: {message}",
    ]
    return "\n".join(lines)


def notification_message_for_event(root: Path, run_id: str, event: dict) -> str:
    event_type = str(event.get("type") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if event_type == "message":
        return _message_notification(root, run_id, data)
    return ""


def notify_event(root: Path, run_id: str, event: dict) -> dict:
    event_type = str(event.get("type") or "")
    if event_type not in NOTIFICATION_EVENT_TYPES:
        return {"ok": True, "sent": False, "reason": "ignored_event"}
    state = load_notification_state(root, run_id)
    if not state["enabled"]:
        return {"ok": True, "sent": False, "reason": "disabled"}
    account = load_account(root)
    if not account.get("token") or not account.get("user_id"):
        return {"ok": True, "sent": False, "reason": "not_paired"}
    key = _event_key(root, run_id, event)
    if key in state["sent"]:
        return {"ok": True, "sent": False, "reason": "duplicate", "key": key}
    message = _truncate(notification_message_for_event(root, run_id, event))
    if not message:
        return {"ok": True, "sent": False, "reason": "empty_message", "key": key}
    sent = send_test_notification(root, run_id, message)
    now = utc_now()
    state["sent"][key] = {
        "event_type": event_type,
        "event_id": event.get("event_id"),
        "sent_at": now,
        "message_id": sent.get("message_id"),
    }
    state["last_sent_at"] = now
    state["updated_at"] = now
    write_json(notification_state_path(root, run_id), state)
    return {"ok": True, "sent": True, "key": key, "message_id": sent.get("message_id")}
