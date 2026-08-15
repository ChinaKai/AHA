from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import threading

from aha_cli.locking import exclusive_lock
from aha_cli.domain.models import is_system_managed, utc_now
from aha_cli.domain.models import is_service_assistant_task
from aha_cli.services.feishu import (
    FeishuError,
    bind_confirmation_card,
    send_card_message,
    send_text_message,
    update_card_message,
)
from aha_cli.services.feishu_audit import _AUTH_RE, _SECRET_RE, audit_feishu_channel
from aha_cli.services.feishu_runtime import (
    feishu_config,
    feishu_credentials,
    send_via_active_channel,
    update_card_via_active_channel,
)
from aha_cli.services.service_assistant_handoffs import (
    consume_status_suppressions,
    mark_service_handoff,
    pending_handoff_for_reply,
)
from aha_cli.store.event_views import conversation_event_visible, is_aha_action_envelope_text
from aha_cli.store.io import iter_jsonl_reverse, read_json, write_json
from aha_cli.store.paths import aha_home_path, event_path, plan_path

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
TASK_CHAT_CONTROL_ACTION_KIND = "aha_task_chat_control"
TASK_CHAT_ENTRY_ACTION_KIND = "aha_task_chat_entry"
TASK_CHAT_CONTROL_STAY_CHOICE_ID = "stay"
TASK_CHAT_CONTROL_EXIT_CHOICE_ID = "exit"
TASK_CHAT_CONTROL_STATUSES = {"awaiting_user", "completed", "failed", "blocked"}
TASK_CHAT_MIRROR_LIMIT = 64
TASK_CHAT_MIRROR_EVENT_WINDOW = 256
TASK_CHAT_MIRROR_TURN_TTL_SECONDS = 120

_state_lock = threading.RLock()


def subscription_state_path(root: Path) -> Path:
    return aha_home_path(root) / "feishu" / "subscriptions.json"


def status_cards_path(root: Path) -> Path:
    return aha_home_path(root) / "feishu" / "status_cards.json"


def _load_status_cards_unlocked(root: Path) -> dict:
    try:
        cards = read_json(status_cards_path(root))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        cards = {}
    return cards if isinstance(cards, dict) else {}


def status_card_key(run_id: str, task_id: str, chat_id: str) -> str:
    return f"{run_id}:{task_id}:{chat_id}"


def _record_status_card_unlocked(root: Path, key: str, message_id: str, *, message: str = "") -> dict | None:
    cards = _load_status_cards_unlocked(root)
    previous = cards.get(key)
    cards[key] = {
        "message_id": str(message_id or "").strip(),
        "message": str(message or "").strip(),
        "active": True,
        "updated_at": utc_now(),
    }
    write_json(status_cards_path(root), cards)
    return dict(previous) if isinstance(previous, dict) and previous.get("message_id") else None


def record_status_card(root: Path, key: str, message_id: str, *, message: str = "") -> dict | None:
    """Record the latest status card for a task/chat and return the previous one.

    A new status card for the same task+chat supersedes the previous one, so the
    caller can invalidate the old card (e.g. update it to 'status updated').
    """
    with _locked_subscription_state(root):
        return _record_status_card_unlocked(root, key, message_id, message=message)


def consume_status_card(root: Path, key: str, message_id: str) -> bool:
    """Deactivate a status card after it has been acted on (e.g. entering Task Chat)."""
    with _locked_subscription_state(root):
        cards = _load_status_cards_unlocked(root)
        record = cards.get(key)
        if not isinstance(record, dict) or str(record.get("message_id") or "") != str(message_id or ""):
            return False
        record["active"] = False
        record["consumed_at"] = utc_now()
        write_json(status_cards_path(root), cards)
        return True


def _status_card_terminal(card: dict, reason: str) -> dict:
    """Build a terminal card marking a status card as no longer actionable."""
    labels = {
        "entered": ("已进入 Task Chat", "green", "本状态卡已处理，请直接在该 Task Chat 中继续对话。"),
        "superseded": ("状态已更新", "grey", "本状态卡已失效，请查看最新卡片。"),
    }
    title, template, detail = labels.get(reason, ("状态已更新", "grey", "本卡片已失效。"))
    body_elements: list[dict] = []
    content = str(card.get("message") or "")
    if content:
        body_elements.append({"tag": "markdown", "content": content})
    body_elements.append({"tag": "markdown", "content": f"<font color='grey'>{detail}</font>"})
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "body": {"elements": body_elements},
    }


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
    mode: str | None = None,
) -> dict:
    key = str(session_key or "").strip()
    if not key or not chat_id or not run_id:
        raise FeishuError("飞书订阅需要 session_key、chat_id 和 run_id")
    with _locked_subscription_state(root):
        state = _load_subscription_state_unlocked(root)
        if enabled:
            subscription = {
                "session_key": key,
                "tenant_key": _session_tenant_key(key),
                "chat_id": str(chat_id),
                "open_id": str(open_id or ""),
                "chat_type": str(chat_type or ("group" if ":group:" in key else "p2p")),
                "run_id": str(run_id),
                "task_id": str(task_id or "") or None,
                "enabled": True,
                "updated_at": utc_now(),
            }
            normalized_mode = str(mode or "").strip()
            if normalized_mode:
                subscription["mode"] = normalized_mode
            state["subscriptions"][key] = subscription
        else:
            state["subscriptions"].pop(key, None)
        state["updated_at"] = utc_now()
        _write_subscription_state_unlocked(root, state)
    return dict(state["subscriptions"].get(key) or {"session_key": key, "enabled": False})


