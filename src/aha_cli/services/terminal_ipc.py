"""Local realtime IPC shared by machine-level hardware terminal bridges."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import socket
import stat
from typing import Iterable


_MAX_CLIENTS = 8
_MAX_FRAME_BYTES = 256 * 1024
_MAX_PENDING_BYTES = 1024 * 1024


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

    def close(self) -> None:
        for client in list(self._clients):
            self._disconnect(client)
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        try:
            if self.socket_path.is_socket():
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


__all__ = ["BridgeTerminalIpc"]
