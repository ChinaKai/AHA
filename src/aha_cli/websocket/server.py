from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import struct

from aha_cli.constants import WS_GUID
from aha_cli.store.filesystem import append_message, event_path, iter_jsonl_from, status_snapshot


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


async def ws_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
    raw = await reader.readuntil(b"\r\n\r\n")
    headers = raw.decode("utf-8", errors="replace").split("\r\n")
    values: dict[str, str] = {}
    for line in headers[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip().lower()] = value.strip()
    key = values.get("sec-websocket-key")
    if not key:
        writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await writer.drain()
        return False
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
    return True


async def handle_ws_client(root: Path, run_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, interval: float) -> None:
    if not await ws_handshake(reader, writer):
        writer.close()
        await writer.wait_closed()
        return
    await ws_send_text(writer, json.dumps({"type": "status", "data": status_snapshot(root, run_id)}, ensure_ascii=False))
    offset = 0
    try:
        while True:
            events, offset = iter_jsonl_from(event_path(root, run_id), offset)
            for event in events:
                await ws_send_text(writer, json.dumps({"type": "event", "data": event}, ensure_ascii=False))
            try:
                message = await asyncio.wait_for(ws_read_text(reader), timeout=interval)
            except asyncio.TimeoutError:
                continue
            if message is None:
                break
            if not message:
                continue
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                await ws_send_text(writer, json.dumps({"type": "error", "message": "invalid json"}))
                continue
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
                await ws_send_text(writer, json.dumps({"type": "status", "data": status_snapshot(root, run_id)}, ensure_ascii=False))
            else:
                await ws_send_text(writer, json.dumps({"type": "error", "message": "unknown message type"}))
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def run_ws_server(root: Path, run_id: str, host: str, port: int, interval: float) -> None:
    server = await asyncio.start_server(lambda r, w: handle_ws_client(root, run_id, r, w, interval), host, port)
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"AHA WebSocket server for run {run_id}: ws://{host}:{port}")
    print(f"Listening on {addresses}")
    async with server:
        await server.serve_forever()
