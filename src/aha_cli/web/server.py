from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from aha_cli.services.run_archive import RunArchiveError
from aha_cli.services.run_retention_policy import (
    retention_policy_report_due,
    retention_policy_schedule_config,
    scheduled_retention_policy_report,
)
from aha_cli.services.weixin import WeixinError, fetch_updates, load_account, notify_channel_start, notify_channel_stop
from aha_cli.store.config import load_config
from aha_cli.store.paths import config_path
from aha_cli.websocket.server import handle_ws_connection, ws_handshake_from_headers
from aha_cli.web.game_routes import game_route_response
from aha_cli.web.auth import (
    append_response_headers,
    auth_cookie_header,
    is_authorized_request,
    login_response,
    logout_response,
    optional_authorized_request,
    unauthorized_response,
)
from aha_cli.web.http_utils import http_response, json_response, read_http_request, static_response
from aha_cli.web.run_api import ApiRunNotFound, require_api_run_id, workspace_options
from aha_cli.web.knowledge_routes import knowledge_route_response
from aha_cli.web.run_routes import handle_run_workspace_route
from aha_cli.web.session_debug import backend_session_jsonl_info
from aha_cli.web.skill_routes import skill_route_response
from aha_cli.web.status import recover_stale_running_agent, recover_stale_running_agents, web_status_snapshot
from aha_cli.web.system_routes import (
    WEB_RESTART_EXIT_CODE,
    consume_web_restart_requested,
    system_route_response,
)
from aha_cli.web.task_actions import (
    compact_reset_selected_agent,
    finalization_prompt,
    format_agent_command,
    format_aha_command,
    handle_send_payload,
    handle_slash_command,
    request_task_finalization_with_backend,
)
from aha_cli.web.task_routes import route_task_agent_request, task_final_view_snapshot

WEIXIN_KEEPALIVE_INTERVAL_SECONDS = 30
WEB_RESTART_EXIT_DELAY_SECONDS = 0.25
RETENTION_POLICY_REPORT_MIN_SLEEP_SECONDS = 60
UI_WS_INTERVAL_SECONDS = 0.1


def exit_for_web_restart(exit_code: int = WEB_RESTART_EXIT_CODE) -> None:
    raise SystemExit(exit_code)


def schedule_web_restart_exit(delay_seconds: float = WEB_RESTART_EXIT_DELAY_SECONDS) -> None:
    loop = asyncio.get_running_loop()
    loop.call_later(delay_seconds, exit_for_web_restart, WEB_RESTART_EXIT_CODE)


def task_agent_response(route_result: dict, method: str) -> bytes | None:
    if not route_result.get("handled"):
        return None
    status = str(route_result.get("status") or "200 OK")
    if "body" in route_result:
        body = bytes(route_result.get("body") or b"")
        content_type = str(route_result.get("content_type") or "application/octet-stream")
        headers = route_result.get("headers") if isinstance(route_result.get("headers"), dict) else None
        return http_response(status, b"" if method == "HEAD" else body, content_type, headers=headers)
    if method == "HEAD":
        return http_response(status, b"", "application/json; charset=utf-8")
    return json_response(route_result.get("payload") or {}, status)