def remove_subscriptions(root: Path, session_keys: list[str] | set[str] | tuple[str, ...]) -> dict:
    keys = {str(key or "").strip() for key in session_keys if str(key or "").strip()}
    if not keys:
        return {"removed_count": 0}
    with _locked_subscription_state(root):
        state = _load_subscription_state_unlocked(root)
        before = len(state["subscriptions"])
        for key in keys:
            state["subscriptions"].pop(key, None)
        removed_count = before - len(state["subscriptions"])
        if removed_count:
            state["updated_at"] = utc_now()
            _write_subscription_state_unlocked(root, state)
    return {"removed_count": removed_count}


def resolve_task_chat_control(
    root: Path,
    *,
    message_id: str,
    chat_id: str,
    open_id: str,
    choice_id: str,
) -> dict:
    identity = str(message_id or "").strip()
    choice = str(choice_id or "").strip().lower()
    if not identity or choice not in {TASK_CHAT_CONTROL_STAY_CHOICE_ID, TASK_CHAT_CONTROL_EXIT_CHOICE_ID}:
        raise FeishuError("Task Chat 控制卡片操作无效", code="invalid_task_chat_control")
    with _locked_subscription_state(root):
        state = _load_subscription_state_unlocked(root)
        for session_key, subscription in state["subscriptions"].items():
            if not isinstance(subscription, dict) or not subscription.get("enabled"):
                continue
            if str(subscription.get("mode") or "") != "task_chat":
                continue
            if str(subscription.get("chat_id") or "") != str(chat_id or ""):
                continue
            if str(subscription.get("open_id") or "") != str(open_id or ""):
                continue
            control = subscription.get("task_chat_control")
            if not isinstance(control, dict) or str(control.get("message_id") or "") != identity:
                continue
            if not control.get("active"):
                raise FeishuError("该 Task Chat 控制卡片已失效，请使用最新卡片", code="stale_task_chat_control")
            control["active"] = False
            control["choice_id"] = choice
            control["resolved_at"] = utc_now()
            subscription["task_chat_control"] = control
            state["updated_at"] = utc_now()
            _write_subscription_state_unlocked(root, state)
            return {"session_key": str(session_key), "subscription": dict(subscription), "control": dict(control)}
    raise FeishuError("该 Task Chat 控制卡片已失效，请使用最新卡片", code="stale_task_chat_control")


def set_task_chat_control(root: Path, session_key: str, control: dict) -> dict:
    """Attach an active Task Chat control card to a subscription.

    Used when a Task Chat entry card (with its exit button) is shown so the
    button's callback can be resolved by :func:`resolve_task_chat_control`.
    ``control`` must carry ``message_id`` (the card's message id), ``run_id``,
    ``task_id`` and ``status``.
    """
    key = str(session_key or "").strip()
    if not key or not isinstance(control, dict) or not str(control.get("message_id") or "").strip():
        raise FeishuError("需要有效的 session_key 和 task_chat_control.message_id", code="invalid_task_chat_control")
    with _locked_subscription_state(root):
        state = _load_subscription_state_unlocked(root)
        subscription = state["subscriptions"].get(key)
        if not isinstance(subscription, dict):
            raise FeishuError("Task Chat 会话不存在或已结束", code="task_chat_not_found")
        subscription["task_chat_control"] = {
            "active": True,
            "message_id": str(control.get("message_id") or "").strip(),
            "run_id": str(control.get("run_id") or subscription.get("run_id") or ""),
            "task_id": str(control.get("task_id") or subscription.get("task_id") or "") or None,
            "status": str(control.get("status") or "-"),
            "sent_at": utc_now(),
        }
        state["updated_at"] = utc_now()
        _write_subscription_state_unlocked(root, state)
        return dict(subscription["task_chat_control"])


def _session_tenant_key(session_key: object) -> str:
    return str(session_key or "").split(":", 1)[0].strip()


def _current_tenant_key(root: Path) -> str:
    config = feishu_config(root)
    app_id, _app_secret = feishu_credentials(config)
    return str(app_id or "").strip()


def _matches_current_tenant(session_key: object, current_tenant_key: str) -> bool:
    current = str(current_tenant_key or "").strip()
    if not current:
        return True
    tenant = _session_tenant_key(session_key)
    return bool(tenant and tenant == current)


def _subscription_chat_type(session_key: object, subscription: dict) -> str:
    value = str(subscription.get("chat_type") or "").strip().lower()
    if value:
        return value
    return "group" if ":group:" in str(session_key or "").lower() else "p2p"


def _status_owner(root: Path, current_tenant_key: str) -> dict:
    tenant = str(current_tenant_key or "").strip()
    if not tenant:
        return {}
    try:
        from aha_cli.services.feishu_owner import resolve_feishu_owner

        owner = resolve_feishu_owner(root, tenant_key=tenant, config=feishu_config(root))
    except (FeishuError, OSError, ValueError, RuntimeError):
        return {}
    return owner if isinstance(owner, dict) and owner.get("ok") else {}


