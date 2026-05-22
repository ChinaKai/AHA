from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.backend_runtime import PROCESS_AGENT_BACKENDS, backend_status, start_backend
from aha_cli.services.chat import chat_offset_path, save_chat_offset
from aha_cli.store.filesystem import (
    append_message,
    inbox_path,
    run_dir,
    task_snapshot,
)
from aha_cli.web.status import (
    TERMINAL_TASK_STATUSES,
    consume_agent_recovery_context,
    invalidate_backend_status_cache,
    merge_recovery_context_message,
)

CommandHandler = Callable[[Path, str, dict, str, str | None], tuple[bool, str | None, dict]]
PreparedBackendStarter = Callable[[Path, str, dict | None], dict | None]
DebugLogger = Callable[..., None]


def realtime_debug_log(source: str, **fields: object) -> None:
    root = fields.pop("_root", None)
    run_id = str(fields.get("run_id") or "")
    payload = {"ts": utc_now(), "source": source, **fields}
    line = "[aha realtime] " + json.dumps(payload, ensure_ascii=False, default=str)
    print(line, flush=True)
    if isinstance(root, Path) and run_id:
        try:
            log_dir = run_dir(root, run_id) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "realtime-debug.log").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


def _default_handle_slash_command(root: Path, run_id: str, payload: dict, message: str, task_id: str | None) -> tuple[bool, str | None, dict]:
    from aha_cli.web.task_actions import handle_slash_command

    return handle_slash_command(root, run_id, payload, message, task_id)


def _default_start_prepared_backend(root: Path, run_id: str, autostart: dict | None) -> dict | None:
    from aha_cli.web.task_actions import start_prepared_backend

    return start_prepared_backend(root, run_id, autostart)


def is_task_supervision_host_target(task: dict, target_id: str | None) -> bool:
    supervision = task.get("supervision") if isinstance(task.get("supervision"), dict) else {}
    host_agent_id = str(supervision.get("host_agent_id") or "host")
    return bool(
        target_id
        and target_id == host_agent_id
        and supervision.get("mode") == "assisted"
        and supervision.get("real_agent_enabled")
        and supervision.get("host_backend") != "stub"
    )


def is_supervision_host_message(root: Path, run_id: str, task_id: str | None, target_id: str) -> bool:
    if not task_id:
        return False
    try:
        return is_task_supervision_host_target(task_snapshot(root, run_id, task_id)["task"], target_id)
    except KeyError:
        return False


def message_backend_autostart_config(root: Path, run_id: str, task_id: str | None, target_id: str) -> dict | None:
    if not task_id or not target_id:
        return None
    try:
        detail = task_snapshot(root, run_id, task_id)
    except KeyError:
        return None
    task = detail["task"]
    if is_task_supervision_host_target(task, target_id):
        return None
    agent = next((item for item in task.get("agents", []) if item.get("id") == target_id), None)
    if not agent:
        return None
    backend = str(agent.get("backend") or task.get("preferred_backend") or "codex")
    if backend not in PROCESS_AGENT_BACKENDS:
        return None
    state = backend_status(root, run_id, target_id, task_id=task_id)
    if state.get("status") != "stopped":
        return None
    return {
        "backend": backend,
        "target": target_id,
        "task_id": task_id,
        "model": agent.get("model") or task.get("preferred_model"),
        "sandbox": agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
        "approval": agent.get("approval") or task.get("preferred_approval") or "never",
    }


def save_chat_offset_after_message(root: Path, run_id: str, task_id: str, target_id: str) -> None:
    inbox = inbox_path(root, run_id, target_id)
    offset_file = chat_offset_path(run_dir(root, run_id), target_id, task_id)
    save_chat_offset(offset_file, inbox.stat().st_size if inbox.exists() else 0)


def ensure_chat_offset_before_message(root: Path, run_id: str, task_id: str, target_id: str) -> None:
    offset_file = chat_offset_path(run_dir(root, run_id), target_id, task_id)
    if offset_file.exists():
        return
    inbox = inbox_path(root, run_id, target_id)
    save_chat_offset(offset_file, inbox.stat().st_size if inbox.exists() else 0)


def task_locked_for_messages(root: Path, run_id: str, task_id: str | None) -> str | None:
    if not task_id:
        return None
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return None
    status = str(task.get("status") or "")
    return status if status in TERMINAL_TASK_STATUSES else None


