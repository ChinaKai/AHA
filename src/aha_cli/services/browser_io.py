from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from aha_cli.domain.models import utc_now
from aha_cli.store.events import append_event
from aha_cli.store.io import append_jsonl, iter_jsonl_records_from, iter_jsonl_reverse
from aha_cli.store.paths import run_dir
from aha_cli.store.runs import require_plan


def browser_io_path(root: Path, run_id: str, task_id: str) -> Path:
    return run_dir(root, run_id) / "tasks" / task_id / "browser_io.jsonl"


def _require_task(root: Path, run_id: str, task_id: str) -> None:
    plan = require_plan(root, run_id)
    if not any(task.get("id") == task_id and not task.get("deleted_at") for task in plan.get("tasks", [])):
        raise KeyError(task_id)


def redact_browser_url(value: object) -> str:
    """Keep a useful origin without persisting credentials, tokens, or paths."""

    raw = str(value or "").strip()
    if raw == "about:blank":
        return raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    default_port = 80 if parsed.scheme == "http" else 443
    netloc = f"{host}:{port}" if port and port != default_port else host
    suffix = "/…" if parsed.path not in {"", "/"} or parsed.query or parsed.fragment else "/"
    return f"{parsed.scheme}://{netloc}{suffix}"[:512]


def append_browser_io_record(
    root: Path,
    run_id: str,
    task_id: str,
    payload: dict,
) -> dict:
    """Record action metadata without page text, credentials, or typed values."""

    _require_task(root, run_id, task_id)
    record = {
        "ts": str(payload.get("ts") or utc_now()),
        "task_id": task_id,
        "agent_id": str(payload.get("agent_id") or "main"),
        "source": str(payload.get("source") or "agent"),
        "action": str(payload.get("action") or "unknown"),
        "status": str(payload.get("status") or "ok"),
        "page_id": str(payload.get("page_id") or ""),
        "url_before": redact_browser_url(payload.get("url_before")),
        "url_after": redact_browser_url(payload.get("url_after")),
        "revision": max(0, int(payload.get("revision") or 0)),
    }
    error = str(payload.get("error") or "").strip()
    if error:
        record["error"] = error[:200]
    offset = append_jsonl(browser_io_path(root, run_id, task_id), record)
    event = append_event(root, run_id, "browser_io", {**record, "offset": offset}, ts=record["ts"])
    return {"record": {**record, "offset": offset}, "event": event}


def browser_io_page(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    limit: int = 200,
    after: int | None = None,
    before: int | None = None,
) -> dict:
    _require_task(root, run_id, task_id)
    path = browser_io_path(root, run_id, task_id)
    file_size = path.stat().st_size if path.exists() else 0
    safe_limit = max(1, min(int(limit or 200), 1000))
    if after is not None:
        records, next_offset = iter_jsonl_records_from(path, max(0, int(after)), limit=safe_limit)
        return {
            "events": [{**record, "offset": line_end} for record, line_end in records],
            "after_offset": next_offset,
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


__all__ = ["append_browser_io_record", "browser_io_page", "browser_io_path", "redact_browser_url"]
