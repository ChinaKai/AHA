from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from aha_cli.domain.models import make_task_round, utc_now
from aha_cli.store.io import iter_jsonl_from, read_json, write_json
from aha_cli.store.paths import run_dir
from aha_cli.store.runs import locked_plan, require_plan, save_plan


def round_sequence_from_id(round_id: object) -> int | None:
    text = str(round_id or "")
    if text.startswith("round-"):
        try:
            return int(text.split("-", 1)[1])
        except ValueError:
            return None
    return None


def task_lifecycle_rounds_dir(root: Path, run_id: str, task_id: str) -> Path:
    return run_dir(root, run_id) / "tasks" / task_id / "rounds"


def task_lifecycle_round_dir(root: Path, run_id: str, task_id: str, round_id: str) -> Path:
    return task_lifecycle_rounds_dir(root, run_id, task_id) / round_id


def task_lifecycle_round_path(root: Path, run_id: str, task_id: str, round_id: str) -> Path:
    return task_lifecycle_round_dir(root, run_id, task_id, round_id) / "round.json"


def task_round_final_path(root: Path, run_id: str, task_id: str, round_id: str) -> Path:
    return task_lifecycle_round_dir(root, run_id, task_id, round_id) / "final.md"


def task_round_final_meta_path(root: Path, run_id: str, task_id: str, round_id: str) -> Path:
    return task_round_final_path(root, run_id, task_id, round_id).with_suffix(".meta.json")


def task_round_started_at(task: dict, *, now_func: Callable[[], str] = utc_now) -> str:
    return str(task.get("started_at") or task.get("created_at") or now_func())


def ensure_task_round_record(root: Path, run_id: str, task: dict, *, now_func: Callable[[], str] = utc_now) -> dict:
    task_id = str(task["id"])
    sequence = int(task.get("round_sequence") or round_sequence_from_id(task.get("current_round_id")) or 1)
    round_id = str(task.get("current_round_id") or f"round-{sequence:03d}")
    sequence = round_sequence_from_id(round_id) or sequence
    task["current_round_id"] = round_id
    task["round_sequence"] = sequence
    task.setdefault("last_final_round_id", None)
    task.setdefault("last_final_at", None)

    path = task_lifecycle_round_path(root, run_id, task_id, round_id)
    if path.exists():
        record = read_json(path)
        changed = False
        for key, value in {"task_id": task_id, "round_id": round_id, "sequence": sequence}.items():
            if record.get(key) != value:
                record[key] = value
                changed = True
        record.setdefault("status", "active")
        record.setdefault("started_at", task_round_started_at(task, now_func=now_func))
        record.setdefault("finalized_at", None)
        record.setdefault("final_path", None)
        record.setdefault("final_meta_path", None)
        record.setdefault("reopened_from_round_id", None)
        if changed:
            write_json(path, record)
        return record

    record = make_task_round(task_id, sequence, task_round_started_at(task, now_func=now_func))
    write_json(path, record)
    return record


def ensure_current_task_round(root: Path, run_id: str, task_id: str, *, now_func: Callable[[], str] = utc_now) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        record = ensure_task_round_record(root, run_id, task, now_func=now_func)
        plan["updated_at"] = now_func()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    return record


def list_task_lifecycle_rounds(root: Path, run_id: str, task_id: str) -> list[dict]:
    base = task_lifecycle_rounds_dir(root, run_id, task_id)
    if not base.is_dir():
        return []
    rounds: list[dict] = []
    for path in sorted(base.glob("round-*/round.json")):
        try:
            rounds.append(read_json(path))
        except (OSError, ValueError):
            continue
    return sorted(rounds, key=lambda item: int(item.get("sequence") or round_sequence_from_id(item.get("round_id")) or 0))


def task_rounds_path(root: Path, run_id: str, task_id: str) -> Path:
    return run_dir(root, run_id) / "tasks" / task_id / "rounds.jsonl"


def list_task_rounds(root: Path, run_id: str, task_id: str) -> list[dict]:
    rounds, _ = iter_jsonl_from(task_rounds_path(root, run_id, task_id), 0)
    return rounds
