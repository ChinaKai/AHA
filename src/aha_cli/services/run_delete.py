from __future__ import annotations

from pathlib import Path
import shutil

from aha_cli.constants import PLAN_FILE
from aha_cli.services.run_cleanup import DEFAULT_ACTIVE_HEARTBEAT_SECONDS, run_has_active_heartbeat
from aha_cli.store.paths import aha_home_path, run_dir


class RunDeleteError(Exception):
    def __init__(self, message: str, *, reason: str, status_code: str = "400 Bad Request") -> None:
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


def _validate_run_id(run_id: str) -> str:
    value = str(run_id or "").strip()
    if not value:
        raise ValueError("run id is required")
    if value in {".", ".."} or "/" in value or "\\" in value or Path(value).name != value:
        raise ValueError(f"invalid run id: {run_id}")
    return value


def delete_run(
    root: Path,
    run_id: str,
    *,
    current_run_id: str | None = None,
    force: bool = False,
    active_heartbeat_seconds: int = DEFAULT_ACTIVE_HEARTBEAT_SECONDS,
) -> dict:
    selected_run_id = _validate_run_id(run_id)
    if current_run_id and selected_run_id == current_run_id:
        raise RunDeleteError(
            "Cannot delete the current run",
            reason="current_run",
            status_code="409 Conflict",
        )

    run_path = run_dir(root, selected_run_id)
    home = aha_home_path(root)
    try:
        resolved_path = run_path.resolve()
        runs_root = (home / "runs").resolve()
    except OSError as exc:
        raise RunDeleteError(str(exc), reason="path_error") from exc
    if runs_root not in [resolved_path, *resolved_path.parents]:
        raise ValueError(f"invalid run path: {run_path}")
    if not run_path.exists():
        raise RunDeleteError(
            f"Run not found: {selected_run_id}",
            reason="not_found",
            status_code="404 Not Found",
        )
    if not run_path.is_dir():
        raise RunDeleteError(
            f"Run path is not a directory: {run_path}",
            reason="not_directory",
            status_code="409 Conflict",
        )
    if not force and run_has_active_heartbeat(run_path, active_heartbeat_seconds=active_heartbeat_seconds):
        raise RunDeleteError(
            "Cannot delete a run with active heartbeat",
            reason="active_heartbeat",
            status_code="409 Conflict",
        )

    had_plan = (run_path / PLAN_FILE).exists()
    try:
        shutil.rmtree(run_path)
    except OSError as exc:
        raise RunDeleteError(str(exc), reason="delete_failed") from exc
    return {
        "kind": "run",
        "run_id": selected_run_id,
        "path": str(run_path),
        "action": "deleted",
        "reason": "forced" if force else "requested",
        "had_plan": had_plan,
    }


__all__ = ["RunDeleteError", "delete_run"]
