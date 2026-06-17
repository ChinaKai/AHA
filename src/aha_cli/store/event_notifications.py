from __future__ import annotations

import asyncio
import errno
import hashlib
import os
from pathlib import Path
import socket
import tempfile
import threading
import uuid

from aha_cli.store.paths import event_path

EVENT_NOTIFY_SOCKET_SUFFIX = ".sock"
EVENT_NOTIFY_PAYLOAD = b"event"
EVENT_NOTIFY_MAX_BYTES = 128
STALE_NOTIFY_ERRNOS = {errno.ENOENT, errno.ECONNREFUSED, errno.ENOTSOCK}


def event_notification_dir(events_file: Path) -> Path:
    # Keep socket paths short; Unix domain socket paths are commonly capped at 108 bytes.
    normalized = events_file.expanduser().resolve(strict=False)
    digest = hashlib.sha1(str(normalized).encode("utf-8")).hexdigest()[:16]
    uid = getattr(os, "getuid", lambda: os.getpid())()
    return Path(tempfile.gettempdir()) / f"aha-ws-{uid}" / digest


def _unix_datagram_supported() -> bool:
    return hasattr(socket, "AF_UNIX")


def notify_event_appended(events_file: Path | None) -> None:
    if events_file is None or not _unix_datagram_supported():
        return
    directory = event_notification_dir(events_file)
    if not directory.is_dir():
        return
    try:
        listeners = list(directory.glob(f"*{EVENT_NOTIFY_SOCKET_SUFFIX}"))
    except OSError:
        return
    if not listeners:
        return
    try:
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    except OSError:
        return
    try:
        sender.setblocking(False)
        for listener in listeners:
            try:
                sender.sendto(EVENT_NOTIFY_PAYLOAD, str(listener))
            except OSError as exc:
                if exc.errno in STALE_NOTIFY_ERRNOS:
                    _unlink_stale_listener(listener)
    finally:
        sender.close()


def _unlink_stale_listener(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


class EventNotificationListener:
    def __init__(self, events_file: Path):
        if not _unix_datagram_supported():
            raise OSError("Unix datagram sockets are not supported")
        self.events_file = events_file
        self.directory = event_notification_dir(events_file)
        self.path = self.directory / f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}{EVENT_NOTIFY_SOCKET_SUFFIX}"
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                self.directory.parent.chmod(0o700)
                self.directory.chmod(0o700)
            except OSError:
                pass
            self._socket.bind(str(self.path))
            self._socket.setblocking(False)
        except OSError:
            self.close()
            raise

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.sock_recv(self._socket, EVENT_NOTIFY_MAX_BYTES)
        self.drain()

    def drain(self) -> None:
        while True:
            try:
                self._socket.recv(EVENT_NOTIFY_MAX_BYTES)
            except BlockingIOError:
                return
            except OSError:
                return

    def close(self) -> None:
        try:
            self._socket.close()
        finally:
            _unlink_stale_listener(self.path)
            try:
                self.directory.rmdir()
            except OSError:
                pass
            try:
                self.directory.parent.rmdir()
            except OSError:
                pass

    def __enter__(self) -> EventNotificationListener:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


def open_event_notification_listener(root: Path, run_id: str) -> EventNotificationListener | None:
    try:
        return EventNotificationListener(event_path(root, run_id))
    except OSError:
        return None
