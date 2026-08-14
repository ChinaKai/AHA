from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from aha_cli import locking
from aha_cli.domain.models import utc_now
from aha_cli.services.subagent_state import task_has_incomplete_sub_agents
from aha_cli.store.filesystem import iter_jsonl_from, read_json, task_snapshot, write_json
from aha_cli.store.paths import inbox_path, run_dir


TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}


def safe_target_name(target: str) -> str:
    return (target or "main").replace("/", "_")


def chat_offset_path(run: Path, target: str, task_id: str | None = None) -> Path:
    target_name = safe_target_name(target)
    if task_id:
        return run / "runtime" / f"chat-offset-{safe_target_name(task_id)}-{target_name}.json"
    return run / "runtime" / f"chat-offset-{target_name}.json"


def chat_consumer_lock_path(run: Path, target: str, task_id: str | None = None) -> Path:
    target_name = safe_target_name(target)
    task_name = f"{safe_target_name(task_id)}-" if task_id else ""
    return run / "runtime" / f"chat-consumer-{task_name}{target_name}.lock"


def acquire_chat_consumer(run: Path, target: str, task_id: str | None = None) -> int | None:
    """Claim the single durable inbox consumer for one task/agent worker."""

    path = chat_consumer_lock_path(run, target, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        locking.acquire(handle, blocking=False)
    except BlockingIOError:
        os.close(handle)
        return None
    return handle


def release_chat_consumer(handle: int | None) -> None:
    if handle is None:
        return
    try:
        locking.release(handle)
    finally:
        os.close(handle)


def chat_turn_checkpoint_path(run: Path, target: str, task_id: str | None = None) -> Path:
    target_name = safe_target_name(target)
    task_name = f"{safe_target_name(task_id)}-" if task_id else ""
    return run / "runtime" / f"chat-turn-{task_name}{target_name}.json"


def chat_turn_identity(item_offset: int, item: dict) -> str:
    payload = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:24]
    return f"{max(0, int(item_offset))}:{digest}"


def load_chat_turn_checkpoint(path: Path, item_offset: int, item: dict) -> dict | None:
    if not path.exists():
        return None
    try:
        checkpoint = read_json(path)
    except (OSError, ValueError):
        return None
    if checkpoint.get("identity") != chat_turn_identity(item_offset, item):
        return None
    if checkpoint.get("phase") not in {"prepared", "executed", "finished"}:
        return None
    return checkpoint


def chat_turn_result_recoverable(checkpoint: dict | None, backend: str, model: str | None = None) -> bool:
    if not isinstance(checkpoint, dict) or checkpoint.get("phase") != "executed":
        return False
    try:
        exit_code = int(checkpoint.get("exit_code"))
    except (TypeError, ValueError):
        exit_code = None
    if exit_code != 0:
        # Only successful executed turns carry recoverable side effects. A failed
        # turn (e.g. backend refused an over-long prompt) has nothing to preserve,
        # and recovering it would make a reopen hit the old failure immediately.
        return False
    if not str(checkpoint.get("reply") or "").strip():
        # A successful process exit without a deliverable reply is not a
        # recoverable turn result. Older backend completion handling could leave
        # this shape behind after mistaking a background-task notification for
        # the real turn boundary; replaying it makes every reopen fail again.
        return False
    expected_backend = str(backend or "").removesuffix("-chat").strip().lower()
    checkpoint_backend = str(checkpoint.get("backend") or "").removesuffix("-chat").strip().lower()
    if not checkpoint_backend:
        prompt_event = checkpoint.get("prompt_event") if isinstance(checkpoint.get("prompt_event"), dict) else {}
        prompt_data = prompt_event.get("data") if isinstance(prompt_event.get("data"), dict) else {}
        checkpoint_backend = str(prompt_data.get("source") or "").removesuffix("-chat").strip().lower()
    if checkpoint_backend and checkpoint_backend != expected_backend:
        return False
    expected_model = str(model or "").strip()
    checkpoint_model = str(checkpoint.get("model") or "").strip()
    return not checkpoint_model or not expected_model or checkpoint_model == expected_model


