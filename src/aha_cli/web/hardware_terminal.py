from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aha_cli.domain.models import task_hardware_debug_can_write, task_hardware_group
from aha_cli.services.hardware_bridge import (
    bridge_status,
    device_stream_page,
    device_terminal_socket_path,
    ensure_bridge,
    task_serial_group_target,
)
from aha_cli.services.serial_ports import list_serial_ports
from aha_cli.web.http_utils import http_response
from aha_cli.services.local_terminal import normalize_terminal_size
from aha_cli.services.network_terminal import (
    ensure_network_terminal,
    network_status,
    network_stream_page,
    network_terminal_socket_path,
    task_network_target,
)
from aha_cli.store.filesystem import task_snapshot
from aha_cli.websocket.server import ws_read_text, ws_send_text

_TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}
_INITIAL_EVENT_LIMIT = 1000
_MAX_INPUT_CHARS = 65536
_IPC_CONNECT_TIMEOUT_SECONDS = 4.0


def hardware_serial_ports_response(method: str, path: str) -> bytes | None:
    """``GET /api/hardware/serial-ports`` — detected serial devices for the dropdown."""
    if method != "GET" or path != "/api/hardware/serial-ports":
        return None
    body = json.dumps({"ports": list_serial_ports()}, ensure_ascii=False).encode("utf-8")
    return http_response("200 OK", body, "application/json; charset=utf-8")


def hardware_terminal_target(task: dict, requested: object = "", hardware_id: object = "") -> dict | None:
    transports: list[str] = []
    group = task_hardware_group(task, hardware_id)
    if not group:
        return None
    serial_target = task_serial_group_target(task, group.get("id"))
    network_target = task_network_target(task, group.get("id"))
    credentials = group.get("credentials") if isinstance(group.get("credentials"), dict) else {}
    if serial_target:
        transports.append("serial")
    if network_target:
        transports.append("network")
    transport = {"uart": "serial", "telnet": "network"}.get(
        str(requested or "").strip().lower(),
        str(requested or "").strip().lower(),
    )
    if transport not in transports:
        transport = transports[0] if transports else ""
    if not transport:
        return None
    if transport == "network":
        host, port, username, password = network_target or ("", 23, "", "")
        return {
            "transport": transport,
            "transports": transports,
            "endpoint": f"{host}:{port}",
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "credentials": credentials,
            "hardware": group.get("id"),
            "description": group.get("description") or "",
        }
    device, baudrate = serial_target or ("", 115200)
    return {
        "transport": transport,
        "transports": transports,
        "endpoint": device,
        "device": device,
        "baudrate": baudrate,
        "credentials": credentials,
        "hardware": group.get("id"),
        "description": group.get("description") or "",
    }


def _is_read_only(task: dict, hardware_id: object = "") -> bool:
    return _is_archived(task) or not task_hardware_debug_can_write(task, hardware_id)


def _is_archived(task: dict) -> bool:
    return bool(task.get("deleted_at")) or str(task.get("status") or "") in _TERMINAL_TASK_STATUSES


def _ensure_target(root: Path, target: dict) -> dict:
    if target["transport"] == "network":
        return ensure_network_terminal(
            root,
            target["host"],
            target["port"],
            username=target["username"],
            password=target["password"],
        )
    return ensure_bridge(root, target["device"], target["baudrate"])


def _target_status(root: Path, target: dict) -> dict:
    if target["transport"] == "network":
        return network_status(root, target["host"], target["port"])
    return bridge_status(root, target["device"])


def _stream_page(root: Path, target: dict, *, after: int | None, before: int | None = None, limit: int) -> dict:
    if target["transport"] == "network":
        return network_stream_page(root, target["host"], target["port"], after=after, before=before, limit=limit)
    return device_stream_page(root, target["device"], after=after, before=before, limit=limit)


