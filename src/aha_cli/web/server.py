from __future__ import annotations

import asyncio
from email.parser import BytesParser
from email.policy import default as email_policy
from importlib import resources
import json
from pathlib import Path
import tempfile
import textwrap
from urllib.parse import parse_qs, unquote, urlparse

from aha_cli.backends.registry import agent_backend_names, agent_backend_or_default, agent_backends, model_options
from aha_cli.domain.models import utc_now
from aha_cli.services.backend_runtime import (
    PROCESS_AGENT_BACKENDS,
    backend_status,
    start_backend,
    stop_backend,
)
from aha_cli.services.chat import chat_offset_path, save_chat_offset
from aha_cli.services.orchestrator import dispatch_task_to_main
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.services.run_archive import RunArchiveError, export_run_archive, import_run_archive
from aha_cli.services.session_compact import compact_reset_backend_session
from aha_cli.services.tasks import create_task_and_dispatch
from aha_cli.store.filesystem import (
    add_agent,
    add_workspace,
    append_event,
    append_message,
    append_task_round,
    config_path,
    conversation_events_page,
    create_plan,
    delete_task,
    event_agent_refs,
    event_path,
    event_stream_page,
    event_stream_position,
    event_task_id,
    inbox_path,
    iter_jsonl_from,
    iter_jsonl_reverse,
    list_task_rounds,
    list_run_summaries,
    list_workspaces,
    load_config,
    mark_task_coordination,
    require_plan,
    reopen_task,
    resolve_workspace_path,
    run_exists,
    run_dir,
    run_summary,
    read_json,
    set_agent_status,
    set_task_status,
    set_task_hidden,
    session_path,
    status_snapshot,
    task_context_snapshot,
    task_final_snapshot,
    task_log_page,
    task_snapshot,
    update_agent_config,
    update_task_proxy_config,
)
from aha_cli.websocket.server import handle_ws_connection, ws_handshake_from_headers

STATIC_PACKAGE = "aha_cli.web"
HL_PROJECT_ROOT = Path("/home/kaikai/kk-workspace/hl_project")
MY_PROJECT_ROOT = Path("/home/kaikai/kk-workspace/my_project")
WORKSPACE_ROOTS = [HL_PROJECT_ROOT, MY_PROJECT_ROOT]
SANDBOX_OPTIONS = {"read-only", "workspace-write", "danger-full-access"}
APPROVAL_OPTIONS = {"untrusted", "on-failure", "on-request", "never"}
TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}
DEFAULT_EVENTS_LIMIT = 500
MAX_EVENTS_LIMIT = 2000
AHA_BACKEND_PROMPT_MARKER = render_prompt_template("backend_prompt_prefix.md").strip()


def session_jsonl_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := session_jsonl_text(item)))
    if isinstance(value, dict):
        return "\n".join(part for item in value.values() if (part := session_jsonl_text(item)))
    return ""


def classify_aha_session_prompt(text: str) -> str:
    if AHA_BACKEND_PROMPT_MARKER not in text or "User message from" not in text:
        return ""
    if "Current delta status:" in text:
        return "sticky_delta"
    if "Current status:" in text:
        return "full"
    return "unknown"


def bump_count(counts: dict, key: str, amount: int = 1) -> None:
    counts[key] = int(counts.get(key) or 0) + amount


def message_content_text(message: dict) -> str:
    if not isinstance(message, dict):
        return ""
    return session_jsonl_text(message.get("content"))


def message_has_content_type(message: dict, content_type: str) -> bool:
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, list) and any(isinstance(item, dict) and item.get("type") == content_type for item in content)


def analyze_backend_session_jsonl(path: Path, backend: str = "") -> dict:
    type_counts: dict[str, int] = {}
    response_item_counts: dict[str, int] = {}
    response_item_chars: dict[str, int] = {}
    aha_prompt_counts: dict[str, int] = {}
    aha_prompt_chars: dict[str, int] = {}
    mirror_prompt_counts: dict[str, int] = {}
    mirror_prompt_chars: dict[str, int] = {}
    latest_aha_prompts: list[dict] = []
    line_count = 0
    parse_errors = 0
    total_payload_text_chars = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_count, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            record_type = str(record.get("type") or "unknown")
            bump_count(type_counts, record_type)
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            if backend == "claude":
                text = session_jsonl_text(record.get("message") or record.get("content") or record.get("lastPrompt") or record.get("attachment") or "")
            else:
                text = session_jsonl_text(payload)
            text_chars = len(text)
            total_payload_text_chars += text_chars

            if record_type == "response_item":
                payload_type = str(payload.get("type") or "-")
                role = str(payload.get("role") or "-")
                key = f"{payload_type}:{role}"
                bump_count(response_item_counts, key)
                bump_count(response_item_chars, key, text_chars)
                if payload_type == "message" and role == "user":
                    prompt_mode = classify_aha_session_prompt(text)
                    if prompt_mode:
                        bump_count(aha_prompt_counts, prompt_mode)
                        bump_count(aha_prompt_chars, prompt_mode, text_chars)
                        latest_aha_prompts.append({"line": line_count, "mode": prompt_mode, "chars": text_chars})
                        latest_aha_prompts = latest_aha_prompts[-6:]
            elif record_type == "event_msg":
                prompt_mode = classify_aha_session_prompt(text)
                if prompt_mode:
                    bump_count(mirror_prompt_counts, prompt_mode)
                    bump_count(mirror_prompt_chars, prompt_mode, text_chars)
            elif backend == "claude" and record_type == "queue-operation":
                content = str(record.get("content") or "")
                prompt_mode = classify_aha_session_prompt(content)
                if prompt_mode:
                    bump_count(mirror_prompt_counts, prompt_mode)
                    bump_count(mirror_prompt_chars, prompt_mode, len(content))
            elif backend == "claude" and record_type in {"user", "assistant"}:
                message = record.get("message") if isinstance(record.get("message"), dict) else {}
                content_text = message_content_text(message)
                content_chars = len(content_text)
                role = str(message.get("role") or record_type)
                key = "tool_result:user" if record_type == "user" and message_has_content_type(message, "tool_result") else f"message:{role}"
                bump_count(response_item_counts, key)
                bump_count(response_item_chars, key, content_chars)
                if record_type == "user":
                    prompt_mode = classify_aha_session_prompt(content_text)
                    if prompt_mode:
                        bump_count(aha_prompt_counts, prompt_mode)
                        bump_count(aha_prompt_chars, prompt_mode, content_chars)
                        latest_aha_prompts.append({"line": line_count, "mode": prompt_mode, "chars": content_chars})
                        latest_aha_prompts = latest_aha_prompts[-6:]

    tool_output_chars = sum(
        int(response_item_chars.get(key) or 0)
        for key in ("function_call_output:-", "custom_tool_call_output:-", "tool_result:-", "tool_result:user")
    )
    return {
        "backend": backend,
        "line_count": line_count,
        "parse_errors": parse_errors,
        "type_counts": type_counts,
        "response_item_counts": response_item_counts,
        "response_item_chars": response_item_chars,
        "aha_prompt_counts": aha_prompt_counts,
        "aha_prompt_chars": aha_prompt_chars,
        "aha_prompt_total_count": sum(int(value or 0) for value in aha_prompt_counts.values()),
        "aha_prompt_total_chars": sum(int(value or 0) for value in aha_prompt_chars.values()),
        "event_msg_prompt_mirror_counts": mirror_prompt_counts,
        "event_msg_prompt_mirror_chars": mirror_prompt_chars,
        "event_msg_prompt_mirror_total_count": sum(int(value or 0) for value in mirror_prompt_counts.values()),
        "event_msg_prompt_mirror_total_chars": sum(int(value or 0) for value in mirror_prompt_chars.values()),
        "tool_output_chars": tool_output_chars,
        "assistant_message_chars": int(response_item_chars.get("message:assistant") or 0),
        "total_payload_text_chars": total_payload_text_chars,
        "latest_prompt_mode": latest_aha_prompts[-1]["mode"] if latest_aha_prompts else "",
        "latest_aha_prompts": latest_aha_prompts,
    }


