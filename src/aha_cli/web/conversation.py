from __future__ import annotations

import json
from pathlib import Path

from aha_cli.services.backend_runtime import backend_status
from aha_cli.services.prompt_artifacts import read_prompt_artifact
from aha_cli.store.event_views import conversation_events_page, event_agent_refs, event_task_id
from aha_cli.store.filesystem import (
    event_path,
    event_stream_page,
    event_stream_position,
    iter_jsonl_reverse,
    read_json,
    session_path,
    task_log_page,
)
from aha_cli.web.session_debug import backend_session_jsonl_info

DEFAULT_EVENTS_LIMIT = 500
MAX_EVENTS_LIMIT = 2000


def conversation_turn_events(root: Path, run_id: str, task_id: str, target: str, limit: int = 500) -> list[dict]:
    events_file = event_path(root, run_id)
    events: list[dict] = []
    safe_limit = max(1, min(limit, 2000))
    target = target or "main"
    for offset, event in iter_jsonl_reverse(events_file) or ():
        if (
            str(event.get("type") or "") in {
                "backend_start_queued",
                "backend_started",
                "agent_started",
                "agent_error",
                "agent_prompt_metrics",
                "agent_usage",
                "agent_context_overflow",
                "agent_thread",
                "agent_finished",
                "agent_status_changed",
                "backend_stopped",
            }
            and event_task_id(event) == task_id
            and target in event_agent_refs(event)
        ):
            item = dict(event)
            item["_cursor"] = offset
            events.append(item)
            if event.get("type") == "agent_started" or len(events) >= safe_limit:
                break
    return list(reversed(events))


def is_aha_action_envelope_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("{") or '"actions"' not in stripped or '"response"' not in stripped:
        return False
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    return isinstance(payload.get("actions"), list) and isinstance(payload.get("response"), str)


def is_raw_action_agent_message(event: dict) -> bool:
    if event.get("type") != "agent_message":
        return False
    data = event.get("data") or {}
    return is_aha_action_envelope_text(str(data.get("text") or ""))


def hide_raw_action_agent_message(event: dict, target: str) -> bool:
    return (target or "main") == "main" and is_raw_action_agent_message(event)


def slim_conversation_event(event: dict, *, include_command_output: bool = False) -> dict:
    if include_command_output or event.get("type") != "agent_command_finished":
        return event
    data = event.get("data") or {}
    output_tail = str(data.get("output_tail") or "")
    if not output_tail:
        return event
    slim = dict(event)
    slim_data = dict(data)
    slim_data.pop("output_tail", None)
    slim_data["output_tail_omitted"] = True
    slim_data["output_tail_chars"] = len(output_tail)
    slim["data"] = slim_data
    return slim


def conversation_view_page(
    root: Path,
    run_id: str,
    task_id: str,
    target: str,
    limit: int = 50,
    before: int | None = None,
    categories: set[str] | None = None,
    include_command_output: bool = False,
) -> dict:
    page = conversation_events_page(root, run_id, task_id, target, limit=limit, before=before, categories=categories)
    events = [
        slim_conversation_event(event, include_command_output=include_command_output)
        for event in page.get("events", [])
        if not hide_raw_action_agent_message(event, target)
    ]
    view = dict(page)
    view["events"] = events
    view["count"] = len(events)
    view["categories"] = sorted(categories) if categories is not None else []
    view["include_command_output"] = include_command_output
    view["turn_events"] = conversation_turn_events(root, run_id, task_id, target) if before is None else []
    session_file = session_path(root, run_id, task_id, target)
    backend_state = backend_status(root, run_id, target, task_id=task_id)
    session_info = backend_session_jsonl_info(read_json(session_file)) if session_file.exists() else {}
    session_info["runtime"] = backend_state
    session_info["context_pressure"] = backend_state.get("context_pressure")
    session_info["runtime_context_usage"] = backend_state.get("runtime_context_usage")
    session_info["latest_usage"] = backend_state.get("latest_usage")
    session_info["latest_prompt_metrics"] = backend_state.get("latest_prompt_metrics")
    session_info["requested_model"] = backend_state.get("requested_model")
    session_info["resolved_model"] = backend_state.get("resolved_model")
    view["backend_session"] = session_info
    return view


def prompt_artifact_view(root: Path, run_id: str, ref: str) -> dict:
    return read_prompt_artifact(root, run_id, ref)


def event_stream_view_page(root: Path, run_id: str, offset: int = 0, limit: int = DEFAULT_EVENTS_LIMIT) -> dict:
    safe_limit = max(1, min(limit, MAX_EVENTS_LIMIT))
    if offset < 0:
        snapshot_offset = event_stream_position(root, run_id)
        page = event_stream_page(root, run_id, snapshot_offset, limit=safe_limit, snapshot_event_id=snapshot_offset)
    else:
        page = event_stream_page(root, run_id, offset, limit=safe_limit)
    new_offset = int(page["last_event_id"])
    snapshot_offset = int(page["snapshot_event_id"])
    return {
        "run_id": run_id,
        "offset": new_offset,
        "last_event_id": page["last_event_id"],
        "snapshot_offset": snapshot_offset,
        "snapshot_event_id": page["snapshot_event_id"],
        "has_more": new_offset < snapshot_offset,
        "limit": safe_limit,
        "events": page["events"],
    }


def task_log_view_page(
    root: Path,
    run_id: str,
    task_id: str,
    limit: int = 200,
    before: int | None = None,
    source: str = "auto",
) -> dict:
    return task_log_page(root, run_id, task_id, limit=limit, before=before, source=source)