def _target_socket_path(root: Path, target: dict) -> Path:
    if target["transport"] == "network":
        return network_terminal_socket_path(root, target["host"], target["port"])
    return device_terminal_socket_path(root, target["device"])


async def _open_target_ipc(root: Path, target: dict) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    socket_path = _target_socket_path(root, target)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _IPC_CONNECT_TIMEOUT_SECONDS
    last_error: OSError | None = None
    use_unix = hasattr(socket, "AF_UNIX")
    while loop.time() < deadline:
        try:
            if use_unix:
                return await asyncio.open_unix_connection(str(socket_path))
            # Windows (no AF_UNIX): the bridge wrote its loopback TCP port to the
            # socket_path file; connect to 127.0.0.1:<port>.
            port = _read_ipc_port(socket_path)
            if port:
                return await asyncio.open_connection("127.0.0.1", int(port))
        except OSError as exc:
            last_error = exc
        await asyncio.sleep(0.02)
    raise ConnectionError(f"hardware terminal bridge IPC unavailable: {last_error or socket_path}")


def _read_ipc_port(socket_path: Path) -> int | None:
    try:
        text = socket_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text) if text else None
    except ValueError:
        return None


def _arm_auto_login(root: Path, task: dict, target: dict) -> None:
    # Best-effort: arm login/password rules from the task's credentials so opening
    # the terminal logs in automatically. Never let it block the terminal.
    try:
        from aha_cli.services.hardware_login import arm_auto_login

        credentials = target.get("credentials") if isinstance(target.get("credentials"), dict) else {}
        if target.get("transport") == "network":
            arm_auto_login(root, credentials, host=target.get("host"), port=target.get("port"))
        else:
            arm_auto_login(root, credentials, device=target.get("device"))
    except Exception:
        pass


async def _send_ipc_message(writer: asyncio.StreamWriter, message_type: str, **data: object) -> None:
    payload = json.dumps({"type": message_type, **data}, ensure_ascii=False, separators=(",", ":")) + "\n"
    writer.write(payload.encode("utf-8"))
    await writer.drain()


