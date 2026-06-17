from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import struct
from urllib.parse import parse_qs, urlparse

from aha_cli.constants import WS_GUID
from aha_cli.domain.models import utc_now
from aha_cli.store.event_notifications import open_event_notification_listener
from aha_cli.store.filesystem import append_message, event_stream_page, event_stream_position
from aha_cli.web.realtime_debug import realtime_debug_log
from aha_cli.web.status import web_tasks_snapshot

WS_EVENTS_LIMIT = 500
WS_HEARTBEAT_INTERVAL = 5.0
WS_EVENT_FALLBACK_INTERVAL = 1.0


async def ws_send_text(writer: asyncio.StreamWriter, message: str) -> None:
    payload = message.encode("utf-8")
    length = len(payload)
    if length < 126:
        header = bytes([0x81, length])
    elif length <= 0xFFFF:
        header = bytes([0x81, 126]) + struct.pack("!H", length)
    else:
        header = bytes([0x81, 127]) + struct.pack("!Q", length)
    writer.write(header + payload)
    await writer.drain()


async def ws_read_text(reader: asyncio.StreamReader) -> str | None:
    header = await reader.readexactly(2)
    first, second = header
    opcode = first & 0x0F
    masked = second & 0x80
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[idx % 4] for idx, byte in enumerate(payload))
    if opcode == 0x8:
        return None
    if opcode != 0x1:
        return ""
    return payload.decode("utf-8", errors="replace")


