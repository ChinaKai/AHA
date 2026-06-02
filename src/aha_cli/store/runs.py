from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
import threading

from aha_cli.constants import PLAN_FILE, RUNS_DIR
from aha_cli.domain.models import enrich_plan, utc_now
from aha_cli.domain.run_lifecycle import apply_run_lifecycle_status, run_lifecycle_projection
from aha_cli.services.proxy import backend_proxy_config
from aha_cli.store.config import load_config
from aha_cli.store.events import append_event
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import aha_home_path, plan_path, run_dir

_PLAN_LOCK = threading.RLock()


@contextmanager
def locked_plan(root: Path, run_id: str):
    lock_path = run_dir(root, run_id) / "runtime" / "plan.lock"
    with _PLAN_LOCK:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def require_plan(root: Path, run_id: str) -> dict:
    path = plan_path(root, run_id)
    if not path.exists():
        raise SystemExit(f"Run not found: {run_id}")
    return enrich_plan(read_json(path), load_config(root).get("backend", "codex"))


def save_plan(root: Path, plan: dict) -> None:
    write_json(plan_path(root, plan["id"]), plan)


def latest_run_id(root: Path) -> str | None:
    runs = aha_home_path(root) / RUNS_DIR
    if not runs.is_dir():
        return None
    candidates = sorted(p.name for p in runs.iterdir() if (p / PLAN_FILE).exists())
    return candidates[-1] if candidates else None


def run_exists(root: Path, run_id: str) -> bool:
    return bool(run_id) and plan_path(root, run_id).exists()


def run_summary_from_plan(root: Path, plan: dict) -> dict:
    cfg = load_config(root)
    tasks = [task for task in plan.get("tasks", []) if not task.get("deleted_at")]
    lifecycle = run_lifecycle_projection(plan)
    completed = sum(1 for task in tasks if task.get("status") == "completed")
    failed = any(task.get("status") == "failed" for task in tasks)
    blocked = any(task.get("status") == "blocked" for task in tasks)
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
        "hidden_count": sum(1 for task in tasks if task.get("hidden")),
        "lifecycle": lifecycle,
        "lifecycle_status": lifecycle["status"],
        "hidden": lifecycle["hidden"],
        "hidden_at": lifecycle["hidden_at"],
        "archived": lifecycle["archived"],
        "archived_at": lifecycle["archived_at"],
        "proxy": backend_proxy_config(cfg, cfg.get("backend"), plan),
        "path": str(plan_path(root, plan["id"])),
    }


def run_summary(root: Path, run_id: str) -> dict:
    plan = enrich_plan(read_json(plan_path(root, run_id)), load_config(root).get("backend", "codex"))
    return run_summary_from_plan(root, plan)


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


def list_run_summaries(root: Path) -> list[dict]:
    runs = aha_home_path(root) / RUNS_DIR
    if not runs.is_dir():
        return []
    summaries: list[dict] = []
    for path in sorted(runs.glob(f"*/{PLAN_FILE}"), reverse=True):
        try:
            plan = enrich_plan(read_json(path), load_config(root).get("backend", "codex"))
            summaries.append(run_summary_from_plan(root, plan))
        except (OSError, ValueError, KeyError):
            continue
    return summaries


def resolve_run_id(root: Path, run_id: str | None) -> str:
    if run_id:
        return run_id
    latest = latest_run_id(root)
    if not latest:
        raise SystemExit("No runs found")
    return latest
