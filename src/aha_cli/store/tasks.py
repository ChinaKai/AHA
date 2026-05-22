from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.store.events import append_event as default_append_event
from aha_cli.store.io import write_json
from aha_cli.store.paths import run_dir
from aha_cli.store.runs import locked_plan, require_plan, save_plan

TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}


def _find_task(plan: dict, task_id: str, *, allow_deleted: bool = False) -> dict:
    task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
    if task is None or (task.get("deleted_at") and not allow_deleted):
        raise SystemExit(f"Task not found: {task_id}")
    return task


def _write_task(root: Path, run_id: str, task: dict) -> None:
    write_json(run_dir(root, run_id) / "tasks" / task["id"] / "task.json", task)


def mark_task_coordination(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    now_func: Callable[[], str] = utc_now,
    append_event_func: Callable[[Path, str, str, dict], dict] = default_append_event,
    **fields: object,
) -> dict:
    now = now_func()
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = _find_task(plan, task_id)
        coordination = task.setdefault("coordination", {})
        for key, value in fields.items():
            if value is not None:
                coordination[key] = value
        coordination["updated_at"] = now
        plan["updated_at"] = now
        save_plan(root, plan)
        _write_task(root, run_id, task)
    append_event_func(root, run_id, "task_coordination_updated", {"task_id": task_id, **fields})
    return task


def set_task_hidden(
    root: Path,
    run_id: str,
    task_id: str,
    hidden: bool,
    *,
    now_func: Callable[[], str] = utc_now,
    append_event_func: Callable[[Path, str, str, dict], dict] = default_append_event,
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = _find_task(plan, task_id)
        task["hidden"] = hidden
        task["hidden_at"] = now_func() if hidden else None
        plan["updated_at"] = now_func()
        save_plan(root, plan)
        _write_task(root, run_id, task)
    append_event_func(root, run_id, "task_hidden" if hidden else "task_restored", {"task_id": task_id})
    return task


def delete_task(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    now_func: Callable[[], str] = utc_now,
    append_event_func: Callable[[Path, str, str, dict], dict] = default_append_event,
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = _find_task(plan, task_id, allow_deleted=True)
        task["hidden"] = True
        task["hidden_at"] = task.get("hidden_at") or now_func()
        task["deleted_at"] = task.get("deleted_at") or now_func()
        plan["updated_at"] = now_func()
        save_plan(root, plan)
        _write_task(root, run_id, task)
    append_event_func(root, run_id, "task_deleted", {"task_id": task_id})
    return task


def set_task_status(
    root: Path,
    run_id: str,
    task_id: str,
    status: str,
    exit_code: int | None = None,
    *,
    allow_terminal_transition: bool = False,
    now_func: Callable[[], str] = utc_now,
    append_event_func: Callable[[Path, str, str, dict], dict] = default_append_event,
    render_overview_func: Callable[..., object] | None = None,
) -> dict:
    now = now_func()
    should_append = True
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = _find_task(plan, task_id)
        if task.get("status") in TERMINAL_TASK_STATUSES and not allow_terminal_transition:
            should_append = False
        else:
            task["status"] = status
        if status == "running":
            if should_append:
                task["started_at"] = task.get("started_at") or now
                task["finished_at"] = None
                task["exit_code"] = None
        elif status == "awaiting_user":
            if should_append:
                task["started_at"] = task.get("started_at") or now
                task["finished_at"] = None
                task["exit_code"] = None
        elif status in TERMINAL_TASK_STATUSES:
            if should_append:
                if not task.get("started_at"):
                    task["started_at"] = now
                task["finished_at"] = now
                task["exit_code"] = exit_code
        plan["updated_at"] = now
        save_plan(root, plan)
        _write_task(root, run_id, task)
    if should_append:
        append_event_func(root, run_id, "task_status_changed", {"task_id": task_id, "status": status, "exit_code": exit_code})
        if render_overview_func and status in {"awaiting_user", *TERMINAL_TASK_STATUSES}:
            render_overview_func(root, run_id, task_id, policy="journal")
    return task
