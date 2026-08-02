from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
import threading

from aha_cli.locking import exclusive_lock
from aha_cli.domain.models import utc_now
from aha_cli.domain.models import is_service_assistant_task
from aha_cli.services.feishu import FeishuError, bind_confirmation_card, send_card_message, send_text_message
from aha_cli.services.feishu_audit import audit_feishu_channel
from aha_cli.services.feishu_runtime import feishu_config, feishu_credentials, send_via_active_channel
from aha_cli.services.service_assistant_handoffs import (
    consume_status_suppressions,
    mark_service_handoff,
    pending_handoff_for_reply,
)
from aha_cli.store.io import iter_jsonl_reverse, read_json, write_json
from aha_cli.store.paths import aha_home_path, event_path
from aha_cli.store.snapshots import task_snapshot

DIRECT_REPLY_ROUTES = {("main", "feishu"), ("host", "feishu")}
USER_REPLY_ROUTES = DIRECT_REPLY_ROUTES | {
    ("main", "browser"),
    ("host", "browser"),
    ("main", "feishu-assistant"),
    ("host", "feishu-assistant"),
}
USER_TRIGGER_ROUTES = {
    (sender, target)
    for sender in ("browser", "feishu", "feishu-assistant", "weixin", "user")
    for target in ("main", "host")
}
MAX_NOTIFICATION_CHARS = 1800

_state_lock = threading.RLock()


def subscription_state_path(root: Path) -> Path:
    return aha_home_path(root) / "feishu" / "subscriptions.json"


def subscription_state_lock_path(root: Path) -> Path:
    return aha_home_path(root) / "feishu" / ".subscriptions.lock"


@contextmanager
def _locked_subscription_state(root: Path):
    lock_path = subscription_state_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_path.parent.chmod(0o700)
    except OSError:
        pass
    with _state_lock, lock_path.open("a+b") as handle, exclusive_lock(handle):
        try:
            lock_path.chmod(0o600)
        except OSError:
            pass
        state_path = subscription_state_path(root)
        if state_path.exists():
            try:
                state_path.chmod(0o600)
            except OSError:
                pass
        yield


def _load_subscription_state_unlocked(root: Path) -> dict:
    try:
        state = read_json(subscription_state_path(root))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = {}
    subscriptions = state.get("subscriptions") if isinstance(state.get("subscriptions"), dict) else {}
    sent = state.get("sent") if isinstance(state.get("sent"), dict) else {}
    return {"subscriptions": subscriptions, "sent": sent, "updated_at": str(state.get("updated_at") or "")}


def load_subscription_state(root: Path) -> dict:
    with _locked_subscription_state(root):
        return _load_subscription_state_unlocked(root)


def _write_subscription_state_unlocked(root: Path, state: dict) -> None:
    path = subscription_state_path(root)
    write_json(path, state)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def set_subscription(
    root: Path,
    session_key: str,
    *,
    chat_id: str,
    open_id: str,
    run_id: str,
    task_id: str | None,
    chat_type: str | None = None,
    enabled: bool = True,
) -> dict:
    key = str(session_key or "").strip()
    if not key or not chat_id or not run_id:
        raise FeishuError("飞书订阅需要 session_key、chat_id 和 run_id")
    with _locked_subscription_state(root):
        state = _load_subscription_state_unlocked(root)
        if enabled:
            state["subscriptions"][key] = {
                "session_key": key,
                "chat_id": str(chat_id),
                "open_id": str(open_id or ""),
                "chat_type": str(chat_type or ("group" if ":group:" in key else "p2p")),
                "run_id": str(run_id),
                "task_id": str(task_id or "") or None,
                "enabled": True,
                "updated_at": utc_now(),
            }
        else:
            state["subscriptions"].pop(key, None)
        state["updated_at"] = utc_now()
        _write_subscription_state_unlocked(root, state)
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
        return {**send_via_active_channel(root, chat_id, message), "transport": "channel_ws"}
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
        return {
            "ok": True,
            "sent": True,
            "message_id": data.get("message_id"),
            "target": chat_id,
            "transport": "rest",
        }


