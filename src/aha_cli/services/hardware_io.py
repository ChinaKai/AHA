from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.store.events import append_event as append_stream_event
from aha_cli.store.io import append_jsonl, iter_jsonl_records_from, iter_jsonl_reverse
from aha_cli.store.paths import run_dir
from aha_cli.store.runs import require_plan

HARDWARE_IO_DIRECTIONS = {"tx", "rx", "system"}
HARDWARE_IO_ENCODINGS = {"text", "base64", "hex"}
HARDWARE_IO_EVENT_INLINE_LIMIT = 12000


def hardware_io_path(root: Path, run_id: str, task_id: str) -> Path:
    return run_dir(root, run_id) / "tasks" / task_id / "hardware_io.jsonl"


def require_hardware_io_task(root: Path, run_id: str, task_id: str) -> None:
    plan = require_plan(root, run_id)
    if not any(task.get("id") == task_id and not task.get("deleted_at") for task in plan.get("tasks", [])):
        raise KeyError(task_id)


def _trim_text(value: object, default: str = "") -> str:
    text = str(value if value is not None else default)
    return text.strip()


def _inline_data(value: object) -> tuple[str, bool]:
    data = str(value if value is not None else "")
    if len(data) <= HARDWARE_IO_EVENT_INLINE_LIMIT:
        return data, False
    return data[:HARDWARE_IO_EVENT_INLINE_LIMIT], True


def normalize_hardware_io_record(
    payload: dict,
    *,
    task_id: str,
    default_agent_id: str = "main",
) -> dict:
    direction = _trim_text(payload.get("direction"), "system").lower()
    if direction not in HARDWARE_IO_DIRECTIONS:
        direction = "system"
    encoding = _trim_text(payload.get("encoding"), "text").lower()
    if encoding not in HARDWARE_IO_ENCODINGS:
        encoding = "text"
    data, truncated = _inline_data(payload.get("data", payload.get("text", "")))
    record = {
        "ts": _trim_text(payload.get("ts")) or utc_now(),
        "task_id": task_id,
        "agent_id": _trim_text(payload.get("agent_id"), default_agent_id) or default_agent_id,
        "channel": _trim_text(payload.get("channel"), "hardware").lower() or "hardware",
        "endpoint": _trim_text(payload.get("endpoint")),
        "direction": direction,
        "encoding": encoding,
        "data": data,
        "truncated": bool(payload.get("truncated")) or truncated,
    }
    # Optional provenance for TX/system rows: "interactive", "rule:<id>", "session", etc.
    source = _trim_text(payload.get("source"))
    if source:
        record["source"] = source
    return record


def append_hardware_io_record(
    root: Path,
    run_id: str,
    task_id: str,
    payload: dict,
    *,
    default_agent_id: str = "main",
) -> dict:
    require_hardware_io_task(root, run_id, task_id)
    record = normalize_hardware_io_record(payload, task_id=task_id, default_agent_id=default_agent_id)
    offset = append_jsonl(hardware_io_path(root, run_id, task_id), record)
    event = append_stream_event(
        root,
        run_id,
        "hardware_io",
        {
            **record,
            "offset": offset,
        },
        ts=record["ts"],
    )
    return {
        "record": {
            **record,
            "offset": offset,
        },
        "event": event,
    }


def hardware_io_page(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    limit: int = 500,
    after: int | None = None,
    before: int | None = None,
) -> dict:
    require_hardware_io_task(root, run_id, task_id)
    path = hardware_io_path(root, run_id, task_id)
    file_size = path.stat().st_size if path.exists() else 0
    safe_limit = max(1, min(int(limit or 500), 2000))
    if after is not None:
        records, next_offset = iter_jsonl_records_from(path, max(0, int(after)), limit=safe_limit)
        events = [{**record, "offset": line_end} for record, line_end in records]
        return {
            "events": events,
            "after_offset": next_offset,
            "before_offset": max(0, int(after)),
            "has_more": next_offset < file_size,
            "limit": safe_limit,
        }

    end_offset = file_size if before is None else max(0, min(int(before), file_size))
    matches: list[dict] = []
    for offset, record in iter_jsonl_reverse(path, before=end_offset) or ():
        matches.append({**record, "offset": offset})
        if len(matches) > safe_limit:
            break
    has_more = len(matches) > safe_limit
    events = list(reversed(matches[:safe_limit]))
    return {
        "events": events,
        "after_offset": file_size,
        "before_offset": end_offset,
        "next_before_offset": events[0].get("offset") if has_more and events else None,
        "has_more": has_more,
        "limit": safe_limit,
    }


__all__ = [
    "append_hardware_io_record",
    "hardware_io_page",
    "hardware_io_path",
    "normalize_hardware_io_record",
]