def _matches_status_owner_subscription(session_key: object, subscription: dict, owner: dict) -> bool:
    if not owner:
        return False
    if _subscription_chat_type(session_key, subscription) != "p2p":
        return False
    owner_session_key = str(owner.get("session_key") or "").strip()
    if owner_session_key and str(session_key or "") == owner_session_key:
        return True
    return bool(
        str(subscription.get("open_id") or "").strip() == str(owner.get("open_id") or "").strip()
        and str(subscription.get("chat_id") or "").strip() == str(owner.get("chat_id") or "").strip()
    )


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


def _hard_redact_error(text: str) -> str:
    """Remove credential material regardless of audience. Otherwise keep detail."""
    text = _AUTH_RE.sub("Authorization=[REDACTED]", text)
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return text


def sanitize_agent_error_message(message: object, *, group: bool = False) -> str:
    """Build a user-visible agent_error text scoped to the receiving audience.

    Private chats keep the underlying error detail (credential-level redaction
    only). Group chats get a generic, actionable hint because the audience is
    wider and must not see upstream hosts, paths, ids, tokens or stack traces.
    """
    text = " ".join(str(message or "").split())
    if not text:
        return ""
    if group:
        return "AHA Agent 执行失败，请稍后重试或联系管理员。"
    return _trim_notification(_hard_redact_error(text))


def _last_task_agent_error(root: Path, run_id: str, task_id: str, event: dict) -> str:
    """Find the newest agent_error message for a task before the given event."""
    current_event_id = event.get("event_id")
    event_data = event.get("data") if isinstance(event.get("data"), dict) else {}
    found_current = False
    for _offset, candidate in iter_jsonl_reverse(event_path(root, run_id)) or ():
        if not found_current:
            candidate_data = candidate.get("data") if isinstance(candidate.get("data"), dict) else {}
            same_id = current_event_id not in {None, ""} and candidate.get("event_id") == current_event_id
            same_payload = (
                candidate.get("ts") == event.get("ts")
                and candidate.get("type") == event.get("type")
                and str(candidate_data.get("task_id") or "") == str(event_data.get("task_id") or "")
            )
            if same_id or same_payload:
                found_current = True
            continue
        candidate_data = candidate.get("data") if isinstance(candidate.get("data"), dict) else {}
        if str(candidate_data.get("task_id") or "") != task_id:
            continue
        if str(candidate.get("type") or "") != "agent_error":
            continue
        message = " ".join(
            str(candidate_data.get("message") or candidate_data.get("error") or candidate_data.get("text") or "").split()
        )
        if message:
            return message
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


def _event_plan_and_task(root: Path, run_id: str, task_id: str) -> tuple[dict, dict]:
    if not task_id:
        return {}, {}
    try:
        plan = read_json(plan_path(root, run_id))
    except (FileNotFoundError, KeyError, json.JSONDecodeError, OSError, ValueError):
        return {}, {}
    if not isinstance(plan, dict):
        return {}, {}
    tasks = plan.get("tasks") if isinstance(plan.get("tasks"), list) else []
    for task in tasks:
        if isinstance(task, dict) and str(task.get("id") or "") == task_id:
            return plan, task
    return plan, {}


