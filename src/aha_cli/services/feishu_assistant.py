from __future__ import annotations

import json
from pathlib import Path
import queue
import secrets
import sys
import threading
import time
from typing import Any

from aha_cli.services.feishu import (
    ACTION_TOKEN_TTL_SECONDS,
    FeishuError,
    bind_confirmation_card,
    claim_inbound_message,
    consume_confirmation_card,
    confirmation_card_for_message,
    finalize_confirmation_card,
    get_session_binding,
    identity_label_items,
    issue_action_token,
    make_session_key,
    mark_confirmation_card_updated,
    message_attachment_summary,
    message_resource_attachments,
    record_recent_group,
    record_recent_private_chat,
    remember_identity_profile,
    register_confirmation_card,
    refresh_identity_profiles,
    sanitize_card_payload,
    set_session_binding,
)
from aha_cli.services.feishu_audit import audit_feishu_channel
from aha_cli.services.feishu_notifications import load_subscription_state, set_subscription
from aha_cli.services.feishu_owner import remember_owner_private_chat, resolve_feishu_owner, resolve_feishu_owner_by_open_id
from aha_cli.services.feishu_runtime import feishu_config, feishu_credentials
from aha_cli.services.feishu_work_run import feishu_work_run_options, resolve_feishu_work_run_id
from aha_cli.services.feishu_group import (
    ensure_feishu_group_run,
    ensure_feishu_group_task,
    feishu_group_state_dir,
    feishu_group_user_session_key,
    group_agent_message,
    mark_feishu_group_task_interaction,
)
from aha_cli.services.service_assistant import (
    LEGACY_ASSISTANT_RUN_TITLE,
    ensure_service_assistant_run,
    ensure_service_assistant_task,
    session_task_title,
)
from aha_cli.services.service_assistant_actions import (
    ServiceAssistantActionError,
    prepare_service_assistant_action,
    resolve_choice,
    resolve_confirmation,
)
from aha_cli.store.config import load_config
from aha_cli.store.paths import aha_home_path
from aha_cli.store.runs import require_plan, run_exists
from aha_cli.store.snapshots import task_snapshot
from aha_cli.store.task_memos import normalize_memo_date, normalize_memo_status, read_task_memos
from aha_cli.domain.models import is_feishu_group_task, is_service_assistant_task, utc_now
from aha_cli.web.status import TERMINAL_TASK_STATUSES
from aha_cli.web.task_messaging import handle_send_payload

ASSISTANT_QUEUE_LIMIT = 128
ASSISTANT_RUN_TITLE = LEGACY_ASSISTANT_RUN_TITLE
ASSISTANT_TASK_TITLE = "AHA Assistant"
FEISHU_BOT_MENU_EVENT_TYPE = "application.bot.menu_v6"
MENU_QUERY_ACTION = "feishu_menu_query"
MENU_QUERY_SUBMIT_CHOICE_ID = "__submit_menu_query__"
OWNER_MENU_ACTIONS = {
    "aha_create_memo": "create_memo",
    "create_memo": "create_memo",
    "memo_create": "create_memo",
    "aha_create_task": "create_task",
    "create_task": "create_task",
    "task_create": "create_task",
    "aha_list_memos": "list_memos",
    "list_memos": "list_memos",
    "memo_list": "list_memos",
    "aha_list_tasks": "list_tasks",
    "list_tasks": "list_tasks",
    "task_list": "list_tasks",
}

_assistant_queue: queue.Queue[tuple[Path, str, Any, dict] | None] = queue.Queue(maxsize=ASSISTANT_QUEUE_LIMIT)
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def _plain_message(root: Path, message: Any) -> dict:
    config = feishu_config(root)
    app_id, _secret = feishu_credentials(config)
    raw = getattr(message, "raw", {})
    raw = raw if isinstance(raw, dict) else {}
    header = raw.get("header") if isinstance(raw.get("header"), dict) else {}
    raw_message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    raw_event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    if not raw_message and isinstance(raw_event.get("message"), dict):
        raw_message = raw_event["message"]
    sender = getattr(message, "sender", None)
    raw_sender = raw_event.get("sender") if isinstance(raw_event.get("sender"), dict) else {}
    raw_chat = raw_event.get("chat") if isinstance(raw_event.get("chat"), dict) else {}
    message_type = str(
        getattr(message, "message_type", "")
        or raw_message.get("message_type")
        or raw.get("message_type")
        or ""
    ).strip()
    content_value = (
        getattr(message, "content", None)
        or getattr(message, "raw_content", None)
        or raw_message.get("content")
        or raw.get("content")
    )
    if isinstance(content_value, dict):
        content = content_value
    elif isinstance(content_value, str):
        try:
            parsed = json.loads(content_value)
        except json.JSONDecodeError:
            content = {"text": content_value}
        else:
            content = parsed if isinstance(parsed, dict) else {"text": content_value}
    else:
        content = {}
    attachments = message_resource_attachments(
        message_type,
        content,
        message_id=str(getattr(message, "message_id", "") or getattr(message, "id", "") or raw_message.get("message_id") or ""),
    )
    text = str(
        getattr(message, "body_text", "")
        or getattr(message, "safe_content_text", "")
        or getattr(message, "content_text", "")
        or content.get("text")
        or ""
    ).strip()
    if not text and attachments:
        text = message_attachment_summary(attachments)
    return {
        "tenant_key": str(header.get("tenant_key") or app_id or "local"),
        "open_id": str(getattr(message, "sender_id", "") or getattr(sender, "open_id", "") or ""),
        "chat_id": str(getattr(message, "chat_id", "") or ""),
        "chat_type": str(getattr(message, "chat_type", "") or "unknown").lower(),
        "message_id": str(getattr(message, "message_id", "") or getattr(message, "id", "") or raw_message.get("message_id") or ""),
        "root_id": str(getattr(message, "root_id", "") or ""),
        "thread_id": str(getattr(message, "thread_id", "") or getattr(message, "root_id", "") or ""),
        "parent_id": str(getattr(message, "parent_id", "") or ""),
        "message_type": message_type,
        "text": text,
        "attachments": attachments,
        "is_at_bot": bool(getattr(message, "mentioned_bot", False)),
        "sender_is_bot": bool(getattr(message, "sender_is_bot", False)),
        "sender_name": _first_string(
            getattr(message, "sender_name", ""),
            getattr(message, "sendname", ""),
            getattr(message, "send_name", ""),
            getattr(sender, "sendname", ""),
            getattr(sender, "send_name", ""),
            getattr(sender, "name", ""),
            getattr(sender, "display_name", ""),
            getattr(sender, "nickname", ""),
            raw_message.get("sender_name"),
            raw_message.get("sendname"),
            raw_message.get("send_name"),
            _nested(raw_sender, "sender_name"),
            _nested(raw_sender, "sendname"),
            _nested(raw_sender, "send_name"),
            _nested(raw_sender, "name"),
            _nested(raw_sender, "display_name"),
            _nested(raw_sender, "user", "name"),
            _nested(raw_sender, "user", "sendname"),
            _nested(raw_sender, "user", "send_name"),
        ),
        "chat_name": _first_string(
            getattr(message, "chat_name", ""),
            raw_message.get("chat_name"),
            _nested(raw_chat, "name"),
            _nested(raw_chat, "chat_name"),
        ),
    }


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, name="aha-feishu-assistant", daemon=True)
        _worker.start()


def enqueue_message(root: Path, default_run_id: str, channel: Any, message: Any) -> None:
    _ensure_worker()
    try:
        payload = _plain_message(root, message)
        payload["kind"] = "message"
        _assistant_queue.put_nowait((root, default_run_id, channel, payload))
        audit_feishu_channel(
            root,
            direction="inbound",
            kind="message",
            status="queued",
            transport="channel_ws",
            message_id=str(payload.get("message_id") or ""),
            chat_id=str(payload.get("chat_id") or ""),
            open_id=str(payload.get("open_id") or ""),
            content=str(payload.get("text") or ""),
        )
    except queue.Full:
        chat_id = str(getattr(message, "chat_id", "") or "")
        audit_feishu_channel(
            root,
            direction="inbound",
            kind="message",
            status="dropped",
            transport="channel_ws",
            chat_id=chat_id,
            reason="assistant_queue_full",
        )
        _send_text_background(root, channel, chat_id, "AHA 助手当前繁忙，请稍后重试。")


def _mapping_attr(value: object, name: str) -> dict:
    raw = getattr(value, name, None)
    return dict(raw) if isinstance(raw, dict) else {}


def _card_action_form_values_from_event(event: Any, action: Any) -> dict:
    for source in (action, event):
        for name in ("form_value", "form_values", "formValue", "input_values", "inputs"):
            values = _mapping_attr(source, name)
            if values:
                return values
    return {}


