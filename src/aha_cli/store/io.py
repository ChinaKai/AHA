from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
import uuid

from aha_cli.domain.models import utc_now


_WINDOWS = os.name == "nt"

# Serializes JSONL appends within one process. Cross-process serialization is
# handled by the sidecar lock in _acquire_append_lock; this guards the file-fd
# lifecycle so two threads in the same process cannot interleave.
_jsonl_write_lock = threading.Lock()

_APPEND_LOCK_ACQUIRE_RETRIES = 2000
_APPEND_LOCK_RETRY_DELAY = 0.005
_APPEND_LOCK_STALE_SECONDS = 30.0


def _acquire_append_lock(lock_path: Path) -> None:
    """Acquire an exclusive sidecar lock, blocking with retry.

    Uses ``os.open(..., O_CREAT | O_EXCL)`` which is atomic on both POSIX and
    Windows and resolves to the same directory entry across filesystem views
    (e.g. ``/mnt/c/...`` vs ``C:\\...`` for the same shared file). A stale lock
    (crashed writer) is reclaimed after ``_APPEND_LOCK_STALE_SECONDS``.
    """
    deadline = time.monotonic() + _APPEND_LOCK_STALE_SECONDS
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
            os.close(fd)
            return
        except FileExistsError:
            if time.monotonic() >= deadline:
                # Stale lock: the previous writer crashed without releasing.
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            time.sleep(_APPEND_LOCK_RETRY_DELAY)
        except OSError:
            # e.g. transient 9p failure; retry without spinning too hot.
            time.sleep(_APPEND_LOCK_RETRY_DELAY)


def _release_append_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
_WRITE_LOCKS_GUARD = threading.Lock()
_WRITE_LOCKS: dict[str, threading.RLock] = {}
_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)
_SIDECAR_LOCK_TIMEOUT_SECONDS = 65.0
# A live holder refreshes the lock mtime on a heartbeat, so the stale threshold
# only needs to outlive a crashed holder's last heartbeat. Keep it well above
# the acquire timeout so a waiter times out rather than stealing a live-but-slow
# holder's lock (e.g. a long fsync over a WSL /mnt mount).
_SIDECAR_LOCK_STALE_SECONDS = 120.0
_SIDECAR_LOCK_HEARTBEAT_SECONDS = 30.0
_SIDECAR_LOCK_RETRY_DELAY = 0.01


def _write_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _WRITE_LOCKS_GUARD:
        return _WRITE_LOCKS.setdefault(key, threading.RLock())


def _replace_with_retry(tmp: Path, path: Path) -> None:
    for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if not _WINDOWS or attempt >= len(_REPLACE_RETRY_DELAYS):
                raise
            time.sleep(_REPLACE_RETRY_DELAYS[attempt])