def backend_session_jsonl_info(session: dict) -> dict:
    session_id = str(session.get("backend_session_id") or "").strip()
    backend = str(session.get("backend") or "").strip()
    history = session.get("history_backend_sessions") if isinstance(session.get("history_backend_sessions"), list) else []
    compact_summary = session.get("compact_summary") if isinstance(session.get("compact_summary"), dict) else None
    if not session_id:
        return {"id": "", "backend": backend, "path": "", "size_bytes": None, "exists": False, "analysis": {}, "history": history, "compact_summary": compact_summary}

    candidates: list[Path] = []
    home = Path.home()
    if backend == "claude":
        candidates.extend((home / ".claude" / "projects").glob(f"*/*{session_id}.jsonl"))
    else:
        candidates.extend((home / ".codex" / "sessions").glob(f"**/*{session_id}.jsonl"))

    path = candidates[0] if candidates else None
    if not path or not path.exists():
        return {"id": session_id, "backend": backend, "path": "", "size_bytes": None, "exists": False, "analysis": {}, "history": history, "compact_summary": compact_summary}
    try:
        stat = path.stat()
    except OSError:
        return {"id": session_id, "backend": backend, "path": str(path), "size_bytes": None, "exists": False, "analysis": {}, "history": history, "compact_summary": compact_summary}
    try:
        analysis = analyze_backend_session_jsonl(path, backend)
    except OSError as exc:
        analysis = {"error": str(exc)}
    return {
        "id": session_id,
        "backend": backend,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
        "exists": True,
        "analysis": analysis,
        "history": history,
        "compact_summary": compact_summary,
    }