def enqueue_card_action(root: Path, default_run_id: str, channel: Any, event: Any) -> None:
    _ensure_worker()
    action = getattr(event, "action", None)
    operator = getattr(event, "operator", None)
    form_values = _card_action_form_values_from_event(event, action)
    payload = {
        "kind": "card_action",
        "chat_id": str(getattr(event, "chat_id", "") or ""),
        "message_id": str(getattr(event, "message_id", "") or ""),
        "open_id": str(getattr(operator, "open_id", "") or ""),
        "action": getattr(action, "value", None),
    }
    if form_values:
        payload["form_values"] = form_values
    try:
        _assistant_queue.put_nowait((root, default_run_id, channel, payload))
        action_value = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        audit_feishu_channel(
            root,
            direction="inbound",
            kind="card_action",
            status="queued",
            transport="channel_ws",
            message_id=payload["message_id"],
            chat_id=payload["chat_id"],
            open_id=payload["open_id"],
            decision=str(action_value.get("decision") or ""),
        )
    except queue.Full:
        audit_feishu_channel(
            root,
            direction="inbound",
            kind="card_action",
            status="dropped",
            transport="channel_ws",
            message_id=payload["message_id"],
            chat_id=payload["chat_id"],
            open_id=payload["open_id"],
            reason="assistant_queue_full",
        )
        _send_text_background(root, channel, payload["chat_id"], "AHA 助手当前繁忙，请稍后重试。")


def _event_mapping(value: object) -> dict:
    if isinstance(value, dict):
        return value
    result: dict = {}
    for name in (
        "schema",
        "header",
        "event",
        "type",
        "event_type",
        "event_key",
        "operator",
        "open_id",
        "chat_id",
        "open_chat_id",
        "tenant_key",
        "event_id",
    ):
        if hasattr(value, name):
            result[name] = getattr(value, name)
    return result


def _nested(mapping: dict, *keys: str) -> object:
    current: object = mapping
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
        if current is None:
            return None
    return current