def _http_error(status: str, message: str) -> bytes:
    body = f"{message}\n".encode("utf-8")
    return (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body


def _parse_ws_cursor(query: dict[str, list[str]], max_event_id: int) -> tuple[int | None, str | None]:
    cursor_name = ""
    values: list[str] = []
    for key in ("last_event_id", "after_event_id"):
        if key in query:
            cursor_name = key
            values = query[key]
            break
    if not cursor_name:
        return None, None
    raw = str(values[0] if values else "").strip()
    if not raw:
        return None, f"{cursor_name} must be a non-negative integer event cursor"
    try:
        cursor = int(raw)
    except ValueError:
        return None, f"{cursor_name} must be a non-negative integer event cursor"
    if cursor < 0:
        return None, f"{cursor_name} must be a non-negative integer event cursor"
    if cursor > max_event_id:
        return None, f"{cursor_name} is beyond the current event stream"
    return cursor, None


def _parse_ws_status_options(query: dict[str, list[str]]) -> dict:
    lite = str((query.get("lite") or [""])[0]).strip().lower() in {"1", "true", "yes", "on"}
    selected_task_id = str((query.get("selected_task_id") or query.get("task_id") or [""])[0]).strip() or None
    return {"lite": lite, "selected_task_id": selected_task_id}


async def ws_handshake_from_headers(
    root: Path,
    run_id: str,
    target: str,
    headers: dict[str, str],
    writer: asyncio.StreamWriter,
) -> tuple[bool, int | None, dict]:
    query = parse_qs(urlparse(target).query, keep_blank_values=True)
    key = headers.get("sec-websocket-key")
    if not key:
        writer.write(_http_error("400 Bad Request", "missing Sec-WebSocket-Key"))
        await writer.drain()
        return False, None, {}
    cursor, cursor_error = _parse_ws_cursor(query, event_stream_position(root, run_id))
    if cursor_error:
        writer.write(_http_error("400 Bad Request", cursor_error))
        await writer.drain()
        return False, None, {}
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    writer.write(response.encode("ascii"))
    await writer.drain()
    return True, cursor, _parse_ws_status_options(query)


async def ws_handshake(root: Path, run_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[bool, int | None, dict]:
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.decode("utf-8", errors="replace").split("\r\n")
    request = lines[0].split()
    target = request[1] if len(request) >= 2 else "/"
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return await ws_handshake_from_headers(root, run_id, target, headers, writer)


async def _send_status(root: Path, run_id: str, writer: asyncio.StreamWriter, status_options: dict | None = None) -> None:
    options = status_options or {}
    await ws_send_text(
        writer,
        json.dumps(
            {
                "type": "status",
                "data": web_tasks_snapshot(
                    root,
                    run_id,
                    lite=bool(options.get("lite")),
                    selected_task_id=options.get("selected_task_id"),
                ),
            },
            ensure_ascii=False,
        ),
    )


async def _send_events(root: Path, run_id: str, writer: asyncio.StreamWriter, last_event_id: int, snapshot_event_id: int) -> int:
    while last_event_id < snapshot_event_id:
        page = event_stream_page(root, run_id, last_event_id, limit=WS_EVENTS_LIMIT, snapshot_event_id=snapshot_event_id)
        events = page["events"]
        if not events and int(page["last_event_id"]) == last_event_id:
            break
        for event in events:
            await ws_send_text(writer, json.dumps({"type": "event", "data": event}, ensure_ascii=False))
        last_event_id = int(page["last_event_id"])
        if not page["has_more"]:
            break
    return last_event_id


async def _send_heartbeat(writer: asyncio.StreamWriter, last_event_id: int) -> None:
    await ws_send_text(writer, json.dumps({"type": "heartbeat", "ts": utc_now(), "last_event_id": last_event_id}, ensure_ascii=False))


def _event_wait_timeout(interval: float, notification_enabled: bool) -> float:
    if not notification_enabled:
        return max(0.01, interval)
    return max(interval, WS_EVENT_FALLBACK_INTERVAL)


async def _cancel_task(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def handle_ws_connection(
    root: Path,
    run_id: str,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    interval: float,
    cursor: int | None,
    status_options: dict | None = None,
) -> None:
    conn_id = f"{id(writer):x}"
    realtime_debug_log(
        "ws",
        _root=root,
        phase="open",
        conn_id=conn_id,
        run_id=run_id,
        cursor=cursor if cursor is not None else "",
        peer=writer.get_extra_info("peername"),
    )
    listener = open_event_notification_listener(root, run_id)
    realtime_debug_log(
        "ws",
        _root=root,
        phase="event_notify_listener",
        conn_id=conn_id,
        run_id=run_id,
        enabled=bool(listener),
    )
    offset = cursor if cursor is not None else 0
    read_task: asyncio.Task | None = None
    try:
        await _send_status(root, run_id, writer, status_options)
        last_heartbeat_at = asyncio.get_running_loop().time()
        realtime_debug_log("ws", _root=root, phase="status_sent", conn_id=conn_id, run_id=run_id)
        if cursor is not None:
            snapshot_event_id = event_stream_position(root, run_id)
            before_offset = cursor
            offset = await _send_events(root, run_id, writer, cursor, snapshot_event_id)
            realtime_debug_log(
                "ws",
                _root=root,
                phase="replay_sent",
                conn_id=conn_id,
                run_id=run_id,
                from_event_id=before_offset,
                to_event_id=offset,
                snapshot_event_id=snapshot_event_id,
                sent_count=max(0, offset - before_offset),
            )
        else:
            offset = event_stream_position(root, run_id)
            realtime_debug_log("ws", _root=root, phase="tail_start", conn_id=conn_id, run_id=run_id, offset=offset)
        read_task = asyncio.create_task(ws_read_text(reader))
        while True:
            snapshot_event_id = event_stream_position(root, run_id)
            before_offset = offset
            offset = await _send_events(root, run_id, writer, offset, snapshot_event_id)
            if offset != before_offset:
                last_heartbeat_at = asyncio.get_running_loop().time()
                realtime_debug_log(
                    "ws",
                    _root=root,
                    phase="events_sent",
                    conn_id=conn_id,
                    run_id=run_id,
                    from_event_id=before_offset,
                    to_event_id=offset,
                    snapshot_event_id=snapshot_event_id,
                    sent_count=max(0, offset - before_offset),
                )
            notify_task = asyncio.create_task(listener.wait()) if listener is not None else None
            wait_tasks = {read_task}
            if notify_task is not None:
                wait_tasks.add(notify_task)
            now = asyncio.get_running_loop().time()
            heartbeat_timeout = max(0.0, WS_HEARTBEAT_INTERVAL - (now - last_heartbeat_at))
            done, _pending = await asyncio.wait(
                wait_tasks,
                timeout=min(_event_wait_timeout(interval, listener is not None), heartbeat_timeout),
                return_when=asyncio.FIRST_COMPLETED,
            )
            notified = False
            if notify_task is not None and notify_task in done:
                try:
                    notify_task.result()
                    notified = True
                except OSError as exc:
                    realtime_debug_log(
                        "ws",
                        _root=root,
                        phase="event_notify_error",
                        conn_id=conn_id,
                        run_id=run_id,
                        error=type(exc).__name__,
                    )
                    listener.close()
                    listener = None
            elif notify_task is not None:
                await _cancel_task(notify_task)
            if notified:
                realtime_debug_log("ws", _root=root, phase="event_notify_received", conn_id=conn_id, run_id=run_id, offset=offset)
            if read_task not in done:
                now = asyncio.get_running_loop().time()
                if now - last_heartbeat_at >= WS_HEARTBEAT_INTERVAL:
                    await _send_heartbeat(writer, offset)
                    last_heartbeat_at = now
                    realtime_debug_log("ws", _root=root, phase="heartbeat_sent", conn_id=conn_id, run_id=run_id, offset=offset)
                continue
            message = read_task.result()
            read_task = None
            if message is None:
                realtime_debug_log("ws", _root=root, phase="client_close_frame", conn_id=conn_id, run_id=run_id, offset=offset)
                break
            read_task = asyncio.create_task(ws_read_text(reader))
            if not message:
                realtime_debug_log("ws", _root=root, phase="client_non_text_frame", conn_id=conn_id, run_id=run_id)
                continue
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                realtime_debug_log("ws", _root=root, phase="client_invalid_json", conn_id=conn_id, run_id=run_id)
                await ws_send_text(writer, json.dumps({"type": "error", "message": "invalid json"}))
                continue
            realtime_debug_log(
                "ws",
                _root=root,
                phase="client_message",
                conn_id=conn_id,
                run_id=run_id,
                message_type=payload.get("type", ""),
                task_id=payload.get("task_id", ""),
                target=payload.get("target", ""),
            )
            if payload.get("type") == "send":
                text = payload.get("message", "")
                if text:
                    append_message(
                        root,
                        run_id,
                        payload.get("target", "main"),
                        text,
                        sender=payload.get("sender", "websocket"),
                        task_id=payload.get("task_id"),
                        role=payload.get("role"),
                        from_agent=payload.get("from_agent"),
                        to_agent=payload.get("to_agent"),
                    )
            elif payload.get("type") == "status":
                await _send_status(root, run_id, writer, status_options)
                realtime_debug_log("ws", _root=root, phase="status_sent", conn_id=conn_id, run_id=run_id)
            else:
                await ws_send_text(writer, json.dumps({"type": "error", "message": "unknown message type"}))
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError) as exc:
        realtime_debug_log("ws", _root=root, phase="disconnect_error", conn_id=conn_id, run_id=run_id, error=type(exc).__name__, offset=offset)
    finally:
        await _cancel_task(read_task)
        if listener is not None:
            listener.close()
        realtime_debug_log("ws", _root=root, phase="closed", conn_id=conn_id, run_id=run_id, offset=offset)
        writer.close()
        await writer.wait_closed()


async def handle_ws_client(root: Path, run_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, interval: float) -> None:
    ok, cursor, status_options = await ws_handshake(root, run_id, reader, writer)
    if not ok:
        writer.close()
        await writer.wait_closed()
        return
    await handle_ws_connection(root, run_id, reader, writer, interval, cursor, status_options)


async def run_ws_server(root: Path, run_id: str, host: str, port: int, interval: float) -> None:
    server = await asyncio.start_server(lambda r, w: handle_ws_client(root, run_id, r, w, interval), host, port)
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"AHA WebSocket server for run {run_id}: ws://{host}:{port}")
    print(f"Listening on {addresses}")
    async with server:
        await server.serve_forever()
