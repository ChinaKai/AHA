from __future__ import annotations

from pathlib import Path
import threading

from aha_cli.domain.models import utc_now
from aha_cli.store.event_notifications import notify_event_appended
from aha_cli.store.io import append_jsonl, iter_jsonl_records_from
from aha_cli.store.paths import event_path

_EVENT_LOCK = threading.Lock()


def event_stream_position(root: Path, run_id: str) -> int:
    path = event_path(root, run_id)
    return path.stat().st_size if path.exists() else 0


def normalize_event_id(event_id: object, default: int = 0) -> int:
    if event_id is None or event_id == "":
        return default
    try:
        return max(0, int(event_id))
    except (TypeError, ValueError):
        return default


def with_event_id(event: dict, event_id: int) -> dict:
    item = dict(event)
    item.setdefault("event_id", event_id)
    return item


def append_event(root: Path, run_id: str, event_type: str, data: dict, ts: str | None = None) -> dict:
    event = {
        "ts": ts or utc_now(),
        "run_id": run_id,
        "type": event_type,
        "data": data,
    }
    with _EVENT_LOCK:
        path = event_path(root, run_id)
        event_id = append_jsonl(path, event)
    notify_event_appended(path)
    return with_event_id(event, event_id)


def append_event_to_file(events_file: Path | None, run_id: str, event_type: str, data: dict, ts: str | None = None) -> dict:
    event = {
        "ts": ts or utc_now(),
        "run_id": run_id,
        "type": event_type,
        "data": data,
    }
    if events_file is not None:
        event_id = append_jsonl(events_file, event)
        notify_event_appended(events_file)
        return with_event_id(event, event_id)
    return event


def event_stream_page(
    root: Path,
    run_id: str,
    last_event_id: object = 0,
    limit: int | None = None,
    snapshot_event_id: object | None = None,
) -> dict:
    path = event_path(root, run_id)
    snapshot_id = normalize_event_id(snapshot_event_id, event_stream_position(root, run_id))
    start_id = normalize_event_id(last_event_id)
    if not path.exists():
        return {
            "events": [],
            "last_event_id": snapshot_id,
            "snapshot_event_id": snapshot_id,
            "has_more": False,
            "limit": limit,
        }
    records, next_id = iter_jsonl_records_from(path, start_id, before=snapshot_id, limit=limit)
    return {
        "events": [with_event_id(event, line_end) for event, line_end in records],
        "last_event_id": next_id,
        "snapshot_event_id": snapshot_id,
        "has_more": next_id < snapshot_id,
        "limit": limit,
    }
