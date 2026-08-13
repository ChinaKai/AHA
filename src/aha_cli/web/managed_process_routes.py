from __future__ import annotations

import asyncio
from pathlib import Path

from aha_cli.services.managed_processes import (
    ManagedProcessConflict,
    ManagedProcessError,
    list_managed_processes,
    managed_process_status,
    start_managed_process,
    stop_managed_process,
)
from aha_cli.web.http_utils import json_response, parse_json_body
from aha_cli.web.run_api import require_api_run_id


def _query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[0] if values else "").strip()


async def managed_process_route_response(
    root: Path,
    default_run_id: str,
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: bytes,
) -> bytes | None:
    if path != "/api/managed-processes":
        return None
    try:
        payload = parse_json_body(body) if body else {}
    except ValueError:
        return json_response({"ok": False, "error": "invalid json"}, "400 Bad Request")
    if not isinstance(payload, dict):
        return json_response({"ok": False, "error": "json body must be an object"}, "400 Bad Request")
    run_id = require_api_run_id(root, default_run_id, query, payload)
    task_id = str(payload.get("task_id") or _query_value(query, "task_id")).strip()
    agent_id = str(payload.get("agent_id") or _query_value(query, "agent_id") or "main").strip()
    name = str(payload.get("name") or _query_value(query, "name")).strip()
    if not task_id:
        return json_response({"ok": False, "error": "task_id is required"}, "400 Bad Request")
    try:
        if method == "GET":
            if name:
                process = await asyncio.to_thread(managed_process_status, root, run_id, task_id, agent_id, name)
                return json_response({"ok": True, "process": process})
            processes = await asyncio.to_thread(list_managed_processes, root, run_id, task_id, agent_id)
            return json_response({"ok": True, "processes": processes})
        if method == "POST":
            command = payload.get("command")
            if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
                return json_response({"ok": False, "error": "command must be a string array"}, "400 Bad Request")
            if not name:
                return json_response({"ok": False, "error": "name is required"}, "400 Bad Request")
            process = await asyncio.to_thread(
                start_managed_process,
                root,
                run_id,
                task_id,
                agent_id,
                name,
                command,
                cwd=payload.get("cwd"),
            )
            status = "200 OK" if process.get("already_running") else "201 Created"
            return json_response({"ok": True, "process": process}, status)
        if method == "DELETE":
            if not name:
                return json_response({"ok": False, "error": "name is required"}, "400 Bad Request")
            process = await asyncio.to_thread(stop_managed_process, root, run_id, task_id, agent_id, name)
            return json_response({"ok": True, "process": process})
        return json_response({"ok": False, "error": "method not allowed"}, "405 Method Not Allowed")
    except FileNotFoundError as exc:
        return json_response({"ok": False, "error": str(exc)}, "404 Not Found")
    except ManagedProcessConflict as exc:
        return json_response({"ok": False, "error": str(exc)}, "409 Conflict")
    except (ManagedProcessError, OSError, ValueError) as exc:
        return json_response({"ok": False, "error": str(exc)}, "400 Bad Request")


__all__ = ["managed_process_route_response"]