def _first_string(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value not in (None, "") and not isinstance(value, (dict, list, tuple)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _plain_menu_event(event: Any) -> dict:
    raw = _event_mapping(event)
    header = _event_mapping(raw.get("header"))
    body = _event_mapping(raw.get("event"))
    event_type = _first_string(
        _nested(header, "event_type"),
        raw.get("event_type"),
        raw.get("type"),
        _nested(body, "event_type"),
    )
    if event_type != FEISHU_BOT_MENU_EVENT_TYPE:
        return {}
    operator = _event_mapping(body.get("operator") or raw.get("operator"))
    operator_id = _event_mapping(
        operator.get("operator_id")
        or operator.get("user_id")
        or body.get("operator_id")
        or body.get("user_id")
    )
    event_key = _first_string(
        body.get("event_key"),
        body.get("menu_key"),
        body.get("key"),
        raw.get("event_key"),
        raw.get("menu_key"),
    )
    open_id = _first_string(
        operator_id.get("open_id"),
        operator.get("open_id"),
        body.get("open_id"),
        raw.get("open_id"),
    )
    return {
        "kind": "menu_action",
        "event_type": event_type,
        "event_key": event_key,
        "tenant_key": _first_string(
            operator.get("tenant_key"),
            body.get("tenant_key"),
            header.get("tenant_key"),
            raw.get("tenant_key"),
        ),
        "open_id": open_id,
        "operator_name": _first_string(
            operator.get("name"),
            operator.get("display_name"),
            operator.get("user_name"),
            _nested(operator, "user", "name"),
        ),
        "chat_id": _first_string(
            body.get("chat_id"),
            body.get("open_chat_id"),
            _nested(body, "chat", "chat_id"),
            raw.get("chat_id"),
            raw.get("open_chat_id"),
        ),
        "message_id": _first_string(header.get("event_id"), body.get("event_id"), raw.get("event_id")),
        "raw": raw,
    }


def enqueue_raw_event(root: Path, default_run_id: str, channel: Any, event: Any) -> None:
    payload = _plain_menu_event(event)
    if not payload:
        return
    _ensure_worker()
    try:
        _assistant_queue.put_nowait((root, default_run_id, channel, payload))
        audit_feishu_channel(
            root,
            direction="inbound",
            kind="menu_action",
            status="queued",
            transport="channel_ws",
            message_id=str(payload.get("message_id") or ""),
            chat_id=str(payload.get("chat_id") or ""),
            open_id=str(payload.get("open_id") or ""),
            decision=str(payload.get("event_key") or ""),
        )
    except queue.Full:
        audit_feishu_channel(
            root,
            direction="inbound",
            kind="menu_action",
            status="dropped",
            transport="channel_ws",
            message_id=str(payload.get("message_id") or ""),
            chat_id=str(payload.get("chat_id") or ""),
            open_id=str(payload.get("open_id") or ""),
            reason="assistant_queue_full",
        )
        _send_text_background(root, channel, str(payload.get("chat_id") or ""), "AHA 助手当前繁忙，请稍后重试。")


def _worker_loop() -> None:
    while True:
        item = _assistant_queue.get()
        try:
            if item is None:
                return
            root, default_run_id, channel, payload = item
            if payload.get("kind") == "card_action":
                _handle_card_action(root, channel, payload)
            elif payload.get("kind") == "menu_action":
                _handle_menu_action(root, default_run_id, channel, payload)
            else:
                _handle_message(root, default_run_id, channel, payload)
        except Exception as exc:  # noqa: BLE001 - one bad event must not stop the assistant worker.
            print(f"[aha feishu] assistant message failed: {exc!r}", file=sys.stderr, flush=True)
            audit_feishu_channel(
                item[0],
                direction="inbound",
                kind=str(item[3].get("kind") or "message"),
                status="failed",
                transport="channel_ws",
                message_id=str(item[3].get("message_id") or ""),
                chat_id=str(item[3].get("chat_id") or ""),
                open_id=str(item[3].get("open_id") or ""),
                error=exc,
            )
            try:
                _send_text(item[0], item[2], str(item[3].get("chat_id") or ""), "AHA 助手处理失败，请稍后重试。")
            except Exception:  # noqa: BLE001
                pass
        finally:
            _assistant_queue.task_done()


def _send(root: Path, channel: Any, chat_id: str, message: object, opts: dict | None = None) -> dict:
    if not chat_id:
        raise FeishuError("飞书消息缺少 chat_id")
    if isinstance(message, dict) and isinstance(message.get("card"), dict):
        message = {**message, "card": sanitize_card_payload(message.get("card") or {})}
    kind = "card" if isinstance(message, dict) and isinstance(message.get("card"), dict) else "message"
    try:
        result = channel.schedule(channel.send(chat_id, message, opts)).result(timeout=20)
        if hasattr(result, "success") and not result.success:
            raise FeishuError(str(getattr(result, "error", None) or "飞书消息发送失败"))
    except Exception as exc:  # noqa: BLE001 - SDK futures may raise transport-specific exceptions.
        audit_feishu_channel(
            root,
            direction="outbound",
            kind=kind,
            status="failed",
            transport="channel_ws",
            chat_id=chat_id,
            content=message,
            error=exc,
        )
        raise
    message_id = getattr(result, "message_id", None)
    audit_feishu_channel(
        root,
        direction="outbound",
        kind=kind,
        status="sent",
        transport="channel_ws",
        message_id=str(message_id or ""),
        chat_id=chat_id,
        content=message,
    )
    return {"ok": True, "message_id": message_id}


def _send_text(root: Path, channel: Any, chat_id: str, text: str, *, reply_to: str = "") -> dict:
    opts = {"reply_to": reply_to} if reply_to else None
    return _send(root, channel, chat_id, {"text": str(text)}, opts)


def _send_text_background(root: Path, channel: Any, chat_id: str, text: str) -> None:
    if not chat_id:
        return
    try:
        channel.schedule(channel.send(chat_id, {"text": str(text)}, None))
        audit_feishu_channel(
            root,
            direction="outbound",
            kind="message",
            status="scheduled",
            transport="channel_ws",
            chat_id=chat_id,
            content=text,
        )
    except Exception:  # noqa: BLE001 - the SDK callback must return without blocking.
        audit_feishu_channel(
            root,
            direction="outbound",
            kind="message",
            status="failed",
            transport="channel_ws",
            chat_id=chat_id,
            content=text,
            reason="background_schedule_failed",
        )
        pass


def _refresh_identity_profiles_background(root: Path, config: dict, payload: dict) -> None:
    app_id, app_secret = feishu_credentials(config)
    if not app_id or not app_secret:
        return
    open_id = str(payload.get("open_id") or "").strip()
    chat_id = str(payload.get("chat_id") or "").strip()
    chat_type = str(payload.get("chat_type") or "").lower()
    open_ids = [open_id] if open_id else []
    chat_ids = [chat_id] if chat_type == "group" and chat_id else []
    if not open_ids and not chat_ids:
        return

    def refresh() -> None:
        try:
            refresh_identity_profiles(
                root,
                app_id,
                app_secret,
                open_ids=open_ids,
                chat_ids=chat_ids,
                max_items=2,
            )
        except Exception as exc:  # noqa: BLE001 - profile names are display-only.
            print(f"Feishu identity profile refresh failed: {exc}", file=sys.stderr)

    threading.Thread(target=refresh, name="aha-feishu-identity-refresh", daemon=True).start()


def _update_confirmation_card(root: Path, channel: Any, confirmation: dict) -> None:
    message_id = str(confirmation.get("confirmation_message_id") or confirmation.get("message_id") or "")
    card = confirmation.get("confirmation_card") or confirmation.get("terminal_card")
    if not message_id or not isinstance(card, dict):
        return
    card = sanitize_card_payload(card)
    try:
        result = channel.schedule(channel.update_card(message_id, card)).result(timeout=20)
        if hasattr(result, "success") and not result.success:
            raise FeishuError(str(getattr(result, "error", None) or "飞书卡片更新失败"))
    except Exception as exc:  # noqa: BLE001 - preserve SDK exception after recording its sanitized summary.
        audit_feishu_channel(
            root,
            direction="outbound",
            kind="card_update",
            status="failed",
            transport="channel_ws",
            message_id=message_id,
            content={"card": card},
            error=exc,
        )
        raise
    audit_feishu_channel(
        root,
        direction="outbound",
        kind="card_update",
        status="updated",
        transport="channel_ws",
        message_id=message_id,
        content={"card": card},
    )
    mark_confirmation_card_updated(root, str(confirmation.get("confirmation_id") or ""))


def _authorization_error(config: dict, *, chat_type: str, chat_id: str, open_id: str) -> str:
    allowed_users = {str(item) for item in config.get("allowed_open_ids") or []}
    kind = str(chat_type or "").strip().lower()
    if kind == "p2p":
        return "" if open_id and open_id in allowed_users else "user_not_allowed"
    if kind != "group":
        return "unsupported_chat_type"
    allowed_chats = {str(item) for item in config.get("allowed_chat_ids") or []}
    if not chat_id or chat_id not in allowed_chats:
        return "chat_not_allowed"
    if str(config.get("group_access_mode") or "allowed_users") == "all_members":
        return "" if open_id else "user_identity_missing"
    return "" if open_id and open_id in allowed_users else "user_not_allowed"


def _unauthorized_message(chat_type: str, open_id: str, reason: str) -> str:
    if str(chat_type or "").lower() != "p2p" and reason == "chat_not_allowed":
        return (
            "该群尚未被授权访问此 AHA。请管理员在 AHA 飞书助手页面的“最近检测群组”中加入该群，"
            "或将 chat_id 添加到 allowed_chat_ids。"
        )
    base = "你尚未被授权访问此 AHA。请管理员把你的 open_id 加入 integrations.feishu.allowed_open_ids。"
    if str(chat_type or "").lower() != "p2p":
        return f"{base}\n为避免在群聊公开用户标识，请私聊机器人发送任意消息获取你的 open_id。"
    detected = str(open_id or "").strip()
    if not detected:
        return f"{base}\n本次消息未能识别 open_id，请联系管理员检查飞书事件权限。"
    return f"{base}\n本次消息检测到的 open_id：{detected}"


def _session_key(payload: dict) -> str:
    return make_session_key(
        tenant_key=str(payload.get("tenant_key") or "local"),
        open_id=str(payload.get("open_id") or ""),
        chat_id=str(payload.get("chat_id") or ""),
        chat_type=str(payload.get("chat_type") or ""),
    )


def _audit_inbound_resolution(
    root: Path,
    payload: dict,
    status: str,
    *,
    reason: str = "",
    session_key: str = "",
    run_id: str = "",
    task_id: str = "",
    error: object = None,
) -> None:
    kind = str(payload.get("kind") or "message")
    action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
    audit_feishu_channel(
        root,
        direction="inbound",
        kind=kind,
        status=status,
        transport="channel_ws",
        message_id=str(payload.get("message_id") or ""),
        chat_id=str(payload.get("chat_id") or ""),
        open_id=str(payload.get("open_id") or ""),
        session_key=session_key,
        run_id=run_id,
        task_id=task_id,
        content=str(payload.get("text") or "") if kind == "message" else None,
        error=error,
        reason=reason,
        decision=str(action.get("decision") or ""),
    )


def _dedicated_run(root: Path) -> str:
    return ensure_service_assistant_run(root, _assistant_agent_defaults(root))


def _assistant_backend(root: Path, config: dict | None = None) -> tuple[str, str | None]:
    defaults = _assistant_agent_defaults(root, config)
    return str(defaults["backend"]), defaults["model"]


def _assistant_agent_defaults(root: Path, config: dict | None = None) -> dict[str, object]:
    global_config = config if isinstance(config, dict) else load_config(root)
    integration = feishu_config(root)
    backend = str(integration.get("backend") or global_config.get("backend") or "codex")
    backend_config = global_config.get(backend) if isinstance(global_config.get(backend), dict) else {}
    model = str(integration.get("model") or backend_config.get("model") or "").strip() or None
    reasoning_effort = str(integration.get("reasoning_effort") or backend_config.get("reasoning_effort") or "").strip() or None
    backend_proxy = backend_config.get("proxy") if isinstance(backend_config.get("proxy"), dict) else {}
    configured_proxy_enabled = integration.get("proxy_enabled")
    proxy_enabled = (
        bool(configured_proxy_enabled)
        if isinstance(configured_proxy_enabled, bool)
        else bool(backend_proxy.get("enabled"))
    )
    return {
        "backend": backend,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "proxy_enabled": proxy_enabled,
    }


def _binding(root: Path, session_key: str, open_id: str, server_default_run_id: str) -> dict:
    del server_default_run_id  # Kept in the callback signature for SDK compatibility.
    run_id = _dedicated_run(root)
    current = get_session_binding(root, session_key)
    if current is not None and str(current.get("active_run_id") or "") == run_id:
        return current
    return set_session_binding(
        root,
        session_key,
        active_run_id=run_id or None,
        active_task_id=None,
        acl_subject=open_id,
    )


def _task_workspace(root: Path, run_id: str) -> str:
    del run_id
    return str(aha_home_path(root).resolve())


def _assistant_task_title(session_key: str) -> str:
    return session_task_title(session_key)


def _open_id_display_name(root: Path, open_id: str, *fallbacks: object) -> str:
    for value in fallbacks:
        name = _first_string(value)
        if name:
            return name
    user_identity = str(open_id or "").strip()
    if not user_identity:
        return ""
    try:
        items = identity_label_items(root, kind="open_id", identities=[user_identity])
    except Exception:  # noqa: BLE001 - display names must never block routing.
        return ""
    if not items:
        return ""
    return _first_string(items[0].get("display_name"))


def _feishu_group_run(root: Path) -> str:
    return ensure_feishu_group_run(root, _assistant_agent_defaults(root))


def _active_task(root: Path, run_id: str, task_id: str) -> dict | None:
    if not run_id or not task_id:
        return None
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except (KeyError, SystemExit):
        return None
    workspace = str(task.get("workspace_path") or "").strip()
    if workspace and not Path(workspace).is_dir():
        return None
    if not is_service_assistant_task(task) or Path(workspace).resolve() != aha_home_path(root).resolve():
        return None
    return None if str(task.get("status") or "") in TERMINAL_TASK_STATUSES else task


def _active_group_task(root: Path, run_id: str, task_id: str) -> dict | None:
    if not run_id or not task_id:
        return None
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except (KeyError, SystemExit):
        return None
    workspace = str(task.get("workspace_path") or "").strip()
    if workspace and not Path(workspace).is_dir():
        return None
    if not is_feishu_group_task(task) or Path(workspace).resolve() != feishu_group_state_dir(root).resolve():
        return None
    return None if str(task.get("status") or "") in TERMINAL_TASK_STATUSES else task


def _ensure_agent_task(
    root: Path,
    run_id: str,
    session_key: str,
    open_id: str,
    binding: dict,
    *,
    display_name: str = "",
) -> dict:
    active = _active_task(root, run_id, str(binding.get("active_task_id") or ""))
    if active is not None:
        if display_name:
            return ensure_service_assistant_task(
                root,
                run_id,
                session_key,
                _assistant_agent_defaults(root),
                display_name=display_name,
            )
        return active
    task = ensure_service_assistant_task(
        root,
        run_id,
        session_key,
        _assistant_agent_defaults(root),
        display_name=display_name,
    )
    set_session_binding(
        root,
        session_key,
        active_run_id=run_id,
        active_task_id=str(task.get("id") or ""),
        acl_subject=open_id,
    )
    return task


def _ensure_group_agent_task(
    root: Path,
    run_id: str,
    session_key: str,
    open_id: str,
    binding: dict,
    *,
    display_name: str = "",
) -> dict:
    active = _active_group_task(root, run_id, str(binding.get("active_task_id") or ""))
    if active is not None:
        if display_name:
            return ensure_feishu_group_task(
                root,
                run_id,
                session_key,
                _assistant_agent_defaults(root),
                display_name=display_name,
            )
        return active
    task = ensure_feishu_group_task(
        root,
        run_id,
        session_key,
        _assistant_agent_defaults(root),
        display_name=display_name,
    )
    set_session_binding(
        root,
        session_key,
        active_run_id=run_id,
        active_task_id=str(task.get("id") or ""),
        acl_subject=open_id,
    )
    return task


def _never_handle_command(_root: Path, _run_id: str, _payload: dict, _message: str, _task_id: str | None) -> tuple[bool, None, dict]:
    """Keep Feishu text as agent input, including text that starts with '/'."""
    return False, None, {}


def _handle_group_mention(root: Path, channel: Any, payload: dict, *, text: str) -> None:
    chat_id = str(payload.get("chat_id") or "")
    message_id = str(payload.get("message_id") or "")
    open_id = str(payload.get("open_id") or "")
    try:
        session_key = feishu_group_user_session_key(
            tenant_key=str(payload.get("tenant_key") or "local"),
            open_id=open_id,
        )
        run_id = _feishu_group_run(root)
        binding = get_session_binding(root, session_key)
        if binding is None or str(binding.get("active_run_id") or "") != run_id:
            binding = set_session_binding(
                root,
                session_key,
                active_run_id=run_id,
                active_task_id=None,
                acl_subject=open_id,
        )
        display_name = _open_id_display_name(root, open_id, payload.get("sender_name"))
        task = _ensure_group_agent_task(root, run_id, session_key, open_id, binding, display_name=display_name)
        task_id = str(task.get("id") or "")
        mark_feishu_group_task_interaction(root, run_id, task_id)
        handle_send_payload(
            root,
            run_id,
            {
                "task_id": task_id,
                "target": "main",
                "sender": "feishu",
                "reply_target": "feishu",
                "message": group_agent_message(payload, text),
                "feishu_chat_id": chat_id,
                "feishu_reply_to": message_id,
                "feishu_mention_open_id": open_id,
                "feishu_channel": "group_digital_human",
                "feishu_tenant_key": str(payload.get("tenant_key") or "local"),
                "feishu_chat_type": "group",
                "feishu_message_id": message_id,
                "feishu_session_key": session_key,
                "feishu_original_text": text,
                "feishu_attachments": payload.get("attachments") if isinstance(payload.get("attachments"), list) else [],
            },
            command_handler=_never_handle_command,
            background_backend_start=True,
        )
        _audit_inbound_resolution(
            root,
            payload,
            "accepted",
            session_key=session_key,
            run_id=run_id,
            task_id=task_id,
        )
    except (FeishuError, KeyError, SystemExit, ValueError) as exc:
        _audit_inbound_resolution(root, payload, "failed", error=exc)
        _send_text(root, channel, chat_id, "飞书群聊数字人处理失败，请稍后重试。", reply_to=message_id)


def _confirmation_subscription(root: Path, chat_id: str, open_id: str) -> tuple[str, dict]:
    state = load_subscription_state(root)
    candidates = [
        (str(session_key), subscription)
        for session_key, subscription in state.get("subscriptions", {}).items()
        if isinstance(subscription, dict)
        and subscription.get("enabled")
        and str(subscription.get("chat_id") or "") == chat_id
    ]
    exact = [item for item in candidates if str(item[1].get("open_id") or "") == open_id]
    selected = exact if exact else candidates
    if len(selected) != 1:
        raise ServiceAssistantActionError("无法唯一定位该卡片对应的 AHA 会话")
    return selected[0]


def _confirmation_subscription_for_card(root: Path, chat_id: str, open_id: str, message_id: str) -> tuple[str, dict]:
    record = confirmation_card_for_message(root, message_id)
    if isinstance(record, dict):
        expected_open_id = str(record.get("open_id") or "").strip()
        if expected_open_id and expected_open_id != str(open_id or "").strip():
            raise ServiceAssistantActionError("确认卡片身份不匹配")
        expected_chat_id = str(record.get("chat_id") or "").strip()
        if expected_chat_id and expected_chat_id != str(chat_id or "").strip():
            raise ServiceAssistantActionError("确认卡片会话不匹配")
        session_key = str(record.get("session_key") or "").strip()
        if not session_key:
            raise ServiceAssistantActionError("确认卡片缺少会话绑定")
        subscription = dict(load_subscription_state(root).get("subscriptions", {}).get(session_key) or {})
        binding = get_session_binding(root, session_key) or {}
        if not subscription:
            subscription = {
                "enabled": True,
                "chat_id": expected_chat_id or str(chat_id or "").strip(),
                "open_id": expected_open_id or str(open_id or "").strip(),
                "chat_type": "group" if ":group:" in session_key else "p2p",
            }
        if not subscription.get("run_id") and binding.get("active_run_id"):
            subscription["run_id"] = binding.get("active_run_id")
        if not subscription.get("task_id") and binding.get("active_task_id"):
            subscription["task_id"] = binding.get("active_task_id")
        return session_key, subscription
    return _confirmation_subscription(root, chat_id, open_id)


def _direct_followup_confirmation(root: Path, channel: Any, chat_id: str, confirmation: dict) -> bool:
    result = confirmation.get("result") if isinstance(confirmation.get("result"), dict) else {}
    target_operation = str(result.get("target_operation") or "").strip()
    next_arguments = result.get("next_arguments") if isinstance(result.get("next_arguments"), dict) else {}
    if target_operation not in {"create_memo", "create_task", "dismiss_feishu_group_handoff"} or not next_arguments:
        return False
    run_id = str(confirmation.get("assistant_run_id") or "").strip()
    task_id = str(confirmation.get("assistant_task_id") or "").strip()
    if not run_id or not task_id:
        return False
    task = task_snapshot(root, run_id, task_id)["task"]
    action = prepare_service_assistant_action(
        root,
        run_id,
        task,
        {"operation": target_operation, "arguments": next_arguments},
    )
    if action.get("confirmation_card"):
        _send_menu_card(root, channel, chat_id, action)
        return True
    response = str(action.get("user_response") or "").strip()
    if response:
        _send_text(root, channel, chat_id, response)
        return True
    return False


def _direct_write_response(confirmation: dict) -> str:
    operation = str(confirmation.get("operation") or "")
    result = confirmation.get("result") if isinstance(confirmation.get("result"), dict) else {}
    if operation not in {
        "create_memo",
        "create_task",
        "dismiss_feishu_group_handoff",
        "send_feishu_group_reply",
        "handle_feishu_group_handoff",
    }:
        return ""
    if not bool(result.get("ok")):
        return f"操作失败：{result.get('error') or result}"
    if operation == "create_memo":
        memo = result.get("memo") if isinstance(result.get("memo"), dict) else {}
        lines = [
            "已创建 Memo。",
            f"memo_id：{memo.get('id') or '-'}",
            f"标题：{memo.get('title') or '-'}",
        ]
        ack = result.get("group_handoff_ack") if isinstance(result.get("group_handoff_ack"), dict) else {}
        if ack.get("sent"):
            lines.append("已回群告知加入待办。")
        elif ack.get("error"):
            lines.append(f"回群告知失败：{ack.get('error')}")
        return "\n".join(lines)
    if operation == "create_task":
        task = result.get("task") if isinstance(result.get("task"), dict) else {}
        lines = [
            "已创建 Task。",
            f"task_id：{task.get('id') or '-'}",
            f"标题：{task.get('title') or '-'}",
        ]
        memo = result.get("memo") if isinstance(result.get("memo"), dict) else {}
        if memo.get("id"):
            lines.append(f"已关联 Memo：{memo.get('id')}")
        return "\n".join(lines)
    if operation == "dismiss_feishu_group_handoff":
        return "\n".join(
            [
                "已关闭数字人转单。",
                f"handoff_id：{result.get('handoff_id') or '-'}",
                f"终态：{result.get('status_label') or result.get('status') or '-'}",
            ]
        )
    if operation == "send_feishu_group_reply":
        return "\n".join(
            [
                "已由数字人代发群聊回复。",
                f"handoff_id：{result.get('handoff_id') or '-'}",
                f"message_id：{result.get('message_id') or '-'}",
            ]
        )
    if operation == "handle_feishu_group_handoff":
        if str(result.get("selected_action") or "") == "dismissed":
            return "\n".join(
                [
                    "已标记为无需处理。",
                    f"handoff_id：{result.get('handoff_id') or '-'}",
                    "不会回群。",
                ]
            )
    return ""


def _finish_confirmation(
    root: Path,
    channel: Any,
    *,
    chat_id: str,
    message_id: str,
    run_id: str,
    task_id: str,
    confirmation: dict,
) -> None:
    try:
        _update_confirmation_card(root, channel, confirmation)
    except (FeishuError, RuntimeError, TimeoutError):
        # Runtime sweep retries terminal card updates; execution state must not
        # be rolled back merely because the visual update failed.
        pass
    if confirmation.get("cancelled"):
        _send_text(root, channel, chat_id, str(confirmation.get("user_response") or "已取消。"), reply_to=message_id)
        return
    if confirmation.get("choice") and _direct_followup_confirmation(root, channel, chat_id, confirmation):
        _send_text(root, channel, chat_id, "已收到选择，请继续确认最终操作。", reply_to=message_id)
        return
    direct_response = _direct_write_response(confirmation)
    if direct_response:
        _send_text(root, channel, chat_id, direct_response, reply_to=message_id)
        return
    handle_send_payload(
        root,
        run_id,
        {
            "task_id": task_id,
            "target": "main",
            "sender": "aha",
            "reply_target": "feishu",
            "message": str(confirmation.get("tool_message") or ""),
            "service_action_depth": 1,
        },
        command_handler=_never_handle_command,
        background_backend_start=True,
    )
    reply = "已收到选择，AHA 助手正在继续处理。" if confirmation.get("choice") else "操作已确认并执行，AHA 助手正在整理结果。"
    _send_text(root, channel, chat_id, reply, reply_to=message_id)


def _card_action_form_values(payload: dict) -> dict:
    for source in (payload, payload.get("action") if isinstance(payload.get("action"), dict) else {}):
        if not isinstance(source, dict):
            continue
        for key in ("form_values", "form_value", "formValue", "input_values", "inputs"):
            values = source.get(key)
            if isinstance(values, dict) and values:
                return values
    return {}


def _handle_menu_query_card_action(root: Path, channel: Any, payload: dict, value: dict) -> None:
    chat_id = str(payload.get("chat_id") or "")
    message_id = str(payload.get("message_id") or "")
    open_id = str(payload.get("open_id") or "")
    choice_id = str(value.get("choice_id") or "").strip()
    if choice_id not in {MENU_QUERY_SUBMIT_CHOICE_ID, "__cancel__"}:
        _audit_inbound_resolution(root, payload, "rejected", reason="invalid_menu_query_choice")
        _send_text(root, channel, chat_id, "无法处理查询卡片：操作数据不完整。", reply_to=message_id)
        return
    try:
        session_key, subscription = _confirmation_subscription_for_card(root, chat_id, open_id, message_id)
        authorization_error = _authorization_error(
            feishu_config(root),
            chat_type=str(subscription.get("chat_type") or "p2p"),
            chat_id=chat_id,
            open_id=open_id,
        )
        if authorization_error:
            _audit_inbound_resolution(root, payload, "rejected", reason=authorization_error, session_key=session_key)
            _send_text(root, channel, chat_id, "你尚未被授权执行该 AHA 操作。", reply_to=message_id)
            return
        context = consume_confirmation_card(
            root,
            message_id=message_id,
            open_id=open_id,
            session_key=session_key,
            action=MENU_QUERY_ACTION,
            decision=choice_id,
        )
        confirmation_id = str(context.get("confirmation_id") or "")
        if choice_id == "__cancel__":
            record = finalize_confirmation_card(root, confirmation_id, "cancelled")
            if isinstance((record or {}).get("terminal_card"), dict):
                _update_confirmation_card(root, channel, record or {})
            _send_text(root, channel, chat_id, "已取消本次查询。", reply_to=message_id)
            _audit_inbound_resolution(root, payload, "handled", reason="menu_query_cancelled", session_key=session_key)
            return
        operation = str(context.get("operation") or "")
        fields = context.get("fields") if isinstance(context.get("fields"), dict) else {}
        arguments = _menu_query_arguments(operation, fields, _card_action_form_values(payload))
        record = finalize_confirmation_card(root, confirmation_id, "selected", "已提交查询条件")
        if isinstance((record or {}).get("terminal_card"), dict):
            _update_confirmation_card(root, channel, record or {})
        _send(root, channel, chat_id, {"card": _menu_list_card(root, operation, arguments)})
        _audit_inbound_resolution(
            root,
            payload,
            "handled",
            reason="menu_query_submitted",
            session_key=session_key,
            run_id=str(arguments.get("run_id") or ""),
        )
    except (FeishuError, ServiceAssistantActionError, KeyError, SystemExit, ValueError) as exc:
        _audit_inbound_resolution(root, payload, "failed", error=exc)
        record = confirmation_card_for_message(root, message_id)
        if record is not None and isinstance(record.get("terminal_card"), dict):
            try:
                _update_confirmation_card(root, channel, record)
            except (FeishuError, RuntimeError, TimeoutError):
                pass
        _send_text(root, channel, chat_id, f"无法处理查询：{exc}", reply_to=message_id)


def _handle_card_action(root: Path, channel: Any, payload: dict) -> None:
    value = payload.get("action") if isinstance(payload.get("action"), dict) else {}
    action_kind = str(value.get("kind") or "")
    if action_kind == "aha_menu_query":
        _handle_menu_query_card_action(root, channel, payload, value)
        return
    if action_kind not in {"aha_service_confirmation", "aha_service_choice"}:
        _audit_inbound_resolution(root, payload, "ignored", reason="unsupported_card_action")
        return
    chat_id = str(payload.get("chat_id") or "")
    message_id = str(payload.get("message_id") or "")
    open_id = str(payload.get("open_id") or "")
    decision = {"confirm": "确认", "cancel": "取消"}.get(str(value.get("decision") or "").lower())
    choice_id = str(value.get("choice_id") or "").strip()
    if action_kind == "aha_service_confirmation" and not decision:
        _audit_inbound_resolution(root, payload, "rejected", reason="invalid_decision")
        _send_text(root, channel, chat_id, "无法处理卡片操作：确认数据不完整。", reply_to=message_id)
        return
    if action_kind == "aha_service_choice" and not choice_id:
        _audit_inbound_resolution(root, payload, "rejected", reason="invalid_choice")
        _send_text(root, channel, chat_id, "无法处理卡片操作：选择数据不完整。", reply_to=message_id)
        return
    try:
        session_key, subscription = _confirmation_subscription_for_card(root, chat_id, open_id, message_id)
        chat_type = str(subscription.get("chat_type") or ("group" if ":group:" in session_key else "p2p"))
        authorization_error = _authorization_error(
            feishu_config(root),
            chat_type=chat_type,
            chat_id=chat_id,
            open_id=open_id,
        )
        if authorization_error:
            _audit_inbound_resolution(root, payload, "rejected", reason=authorization_error, session_key=session_key)
            _send_text(root, channel, chat_id, "你尚未被授权执行该 AHA 操作。", reply_to=message_id)
            return
        if action_kind == "aha_service_choice":
            choice_kwargs = {
                "open_id": open_id,
                "session_key": session_key,
                "message_id": message_id,
                "choice_id": choice_id,
            }
            form_values = _card_action_form_values(payload)
            if form_values:
                choice_kwargs["form_values"] = form_values
            confirmation = resolve_choice(root, **choice_kwargs)
        else:
            confirmation = resolve_confirmation(
                root,
                open_id=open_id,
                session_key=session_key,
                text=decision,
                message_id=message_id,
            )
        if confirmation is None:
            raise ServiceAssistantActionError("卡片确认数据无效")
        _finish_confirmation(
            root,
            channel,
            chat_id=chat_id,
            message_id=message_id,
            run_id=str(confirmation.get("assistant_run_id") or subscription.get("run_id") or ""),
            task_id=str(confirmation.get("assistant_task_id") or subscription.get("task_id") or ""),
            confirmation=confirmation,
        )
        _audit_inbound_resolution(
            root,
            payload,
            "handled",
            session_key=session_key,
            run_id=str(confirmation.get("assistant_run_id") or subscription.get("run_id") or ""),
            task_id=str(confirmation.get("assistant_task_id") or subscription.get("task_id") or ""),
        )
    except (FeishuError, ServiceAssistantActionError, KeyError, SystemExit, ValueError) as exc:
        _audit_inbound_resolution(root, payload, "failed", error=exc)
        record = confirmation_card_for_message(root, message_id)
        if record is not None and isinstance(record.get("terminal_card"), dict):
            try:
                _update_confirmation_card(root, channel, record)
            except (FeishuError, RuntimeError, TimeoutError):
                pass
        _send_text(root, channel, chat_id, f"无法处理确认：{exc}", reply_to=message_id)


def _owner_menu_operation(event_key: object) -> str:
    return OWNER_MENU_ACTIONS.get(str(event_key or "").strip(), "")


def _owner_menu_arguments(root: Path, operation: str) -> dict:
    work_run_id = resolve_feishu_work_run_id(root)
    if operation == "create_memo":
        return {
            "run_id": work_run_id,
            "title": "未命名 Memo",
            "description": "",
        }
    if operation == "create_task":
        return {
            "run_id": work_run_id,
            "title": "未命名 Task",
            "description": "",
        }
    if operation in {"list_memos", "list_tasks"}:
        return {"run_id": work_run_id, "limit": 10}
    raise ServiceAssistantActionError(f"不支持的飞书菜单操作：{operation or '-'}")


def _owner_menu_private_context(
    root: Path,
    payload: dict,
) -> tuple[str, str, str, str]:
    config = feishu_config(root)
    raw_tenant_key = str(payload.get("tenant_key") or "").strip()
    tenant_key = raw_tenant_key or "local"
    open_id = str(payload.get("open_id") or "").strip()
    owner = resolve_feishu_owner(root, tenant_key=tenant_key, config=config)
    configured_owner = str(config.get("owner_open_id") or "").strip()
    if open_id and (not raw_tenant_key or not str(owner.get("chat_id") or "").strip()):
        owner_by_open_id = resolve_feishu_owner_by_open_id(root, open_id=open_id, config=config)
        if str(owner_by_open_id.get("open_id") or "").strip() == open_id:
            owner = {**owner, **owner_by_open_id}
            tenant_key = str(owner.get("tenant_key") or tenant_key).strip() or tenant_key
    expected_owner = str(configured_owner or owner.get("open_id") or "").strip()
    if not expected_owner:
        raise ServiceAssistantActionError("请先在飞书助手设置里配置唯一 owner_open_id")
    if open_id != expected_owner:
        raise ServiceAssistantActionError("飞书菜单只允许 owner 私聊使用")
    chat_id = str(payload.get("chat_id") or owner.get("chat_id") or config.get("owner_chat_id") or "").strip()
    if not chat_id:
        raise ServiceAssistantActionError("无法定位 owner 私聊，请先给飞书助手发送一条私聊消息")
    session_key = str(owner.get("session_key") or "").strip()
    if not session_key:
        session_key = make_session_key(tenant_key=tenant_key, open_id=open_id, chat_id=chat_id, chat_type="p2p")
    payload["tenant_key"] = tenant_key
    payload["chat_id"] = chat_id
    return tenant_key, open_id, chat_id, session_key


def _owner_menu_session(
    root: Path,
    server_default_run_id: str,
    payload: dict,
) -> tuple[str, str, str, dict]:
    tenant_key, open_id, chat_id, session_key = _owner_menu_private_context(root, payload)
    binding = _binding(root, session_key, open_id, server_default_run_id)
    run_id = str(binding.get("active_run_id") or "")
    if not run_id or not run_exists(root, run_id):
        raise ServiceAssistantActionError("AHA 尚无可用管家 Run，请先在 Web 中创建或启动飞书助手")
    display_name = _open_id_display_name(root, open_id, payload.get("operator_name"))
    task = _ensure_agent_task(root, run_id, session_key, open_id, binding, display_name=display_name)
    task_id = str(task.get("id") or "")
    set_subscription(
        root,
        session_key,
        chat_id=chat_id,
        open_id=open_id,
        run_id=run_id,
        task_id=task_id,
        chat_type="p2p",
    )
    remember_owner_private_chat(
        root,
        tenant_key=tenant_key,
        open_id=open_id,
        chat_id=chat_id,
        session_key=session_key,
    )
    return chat_id, run_id, task_id, task


def _record_owner_menu_private_chat(root: Path, payload: dict, *, chat_id: str) -> None:
    open_id = str(payload.get("open_id") or "")
    if not chat_id and not open_id:
        return
    record_recent_private_chat(
        root,
        chat_id=chat_id,
        open_id=open_id,
        display_name=str(payload.get("operator_name") or ""),
    )
    _refresh_identity_profiles_background(
        root,
        feishu_config(root),
        {**payload, "chat_id": chat_id, "chat_type": "p2p"},
    )


def _send_menu_card(root: Path, channel: Any, chat_id: str, action: dict) -> None:
    card = action.get("confirmation_card")
    if not isinstance(card, dict):
        return
    result = _send(root, channel, chat_id, {"card": card})
    message_id = str(result.get("message_id") or "")
    confirmation_id = str(action.get("confirmation_id") or "")
    if confirmation_id and message_id:
        bind_confirmation_card(root, confirmation_id, message_id=message_id, chat_id=chat_id)


def _menu_query_field_select(name: str, label: str, options: list[dict], default_value: object = "") -> dict:
    default = str(default_value or "").strip()
    rendered_options = []
    for option in options[:80]:
        value = str(option.get("value") or "").strip()
        option_label = str(option.get("label") or value).strip()
        if not value or not option_label:
            continue
        if default and value == default:
            option_label = f"{option_label}（默认）"
        rendered_options.append({"text": {"tag": "plain_text", "content": option_label[:120]}, "value": value})
    if not rendered_options:
        rendered_options.append({"text": {"tag": "plain_text", "content": "无可用选项"}, "value": ""})
    placeholder = label
    if default:
        selected = next((str(item.get("label") or "") for item in options if str(item.get("value") or "") == default), "")
        if selected:
            placeholder = f"{label}（默认：{selected[:60]}）"
    return {
        "tag": "select_static",
        "element_id": name,
        "name": name,
        "placeholder": {"tag": "plain_text", "content": placeholder[:120]},
        "options": rendered_options,
    }


def _menu_query_date_picker(name: str, label: str) -> dict:
    return {
        "tag": "date_picker",
        "element_id": name,
        "name": name,
        "placeholder": {"tag": "plain_text", "content": label},
    }


def _menu_query_button(label: str, choice_id: str, button_type: str, element_id: str, *, submit: bool = False) -> dict:
    payload = {
        "tag": "button",
        "element_id": element_id,
        "text": {"tag": "plain_text", "content": label},
        "type": button_type,
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "kind": "aha_menu_query",
                    "choice_id": choice_id,
                },
            }
        ],
    }
    if submit:
        payload["action_type"] = "form_submit"
        payload["form_action_type"] = "submit"
        payload["name"] = "form_submit"
    return payload


