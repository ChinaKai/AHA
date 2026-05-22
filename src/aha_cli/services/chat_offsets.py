from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.orchestrator import task_has_incomplete_sub_agents
from aha_cli.store.filesystem import iter_jsonl_from, read_json, task_snapshot, write_json


TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}


def safe_target_name(target: str) -> str:
    return (target or "main").replace("/", "_")


def chat_offset_path(run: Path, target: str, task_id: str | None = None) -> Path:
    target_name = safe_target_name(target)
    if task_id:
        return run / "runtime" / f"chat-offset-{safe_target_name(task_id)}-{target_name}.json"
    return run / "runtime" / f"chat-offset-{target_name}.json"


def load_chat_offset(inbox: Path, offset_file: Path, from_start: bool) -> int:
    if from_start:
        return 0
    if offset_file.exists():
        try:
            offset = int(read_json(offset_file).get("offset") or 0)
            if not inbox.exists() or offset <= inbox.stat().st_size:
                return max(0, offset)
        except (OSError, TypeError, ValueError):
            pass
    _, offset = iter_jsonl_from(inbox, 0)
    return offset


def save_chat_offset(offset_file: Path, offset: int) -> None:
    write_json(offset_file, {"offset": offset, "updated_at": utc_now()})


def worker_backend_should_exit_after_turn(
    root: Path,
    run_id: str,
    task_id: str | None,
    worker_task_id: str | None,
    inbox: Path,
    processed_offset: int,
) -> bool:
    if not task_id or not worker_task_id:
        return False
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return True
    status = str(task.get("status") or "")
    if status != "awaiting_user" and status not in TERMINAL_TASK_STATUSES:
        return False
    if task_has_incomplete_sub_agents(task):
        return False
    try:
        if inbox.exists() and inbox.stat().st_size > processed_offset:
            return False
    except OSError:
        return False
    return True


__all__ = [
    "chat_offset_path",
    "load_chat_offset",
    "safe_target_name",
    "save_chat_offset",
    "worker_backend_should_exit_after_turn",
]
