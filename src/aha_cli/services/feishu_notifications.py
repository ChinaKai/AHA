from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading

from aha_cli.domain.models import utc_now
from aha_cli.domain.models import is_service_assistant_task
from aha_cli.services.feishu import FeishuError, send_card_message, send_text_message
from aha_cli.services.feishu_runtime import feishu_config, feishu_credentials, send_via_active_channel
from aha_cli.store.io import iter_jsonl_reverse, read_json, write_json
from aha_cli.store.paths import aha_home_path, event_path
from aha_cli.store.snapshots import task_snapshot

DIRECT_REPLY_ROUTES = {("main", "feishu"), ("host", "feishu")}
USER_REPLY_ROUTES = DIRECT_REPLY_ROUTES | {("main", "browser"), ("host", "browser")}
USER_TRIGGER_ROUTES = {
    (sender, target)
    for sender in ("browser", "feishu", "weixin", "user")
    for target in ("main", "host")
}
MAX_NOTIFICATION_CHARS = 1800

_state_lock = threading.RLock()


def subscription_state_path(root: Path) -> Path:
    return aha_home_path(root) / "feishu" / "subscriptions.json"


def load_subscription_state(root: Path) -> dict:
    try:
        state = read_json(subscription_state_path(root))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = {}
    subscriptions = state.get("subscriptions") if isinstance(state.get("subscriptions"), dict) else {}
    sent = state.get("sent") if isinstance(state.get("sent"), dict) else {}
    return {"subscriptions": subscriptions, "sent": sent, "updated_at": str(state.get("updated_at") or "")}


def set_subscription(
    root: Path,
    session_key: str,
    *,
    chat_id: str,
    open_id: str,
    run_id: str,
    task_id: str | None,
    enabled: bool = True,
) -> dict:
    key = str(session_key or "").strip()
    if not key or not chat_id or not run_id:
        raise FeishuError("飞书订阅需要 session_key、chat_id 和 run_id")
    with _state_lock:
        state = load_subscription_state(root)
        if enabled:
            state["subscriptions"][key] = {
                "session_key": key,
                "chat_id": str(chat_id),
                "open_id": str(open_id or ""),
                "run_id": str(run_id),
                "task_id": str(task_id or "") or None,
                "enabled": True,
                "updated_at": utc_now(),
            }
        else:
            state["subscriptions"].pop(key, None)
        state["updated_at"] = utc_now()
        write_json(subscription_state_path(root), state)
    return dict(state["subscriptions"].get(key) or {"session_key": key, "enabled": False})


def _message_route(data: dict) -> tuple[str, str]:
    sender = str(data.get("display_sender") or data.get("sender") or data.get("from_agent") or "").lower()
    target = str(data.get("display_target") or data.get("to_agent") or data.get("target") or "").lower()
    return sender, target


def _message_text(data: dict) -> str:
    return str(data.get("message") or data.get("text") or "").strip()


def _display_status(value: object) -> str:
    status = str(value or "").strip().lower()
    return {"running": "busy", "awaiting_user": "awaiting"}.get(status, status or "-")


def _last_task_message(
    root: Path,
    run_id: str,
    task_id: str,
    event: dict,
    routes: set[tuple[str, str]],
) -> str:
    current_event_id = event.get("event_id")
    event_data = event.get("data") if isinstance(event.get("data"), dict) else {}
    found_current = False
    try:
        before = int(current_event_id) if current_event_id not in {None, ""} else None
    except (TypeError, ValueError):
        before = None
    for _offset, candidate in iter_jsonl_reverse(event_path(root, run_id), before=before) or ():
        if not found_current:
            candidate_data = candidate.get("data") if isinstance(candidate.get("data"), dict) else {}
            same_id = current_event_id not in {None, ""} and candidate.get("event_id") == current_event_id
            same_payload = (
                candidate.get("ts") == event.get("ts")
                and candidate.get("type") == event.get("type")
                and str(candidate_data.get("task_id") or "") == str(event_data.get("task_id") or "")
                and str(candidate_data.get("previous_status") or "") == str(event_data.get("previous_status") or "")
                and str(candidate_data.get("status") or "") == str(event_data.get("status") or "")
            )
            if same_id or same_payload:
                found_current = True
            continue
        candidate_data = candidate.get("data") if isinstance(candidate.get("data"), dict) else {}
        if str(candidate_data.get("task_id") or "") != task_id:
            continue
        if str(candidate.get("type") or "") == "task_status_changed":
            break
        if str(candidate.get("type") or "") != "message":
            continue
        if _message_route(candidate_data) not in routes:
            continue
        return " ".join(_message_text(candidate_data).split())
    return ""


def _status_event_reason(data: dict) -> str:
    for key in ("reason", "error", "waiting_reason", "message"):
        value = " ".join(str(data.get(key) or "").split())
        if value:
            return value
    return ""


def _trim_notification(message: str) -> str:
    return message if len(message) <= MAX_NOTIFICATION_CHARS else message[: MAX_NOTIFICATION_CHARS - 1].rstrip() + "…"


