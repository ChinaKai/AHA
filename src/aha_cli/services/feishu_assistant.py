from __future__ import annotations

from pathlib import Path
import queue
import sys
import threading
from typing import Any

from aha_cli.services.feishu import (
    FeishuError,
    claim_inbound_message,
    get_session_binding,
    make_session_key,
    set_session_binding,
)
from aha_cli.services.feishu_notifications import set_subscription
from aha_cli.services.feishu_runtime import feishu_config, feishu_credentials
from aha_cli.services.tasks import create_task_and_dispatch
from aha_cli.store.config import load_config
from aha_cli.store.runs import list_run_summaries, require_plan, run_exists
from aha_cli.store.snapshots import task_snapshot
from aha_cli.web.status import TERMINAL_TASK_STATUSES
from aha_cli.web.task_messaging import handle_send_payload

ASSISTANT_QUEUE_LIMIT = 128
ASSISTANT_TASK_TITLE = "AHA 飞书助手"
ASSISTANT_TASK_DESCRIPTION = """
你是与用户在飞书中持续对话的真实 AHA 助手。请直接理解自然语言，并使用 AHA 现有能力帮助查看或管理 run、task、memo、KB 和 Settings。
不要使用关键词匹配或预制菜单回答。用中文简洁回复；遇到需要用户选择或高风险的写操作时先说明影响并请求确认。
这是持久的飞书助手会话，不要主动结束或完成该 task。
""".strip()

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
        _assistant_queue.put_nowait((root, default_run_id, channel, _plain_message(root, message)))
    except queue.Full:
        _send_text_background(channel, str(getattr(message, "chat_id", "") or ""), "AHA 助手当前繁忙，请稍后重试。")


def _worker_loop() -> None:
    while True:
        item = _assistant_queue.get()
        try:
            if item is None:
                return
            root, default_run_id, channel, payload = item
            _handle_message(root, default_run_id, channel, payload)
        except Exception as exc:  # noqa: BLE001 - one bad event must not stop the assistant worker.
            print(f"[aha feishu] assistant message failed: {exc!r}", file=sys.stderr, flush=True)
            try:
                _send_text(item[2], str(item[3].get("chat_id") or ""), "AHA 助手处理失败，请稍后重试。")
            except Exception:  # noqa: BLE001
                pass
        finally:
            _assistant_queue.task_done()


def _send(channel: Any, chat_id: str, message: object, opts: dict | None = None) -> dict:
    if not chat_id:
        raise FeishuError("飞书消息缺少 chat_id")
    result = channel.schedule(channel.send(chat_id, message, opts)).result(timeout=20)
    if hasattr(result, "success") and not result.success:
        raise FeishuError(str(getattr(result, "error", None) or "飞书消息发送失败"))
    return {"ok": True, "message_id": getattr(result, "message_id", None)}


def _send_text(channel: Any, chat_id: str, text: str, *, reply_to: str = "") -> dict:
    opts = {"reply_to": reply_to} if reply_to else None
    return _send(channel, chat_id, {"text": str(text)}, opts)


def _send_text_background(channel: Any, chat_id: str, text: str) -> None:
    if not chat_id:
        return
    try:
        channel.schedule(channel.send(chat_id, {"text": str(text)}, None))
    except Exception:  # noqa: BLE001 - the SDK callback must return without blocking.
        pass


def _allowed(config: dict, open_id: str) -> bool:
    allowed = {str(item) for item in config.get("allowed_open_ids") or []}
    return bool(open_id and open_id in allowed)


def _unauthorized_message(chat_type: str, open_id: str) -> str:
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


def _default_run(root: Path, server_default_run_id: str) -> str:
    if server_default_run_id and run_exists(root, server_default_run_id):
        return server_default_run_id
    for summary in list_run_summaries(root):
        lifecycle = summary.get("lifecycle") if isinstance(summary.get("lifecycle"), dict) else {}
        if str(lifecycle.get("status") or "active") == "active":
            return str(summary.get("id") or "")
    return ""


def _binding(root: Path, session_key: str, open_id: str, server_default_run_id: str) -> dict:
    current = get_session_binding(root, session_key)
    if current is not None:
        return current
    return set_session_binding(
        root,
        session_key,
        active_run_id=_default_run(root, server_default_run_id) or None,
        active_task_id=None,
        acl_subject=open_id,
    )


def _task_workspace(root: Path, run_id: str) -> str:
    plan = require_plan(root, run_id)
    for task in reversed(plan.get("tasks", [])):
        workspace = str(task.get("workspace_path") or "").strip()
        if workspace and not task.get("deleted_at") and Path(workspace).is_dir():
            return workspace
    current = Path.cwd().resolve()
    return str(current if current.is_dir() else root.resolve())


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
    return None if str(task.get("status") or "") in TERMINAL_TASK_STATUSES else task


def _ensure_agent_task(root: Path, run_id: str, session_key: str, open_id: str, binding: dict) -> dict:
    active = _active_task(root, run_id, str(binding.get("active_task_id") or ""))
    if active is not None:
        return active
    config = load_config(root)
    task = create_task_and_dispatch(
        root,
        run_id,
        ASSISTANT_TASK_TITLE,
        backend=str(config.get("backend") or "codex"),
        workspace_path=_task_workspace(root, run_id),
        description=ASSISTANT_TASK_DESCRIPTION,
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


def _handle_message(root: Path, server_default_run_id: str, channel: Any, payload: dict) -> None:
    config = feishu_config(root)
    chat_id = str(payload.get("chat_id") or "")
    message_id = str(payload.get("message_id") or "")
    open_id = str(payload.get("open_id") or "")
    if payload.get("sender_is_bot"):
        return
    if payload.get("chat_type") != "p2p" and config.get("group_mentions_only") and not payload.get("is_at_bot"):
        return
    if not claim_inbound_message(root, message_id):
        return
    if not _allowed(config, open_id):
        _send_text(channel, chat_id, _unauthorized_message(str(payload.get("chat_type") or ""), open_id), reply_to=message_id)
        return
    text = str(payload.get("text") or "").strip()
    if not text:
        _send_text(channel, chat_id, "请发送文本消息。", reply_to=message_id)
        return

    session_key = _session_key(payload)
    binding = _binding(root, session_key, open_id, server_default_run_id)
    run_id = str(binding.get("active_run_id") or "")
    if not run_id or not run_exists(root, run_id):
        _send_text(channel, chat_id, "AHA 尚无可用 Run，请先在 Web 中创建一个 Run。", reply_to=message_id)
        return
    task = _ensure_agent_task(root, run_id, session_key, open_id, binding)
    task_id = str(task.get("id") or "")
    handle_send_payload(
        root,
        run_id,
        {"task_id": task_id, "target": "main", "sender": "feishu", "message": text},
        command_handler=_never_handle_command,
        background_backend_start=True,
    )
    set_subscription(root, session_key, chat_id=chat_id, open_id=open_id, run_id=run_id, task_id=task_id)
    _send_text(channel, chat_id, "已交给 AHA agent，回复会推送到本会话。", reply_to=message_id)


__all__ = ["enqueue_message"]
