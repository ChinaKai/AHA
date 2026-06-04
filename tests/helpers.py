from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from unittest import mock

from aha_cli.store.filesystem import append_jsonl, set_agent_status, set_task_status
from aha_cli.web.server import handle_ui_client
from aha_cli.websocket.server import handle_ws_client, ws_read_text

AHA_RUNTIME_ENV_KEYS = (
    "AHA_HOME",
    "AHA_ROOT",
    "AHA_RUN_ID",
    "AHA_TASK_ID",
    "AHA_AGENT_ID",
    "AHA_BACKEND",
    "AHA_MODEL",
    "AHA_GENERATED_BY",
)


def _is_temp_path(value: str) -> bool:
    if not value:
        return False
    try:
        path = Path(value).expanduser().resolve(strict=False)
        temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return path == temp_root or temp_root in path.parents


@contextmanager
def isolated_cli_environment(*, allow_temp_aha_home: bool = True, allow_aha_keys: Iterable[str] = ()):
    allowed_keys = set(allow_aha_keys)
    env = {}
    for key, value in os.environ.items():
        if not key.startswith("AHA_"):
            env[key] = value
        elif key in allowed_keys:
            env[key] = value
        elif key == "AHA_VERSION":
            env[key] = value
        elif allow_temp_aha_home and key in {"AHA_HOME", "AHA_ROOT"} and _is_temp_path(value):
            env[key] = value
    with mock.patch.dict(os.environ, env, clear=True):
        yield


def write_plan_statuses(root_path: str, run_id: str, task_id: str, agent_id: str, iterations: int) -> None:
    root = Path(root_path)
    for _ in range(iterations):
        set_task_status(root, run_id, task_id, "running")
        set_agent_status(root, run_id, task_id, agent_id, "running")


def append_jsonl_records(path: str, worker_id: int, iterations: int) -> None:
    for index in range(iterations):
        append_jsonl(Path(path), {"worker": worker_id, "index": index})


async def fetch_ui_response(
    root: Path,
    run_id: str,
    target: str,
    timeout: float = 1.0,
    method: str = "GET",
    payload: dict | None = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    auth_token: str = "",
) -> bytes:
    server = await asyncio.start_server(lambda reader, writer: handle_ui_client(root, run_id, reader, writer, auth_token), "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()
    try:
        reader, writer = await asyncio.open_connection(host, port)
        body_bytes = body if body is not None else (json.dumps(payload).encode("utf-8") if payload is not None else b"")
        request_headers = {"Host": "test", "Connection": "close", **(headers or {})}
        if payload is not None and not any(key.lower() == "content-type" for key in request_headers):
            request_headers["Content-Type"] = "application/json"
        header_lines = [f"{method} {target} HTTP/1.1"]
        header_lines.extend(f"{key}: {value}" for key, value in request_headers.items())
        header_lines.append(f"Content-Length: {len(body_bytes)}")
        writer.write(
            ("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii")
            + body_bytes
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return response
    finally:
        server.close()
        await server.wait_closed()


async def fetch_initial_ws_messages(root: Path, run_id: str, timeout: float = 0.2) -> list[dict]:
    return await fetch_ws_messages(root, run_id, timeout=timeout)


async def fetch_ws_messages(root: Path, run_id: str, path: str = "/", timeout: float = 0.2, max_messages: int = 2) -> list[dict]:
    server = await asyncio.start_server(lambda reader, writer: handle_ws_client(root, run_id, reader, writer, 0.05), "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()
    writer = None
    try:
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(
            (
                f"GET {path} HTTP/1.1\r\n"
                "Host: test\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        messages = []
        while len(messages) < max_messages:
            try:
                next_message = await asyncio.wait_for(ws_read_text(reader), timeout=timeout)
            except asyncio.TimeoutError:
                break
            if next_message:
                messages.append(json.loads(next_message))
        writer.close()
        await writer.wait_closed()
        return messages
    finally:
        if writer and not writer.is_closing():
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()


def json_response_body(response: bytes) -> dict:
    return json.loads(response.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