def _menu_query_run_options(root: Path, default_run_id: str) -> list[dict]:
    options: list[dict] = []
    seen: set[str] = set()

    def add(summary: dict) -> None:
        run_id = str(summary.get("id") or "").strip()
        if not run_id or run_id in seen:
            return
        seen.add(run_id)
        name = str(summary.get("goal") or "Run").strip() or "Run"
        options.append({"value": run_id, "label": f"{name}.{run_id}"[:120]})

    if default_run_id:
        try:
            add(require_plan(root, default_run_id) | {"id": default_run_id})
        except (KeyError, SystemExit, ValueError):
            pass
    for summary in feishu_work_run_options(root, limit=80):
        add(summary)
    if not options:
        raise ServiceAssistantActionError("没有可用于飞书查询的普通 Run，请先创建一个非系统 Run")
    return options


def _menu_query_status_options(operation: str) -> list[dict]:
    if operation == "list_memos":
        return [
            {"value": "all", "label": "全部"},
            {"value": "todo", "label": "待办"},
            {"value": "doing", "label": "进行中"},
            {"value": "done", "label": "已完成"},
            {"value": "closed", "label": "已关闭"},
        ]
    return [
        {"value": "all", "label": "全部"},
        {"value": "pending", "label": "pending"},
        {"value": "running", "label": "running"},
        {"value": "awaiting_user", "label": "awaiting_user"},
        {"value": "completed", "label": "completed"},
        {"value": "failed", "label": "failed"},
        {"value": "blocked", "label": "blocked"},
    ]


