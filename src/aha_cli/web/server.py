from __future__ import annotations

import asyncio
import json
from pathlib import Path
import textwrap
from urllib.parse import parse_qs, unquote, urlparse

from aha_cli.backends.registry import agent_backend_names, agent_backends, model_options
from aha_cli.services.backend_runtime import (
    backend_status,
    start_backend,
)
from aha_cli.services.chat import chat_offset_path, save_chat_offset
from aha_cli.services.tasks import create_task_and_dispatch
from aha_cli.store.filesystem import (
    add_agent,
    append_event,
    append_message,
    conversation_events_page,
    delete_task,
    event_path,
    inbox_path,
    iter_jsonl_from,
    run_dir,
    set_task_hidden,
    status_snapshot,
    task_context_snapshot,
    task_final_snapshot,
    task_log_page,
    task_snapshot,
    update_agent_config,
)

STATIC_DIR = Path(__file__).parent / "static"
HL_PROJECT_ROOT = Path("/home/kaikai/kk-workspace/hl_project")
MY_PROJECT_ROOT = Path("/home/kaikai/kk-workspace/my_project")
WORKSPACE_ROOTS = [HL_PROJECT_ROOT, MY_PROJECT_ROOT]
SANDBOX_OPTIONS = {"read-only", "workspace-write", "danger-full-access"}
APPROVAL_OPTIONS = {"untrusted", "on-failure", "on-request", "never"}
TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}
ACTIVE_BACKEND_STATUSES = {"running", "busy"}


def http_response(status: str, body: bytes, content_type: str = "text/plain; charset=utf-8") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body


def json_response(data: dict, status: str = "200 OK") -> bytes:
    return http_response(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")


def static_response(path: Path, method: str) -> bytes:
    if not path.exists() or not path.is_file():
        return http_response("404 Not Found", b"not found\n")
    suffix = path.suffix
    content_type = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
    }.get(suffix, "application/octet-stream")
    body = path.read_bytes()
    return http_response("200 OK", b"" if method == "HEAD" else body, content_type)


async def read_http_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    raw = await reader.readuntil(b"\r\n\r\n")
    header_text = raw.decode("utf-8", errors="replace")
    lines = header_text.split("\r\n")
    request = lines[0].split()
    if len(request) < 2:
        return "GET", "/", {}, b""
    method, target = request[0], request[1]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0") or "0")
    body = await reader.readexactly(length) if length else b""
    return method, target, headers, body


def parse_json_body(body: bytes) -> dict:
    return json.loads(body.decode("utf-8") or "{}")


def workspace_options(roots: list[Path] | None = None) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    workspace_roots = WORKSPACE_ROOTS if roots is None else roots
    for root in workspace_roots:
        if not root.is_dir():
            continue
        for path in sorted(item for item in root.iterdir() if item.is_dir()):
            options.append(
                {
                    "name": path.name,
                    "label": f"{root.name}/{path.name}",
                    "path": str(path),
                    "root": str(root),
                }
            )
    return options


def task_outcome_snapshots(root: Path, run_id: str) -> dict[str, dict]:
    outcomes: dict[str, dict] = {}
    events, _ = iter_jsonl_from(event_path(root, run_id), 0)
    for event in events:
        if event.get("type") != "task_status_changed":
            continue
        data = event.get("data") or {}
        task_id = str(data.get("task_id") or "")
        status = str(data.get("status") or "")
        if task_id and status in TERMINAL_TASK_STATUSES:
            outcomes[task_id] = {
                "status": status,
                "exit_code": data.get("exit_code"),
                "updated_at": event.get("ts"),
            }
    return outcomes


def task_activity_status(task: dict) -> str:
    process_statuses = {
        str(agent.get("backend_process_status") or "stopped").lower()
        for agent in task.get("agents", [])
    }
    if "busy" in process_statuses:
        return "busy"
    if str(task.get("status") or "").lower() == "running" or process_statuses.intersection(ACTIVE_BACKEND_STATUSES):
        return "running"
    return "idle"


