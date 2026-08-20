from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
import uuid

from aha_cli import locking
from aha_cli.constants import RUNS_DIR
from aha_cli.domain.models import enrich_plan, make_agent, utc_now
from aha_cli.domain.run_lifecycle import apply_run_lifecycle_status, run_lifecycle_projection
from aha_cli.services.proxy import backend_proxy_config, normalize_proxy_config
from aha_cli.store.config import load_config
from aha_cli.store.events import append_event
from aha_cli.store.io import (
    exclusive_sidecar_lock,
    iter_jsonl_reverse,
    json_backup_path,
    read_json,
    write_json,
)
from aha_cli.store.paths import aha_home_path, plan_path, run_dir

_PLAN_LOCK = threading.RLock()
_PLAN_LOCK_STATE = threading.local()
_PLAN_SIDECAR_LOCK = "plan.write.lock"


@contextmanager
def locked_plan(root: Path, run_id: str):
    lock_path = run_dir(root, run_id) / "runtime" / "plan.lock"
    sidecar_path = lock_path.with_name(_PLAN_SIDECAR_LOCK)
    key = str(sidecar_path.resolve())
    with _PLAN_LOCK:
        depths = getattr(_PLAN_LOCK_STATE, "depths", None)
        if depths is None:
            depths = {}
            _PLAN_LOCK_STATE.depths = depths
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as lock_file:
            locking.acquire(lock_file.fileno())
            try:
                with exclusive_sidecar_lock(sidecar_path):
                    depths[key] = 1
                    try:
                        yield
                    finally:
                        depths.pop(key, None)
            finally:
                locking.release(lock_file.fileno())


def _read_plan_candidate(path: Path, run_id: str) -> dict | None:
    try:
        plan = read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(plan, dict) or str(plan.get("id") or "") != run_id:
        return None
    return plan


def _recovery_stamp(timestamp: str) -> str:
    digits = "".join(character for character in timestamp if character.isdigit())
    return f"{digits[:14]}-{uuid.uuid4().hex[:8]}Z"


def _task_snapshots(root: Path, run_id: str) -> list[dict]:
    tasks_dir = run_dir(root, run_id) / "tasks"
    if not tasks_dir.is_dir():
        return []
    tasks: list[dict] = []
    for path in sorted(tasks_dir.glob("task-*/task.json")):
        try:
            task = read_json(path)
        except (OSError, ValueError):
            continue
        if isinstance(task, dict) and str(task.get("id") or "") == path.parent.name:
            tasks.append(task)
    return tasks


def _plan_event_metadata(root: Path, run_id: str) -> dict:
    metadata: dict = {}
    events_path = run_dir(root, run_id) / "events.jsonl"
    for _offset, event in iter_jsonl_reverse(events_path) or ():
        event_type = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        timestamp = str(event.get("ts") or "")
        if timestamp and not metadata.get("updated_at"):
            metadata["updated_at"] = timestamp
        if event_type == "run_renamed" and not metadata.get("goal"):
            metadata["goal"] = str(data.get("name") or "")
        elif event_type == "run_lifecycle_updated" and not metadata.get("lifecycle_status"):
            metadata["lifecycle_status"] = str(data.get("status") or "")
            metadata["lifecycle_at"] = timestamp
        elif event_type == "run_selected_task_updated" and "selected_task_id" not in metadata:
            metadata["selected_task_id"] = str(data.get("selected_task_id") or "")
        elif event_type == "plan_created":
            metadata.setdefault("goal", str(data.get("goal") or ""))
            metadata.setdefault("mode", str(data.get("mode") or "research"))
            metadata.setdefault("created_at", timestamp)
            metadata.setdefault("proxy_enabled", bool(data.get("proxy_enabled")))
    return metadata


def _snapshot_timestamp(tasks: list[dict], *fields: str, latest: bool = True) -> str:
    values = [
        str(item.get(field) or "")
        for item in tasks
        for field in fields
        if str(item.get(field) or "")
    ]
    if not values:
        return ""
    return max(values) if latest else min(values)


def _reconstruct_plan(
    root: Path,
    run_id: str,
    tasks: list[dict],
    recovered_at: str,
    metadata: dict | None = None,
) -> dict:
    metadata = metadata or _plan_event_metadata(root, run_id)
    cfg = load_config(root)
    first_task = tasks[0] if tasks else {}
    backend = str(first_task.get("preferred_backend") or cfg.get("backend") or "codex")
    workspace_path = str(first_task.get("workspace_path") or root)
    created_at = (
        str(metadata.get("created_at") or "")
        or _snapshot_timestamp(tasks, "created_at", latest=False)
        or recovered_at
    )
    updated_at = (
        str(metadata.get("updated_at") or "")
        or _snapshot_timestamp(tasks, "last_final_at", "finished_at", "started_at", "created_at")
        or recovered_at
    )
    plan = {
        "id": run_id,
        "goal": str(metadata.get("goal") or run_id),
        "mode": str(metadata.get("mode") or "research"),
        "created_at": created_at,
        "updated_at": updated_at,
        "write_scopes": [],
        "proxy": normalize_proxy_config(bool(metadata.get("proxy_enabled"))),
        "main_agent": make_agent(
            "main",
            "run-main",
            backend,
            status="active",
            workspace_path=workspace_path,
        ),
        "tasks": tasks,
        "recovery": {
            "source": "task_snapshots",
            "recovered_at": recovered_at,
            "task_snapshots": len(tasks),
        },
    }
    lifecycle_status = str(metadata.get("lifecycle_status") or "")
    if lifecycle_status:
        apply_run_lifecycle_status(
            plan,
            lifecycle_status,
            timestamp=str(metadata.get("lifecycle_at") or recovered_at),
        )
    selected_task_id = str(metadata.get("selected_task_id") or "")
    if selected_task_id and any(str(task.get("id") or "") == selected_task_id for task in tasks):
        plan["ui"] = {"selected_task_id": selected_task_id}
    return plan