def _menu_query_limit_options() -> list[dict]:
    return [
        {"value": "10", "label": "10 条"},
        {"value": "20", "label": "20 条"},
    ]


def _menu_query_card(operation: str, fields: dict) -> dict:
    title = "查询 Memo" if operation == "list_memos" else "查询 Task"
    time_hint = "Memo 时间范围按开始日期筛选；没有开始日期时按创建日期兜底。" if operation == "list_memos" else "Task 时间范围按创建日期筛选。"
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "body": {
            "elements": [
                {"tag": "markdown", "content": "请设置查询条件。"},
                {
                    "tag": "form",
                    "name": "aha_menu_query",
                    "elements": [
                        {"tag": "markdown", "content": "**Run**"},
                        _menu_query_field_select("run_id", "Run", fields.get("runs") or [], fields.get("run_id")),
                        {"tag": "markdown", "content": "**状态**"},
                        _menu_query_field_select("status", "状态", fields.get("statuses") or [], fields.get("status")),
                        {"tag": "markdown", "content": "**开始日期**"},
                        _menu_query_date_picker("start_date", "开始日期"),
                        {"tag": "markdown", "content": "**结束日期**"},
                        _menu_query_date_picker("end_date", "结束日期"),
                        {"tag": "markdown", "content": "**数量上限**"},
                        _menu_query_field_select("limit", "数量上限", fields.get("limits") or [], fields.get("limit")),
                        {
                            "tag": "column_set",
                            "columns": [
                                {
                                    "tag": "column",
                                    "width": "auto",
                                    "elements": [
                                        _menu_query_button(
                                            "查询",
                                            MENU_QUERY_SUBMIT_CHOICE_ID,
                                            "primary",
                                            "aha_menu_query_submit",
                                            submit=True,
                                        )
                                    ],
                                },
                                {
                                    "tag": "column",
                                    "width": "auto",
                                    "elements": [
                                        _menu_query_button("取消", "__cancel__", "default", "aha_menu_query_cancel")
                                    ],
                                },
                            ],
                        },
                    ],
                },
                {"tag": "markdown", "content": f"<font color='grey'>{time_hint} 查询由 AHA 直接执行，不调用 agent/backend 模型。</font>"},
            ]
        },
    }


