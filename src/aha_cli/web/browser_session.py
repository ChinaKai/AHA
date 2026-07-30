from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import uuid

from aha_cli.services.browser_bridge import (
    BrowserBridgeError,
    open_browser_bridge_ipc,
)
from aha_cli.websocket.server import ws_read_text, ws_send_text

_MAX_WEB_MESSAGE_CHARS = 1024 * 1024


async def _send_web(writer: asyncio.StreamWriter, payload: dict) -> None:
    await ws_send_text(writer, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


async def _send_ipc(writer: asyncio.StreamWriter, payload: dict) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    writer.write(encoded)
    await writer.drain()


async def _read_ipc(reader: asyncio.StreamReader) -> dict | None:
    raw = await reader.readline()
    if not raw:
        return None
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid browser bridge frame")
    return payload


async def handle_browser_session_ws_connection(
    root: Path,
    run_id: str,
    target_url: str,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    query = parse_qs(urlparse(target_url).query, keep_blank_values=True)
    task_id = str((query.get("task_id") or [""])[0] or "").strip()
    browser_task: asyncio.Task[str | None] | None = None
    ipc_task: asyncio.Task[dict | None] | None = None
    ipc_writer: asyncio.StreamWriter | None = None
    try:
        if not task_id:
            await _send_web(writer, {"type": "error", "code": "task_required", "message": "task_id is required"})
            return
        ipc_reader, ipc_writer, ready = await open_browser_bridge_ipc(
            root,
            run_id,
            task_id,
            parent_bound=True,
        )
        await _send_web(
            writer,
            {
                "type": "ready",
                "run_id": run_id,
                "task_id": task_id,
                "state": ready.get("state") or {},
            },
        )
        ready_state = ready.get("state") if isinstance(ready.get("state"), dict) else {}
        display = ready_state.get("display") if isinstance(ready_state.get("display"), dict) else {}
        if display.get("active") != "native":
            subscribe_id = uuid.uuid4().hex
            await _send_ipc(
                ipc_writer,
                {
                    "type": "command",
                    "id": subscribe_id,
                    "action": "subscribe",
                    "args": {},
                    "source": "user",
                    "agent_id": "browser",
                },
            )
        browser_task = asyncio.create_task(ws_read_text(reader))
        ipc_task = asyncio.create_task(_read_ipc(ipc_reader))
        while True:
            done, _pending = await asyncio.wait(
                {browser_task, ipc_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ipc_task in done:
                payload = ipc_task.result()
                if payload is None:
                    break
                await _send_web(writer, payload)
                ipc_task = asyncio.create_task(_read_ipc(ipc_reader))
            if browser_task in done:
                message = browser_task.result()
                if message is None:
                    break
                if len(message) > _MAX_WEB_MESSAGE_CHARS:
                    await _send_web(
                        writer,
                        {"type": "error", "code": "message_too_large", "message": "Browser message is too large."},
                    )
                    browser_task = asyncio.create_task(ws_read_text(reader))
                    continue
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    await _send_web(writer, {"type": "error", "code": "invalid_json", "message": "Invalid JSON."})
                    browser_task = asyncio.create_task(ws_read_text(reader))
                    continue
                if not isinstance(payload, dict):
                    await _send_web(writer, {"type": "error", "code": "invalid_frame", "message": "Expected an object."})
                    browser_task = asyncio.create_task(ws_read_text(reader))
                    continue
                if payload.get("type") == "close":
                    break
                if payload.get("type") != "command":
                    await _send_web(
                        writer,
                        {"type": "error", "code": "invalid_frame", "message": "Expected a command frame."},
                    )
                    browser_task = asyncio.create_task(ws_read_text(reader))
                    continue
                await _send_ipc(
                    ipc_writer,
                    {
                        "type": "command",
                        "id": str(payload.get("id") or uuid.uuid4().hex),
                        "action": str(payload.get("action") or ""),
                        "args": payload.get("args") if isinstance(payload.get("args"), dict) else {},
                        "source": "user",
                        "agent_id": "browser",
                    },
                )
                browser_task = asyncio.create_task(ws_read_text(reader))
    except BrowserBridgeError as exc:
        await _send_web(writer, {"type": "error", "code": exc.code, "message": str(exc)})
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    except Exception as exc:
        await _send_web(writer, {"type": "error", "code": "browser_session_failed", "message": str(exc)})
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
            try:
                await ipc_writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, BrokenPipeError):
            pass


__all__ = ["handle_browser_session_ws_connection"]