def notification_message_for_event(root: Path, run_id: str, event: dict) -> str:
    event_type = str(event.get("type") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    config = feishu_config(root)
    task_id = str(data.get("task_id") or "")
    plan, task = _event_plan_and_task(root, run_id, task_id)
    system_assistant = is_service_assistant_task(task)
    system_status_source = is_system_managed(plan) or is_system_managed(task)
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
    if system_status_source:
        return ""
    previous = _display_status(data.get("previous_status"))
    current = _display_status(data.get("status"))
    if previous == current:
        return ""
    if not task_id:
        return ""
    reason = _status_event_reason(data)
    if current == "failed":
        agent_error = _last_task_agent_error(root, run_id, task_id, event)
        source_message = (
            reason
            or (_hard_redact_error(agent_error) if agent_error else "")
            or _last_task_message(root, run_id, task_id, event, USER_REPLY_ROUTES)
        )
    elif current == "busy":
        source_message = _last_task_message(root, run_id, task_id, event, USER_TRIGGER_ROUTES) or reason
    elif previous == "busy":
        source_message = _last_task_message(root, run_id, task_id, event, USER_REPLY_ROUTES) or reason
    else:
        source_message = reason or _last_task_message(root, run_id, task_id, event, USER_REPLY_ROUTES)
    event_time = " ".join(str(event.get("ts") or "").split()).replace("T", " ", 1) or "-"
    run_name = " ".join(str(plan.get("goal") or plan.get("name") or run_id).split()) or run_id
    task_title = " ".join(str(task.get("title") or "").split()) or "-"
    message = "\n".join(
        [
            f"Time: {event_time}",
            f"Task: {run_name}.{task_id}",
            f"Task Title: {task_title}",
            f"Status: {previous} -> {current}",
            f"Message: {source_message or '-'}",
        ]
    )
    return _trim_notification(message)


_STATUS_MESSAGE_MAX_CHARS = 96


def _status_message_elements(value: str, *, max_chars: int = _STATUS_MESSAGE_MAX_CHARS) -> list[dict]:
    """Render the status Message as at most two visible lines.

    Card JSON 2.0 supports collapsible_panel, so a long message is folded in
    full behind a collapsed panel instead of being truncated; the plain-text
    transport (which cannot fold) keeps its truncation in ``_trim_notification``.
    """
    text = " ".join(str(value or "-").split())
    if len(text) <= max_chars:
        return [{"tag": "markdown", "content": f"**Message**\n{text}"}]
    return [
        {
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {
                "title": {"tag": "plain_text", "content": "Message（正文较长，点击展开）"},
                "vertical_align": "center",
            },
            "border": {"color": "grey", "corner_radius": "5px"},
            "elements": [{"tag": "markdown", "content": text}],
        }
    ]


def _status_notification_card(message: str, event: dict) -> dict:
    fields: dict[str, str] = {}
    for line in str(message or "").splitlines():
        key, separator, value = line.partition(": ")
        if separator:
            fields[key] = value
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    status = str(data.get("status") or "").strip().lower()
    template = {
        "running": "blue",
        "awaiting_user": "orange",
        "completed": "green",
        "failed": "red",
        "blocked": "red",
    }.get(status, "grey")
    body_elements = [
        {
            "tag": "markdown",
            "content": "\n".join(
                [
                    f"**Time**：{fields.get('Time', '-')}",
                    f"**Task**：`{fields.get('Task', '-')}`",
                    f"**Task Title**：{fields.get('Task Title', '-')}",
                    f"**Status**：`{fields.get('Status', '-')}`",
                ]
            ),
        },
        *_status_message_elements(fields.get("Message", "-")),
    ]
    task_id = str(data.get("task_id") or "").strip()
    run_id = str(event.get("run_id") or "").strip()
    if task_id:
        body_elements.append(
            {
                "tag": "column_set",
                "columns": [
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            {
                                "tag": "button",
                                "element_id": "aha_task_chat_entry",
                                "text": {"tag": "plain_text", "content": "进入 Task Chat"},
                                "type": "primary",
                                "behaviors": [
                                    {
                                        "type": "callback",
                                        "value": {
                                            "kind": TASK_CHAT_ENTRY_ACTION_KIND,
                                            "run_id": run_id,
                                            "task_id": task_id,
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "AHA Task 状态更新"},
            "template": template,
        },
        "body": {"elements": body_elements},
    }


def _task_chat_message_for_event(event: dict, task_id: str) -> str:
    event_type = str(event.get("type") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if not conversation_event_visible(event, str(task_id or ""), "main", {"chat"}):
        return ""
    if event_type == "message":
        sender, _target = _message_route(data)
        if sender == "feishu":
            return ""
        message = _message_text(data)
    elif event_type == "agent_message":
        message = str(data.get("text") or "").strip()
        if is_aha_action_envelope_text(message):
            return ""
    elif event_type == "agent_error":
        error = str(data.get("message") or data.get("error") or data.get("text") or "").strip()
        target = str(data.get("target") or data.get("agent_id") or "main").strip() or "main"
        message = f"Agent error ({target})\n{error}" if error else f"Agent error ({target})"
    else:
        return ""
    if not message:
        return ""
    return _trim_notification(message)


def _event_id_number(event: dict) -> int | None:
    try:
        return int(event.get("event_id"))
    except (TypeError, ValueError):
        return None


def _task_chat_mirror_source(event: dict) -> tuple[str, str] | None:
    if str(event.get("type") or "") != "agent_message":
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    text = str(data.get("text") or "").strip()
    agent = str(data.get("target") or "main").strip().lower() or "main"
    return (agent, text) if text and not is_aha_action_envelope_text(text) else None


def _task_chat_mirror_candidate(event: dict) -> tuple[str, str, str] | None:
    if str(event.get("type") or "") != "message":
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    sender, _target = _message_route(data)
    text = _message_text(data)
    turn_identity = str(data.get("source_turn_identity") or "").strip()
    return (sender, text, turn_identity) if sender and text else None


def _task_chat_mirror_recorded_at(item: dict) -> float | None:
    value = str(item.get("recorded_at") or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _remember_task_chat_mirror(subscription: dict, event: dict) -> None:
    source = _task_chat_mirror_source(event)
    if source is None:
        return
    agent, text = source
    pending = subscription.get("task_chat_pending_mirrors")
    mirrors = list(pending) if isinstance(pending, list) else []
    mirrors.append(
        {
            "agent": agent,
            "text": text,
            "event_id": _event_id_number(event),
            "recorded_at": utc_now(),
        }
    )
    subscription["task_chat_pending_mirrors"] = mirrors[-TASK_CHAT_MIRROR_LIMIT:]


def _consume_task_chat_mirror(subscription: dict, event: dict) -> bool:
    candidate = _task_chat_mirror_candidate(event)
    pending = subscription.get("task_chat_pending_mirrors")
    if candidate is None or not isinstance(pending, list):
        return False
    sender, text, turn_identity = candidate
    current_event_id = _event_id_number(event)
    current_time = datetime.fromisoformat(utc_now().replace("Z", "+00:00")).timestamp()
    mirrors = [item for item in pending if isinstance(item, dict)]
    match_index: int | None = None
    kept: list[dict] = []
    for item in mirrors:
        source_event_id = item.get("event_id") if isinstance(item.get("event_id"), int) else None
        recorded_at = _task_chat_mirror_recorded_at(item)
        if turn_identity:
            if recorded_at is None or current_time - recorded_at > TASK_CHAT_MIRROR_TURN_TTL_SECONDS:
                continue
        elif (
            current_event_id is not None
            and source_event_id is not None
            and current_event_id - source_event_id > TASK_CHAT_MIRROR_EVENT_WINDOW
        ):
            continue
        kept.append(item)
    for index in range(len(kept) - 1, -1, -1):
        item = kept[index]
        source_event_id = item.get("event_id") if isinstance(item.get("event_id"), int) else None
        follows_source = current_event_id is None or source_event_id is None or current_event_id >= source_event_id
        if follows_source and str(item.get("agent") or "") == sender and str(item.get("text") or "") == text:
            match_index = index
            break
    if match_index is None:
        subscription["task_chat_pending_mirrors"] = kept[-TASK_CHAT_MIRROR_LIMIT:]
        return False
    kept.pop(match_index)
    subscription["task_chat_pending_mirrors"] = kept[-TASK_CHAT_MIRROR_LIMIT:]
    return True


def _has_task_chat_subscription(root: Path, run_id: str, task_id: str) -> bool:
    if not run_id or not task_id:
        return False
    state = load_subscription_state(root)
    for subscription in state.get("subscriptions", {}).values():
        if not isinstance(subscription, dict) or not subscription.get("enabled"):
            continue
        if str(subscription.get("mode") or "") != "task_chat":
            continue
        if str(subscription.get("run_id") or "") == run_id and str(subscription.get("task_id") or "") == task_id:
            return True
    return False


def _has_plain_subscription(root: Path, run_id: str, task_id: str) -> bool:
    if not run_id or not task_id:
        return False
    state = load_subscription_state(root)
    for subscription in state.get("subscriptions", {}).values():
        if not isinstance(subscription, dict) or not subscription.get("enabled"):
            continue
        if str(subscription.get("mode") or "") == "task_chat":
            continue
        if str(subscription.get("run_id") or "") == run_id and str(subscription.get("task_id") or "") == task_id:
            return True
    return False


def _task_chat_control_button(label: str, choice_id: str, button_type: str) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": button_type,
        "behaviors": [
            {
                "type": "callback",
                "value": {"kind": TASK_CHAT_CONTROL_ACTION_KIND, "choice_id": choice_id},
            }
        ],
    }


def _task_chat_control_card(run_id: str, task: dict, status: str) -> dict:
    task_id = str(task.get("id") or "")
    title = " ".join(str(task.get("title") or "未命名 Task").split())[:120]
    terminal = status in {"completed", "failed", "blocked"}
    prompt = (
        "该 Task 已进入终态。你可以退出 Task Chat 回到 AHA 管家，或暂时保留当前会话。"
        if terminal
        else "当前回合已结束。继续发送文本仍会转给该 Task；也可以退出并回到 AHA 管家。"
    )
    stay_label = "暂不退出" if terminal else "继续当前 Task"
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "Task Chat 等待操作"},
            "template": "grey" if terminal else "orange",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "\n".join(
                        [
                            f"**Task**：`{run_id} / {task_id}`",
                            f"**标题**：{title}",
                            f"**状态**：{status}",
                            "",
                            prompt,
                        ]
                    ),
                },
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                _task_chat_control_button(
                                    stay_label,
                                    TASK_CHAT_CONTROL_STAY_CHOICE_ID,
                                    "default",
                                )
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                _task_chat_control_button(
                                    "退出 Task Chat",
                                    TASK_CHAT_CONTROL_EXIT_CHOICE_ID,
                                    "primary",
                                )
                            ],
                        },
                    ],
                },
            ]
        },
    }


