from __future__ import annotations

import ipaddress
import os
import subprocess
from pathlib import Path

from aha_cli.backends.registry import agent_backend_names, agent_backends, model_options
from aha_cli.services.app_version import aha_version
from aha_cli.services.proxy import apply_proxy_environment, core_proxy_config, proxy_configured
from aha_cli.services.token_usage import daily_token_usage_cached, start_daily_token_usage_refresh, stop_daily_token_usage_refresh
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
from aha_cli.store.config import load_config
from aha_cli.store.filesystem import append_event
from aha_cli.store.runs import run_exists
from aha_cli.store.ui_state import read_global_ui_state, read_ui_state, update_global_ui_state, update_ui_state
from aha_cli.web.conversation import MAX_EVENTS_LIMIT, conversation_view_page, event_stream_view_page, prompt_artifact_view
from aha_cli.web.http_utils import http_response, json_response, parse_json_body
from aha_cli.web.run_api import default_api_run_id, require_api_run_id
from aha_cli.web.session_debug import realtime_debug_log
from aha_cli.web.auth import bind_host_exposes_network
from aha_cli.web.status import (
    cached_backend_status,
    recover_stale_running_agents,
    web_agents_runtime_snapshot,
    web_status_snapshot,
    web_task_options_snapshot,
    web_tasks_snapshot,
)
from aha_cli.web.upgrade import web_upgrade_command, web_upgrade_status

WEB_RESTART_EXIT_CODE = 75
_web_restart_requested = False

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


def service_health_payload(
    root: Path,
    default_run_id: str,
    auth_required: bool = False,
    bind_host: str | None = None,
    bind_port: int | str | None = None,
) -> dict:
    selected_run_id = default_api_run_id(root, default_run_id)
    bind_host_text = str(bind_host or "").strip()
    bind_port_text = str(bind_port or "").strip()
    return {
        "ok": True,
        "service": "aha-web",
        "aha_home": str(root),
        "aha_version": aha_version(root),
        "auth_required": auth_required,
        "bind_host": bind_host_text,
        "bind_port": bind_port_text,
        "bind_network_visible": bind_host_exposes_network(bind_host_text) if bind_host_text else False,
        "web_upgrade": web_upgrade_status(),
        "initialized": (root / "config.json").exists(),
        "default_run_id": selected_run_id,
        "default_run_available": bool(selected_run_id),
    }


def _hostname_from_host_header(host_header: str) -> str:
    host = str(host_header or "").strip()
    if host.startswith("[") and "]" in host:
        return host[1 : host.index("]")]
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def _is_loopback_hostname(hostname: str) -> bool:
    value = hostname.strip().lower()
    if value in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _is_unspecified_hostname(hostname: str) -> bool:
    try:
        return ipaddress.ip_address(hostname.strip()).is_unspecified
    except ValueError:
        return False


def access_control_payload(
    headers: dict[str, str] | None = None,
    auth_required: bool = False,
    bind_host: str | None = None,
    bind_port: int | str | None = None,
) -> dict:
    host_header = str((headers or {}).get("host") or "")
    hostname = _hostname_from_host_header(host_header)
    loopback = _is_loopback_hostname(hostname)
    unspecified = _is_unspecified_hostname(hostname)
    bind_host_text = str(bind_host or "").strip()
    bind_port_text = str(bind_port or "").strip()
    bind_hostname = _hostname_from_host_header(bind_host_text)
    bind_network_visible = bind_host_exposes_network(bind_host_text) if bind_host_text else False
    effective_network_visible = bind_network_visible or (not bind_host_text and bool(hostname) and not loopback)
    if effective_network_visible:
        risk_level = "high"
        recommendation = (
            "network-visible bind is protected by token auth; prefer 127.0.0.1 plus SSH/VPN/TLS proxy"
            if auth_required
            else "bind to 127.0.0.1 or enable token auth behind SSH/VPN/authenticated reverse proxy"
        )
    elif loopback or (bind_host_text and not bind_network_visible):
        risk_level = "low"
        recommendation = "local loopback access"
    elif hostname:
        risk_level = "high"
        recommendation = "bind to 127.0.0.1 or put AHA behind SSH/VPN/authenticated reverse proxy"
    else:
        risk_level = "unknown"
        recommendation = "verify the UI is not exposed to an untrusted network"
    return {
        "ok": True,
        "auth_mode": "token" if auth_required else "none",
        "token_required": auth_required,
        "host_header": host_header,
        "hostname": hostname,
        "request_hostname": hostname,
        "bind_host": bind_host_text,
        "bind_port": bind_port_text,
        "bind_hostname": bind_hostname,
        "bind_network_visible": bind_network_visible,
        "loopback": loopback,
        "unspecified": unspecified,
        "risk_level": risk_level,
        "recommendation": recommendation,
    }


