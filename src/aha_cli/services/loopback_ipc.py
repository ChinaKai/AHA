"""Cross-platform loopback IPC for bridge servers and their clients.

POSIX binds an ``AF_UNIX`` socket at a filesystem path. Some Windows Python
builds lack ``AF_UNIX`` entirely, so on those we serve the IPC over a
``127.0.0.1`` TCP socket and store the chosen port in the path file. The same
``path`` is passed to both server and clients, so callers stay path-based and
need not know which transport is in use.

Also used by the browser bridge IPC (server + liveness probe + WS client).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from pathlib import Path


def supports_unix() -> bool:
    return hasattr(socket, "AF_UNIX")


def _write_port(path: Path, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(port), encoding="utf-8")


def _read_port(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text) if text else None
    except ValueError:
        return None


async def start_server(client_handler, *, path: Path, limit: int = 65536):
    """Start a loopback IPC server bound to ``path``. Returns the asyncio server."""
    if supports_unix():
        server = await asyncio.start_unix_server(client_handler, path=str(path), limit=limit)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return server
    server = await asyncio.start_server(client_handler, host="127.0.0.1", port=0, limit=limit)
    port = int(server.sockets[0].getsockname()[1])
    _write_port(path, port)
    return server


async def open_connection(path: Path, *, limit: int = 65536):
    """Connect to a loopback IPC server bound to ``path``. Returns (reader, writer)."""
    if supports_unix():
        return await asyncio.open_unix_connection(str(path), limit=limit)
    port = _read_port(path)
    if not port:
        raise OSError(f"no loopback IPC port at {path}")
    return await asyncio.open_connection("127.0.0.1", port, limit=limit)


def is_accepting(path: Path) -> bool:
    """Sync liveness probe: is a server listening at ``path``?"""
    if supports_unix():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(0.2)
            client.connect(str(path))
            return True
        except OSError:
            return False
        finally:
            client.close()
    port = _read_port(path)
    if not port:
        return False
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.settimeout(0.2)
        client.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        client.close()
