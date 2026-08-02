from __future__ import annotations

from pathlib import Path
import queue
import sys
import threading
from typing import Any

from aha_cli.services.feishu import (
    FeishuError,
    claim_inbound_message,
    confirmation_card_for_message,
    get_session_binding,
    make_session_key,
    mark_confirmation_card_updated,
    record_recent_group,
    set_session_binding,
)
from aha_cli.services.feishu_audit import audit_feishu_channel
from aha_cli.services.feishu_notifications import load_subscription_state, set_subscription
from aha_cli.services.feishu_runtime import feishu_config, feishu_credentials
from aha_cli.services.service_assistant import (
    LEGACY_ASSISTANT_RUN_TITLE,
    ensure_service_assistant_run,
    ensure_service_assistant_task,
    session_task_title,
)
from aha_cli.services.service_assistant_actions import ServiceAssistantActionError, resolve_confirmation
from aha_cli.store.config import load_config
from aha_cli.store.paths import aha_home_path
from aha_cli.store.runs import run_exists
from aha_cli.store.snapshots import task_snapshot
from aha_cli.domain.models import is_service_assistant_task
from aha_cli.web.status import TERMINAL_TASK_STATUSES
from aha_cli.web.task_messaging import handle_send_payload

ASSISTANT_QUEUE_LIMIT = 128
ASSISTANT_RUN_TITLE = LEGACY_ASSISTANT_RUN_TITLE
ASSISTANT_TASK_TITLE = "AHA Assistant"

_assistant_queue: queue.Queue[tuple[Path, str, Any, dict] | None] = queue.Queue(maxsize=ASSISTANT_QUEUE_LIMIT)
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def _plain_message(root: Path, message: Any) -> dict:
    config = feishu_config(root)
    app_id, _secret = feishu_credentials(config)
    raw = getattr(message, "raw", {})
    raw = raw if isinstance(raw, dict) else {}
    header = raw.get("header") if isinstance(raw.get("header"), dict) else {}
    sender = getattr(message, "sender", None)
    return {
        "tenant_key": str(header.get("tenant_key") or app_id or "local"),
        "open_id": str(getattr(message, "sender_id", "") or getattr(sender, "open_id", "") or ""),
        "chat_id": str(getattr(message, "chat_id", "") or ""),
        "chat_type": str(getattr(message, "chat_type", "") or "unknown").lower(),
        "message_id": str(getattr(message, "message_id", "") or getattr(message, "id", "") or ""),
        "text": str(
            getattr(message, "body_text", "")
            or getattr(message, "safe_content_text", "")
            or getattr(message, "content_text", "")
            or ""
        ).strip(),
        "is_at_bot": bool(getattr(message, "mentioned_bot", False)),
        "sender_is_bot": bool(getattr(message, "sender_is_bot", False)),
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


def enqueue_card_action(root: Path, default_run_id: str, channel: Any, event: Any) -> None:
    _ensure_worker()
    action = getattr(event, "action", None)
    operator = getattr(event, "operator", None)
    payload = {
        "kind": "card_action",
        "chat_id": str(getattr(event, "chat_id", "") or ""),
        "message_id": str(getattr(event, "message_id", "") or ""),
        "open_id": str(getattr(operator, "open_id", "") or ""),
        "action": getattr(action, "value", None),
    }
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


def _worker_loop() -> None:
    while True:
        item = _assistant_queue.get()
        try:
            if item is None:
                return
            root, default_run_id, channel, payload = item
            if payload.get("kind") == "card_action":
                _handle_card_action(root, channel, payload)
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


def _update_confirmation_card(root: Path, channel: Any, confirmation: dict) -> None:
    message_id = str(confirmation.get("confirmation_message_id") or confirmation.get("message_id") or "")
    card = confirmation.get("confirmation_card") or confirmation.get("terminal_card")
    if not message_id or not isinstance(card, dict):
        return
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


def _ensure_agent_task(root: Path, run_id: str, session_key: str, open_id: str, binding: dict) -> dict:
    active = _active_task(root, run_id, str(binding.get("active_task_id") or ""))
    if active is not None:
        return active
    task = ensure_service_assistant_task(root, run_id, session_key, _assistant_agent_defaults(root))
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
    _send_text(root, channel, chat_id, "操作已确认并执行，AHA 助手正在整理结果。", reply_to=message_id)


def _handle_card_action(root: Path, channel: Any, payload: dict) -> None:
    value = payload.get("action") if isinstance(payload.get("action"), dict) else {}
    if str(value.get("kind") or "") != "aha_service_confirmation":
        _audit_inbound_resolution(root, payload, "ignored", reason="unsupported_card_action")
        return
    chat_id = str(payload.get("chat_id") or "")
    message_id = str(payload.get("message_id") or "")
    open_id = str(payload.get("open_id") or "")
    decision = {"confirm": "确认", "cancel": "取消"}.get(str(value.get("decision") or "").lower())
    if not decision:
        _audit_inbound_resolution(root, payload, "rejected", reason="invalid_decision")
        _send_text(root, channel, chat_id, "无法处理卡片操作：确认数据不完整。", reply_to=message_id)
        return
    try:
        session_key, subscription = _confirmation_subscription(root, chat_id, open_id)
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
            run_id=str(subscription.get("run_id") or ""),
            task_id=str(subscription.get("task_id") or ""),
            confirmation=confirmation,
        )
        _audit_inbound_resolution(
            root,
            payload,
            "handled",
            session_key=session_key,
            run_id=str(subscription.get("run_id") or ""),
            task_id=str(subscription.get("task_id") or ""),
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
        record_recent_group(root, chat_id)
    if payload.get("chat_type") != "p2p" and config.get("group_mentions_only") and not payload.get("is_at_bot"):
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

    session_key = _session_key(payload)
    binding = _binding(root, session_key, open_id, server_default_run_id)
    run_id = str(binding.get("active_run_id") or "")
    if not run_id or not run_exists(root, run_id):
        _audit_inbound_resolution(root, payload, "rejected", reason="run_unavailable", session_key=session_key)
        _send_text(root, channel, chat_id, "AHA 尚无可用 Run，请先在 Web 中创建一个 Run。", reply_to=message_id)
        return
    task = _ensure_agent_task(root, run_id, session_key, open_id, binding)
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
        {"task_id": task_id, "target": "main", "sender": "feishu", "message": text},
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


__all__ = ["enqueue_card_action", "enqueue_message"]
