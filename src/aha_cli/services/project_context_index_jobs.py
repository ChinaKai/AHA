from __future__ import annotations

from pathlib import Path
import threading
import uuid

from aha_cli.domain.models import utc_now
from aha_cli.services.project_context_index import build_project_context_index
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import aha_home_path


JOB_DIR = "project_context_jobs"
TERMINAL_JOB_STATUSES = {"completed", "failed", "stopped"}


def project_context_job_dir(root: Path) -> Path:
    return aha_home_path(root) / "runtime" / JOB_DIR


def project_context_job_path(root: Path, job_id: str) -> Path:
    return project_context_job_dir(root) / f"{job_id}.json"


def _public_job(job: dict) -> dict:
    public = dict(job)
    public.pop("_path", None)
    return public


def create_project_context_job(
    root: Path,
    *,
    workspace_path: str,
    project_key_value: str | None = None,
) -> dict:
    now = utc_now()
    job = {
        "id": f"pcidx_{uuid.uuid4().hex[:12]}",
        "status": "queued",
        "phase": "queued",
        "created_at": now,
        "updated_at": now,
        "workspace_path": str(Path(workspace_path).expanduser()),
        "project_key": project_key_value,
        "summary": "Project context index refresh queued",
        "stop_requested": False,
    }
    write_json(project_context_job_path(root, job["id"]), job)
    return _public_job(job)


def read_project_context_job(root: Path, job_id: str) -> dict | None:
    path = project_context_job_path(root, job_id)
    if not path.exists():
        return None
    job = read_json(path)
    job["_path"] = str(path)
    return job


def update_project_context_job(root: Path, job_id: str, **fields) -> dict:
    job = read_project_context_job(root, job_id)
    if job is None:
        raise FileNotFoundError(f"project context index job not found: {job_id}")
    job.pop("_path", None)
    job.update({key: value for key, value in fields.items() if value is not None})
    job["updated_at"] = utc_now()
    write_json(project_context_job_path(root, job_id), job)
    return _public_job(job)


def run_project_context_refresh_job(root: Path, job_id: str, *, config: dict | None = None) -> dict:
    job = read_project_context_job(root, job_id)
    if job is None:
        return {"ok": False, "error": f"project context index job not found: {job_id}"}
    if job.get("stop_requested"):
        return update_project_context_job(
            root,
            job_id,
            status="stopped",
            phase="stopped",
            completed_at=utc_now(),
            summary="Project context index refresh stopped before start",
        )
    workspace_path = str(job.get("workspace_path") or "").strip()
    try:
        update_project_context_job(
            root,
            job_id,
            status="running",
            phase="building",
            started_at=utc_now(),
            summary="Project context index refresh running",
        )
        result = build_project_context_index(
            root,
            Path(workspace_path),
            project_key_value=job.get("project_key") or None,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 - background job errors must be pollable.
        return update_project_context_job(
            root,
            job_id,
            status="failed",
            phase="failed",
            completed_at=utc_now(),
            summary="Project context index refresh failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    current = read_project_context_job(root, job_id) or {}
    if current.get("stop_requested"):
        job_result = {
            "status": result.get("status"),
            "project_key": result.get("project_key"),
            "workspace_id": result.get("workspace_id"),
            "workspace": result.get("workspace"),
            "paths": result.get("paths"),
            "counts": result.get("counts"),
        }
        return update_project_context_job(
            root,
            job_id,
            status="stopped",
            phase="stopped",
            completed_at=utc_now(),
            summary="Project context index refresh stopped after build",
            result=job_result,
        )
    job_result = {
        "status": result.get("status"),
        "project_key": result.get("project_key"),
        "workspace_id": result.get("workspace_id"),
        "workspace": result.get("workspace"),
        "paths": result.get("paths"),
        "counts": result.get("counts"),
    }
    return update_project_context_job(
        root,
        job_id,
        status="completed",
        phase="completed",
        completed_at=utc_now(),
        summary="Project context index refreshed",
        project_key=result.get("project_key"),
        workspace_id=result.get("workspace_id"),
        counts=result.get("counts"),
        paths=result.get("paths"),
        result=job_result,
    )


def _default_dispatch_project_context_job(root: Path, job_id: str, *, config: dict | None = None) -> None:
    threading.Thread(
        target=run_project_context_refresh_job,
        args=(root, job_id),
        kwargs={"config": config},
        daemon=True,
    ).start()


dispatch_project_context_job = _default_dispatch_project_context_job


def start_project_context_refresh_job(
    root: Path,
    *,
    workspace_path: str,
    project_key_value: str | None = None,
    config: dict | None = None,
) -> dict:
    job = create_project_context_job(root, workspace_path=workspace_path, project_key_value=project_key_value)
    try:
        dispatch_project_context_job(root, job["id"], config=config)
    except Exception as exc:  # noqa: BLE001 - dispatch failures should be visible to callers.
        job = update_project_context_job(
            root,
            job["id"],
            status="failed",
            phase="failed",
            completed_at=utc_now(),
            summary="Project context index refresh failed to dispatch",
            error=f"{type(exc).__name__}: {exc}",
        )
    return _public_job(read_project_context_job(root, job["id"]) or job)


def stop_project_context_refresh_job(root: Path, job_id: str) -> dict:
    job = read_project_context_job(root, job_id)
    if job is None:
        raise FileNotFoundError(f"project context index job not found: {job_id}")
    if str(job.get("status") or "") in TERMINAL_JOB_STATUSES:
        return _public_job(job) | {"stopped": False, "already_terminal": True}
    return update_project_context_job(
        root,
        job_id,
        status="stopping",
        phase="stopping",
        stop_requested=True,
        summary="Project context index refresh stop requested",
    ) | {"stopped": True}


__all__ = [
    "create_project_context_job",
    "dispatch_project_context_job",
    "project_context_job_dir",
    "project_context_job_path",
    "read_project_context_job",
    "run_project_context_refresh_job",
    "start_project_context_refresh_job",
    "stop_project_context_refresh_job",
    "update_project_context_job",
]
