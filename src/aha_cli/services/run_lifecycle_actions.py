from __future__ import annotations

from pathlib import Path

from aha_cli.services.run_cleanup import DEFAULT_ACTIVE_HEARTBEAT_SECONDS
from aha_cli.store.filesystem import run_exists, update_run_lifecycle
from aha_cli.store.runs import require_plan


class RunLifecycleActionError(Exception):
    def __init__(self, message: str, *, reason: str, status_code: str = "400 Bad Request") -> None:
        self.reason = reason
        self.status_code = status_code
        super().__init__(message)


def run_has_running_work(root: Path, run_id: str) -> bool:
    try:
        plan = require_plan(root, run_id)
    except SystemExit:
        return False
    for task in plan.get("tasks", []):
        if task.get("deleted_at"):
            continue
        if str(task.get("status") or "").lower() == "running":
            return True
        if any(str(agent.get("status") or "").lower() == "running" for agent in task.get("agents", [])):
            return True
    return False


def set_run_lifecycle_status(
    root: Path,
    run_id: str,
    status: object,
    *,
    current_run_id: str | None = None,
    active_heartbeat_seconds: int = DEFAULT_ACTIVE_HEARTBEAT_SECONDS,
    now: float | None = None,
) -> dict:
    selected_run_id = str(run_id or "").strip()
    if not selected_run_id or not run_exists(root, selected_run_id):
        raise RunLifecycleActionError(f"Run not found: {selected_run_id or '-'}", reason="run_not_found", status_code="404 Not Found")
    if run_has_running_work(root, selected_run_id):
        raise RunLifecycleActionError(
            "Cannot change lifecycle for a run with running tasks",
            reason="running_work",
            status_code="409 Conflict",
        )
    return update_run_lifecycle(root, selected_run_id, status)