def handle_send_payload(
    root: Path,
    run_id: str,
    payload: dict,
    *,
    command_handler: CommandHandler | None = None,
    prepared_backend_starter: PreparedBackendStarter | None = None,
    debug_logger: DebugLogger = realtime_debug_log,
) -> dict:
    message = str(payload.get("message", "")).strip()
    task_id = str(payload.get("task_id", "")).strip() or None
    role = str(payload.get("role", "")).strip() or None
    target_id = str(payload.get("target", "")).strip()
    if not target_id:
        target_id = task_id if role == "sub" and task_id else "main"
    if not message:
        raise ValueError("message cannot be empty")

    debug_logger(
        "api.send",
        _root=root,
        phase="request",
        run_id=run_id,
        task_id=task_id or "",
        target=target_id,
        role=role or "",
        sender=str(payload.get("sender", "") or ""),
        from_agent=str(payload.get("from_agent", "") or ""),
        to_agent=str(payload.get("to_agent", "") or ""),
        message_len=len(message),
        is_command=message.startswith("/"),
    )
    handle_command = command_handler or _default_handle_slash_command
    handled, agent_message, command_payload = handle_command(root, run_id, payload, message, task_id)
    if handled:
        backend_autostart = command_payload.pop("backend_autostart", None)
        start_prepared = prepared_backend_starter or _default_start_prepared_backend
        backend = start_prepared(root, run_id, backend_autostart)
        if backend:
            if backend_autostart:
                invalidate_backend_status_cache(
                    root,
                    run_id,
                    str(backend_autostart.get("target") or target_id),
                    str(backend_autostart.get("task_id") or task_id or "") or None,
                )
            command_payload["backend"] = backend
        debug_logger(
            "api.send",
            _root=root,
            phase="handled_command",
            run_id=run_id,
            task_id=task_id or "",
            target=target_id,
            backend_started=bool(backend),
            reply_keys=sorted(command_payload.keys()),
        )
        return {"ok": True, "handled_by": "aha", **command_payload}

    locked_status = task_locked_for_messages(root, run_id, task_id)
    if locked_status:
        raise ValueError(f"task {task_id} is {locked_status}; use /aha reopen before sending follow-up messages")

    supervision_host_message = is_supervision_host_message(root, run_id, task_id, target_id)
    autostart = message_backend_autostart_config(root, run_id, task_id, target_id)
    if autostart and task_id:
        ensure_chat_offset_before_message(root, run_id, task_id, target_id)

    message = agent_message or message
    recovery_context = consume_agent_recovery_context(root, run_id, task_id, target_id)
    if recovery_context:
        message = merge_recovery_context_message(recovery_context, message)
    sent = append_message(
        root,
        run_id,
        target_id,
        message,
        str(payload.get("sender", "browser") or "browser"),
        task_id=task_id,
        role=role,
        from_agent=str(payload.get("from_agent", "") or "") or None,
        to_agent=str(payload.get("to_agent", "") or "") or None,
        command_namespace=str(command_payload.get("command_namespace", "") or "") or None,
        original_command=str(command_payload.get("original_command", "") or "") or None,
        result_policy=str(command_payload.get("result_policy", "") or "") or None,
    )
    response = {"ok": True, "message": sent}
    if supervision_host_message and task_id:
        save_chat_offset_after_message(root, run_id, task_id, target_id)
    if autostart:
        response["backend"] = start_backend(
            root,
            run_id,
            autostart["target"],
            backend=autostart["backend"],
            model=autostart["model"],
            sandbox=autostart["sandbox"],
            approval=autostart["approval"],
            from_start=False,
            task_id=autostart["task_id"],
        )
        invalidate_backend_status_cache(root, run_id, autostart["target"], autostart["task_id"])
    debug_logger(
        "api.send",
        _root=root,
        phase="stored",
        run_id=run_id,
        task_id=task_id or "",
        target=target_id,
        backend_started=bool(response.get("backend")),
        response_keys=sorted(response.keys()),
    )
    return response


__all__ = [
    "ensure_chat_offset_before_message",
    "handle_send_payload",
    "is_supervision_host_message",
    "is_task_supervision_host_target",
    "message_backend_autostart_config",
    "realtime_debug_log",
    "save_chat_offset_after_message",
    "task_locked_for_messages",
]