def _event_key(run_id: str, event: dict) -> str:
    event_id = event.get("event_id")
    if event_id not in {None, ""}:
        return f"{run_id}:{event_id}"
    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"{run_id}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _status_recipient_key(chat_id: str) -> str:
    return "chat:" + hashlib.sha256(chat_id.encode("utf-8")).hexdigest()


def notification_message_for_event(root: Path, run_id: str, event: dict) -> str:
    event_type = str(event.get("type") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    config = feishu_config(root)
    task_id = str(data.get("task_id") or "")
    system_assistant = False
    if task_id:
        try:
            system_assistant = is_service_assistant_task(task_snapshot(root, run_id, task_id)["task"])
        except (KeyError, SystemExit):
            system_assistant = False
    if event_type == "message":
        if _message_route(data) not in DIRECT_REPLY_ROUTES:
            return ""
        # When status push is enabled, the following task status event carries
        # this reply together with the transition to avoid two Feishu messages.
        if config.get("notifications_enabled") and not system_assistant:
            return ""
        return _trim_notification(_message_text(data))
    if event_type != "task_status_changed" or not config.get("notifications_enabled"):
        return ""
    if system_assistant:
        return ""
    previous = _display_status(data.get("previous_status"))
    current = _display_status(data.get("status"))
    if previous == current:
        return ""
    if not task_id:
        return ""
    reason = _status_event_reason(data)
    if current == "busy":
        source_message = _last_task_message(root, run_id, task_id, event, USER_TRIGGER_ROUTES) or reason
    elif previous == "busy":
        source_message = _last_task_message(root, run_id, task_id, event, USER_REPLY_ROUTES) or reason
    else:
        source_message = reason or _last_task_message(root, run_id, task_id, event, USER_REPLY_ROUTES)
    message = "\n".join(
        [
            f"{run_id} {task_id}:",
            f"status: {previous}->{current}",
            f"message: {source_message or '-'}",
        ]
    )
    return _trim_notification(message)


def _send(root: Path, chat_id: str, text: str, *, card: dict | None = None) -> dict:
    try:
        message = {"card": card} if isinstance(card, dict) and card else {"text": text}
        return send_via_active_channel(root, chat_id, message)
    except (RuntimeError, TimeoutError):
        config = feishu_config(root)
        app_id, app_secret = feishu_credentials(config)
        if not app_id or not app_secret:
            raise FeishuError("飞书 App ID 或 App Secret 未配置")
        if isinstance(card, dict) and card:
            payload = send_card_message(root, app_id, app_secret, chat_id, card, receive_id_type="chat_id")
        else:
            payload = send_text_message(root, app_id, app_secret, chat_id, text, receive_id_type="chat_id")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {"ok": True, "sent": True, "message_id": data.get("message_id"), "target": chat_id}


def notify_event(root: Path, run_id: str, event: dict) -> dict:
    message = notification_message_for_event(root, run_id, event)
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    card = data.get("feishu_card") if isinstance(data.get("feishu_card"), dict) else None
    if not message and not card:
        return {"ok": True, "sent": False, "reason": "ignored_event"}
    task_id = str(data.get("task_id") or "")
    event_key = _event_key(run_id, event)
    is_status_event = str(event.get("type") or "") == "task_status_changed"
    sent_count = 0
    visited_recipients: set[str] = set()
    with _state_lock:
        state = load_subscription_state(root)
        for session_key, subscription in list(state["subscriptions"].items()):
            if not isinstance(subscription, dict) or not subscription.get("enabled"):
                continue
            # Status notifications are integration-wide: a Feishu subscriber
            # receives task transitions from every run. Direct assistant replies
            # remain scoped to the originating run/task conversation.
            if not is_status_event and str(subscription.get("run_id") or "") != run_id:
                continue
            subscribed_task = str(subscription.get("task_id") or "")
            if not is_status_event and subscribed_task and subscribed_task != task_id:
                continue
            chat_id = str(subscription.get("chat_id") or "")
            if not chat_id:
                continue
            recipient_key = _status_recipient_key(chat_id) if is_status_event else session_key
            if recipient_key in visited_recipients:
                continue
            visited_recipients.add(recipient_key)
            sent_key = f"{recipient_key}:{event_key}"
            if sent_key in state["sent"]:
                continue
            result = _send(root, chat_id, message, card=card)
            state["sent"][sent_key] = {
                "sent_at": utc_now(),
                "message_id": result.get("message_id"),
            }
            sent_count += 1
        if len(state["sent"]) > 4096:
            state["sent"] = dict(list(state["sent"].items())[-4096:])
        state["updated_at"] = utc_now()
        write_json(subscription_state_path(root), state)
    return {"ok": True, "sent": sent_count > 0, "sent_count": sent_count, "reason": "sent" if sent_count else "no_subscription"}


__all__ = [
    "load_subscription_state",
    "notification_message_for_event",
    "notify_event",
    "set_subscription",
    "subscription_state_path",
]
