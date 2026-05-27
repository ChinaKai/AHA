from __future__ import annotations

from pathlib import Path

from aha_cli.services.backend_runtime import backend_status, stop_backend
from aha_cli.services.chat import chat_offset_path, save_chat_offset
from aha_cli.services.session_compact import compact_reset_backend_session
from aha_cli.store.filesystem import (
    append_event,
    append_task_round,
    inbox_path,
    reopen_task,
    run_dir,
    set_agent_status,
    set_task_status,
    task_snapshot,
)
from aha_cli.web.task_runtime import (
    finalization_prompt,
    format_task_journal_for_prompt,
    prepare_task_main_autostart,
    request_task_finalization,
    request_task_finalization_with_backend,
    start_prepared_backend,
)


def compact_reset_selected_agent(root: Path, run_id: str, task_id: str | None, target: str, *, restart: bool = True) -> tuple[str, dict]:
    if not task_id:
        return "No task is selected.", {"ok": False, "reason": "no_task"}
    try:
        payload = compact_reset_backend_session(root, run_id, task_id, target or "main", reason="manual", restart=restart)
    except KeyError as exc:
        return f"Task or agent not found: {exc}", {"ok": False, "reason": "not_found"}
    except ValueError as exc:
        return str(exc), {"ok": False, "reason": "invalid"}
    return (
        f"Compact-reset completed for {task_id}/{target or 'main'}. "
        f"Archived `{payload.get('old_backend_session_id')}` and wrote `{payload.get('summary_path')}`.",
        payload,
    )


def record_task_checkpoint(root: Path, run_id: str, task_id: str | None, command: str) -> str:
    if not task_id:
        return "No task is selected."
    parts = command.split(maxsplit=2)
    summary = parts[2].strip() if len(parts) > 2 else ""
    if not summary:
        return "Usage: /aha checkpoint <summary>"
    try:
        record = append_task_round(root, run_id, task_id, {"trigger": "manual", "summary": summary, "agents": ["browser"]})
    except KeyError:
        return f"Task not found: {task_id}"
    return f"Checkpoint recorded for {task_id}: {record['round_id']}"


def reopen_selected_task(root: Path, run_id: str, task_id: str | None) -> str:
    if not task_id:
        return "No task is selected."
    try:
        reopen_task(root, run_id, task_id)
    except SystemExit:
        return f"Task not found: {task_id}"
    return f"{task_id} reopened. Follow-up messages are allowed again."


def _agent_by_id(task: dict, agent_id: str) -> dict:
    return next((agent for agent in task.get("agents", []) if str(agent.get("id") or "") == agent_id), {})


def _is_host_agent(task: dict, agent_id: str) -> bool:
    supervision = task.get("supervision") if isinstance(task.get("supervision"), dict) else {}
    host_agent_id = str(supervision.get("host_agent_id") or "host")
    agent = _agent_by_id(task, agent_id)
    return agent_id == host_agent_id or str(agent.get("role") or "") == "host"


def _main_waiting_for_host(task: dict) -> bool:
    main_agent = _agent_by_id(task, "main")
    return (
        str(main_agent.get("status") or "").lower() == "waiting"
        and str(main_agent.get("waiting_reason") or "").lower() == "host"
    )


def _can_logically_interrupt_stopped_host(task: dict, agent_id: str) -> bool:
    if not _is_host_agent(task, agent_id):
        return False
    agent_status = str(_agent_by_id(task, agent_id).get("status") or "").lower()
    return agent_status in {"pending", "running", "waiting"} or _main_waiting_for_host(task)


def _clear_main_host_wait_if_needed(root: Path, run_id: str, task_id: str, task: dict, interrupted_agent_id: str) -> None:
    if _is_host_agent(task, interrupted_agent_id) and _main_waiting_for_host(task):
        set_agent_status(root, run_id, task_id, "main", "completed")


def interrupt_selected_agent(root: Path, run_id: str, task_id: str | None, target: str) -> tuple[str, dict]:
    if not task_id:
        return "No task is selected.", {"interrupted": False, "reason": "no_task"}
    try:
        detail = task_snapshot(root, run_id, task_id)
    except KeyError:
        return f"Task not found: {task_id}", {"interrupted": False, "reason": "task_not_found"}
    task = detail["task"]
    agent_id = target or "main"
    if not any(str(agent.get("id") or "") == agent_id for agent in task.get("agents", [])):
        return f"Agent not found: {agent_id}", {"interrupted": False, "reason": "agent_not_found", "agent_id": agent_id}
    state = backend_status(root, run_id, agent_id, task_id=task_id)
    state_status = str(state.get("status") or "").lower()
    logical_interrupt = state_status == "stopped" and _can_logically_interrupt_stopped_host(task, agent_id)
    if state_status not in {"busy", "running"} and not logical_interrupt:
        return (
            f"No active turn to interrupt for {agent_id} on {task_id}.",
            {"interrupted": False, "reason": "not_busy", "agent_id": agent_id, "task_id": task_id, "backend": state},
        )
    stopped = stop_backend(root, run_id, agent_id, task_id=task_id, timeout=2.0)
    offset_file = chat_offset_path(run_dir(root, run_id), agent_id, task_id)
    inbox = inbox_path(root, run_id, agent_id)
    save_chat_offset(offset_file, inbox.stat().st_size if inbox.exists() else 0)
    set_agent_status(root, run_id, task_id, agent_id, "interrupted")
    _clear_main_host_wait_if_needed(root, run_id, task_id, task, agent_id)
    set_task_status(root, run_id, task_id, "awaiting_user")
    append_event(
        root,
        run_id,
        "agent_interrupted",
        {"task_id": task_id, "agent_id": agent_id, "target": agent_id, "backend": stopped},
    )
    return (
        f"Interrupted {agent_id} on {task_id}. Pending user messages were not sent automatically.",
        {"interrupted": True, "agent_id": agent_id, "task_id": task_id, "backend": stopped},
    )


__all__ = [
    "compact_reset_selected_agent",
    "finalization_prompt",
    "format_task_journal_for_prompt",
    "interrupt_selected_agent",
    "prepare_task_main_autostart",
    "record_task_checkpoint",
    "reopen_selected_task",
    "request_task_finalization",
    "request_task_finalization_with_backend",
    "start_prepared_backend",
]