def _handoff_closure(root: Path, run_id: str, event: dict) -> tuple[dict, str] | None:
    if str(event.get("type") or "") != "message":
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if _message_route(data) not in {("main", "feishu-assistant"), ("host", "feishu-assistant")}:
        return None
    task_id = str(data.get("task_id") or "")
    reply = _message_text(data)
    if not task_id or not reply:
        return None
    handoff = pending_handoff_for_reply(root, run_id, task_id)
    if handoff is None:
        return None
    message = "\n".join(
        [
            "AHA 跟进已完成",
            f"目标：{run_id} / {task_id}",
            "结果：",
            reply,
        ]
    )
    return handoff, _trim_notification(message)


def notify_event(root: Path, run_id: str, event: dict) -> dict:
    closure = _handoff_closure(root, run_id, event)
    if closure is not None:
        handoff, closure_message = closure
        chat_id = str(handoff.get("chat_id") or "")
        if not chat_id:
            mark_service_handoff(root, str(handoff.get("id") or ""), "failed", error="originating chat is missing")
            return {"ok": False, "sent": False, "reason": "handoff_chat_missing"}
        try:
            result = _send(root, chat_id, closure_message)
        except Exception as exc:
            mark_service_handoff(root, str(handoff.get("id") or ""), "pending", error=str(exc))
            raise
        mark_service_handoff(root, str(handoff.get("id") or ""), "delivered")
        audit_feishu_channel(
            root,
            direction="outbound",
            kind="handoff_closure",
            status="delivered",
            transport=str(result.get("transport") or "unknown"),
            message_id=str(result.get("message_id") or ""),
            chat_id=chat_id,
            session_key=str(handoff.get("session_key") or ""),
            run_id=run_id,
            task_id=str((event.get("data") or {}).get("task_id") or ""),
            content=closure_message,
        )
        return {
            "ok": True,
            "sent": True,
            "sent_count": 1,
            "message_id": result.get("message_id"),
            "reason": "service_handoff_closed",
        }
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    is_status_event = str(event.get("type") or "") == "task_status_changed"
    suppressed_chats = (
        consume_status_suppressions(root, run_id, str(data.get("task_id") or ""))
        if is_status_event
        else set()
    )
    message = notification_message_for_event(root, run_id, event)
    card = data.get("feishu_card") if isinstance(data.get("feishu_card"), dict) else None
    confirmation_id = str(data.get("feishu_confirmation_id") or "")
    if not message and not card:
        return {"ok": True, "sent": False, "reason": "ignored_event"}
    task_id = str(data.get("task_id") or "")
    event_key = _event_key(run_id, event)
    sent_count = 0
    visited_recipients: set[str] = set()
    with _locked_subscription_state(root):
        state = _load_subscription_state_unlocked(root)
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
            if is_status_event and chat_id in suppressed_chats:
                continue
            recipient_key = _status_recipient_key(chat_id) if is_status_event else session_key
            if recipient_key in visited_recipients:
                continue
            visited_recipients.add(recipient_key)
            sent_key = f"{recipient_key}:{event_key}"
            if sent_key in state["sent"]:
                continue
            result = _send(root, chat_id, message, card=card)
            audit_feishu_channel(
                root,
                direction="outbound",
                kind="confirmation_card" if card else "notification",
                status="delivered",
                transport=str(result.get("transport") or "unknown"),
                message_id=str(result.get("message_id") or ""),
                chat_id=chat_id,
                session_key=str(session_key),
                run_id=run_id,
                task_id=task_id,
                content={"card": card} if card else message,
                reason=str(event.get("type") or ""),
            )
            if confirmation_id and result.get("message_id"):
                bind_confirmation_card(
                    root,
                    confirmation_id,
                    message_id=str(result.get("message_id") or ""),
                    chat_id=chat_id,
                )
            state["sent"][sent_key] = {
                "sent_at": utc_now(),
                "message_id": result.get("message_id"),
            }
            sent_count += 1
        if len(state["sent"]) > 4096:
            state["sent"] = dict(list(state["sent"].items())[-4096:])
        state["updated_at"] = utc_now()
        _write_subscription_state_unlocked(root, state)
    return {"ok": True, "sent": sent_count > 0, "sent_count": sent_count, "reason": "sent" if sent_count else "no_subscription"}


__all__ = [
    "load_subscription_state",
    "notification_message_for_event",
    "notify_event",
    "set_subscription",
    "subscription_state_lock_path",
    "subscription_state_path",
]