def web_status_snapshot(root: Path, run_id: str) -> dict:
    snapshot = status_snapshot(root, run_id)
    outcomes = task_outcome_snapshots(root, run_id)
    backend_cache: dict[tuple[str | None, str], dict] = {}
    for task in snapshot.get("tasks", []):
        raw_task_id = str(task.get("id") or "")
        task_id = raw_task_id or None
        current_status = str(task.get("status") or "pending")
        outcome = current_status if current_status in TERMINAL_TASK_STATUSES else outcomes.get(raw_task_id, {}).get("status")
        for agent in task.get("agents", []):
            target = str(agent.get("id") or "main")
            key = (task_id, target)
            if key not in backend_cache:
                backend_cache[key] = backend_status(root, run_id, target, task_id=task_id)
            state = backend_cache[key]
            agent["backend_process_status"] = state.get("status") or "stopped"
            agent["backend_process_pid"] = state.get("pid")
            agent["backend_process_last_reply_at"] = state.get("last_reply_at")
        task["current_status"] = current_status
        task["outcome_status"] = outcome
        task["activity_status"] = task_activity_status(task)
        task["display_status"] = outcome or current_status
    return snapshot


def format_aha_command(root: Path, run_id: str, task_id: str | None, command: str, target: str = "main") -> str:
    parts = command.split()
    name = parts[1] if len(parts) > 1 else "help"
    if name == "help":
        return "\n".join(
            [
                "AHA commands:",
                "- /aha help: show AHA commands",
                "- /aha status: show selected task status",
                "- /aha agents: list selected task agents",
                "- /aha final: ask task-main to generate or update the Final",
                "- /aha finalize: alias for /aha final",
                "",
                "Agent command:",
                "- /agent <command>: route /<command> to the selected agent",
            ]
        )
    if not task_id:
        return "No task is selected."
    try:
        detail = task_snapshot(root, run_id, task_id)
    except KeyError:
        return f"Task not found: {task_id}"
    task = detail["task"]
    if name == "status":
        return "\n".join(
            [
                f"Task: {task['id']} {task['title']}",
                f"Status: {task.get('status')} exit={task.get('exit_code')}",
                f"Backend: {task.get('preferred_backend')} model={task.get('preferred_model') or 'default'}",
                f"Workspace: {task.get('workspace_path') or '-'}",
            ]
        )
    if name == "agents":
        lines = ["Agents:"]
        for agent in task.get("agents", []):
            lines.append(
                f"- {agent.get('id')} role={agent.get('role')} backend={agent.get('backend')} "
                f"sandbox={agent.get('sandbox') or task.get('preferred_sandbox') or '-'} "
                f"approval={agent.get('approval') or task.get('preferred_approval') or '-'} "
                f"assignment={agent.get('assignment') or agent.get('created_reason') or '-'}"
            )
        return "\n".join(lines)
    if name in {"final", "finalize"}:
        return "Use `/aha final` from the selected task conversation to ask task-main to generate or update the Final."
    return f"Unknown AHA command: /aha {name}. Try /aha help."


def finalization_prompt(task_id: str, title: str) -> str:
    return textwrap.dedent(
        f"""\
        AHA finalize request.

        Task:
        - id: {task_id}
        - title: {title}

        Generate or update the task Final now.

        Requirements:
        - Return concise Markdown only.
        - Summarize the stable outcome of this task, not the whole noisy chat transcript.
        - Include changed files or concrete decisions when relevant.
        - Include verification performed when relevant.
        - Include remaining risks or next steps only if they are actionable.
        - Do not include internal AHA command chatter unless it directly affects the outcome.
        """
    )


