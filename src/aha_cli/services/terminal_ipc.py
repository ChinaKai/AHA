"""Local realtime IPC shared by machine-level hardware terminal bridges."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import socket
import stat
import time
from typing import Iterable

from aha_cli.store.io import iter_jsonl_records_from

_MAX_CLIENTS = 8
_MAX_FRAME_BYTES = 256 * 1024
_MAX_PENDING_BYTES = 1024 * 1024

# Control inbox hygiene (shared by the serial and network bridges).
_CONTROL_MAX_BYTES = 2 * 1024 * 1024          # rotate the control inbox above this size
_CONTROL_RETRY_ATTEMPTS = 6                   # transient WSL/Windows share errors are retried
_CONTROL_RETRY_DELAY = 0.05
_BRIDGE_HEARTBEAT_TTL = 15.0                  # a bridge with a fresh heartbeat is alive even when
                                              # its PID is not checkable from this OS (WSL vs Windows)


def read_control_records(path: Path, start: int = 0, limit: int = 200) -> tuple[list[tuple[dict, int]], int]:
    """Read ``control.jsonl`` records with bounded retries.

    On Windows the control file lives on the shared WSL/Windows filesystem and a
    transient ``OSError`` (Errno 22) while the peer holds the file was observed to
    crash the bridge. Retry with backoff instead of terminating the daemon.
    """
    last_error: OSError | None = None
    for attempt in range(_CONTROL_RETRY_ATTEMPTS):
        try:
            return iter_jsonl_records_from(path, start=start, limit=limit)
        except OSError as exc:
            last_error = exc
            if attempt + 1 < _CONTROL_RETRY_ATTEMPTS:
                time.sleep(_CONTROL_RETRY_DELAY * (attempt + 1))
    raise last_error or OSError(f"cannot read {path}")


def rotate_control_file(path: Path) -> None:
    """Archive ``control.jsonl`` once it grows beyond a size threshold.

    Large file transfers / many arm records were observed to grow the inbox to
    megabytes; archives keep a forensic copy and reset the live file. The consumer
    offset is cleared because the archived records are no longer consumed.
    """
    if not path.exists():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < _CONTROL_MAX_BYTES:
        return
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive_dir = path.parent / "archive"
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        target = archive_dir / f"control.{stamp}.jsonl"
        path.rename(target)
    except OSError:
        # Renaming must never break the caller (append/read); truncate as fallback.
        try:
            path.write_text("", encoding="utf-8")
        except OSError:
            pass
    offset_path = Path(str(path) + ".offset")
    try:
        offset_path.unlink(missing_ok=True)
    except OSError:
        pass


def state_has_fresh_heartbeat(state: dict | None, *, ttl: float = _BRIDGE_HEARTBEAT_TTL) -> bool:
    """True when ``state`` carries a heartbeat updated within ``ttl`` seconds.

    Used together with PID checks: a bridge whose PID cannot be resolved from this
    OS (a Windows-owned process observed from WSL) is still alive while its
    heartbeat is fresh, so callers do not tear it down / respawn it.
    """
    if not isinstance(state, dict):
        return False
    raw = state.get("heartbeat_at")
    if raw is None:
        return False
    try:
        # Heartbeats are written as Unix epoch floats by the daemons; tolerate ISO
        # timestamps from older state files too.
        value = float(raw)
    except (TypeError, ValueError):
        text = str(raw)
        try:
            from datetime import datetime

            value = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return False
    return (time.time() - value) <= ttl


def stamp_control_generation(record: dict, state: dict | None) -> dict:
    """Stamp a control record (e.g. ``stop``) with the target bridge's generation.

    A freshly spawned bridge ignores stop records whose generation predates it, so
    ``hardware-stop`` immediately followed by ``hardware-attach`` cannot kill the
    new bridge with the previous instance's stop.
    """
    record = dict(record)
    if state:
        generation = int(state.get("generation") or 0)
        if generation > 0:
            record["generation"] = generation
    return record


@dataclass
class _ClientState:
    incoming: bytearray = field(default_factory=bytearray)
    outgoing: bytearray = field(default_factory=bytearray)


class BridgeTerminalIpc:
    """Non-blocking Unix socket fan-out for one bridge process.

    Frames are newline-delimited JSON objects. The bridge remains the only owner
    of the physical UART/Telnet transport; Web clients attach to this local IPC.
    """

    def __init__(self, socket_path: Path, stream_path: Path) -> None:
        self.socket_path = socket_path
        self.stream_path = stream_path
        self._listener: socket.socket | None = None
        self._clients: dict[socket.socket, _ClientState] = {}

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if hasattr(socket, "AF_UNIX"):
            self._start_unix()
        else:
            self._start_tcp_loopback()

    def _start_unix(self) -> None:
        if self.socket_path.exists():
            if not stat.S_ISSOCK(self.socket_path.stat().st_mode):
                raise RuntimeError(f"terminal IPC path is not a socket: {self.socket_path}")
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(_MAX_CLIENTS)
            listener.setblocking(False)
        except Exception:
            listener.close()
            raise
        self._listener = listener

    def _start_tcp_loopback(self) -> None:
        # No AF_UNIX on this build (e.g. some Windows Python): serve the IPC over
        # a localhost TCP socket and record the chosen port in socket_path so
        # clients can find it. Same newline-JSON framing; the daemon's select loop
        # works unchanged because Windows select supports TCP sockets.
        if self.socket_path.exists():
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(_MAX_CLIENTS)
            listener.setblocking(False)
            port = int(listener.getsockname()[1])
            self.socket_path.write_text(str(port), encoding="utf-8")
        except Exception:
            listener.close()
            raise
        self._listener = listener

    def close(self) -> None:
        for client in list(self._clients):
            self._disconnect(client)
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except OSError:
            pass

    def readables(self) -> list[socket.socket]:
        sockets = list(self._clients)
        if self._listener is not None:
            sockets.insert(0, self._listener)
        return sockets

    def writables(self) -> list[socket.socket]:
        return [client for client, state in self._clients.items() if state.outgoing]

    def process(self, readable: Iterable[object], writable: Iterable[object]) -> list[dict]:
        readable_set = set(readable)
        writable_set = set(writable)
        commands: list[dict] = []
        if self._listener is not None and self._listener in readable_set:
            commands.extend(self._accept_all())
        for client in list(self._clients):
            if client in readable_set:
                commands.extend(self._read_client(client))
            if client in self._clients and (client in writable_set or self._clients[client].outgoing):
                self._flush_client(client)
        return commands

    def broadcast(self, message_type: str, **data: object) -> None:
        frame = self._encode({"type": message_type, **data})
        for client in list(self._clients):
            self._queue(client, frame)
            if client in self._clients:
                self._flush_client(client)

    def _accept_all(self) -> list[dict]:
        commands: list[dict] = []
        if self._listener is None:
            return commands
        while True:
            try:
                client, _address = self._listener.accept()
            except BlockingIOError:
                break
            except OSError:
                break
            if len(self._clients) >= _MAX_CLIENTS:
                client.close()
                continue
            client.setblocking(False)
            self._clients[client] = _ClientState()
            try:
                offset = self.stream_path.stat().st_size
            except OSError:
                offset = 0
            self._queue(client, self._encode({"type": "ready", "protocol": 1, "after_offset": offset}))
            self._flush_client(client)
            commands.extend(self._read_client(client))
        return commands

    def _read_client(self, client: socket.socket) -> list[dict]:
        state = self._clients.get(client)
        if state is None:
            return []
        while True:
            try:
                chunk = client.recv(65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self._disconnect(client)
                return []
            if not chunk:
                self._disconnect(client)
                return []
            state.incoming.extend(chunk)
            if len(state.incoming) > _MAX_FRAME_BYTES:
                self._disconnect(client)
                return []
        commands: list[dict] = []
        while client in self._clients:
            newline = state.incoming.find(b"\n")
            if newline < 0:
                break
            raw = bytes(state.incoming[:newline])
            del state.incoming[: newline + 1]
            if not raw:
                continue
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                commands.append(payload)
        return commands

    def _queue(self, client: socket.socket, frame: bytes) -> None:
        state = self._clients.get(client)
        if state is None:
            return
        if len(state.outgoing) + len(frame) > _MAX_PENDING_BYTES:
            self._disconnect(client)
            return
        state.outgoing.extend(frame)

    def _flush_client(self, client: socket.socket) -> None:
        state = self._clients.get(client)
        if state is None or not state.outgoing:
            return
        try:
            sent = client.send(state.outgoing)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._disconnect(client)
            return
        if sent > 0:
            del state.outgoing[:sent]

    def _disconnect(self, client: socket.socket) -> None:
        self._clients.pop(client, None)
        try:
            client.close()
        except OSError:
            pass

    @staticmethod
    def _encode(payload: dict) -> bytes:
        return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


__all__ = [
    "BridgeTerminalIpc",
    "read_control_records",
    "rotate_control_file",
    "stamp_control_generation",
    "state_has_fresh_heartbeat",
]