async def handle_ui_client(
    root: Path,
    run_id: str,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    auth_token: str = "",
    bind_host: str = "",
    bind_port: int | str | None = None,
) -> None:
    try:
        method, target, headers, body = await read_http_request(reader)
        parsed = urlparse(target)
        path = parsed.path
        query = parse_qs(parsed.query)
        set_auth_cookie = False
        public_ui_shell = method in {"GET", "HEAD"} and (path == "/" or path.startswith("/static/"))
        public_auth_route = path in {"/api/login", "/api/logout"}
        public_health = method in {"GET", "HEAD"} and path == "/api/health"
        if auth_token and public_ui_shell:
            authorized, set_auth_cookie = optional_authorized_request(auth_token, target, headers)
            if not authorized:
                writer.write(unauthorized_response(method))
                await writer.drain()
                return
        elif auth_token and not (public_health or public_auth_route):
            authorized, set_auth_cookie = is_authorized_request(auth_token, target, headers)
            if not authorized:
                writer.write(unauthorized_response(method))
                await writer.drain()
                return

        if method == "GET" and path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
            selected_run_id = require_api_run_id(root, run_id, query)
            ok, cursor, status_options = await ws_handshake_from_headers(root, selected_run_id, target, headers, writer)
            if ok:
                await handle_ws_connection(root, selected_run_id, reader, writer, UI_WS_INTERVAL_SECONDS, cursor, status_options)
            return

        if path == "/api/login":
            writer.write(login_response(auth_token, method, body))
        elif path == "/api/logout":
            writer.write(logout_response(method))
        elif method in {"GET", "HEAD"} and path == "/":
            response = static_response("index.html", method, headers)
            writer.write(
                append_response_headers(response, auth_cookie_header(auth_token))
                if set_auth_cookie
                else response
            )
        elif method in {"GET", "HEAD"} and path.startswith("/static/"):
            static_name = unquote(path.removeprefix("/static/"))
            if "/" in static_name or static_name.startswith("."):
                response = http_response("404 Not Found", b"not found\n")
            else:
                response = static_response(static_name, method, headers, versioned=bool(parsed.query))
            writer.write(
                append_response_headers(response, auth_cookie_header(auth_token))
                if set_auth_cookie
                else response
            )
        else:
            response = handle_run_workspace_route(root, run_id, method, path, query, headers, body)
            if response is None:
                response = knowledge_route_response(root, method, path, query, body, headers)
            if response is None:
                response = game_route_response(root, run_id, method, path)
            if response is None:
                response = skill_route_response(root, method, path, body)
            if response is None:
                response = system_route_response(
                    root,
                    run_id,
                    method,
                    path,
                    query,
                    body,
                    headers,
                    auth_required=bool(auth_token),
                    bind_host=bind_host,
                    bind_port=bind_port,
                )
            if response is None:
                route_result = await asyncio.to_thread(route_task_agent_request, root, run_id, method, path, query, body, headers)
                response = task_agent_response(route_result, method)
            if response is None:
                response = http_response("404 Not Found", b"not found\n")
            writer.write(
                append_response_headers(response, auth_cookie_header(auth_token))
                if set_auth_cookie
                else response
            )

        await writer.drain()
        if consume_web_restart_requested():
            schedule_web_restart_exit()
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


async def weixin_keepalive_loop(root: Path, interval_seconds: int = WEIXIN_KEEPALIVE_INTERVAL_SECONDS) -> None:
    notified_account_token = ""
    while True:
        try:
            account = load_account(root)
            account_token = str(account.get("token") or "")
            if account_token:
                if account_token != notified_account_token:
                    try:
                        await asyncio.to_thread(notify_channel_start, root)
                        notified_account_token = account_token
                    except WeixinError:
                        pass
                await asyncio.to_thread(fetch_updates, root)
        except asyncio.CancelledError:
            raise
        except WeixinError:
            pass
        await asyncio.sleep(max(1, interval_seconds))


async def retention_policy_report_loop(root: Path, current_run_id: str) -> None:
    while True:
        sleep_seconds = RETENTION_POLICY_REPORT_MIN_SLEEP_SECONDS
        try:
            if config_path(root).exists():
                cfg = load_config(root)
                schedule = retention_policy_schedule_config(cfg.get("retention_policy"))
                sleep_seconds = max(RETENTION_POLICY_REPORT_MIN_SLEEP_SECONDS, min(int(schedule["report_interval_seconds"]), 60 * 60))
                if schedule["scheduled_report_enabled"] and retention_policy_report_due(
                    root,
                    interval_seconds=int(schedule["report_interval_seconds"]),
                ):
                    await asyncio.to_thread(
                        scheduled_retention_policy_report,
                        root,
                        current_run_id=current_run_id or None,
                        config=schedule,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(sleep_seconds)


async def run_ui_server(root: Path, run_id: str, host: str, port: int, _poll_interval_ms: int, auth_token: str = "") -> None:
    if run_id:
        recover_stale_running_agents(root, run_id)
    server = await asyncio.start_server(lambda r, w: handle_ui_client(root, run_id, r, w, auth_token, host, port), host, port)
    weixin_keepalive = asyncio.create_task(weixin_keepalive_loop(root))
    retention_policy_reporter = asyncio.create_task(retention_policy_report_loop(root, run_id))
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    if run_id:
        print(f"AHA dashboard for run {run_id}: http://{host}:{port}")
    else:
        print(f"AHA dashboard for {root}: http://{host}:{port}")
    if auth_token:
        print("Authentication: enabled; open with ?token=<token> once or send Authorization: Bearer <token>")
    print(f"Listening on {addresses}")
    try:
        async with server:
            await server.serve_forever()
    finally:
        weixin_keepalive.cancel()
        retention_policy_reporter.cancel()
        for task in (weixin_keepalive, retention_policy_reporter):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(WeixinError):
            await asyncio.to_thread(notify_channel_stop, root)