def request_task_finalization(root: Path, run_id: str, task_id: str | None, command: str) -> str:
    if not task_id:
        return "No task is selected."
    try:
        detail = task_snapshot(root, run_id, task_id)
    except KeyError:
        return f"Task not found: {task_id}"
    task = detail["task"]
    append_message(
        root,
        run_id,
        "main",
        finalization_prompt(task_id, str(task.get("title", ""))),
        sender="aha",
        task_id=task_id,
        role="main",
        from_agent="aha",
        to_agent="main",
        command_namespace="aha",
        original_command=command,
        result_policy="finalize",
    )
    append_event(root, run_id, "task_final_requested", {"task_id": task_id, "target": "main"})
    return f"Finalization requested for {task_id}. Task-main will write the Final when it finishes."


def format_agent_command(root: Path, run_id: str, task_id: str | None, agent_id: str | None, command: str) -> tuple[bool, str | None, str | None]:
    del root, run_id, task_id, agent_id
    suffix = command.removeprefix("/agent").strip()
    if not suffix:
        return True, None, "Usage: /agent <command> routes /<command> to the selected agent. Example: /agent status -> /status"
    return False, suffix if suffix.startswith("/") else f"/{suffix}", None


def handle_slash_command(root: Path, run_id: str, payload: dict, message: str, task_id: str | None) -> tuple[bool, str | None, dict]:
    sender = str(payload.get("sender", "browser") or "browser")
    stripped = message.strip()
    if not stripped.startswith("/"):
        return False, message, {}
    if stripped == "/agent" or stripped.startswith("/agent "):
        handled, agent_message, reply = format_agent_command(root, run_id, task_id, str(payload.get("to_agent") or payload.get("target") or "main"), stripped)
        if not handled:
            if agent_message:
                return False, agent_message, {"command_namespace": "agent", "original_command": stripped}
            reply = reply or "Usage: /agent send <message>"
    elif stripped == "/aha" or stripped.startswith("/aha "):
        target = str(payload.get("to_agent", "") or payload.get("target", "") or "main")
        append_message(root, run_id, "aha", stripped, sender=sender, task_id=task_id, role="aha", from_agent=sender, to_agent="aha", agent_id=target)
        parts = stripped.split()
        name = parts[1] if len(parts) > 1 else "help"
        if name in {"final", "finalize"}:
            reply = request_task_finalization(root, run_id, task_id, stripped)
        else:
            reply = format_aha_command(root, run_id, task_id, stripped, target)
    else:
        reply = f"Unknown command: {stripped.split()[0]}. Use /aha help or /agent <command>."

    append_event(root, run_id, "aha_command_handled", {"task_id": task_id, "command": stripped})
    response = append_message(
        root,
        run_id,
        "browser",
        reply,
        sender="AHA",
        task_id=task_id,
        role="aha",
        from_agent="aha",
        to_agent="browser",
        agent_id=target if stripped.startswith("/aha") else None,
    )
    return True, None, {"message": response}


def message_backend_autostart_config(root: Path, run_id: str, task_id: str | None, target_id: str) -> dict | None:
    if not task_id or not target_id:
        return None
    try:
        detail = task_snapshot(root, run_id, task_id)
    except KeyError:
        return None
    task = detail["task"]
    agent = next((item for item in task.get("agents", []) if item.get("id") == target_id), None)
    if not agent:
        return None
    if (agent.get("backend") or task.get("preferred_backend")) != "codex":
        return None
    state = backend_status(root, run_id, target_id, task_id=task_id)
    if state.get("status") != "stopped":
        return None
    return {
        "target": target_id,
        "task_id": task_id,
        "model": agent.get("model") or task.get("preferred_model"),
        "sandbox": agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
        "approval": agent.get("approval") or task.get("preferred_approval") or "never",
    }


def ensure_chat_offset_before_message(root: Path, run_id: str, task_id: str, target_id: str) -> None:
    offset_file = chat_offset_path(run_dir(root, run_id), target_id, task_id)
    if offset_file.exists():
        return
    inbox = inbox_path(root, run_id, target_id)
    save_chat_offset(offset_file, inbox.stat().st_size if inbox.exists() else 0)