def task_chat_control_terminal_card(control: dict, outcome: str) -> dict:
    run_id = str(control.get("run_id") or "")
    task_id = str(control.get("task_id") or "")
    status = str(control.get("status") or "-")
    stay_detail = (
        "会话已保留；如需继续发送消息，请先在 Web 中重新打开该 Task。"
        if status in {"completed", "failed", "blocked"}
        else "后续文本仍会发给当前 Task。"
    )
    labels = {
        "stay": ("已保留 Task Chat", "blue", stay_detail),
        "exit": ("已退出 Task Chat", "green", "后续文本将发给 AHA 管家。"),
        "running": ("Task 正在处理中", "blue", "本轮控制卡已失效，处理结束后会发送新的控制卡。"),
        "superseded": ("控制卡已更新", "grey", "请使用聊天底部最新的 Task Chat 控制卡。"),
    }
    title, template, detail = labels.get(str(outcome or ""), labels["superseded"])
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "\n".join(
                        [
                            f"**Task**：`{run_id} / {task_id}`",
                            f"**状态**：{status}",
                            "",
                            detail,
                        ]
                    ),
                }
            ]
        },
    }


def _update_card(root: Path, message_id: str, card: dict) -> dict:
    try:
        return update_card_via_active_channel(root, message_id, card)
    except (RuntimeError, TimeoutError):
        config = feishu_config(root)
        app_id, app_secret = feishu_credentials(config)
        if not app_id or not app_secret:
            raise FeishuError("飞书 App ID 或 App Secret 未配置")
        return update_card_message(root, app_id, app_secret, message_id, card)


def _send(root: Path, chat_id: str, text: str, *, card: dict | None = None, opts: dict | None = None) -> dict:
    try:
        message = {"card": card} if isinstance(card, dict) and card else {"text": text}
        return {**send_via_active_channel(root, chat_id, message, opts), "transport": "channel_ws"}
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