def _preserve_invalid_plan(path: Path, recovered_at: str) -> str:
    if not path.exists():
        return ""
    recovery_dir = path.parent / "recovery"
    recovery_dir.mkdir(parents=True, exist_ok=True)
    target = recovery_dir / f"plan.invalid-{_recovery_stamp(recovered_at)}.json"
    try:
        target.write_bytes(path.read_bytes())
    except OSError:
        return ""
    return str(target.relative_to(path.parent))


def _append_plan_recovered_event(root: Path, run_id: str, data: dict, recovered_at: str) -> None:
    try:
        append_event(root, run_id, "plan_recovered", data, ts=recovered_at)
    except OSError:
        pass


def _recover_plan_unlocked(root: Path, run_id: str) -> dict | None:
    path = plan_path(root, run_id)
    existing = _read_plan_candidate(path, run_id)
    if existing is not None:
        return existing
    recovered_at = utc_now()
    invalid_path = _preserve_invalid_plan(path, recovered_at)
    backup = json_backup_path(path)
    backup_plan = _read_plan_candidate(backup, run_id)
    if backup_plan is not None:
        recovered = dict(backup_plan)
        tasks = _task_snapshots(root, run_id)
        if tasks:
            recovered["tasks"] = tasks
        recovered["recovery"] = {
            "source": "plan_backup",
            "recovered_at": recovered_at,
            "backup_path": backup.name,
            "task_snapshots": len(tasks),
        }
        write_json(path, recovered, verify=True)
        _append_plan_recovered_event(
            root,
            run_id,
            {
                "source": "plan_backup",
                "backup_path": backup.name,
                "invalid_plan_path": invalid_path,
            },
            recovered_at,
        )
        return recovered
    tasks = _task_snapshots(root, run_id)
    metadata = _plan_event_metadata(root, run_id)
    if not tasks and not metadata.get("created_at"):
        return None
    recovered = _reconstruct_plan(root, run_id, tasks, recovered_at, metadata)
    source = "task_snapshots" if tasks else "durable_events"
    recovered["recovery"]["source"] = source
    recovery_dir = path.parent / "recovery"
    recovery_dir.mkdir(parents=True, exist_ok=True)
    artifact = recovery_dir / f"plan.reconstructed-{_recovery_stamp(recovered_at)}.json"
    write_json(artifact, recovered, verify=True)
    write_json(path, recovered, verify=True)
    _append_plan_recovered_event(
        root,
        run_id,
        {
            "source": source,
            "task_snapshots": len(tasks),
            "backup_path": str(artifact.relative_to(path.parent)),
            "invalid_plan_path": invalid_path,
            "goal": recovered.get("goal"),
        },
        recovered_at,
    )
    return recovered


def _plan_recoverable(root: Path, run_id: str) -> bool:
    """Non-mutating check for whether a plan can be reconstructed if missing."""
    path = plan_path(root, run_id)
    if _read_plan_candidate(path, run_id) is not None:
        return True
    if _read_plan_candidate(json_backup_path(path), run_id) is not None:
        return True
    if _task_snapshots(root, run_id):
        return True
    return bool(_plan_event_metadata(root, run_id).get("created_at"))


def recover_plan(root: Path, run_id: str) -> dict | None:
    path = plan_path(root, run_id)
    existing = _read_plan_candidate(path, run_id)
    if existing is not None:
        return existing
    if not _plan_recoverable(root, run_id):
        return None
    with locked_plan(root, run_id):
        return _recover_plan_unlocked(root, run_id)


def require_plan(root: Path, run_id: str) -> dict:
    path = plan_path(root, run_id)
    plan = _read_plan_candidate(path, run_id)
    if plan is None:
        plan = recover_plan(root, run_id)
    if plan is None:
        raise SystemExit(f"Run not found or plan is not recoverable: {run_id}")
    return enrich_plan(plan, load_config(root).get("backend", "codex"))


def save_plan(root: Path, plan: dict) -> None:
    with locked_plan(root, str(plan["id"])):
        write_json(plan_path(root, plan["id"]), plan, backup=True, verify=True)