def handle_send_payload(root: Path, run_id: str, payload: dict) -> dict:
    message = str(payload.get("message", "")).strip()
    task_id = str(payload.get("task_id", "")).strip() or None
    role = str(payload.get("role", "")).strip() or None
    target_id = str(payload.get("target", "")).strip()
    if not target_id:
        target_id = task_id if role == "sub" and task_id else "main"
    if not message:
        raise ValueError("message cannot be empty")

    handled, agent_message, command_payload = handle_slash_command(root, run_id, payload, message, task_id)
    if handled:
        return {"ok": True, "handled_by": "aha", **command_payload}

    autostart = message_backend_autostart_config(root, run_id, task_id, target_id)
    if autostart and task_id:
        ensure_chat_offset_before_message(root, run_id, task_id, target_id)

    message = agent_message or message
    sent = append_message(
        root,
        run_id,
        target_id,
        message,
        str(payload.get("sender", "browser") or "browser"),
        task_id=task_id,
        role=role,
        from_agent=str(payload.get("from_agent", "") or "") or None,
        to_agent=str(payload.get("to_agent", "") or "") or None,
        command_namespace=str(command_payload.get("command_namespace", "") or "") or None,
        original_command=str(command_payload.get("original_command", "") or "") or None,
        result_policy=str(command_payload.get("result_policy", "") or "") or None,
    )
    response = {"ok": True, "message": sent}
    if autostart:
        response["backend"] = start_backend(
            root,
            run_id,
            autostart["target"],
            model=autostart["model"],
            sandbox=autostart["sandbox"],
            approval=autostart["approval"],
            from_start=False,
            task_id=autostart["task_id"],
        )
    return response