def _prepare_menu_query_form(root: Path, operation: str, *, open_id: str, session_key: str) -> dict:
    default_run_id = resolve_feishu_work_run_id(root)
    fields = {
        "run_id": default_run_id,
        "runs": _menu_query_run_options(root, default_run_id),
        "status": "all",
        "statuses": _menu_query_status_options(operation),
        "limit": "10",
        "limits": _menu_query_limit_options(),
    }
    confirmation_id = secrets.token_urlsafe(18)
    card = _menu_query_card(operation, fields)
    context = {
        "operation": operation,
        "fields": fields,
        "confirmation_id": confirmation_id,
    }
    issue_action_token(
        root,
        open_id=open_id,
        session_key=session_key,
        action=MENU_QUERY_ACTION,
        context=context,
    )
    register_confirmation_card(
        root,
        confirmation_id,
        open_id=open_id,
        session_key=session_key,
        action=MENU_QUERY_ACTION,
        card=card,
        expires_at=time.time() + ACTION_TOKEN_TTL_SECONDS,
    )
    return {
        "confirmation_id": confirmation_id,
        "confirmation_card": card,
    }


def _allowed_menu_query_values(options: object) -> set[str]:
    return {str(item.get("value") or "") for item in options if isinstance(item, dict)}


def _menu_query_form_value(form_values: dict | None, key: str, default: object = "") -> str:
    values = form_values if isinstance(form_values, dict) else {}
    for candidate in (key, f"aha_menu_query.{key}"):
        if candidate in values:
            value = values.get(candidate)
            if isinstance(value, dict):
                value = value.get("value") or value.get("text") or value.get("date")
            if isinstance(value, list):
                value = value[0] if value else ""
            return str(value or "").strip()
    return str(default or "").strip()


