"""UUCP-compatible ownership for physical serial devices.

Linux permits multiple processes to open the same tty.  Multiple readers then
split the RX stream, which looks exactly like random missing terminal output.
Traditional terminal tools coordinate through ``LCK..<tty>`` files; AHA must do
the same so it never silently competes with minicom, picocom, or a flasher.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import signal
import sys
import time


def process_alive(pid: object) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        stat_text = Path(f"/proc/{value}/stat").read_text(encoding="utf-8")
        closing = stat_text.rfind(")")
        if closing >= 0 and stat_text[closing + 2 : closing + 3] == "Z":
            return False
    except OSError:
        pass
    return True


def serial_lock_path(device: str, *, lock_directory: Path | None = None) -> Path | None:
    resolved = Path(str(device or "")).resolve(strict=False)
    if str(resolved).startswith("/dev/pts/") or not resolved.name.startswith("tty"):
        return None
    return (lock_directory or Path("/run/lock")) / f"LCK..{resolved.name}"


def _lock_pid(data: bytes) -> int | None:
    try:
        return int(data.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        if len(data) == 4:
            return int.from_bytes(data, byteorder=sys.byteorder, signed=True)
    return None


def _process_name(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/comm").read_bytes()
    except OSError:
        return ""
    return raw.decode("utf-8", "replace").strip()


def _process_uid(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{pid}").stat().st_uid)
    except OSError:
        return None


def serial_lock_owner(device: str, *, lock_directory: Path | None = None) -> dict | None:
    path = serial_lock_path(device, lock_directory=lock_directory)
    if path is None or not path.exists():
        return None
    try:
        pid = _lock_pid(path.read_bytes())
    except OSError:
        pid = None
    alive = process_alive(pid)
    uid = _process_uid(pid) if pid else None
    effective_uid = os.geteuid()
    return {
        "path": str(path),
        "pid": pid,
        "alive": alive,
        "process": _process_name(pid) if pid else "",
        "uid": uid,
        "can_terminate": bool(alive and uid is not None and (effective_uid == 0 or uid == effective_uid)),
    }


class SerialDeviceBusyError(OSError):
    def __init__(self, device: str, owner: dict) -> None:
        self.device = device
        self.owner = owner
        pid = owner.get("pid") or "unknown"
        process = owner.get("process") or "process"
        super().__init__(errno.EBUSY, f"{device} is locked by {process} (PID {pid})")


@dataclass
class SerialDeviceLock:
    device: str
    path: Path
    pid: int
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            current_pid = _lock_pid(self.path.read_bytes())
        except OSError:
            return
        if current_pid != self.pid:
            return
        try:
            self.path.unlink()
        except OSError:
            pass


def _remove_stale_lock(path: Path, expected_pid: int | None) -> bool:
    try:
        current_pid = _lock_pid(path.read_bytes())
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if current_pid != expected_pid:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def acquire_serial_lock(
    device: str,
    *,
    pid: int | None = None,
    lock_directory: Path | None = None,
) -> SerialDeviceLock | None:
    path = serial_lock_path(device, lock_directory=lock_directory)
    if path is None:
        return None
    owner_pid = int(pid or os.getpid())
    if lock_directory is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(3):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            owner = serial_lock_owner(device, lock_directory=lock_directory) or {
                "path": str(path), "pid": None, "alive": False, "process": "",
                "uid": None, "can_terminate": False,
            }
            if owner.get("alive") or not _remove_stale_lock(path, owner.get("pid")):
                raise SerialDeviceBusyError(device, owner)
            continue
        try:
            os.write(fd, f"{owner_pid}\n".encode("ascii"))
        except Exception:
            os.close(fd)
            _remove_stale_lock(path, owner_pid)
            raise
        os.close(fd)
        return SerialDeviceLock(device=device, path=path, pid=owner_pid)
    owner = serial_lock_owner(device, lock_directory=lock_directory) or {
        "path": str(path), "pid": None, "alive": False, "process": "",
        "uid": None, "can_terminate": False,
    }
    raise SerialDeviceBusyError(device, owner)


def takeover_serial_device(
    device: str,
    *,
    timeout: float = 2.0,
    lock_directory: Path | None = None,
) -> dict:
    owner = serial_lock_owner(device, lock_directory=lock_directory)
    if owner is None:
        return {"released": True, "owner": None}
    pid = owner.get("pid")
    if not owner.get("alive"):
        released = _remove_stale_lock(Path(owner["path"]), pid)
        if not released:
            raise PermissionError(errno.EPERM, f"cannot remove stale serial lock {owner['path']}")
        return {"released": True, "owner": owner}
    if int(pid or 0) == os.getpid():
        raise OSError(errno.EBUSY, "current process owns the serial lock")
    if not owner.get("can_terminate"):
        process = owner.get("process") or "process"
        owner_uid = owner.get("uid")
        owner_identity = f"UID {owner_uid}" if owner_uid is not None else "an unknown UID"
        raise PermissionError(
            errno.EPERM,
            f"AHA runs as UID {os.geteuid()} and cannot terminate {process} "
            f"PID {pid} owned by {owner_identity}; close it manually.",
        )
    try:
        os.kill(int(pid), signal.SIGTERM)
    except PermissionError as exc:
        process = owner.get("process") or "process"
        owner_uid = owner.get("uid")
        owner_identity = f"UID {owner_uid}" if owner_uid is not None else "an unknown UID"
        raise PermissionError(
            errno.EPERM,
            f"AHA runs as UID {os.geteuid()} and cannot terminate {process} "
            f"PID {pid} owned by {owner_identity}; close it manually.",
        ) from exc
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline and process_alive(pid):
        time.sleep(0.02)
    if process_alive(pid):
        raise TimeoutError(f"PID {pid} did not exit after SIGTERM")
    released = _remove_stale_lock(Path(owner["path"]), pid)
    if not released:
        current = serial_lock_owner(device, lock_directory=lock_directory)
        if current is not None:
            raise OSError(errno.EBUSY, "serial lock changed owner during takeover")
    return {"released": True, "owner": owner}


__all__ = [
    "SerialDeviceBusyError",
    "SerialDeviceLock",
    "acquire_serial_lock",
    "process_alive",
    "serial_lock_owner",
    "serial_lock_path",
    "takeover_serial_device",
]
