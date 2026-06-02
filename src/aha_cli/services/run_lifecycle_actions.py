from __future__ import annotations

from pathlib import Path

from aha_cli.services.run_cleanup import DEFAULT_ACTIVE_HEARTBEAT_SECONDS, run_has_active_heartbeat
from aha_cli.store.filesystem import run_dir, run_exists, update_run_lifecycle


class RunLifecycleActionError(Exception):
    def __init__(self, message: str, *, reason: str, status_code: str = "400 Bad Request") -> None:
        self.reason = reason
        self.status_code = status_code
        super().__init__(message)


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
    if current_run_id and selected_run_id == current_run_id:
        raise RunLifecycleActionError(
            "Cannot change lifecycle for the current run",
            reason="current_run",
            status_code="409 Conflict",
        )
    if run_has_active_heartbeat(run_dir(root, selected_run_id), now=now, active_heartbeat_seconds=active_heartbeat_seconds):
        raise RunLifecycleActionError(
            "Cannot change lifecycle for a run with active heartbeat",
            reason="active_heartbeat",
            status_code="409 Conflict",
        )
    return update_run_lifecycle(root, selected_run_id, status)