def send_direct_message(root: Path, chat_id: str, text: str, *, card: dict | None = None, opts: dict | None = None) -> dict:
    return _send(root, chat_id, text, card=card, opts=opts)


def _mention_text(text: str, open_id: str) -> str:
    identity = str(open_id or "").strip()
    message = str(text or "").strip()
    if not identity or message.startswith("<at "):
        return message
    return f'<at user_id="{identity}"></at> {message}'.strip()


def _last_group_feishu_chat(root: Path, run_id: str, task_id: str) -> str:
    """Find the newest group-digital-human chat_id for a task from its event stream."""
    if not task_id:
        return ""
    for _offset, candidate in iter_jsonl_reverse(event_path(root, run_id)) or ():
        candidate_data = candidate.get("data") if isinstance(candidate.get("data"), dict) else {}
        if str(candidate_data.get("task_id") or "") != task_id:
            continue
        if str(candidate.get("type") or "") != "message":
            continue
        if str(candidate_data.get("feishu_channel") or "") != "group_digital_human":
            continue
        chat_id = str(candidate_data.get("feishu_chat_id") or "").strip()
        if chat_id:
            return chat_id
    return ""


def _group_agent_error_delivery(root: Path, run_id: str, event: dict) -> dict | None:
    if str(event.get("type") or "") != "agent_error":
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    task_id = str(data.get("task_id") or "")
    raw_error = str(data.get("message") or data.get("error") or data.get("text") or "").strip()
    if not task_id or not raw_error:
        return None
    chat_id = _last_group_feishu_chat(root, run_id, task_id)
    if not chat_id:
        return None
    message = sanitize_agent_error_message(raw_error, group=True)
    event_key = _event_key(run_id, event)
    sent_key = f"group-error:{_status_recipient_key(chat_id)}:{event_key}"
    with _locked_subscription_state(root):
        state = _load_subscription_state_unlocked(root)
        if sent_key in state["sent"]:
            return {"ok": True, "sent": False, "reason": "duplicate_event"}
        result = _send(root, chat_id, message)
        state["sent"][sent_key] = {"sent_at": utc_now(), "message_id": result.get("message_id")}
        state["updated_at"] = utc_now()
        _write_subscription_state_unlocked(root, state)
    audit_feishu_channel(
        root,
        direction="outbound",
        kind="group_error_notice",
        status="delivered",
        transport=str(result.get("transport") or "unknown"),
        message_id=str(result.get("message_id") or ""),
        chat_id=chat_id,
        run_id=run_id,
        task_id=task_id,
        content=message,
        reason=str(event.get("type") or ""),
    )
    return {
        "ok": True,
        "sent": True,
        "sent_count": 1,
        "message_id": result.get("message_id"),
        "reason": "group_agent_error",
    }