async def _read_ipc_message(reader: asyncio.StreamReader) -> dict | None:
    line = await reader.readline()
    if not line:
        return None
    payload = json.loads(line.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid bridge IPC frame")
    return payload


async def _send_message(writer: asyncio.StreamWriter, message_type: str, **data: object) -> None:
    await ws_send_text(writer, json.dumps({"type": message_type, **data}, ensure_ascii=False))


async def _send_rx_events(writer: asyncio.StreamWriter, events: list[dict]) -> None:
    data = "".join(
        str(event.get("data") or "")
        for event in events
        if str(event.get("direction") or "").lower() == "rx"
    )
    if data:
        await _send_message(writer, "output", data=data)


async def handle_hardware_terminal_ws_connection(
    root: Path,
    run_id: str,
    target_url: str,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    query = parse_qs(urlparse(target_url).query, keep_blank_values=True)
    task_id = str((query.get("task_id") or [""])[0] or "").strip()
    requested_transport = str((query.get("transport") or [""])[0] or "").strip()
    hardware_id = str((query.get("hardware") or [""])[0] or "").strip()
    cols, rows = normalize_terminal_size(
        (query.get("cols") or [""])[0],
        (query.get("rows") or [""])[0],
    )
    browser_task: asyncio.Task[str | None] | None = None
    ipc_task: asyncio.Task[dict | None] | None = None
    ipc_writer: asyncio.StreamWriter | None = None
    try:
        if not task_id:
            await _send_message(writer, "error", message="task_id is required")
            return
        task = task_snapshot(root, run_id, task_id)["task"]
        target = hardware_terminal_target(task, requested_transport, hardware_id)
        if not target:
            await _send_message(writer, "error", message="task has no hardware terminal configured")
            return
        read_only = _is_read_only(task, target.get("hardware"))
        archived = _is_archived(task)
        replay_boundary: int | None = None
        ipc_reader: asyncio.StreamReader | None = None
        if not archived:
            _ensure_target(root, target)
            _arm_auto_login(root, task, target)
            ipc_reader, ipc_writer = await _open_target_ipc(root, target)
            hello = await asyncio.wait_for(
                _read_ipc_message(ipc_reader),
                timeout=_IPC_CONNECT_TIMEOUT_SECONDS,
            )
            if not hello or hello.get("type") != "ready":
                raise ConnectionError("hardware terminal bridge IPC did not become ready")
            replay_boundary = max(0, int(hello.get("after_offset") or 0))
            await _send_ipc_message(ipc_writer, "resize", cols=cols, rows=rows)
        status = _target_status(root, target)
        await _send_message(
            writer,
            "ready",
            run_id=run_id,
            task_id=task_id,
            transport=target["transport"],
            transports=target["transports"],
            endpoint=target["endpoint"],
            read_only=read_only,
            cols=cols,
            rows=rows,
            bridge=status,
        )
        page = _stream_page(
            root,
            target,
            after=None,
            before=replay_boundary,
            limit=_INITIAL_EVENT_LIMIT,
        )
        await _send_rx_events(writer, page.get("events") or [])
        live_offset = int(page.get("after_offset") or replay_boundary or 0)
        browser_task = asyncio.create_task(ws_read_text(reader))
        if ipc_reader is not None:
            ipc_task = asyncio.create_task(_read_ipc_message(ipc_reader))
        while True:
            pending_tasks = {task for task in (browser_task, ipc_task) if task is not None}
            if not pending_tasks:
                break
            done, _pending = await asyncio.wait(pending_tasks, return_when=asyncio.FIRST_COMPLETED)

            if ipc_task is not None and ipc_task in done:
                ipc_payload = ipc_task.result()
                ipc_task = None
                if ipc_payload is None:
                    break
                ipc_type = str(ipc_payload.get("type") or "")
                if ipc_type == "output":
                    offset = max(0, int(ipc_payload.get("offset") or 0))
                    if not offset or offset > live_offset:
                        await _send_message(writer, "output", data=str(ipc_payload.get("data") or ""))
                    live_offset = max(live_offset, offset)
                elif ipc_type == "status":
                    await _send_message(writer, "status", bridge=ipc_payload.get("bridge") or {})
                if ipc_reader is not None:
                    ipc_task = asyncio.create_task(_read_ipc_message(ipc_reader))

            if browser_task is not None and browser_task in done:
                message = browser_task.result()
                browser_task = None
                if message is None:
                    break
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    await _send_message(writer, "error", message="invalid json")
                    browser_task = asyncio.create_task(ws_read_text(reader))
                    continue
                message_type = str(payload.get("type") or "")
                if message_type == "close":
                    break
                if message_type == "input":
                    if read_only or ipc_writer is None:
                        await _send_message(writer, "error", message="terminal is read-only")
                    else:
                        await _send_ipc_message(
                            ipc_writer,
                            "input",
                            data=str(payload.get("data") or "")[:_MAX_INPUT_CHARS],
                        )
                elif message_type == "resize":
                    if ipc_writer is not None:
                        cols, rows = normalize_terminal_size(payload.get("cols"), payload.get("rows"))
                        await _send_ipc_message(ipc_writer, "resize", cols=cols, rows=rows)
                else:
                    await _send_message(writer, "error", message="unknown message type")
                browser_task = asyncio.create_task(ws_read_text(reader))
    except (KeyError, SystemExit) as exc:
        await _send_message(writer, "error", message=f"task not found: {exc}")
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    except Exception as exc:
        await _send_message(writer, "error", message=str(exc))
    finally:
        for task in (browser_task, ipc_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if ipc_writer is not None:
            ipc_writer.close()
            await ipc_writer.wait_closed()
        writer.close()
        await writer.wait_closed()


__all__ = ["handle_hardware_terminal_ws_connection", "hardware_terminal_target"]
