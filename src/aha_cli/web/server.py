from __future__ import annotations

import asyncio
from email.parser import BytesParser
from email.policy import default as email_policy
import gzip
from importlib import resources
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
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
    update_agent_runtime,
    update_task_supervision_config,
    update_task_proxy_config,
)
from aha_cli.websocket.server import handle_ws_connection, ws_handshake_from_headers
from aha_cli.web.conversation import DEFAULT_EVENTS_LIMIT, MAX_EVENTS_LIMIT, conversation_view_page
from aha_cli.web.http_utils import (
    http_response,
    json_response,
    parse_json_body,
    parse_multipart_form,
    parse_optional_bool,
    parse_query_bool,
    read_http_request,
    static_response,
)
from aha_cli.web.run_api import (
    ApiRunNotFound,
    HL_PROJECT_ROOT,
    WORKSPACE_ROOTS,
    archive_upload_suffix,
    default_api_run_id,
    require_api_run_id,
    safe_download_name,
    workspace_options,
)
from aha_cli.web.session_debug import backend_session_jsonl_info, realtime_debug_log
from aha_cli.web.status import recover_stale_running_agent, web_status_snapshot
from aha_cli.web.task_actions import (
    compact_reset_selected_agent,
    finalization_prompt,
    format_agent_command,
    format_aha_command,
    handle_send_payload,
    handle_slash_command,
    parse_task_proxy_fields,
    parse_task_supervision_fields,
    request_task_finalization_with_backend,
    start_dispatched_task_backend,
)

SANDBOX_OPTIONS = {"read-only", "workspace-write", "danger-full-access"}
APPROVAL_OPTIONS = {"untrusted", "on-failure", "on-request", "never"}
WEB_RESTART_HOST = "0.0.0.0"
WEB_RESTART_PORT = 8766
WEB_RESTART_SOURCE_UNIT = "aha-ui-source-8766"
WEB_RESTART_LEGACY_UNIT = "aha-ui-8766.service"