def _direct_feishu_delivery(root: Path, run_id: str, event: dict) -> dict | None:
    if str(event.get("type") or "") != "message":
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if _message_route(data) not in DIRECT_REPLY_ROUTES:
        return None
    chat_id = str(data.get("feishu_chat_id") or "").strip()
    if not chat_id:
        return None
    message = _message_text(data)
    card = data.get("feishu_card") if isinstance(data.get("feishu_card"), dict) else None
    if not message and not card:
        return {"ok": True, "sent": False, "reason": "ignored_event"}
    mention_open_id = str(data.get("feishu_mention_open_id") or "").strip()
    group_context = str(data.get("feishu_chat_type") or "").lower() == "group" or str(data.get("feishu_channel") or "") == "group_digital_human"
    if mention_open_id and group_context:
        message = _mention_text(message, mention_open_id)
    reply_to = str(data.get("feishu_reply_to") or data.get("feishu_message_id") or "").strip()
    opts = {"reply_to": reply_to} if reply_to else None
    event_key = _event_key(run_id, event)
    recipient_key = f"direct:{_status_recipient_key(chat_id)}"
    sent_key = f"{recipient_key}:{event_key}"
    with _locked_subscription_state(root):
        state = _load_subscription_state_unlocked(root)
        if sent_key in state["sent"]:
            return {"ok": True, "sent": False, "reason": "duplicate_event"}
        result = _send(root, chat_id, message, card=card, opts=opts)
        confirmation_id = str(data.get("feishu_confirmation_id") or "")
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
        state["updated_at"] = utc_now()
        _write_subscription_state_unlocked(root, state)
    audit_feishu_channel(
        root,
        direction="outbound",
        kind="direct_reply",
        status="delivered",
        transport=str(result.get("transport") or "unknown"),
        message_id=str(result.get("message_id") or ""),
        chat_id=chat_id,
        run_id=run_id,
        task_id=str(data.get("task_id") or ""),
        content={"card": card} if card else message,
        reason=str(event.get("type") or ""),
    )
    return {
        "ok": True,
        "sent": True,
        "sent_count": 1,
        "message_id": result.get("message_id"),
        "reason": "direct_metadata",
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
    group_error = _group_agent_error_delivery(root, run_id, event)
    if group_error is not None:
        return group_error
    direct = _direct_feishu_delivery(root, run_id, event)
    if direct is not None:
        return direct
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
    task_id = str(data.get("task_id") or "")
    is_status_event = str(event.get("type") or "") == "task_status_changed"
    suppressed_chats = consume_status_suppressions(root, run_id, task_id) if is_status_event else set()
    message = notification_message_for_event(root, run_id, event)
    card = data.get("feishu_card") if isinstance(data.get("feishu_card"), dict) else None
    if is_status_event and message and card is None:
        card = _status_notification_card(message, event)
    notification_kind = (
        "status_card" if is_status_event and card else "confirmation_card" if card else "notification"
    )
    confirmation_id = str(data.get("feishu_confirmation_id") or "")
    task_chat_message = _task_chat_message_for_event(event, task_id)
    has_task_chat = _has_task_chat_subscription(root, run_id, task_id)
    if task_chat_message and not has_task_chat:
        task_chat_message = ""
    is_agent_error_event = not is_status_event and str(event.get("type") or "") == "agent_error"
    raw_error_message = (
        str(data.get("message") or data.get("error") or data.get("text") or "")
        if is_agent_error_event
        else ""
    )
    task_chat_status = str(data.get("status") or "").strip().lower() if is_status_event and has_task_chat else ""
    if task_chat_status not in TASK_CHAT_CONTROL_STATUSES | {"running"}:
        task_chat_status = ""
    _plan, event_task = _event_plan_and_task(root, run_id, task_id)
    task_chat_control_card = (
        _task_chat_control_card(run_id, event_task or {"id": task_id}, task_chat_status)
        if task_chat_status in TASK_CHAT_CONTROL_STATUSES
        else None
    )
    has_non_task_chat_agent_error = bool(is_agent_error_event and raw_error_message and _has_plain_subscription(root, run_id, task_id))
    if not message and not card and not task_chat_message and not task_chat_status and not has_non_task_chat_agent_error:
        return {"ok": True, "sent": False, "reason": "ignored_event"}
    event_key = _event_key(run_id, event)
    sent_count = 0
    updated_count = 0
    deduplicated_count = 0
    failed_count = 0
    skipped_tenant_count = 0
    current_tenant_key = _current_tenant_key(root)
    owner = _status_owner(root, current_tenant_key) if is_status_event else {}
    skipped_owner_count = 0
    skipped_group_count = 0
    visited_recipients: set[str] = set()
    with _locked_subscription_state(root):
        state = _load_subscription_state_unlocked(root)
        for session_key, subscription in list(state["subscriptions"].items()):
            if not isinstance(subscription, dict) or not subscription.get("enabled"):
                continue
            if not _matches_current_tenant(session_key, current_tenant_key):
                skipped_tenant_count += 1
                continue
            if _subscription_chat_type(session_key, subscription) == "group" and not is_agent_error_event:
                skipped_group_count += 1
                continue
            mode = str(subscription.get("mode") or "")
            chat_id = str(subscription.get("chat_id") or "")
            if not chat_id:
                continue
            subscribed_task = str(subscription.get("task_id") or "")
            if (
                is_status_event
                and mode == "task_chat"
                and str(subscription.get("run_id") or "") == run_id
                and subscribed_task == task_id
            ):
                if not task_chat_status:
                    continue
                recipient_key = str(session_key)
                if recipient_key in visited_recipients:
                    continue
                visited_recipients.add(recipient_key)
                sent_key = f"{recipient_key}:{event_key}"
                if sent_key in state["sent"]:
                    continue
                previous_control = (
                    dict(subscription.get("task_chat_control") or {})
                    if isinstance(subscription.get("task_chat_control"), dict)
                    else {}
                )
                previous_message_id = str(previous_control.get("message_id") or "")
                if task_chat_status == "running":
                    if not previous_control.get("active") or not previous_message_id:
                        continue
                    previous_control["active"] = False
                    previous_control["invalidated_at"] = utc_now()
                    previous_control["invalidated_by"] = "running"
                    subscription["task_chat_control"] = previous_control
                    try:
                        _update_card(root, previous_message_id, task_chat_control_terminal_card(previous_control, "running"))
                    except Exception:  # noqa: BLE001 - stale card rejection remains enforced by persisted state.
                        pass
                    state["sent"][sent_key] = {"sent_at": utc_now(), "message_id": previous_message_id}
                    updated_count += 1
                    continue
                try:
                    result = _send(root, chat_id, "", card=task_chat_control_card)
                except Exception as exc:  # noqa: BLE001 - one stale Feishu chat must not block valid subscribers.
                    failed_count += 1
                    audit_feishu_channel(
                        root,
                        direction="outbound",
                        kind="task_chat_control",
                        status="failed",
                        transport="notification",
                        chat_id=chat_id,
                        session_key=str(session_key),
                        run_id=run_id,
                        task_id=task_id,
                        content={"card": task_chat_control_card},
                        error=exc,
                        reason=str(event.get("type") or ""),
                    )
                    continue
                message_id = str(result.get("message_id") or "")
                control = {
                    "active": True,
                    "message_id": message_id,
                    "run_id": run_id,
                    "task_id": task_id,
                    "status": task_chat_status,
                    "event_key": event_key,
                    "sent_at": utc_now(),
                }
                subscription["task_chat_control"] = control
                if previous_control.get("active") and previous_message_id and previous_message_id != message_id:
                    previous_control["active"] = False
                    previous_control["invalidated_at"] = utc_now()
                    previous_control["invalidated_by"] = "superseded"
                    try:
                        _update_card(
                            root,
                            previous_message_id,
                            task_chat_control_terminal_card(previous_control, "superseded"),
                        )
                    except Exception:  # noqa: BLE001 - latest message id still rejects stale callbacks.
                        pass
                audit_feishu_channel(
                    root,
                    direction="outbound",
                    kind="task_chat_control",
                    status="delivered",
                    transport=str(result.get("transport") or "unknown"),
                    message_id=message_id,
                    chat_id=chat_id,
                    session_key=str(session_key),
                    run_id=run_id,
                    task_id=task_id,
                    content={"card": task_chat_control_card},
                    reason=str(event.get("type") or ""),
                )
                state["sent"][sent_key] = {"sent_at": utc_now(), "message_id": message_id}
                sent_count += 1
                continue
            # Status notifications go only to the resolved owner private chat.
            # Direct assistant replies remain scoped to the originating
            # run/task private conversation.
            if is_status_event and not _matches_status_owner_subscription(session_key, subscription, owner):
                skipped_owner_count += 1
                continue
            if not is_status_event and str(subscription.get("run_id") or "") != run_id:
                continue
            if not is_status_event and subscribed_task and subscribed_task != task_id:
                continue
            if is_status_event and chat_id in suppressed_chats:
                continue
            outbound_message = message
            if not is_status_event and str(subscription.get("mode") or "") == "task_chat":
                outbound_message = task_chat_message
            elif is_agent_error_event:
                outbound_message = sanitize_agent_error_message(
                    raw_error_message,
                    group=_subscription_chat_type(session_key, subscription) == "group",
                )
            if not outbound_message and not card:
                continue
            recipient_key = _status_recipient_key(chat_id) if is_status_event else session_key
            if recipient_key in visited_recipients:
                continue
            visited_recipients.add(recipient_key)
            sent_key = f"{recipient_key}:{event_key}"
            if sent_key in state["sent"]:
                continue
            is_task_chat_message = not is_status_event and mode == "task_chat"
            if is_task_chat_message and _consume_task_chat_mirror(subscription, event):
                state["sent"][sent_key] = {"sent_at": utc_now(), "deduplicated": True}
                deduplicated_count += 1
                continue
            try:
                result = _send(root, chat_id, outbound_message, card=card)
            except Exception as exc:  # noqa: BLE001 - one stale Feishu chat must not block valid subscribers.
                failed_count += 1
                audit_feishu_channel(
                    root,
                    direction="outbound",
                    kind=notification_kind,
                    status="failed",
                    transport="notification",
                    chat_id=chat_id,
                    session_key=str(session_key),
                    run_id=run_id,
                    task_id=task_id,
                    content={"card": card} if card else outbound_message,
                    error=exc,
                    reason=str(event.get("type") or ""),
                )
                continue
            audit_feishu_channel(
                root,
                direction="outbound",
                kind=notification_kind,
                status="delivered",
                transport=str(result.get("transport") or "unknown"),
                message_id=str(result.get("message_id") or ""),
                chat_id=chat_id,
                session_key=str(session_key),
                run_id=run_id,
                task_id=task_id,
                content={"card": card} if card else outbound_message,
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
            # Track the latest status card per task+chat so a newer card can
            # invalidate the older one, and the entry button can be consumed.
            if is_status_event and result.get("message_id"):
                card_key = status_card_key(run_id, task_id, chat_id)
                previous = _record_status_card_unlocked(root, card_key, str(result.get("message_id") or ""), message=outbound_message)
                if previous and previous.get("message_id"):
                    try:
                        _update_card(
                            root,
                            str(previous["message_id"]),
                            _status_card_terminal(previous, "superseded"),
                        )
                    except Exception:  # noqa: BLE001 - stale card update must not break the new push.
                        pass
            if is_task_chat_message:
                _remember_task_chat_mirror(subscription, event)
            sent_count += 1
        if len(state["sent"]) > 4096:
            state["sent"] = dict(list(state["sent"].items())[-4096:])
        state["updated_at"] = utc_now()
        _write_subscription_state_unlocked(root, state)
    reason = (
        "sent"
        if sent_count
        else "updated"
        if updated_count
        else "deduplicated"
        if deduplicated_count
        else "send_failed"
        if failed_count
        else "no_subscription"
    )
    return {
        "ok": not failed_count or sent_count > 0 or updated_count > 0,
        "sent": sent_count > 0,
        "sent_count": sent_count,
        "updated_count": updated_count,
        "deduplicated_count": deduplicated_count,
        "failed_count": failed_count,
        "skipped_tenant_count": skipped_tenant_count,
        "skipped_group_count": skipped_group_count,
        "skipped_owner_count": skipped_owner_count,
        "reason": reason,
    }


__all__ = [
    "TASK_CHAT_CONTROL_ACTION_KIND",
    "TASK_CHAT_ENTRY_ACTION_KIND",
    "TASK_CHAT_CONTROL_EXIT_CHOICE_ID",
    "TASK_CHAT_CONTROL_STAY_CHOICE_ID",
    "load_subscription_state",
    "notification_message_for_event",
    "notify_event",
    "remove_subscriptions",
    "resolve_task_chat_control",
    "send_direct_message",
    "set_subscription",
    "status_cards_path",
    "status_card_key",
    "record_status_card",
    "consume_status_card",
    "subscription_state_lock_path",
    "subscription_state_path",
    "task_chat_control_terminal_card",
    "_status_card_terminal",
]