def request_web_restart(root: Path, run_id: str) -> dict:
    global _web_restart_requested
    _web_restart_requested = True
    payload = {
        "run_id": run_id,
        "restart": "process-exit",
        "exit_code": WEB_RESTART_EXIT_CODE,
    }
    append_event(root, run_id, "web_restart_requested", payload)
    return payload


def _web_upgrade_command() -> list[str]:
    return web_upgrade_command()


def _web_upgrade_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    try:
        config = load_config(root)
    except Exception:  # noqa: BLE001 - upgrade should still run without optional proxy config.
        return env
    proxy = core_proxy_config(config)
    if not (proxy.get("enabled") and proxy_configured(proxy)):
        return env
    values = {
        "HTTP_PROXY": proxy.get("http_proxy"),
        "HTTPS_PROXY": proxy.get("https_proxy"),
        "NO_PROXY": proxy.get("no_proxy"),
    }
    proxy_env: dict[str, str] = {}
    for key, value in values.items():
        text = str(value or "").strip()
        if text:
            proxy_env[key] = text
            proxy_env[key.lower()] = text
    return apply_proxy_environment(env, proxy_env)


def request_web_upgrade(root: Path, run_id: str) -> dict:
    command = _web_upgrade_command()
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "web-upgrade.log"
    with log_path.open("ab") as log_file:
        log_file.write(f"\n--- AHA web upgrade requested for {run_id} ---\n".encode("utf-8"))
        process = subprocess.Popen(  # noqa: S603 - fixed command, no shell.
            command,
            cwd=Path.home(),
            env=_web_upgrade_env(root),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    payload = {
        "run_id": run_id,
        "upgrade": "service-upgrade-user",
        "command": command,
        "cwd": str(Path.home()),
        "pid": process.pid,
        "log_path": str(log_path),
    }
    append_event(root, run_id, "web_upgrade_requested", payload)
    return payload


def consume_web_restart_requested() -> bool:
    global _web_restart_requested
    requested = _web_restart_requested
    _web_restart_requested = False
    return requested


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


def token_usage_daily_response(
    root: Path,
    run_id: str,
    method: str,
    query: dict[str, list[str]],
    headers: dict[str, str] | None = None,
) -> bytes:
    limit_days_text = str(query.get("limit_days", [""])[0] or "").strip()
    try:
        limit_days = int(limit_days_text) if limit_days_text else None
    except ValueError:
        return json_response({"error": "limit_days must be a valid integer"}, "400 Bad Request")
    try:
        payload = daily_token_usage_cached(
            root,
            run_id,
            timezone=str(query.get("timezone", query.get("tz", ["UTC"]))[0] or "UTC"),
            since=str(query.get("since", [""])[0] or ""),
            until=str(query.get("until", [""])[0] or ""),
            task_id=str(query.get("task_id", [""])[0] or ""),
            target=str(query.get("target", [""])[0] or ""),
            backend=str(query.get("backend", [""])[0] or ""),
            limit_days=limit_days,
            offline=query_bool(query, "offline"),
        )
    except ValueError as exc:
        return json_response({"error": str(exc)}, "400 Bad Request")
    return head_or_json(method, payload, request_headers=headers)


def token_usage_daily_refresh_response(
    root: Path,
    run_id: str,
    query: dict[str, list[str]],
) -> bytes:
    limit_days_text = str(query.get("limit_days", [""])[0] or "").strip()
    try:
        limit_days = int(limit_days_text) if limit_days_text else None
    except ValueError:
        return json_response({"error": "limit_days must be a valid integer"}, "400 Bad Request")
    try:
        payload = start_daily_token_usage_refresh(
            root,
            run_id,
            timezone=str(query.get("timezone", query.get("tz", ["UTC"]))[0] or "UTC"),
            since=str(query.get("since", [""])[0] or ""),
            until=str(query.get("until", [""])[0] or ""),
            task_id=str(query.get("task_id", [""])[0] or ""),
            target=str(query.get("target", [""])[0] or ""),
            backend=str(query.get("backend", [""])[0] or ""),
            limit_days=limit_days,
            offline=query_bool(query, "offline"),
        )
    except ValueError as exc:
        return json_response({"error": str(exc)}, "400 Bad Request")
    return json_response({"ok": True, **payload})


def token_usage_daily_stop_response(
    root: Path,
    run_id: str,
    query: dict[str, list[str]],
) -> bytes:
    limit_days_text = str(query.get("limit_days", [""])[0] or "").strip()
    try:
        limit_days = int(limit_days_text) if limit_days_text else None
    except ValueError:
        return json_response({"error": "limit_days must be a valid integer"}, "400 Bad Request")
    try:
        payload = stop_daily_token_usage_refresh(
            root,
            run_id,
            timezone=str(query.get("timezone", query.get("tz", ["UTC"]))[0] or "UTC"),
            since=str(query.get("since", [""])[0] or ""),
            until=str(query.get("until", [""])[0] or ""),
            task_id=str(query.get("task_id", [""])[0] or ""),
            target=str(query.get("target", [""])[0] or ""),
            backend=str(query.get("backend", [""])[0] or ""),
            limit_days=limit_days,
            offline=query_bool(query, "offline"),
        )
    except ValueError as exc:
        return json_response({"error": str(exc)}, "400 Bad Request")
    return json_response({"ok": True, **payload})


def system_route_response(
    root: Path,
    default_run_id: str,
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    auth_required: bool = False,
    bind_host: str | None = None,
    bind_port: int | str | None = None,
) -> bytes | None:
    if method in {"GET", "HEAD"} and path == "/api/health":
        return head_or_json(
            method,
            service_health_payload(root, default_run_id, auth_required=auth_required, bind_host=bind_host, bind_port=bind_port),
            request_headers=headers,
        )
    if method in {"GET", "HEAD"} and path == "/api/access-control":
        return head_or_json(
            method,
            access_control_payload(headers, auth_required=auth_required, bind_host=bind_host, bind_port=bind_port),
            request_headers=headers,
        )
    if method in {"GET", "HEAD"} and path == "/api/status":
        run_id = require_api_run_id(root, default_run_id, query)
        payload = web_status_snapshot(root, run_id, lite=query_bool(query, "lite"), selected_task_id=selected_task_id(query))
        return head_or_json(method, payload, request_headers=headers)
    if method in {"GET", "HEAD"} and path == "/api/task-options":
        run_id = require_api_run_id(root, default_run_id, query)
        try:
            limit = max(1, min(query_int(query, "limit", 100), 500))
        except ValueError:
            return json_response({"error": "limit must be a valid integer"}, "400 Bad Request")
        payload = web_task_options_snapshot(
            root,
            run_id,
            q=str(query.get("q", [""])[0] or "").strip(),
            status_filter=str(query.get("filter", ["active"])[0] or "active").strip(),
            include_id=str(query.get("include_id", [""])[0] or "").strip(),
            limit=limit,
        )
        return head_or_json(method, payload, request_headers=headers)
    if method in {"GET", "HEAD"} and path == "/api/tasks":
        run_id = require_api_run_id(root, default_run_id, query)
        payload = web_tasks_snapshot(root, run_id, lite=query_bool(query, "lite"), selected_task_id=selected_task_id(query))
        return head_or_json(method, payload, request_headers=headers)
    if method in {"GET", "HEAD"} and path == "/api/ui-state":
        selected_run_id = str(query.get("run_id", [""])[0] or "").strip()
        global_state = read_global_ui_state(root)
        if not selected_run_id:
            return head_or_json(method, {"run_id": "", **global_state}, request_headers=headers)
        run_id = require_api_run_id(root, default_run_id, query)
        return head_or_json(method, {"run_id": run_id, **global_state, **read_ui_state(root, run_id)}, request_headers=headers)
    if method == "PATCH" and path == "/api/ui-state":
        payload = parse_json_body(body) if body.strip() else {}
        if "last_selected_run_id" not in payload and "last_selected_task_id" not in payload and "last_selected_memo_id" not in payload:
            return json_response({"error": "last_selected_run_id, last_selected_task_id, or last_selected_memo_id is required"}, "400 Bad Request")
        response = {"ok": True}
        if "last_selected_run_id" in payload:
            selected_run_id = str(payload.get("last_selected_run_id") or "").strip()
            if selected_run_id and not run_exists(root, selected_run_id):
                return json_response({"error": f"run not found: {selected_run_id}"}, "404 Not Found")
            response.update(update_global_ui_state(root, {"last_selected_run_id": selected_run_id}))
        if "last_selected_task_id" in payload:
            run_id = require_api_run_id(root, default_run_id, query, payload)
            response["run_id"] = run_id
            response.update(update_ui_state(root, run_id, {"last_selected_task_id": payload.get("last_selected_task_id")}))
        if "last_selected_memo_id" in payload:
            run_id = require_api_run_id(root, default_run_id, query, payload)
            response["run_id"] = run_id
            response.update(update_ui_state(root, run_id, {"last_selected_memo_id": payload.get("last_selected_memo_id")}))
        return json_response(response)
    if method in {"GET", "HEAD"} and path == "/api/backends":
        return head_or_json(method, {"backends": agent_backends(load_config(root))})
    if method in {"GET", "HEAD"} and path == "/api/models":
        backend = query.get("backend", ["codex"])[0] or "codex"
        if backend not in agent_backend_names():
            return json_response({"error": f"unknown agent backend: {backend}"}, "400 Bad Request")
        return head_or_json(method, {"backend": backend, "models": model_options(backend, load_config(root))})
    if method in {"GET", "HEAD"} and path == "/api/backend":
        run_id = require_api_run_id(root, default_run_id, query)
        target = query.get("target", ["main"])[0] or "main"
        task_id = query.get("task_id", [""])[0] or None
        return head_or_json(method, cached_backend_status(root, run_id, target, task_id=task_id))
    if method == "POST" and path == "/api/usage/daily/refresh":
        run_id = require_api_run_id(root, default_run_id, query)
        return token_usage_daily_refresh_response(root, run_id, query)
    if method == "POST" and path == "/api/usage/daily/stop":
        run_id = require_api_run_id(root, default_run_id, query)
        return token_usage_daily_stop_response(root, run_id, query)
    if method in {"GET", "HEAD"} and path == "/api/usage/daily":
        run_id = require_api_run_id(root, default_run_id, query)
        return token_usage_daily_response(root, run_id, method, query, headers)
    if method in {"GET", "HEAD"} and path == "/api/agents/runtime":
        run_id = require_api_run_id(root, default_run_id, query)
        task_id = selected_task_id(query)
        if not task_id:
            return json_response({"error": "task_id required"}, "400 Bad Request")
        try:
            return head_or_json(method, web_agents_runtime_snapshot(root, run_id, task_id))
        except KeyError:
            return json_response({"error": f"task not found: {task_id}"}, "404 Not Found")
    if method == "POST" and path == "/api/agents/recover-stale":
        payload = parse_json_body(body) if body.strip() else {}
        run_id = require_api_run_id(root, default_run_id, query, payload)
        task_id = str(payload.get("task_id") or query.get("task_id", [""])[0] or "").strip() or None
        target = str(payload.get("target") or query.get("target", [""])[0] or "").strip() or None
        recovery = recover_stale_running_agents(root, run_id, task_id=task_id, target=target)
        return json_response({"ok": True, **recovery})
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
        run_id = require_api_run_id(root, default_run_id, query, payload)
        restart = request_web_restart(root, run_id)
        return json_response({"ok": True, **restart})
    if method == "POST" and path == "/api/web/upgrade":
        payload = parse_json_body(body) if body.strip() else {}
        run_id = require_api_run_id(root, default_run_id, query, payload)
        try:
            upgrade = request_web_upgrade(root, run_id)
        except FileNotFoundError as exc:
            return json_response({"error": str(exc), "web_upgrade": web_upgrade_status()}, "409 Conflict")
        return json_response({"ok": True, **upgrade})
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