async def handle_ui_client(root: Path, run_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        method, target, _headers, body = await read_http_request(reader)
        parsed = urlparse(target)
        path = parsed.path
        if method in {"GET", "HEAD"} and path == "/":
            writer.write(static_response(STATIC_DIR / "index.html", method))
        elif method in {"GET", "HEAD"} and path.startswith("/static/"):
            static_name = unquote(path.removeprefix("/static/"))
            if "/" in static_name or static_name.startswith("."):
                writer.write(http_response("404 Not Found", b"not found\n"))
            else:
                writer.write(static_response(STATIC_DIR / static_name, method))
        elif method in {"GET", "HEAD"} and path == "/api/status":
            response = json_response(web_status_snapshot(root, run_id))
            writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method in {"GET", "HEAD"} and path == "/api/backends":
            response = json_response({"backends": agent_backends()})
            writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method in {"GET", "HEAD"} and path == "/api/models":
            query = parse_qs(parsed.query)
            backend = query.get("backend", ["codex"])[0] or "codex"
            if backend not in agent_backend_names():
                writer.write(json_response({"error": f"unknown agent backend: {backend}"}, "400 Bad Request"))
            else:
                response = json_response({"backend": backend, "models": model_options(backend)})
                writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method in {"GET", "HEAD"} and path == "/api/workspaces":
            response = json_response(
                {
                    "root": str(HL_PROJECT_ROOT),
                    "roots": [str(root) for root in WORKSPACE_ROOTS if root.is_dir()],
                    "workspaces": workspace_options(),
                }
            )
            writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method in {"GET", "HEAD"} and path == "/api/backend":
            query = parse_qs(parsed.query)
            target = query.get("target", ["main"])[0] or "main"
            task_id = query.get("task_id", [""])[0] or None
            response = json_response(backend_status(root, run_id, target, task_id=task_id))
            writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method in {"GET", "HEAD"} and path == "/api/events":
            query = parse_qs(parsed.query)
            offset = int(query.get("offset", ["0"])[0] or "0")
            if offset < 0:
                events_path = event_path(root, run_id)
                events, new_offset = [], events_path.stat().st_size if events_path.exists() else 0
            else:
                events, new_offset = iter_jsonl_from(event_path(root, run_id), offset)
            response = json_response({"offset": new_offset, "events": events})
            writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method in {"GET", "HEAD"} and path == "/api/conversation-events":
            query = parse_qs(parsed.query)
            task_id = query.get("task_id", [""])[0]
            target = query.get("target", ["main"])[0] or "main"
            limit = int(query.get("limit", ["50"])[0] or "50")
            before_values = query.get("before_offset", []) or query.get("before", [])
            try:
                before = int(before_values[0]) if before_values and before_values[0] else None
            except ValueError:
                before = None
            if not task_id:
                writer.write(json_response({"error": "task_id required"}, "400 Bad Request"))
            else:
                response = json_response(conversation_events_page(root, run_id, task_id, target, limit=limit, before=before))
                writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method in {"GET", "HEAD"} and path.startswith("/api/task/"):
            parts = unquote(path.removeprefix("/api/task/")).split("/", 1)
            task_id = parts[0]
            detail_name = parts[1] if len(parts) > 1 else ""
            try:
                if detail_name == "logs":
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["200"])[0] or "200")
                    source = query.get("source", ["auto"])[0] or "auto"
                    before_values = query.get("before_offset", []) or query.get("before", [])
                    try:
                        before = int(before_values[0]) if before_values and before_values[0] else None
                    except ValueError:
                        before = None
                    response = json_response(task_log_page(root, run_id, task_id, limit=limit, before=before, source=source))
                elif detail_name == "final":
                    response = json_response(task_final_snapshot(root, run_id, task_id))
                elif detail_name == "context":
                    response = json_response(task_context_snapshot(root, run_id, task_id))
                elif not detail_name:
                    response = json_response(task_snapshot(root, run_id, task_id))
                else:
                    response = json_response({"error": "task detail not found"}, "404 Not Found")
                writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
            except KeyError:
                writer.write(json_response({"error": "task not found"}, "404 Not Found"))
        elif method == "POST" and path.startswith("/api/task/"):
            parts = path.removeprefix("/api/task/").split("/", 1)
            if len(parts) != 2:
                writer.write(json_response({"error": "task action required"}, "400 Bad Request"))
            else:
                task_id, action = unquote(parts[0]), parts[1]
                try:
                    if action == "hide":
                        task = set_task_hidden(root, run_id, task_id, True)
                    elif action == "restore":
                        task = set_task_hidden(root, run_id, task_id, False)
                    elif action == "delete":
                        task = delete_task(root, run_id, task_id)
                    else:
                        writer.write(json_response({"error": f"unknown task action: {action}"}, "400 Bad Request"))
                        await writer.drain()
                        return
                    writer.write(json_response({"ok": True, "task": task}))
                except SystemExit as exc:
                    writer.write(json_response({"error": str(exc)}, "404 Not Found"))
        elif method == "POST" and path == "/api/tasks":
            payload = parse_json_body(body)
            title = str(payload.get("title", "")).strip()
            if not title:
                writer.write(json_response({"error": "title cannot be empty"}, "400 Bad Request"))
            else:
                backend = str(payload.get("backend", "codex") or "codex")
                preferred_sub_backend = str(payload.get("preferred_sub_backend", "") or "") or None
                if backend not in agent_backend_names():
                    writer.write(json_response({"error": f"unknown agent backend: {backend}"}, "400 Bad Request"))
                    await writer.drain()
                    return
                if preferred_sub_backend is not None and preferred_sub_backend not in agent_backend_names():
                    writer.write(json_response({"error": f"unknown agent backend: {preferred_sub_backend}"}, "400 Bad Request"))
                    await writer.drain()
                    return
                sandbox = str(payload.get("sandbox", "") or "") or None
                approval = str(payload.get("approval", "") or "") or None
                if sandbox is not None and sandbox not in SANDBOX_OPTIONS:
                    writer.write(json_response({"error": f"unknown sandbox: {sandbox}"}, "400 Bad Request"))
                    await writer.drain()
                    return
                if approval is not None and approval not in APPROVAL_OPTIONS:
                    writer.write(json_response({"error": f"unknown approval: {approval}"}, "400 Bad Request"))
                    await writer.drain()
                    return
                task = create_task_and_dispatch(
                    root,
                    run_id,
                    title,
                    backend=backend,
                    model=str(payload.get("model", "") or "") or None,
                    workspace_path=str(payload.get("workspace_path", "") or "") or str(root),
                    sandbox=sandbox,
                    approval=approval,
                    delegation_policy=str(payload.get("delegation_policy", "auto") or "auto"),
                    max_sub_agents=int(payload.get("max_sub_agents", 3) or 0),
                    preferred_sub_backend=preferred_sub_backend,
                    preferred_sub_model=str(payload.get("preferred_sub_model", "") or "") or None,
                    dispatch=bool(payload.get("dispatch", True)),
                )
                writer.write(json_response({"ok": True, "task": task}))
        elif method == "POST" and path == "/api/agents":
            payload = parse_json_body(body)
            task_id = str(payload.get("task_id", "")).strip()
            if not task_id:
                writer.write(json_response({"error": "task_id cannot be empty"}, "400 Bad Request"))
            else:
                backend = str(payload.get("backend", "codex") or "codex")
                if backend not in agent_backend_names():
                    writer.write(json_response({"error": f"unknown agent backend: {backend}"}, "400 Bad Request"))
                    await writer.drain()
                    return
                sandbox = str(payload.get("sandbox", "") or "") or None
                approval = str(payload.get("approval", "") or "") or None
                if sandbox is not None and sandbox not in SANDBOX_OPTIONS:
                    writer.write(json_response({"error": f"unknown sandbox: {sandbox}"}, "400 Bad Request"))
                    await writer.drain()
                    return
                if approval is not None and approval not in APPROVAL_OPTIONS:
                    writer.write(json_response({"error": f"unknown approval: {approval}"}, "400 Bad Request"))
                    await writer.drain()
                    return
                agent = add_agent(root, run_id, task_id, backend=backend, role=str(payload.get("role", "sub") or "sub"), sandbox=sandbox, approval=approval)
                writer.write(json_response({"ok": True, "agent": agent}))
        elif method == "POST" and path == "/api/agent-config":
            payload = parse_json_body(body)
            task_id = str(payload.get("task_id", "")).strip()
            agent_id = str(payload.get("agent_id", "")).strip()
            sandbox = str(payload.get("sandbox", "") or "") or None
            approval = str(payload.get("approval", "") or "") or None
            if not task_id or not agent_id:
                writer.write(json_response({"error": "task_id and agent_id are required"}, "400 Bad Request"))
            elif sandbox is not None and sandbox not in SANDBOX_OPTIONS:
                writer.write(json_response({"error": f"unknown sandbox: {sandbox}"}, "400 Bad Request"))
            elif approval is not None and approval not in APPROVAL_OPTIONS:
                writer.write(json_response({"error": f"unknown approval: {approval}"}, "400 Bad Request"))
            else:
                try:
                    agent = update_agent_config(root, run_id, task_id, agent_id, sandbox=sandbox, approval=approval)
                    writer.write(json_response({"ok": True, "agent": agent}))
                except SystemExit as exc:
                    writer.write(json_response({"error": str(exc)}, "404 Not Found"))
        elif method == "POST" and path == "/api/send":
            payload = parse_json_body(body)
            try:
                writer.write(json_response(handle_send_payload(root, run_id, payload)))
            except ValueError as exc:
                writer.write(json_response({"error": str(exc)}, "400 Bad Request"))
        else:
            writer.write(http_response("404 Not Found", b"not found\n"))
        await writer.drain()
    except json.JSONDecodeError:
        writer.write(json_response({"error": "invalid json"}, "400 Bad Request"))
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def run_ui_server(root: Path, run_id: str, host: str, port: int, _poll_interval_ms: int) -> None:
    server = await asyncio.start_server(lambda r, w: handle_ui_client(root, run_id, r, w), host, port)
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"AHA dashboard for run {run_id}: http://{host}:{port}")
    print(f"Listening on {addresses}")
    async with server:
        await server.serve_forever()
