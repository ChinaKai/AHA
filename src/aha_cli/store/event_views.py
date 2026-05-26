from __future__ import annotations

import json
from pathlib import Path

from aha_cli.store.io import iter_jsonl_reverse
from aha_cli.store.paths import event_path


TIMELINE_EVENT_TYPES = {
    "message",
    "task_dispatched",
    "task_started",
    "task_finished",
    "task_round_started",
    "task_round_recorded",
    "task_journal_rendered",
    "task_result_written",
    "task_final_requested",
    "task_round_summary_requested",
    "task_proxy_config_updated",
    "task_reopened",
    "task_completed",
    "task_waiting_for_subagents",
    "task_status_changed",
    "agent_started",
    "agent_status_changed",
    "agent_thread",
    "agent_command_started",
    "agent_command_finished",
    "agent_message",
    "agent_prompt_metrics",
    "agent_usage",
    "agent_error",
    "agent_context_overflow",
    "agent_delegated",
    "agent_message_routed",
    "claimed_sub_without_aha_agent",
    "native_subagent_tool_used",
    "sub_agent_reported",
    "sub_agent_report_ignored",
    "sub_agent_backend_recovered",
    "sub_agent_backend_failed",
    "agent_created",
    "agent_config_updated",
    "agent_finished",
    "main_reported_to_host",
    "host_decision",
    "main_applied_decision",
    "workspace_missing",
}

SUPERVISION_EVENT_TYPES = {
    "main_reported_to_host",
    "host_decision",
    "main_applied_decision",
}


def format_event_log_line(event: dict) -> str:
    data = event.get("data") or {}
    ts = event.get("ts") or ""
    event_type = str(event.get("type") or "event")
    if event_type == "log":
        return f"[{ts}] {data.get('task_id') or '-'}: {data.get('line') or ''}"
    if event_type == "message":
        task = f" task={data['task_id']}" if data.get("task_id") else ""
        return f"[{ts}] message{task} {data.get('sender') or 'main'} -> {data.get('target') or '-'}: {data.get('message') or ''}"
    return f"[{ts}] {event_type}: {json.dumps(data, ensure_ascii=False)}"


def event_task_id(event: dict) -> str | None:
    data = event.get("data") or {}
    if data.get("task_id"):
        return str(data["task_id"])
    target = str(data.get("target") or "")
    if event.get("type") == "message" and target.startswith("task-") and target[5:].isdigit():
        return target
    return None


def event_agent_refs(event: dict) -> set[str]:
    data = event.get("data") or {}
    refs: set[str] = set()
    event_type = str(event.get("type") or "")
    if event_type == "message":
        target = str(data.get("target") or "").strip()
        sender = str(data.get("sender") or "").strip()
        private_target = bool(target and target.lower() not in {"browser", "system", "aha", "main"})
        if sender == "AHA" and private_target:
            return set()

    def add(value: object) -> None:
        text = str(value or "").strip()
        if text and text.lower() not in {"browser", "system", "aha"}:
            refs.add(text)

    add(data.get("target"))
    add(data.get("to_agent"))
    add(data.get("from_agent"))
    add(data.get("agent_id"))
    if event.get("type") == "message":
        add(data.get("sender"))
        if any(str(data.get(key) or "").lower() == "aha" for key in ("role", "from_agent", "to_agent", "sender", "target")):
            refs.add("main")
    if not refs and (
        event_type.startswith("agent_")
        or event_type.startswith("task_")
        or event_type in SUPERVISION_EVENT_TYPES
        or event_type == "workspace_missing"
    ):
        refs.add("main")
    return refs


def task_event_log_page(root: Path, run_id: str, task_id: str, limit: int = 200, before: int | None = None) -> dict:
    path = event_path(root, run_id)
    after_offset = path.stat().st_size if path.exists() else 0
    end_offset = after_offset if before is None else max(0, min(before, after_offset))
    safe_limit = max(1, min(limit, 1000))
    matches: list[dict] = []
    for offset, event in iter_jsonl_reverse(path, before=end_offset) or ():
        if event_task_id(event) == task_id:
            matches.append({"_cursor": offset, "text": format_event_log_line(event)})
            if len(matches) > safe_limit:
                break

    has_more = len(matches) > safe_limit
    page = list(reversed(matches[:safe_limit]))
    next_before_offset = page[0].get("_cursor") if has_more and page else None
    return {
        "source": "events",
        "path": "events.jsonl",
        "text": "\n".join(item["text"] for item in page),
        "lines": page,
        "before_offset": end_offset,
        "after_offset": after_offset,
        "next_before_offset": next_before_offset,
        "has_more": has_more,
        "count": len(page),
    }