def reset_task_chat_for_reopen(root: Path, run_id: str, task: dict) -> dict:
    """Discard pre-reopen inbox work and stale checkpoints for every task agent."""

    run = run_dir(root, run_id)
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise ValueError("task id is required to reset chat state")
    agents = task.get("agents") if isinstance(task.get("agents"), list) else []
    targets = {
        str(agent.get("id") or "").strip()
        for agent in agents
        if isinstance(agent, dict) and str(agent.get("id") or "").strip()
    }
    targets.add("main")
    boundaries: list[dict] = []
    for target in sorted(targets):
        inbox = inbox_path(root, run_id, target, task_id)
        try:
            boundary = inbox.stat().st_size if inbox.exists() else 0
        except OSError:
            boundary = 0
        offset_file = chat_offset_path(run, target, task_id)
        _write_chat_offset(offset_file, boundary, monotonic=False)

        checkpoint_file = chat_turn_checkpoint_path(run, target, task_id)
        checkpoint_discarded = False
        if checkpoint_file.exists():
            try:
                checkpoint = read_json(checkpoint_file)
            except (OSError, ValueError):
                checkpoint = {}
            if checkpoint.get("phase") in {"prepared", "executed"}:
                checkpoint["phase"] = "discarded"
                checkpoint["discarded_at"] = utc_now()
                checkpoint["discard_reason"] = "task_reopened"
                checkpoint["reopen_boundary_offset"] = boundary
                checkpoint["updated_at"] = utc_now()
                write_json(checkpoint_file, checkpoint)
                checkpoint_discarded = True
        boundaries.append(
            {
                "target": target,
                "offset": boundary,
                "checkpoint_discarded": checkpoint_discarded,
            }
        )
    return {"task_id": task_id, "boundaries": boundaries}


def load_prepared_chat_turn(path: Path, source_offset: int) -> dict | None:
    if not path.exists():
        return None
    try:
        checkpoint = read_json(path)
    except (OSError, ValueError):
        return None
    item = checkpoint.get("item") if isinstance(checkpoint.get("item"), dict) else None
    try:
        prepared_source_offset = int(checkpoint.get("source_offset"))
    except (TypeError, ValueError):
        return None
    if item is None or prepared_source_offset != max(0, int(source_offset)):
        return None
    try:
        item_offset = max(0, int(checkpoint.get("item_offset") or 0))
    except (TypeError, ValueError):
        return None
    if checkpoint.get("phase") not in {"prepared", "executed", "finished"}:
        return None
    if checkpoint.get("identity") != chat_turn_identity(item_offset, item):
        return None
    return checkpoint


def save_chat_turn_preparation(
    path: Path,
    source_offset: int,
    item_offset: int,
    item: dict,
) -> dict:
    checkpoint = {
        "version": 1,
        "identity": chat_turn_identity(item_offset, item),
        "source_offset": max(0, int(source_offset)),
        "item_offset": max(0, int(item_offset)),
        "item": json.loads(json.dumps(item, ensure_ascii=False, default=str)),
        "phase": "prepared",
        "prepared_at": utc_now(),
        "updated_at": utc_now(),
    }
    write_json(path, checkpoint)
    return checkpoint


def save_chat_turn_result(
    path: Path,
    item_offset: int,
    item: dict,
    *,
    exit_code: int,
    reply: str,
    prompt_metrics: dict | None = None,
    prompt_event: dict | None = None,
    git_before: dict | None = None,
    backend: str | None = None,
    model: str | None = None,
) -> dict:
    prepared: dict = {}
    if path.exists():
        try:
            candidate = read_json(path)
        except (OSError, ValueError):
            candidate = {}
        if candidate.get("identity") == chat_turn_identity(item_offset, item):
            prepared = {
                key: candidate[key]
                for key in ("source_offset", "item")
                if key in candidate
            }
    checkpoint = {
        "version": 1,
        **prepared,
        "identity": chat_turn_identity(item_offset, item),
        "item_offset": max(0, int(item_offset)),
        "phase": "executed",
        "exit_code": int(exit_code),
        "reply": str(reply or ""),
        "prompt_metrics": dict(prompt_metrics or {}),
        "prompt_event": dict(prompt_event or {}),
        "git_before": dict(git_before or {}),
        **({"backend": str(backend).removesuffix("-chat")} if backend else {}),
        **({"model": str(model)} if model else {}),
        "executed_at": utc_now(),
        "updated_at": utc_now(),
    }
    write_json(path, checkpoint)
    return checkpoint


def finish_chat_turn(path: Path, item_offset: int, item: dict) -> dict | None:
    checkpoint = load_chat_turn_checkpoint(path, item_offset, item)
    if checkpoint is None:
        return None
    if checkpoint.get("phase") != "finished":
        checkpoint["phase"] = "finished"
        checkpoint["finished_at"] = utc_now()
        checkpoint["updated_at"] = utc_now()
        write_json(path, checkpoint)
    return checkpoint


