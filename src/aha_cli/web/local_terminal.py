from __future__ import annotations

import asyncio
import ipaddress
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aha_cli.services.local_terminal import LocalTerminalSession, normalize_terminal_size
from aha_cli.websocket.server import ws_read_text, ws_send_text


def local_terminal_peer_allowed(peer: object) -> bool:
    if not isinstance(peer, tuple) or not peer:
        return False
    host = str(peer[0] or "")
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in {"localhost", ""}


async def _send_terminal_message(writer: asyncio.StreamWriter, message_type: str, **data: object) -> None:
    await ws_send_text(writer, json.dumps({"type": message_type, **data}, ensure_ascii=False))


async def handle_local_terminal_ws_connection(
    root: Path,
    run_id: str,
    target: str,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    query = parse_qs(urlparse(target).query, keep_blank_values=True)
    cols, rows = normalize_terminal_size((query.get("cols") or [""])[0], (query.get("rows") or [""])[0])
    session = LocalTerminalSession()
    loop = asyncio.get_running_loop()
    read_task: asyncio.Task[str | None] | None = None
    output_task: asyncio.Task[bytes | None] | None = None
    exit_task: asyncio.Task[int] | None = None
    try:
        session.start(cols=cols, rows=rows)
        session.attach_reader(loop)
        await _send_terminal_message(
            writer,
            "ready",
            run_id=run_id,
            cwd=str(session.cwd),
            shell=session.shell,
            cols=cols,
            rows=rows,
        )
        read_task = asyncio.create_task(ws_read_text(reader))
        output_task = asyncio.create_task(session.read())
        exit_task = asyncio.create_task(session.wait())
        while True:
            done, _pending = await asyncio.wait(
                {task for task in (read_task, output_task, exit_task) if task is not None},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if output_task in done:
                chunk = output_task.result()
                output_task = None
                if chunk is None:
                    continue
                await _send_terminal_message(writer, "output", data=chunk.decode("utf-8", errors="replace"))
                output_task = asyncio.create_task(session.read())
            if read_task in done:
                message = read_task.result()
                read_task = None
                if message is None:
                    break
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    await _send_terminal_message(writer, "error", message="invalid json")
                    read_task = asyncio.create_task(ws_read_text(reader))
                    continue
                message_type = str(payload.get("type") or "")
                if message_type == "input":
                    session.write(str(payload.get("data") or ""))
                elif message_type == "resize":
                    session.resize(cols=payload.get("cols"), rows=payload.get("rows"))
                elif message_type == "close":
                    break
                else:
                    await _send_terminal_message(writer, "error", message="unknown message type")
                read_task = asyncio.create_task(ws_read_text(reader))
            if exit_task in done:
                returncode = exit_task.result()
                exit_task = None
                await _send_terminal_message(writer, "exit", returncode=returncode)
                break
    except Exception as exc:
        await _send_terminal_message(writer, "error", message=str(exc))
    finally:
        session.detach_reader(loop)
        for task in (read_task, output_task, exit_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await session.terminate()
        writer.close()
        await writer.wait_closed()