def _menu_query_arguments(operation: str, fields: dict, form_values: dict | None) -> dict:
    run_id = _menu_query_form_value(form_values, "run_id", fields.get("run_id"))
    if run_id not in _allowed_menu_query_values(fields.get("runs") or []):
        raise ServiceAssistantActionError("选择的 Run 不在本次查询卡可用范围内")
    status = _menu_query_form_value(form_values, "status", fields.get("status") or "all").lower()
    if status not in _allowed_menu_query_values(fields.get("statuses") or []):
        raise ServiceAssistantActionError("选择的状态不在本次查询卡可用范围内")
    limit_text = _menu_query_form_value(form_values, "limit", fields.get("limit") or "10")
    if limit_text not in _allowed_menu_query_values(fields.get("limits") or []):
        raise ServiceAssistantActionError("选择的数量上限不在本次查询卡可用范围内")
    start_date = normalize_memo_date(_menu_query_form_value(form_values, "start_date"))
    end_date = normalize_memo_date(_menu_query_form_value(form_values, "end_date"))
    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date
    return {
        "run_id": run_id,
        "status": status,
        "start_date": start_date,
        "end_date": end_date,
        "limit": int(limit_text or 10),
    }


def _brief_menu_text(value: object, *, limit: int = 120, fallback: str = "-") -> str:
    text = " ".join(str(value or "").split()).replace("```", "'''")
    if not text:
        return fallback
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)]}…"


def _memo_menu_line(memo: dict, index: int) -> str:
    parts = [
        f"**{index}. {_brief_menu_text(memo.get('title'), fallback='未命名 Memo')}**",
        f"`{_brief_menu_text(memo.get('id'))}` · {_brief_menu_text(memo.get('status'))}",
    ]
    dates = []
    if memo.get("created_at"):
        dates.append(f"创建：{_brief_menu_text(memo.get('created_at'), limit=24)}")
    if memo.get("scheduled_date"):
        dates.append(f"开始：{_brief_menu_text(memo.get('scheduled_date'), limit=24)}")
    if memo.get("end_date"):
        dates.append(f"结束：{_brief_menu_text(memo.get('end_date'), limit=24)}")
    if dates:
        parts.append(" · ".join(dates))
    if memo.get("created_task_id"):
        parts.append(f"关联 Task：`{_brief_menu_text(memo.get('created_task_id'), limit=40)}`")
    description = _brief_menu_text(memo.get("description"), limit=120, fallback="")
    if description:
        parts.append(description)
    return "\n".join(parts)


def _task_menu_line(task: dict, index: int) -> str:
    parts = [
        f"**{index}. {_brief_menu_text(task.get('title'), fallback='未命名 Task')}**",
        f"`{_brief_menu_text(task.get('id'))}` · {_brief_menu_text(task.get('status'))}",
    ]
    backend = _brief_menu_text(task.get("preferred_backend"), limit=40, fallback="")
    model = _brief_menu_text(task.get("preferred_model"), limit=60, fallback="")
    if backend or model:
        parts.append(" / ".join(item for item in (backend, model) if item))
    description = _brief_menu_text(task.get("description"), limit=120, fallback="")
    if description:
        parts.append(description)
    return "\n".join(parts)


def _date_in_menu_range(value: object, *, start_date: str, end_date: str) -> bool:
    date_value = normalize_memo_date(value)
    if not date_value:
        return not start_date and not end_date
    if start_date and date_value < start_date:
        return False
    if end_date and date_value > end_date:
        return False
    return True


def _memo_query_date(memo: dict) -> str:
    return normalize_memo_date(memo.get("scheduled_date")) or normalize_memo_date(memo.get("created_at"))


def _task_query_date(task: dict) -> str:
    return normalize_memo_date(task.get("created_at"))


def _menu_list_card(root: Path, operation: str, arguments: dict) -> dict:
    run_id = str(arguments.get("run_id") or "").strip() or resolve_feishu_work_run_id(root)
    plan = require_plan(root, run_id)
    if plan.get("system_managed"):
        raise ServiceAssistantActionError("system-managed runs are not available through menu queries")
    limit = max(1, min(20, int(arguments.get("limit") or 10)))
    status_filter = str(arguments.get("status") or "all").strip().lower()
    start_date = normalize_memo_date(arguments.get("start_date"))
    end_date = normalize_memo_date(arguments.get("end_date"))
    run_label = f"{_brief_menu_text(plan.get('goal'), limit=40)}.{run_id}"
    if operation == "list_memos":
        items = [
            item
            for item in read_task_memos(root, run_id)
            if (status_filter in {"", "all"} or normalize_memo_status(item.get("status")) == status_filter)
            and _date_in_menu_range(_memo_query_date(item), start_date=start_date, end_date=end_date)
        ][:limit]
        title = "Memo 列表"
        empty = "当前 Run 暂无 Memo。"
        body = [_memo_menu_line(item, index) for index, item in enumerate(items, start=1)]
    elif operation == "list_tasks":
        tasks = [
            task
            for task in reversed(plan.get("tasks", []))
            if isinstance(task, dict)
            and not task.get("deleted_at")
            and not task.get("hidden")
            and not is_service_assistant_task(task)
            and not is_feishu_group_task(task)
            and (status_filter in {"", "all"} or str(task.get("status") or "").strip().lower() == status_filter)
            and _date_in_menu_range(_task_query_date(task), start_date=start_date, end_date=end_date)
        ][:limit]
        title = "Task 列表"
        empty = "当前 Run 暂无可见 Task。"
        body = [_task_menu_line(item, index) for index, item in enumerate(tasks, start=1)]
    else:
        raise ServiceAssistantActionError(f"不支持的飞书菜单查询：{operation or '-'}")
    elements = [
        {"tag": "markdown", "content": f"**Run**：{run_label}\n**状态**：{status_filter or 'all'}\n**数量**：{len(body)} / 上限 {limit}"},
    ]
    if body:
        for item in body:
            elements.append({"tag": "markdown", "content": item})
    else:
        elements.append({"tag": "markdown", "content": empty})
    elements.append({"tag": "markdown", "content": "<font color='grey'>该菜单查询由 AHA 直接读取本地状态，不调用 agent/backend 模型。</font>"})
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "body": {"elements": elements},
    }