def conversation_event_category(event_type: str) -> str:
    if event_type == "agent_message":
        return "chat"
    if event_type in {"agent_usage", "agent_prompt_metrics"}:
        return "usage"
    if event_type in {"agent_command_started", "agent_command_finished"}:
        return "commands"
    if event_type == "message":
        return "chat"
    return "runtime"


def _message_endpoint(data: dict, *keys: str) -> str:
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value.lower()
    return ""


def _message_body_key(data: dict) -> str:
    return " ".join(str(data.get("message") or "").split())


def _main_host_mirror_key(event: dict) -> tuple[str, str, str] | None:
    if event.get("type") != "message":
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    sender = _message_endpoint(data, "display_sender", "sender", "from_agent")
    target = _message_endpoint(data, "display_target", "to_agent", "target")
    body = _message_body_key(data)
    if sender == "main" and target == "host" and body:
        return (str(data.get("task_id") or ""), sender, body)
    return None


def _main_browser_mirror_key(event: dict) -> tuple[str, str, str] | None:
    if event.get("type") != "message":
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    sender = _message_endpoint(data, "display_sender", "sender", "from_agent")
    target = _message_endpoint(data, "display_target", "to_agent", "target")
    body = _message_body_key(data)
    if sender == "main" and target == "browser" and body:
        return (str(data.get("task_id") or ""), sender, body)
    return None


def _category_allowed(event_type: str, categories: set[str] | None) -> bool:
    return categories is None or conversation_event_category(event_type) in categories


def _conversation_event_matches(event: dict, task_id: str, target: str, categories: set[str] | None) -> bool:
    event_type = str(event.get("type") or "")
    return (
        _category_allowed(event_type, categories)
        and event_type in TIMELINE_EVENT_TYPES
        and event_task_id(event) == task_id
        and (target or "main") in event_agent_refs(event)
    )


def _main_host_mirror_keys(root: Path, run_id: str, task_id: str, target: str, before: int, categories: set[str] | None) -> set[tuple[str, str, str]]:
    if (target or "main") != "main" or not _category_allowed("message", categories):
        return set()
    path = event_path(root, run_id)
    keys: set[tuple[str, str, str]] = set()
    for _offset, event in iter_jsonl_reverse(path, before=before) or ():
        if not _conversation_event_matches(event, task_id, target, categories):
            continue
        key = _main_host_mirror_key(event)
        if key:
            keys.add(key)
    return keys


def conversation_events_page(
    root: Path,
    run_id: str,
    task_id: str,
    target: str,
    limit: int = 50,
    before: int | None = None,
    categories: set[str] | None = None,
) -> dict:
    path = event_path(root, run_id)
    after_offset = path.stat().st_size if path.exists() else 0
    end_offset = after_offset if before is None else max(0, min(before, after_offset))
    safe_limit = max(1, min(limit, 200))
    allowed_categories = categories
    main_host_mirror_keys = _main_host_mirror_keys(root, run_id, task_id, target or "main", end_offset, allowed_categories)
    matches: list[dict] = []
    for offset, event in iter_jsonl_reverse(path, before=end_offset) or ():
        event_type = str(event.get("type") or "")
        if _conversation_event_matches(event, task_id, target or "main", allowed_categories):
            if _main_browser_mirror_key(event) in main_host_mirror_keys:
                continue
            item = dict(event)
            item["_cursor"] = offset
            matches.append(item)
            if len(matches) > safe_limit:
                break

    has_more = len(matches) > safe_limit
    page = list(reversed(matches[:safe_limit]))
    next_before_offset = page[0].get("_cursor") if has_more and page else None
    return {
        "events": page,
        "before_offset": end_offset,
        "after_offset": after_offset,
        "next_before_offset": next_before_offset,
        "before": next_before_offset,
        "has_more": has_more,
        "count": len(page),
    }


__all__ = [
    "conversation_event_category",
    "conversation_events_page",
    "event_agent_refs",
    "event_task_id",
    "format_event_log_line",
    "task_event_log_page",
]