def save_chat_turn_actions(path: Path, item_offset: int, item: dict, executed: list[dict]) -> dict | None:
    checkpoint = load_chat_turn_checkpoint(path, item_offset, item)
    if checkpoint is None:
        return None
    checkpoint["actions_applied"] = True
    checkpoint["executed_actions"] = json.loads(json.dumps(executed, ensure_ascii=False, default=str))
    checkpoint["actions_applied_at"] = utc_now()
    checkpoint["updated_at"] = utc_now()
    write_json(path, checkpoint)
    return checkpoint


def complete_chat_turn(path: Path, offset_file: Path, item_offset: int, item: dict) -> None:
    """Commit completion before advancing the cursor so either write can recover."""

    finish_chat_turn(path, item_offset, item)
    save_chat_offset(offset_file, item_offset)


def _write_chat_offset(offset_file: Path, offset: int, *, monotonic: bool) -> None:
    lock_path = offset_file.with_suffix(f"{offset_file.suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        locking.acquire(lock_file.fileno())
        try:
            current = 0
            if monotonic and offset_file.exists():
                try:
                    current = max(0, int(read_json(offset_file).get("offset") or 0))
                except (OSError, TypeError, ValueError):
                    current = 0
            write_json(offset_file, {"offset": max(current, int(offset)), "updated_at": utc_now()})
        finally:
            locking.release(lock_file.fileno())


def load_chat_offset(inbox: Path, offset_file: Path, from_start: bool) -> int:
    if from_start:
        return 0
    if offset_file.exists():
        try:
            offset = int(read_json(offset_file).get("offset") or 0)
            inbox_size = inbox.stat().st_size if inbox.exists() else 0
            if offset <= inbox_size:
                return max(0, offset)
        except (OSError, TypeError, ValueError):
            pass
    _, offset = iter_jsonl_from(inbox, 0)
    _write_chat_offset(offset_file, offset, monotonic=False)
    return offset


def save_chat_offset(offset_file: Path, offset: int) -> None:
    """Persist a monotonic consumer cursor without allowing stale workers to rewind it."""

    _write_chat_offset(offset_file, offset, monotonic=True)


def advance_chat_offset_to_inbox_end(root: Path, run_id: str, target: str, task_id: str | None = None) -> None:
    """Advance a chat cursor past every message already in the inbox.

    This is the interrupt/recover semantic: messages delivered to the inbox
    before a backend stopped must not be re-read by the next backend start,
    which would replay the interrupted turn and skip the user's newer follow-up.
    """
    offset_file = chat_offset_path(run_dir(root, run_id), target, task_id)
    inbox = inbox_path(root, run_id, target, task_id)
    save_chat_offset(offset_file, inbox.stat().st_size if inbox.exists() else 0)


def worker_backend_should_exit_after_turn(
    root: Path,
    run_id: str,
    task_id: str | None,
    worker_task_id: str | None,
    inbox: Path,
    processed_offset: int,
    *,
    target: str = "main",
) -> bool:
    if not task_id or not worker_task_id:
        return False
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return True
    status = str(task.get("status") or "")
    main_waiting = False
    if str(target or "main") == "main" and status == "running":
        agents = task.get("agents") if isinstance(task.get("agents"), list) else []
        main = next((agent for agent in agents if str(agent.get("id") or "") == "main"), None)
        waiting_reason = str((main or {}).get("waiting_reason") or "").lower()
        main_waiting = (
            str((main or {}).get("status") or "").lower() == "waiting"
            and waiting_reason in {"host", "subagents"}
        )
    if not main_waiting and status != "awaiting_user" and status not in TERMINAL_TASK_STATUSES:
        return False
    if not main_waiting and task_has_incomplete_sub_agents(task):
        return False
    try:
        if inbox.exists() and inbox.stat().st_size > processed_offset:
            return False
    except OSError:
        return False
    return True


__all__ = [
    "acquire_chat_consumer",
    "advance_chat_offset_to_inbox_end",
    "chat_consumer_lock_path",
    "chat_offset_path",
    "chat_turn_checkpoint_path",
    "chat_turn_identity",
    "chat_turn_result_recoverable",
    "complete_chat_turn",
    "finish_chat_turn",
    "load_chat_offset",
    "load_prepared_chat_turn",
    "load_chat_turn_checkpoint",
    "release_chat_consumer",
    "reset_task_chat_for_reopen",
    "safe_target_name",
    "save_chat_offset",
    "save_chat_turn_actions",
    "save_chat_turn_preparation",
    "save_chat_turn_result",
    "worker_backend_should_exit_after_turn",
]
