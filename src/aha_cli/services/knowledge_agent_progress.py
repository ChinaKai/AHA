from __future__ import annotations

from collections.abc import Iterable

from aha_cli.domain.models import utc_now

MAX_AGENT_LOG_EVENTS = 160
TEXT_EXCERPT_LIMIT = 260
COMMAND_EXCERPT_LIMIT = 220


def _clip(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ..."


def agent_log_event(stage: str, message: str, **extra) -> dict:
    event = {"at": utc_now(), "stage": stage, "message": message}
    event.update({key: value for key, value in extra.items() if value not in (None, "", [])})
    return event


def trim_agent_log(events: Iterable[dict], *, limit: int = MAX_AGENT_LOG_EVENTS) -> list[dict]:
    items = [item for item in events if isinstance(item, dict)]
    return items[-limit:]


def summarize_agent_progress(event_type: str, data: dict | None = None) -> dict | None:
    data = data if isinstance(data, dict) else {}
    if event_type == "backend_started":
        backend = str(data.get("backend") or "agent")
        return {
            "stage": "agent",
            "message": f"Starting {backend} agent",
            "backend": data.get("backend"),
            "model": data.get("model"),
            "cwd": data.get("cwd"),
        }
    if event_type == "backend_finished":
        exit_code = data.get("exit_code")
        suffix = f" with exit code {exit_code}" if exit_code is not None else ""
        return {
            "stage": "agent",
            "message": f"Agent finished{suffix}",
            "exit_code": exit_code,
            "reply_chars": data.get("reply_chars"),
        }
    if event_type == "backend_process_started":
        return {
            "stage": "agent",
            "message": "Agent process started",
            "pid": data.get("pid"),
            "process_group": data.get("process_group"),
        }
    if event_type == "agent_thread":
        return {
            "stage": "session",
            "message": "Agent session started",
            "thread_id": data.get("thread_id"),
        }
    if event_type == "agent_error":
        return {
            "stage": "error",
            "message": _clip(data.get("message") or data.get("error") or "Agent error", TEXT_EXCERPT_LIMIT),
        }
    if event_type == "agent_activity":
        return {
            "stage": "activity",
            "message": _clip(data.get("message") or "Agent is working", TEXT_EXCERPT_LIMIT),
        }
    if event_type == "agent_message":
        text = _clip(data.get("text"), TEXT_EXCERPT_LIMIT)
        return {
            "stage": "message",
            "message": "Agent emitted response text" if text else "Agent emitted response",
            "excerpt": text,
        }
    if event_type in {"agent_command_started", "agent_command_finished"}:
        command = _clip(data.get("command") or data.get("tool_name") or "tool", COMMAND_EXCERPT_LIMIT)
        started = event_type == "agent_command_started"
        return {
            "stage": "tool",
            "message": ("Started" if started else "Finished") + f": {command}",
            "tool_name": data.get("tool_name"),
            "status": data.get("status"),
            "exit_code": data.get("exit_code"),
            "output_chars": data.get("output_chars"),
        }
    if event_type == "agent_usage":
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        total = usage.get("total_tokens")
        if total is None and isinstance(usage.get("total_token_usage"), dict):
            total = usage["total_token_usage"].get("total_tokens")
        last_total = None
        if isinstance(usage.get("last_token_usage"), dict):
            last_total = usage["last_token_usage"].get("total_tokens")
        return {
            "stage": "usage",
            "message": f"Token usage updated: {total} total" if total is not None else "Agent usage updated",
            "total_tokens": total,
            "last_tokens": last_total,
        }
    return None