def latest_run_id(root: Path) -> str | None:
    candidates = sorted(str(summary.get("id") or "") for summary in list_run_summaries(root))
    return candidates[-1] if candidates else None


def run_exists(root: Path, run_id: str) -> bool:
    if not run_id or not run_dir(root, run_id).is_dir():
        return False
    # Predicate only: do not trigger a recovery write as a side effect of a
    # guard check (e.g. lifecycle/retention flows call run_exists before acting).
    return _plan_recoverable(root, run_id)


def run_summary_from_plan(root: Path, plan: dict, config: dict | None = None) -> dict:
    cfg = config if isinstance(config, dict) else load_config(root)
    tasks = [task for task in plan.get("tasks", []) if not task.get("deleted_at")]
    lifecycle = run_lifecycle_projection(plan)
    ui = plan.get("ui") if isinstance(plan.get("ui"), dict) else {}
    completed = sum(1 for task in tasks if task.get("status") == "completed")
    failed = any(task.get("status") == "failed" for task in tasks)
    blocked = any(task.get("status") == "blocked" for task in tasks)
    running_task_count = sum(1 for task in tasks if task.get("status") == "running")
    running_agent_count = sum(
        1
        for task in tasks
        for agent in task.get("agents", [])
        if agent.get("status") == "running"
    )
    running = any(task.get("status") in {"running", "awaiting_user"} for task in tasks)
    if failed:
        status = "failed"
    elif blocked:
        status = "blocked"
    elif tasks and completed == len(tasks):
        status = "completed"
    elif running:
        status = "running"
    else:
        status = "pending"
    return {
        "id": plan["id"],
        "goal": plan.get("goal", ""),
        "mode": plan.get("mode", ""),
        "status": status,
        "created_at": plan.get("created_at"),
        "updated_at": plan.get("updated_at"),
        "task_count": len(tasks),
        "completed_count": completed,
        "running_task_count": running_task_count,
        "running_agent_count": running_agent_count,
        "has_running_work": bool(running_task_count or running_agent_count),
        "hidden_count": sum(1 for task in tasks if task.get("hidden")),
        "lifecycle": lifecycle,
        "lifecycle_status": lifecycle["status"],
        "hidden": lifecycle["hidden"],
        "hidden_at": lifecycle["hidden_at"],
        "archived": lifecycle["archived"],
        "archived_at": lifecycle["archived_at"],
        "selected_task_id": str(ui.get("selected_task_id") or ""),
        "system_managed": bool(plan.get("system_managed")),
        "system_purpose": str(plan.get("system_purpose") or ""),
        "proxy": backend_proxy_config(cfg, cfg.get("backend"), plan),
        "path": str(plan_path(root, plan["id"])),
    }


def run_summary(root: Path, run_id: str) -> dict:
    cfg = load_config(root)
    plan = require_plan(root, run_id)
    return run_summary_from_plan(root, plan, cfg)


def update_run_lifecycle(root: Path, run_id: str, status: object) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        previous = run_lifecycle_projection(plan)["status"]
        now = utc_now()
        lifecycle = apply_run_lifecycle_status(plan, status, timestamp=now)
        plan["updated_at"] = now
        save_plan(root, plan)
        append_event(
            root,
            run_id,
            "run_lifecycle_updated",
            {
                "previous_status": previous,
                "status": lifecycle["status"],
            },
        )
        return run_summary_from_plan(root, plan)


def update_run_selected_task(root: Path, run_id: str, task_id: object) -> dict:
    selected = str(task_id or "").strip()
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        if selected:
            task = next(
                (
                    item
                    for item in plan.get("tasks", [])
                    if str(item.get("id") or "") == selected and not item.get("deleted_at")
                ),
                None,
            )
            if task is None:
                raise ValueError(f"task not found: {selected}")
        ui = plan.setdefault("ui", {})
        if selected:
            ui["selected_task_id"] = selected
        else:
            ui.pop("selected_task_id", None)
        now = utc_now()
        plan["updated_at"] = now
        save_plan(root, plan)
        append_event(
            root,
            run_id,
            "run_selected_task_updated",
            {"selected_task_id": selected},
        )
        return run_summary_from_plan(root, plan)


def list_run_summaries(root: Path) -> list[dict]:
    runs = aha_home_path(root) / RUNS_DIR
    if not runs.is_dir():
        return []
    cfg = load_config(root)
    backend = cfg.get("backend", "codex")
    summaries: list[dict] = []
    for path in sorted((path for path in runs.iterdir() if path.is_dir()), reverse=True):
        try:
            plan = recover_plan(root, path.name)
            if plan is None:
                continue
            plan = enrich_plan(plan, backend)
            summaries.append(run_summary_from_plan(root, plan, cfg))
        except (OSError, TimeoutError, ValueError, KeyError):
            continue
    return summaries


def resolve_run_id(root: Path, run_id: str | None) -> str:
    if run_id:
        return run_id
    latest = latest_run_id(root)
    if not latest:
        raise SystemExit("No runs found")
    return latest
