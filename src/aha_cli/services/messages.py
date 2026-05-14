from __future__ import annotations

import json


def format_event(event: dict) -> str:
    event_type = event.get("type", "event")
    ts = event.get("ts", "")
    data = event.get("data", {})
    if event_type == "log":
        return f"[{ts}] {data.get('task_id', '-')}: {data.get('line', '')}"
    if event_type == "message":
        task = f" task={data.get('task_id')}" if data.get("task_id") else ""
        return f"[{ts}] message{task} {data.get('sender', 'main')} -> {data.get('target', '-')}: {data.get('message', '')}"
    return f"[{ts}] {event_type}: {json.dumps(data, ensure_ascii=False)}"