def _menu_error_message(payload: dict, exc: BaseException) -> str:
    event_id = _first_string(payload.get("message_id"), payload.get("event_id"))
    event_key = _first_string(payload.get("event_key"))
    lines = [
        "无法处理飞书菜单",
        f"时间：{utc_now()}",
    ]
    if event_key:
        lines.append(f"event_key：{event_key}")
    if event_id:
        lines.append(f"event_id：{event_id}")
    lines.append(f"原因：{exc}")
    return "\n".join(lines)


def _handle_menu_action(root: Path, server_default_run_id: str, channel: Any, payload: dict) -> None:
    chat_id = str(payload.get("chat_id") or "")
    event_key = str(payload.get("event_key") or "")
    operation = _owner_menu_operation(event_key)
    if not operation:
        _audit_inbound_resolution(root, payload, "ignored", reason="unsupported_menu_action")
        return
    menu_event_id = str(payload.get("message_id") or "").strip()
    if menu_event_id and not claim_inbound_message(root, f"menu:{menu_event_id}"):
        _audit_inbound_resolution(root, payload, "ignored", reason="duplicate_menu_action")
        return
    try:
        if operation in {"list_memos", "list_tasks"}:
            _, open_id, chat_id, session_key = _owner_menu_private_context(root, payload)
            _record_owner_menu_private_chat(root, payload, chat_id=chat_id)
            _send_menu_card(
                root,
                channel,
                chat_id,
                _prepare_menu_query_form(root, operation, open_id=open_id, session_key=session_key),
            )
            _audit_inbound_resolution(
                root,
                payload,
                "handled",
                session_key=session_key,
                reason="menu_query_form",
            )
            return
        chat_id, run_id, task_id, task = _owner_menu_session(root, server_default_run_id, payload)
        _record_owner_menu_private_chat(root, payload, chat_id=chat_id)
        action = prepare_service_assistant_action(
            root,
            run_id,
            task,
            {"operation": operation, "arguments": _owner_menu_arguments(root, operation)},
        )
        if action.get("confirmation_card"):
            _send_menu_card(root, channel, chat_id, action)
        else:
            _send_text(root, channel, chat_id, str(action.get("user_response") or "菜单操作已处理。"))
        session_key = _session_key(
            {
                "tenant_key": payload.get("tenant_key") or "local",
                "open_id": payload.get("open_id") or "",
                "chat_id": chat_id,
                "chat_type": "p2p",
            }
        )
        _audit_inbound_resolution(root, payload, "handled", session_key=session_key, run_id=run_id, task_id=task_id)
    except (FeishuError, ServiceAssistantActionError, KeyError, SystemExit, ValueError) as exc:
        _audit_inbound_resolution(root, payload, "failed", error=exc)
        if chat_id:
            _send_text(root, channel, chat_id, _menu_error_message(payload, exc))


def _handle_message(root: Path, server_default_run_id: str, channel: Any, payload: dict) -> None:
    config = feishu_config(root)
    chat_id = str(payload.get("chat_id") or "")
    message_id = str(payload.get("message_id") or "")
    open_id = str(payload.get("open_id") or "")
    if payload.get("sender_is_bot"):
        _audit_inbound_resolution(root, payload, "ignored", reason="sender_is_bot")
        return
    chat_type = str(payload.get("chat_type") or "").lower()
    if chat_type == "group" and chat_id:
        record_recent_group(root, chat_id, display_name=str(payload.get("chat_name") or ""))
        if open_id and payload.get("sender_name"):
            remember_identity_profile(
                root,
                kind="open_id",
                identity=open_id,
                display_name=str(payload.get("sender_name") or ""),
                chat_type="group",
            )
    elif chat_type == "p2p" and (chat_id or open_id):
        record_recent_private_chat(
            root,
            chat_id=chat_id,
            open_id=open_id,
            display_name=str(payload.get("sender_name") or ""),
        )
    _refresh_identity_profiles_background(root, config, payload)
    if chat_type == "group" and not payload.get("is_at_bot"):
        _audit_inbound_resolution(root, payload, "ignored", reason="group_without_mention")
        return
    if chat_type != "p2p" and config.get("group_mentions_only") and not payload.get("is_at_bot"):
        _audit_inbound_resolution(root, payload, "ignored", reason="group_without_mention")
        return
    if not claim_inbound_message(root, message_id):
        _audit_inbound_resolution(root, payload, "ignored", reason="duplicate_message")
        return
    authorization_error = _authorization_error(config, chat_type=chat_type, chat_id=chat_id, open_id=open_id)
    if authorization_error:
        _audit_inbound_resolution(root, payload, "rejected", reason=authorization_error)
        _send_text(
            root,
            channel,
            chat_id,
            _unauthorized_message(chat_type, open_id, authorization_error),
            reply_to=message_id,
        )
        return
    text = str(payload.get("text") or "").strip()
    if not text:
        _audit_inbound_resolution(root, payload, "rejected", reason="empty_text")
        _send_text(root, channel, chat_id, "请发送文本消息。", reply_to=message_id)
        return
    if chat_type == "group":
        _handle_group_mention(root, channel, payload, text=text)
        return

    session_key = _session_key(payload)
    binding = _binding(root, session_key, open_id, server_default_run_id)
    run_id = str(binding.get("active_run_id") or "")
    if not run_id or not run_exists(root, run_id):
        _audit_inbound_resolution(root, payload, "rejected", reason="run_unavailable", session_key=session_key)
        _send_text(root, channel, chat_id, "AHA 尚无可用 Run，请先在 Web 中创建一个 Run。", reply_to=message_id)
        return
    display_name = _open_id_display_name(root, open_id, payload.get("sender_name"))
    task = _ensure_agent_task(root, run_id, session_key, open_id, binding, display_name=display_name)
    task_id = str(task.get("id") or "")
    set_subscription(
        root,
        session_key,
        chat_id=chat_id,
        open_id=open_id,
        run_id=run_id,
        task_id=task_id,
        chat_type=chat_type,
    )
    remember_owner_private_chat(
        root,
        tenant_key=str(payload.get("tenant_key") or "local"),
        open_id=open_id,
        chat_id=chat_id,
        session_key=session_key,
    )
    try:
        confirmation = resolve_confirmation(root, open_id=open_id, session_key=session_key, text=text)
    except (FeishuError, ServiceAssistantActionError, KeyError, SystemExit, ValueError) as exc:
        _audit_inbound_resolution(
            root,
            payload,
            "failed",
            session_key=session_key,
            run_id=run_id,
            task_id=task_id,
            error=exc,
        )
        _send_text(root, channel, chat_id, f"无法处理确认：{exc}", reply_to=message_id)
        return
    if confirmation is not None:
        _finish_confirmation(
            root,
            channel,
            chat_id=chat_id,
            message_id=message_id,
            run_id=run_id,
            task_id=task_id,
            confirmation=confirmation,
        )
        _audit_inbound_resolution(
            root,
            payload,
            "handled",
            reason="confirmation",
            session_key=session_key,
            run_id=run_id,
            task_id=task_id,
        )
        return
    handle_send_payload(
        root,
        run_id,
        {
            "task_id": task_id,
            "target": "main",
            "sender": "feishu",
            "message": text,
            "feishu_attachments": payload.get("attachments") if isinstance(payload.get("attachments"), list) else [],
        },
        command_handler=_never_handle_command,
        background_backend_start=True,
    )
    _audit_inbound_resolution(
        root,
        payload,
        "accepted",
        session_key=session_key,
        run_id=run_id,
        task_id=task_id,
    )
    _send_text(root, channel, chat_id, "已交给 AHA agent，回复会推送到本会话。", reply_to=message_id)


__all__ = ["enqueue_card_action", "enqueue_message", "enqueue_raw_event"]