def _sidecar_lock_snapshot(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


def _read_sidecar_lock_owner(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii").splitlines()[0].strip()
    except (OSError, IndexError, UnicodeError):
        return ""


def _refresh_sidecar_lock(path: Path) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


def _remove_stale_sidecar_lock(path: Path, snapshot: tuple[int, int, int], owner: str) -> bool:
    # Re-verify both the stat snapshot and the owner token right before unlinking.
    # The token check matters on Windows where st_ino can be unreliable and
    # mtime/size can collide across a same-second replacement.
    if _sidecar_lock_snapshot(path) != snapshot:
        return False
    if owner and _read_sidecar_lock_owner(path) != owner:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


@contextmanager
def exclusive_sidecar_lock(
    lock_path: Path,
    *,
    timeout: float = _SIDECAR_LOCK_TIMEOUT_SECONDS,
    stale_seconds: float = _SIDECAR_LOCK_STALE_SECONDS,
    retry_delay: float = _SIDECAR_LOCK_RETRY_DELAY,
):
    """Serialize writers through a lock file shared by Windows and WSL views."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    payload = f"{token}\n{os.getpid()}\n{time.time()}\n".encode("ascii")
    deadline = time.monotonic() + max(0.0, timeout)
    acquired = False
    while not acquired:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            snapshot = _sidecar_lock_snapshot(lock_path)
            if snapshot is not None:
                try:
                    age = max(0.0, time.time() - (snapshot[1] / 1_000_000_000))
                except (OverflowError, ValueError):
                    age = 0.0
                if age >= max(0.0, stale_seconds) and _remove_stale_sidecar_lock(
                    lock_path, snapshot, _read_sidecar_lock_owner(lock_path)
                ):
                    continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for lock: {lock_path}")
            time.sleep(max(0.001, retry_delay))
            continue
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(max(0.001, retry_delay))
            continue
        try:
            written = 0
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count == 0:
                    raise OSError(f"Unable to write lock owner to {lock_path}")
                written += count
            os.fsync(fd)
        except Exception:
            # We created the lock file but failed to record our ownership; drop it
            # so we do not leak a lock whose owner token is never released.
            try:
                lock_path.unlink()
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
        acquired = True
    stop = threading.Event()
    heartbeat = None

    def _heartbeat() -> None:
        while not stop.wait(_SIDECAR_LOCK_HEARTBEAT_SECONDS):
            _refresh_sidecar_lock(lock_path)

    try:
        heartbeat = threading.Thread(target=_heartbeat, daemon=True, name="aha-sidecar-lock-heartbeat")
        heartbeat.start()
        yield
    finally:
        stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=1.0)
        if _read_sidecar_lock_owner(lock_path) == token:
            try:
                lock_path.unlink()
            except OSError:
                pass


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def json_backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak")


def _json_payload(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write_bytes_atomically(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _valid_json_bytes(path: Path) -> bytes | None:
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return payload if isinstance(value, dict) else None


def _read_json_after_replace(path: Path) -> dict:
    for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
        try:
            return read_json(path)
        except (OSError, ValueError):
            if attempt >= len(_REPLACE_RETRY_DELAYS):
                raise
            time.sleep(_REPLACE_RETRY_DELAYS[attempt])
    raise AssertionError("unreachable")


def write_json(
    path: Path,
    data: dict,
    *,
    backup: bool = False,
    verify: bool = False,
) -> None:
    with _write_lock(path):
        previous = _valid_json_bytes(path) if backup and path.exists() else None
        if previous is not None:
            _write_bytes_atomically(json_backup_path(path), previous)
        try:
            _write_bytes_atomically(path, _json_payload(data))
            if verify and _read_json_after_replace(path) != data:
                raise OSError(f"JSON verification failed after writing {path}")
        except Exception:
            if previous is not None:
                try:
                    _write_bytes_atomically(path, previous)
                except OSError:
                    pass
            raise


def append_jsonl(path: Path, data: dict) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(data, ensure_ascii=False) + "\n"
    payload = line.encode("utf-8")
    # Serialize appends across processes that may see the same file through
    # different views (e.g. a WSL backend appending over /mnt/c while the
    # Windows Web service appends over C:\ for the same single-copy run). On 9p,
    # concurrent os.write(..., O_APPEND) from two processes drops whole lines.
    # A sidecar lock file with atomic O_EXCL creation serializes writers; it
    # works across the two filesystem views because both resolve to the same
    # directory entry in the shared mount.
    lock_path = path.with_name(path.name + ".lock")
    with _jsonl_write_lock:
        _acquire_append_lock(lock_path)
        try:
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o666)
            try:
                written = 0
                while written < len(payload):
                    count = os.write(fd, payload[written:])
                    if count == 0:
                        raise OSError(f"Unable to append JSONL record to {path}")
                    written += count
                return os.lseek(fd, 0, os.SEEK_CUR)
            finally:
                os.close(fd)
        finally:
            _release_append_lock(lock_path)


def iter_jsonl_records_from(
    path: Path,
    start: int = 0,
    before: int | None = None,
    limit: int | None = None,
) -> tuple[list[tuple[dict, int]], int]:
    if not path.exists():
        return [], start
    file_size = path.stat().st_size
    end = file_size if before is None else max(0, min(before, file_size))
    records: list[tuple[dict, int]] = []
    offset = max(0, min(start, end))
    with path.open("rb") as f:
        f.seek(offset)
        while f.tell() < end and (limit is None or len(records) < limit):
            line_start = f.tell()
            line = f.readline(end - line_start if before is not None else -1)
            if not line:
                break
            line_end = f.tell()
            if before is not None and line_end >= end and not line.endswith(b"\n"):
                return records, line_start
            line = line.strip()
            if not line:
                offset = line_end
                continue
            try:
                records.append((json.loads(line.decode("utf-8")), line_end))
            except (UnicodeDecodeError, json.JSONDecodeError):
                records.append(({"ts": utc_now(), "type": "malformed_event", "data": {"line": line.decode("utf-8", errors="replace")}}, line_end))
            offset = line_end
        return records, offset


def iter_jsonl_from(path: Path, start: int = 0, before: int | None = None, limit: int | None = None) -> tuple[list[dict], int]:
    records, offset = iter_jsonl_records_from(path, start, before=before, limit=limit)
    return [item for item, _line_end in records], offset


def iter_jsonl_reverse(path: Path, before: int | None = None, chunk_size: int = 65536):
    if not path.exists():
        return
    file_size = path.stat().st_size
    end = file_size if before is None else max(0, min(before, file_size))
    if end <= 0:
        return
    with path.open("rb") as f:
        carry = b""
        position = end
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            f.seek(position)
            data = f.read(read_size) + carry
            parts = data.split(b"\n")
            if position > 0:
                carry = parts[0]
                line_parts = parts[1:]
                line_start = position + len(parts[0]) + 1
            else:
                carry = b""
                line_parts = parts
                line_start = 0

            records: list[tuple[int, bytes]] = []
            cursor = line_start
            for part in line_parts:
                start = cursor
                cursor += len(part) + 1
                if part.strip():
                    records.append((start, part))

            for start, line in reversed(records):
                try:
                    yield start, json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    yield start, {"ts": utc_now(), "type": "malformed_event", "data": {"line": line.decode("utf-8", errors="replace")}}


def iter_text_lines_reverse(path: Path, before: int | None = None, chunk_size: int = 65536):
    if not path.exists():
        return
    file_size = path.stat().st_size
    end = file_size if before is None else max(0, min(before, file_size))
    if end <= 0:
        return
    with path.open("rb") as f:
        carry = b""
        position = end
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            f.seek(position)
            data = f.read(read_size) + carry
            parts = data.split(b"\n")
            if position > 0:
                carry = parts[0]
                line_parts = parts[1:]
                line_start = position + len(parts[0]) + 1
            else:
                carry = b""
                line_parts = parts
                line_start = 0

            records: list[tuple[int, bytes]] = []
            cursor = line_start
            for part in line_parts:
                start = cursor
                cursor += len(part) + 1
                if part:
                    records.append((start, part))

            for start, line in reversed(records):
                yield start, line.decode("utf-8", errors="replace")


def text_tail_page(path: Path, limit: int = 200, before: int | None = None) -> dict:
    file_size = path.stat().st_size if path.exists() else 0
    end_offset = file_size if before is None else max(0, min(before, file_size))
    safe_limit = max(1, min(limit, 1000))
    matches: list[dict] = []
    for offset, line in iter_text_lines_reverse(path, before=end_offset) or ():
        matches.append({"_cursor": offset, "text": line})
        if len(matches) > safe_limit:
            break

    has_more = len(matches) > safe_limit
    page = list(reversed(matches[:safe_limit]))
    next_before_offset = page[0].get("_cursor") if has_more and page else None
    return {
        "text": "\n".join(item["text"] for item in page),
        "lines": page,
        "before_offset": end_offset,
        "after_offset": file_size,
        "next_before_offset": next_before_offset,
        "has_more": has_more,
        "count": len(page),
    }