def schedule_source_web_restart(root: Path, run_id: str, *, host: str = WEB_RESTART_HOST, port: int = WEB_RESTART_PORT) -> dict:
    safe_host = str(host or WEB_RESTART_HOST).strip() or WEB_RESTART_HOST
    safe_port = int(port or WEB_RESTART_PORT)
    if safe_port < 1 or safe_port > 65535:
        raise ValueError("port must be between 1 and 65535")
    source_root = Path.cwd()
    service_unit = WEB_RESTART_SOURCE_UNIT if safe_port == WEB_RESTART_PORT else f"aha-ui-source-{safe_port}"
    restart_unit = f"aha-ui-source-restart-{safe_port}-{int(time.time())}-{os.getpid()}"
    source_service = f"{service_unit}.service"
    script = textwrap.dedent(
        f"""
        set -e
        if systemctl --user show -p LoadState --value {shlex.quote(source_service)} 2>/dev/null | grep -qv '^not-found$'; then
          systemctl --user restart {shlex.quote(source_service)}
          exit 0
        fi
        systemctl --user stop {shlex.quote(WEB_RESTART_LEGACY_UNIT)} >/dev/null 2>&1 || true
        if command -v fuser >/dev/null 2>&1; then
          fuser -k {safe_port}/tcp >/dev/null 2>&1 || true
        fi
        systemd-run --user \\
          --collect \\
          --unit={shlex.quote(service_unit)} \\
          --working-directory={shlex.quote(str(source_root))} \\
          --setenv=PYTHONPATH=src \\
          --property=Restart=always \\
          --property=RestartSec=2 \\
          {shlex.quote(sys.executable)} -m aha_cli ui {shlex.quote(run_id)} --host {shlex.quote(safe_host)} --port {safe_port}
        """
    ).strip()
    command = [
        "systemd-run",
        "--user",
        "--on-active=1s",
        f"--unit={restart_unit}",
        "/usr/bin/env",
        "bash",
        "-lc",
        script,
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=5)
    payload = {
        "run_id": run_id,
        "host": safe_host,
        "port": safe_port,
        "source_root": str(source_root),
        "scheduler": "systemd-run",
        "restart_unit": restart_unit,
        "service_unit": source_service,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    append_event(root, run_id, "web_restart_requested", payload)
    return payload


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

async def handle_ui_client(root: Path, run_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        method, target, headers, body = await read_http_request(reader)
        parsed = urlparse(target)
        path = parsed.path
        query = parse_qs(parsed.query)
        if method == "GET" and path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
            selected_run_id = require_api_run_id(root, run_id, query)
            ok, cursor, status_options = await ws_handshake_from_headers(root, selected_run_id, target, headers, writer)
            if ok:
                await handle_ws_connection(root, selected_run_id, reader, writer, 1.0, cursor, status_options)
            return
        if method in {"GET", "HEAD"} and path == "/":
            writer.write(static_response("index.html", method, headers))
        elif method in {"GET", "HEAD"} and path.startswith("/static/"):
            static_name = unquote(path.removeprefix("/static/"))
            if "/" in static_name or static_name.startswith("."):
                writer.write(http_response("404 Not Found", b"not found\n"))
            else:
                writer.write(static_response(static_name, method, headers))
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
                    "backends": agent_backends(),
                },
                request_headers=headers,
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
            lite = str(query.get("lite", [""])[0]).strip().lower() in {"1", "true", "yes", "on"}
            selected_task_id = str(query.get("selected_task_id", [""])[0] or query.get("task_id", [""])[0] or "").strip() or None
            response = json_response(web_status_snapshot(root, selected_run_id, lite=lite, selected_task_id=selected_task_id), request_headers=headers)
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
            categories_text = query.get("categories", [""])[0]
            categories = {
                item.strip().lower()
                for item in categories_text.split(",")
                if item.strip().lower() in {"chat", "runtime", "commands", "usage"}
            } if categories_text else None
            include_command_output = str(query.get("include_command_output", [""])[0]).strip().lower() in {"1", "true", "yes", "on"}
            before_values = query.get("before_offset", []) or query.get("before", [])
            try:
                before = int(before_values[0]) if before_values and before_values[0] else None
            except ValueError:
                before = None
            if not task_id:
                writer.write(json_response({"error": "task_id required"}, "400 Bad Request"))
            else:
                response = json_response(
                    conversation_view_page(
                        root,
                        selected_run_id,
                        task_id,
                        target,
                        limit=limit,
                        before=before,
                        categories=categories,
                        include_command_output=include_command_output,
                    ),
                    request_headers=headers,
                )
                writer.write(http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response)
        elif method == "POST" and path == "/api/web/restart":
            selected_run_id = require_api_run_id(root, run_id, query)
            payload = parse_json_body(body) if body.strip() else {}
            try:
                port = int(payload.get("port") or WEB_RESTART_PORT)
                restart = schedule_source_web_restart(
                    root,
                    selected_run_id,
                    host=str(payload.get("host") or WEB_RESTART_HOST),
                    port=port,
                )
                writer.write(json_response({"ok": True, **restart}))
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
                writer.write(json_response({"error": f"failed to schedule web restart: {exc}"}, "500 Internal Server Error"))
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
                    elif action == "supervision":
                        payload = parse_json_body(body)
                        task = update_task_supervision_config(root, selected_run_id, task_id, **parse_task_supervision_fields(payload))
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
                supervision = None
                if "supervision" in payload:
                    if not isinstance(payload.get("supervision"), dict):
                        writer.write(json_response({"error": "supervision must be an object"}, "400 Bad Request"))
                        await writer.drain()
                        return
                    try:
                        supervision = parse_task_supervision_fields(payload["supervision"])
                    except ValueError as exc:
                        writer.write(json_response({"error": str(exc)}, "400 Bad Request"))
                        await writer.drain()
                        return
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
                    supervision=supervision,
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
                    if "supervision" in payload and isinstance(payload.get("supervision"), dict):
                        task = update_task_supervision_config(
                            root,
                            selected_run_id,
                            task_id,
                            **parse_task_supervision_fields(payload["supervision"]),
                        )
                    else:
                        task = update_task_proxy_config(root, selected_run_id, task_id, **parse_task_proxy_fields(payload))
                    writer.write(json_response({"ok": True, "task": task}))
                except (SystemExit, ValueError) as exc:
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