def conversation_turn_events(root: Path, run_id: str, task_id: str, target: str, limit: int = 500) -> list[dict]:
    events_file = event_path(root, run_id)
    events: list[dict] = []
    safe_limit = max(1, min(limit, 2000))
    target = target or "main"
    for offset, event in iter_jsonl_reverse(events_file) or ():
        if (
            str(event.get("type") or "") in {
                "agent_started",
                "agent_prompt_metrics",
                "agent_usage",
                "agent_context_overflow",
                "agent_thread",
                "agent_finished",
                "agent_status_changed",
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


def parse_optional_bool(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{field_name} must be a boolean")


def realtime_debug_log(source: str, **fields: object) -> None:
    root = fields.pop("_root", None)
    run_id = str(fields.get("run_id") or "")
    payload = {"ts": utc_now(), "source": source, **fields}
    line = "[aha realtime] " + json.dumps(payload, ensure_ascii=False, default=str)
    print(line, flush=True)
    if isinstance(root, Path) and run_id:
        try:
            log_dir = run_dir(root, run_id) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "realtime-debug.log").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


def task_final_view_snapshot(root: Path, run_id: str, task_id: str) -> dict:
    detail = task_final_snapshot(root, run_id, task_id)
    plan = require_plan(root, run_id)
    task = next((item for item in plan.get("tasks", []) if item.get("id") == task_id), None)
    output_name = str((task or {}).get("output_file") or "")
    if not output_name:
        return detail
    output_file = run_dir(root, run_id) / output_name
    output_meta_file = output_file.with_suffix(".meta.json")
    output_meta = read_json(output_meta_file) if output_meta_file.exists() else {}
    if output_file.exists() and output_meta.get("policy") in {"finalize", "journal", "overview"}:
        detail["result"] = output_file.read_text(encoding="utf-8")
        detail["result_meta"] = output_meta
    return detail


def is_aha_action_envelope_text(text: str) -> bool:
    stripped = str(text or "").strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
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


def conversation_view_page(
    root: Path,
    run_id: str,
    task_id: str,
    target: str,
    limit: int = 50,
    before: int | None = None,
) -> dict:
    page = conversation_events_page(root, run_id, task_id, target, limit=limit, before=before)
    events = [event for event in page.get("events", []) if not is_raw_action_agent_message(event)]
    view = dict(page)
    view["events"] = events
    view["count"] = len(events)
    view["turn_events"] = conversation_turn_events(root, run_id, task_id, target) if before is None else []
    session_file = session_path(root, run_id, task_id, target)
    view["backend_session"] = backend_session_jsonl_info(read_json(session_file)) if session_file.exists() else {}
    return view
TASK_OUTCOME_SCAN_LIMIT = 10000


class ApiRunNotFound(Exception):
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(run_id)


def http_response(
    status: str,
    body: bytes,
    content_type: str = "text/plain; charset=utf-8",
    headers: dict[str, str] | None = None,
) -> bytes:
    header_lines = [
        f"HTTP/1.1 {status}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Cache-Control: no-store",
    ]
    for key, value in (headers or {}).items():
        safe_key = str(key).replace("\r", "").replace("\n", "")
        safe_value = str(value).replace("\r", " ").replace("\n", " ")
        header_lines.append(f"{safe_key}: {safe_value}")
    header_lines.append("Connection: close")
    return ("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii") + body


def json_response(data: dict, status: str = "200 OK") -> bytes:
    return http_response(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")


def static_response(name: str, method: str) -> bytes:
    try:
        resource = resources.files(STATIC_PACKAGE).joinpath("static", name)
        if not resource.is_file():
            return http_response("404 Not Found", b"not found\n")
        body = resource.read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return http_response("404 Not Found", b"not found\n")
    suffix = Path(name).suffix
    content_type = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
    }.get(suffix, "application/octet-stream")
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


def parse_query_bool(query: dict[str, list[str]], key: str, default: bool = False) -> bool:
    if key not in query:
        return default
    return parse_optional_bool(query.get(key, [""])[0], key)


def safe_download_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)


def parse_multipart_form(headers: dict[str, str], body: bytes) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    content_type = headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("content-type must be multipart/form-data")
    message = BytesParser(policy=email_policy).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    if not message.is_multipart():
        raise ValueError("invalid multipart form")
    fields: dict[str, str] = {}
    files: dict[str, dict[str, object]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is None:
            fields[str(name)] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        else:
            files[str(name)] = {"filename": filename, "body": payload}
    return fields, files


def archive_upload_suffix(filename: str) -> str:
    name = filename.lower()
    for suffix in (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar"):
        if name.endswith(suffix):
            return suffix
    return ".tar.gz"


def request_run_id(default_run_id: str, query: dict[str, list[str]], payload: dict | None = None) -> str:
    payload_run_id = str((payload or {}).get("run_id", "") or "").strip()
    query_run_id = str(query.get("run_id", [""])[0] or "").strip()
    return payload_run_id or query_run_id or default_run_id


def default_api_run_id(root: Path, default_run_id: str) -> str:
    if default_run_id and run_exists(root, default_run_id):
        return default_run_id
    runs = list_run_summaries(root)
    return str(runs[0]["id"]) if runs else ""


def require_api_run_id(root: Path, default_run_id: str, query: dict[str, list[str]], payload: dict | None = None) -> str:
    selected_run_id = request_run_id(default_run_id, query, payload)
    if not selected_run_id:
        selected_run_id = default_api_run_id(root, default_run_id)
    if not run_exists(root, selected_run_id):
        raise ApiRunNotFound(selected_run_id)
    return selected_run_id


def workspace_options(roots: list[Path] | None = None, aha_home: Path | None = None) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    if aha_home is not None:
        for workspace in list_workspaces(aha_home):
            workspace_path = str(workspace["path"])
            seen.add(workspace_path)
            options.append(
                {
                    "id": str(workspace["id"]),
                    "name": str(workspace.get("name") or workspace["id"]),
                    "label": str(workspace.get("name") or workspace["path"]),
                    "path": workspace_path,
                    "root": str(Path(workspace_path).parent),
                    "source": "registry",
                }
            )
    workspace_roots = WORKSPACE_ROOTS if roots is None else roots
    for root in workspace_roots:
        if not root.is_dir():
            continue
        for path in sorted(item for item in root.iterdir() if item.is_dir()):
            if str(path) in seen:
                continue
            seen.add(str(path))
            options.append(
                {
                    "name": path.name,
                    "label": f"{root.name}/{path.name}",
                    "path": str(path),
                    "root": str(root),
                }
            )
    return options


def task_outcome_snapshots(root: Path, run_id: str, task_ids: set[str] | None = None) -> dict[str, dict]:
    outcomes: dict[str, dict] = {}
    wanted = {task_id for task_id in (task_ids or set()) if task_id}
    scanned = 0
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        scanned += 1
        if event.get("type") != "task_status_changed":
            if scanned >= TASK_OUTCOME_SCAN_LIMIT:
                break
            continue
        data = event.get("data") or {}
        task_id = str(data.get("task_id") or "")
        status = str(data.get("status") or "")
        if task_id and task_id not in outcomes and status in TERMINAL_TASK_STATUSES:
            outcomes[task_id] = {
                "status": status,
                "exit_code": data.get("exit_code"),
                "updated_at": event.get("ts"),
            }
            if wanted and wanted.issubset(outcomes):
                break
        if scanned >= TASK_OUTCOME_SCAN_LIMIT:
            break
    return outcomes


def task_activity_status(task: dict) -> str:
    process_statuses = {
        str(agent.get("backend_process_status") or "stopped").lower()
        for agent in task.get("agents", [])
    }
    if "busy" in process_statuses:
        return "busy"
    if str(task.get("status") or "").lower() == "running":
        return "running"
    return "idle"


def recover_stale_running_agent(root: Path, run_id: str, task: dict, agent: dict, backend_state: dict) -> bool:
    task_id = str(task.get("id") or "")
    agent_id = str(agent.get("id") or "main")
    agent_status = str(agent.get("status") or "")
    backend_process_status = str(backend_state.get("status") or "stopped").lower()
    if not task_id or not agent_id or agent_status != "running" or backend_process_status != "stopped":
        return False

    try:
        persisted_task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return False
    persisted_agent = next((item for item in persisted_task.get("agents", []) if str(item.get("id") or "") == agent_id), None)
    if (
        persisted_agent is None
        or str(persisted_task.get("status") or "") != "running"
        or str(persisted_agent.get("status") or "") != "running"
    ):
        return False

    updated_agent = set_agent_status(root, run_id, task_id, agent_id, "interrupted")
    agent.update(updated_agent)

    task_recovered = False
    other_agent_running = any(
        str(item.get("id") or "") != agent_id and str(item.get("status") or "") == "running"
        for item in persisted_task.get("agents", [])
    )
    if not other_agent_running:
        updated_task = set_task_status(root, run_id, task_id, "awaiting_user")
        for field in ("status", "exit_code", "started_at", "finished_at"):
            task[field] = updated_task.get(field)
        task_recovered = True

    append_event(
        root,
        run_id,
        "agent_status_recovered",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "from_status": "running",
            "status": "interrupted",
            "reason": "backend_process_stopped",
            "backend": {"status": backend_process_status, "pid": backend_state.get("pid")},
            "task_recovered": task_recovered,
        },
    )
    return True


def web_status_snapshot(root: Path, run_id: str) -> dict:
    snapshot = status_snapshot(root, run_id)
    task_ids = {str(task.get("id") or "") for task in snapshot.get("tasks", [])}
    outcomes = task_outcome_snapshots(root, run_id, task_ids)
    backend_cache: dict[tuple[str | None, str], dict] = {}
    for task in snapshot.get("tasks", []):
        raw_task_id = str(task.get("id") or "")
        task_id = raw_task_id or None
        for agent in task.get("agents", []):
            target = str(agent.get("id") or "main")
            key = (task_id, target)
            if key not in backend_cache:
                backend_cache[key] = backend_status(root, run_id, target, task_id=task_id)
            state = backend_cache[key]
            recover_stale_running_agent(root, run_id, task, agent, state)
            agent["backend_process_status"] = state.get("status") or "stopped"
            agent["backend_process_pid"] = state.get("pid")
            agent["backend_process_last_reply_at"] = state.get("last_reply_at")
        current_status = str(task.get("status") or "pending")
        outcome = current_status if current_status in TERMINAL_TASK_STATUSES else outcomes.get(raw_task_id, {}).get("status")
        display_status = current_status if current_status in {"running", "awaiting_user"} else outcome or current_status
        task["current_status"] = current_status
        task["outcome_status"] = outcome
        task["activity_status"] = task_activity_status(task)
        task["display_status"] = display_status
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
                "- /aha checkpoint <summary>: record a task journal checkpoint",
                "- /aha final: ask task-main to generate the Final and complete the task",
                "- /aha finalize: alias for /aha final",
                "- /aha complete: alias for /aha final",
                "- /aha reopen: cancel completion and allow follow-up messages",
                "- /aha interrupt: interrupt the selected agent's current turn",
                "- /aha session compact-reset: compact and reset selected agent backend session",
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
                f"proxy={'on' if agent.get('proxy_enabled') else 'off'} "
                f"assignment={agent.get('assignment') or agent.get('created_reason') or '-'}"
            )
        return "\n".join(lines)
    if name == "checkpoint":
        return "Use `/aha checkpoint <summary>` from the selected task conversation to record a journal checkpoint."
    if name in {"final", "finalize"}:
        return "Use `/aha final` from the selected task conversation to ask task-main to generate the Final and complete the task."
    if name in {"complete", "done"}:
        return "Use `/aha complete` as an alias for `/aha final`."
    if name in {"reopen", "resume"}:
        return "Use `/aha reopen` from the selected task conversation to unlock the task for follow-up."
    if name == "session" and len(parts) > 2 and parts[2] == "compact-reset":
        return "Use `/aha session compact-reset` from the selected task conversation to archive the current backend session and start a fresh one."
    return f"Unknown AHA command: /aha {name}. Try /aha help."


def compact_reset_selected_agent(root: Path, run_id: str, task_id: str | None, target: str, *, restart: bool = True) -> tuple[str, dict]:
    if not task_id:
        return "No task is selected.", {"ok": False, "reason": "no_task"}
    try:
        payload = compact_reset_backend_session(root, run_id, task_id, target or "main", reason="manual", restart=restart)
    except KeyError as exc:
        return f"Task or agent not found: {exc}", {"ok": False, "reason": "not_found"}
    except ValueError as exc:
        return str(exc), {"ok": False, "reason": "invalid"}
    return (
        f"Compact-reset completed for {task_id}/{target or 'main'}. "
        f"Archived `{payload.get('old_backend_session_id')}` and wrote `{payload.get('summary_path')}`.",
        payload,
    )


def format_task_journal_for_prompt(rounds: list[dict]) -> str:
    if not rounds:
        return "Task journal (chronological ordered list):\n1. (empty)"
    lines = ["Task journal (chronological ordered list):"]
    for index, item in enumerate(rounds[-50:], start=1):
        lines.append(f"{index}. {item.get('summary')}")
        lines.append(f"   - round_id: {item.get('round_id')}")
        lines.append(f"   - trigger: {item.get('trigger')}")
        changed_files = item.get("changed_files") or []
        verification = item.get("verification") or []
        risks = item.get("risks") or []
        if changed_files:
            lines.append(f"   - files: {', '.join(str(path) for path in changed_files)}")
        if verification:
            lines.append(f"   - verification: {'; '.join(str(check) for check in verification)}")
        if risks:
            lines.append(f"   - risks: {'; '.join(str(risk) for risk in risks)}")
    return "\n".join(lines)


def finalization_prompt(task_id: str, title: str, rounds: list[dict] | None = None) -> str:
    return render_prompt_template(
        "finalization.md",
        task_id=task_id,
        title=title,
        task_journal=format_task_journal_for_prompt(rounds or []),
    )


def request_task_finalization(root: Path, run_id: str, task_id: str | None, command: str) -> str:
    if not task_id:
        return "No task is selected."
    try:
        detail = task_snapshot(root, run_id, task_id)
    except KeyError:
        return f"Task not found: {task_id}"
    task = detail["task"]
    rounds = list_task_rounds(root, run_id, task_id)
    mark_task_coordination(root, run_id, task_id, final_summary_requested_at=utc_now(), final_summary_completed_at="")
    if task.get("status") not in TERMINAL_TASK_STATUSES:
        set_task_status(root, run_id, task_id, "running")
    append_message(
        root,
        run_id,
        "main",
        finalization_prompt(task_id, str(task.get("title", "")), rounds),
        sender="aha",
        task_id=task_id,
        role="main",
        from_agent="aha",
        to_agent="main",
        command_namespace="aha",
        original_command=command,
        result_policy="finalize",
    )
    append_event(root, run_id, "task_final_requested", {"task_id": task_id, "target": "main", "policy": "finalize"})
    return f"Finalization requested for {task_id}. Task-main will write the Final when it finishes."


def prepare_task_main_autostart(root: Path, run_id: str, task_id: str | None) -> dict | None:
    if not task_id:
        return None
    autostart = message_backend_autostart_config(root, run_id, task_id, "main")
    if autostart:
        ensure_chat_offset_before_message(root, run_id, task_id, "main")
    return autostart


def start_prepared_backend(root: Path, run_id: str, autostart: dict | None) -> dict | None:
    if not autostart:
        return None
    return start_backend(
        root,
        run_id,
        autostart["target"],
        backend=autostart["backend"],
        model=autostart["model"],
        sandbox=autostart["sandbox"],
        approval=autostart["approval"],
        from_start=False,
        task_id=autostart["task_id"],
    )


def request_task_finalization_with_backend(
    root: Path,
    run_id: str,
    task_id: str | None,
    command: str,
    *,
    autostart_backend: bool = True,
) -> dict:
    autostart = prepare_task_main_autostart(root, run_id, task_id) if autostart_backend else None
    message = request_task_finalization(root, run_id, task_id, command)
    payload: dict = {"message": message}
    backend = start_prepared_backend(root, run_id, autostart)
    if backend:
        payload["backend"] = backend
    return payload


def parse_task_proxy_fields(payload: dict) -> dict[str, object]:
    fields: dict[str, object] = {}
    if "proxy_enabled" in payload:
        fields["proxy_enabled"] = parse_optional_bool(payload["proxy_enabled"], "proxy_enabled")
    if "http_proxy" in payload:
        fields["http_proxy"] = str(payload.get("http_proxy", "") or "")
    if "https_proxy" in payload:
        fields["https_proxy"] = str(payload.get("https_proxy", "") or "")
    if "no_proxy" in payload:
        fields["no_proxy"] = str(payload.get("no_proxy", "") or "")
    return fields


def record_task_checkpoint(root: Path, run_id: str, task_id: str | None, command: str) -> str:
    if not task_id:
        return "No task is selected."
    parts = command.split(maxsplit=2)
    summary = parts[2].strip() if len(parts) > 2 else ""
    if not summary:
        return "Usage: /aha checkpoint <summary>"
    try:
        record = append_task_round(root, run_id, task_id, {"trigger": "manual", "summary": summary, "agents": ["browser"]})
    except KeyError:
        return f"Task not found: {task_id}"
    return f"Checkpoint recorded for {task_id}: {record['round_id']}"


def complete_selected_task(root: Path, run_id: str, task_id: str | None) -> str:
    return request_task_finalization(root, run_id, task_id, "/aha complete")


def reopen_selected_task(root: Path, run_id: str, task_id: str | None) -> str:
    if not task_id:
        return "No task is selected."
    try:
        reopen_task(root, run_id, task_id)
    except SystemExit:
        return f"Task not found: {task_id}"
    return f"{task_id} reopened. Follow-up messages are allowed again."


def interrupt_selected_agent(root: Path, run_id: str, task_id: str | None, target: str) -> tuple[str, dict]:
    if not task_id:
        return "No task is selected.", {"interrupted": False, "reason": "no_task"}
    try:
        detail = task_snapshot(root, run_id, task_id)
    except KeyError:
        return f"Task not found: {task_id}", {"interrupted": False, "reason": "task_not_found"}
    task = detail["task"]
    agent_id = target or "main"
    if not any(str(agent.get("id") or "") == agent_id for agent in task.get("agents", [])):
        return f"Agent not found: {agent_id}", {"interrupted": False, "reason": "agent_not_found", "agent_id": agent_id}
    state = backend_status(root, run_id, agent_id, task_id=task_id)
    if state.get("status") != "busy":
        return (
            f"No active turn to interrupt for {agent_id} on {task_id}.",
            {"interrupted": False, "reason": "not_busy", "agent_id": agent_id, "task_id": task_id, "backend": state},
        )
    stopped = stop_backend(root, run_id, agent_id, task_id=task_id, timeout=2.0)
    offset_file = chat_offset_path(run_dir(root, run_id), agent_id, task_id)
    inbox = inbox_path(root, run_id, agent_id)
    save_chat_offset(offset_file, inbox.stat().st_size if inbox.exists() else 0)
    set_agent_status(root, run_id, task_id, agent_id, "interrupted")
    set_task_status(root, run_id, task_id, "awaiting_user")
    append_event(
        root,
        run_id,
        "agent_interrupted",
        {"task_id": task_id, "agent_id": agent_id, "target": agent_id, "backend": stopped},
    )
    return (
        f"Interrupted {agent_id} on {task_id}. Pending user messages were not sent automatically.",
        {"interrupted": True, "agent_id": agent_id, "task_id": task_id, "backend": stopped},
    )


def format_agent_command(root: Path, run_id: str, task_id: str | None, agent_id: str | None, command: str) -> tuple[bool, str | None, str | None]:
    del root, run_id, task_id, agent_id
    suffix = command.removeprefix("/agent").strip()
    if not suffix:
        return True, None, "Usage: /agent <command> routes /<command> to the selected agent. Example: /agent status -> /status"
    return False, suffix if suffix.startswith("/") else f"/{suffix}", None


def handle_slash_command(root: Path, run_id: str, payload: dict, message: str, task_id: str | None) -> tuple[bool, str | None, dict]:
    sender = str(payload.get("sender", "browser") or "browser")
    stripped = message.strip()
    backend_autostart = None
    if not stripped.startswith("/"):
        return False, message, {}
    if stripped == "/":
        reply = format_aha_command(root, run_id, task_id, "/aha help", str(payload.get("to_agent") or payload.get("target") or "main"))
    elif stripped == "/agent" or stripped.startswith("/agent "):
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
            backend_autostart = prepare_task_main_autostart(root, run_id, task_id)
            reply = request_task_finalization(root, run_id, task_id, stripped)
        elif name == "checkpoint":
            reply = record_task_checkpoint(root, run_id, task_id, stripped)
        elif name in {"complete", "done"}:
            backend_autostart = prepare_task_main_autostart(root, run_id, task_id)
            reply = complete_selected_task(root, run_id, task_id)
        elif name in {"reopen", "resume"}:
            reply = reopen_selected_task(root, run_id, task_id)
        elif name in {"interrupt", "stop"}:
            reply, interrupt_payload = interrupt_selected_agent(root, run_id, task_id, target)
        elif name == "session" and len(parts) > 2 and parts[2] == "compact-reset":
            reply, compact_reset_payload = compact_reset_selected_agent(root, run_id, task_id, target, restart=True)
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
    command_response = {"message": response}
    if backend_autostart:
        command_response["backend_autostart"] = backend_autostart
    if "interrupt_payload" in locals():
        command_response["interrupt"] = interrupt_payload
    if "compact_reset_payload" in locals():
        command_response["compact_reset"] = compact_reset_payload
    return True, None, command_response


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
    backend = str(agent.get("backend") or task.get("preferred_backend") or "codex")
    if backend not in PROCESS_AGENT_BACKENDS:
        return None
    state = backend_status(root, run_id, target_id, task_id=task_id)
    if state.get("status") != "stopped":
        return None
    return {
        "backend": backend,
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


def task_locked_for_messages(root: Path, run_id: str, task_id: str | None) -> str | None:
    if not task_id:
        return None
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return None
    status = str(task.get("status") or "")
    return status if status in TERMINAL_TASK_STATUSES else None


def handle_send_payload(root: Path, run_id: str, payload: dict) -> dict:
    message = str(payload.get("message", "")).strip()
    task_id = str(payload.get("task_id", "")).strip() or None
    role = str(payload.get("role", "")).strip() or None
    target_id = str(payload.get("target", "")).strip()
    if not target_id:
        target_id = task_id if role == "sub" and task_id else "main"
    if not message:
        raise ValueError("message cannot be empty")

    realtime_debug_log(
        "api.send",
        _root=root,
        phase="request",
        run_id=run_id,
        task_id=task_id or "",
        target=target_id,
        role=role or "",
        sender=str(payload.get("sender", "") or ""),
        from_agent=str(payload.get("from_agent", "") or ""),
        to_agent=str(payload.get("to_agent", "") or ""),
        message_len=len(message),
        is_command=message.startswith("/"),
    )
    handled, agent_message, command_payload = handle_slash_command(root, run_id, payload, message, task_id)
    if handled:
        backend_autostart = command_payload.pop("backend_autostart", None)
        backend = start_prepared_backend(root, run_id, backend_autostart)
        if backend:
            command_payload["backend"] = backend
        realtime_debug_log(
            "api.send",
            _root=root,
            phase="handled_command",
            run_id=run_id,
            task_id=task_id or "",
            target=target_id,
            backend_started=bool(backend),
            reply_keys=sorted(command_payload.keys()),
        )
        return {"ok": True, "handled_by": "aha", **command_payload}

    locked_status = task_locked_for_messages(root, run_id, task_id)
    if locked_status:
        raise ValueError(f"task {task_id} is {locked_status}; use /aha reopen before sending follow-up messages")

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
            backend=autostart["backend"],
            model=autostart["model"],
            sandbox=autostart["sandbox"],
            approval=autostart["approval"],
            from_start=False,
            task_id=autostart["task_id"],
        )
    realtime_debug_log(
        "api.send",
        _root=root,
        phase="stored",
        run_id=run_id,
        task_id=task_id or "",
        target=target_id,
        backend_started=bool(response.get("backend")),
        response_keys=sorted(response.keys()),
    )
    return response


def start_dispatched_task_backend(root: Path, run_id: str, task: dict, dispatch: bool) -> dict | None:
    if not dispatch:
        return None
    task_id = str(task.get("id") or "")
    autostart = message_backend_autostart_config(root, run_id, task_id, "main")
    if not autostart:
        return None
    return start_backend(
        root,
        run_id,
        "main",
        backend=autostart["backend"],
        model=autostart["model"],
        sandbox=autostart["sandbox"],
        approval=autostart["approval"],
        from_start=True,
        task_id=task_id,
    )


async def handle_ui_client(root: Path, run_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        method, target, headers, body = await read_http_request(reader)
        parsed = urlparse(target)
        path = parsed.path
        query = parse_qs(parsed.query)
        if method == "GET" and path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
            selected_run_id = require_api_run_id(root, run_id, query)
            ok, cursor = await ws_handshake_from_headers(root, selected_run_id, target, headers, writer)
            if ok:
                await handle_ws_connection(root, selected_run_id, reader, writer, 1.0, cursor)
            return
        if method in {"GET", "HEAD"} and path == "/":
            writer.write(static_response("index.html", method))
        elif method in {"GET", "HEAD"} and path.startswith("/static/"):
            static_name = unquote(path.removeprefix("/static/"))
            if "/" in static_name or static_name.startswith("."):
                writer.write(http_response("404 Not Found", b"not found\n"))
            else:
                writer.write(static_response(static_name, method))
        elif method in {"GET", "HEAD"} and path == "/api/runs":
            response = json_response({"default_run_id": default_api_run_id(root, run_id), "runs": list_run_summaries(root)})
            writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method in {"GET", "HEAD"} and path == "/api/run/export":
            selected_run_id = require_api_run_id(root, run_id, query)
            no_logs = parse_query_bool(query, "no_logs", False)
            safe_run_id = safe_download_name(selected_run_id)
            with tempfile.TemporaryDirectory(prefix="aha-run-export-") as tmp:
                archive_path = export_run_archive(
                    root,
                    selected_run_id,
                    Path(tmp) / f"aha-run-{safe_run_id}.tar.gz",
                    include_logs=not no_logs,
                )
                payload = b"" if method == "HEAD" else archive_path.read_bytes()
            writer.write(
                http_response(
                    "200 OK",
                    payload,
                    "application/gzip",
                    {
                        "Content-Disposition": f'attachment; filename="aha-run-{safe_run_id}.tar.gz"',
                    },
                )
            )
        elif method == "POST" and path == "/api/run/import":
            temp_archive_path: Path | None = None
            try:
                content_type = headers.get("content-type", "")
                if content_type.lower().startswith("multipart/form-data"):
                    fields, files = parse_multipart_form(headers, body)
                    upload = files.get("archive") or files.get("file")
                    if not upload:
                        writer.write(json_response({"error": "archive file is required"}, "400 Bad Request"))
                        await writer.drain()
                        return
                    upload_body = upload.get("body")
                    if not isinstance(upload_body, bytes) or not upload_body:
                        writer.write(json_response({"error": "archive file is empty"}, "400 Bad Request"))
                        await writer.drain()
                        return
                    suffix = archive_upload_suffix(str(upload.get("filename") or "archive.tar.gz"))
                    with tempfile.NamedTemporaryFile(prefix="aha-run-import-", suffix=suffix, delete=False) as handle:
                        handle.write(upload_body)
                        temp_archive_path = Path(handle.name)
                    payload = fields
                    archive_path = temp_archive_path
                else:
                    payload = parse_json_body(body)
                    archive_path_text = str(payload.get("archive_path", "") or "").strip()
                    if not archive_path_text:
                        writer.write(json_response({"error": "archive_path is required"}, "400 Bad Request"))
                        await writer.drain()
                        return
                    archive_path = Path(archive_path_text)
                target_run_id = str(payload.get("target_run_id", "") or "").strip() or None
                preserve_id = parse_optional_bool(payload.get("preserve_id", False), "preserve_id")
                force = parse_optional_bool(payload.get("force", False), "force")
                source_run_id, imported_run_id = import_run_archive(
                    root,
                    archive_path,
                    target_run_id=target_run_id,
                    preserve_id=preserve_id,
                    force=force,
                )
                writer.write(
                    json_response(
                        {
                            "ok": True,
                            "source_run_id": source_run_id,
                            "imported_run_id": imported_run_id,
                            "run": run_summary(root, imported_run_id),
                            "runs": list_run_summaries(root),
                        },
                        "201 Created",
                    )
                )
            finally:
                if temp_archive_path is not None:
                    temp_archive_path.unlink(missing_ok=True)
        elif method in {"GET", "HEAD"} and path == "/api/bootstrap":
            runs = list_run_summaries(root)
            default_run_id = default_api_run_id(root, run_id)
            response = json_response(
                {
                    "aha_home": str(root),
                    "initialized": config_path(root).exists(),
                    "default_workspace_path": str(Path.cwd()),
                    "default_run_id": default_run_id,
                    "runs": runs,
                    "workspaces": workspace_options(aha_home=root),
                }
            )
            writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method == "POST" and path == "/api/runs":
            payload = parse_json_body(body)
            goal = str(payload.get("goal", "") or "").strip()
            if not goal:
                writer.write(json_response({"error": "goal cannot be empty"}, "400 Bad Request"))
            else:
                cfg = load_config(root)
                mode = str(payload.get("mode", cfg.get("default_mode", "research")) or "research")
                if mode not in {"research", "implementation"}:
                    writer.write(json_response({"error": f"unknown mode: {mode}"}, "400 Bad Request"))
                    await writer.drain()
                    return
                backend = str(payload.get("backend", "") or "") or agent_backend_or_default(cfg.get("backend"), "stub")
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
                task_titles = payload.get("task_titles", payload.get("tasks", []))
                if isinstance(task_titles, str):
                    task_titles = [task_titles]
                write_scopes = payload.get("write_scopes", [])
                if isinstance(write_scopes, str):
                    write_scopes = [write_scopes]
                try:
                    agents = max(1, int(payload.get("agents", 1) or 1))
                except (TypeError, ValueError):
                    writer.write(json_response({"error": "agents must be an integer"}, "400 Bad Request"))
                    await writer.drain()
                    return
                dispatch = bool(payload.get("dispatch", False))
                try:
                    workspace_path, workspace_id = resolve_workspace_path(
                        root,
                        workspace_id=str(payload.get("workspace_id", payload.get("workspace", "")) or "") or None,
                        workspace_path=str(payload.get("workspace_path", "") or "") or None,
                        default=Path.cwd(),
                    )
                except ValueError as exc:
                    writer.write(json_response({"error": str(exc)}, "400 Bad Request"))
                    await writer.drain()
                    return
                plan = create_plan(
                    root=root,
                    goal=goal,
                    agents=agents,
                    mode=mode,
                    task_titles=[str(item) for item in (task_titles or []) if str(item).strip()],
                    write_scopes=[str(item) for item in (write_scopes or []) if str(item).strip()],
                    backend=backend,
                    model=str(payload.get("model", "") or "") or None,
                    workspace_path=workspace_path,
                    workspace_id=workspace_id,
                    sandbox=sandbox,
                    approval=approval,
                    proxy_enabled=parse_optional_bool(payload.get("proxy_enabled", False), "proxy_enabled"),
                    http_proxy=str(payload.get("http_proxy", "") or "") or None,
                    https_proxy=str(payload.get("https_proxy", "") or "") or None,
                    no_proxy=str(payload.get("no_proxy", "") or "") or None,
                )
                backend_states = []
                if dispatch:
                    for task in plan.get("tasks", []):
                        dispatch_task_to_main(root, plan["id"], task)
                        backend_state = start_dispatched_task_backend(root, plan["id"], task, True)
                        if backend_state:
                            backend_states.append(backend_state)
                response = {"ok": True, "run": run_summary(root, plan["id"])}
                if backend_states:
                    response["backends"] = backend_states
                writer.write(json_response(response, "201 Created"))
        elif method in {"GET", "HEAD"} and path == "/api/status":
            selected_run_id = require_api_run_id(root, run_id, query)
            response = json_response(web_status_snapshot(root, selected_run_id))
            writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method in {"GET", "HEAD"} and path == "/api/backends":
            response = json_response({"backends": agent_backends()})
            writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method in {"GET", "HEAD"} and path == "/api/models":
            backend = query.get("backend", ["codex"])[0] or "codex"
            if backend not in agent_backend_names():
                writer.write(json_response({"error": f"unknown agent backend: {backend}"}, "400 Bad Request"))
            else:
                response = json_response({"backend": backend, "models": model_options(backend)})
                writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method in {"GET", "HEAD"} and path == "/api/workspaces":
            response = json_response(
                {
                    "aha_home": str(root),
                    "default_workspace_path": str(Path.cwd()),
                    "root": str(HL_PROJECT_ROOT),
                    "roots": [str(root) for root in WORKSPACE_ROOTS if root.is_dir()],
                    "workspaces": workspace_options(aha_home=root),
                }
            )
            writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method == "POST" and path == "/api/workspaces":
            payload = parse_json_body(body)
            workspace_path = str(payload.get("path", payload.get("workspace_path", "")) or "").strip()
            if not workspace_path:
                writer.write(json_response({"error": "workspace path is required"}, "400 Bad Request"))
                await writer.drain()
                return
            try:
                workspace = add_workspace(root, workspace_path, name=str(payload.get("name", "") or "") or None)
            except ValueError as exc:
                writer.write(json_response({"error": str(exc)}, "400 Bad Request"))
                await writer.drain()
                return
            writer.write(json_response({"ok": True, "workspace": workspace}, "201 Created"))
        elif method in {"GET", "HEAD"} and path == "/api/backend":
            selected_run_id = require_api_run_id(root, run_id, query)
            target = query.get("target", ["main"])[0] or "main"
            task_id = query.get("task_id", [""])[0] or None
            response = json_response(backend_status(root, selected_run_id, target, task_id=task_id))
            writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method == "POST" and path == "/api/debug/realtime":
            payload = parse_json_body(body)
            selected_run_id = require_api_run_id(root, run_id, query, payload)
            allowed_keys = {
                "seq",
                "stage",
                "selected_task_id",
                "target",
                "active_tab",
                "visibility",
                "online",
                "ws_state",
                "ws_ready_state",
                "last_event_id",
                "offset",
                "tail_initialized",
                "last_ws_message_age_ms",
                "message_len",
                "is_aha",
                "backend_active",
                "force_poll",
                "allow_stale_poll",
                "stale_fallback",
                "accepted_count",
                "event_count",
                "response_last_event_id",
                "response_offset",
                "snapshot_event_id",
                "has_more",
                "error",
                "reason",
                "age_ms",
                "stale_after_ms",
                "stale_socket_closed",
            }
            realtime_debug_log(
                "client",
                _root=root,
                run_id=selected_run_id,
                **{key: payload.get(key) for key in allowed_keys if key in payload},
            )
            writer.write(json_response({"ok": True}))
        elif method in {"GET", "HEAD"} and path == "/api/events":
            selected_run_id = require_api_run_id(root, run_id, query)
            try:
                limit = int(query.get("limit", [str(DEFAULT_EVENTS_LIMIT)])[0] or str(DEFAULT_EVENTS_LIMIT))
                last_event_id = str(query.get("last_event_id", [""])[0] or query.get("after_event_id", [""])[0] or "").strip()
                offset = int(last_event_id) if last_event_id else int(query.get("offset", ["0"])[0] or "0")
            except ValueError:
                writer.write(json_response({"error": "offset, limit, and last_event_id must be valid event cursors"}, "400 Bad Request"))
                await writer.drain()
                return
            safe_limit = max(1, min(limit, MAX_EVENTS_LIMIT))
            if offset < 0:
                snapshot_offset = event_stream_position(root, selected_run_id)
                page = event_stream_page(root, selected_run_id, snapshot_offset, limit=safe_limit, snapshot_event_id=snapshot_offset)
            else:
                page = event_stream_page(root, selected_run_id, offset, limit=safe_limit)
            new_offset = int(page["last_event_id"])
            snapshot_offset = int(page["snapshot_event_id"])
            has_more = new_offset < snapshot_offset
            realtime_debug_log(
                "api.events",
                _root=root,
                method=method,
                run_id=selected_run_id,
                cursor_kind="last_event_id" if last_event_id else "offset",
                request_cursor=last_event_id or offset,
                limit=safe_limit,
                returned_offset=new_offset,
                snapshot_event_id=snapshot_offset,
                event_count=len(page["events"]),
                has_more=has_more,
                event_types=[str(event.get("type") or "") for event in page["events"][:8]],
            )
            response = json_response(
                {
                    "run_id": selected_run_id,
                    "offset": new_offset,
                    "last_event_id": page["last_event_id"],
                    "snapshot_offset": snapshot_offset,
                    "snapshot_event_id": page["snapshot_event_id"],
                    "has_more": has_more,
                    "limit": safe_limit,
                    "events": page["events"],
                }
            )
            writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method in {"GET", "HEAD"} and path == "/api/conversation-events":
            selected_run_id = require_api_run_id(root, run_id, query)
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
                response = json_response(conversation_view_page(root, selected_run_id, task_id, target, limit=limit, before=before))
                writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method in {"GET", "HEAD"} and path.startswith("/api/task/"):
            selected_run_id = require_api_run_id(root, run_id, query)
            parts = unquote(path.removeprefix("/api/task/")).split("/", 1)
            task_id = parts[0]
            detail_name = parts[1] if len(parts) > 1 else ""
            try:
                if detail_name == "logs":
                    limit = int(query.get("limit", ["200"])[0] or "200")
                    source = query.get("source", ["auto"])[0] or "auto"
                    before_values = query.get("before_offset", []) or query.get("before", [])
                    try:
                        before = int(before_values[0]) if before_values and before_values[0] else None
                    except ValueError:
                        before = None
                    response = json_response(task_log_page(root, selected_run_id, task_id, limit=limit, before=before, source=source))
                elif detail_name == "final":
                    response = json_response(task_final_view_snapshot(root, selected_run_id, task_id))
                elif detail_name == "context":
                    response = json_response(task_context_snapshot(root, selected_run_id, task_id))
                elif not detail_name:
                    response = json_response(task_snapshot(root, selected_run_id, task_id))
                else:
                    response = json_response({"error": "task detail not found"}, "404 Not Found")
                writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
            except KeyError:
                writer.write(json_response({"error": "task not found"}, "404 Not Found"))
        elif method == "POST" and path.startswith("/api/task/"):
            selected_run_id = require_api_run_id(root, run_id, query)
            parts = path.removeprefix("/api/task/").split("/", 1)
            if len(parts) != 2:
                writer.write(json_response({"error": "task action required"}, "400 Bad Request"))
            else:
                task_id, action = unquote(parts[0]), parts[1]
                try:
                    if action == "hide":
                        task = set_task_hidden(root, selected_run_id, task_id, True)
                    elif action == "restore":
                        task = set_task_hidden(root, selected_run_id, task_id, False)
                    elif action in {"final", "finalize", "complete"}:
                        final_payload = request_task_finalization_with_backend(
                            root,
                            selected_run_id,
                            task_id,
                            f"/api/task/{task_id}/{action}",
                        )
                        task = task_snapshot(root, selected_run_id, task_id)["task"]
                        writer.write(json_response({"ok": True, "task": task, **final_payload}))
                        await writer.drain()
                        return
                    elif action in {"reopen", "resume"}:
                        task = reopen_task(root, selected_run_id, task_id)
                    elif action == "delete":
                        task = delete_task(root, selected_run_id, task_id)
                    elif action == "proxy":
                        payload = parse_json_body(body)
                        task = update_task_proxy_config(root, selected_run_id, task_id, **parse_task_proxy_fields(payload))
                    elif action == "session/compact-reset":
                        payload = parse_json_body(body)
                        agent_id = str(payload.get("agent_id") or payload.get("target") or "main")
                        reason = str(payload.get("reason") or "manual")
                        restart = bool(payload.get("restart", True))
                        compact_payload = compact_reset_backend_session(
                            root,
                            selected_run_id,
                            task_id,
                            agent_id,
                            reason=reason,
                            restart=restart,
                        )
                        task = task_snapshot(root, selected_run_id, task_id)["task"]
                        writer.write(json_response({"ok": True, "task": task, "compact_reset": compact_payload}))
                        await writer.drain()
                        return
                    else:
                        writer.write(json_response({"error": f"unknown task action: {action}"}, "400 Bad Request"))
                        await writer.drain()
                        return
                    writer.write(json_response({"ok": True, "task": task}))
                except (KeyError, SystemExit, ValueError) as exc:
                    writer.write(json_response({"error": str(exc)}, "404 Not Found"))
        elif method == "POST" and path == "/api/tasks":
            payload = parse_json_body(body)
            selected_run_id = require_api_run_id(root, run_id, query, payload)
            title = str(payload.get("title", "")).strip()
            description = str(payload.get("description", "") or "").strip()
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
                try:
                    workspace_path, workspace_id = resolve_workspace_path(
                        root,
                        workspace_id=str(payload.get("workspace_id", payload.get("workspace", "")) or "") or None,
                        workspace_path=str(payload.get("workspace_path", "") or "") or None,
                        default=Path.cwd(),
                    )
                except ValueError as exc:
                    writer.write(json_response({"error": str(exc)}, "400 Bad Request"))
                    await writer.drain()
                    return
                dispatch = bool(payload.get("dispatch", True))
                task = create_task_and_dispatch(
                    root,
                    selected_run_id,
                    title,
                    backend=backend,
                    model=str(payload.get("model", "") or "") or None,
                    workspace_path=workspace_path,
                    workspace_id=workspace_id,
                    sandbox=sandbox,
                    approval=approval,
                    proxy_enabled=parse_optional_bool(payload.get("proxy_enabled", False), "proxy_enabled"),
                    http_proxy=str(payload.get("http_proxy", "") or "") or None,
                    https_proxy=str(payload.get("https_proxy", "") or "") or None,
                    no_proxy=str(payload.get("no_proxy", "") or "") or None,
                    delegation_policy=str(payload.get("delegation_policy", "auto") or "auto"),
                    max_sub_agents=int(payload.get("max_sub_agents", 3) or 0),
                    preferred_sub_backend=preferred_sub_backend,
                    preferred_sub_model=str(payload.get("preferred_sub_model", "") or "") or None,
                    description=description,
                    dispatch=dispatch,
                )
                backend_state = start_dispatched_task_backend(root, selected_run_id, task, dispatch)
                response = {"ok": True, "task": task}
                if backend_state:
                    response["backend"] = backend_state
                writer.write(json_response(response))
        elif method == "POST" and path == "/api/agents":
            payload = parse_json_body(body)
            selected_run_id = require_api_run_id(root, run_id, query, payload)
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
                proxy_enabled = parse_optional_bool(payload["proxy_enabled"], "proxy_enabled") if "proxy_enabled" in payload else None
                agent = add_agent(
                    root,
                    selected_run_id,
                    task_id,
                    backend=backend,
                    role=str(payload.get("role", "sub") or "sub"),
                    sandbox=sandbox,
                    approval=approval,
                    proxy_enabled=proxy_enabled,
                )
                writer.write(json_response({"ok": True, "agent": agent}))
        elif method == "POST" and path == "/api/agent-config":
            payload = parse_json_body(body)
            selected_run_id = require_api_run_id(root, run_id, query, payload)
            task_id = str(payload.get("task_id", "")).strip()
            agent_id = str(payload.get("agent_id", "")).strip()
            sandbox = str(payload.get("sandbox", "") or "") or None
            approval = str(payload.get("approval", "") or "") or None
            proxy_enabled = parse_optional_bool(payload["proxy_enabled"], "proxy_enabled") if "proxy_enabled" in payload else None
            if not task_id or not agent_id:
                writer.write(json_response({"error": "task_id and agent_id are required"}, "400 Bad Request"))
            elif sandbox is not None and sandbox not in SANDBOX_OPTIONS:
                writer.write(json_response({"error": f"unknown sandbox: {sandbox}"}, "400 Bad Request"))
            elif approval is not None and approval not in APPROVAL_OPTIONS:
                writer.write(json_response({"error": f"unknown approval: {approval}"}, "400 Bad Request"))
            else:
                try:
                    agent = update_agent_config(
                        root,
                        selected_run_id,
                        task_id,
                        agent_id,
                        sandbox=sandbox,
                        approval=approval,
                        proxy_enabled=proxy_enabled,
                    )
                    writer.write(json_response({"ok": True, "agent": agent}))
                except SystemExit as exc:
                    writer.write(json_response({"error": str(exc)}, "404 Not Found"))
        elif method == "POST" and path == "/api/task-config":
            payload = parse_json_body(body)
            selected_run_id = require_api_run_id(root, run_id, query, payload)
            task_id = str(payload.get("task_id", "")).strip()
            if not task_id:
                writer.write(json_response({"error": "task_id is required"}, "400 Bad Request"))
            else:
                try:
                    task = update_task_proxy_config(root, selected_run_id, task_id, **parse_task_proxy_fields(payload))
                    writer.write(json_response({"ok": True, "task": task}))
                except SystemExit as exc:
                    writer.write(json_response({"error": str(exc)}, "404 Not Found"))
        elif method == "POST" and path == "/api/send":
            payload = parse_json_body(body)
            selected_run_id = require_api_run_id(root, run_id, query, payload)
            try:
                writer.write(json_response(handle_send_payload(root, selected_run_id, payload)))
            except ValueError as exc:
                writer.write(json_response({"error": str(exc)}, "400 Bad Request"))
        else:
            writer.write(http_response("404 Not Found", b"not found\n"))
        await writer.drain()
    except ApiRunNotFound as exc:
        writer.write(json_response({"error": f"run not found: {exc.run_id}"}, "404 Not Found"))
        await writer.drain()
    except json.JSONDecodeError:
        writer.write(json_response({"error": "invalid json"}, "400 Bad Request"))
        await writer.drain()
    except RunArchiveError as exc:
        writer.write(json_response({"error": str(exc)}, "400 Bad Request"))
        await writer.drain()
    except ValueError as exc:
        writer.write(json_response({"error": str(exc)}, "400 Bad Request"))
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass


async def run_ui_server(root: Path, run_id: str, host: str, port: int, _poll_interval_ms: int) -> None:
    server = await asyncio.start_server(lambda r, w: handle_ui_client(root, run_id, r, w), host, port)
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    if run_id:
        print(f"AHA dashboard for run {run_id}: http://{host}:{port}")
    else:
        print(f"AHA dashboard for {root}: http://{host}:{port}")
    print(f"Listening on {addresses}")
    async with server:
        await server.serve_forever()
