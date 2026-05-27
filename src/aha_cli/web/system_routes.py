from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import textwrap
import time

from aha_cli.backends.registry import agent_backend_names, agent_backends, model_options
from aha_cli.services.backend_runtime import backend_status
from aha_cli.services.weixin import (
    WeixinError,
    fetch_updates,
    recent_received_messages,
    reset_pairing,
    send_test_notification,
    start_pairing,
    status_snapshot as weixin_status_snapshot,
)
from aha_cli.services.weixin_notifications import notification_status, set_notifications_enabled
from aha_cli.store.filesystem import append_event
from aha_cli.web.conversation import MAX_EVENTS_LIMIT, conversation_view_page, event_stream_view_page, prompt_artifact_view
from aha_cli.web.http_utils import http_response, json_response, parse_json_body
from aha_cli.web.run_api import require_api_run_id
from aha_cli.web.session_debug import realtime_debug_log
from aha_cli.web.status import web_status_snapshot

WEB_RESTART_HOST = "0.0.0.0"
WEB_RESTART_PORT = 8766
WEB_RESTART_SOURCE_UNIT = "aha-ui-source-8766"
WEB_RESTART_LEGACY_UNIT = "aha-ui-8766.service"

REALTIME_DEBUG_ALLOWED_KEYS = {
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


def head_or_json(method: str, data: dict, status: str = "200 OK", request_headers: dict[str, str] | None = None) -> bytes:
    return (
        http_response(status, b"", "application/json; charset=utf-8")
        if method == "HEAD"
        else json_response(data, status, request_headers=request_headers)
    )


def query_bool(query: dict[str, list[str]], key: str) -> bool:
    return str(query.get(key, [""])[0]).strip().lower() in {"1", "true", "yes", "on"}


def query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    return int(query.get(key, [str(default)])[0] or str(default))


def selected_task_id(query: dict[str, list[str]]) -> str | None:
    return str(query.get("selected_task_id", [""])[0] or query.get("task_id", [""])[0] or "").strip() or None


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


def events_response(root: Path, run_id: str, method: str, query: dict[str, list[str]]) -> bytes:
    try:
        limit = query_int(query, "limit", 500)
        last_event_id = str(query.get("last_event_id", [""])[0] or query.get("after_event_id", [""])[0] or "").strip()
        offset = int(last_event_id) if last_event_id else int(query.get("offset", ["0"])[0] or "0")
    except ValueError:
        return json_response({"error": "offset, limit, and last_event_id must be valid event cursors"}, "400 Bad Request")
    page = event_stream_view_page(root, run_id, offset=offset, limit=max(1, min(limit, MAX_EVENTS_LIMIT)))
    realtime_debug_log(
        "api.events",
        _root=root,
        method=method,
        run_id=run_id,
        cursor_kind="last_event_id" if last_event_id else "offset",
        request_cursor=last_event_id or offset,
        limit=page["limit"],
        returned_offset=page["offset"],
        snapshot_event_id=page["snapshot_event_id"],
        event_count=len(page["events"]),
        has_more=page["has_more"],
        event_types=[str(event.get("type") or "") for event in page["events"][:8]],
    )
    return head_or_json(method, page)


def conversation_events_response(
    root: Path,
    run_id: str,
    method: str,
    query: dict[str, list[str]],
    headers: dict[str, str] | None = None,
) -> bytes:
    task_id = query.get("task_id", [""])[0]
    target = query.get("target", ["main"])[0] or "main"
    limit = query_int(query, "limit", 50)
    categories_text = query.get("categories", [""])[0]
    categories = {
        item.strip().lower()
        for item in categories_text.split(",")
        if item.strip().lower() in {"chat", "runtime", "commands", "usage"}
    } if categories_text else None
    include_command_output = query_bool(query, "include_command_output")
    before_values = query.get("before_offset", []) or query.get("before", [])
    try:
        before = int(before_values[0]) if before_values and before_values[0] else None
    except ValueError:
        before = None
    if not task_id:
        return json_response({"error": "task_id required"}, "400 Bad Request")
    response = conversation_view_page(
        root,
        run_id,
        task_id,
        target,
        limit=limit,
        before=before,
        categories=categories,
        include_command_output=include_command_output,
    )
    return head_or_json(method, response, request_headers=headers)


def prompt_artifact_response(
    root: Path,
    run_id: str,
    method: str,
    query: dict[str, list[str]],
    headers: dict[str, str] | None = None,
) -> bytes:
    ref = str(query.get("ref", [""])[0] or "").strip()
    if not ref:
        return json_response({"error": "ref required"}, "400 Bad Request")
    try:
        return head_or_json(method, prompt_artifact_view(root, run_id, ref), request_headers=headers)
    except ValueError as exc:
        return json_response({"error": str(exc)}, "400 Bad Request")
    except FileNotFoundError:
        return json_response({"error": "prompt artifact not found"}, "404 Not Found")


def system_route_response(
    root: Path,
    default_run_id: str,
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> bytes | None:
    if method in {"GET", "HEAD"} and path == "/api/status":
        run_id = require_api_run_id(root, default_run_id, query)
        payload = web_status_snapshot(root, run_id, lite=query_bool(query, "lite"), selected_task_id=selected_task_id(query))
        return head_or_json(method, payload, request_headers=headers)
    if method in {"GET", "HEAD"} and path == "/api/backends":
        return head_or_json(method, {"backends": agent_backends()})
    if method in {"GET", "HEAD"} and path == "/api/models":
        backend = query.get("backend", ["codex"])[0] or "codex"
        if backend not in agent_backend_names():
            return json_response({"error": f"unknown agent backend: {backend}"}, "400 Bad Request")
        return head_or_json(method, {"backend": backend, "models": model_options(backend)})
    if method in {"GET", "HEAD"} and path == "/api/backend":
        run_id = require_api_run_id(root, default_run_id, query)
        target = query.get("target", ["main"])[0] or "main"
        task_id = query.get("task_id", [""])[0] or None
        return head_or_json(method, backend_status(root, run_id, target, task_id=task_id))
    if method == "POST" and path == "/api/debug/realtime":
        payload = parse_json_body(body)
        run_id = require_api_run_id(root, default_run_id, query, payload)
        realtime_debug_log(
            "client",
            _root=root,
            run_id=run_id,
            **{key: payload.get(key) for key in REALTIME_DEBUG_ALLOWED_KEYS if key in payload},
        )
        return json_response({"ok": True})
    if method in {"GET", "HEAD"} and path == "/api/events":
        run_id = require_api_run_id(root, default_run_id, query)
        return events_response(root, run_id, method, query)
    if method in {"GET", "HEAD"} and path == "/api/conversation-events":
        run_id = require_api_run_id(root, default_run_id, query)
        return conversation_events_response(root, run_id, method, query, headers)
    if method in {"GET", "HEAD"} and path == "/api/prompt-artifact":
        run_id = require_api_run_id(root, default_run_id, query)
        return prompt_artifact_response(root, run_id, method, query, headers)
    if method == "POST" and path == "/api/web/restart":
        payload = parse_json_body(body) if body.strip() else {}
        run_id = require_api_run_id(root, default_run_id, query)
        try:
            port = int(payload.get("port") or WEB_RESTART_PORT)
            restart = schedule_source_web_restart(
                root,
                run_id,
                host=str(payload.get("host") or WEB_RESTART_HOST),
                port=port,
            )
            return json_response({"ok": True, **restart})
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
            return json_response({"error": f"failed to schedule web restart: {exc}"}, "500 Internal Server Error")
    if method in {"GET", "HEAD"} and path == "/api/weixin":
        run_id = require_api_run_id(root, default_run_id, query)
        payload = weixin_status_snapshot(root, run_id)
        payload["received_messages"] = recent_received_messages(root)
        payload["received_message_count"] = 0
        if method == "GET" and payload.get("paired"):
            try:
                updates = fetch_updates(root)
                payload["received_messages"] = updates.get("recent_messages") or payload["received_messages"]
                payload["received_message_count"] = updates.get("message_count") or 0
            except WeixinError as exc:
                payload["receive_error"] = str(exc)
        payload["notifications"] = notification_status(root, run_id)
        return head_or_json(method, payload, request_headers=headers)
    if method == "POST" and path == "/api/weixin/pair":
        run_id = require_api_run_id(root, default_run_id, query)
        try:
            payload = start_pairing(root, run_id)
            append_event(root, run_id, "weixin_pairing_started", {"status": payload.get("pairing", {}).get("status")})
            return json_response(payload)
        except WeixinError as exc:
            return json_response({"error": str(exc)}, "502 Bad Gateway")
    if method == "POST" and path == "/api/weixin/reset":
        run_id = require_api_run_id(root, default_run_id, query)
        try:
            payload = reset_pairing(root, run_id)
            payload["notifications"] = set_notifications_enabled(root, run_id, False)
            append_event(root, run_id, "weixin_pairing_reset", {"paired": payload.get("paired")})
            append_event(root, run_id, "weixin_notifications_updated", {"enabled": False})
            return json_response(payload)
        except WeixinError as exc:
            return json_response({"error": str(exc)}, "500 Internal Server Error")
    if method == "POST" and path == "/api/weixin/test":
        payload = parse_json_body(body) if body.strip() else {}
        run_id = require_api_run_id(root, default_run_id, query, payload)
        try:
            sent = send_test_notification(root, run_id, str(payload.get("message") or ""))
            append_event(root, run_id, "weixin_test_notification_sent", {"target": sent.get("target"), "message_id": sent.get("message_id")})
            return json_response(sent)
        except WeixinError as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
    if method == "POST" and path == "/api/weixin/notifications":
        payload = parse_json_body(body) if body.strip() else {}
        run_id = require_api_run_id(root, default_run_id, query, payload)
        raw_enabled = payload.get("enabled")
        enabled = raw_enabled if isinstance(raw_enabled, bool) else str(raw_enabled or "").strip().lower() in {"1", "true", "yes", "on"}
        notifications = set_notifications_enabled(root, run_id, enabled)
        append_event(root, run_id, "weixin_notifications_updated", {"enabled": notifications.get("enabled")})
        return json_response({"ok": True, "notifications": notifications})
    return None
