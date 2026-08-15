"""Reliable file transfer over an interactive board shell."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import shlex
import time
import uuid
from typing import Callable

from aha_cli.services.hardware_bridge import device_stream_path
from aha_cli.store.io import iter_jsonl_records_from


DEFAULT_CHUNK_SIZE = 256
MAX_CHUNK_SIZE = 512
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_RETRIES = 3
_RECEIVER_PATH = "/tmp/.aha-recv-v1"
_RECEIVER_READY_PATH = "/tmp/.aha-recv-v1.ready"
_RECEIVER_DELIMITER = "__AHA_RECV_V1__"

_RECEIVER_SCRIPT = r'''#!/bin/sh
dest=$1
size=$2
expect=$3
token=$4
tmp="${dest}.aha-part-${token}"
next=0
total=0
saved_stty=$(stty -g 2>/dev/null || :)
restore_tty() {
    [ -n "$saved_stty" ] && stty "$saved_stty" 2>/dev/null || :
}
fail() {
    printf 'AHA-RECV ERR %s %s\n' "$token" "$1"
}
trap 'restore_tty' 0 1 2 15
: > "$tmp" || { fail open; exit 1; }
stty -echo 2>/dev/null || :
printf 'AHA-RECV READY %s\n' "$token"
while IFS= read -r line; do
    case "$line" in
        D*:*)
            rest=${line#D}
            seq=${rest%%:*}
            data=${rest#*:}
            case "$seq" in ''|*[!0-9]*) fail sequence; continue ;; esac
            if [ "$seq" -lt "$next" ]; then
                printf 'AHA-RECV ACK %s %s %s\n' "$token" "$seq" "$total"
                continue
            fi
            if [ "$seq" -ne "$next" ]; then
                printf 'AHA-RECV NAK %s %s\n' "$token" "$next"
                continue
            fi
            chars=${#data}
            if [ $((chars % 5)) -ne 0 ]; then
                fail encoding
                continue
            fi
            printf '%b' "$data" >> "$tmp" || { fail write; exit 1; }
            total=$((total + chars / 5))
            printf 'AHA-RECV ACK %s %s %s\n' "$token" "$seq" "$total"
            next=$((next + 1))
            ;;
        E)
            if [ "$total" -ne "$size" ]; then
                fail size
                continue
            fi
            actual=$(sha256sum "$tmp" 2>/dev/null)
            actual=${actual%% *}
            if [ -z "$actual" ]; then
                actual=$(busybox sha256sum "$tmp" 2>/dev/null)
                actual=${actual%% *}
            fi
            if [ "$actual" != "$expect" ]; then
                fail sha256
                continue
            fi
            mv -f "$tmp" "$dest" || { fail rename; exit 1; }
            restore_tty
            trap - 0 1 2 15
            printf 'AHA-RECV DONE %s %s %s\n' "$token" "$total" "$actual"
            exit 0
            ;;
        Q)
            rm -f "$tmp"
            fail cancelled
            exit 1
            ;;
        *) fail command ;;
    esac
done
fail eof
exit 1
'''


class HardwareFileTransferError(RuntimeError):
    """The board did not complete a hardware file transfer."""


@dataclass(frozen=True)
class HardwareFileTransferResult:
    source: str
    destination: str
    size: int
    sha256: str
    chunks: int
    retries: int
    elapsed_seconds: float


class _ResponseReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = self.path.stat().st_size if self.path.exists() else 0
        self.buffer = ""

    def wait_for(self, success: str, error_prefix: str, *, timeout: float) -> None:
        self.wait_for_any([success], error_prefix, timeout=timeout)

    def wait_for_any(self, successes: list[str], error_prefix: str, *, timeout: float) -> str:
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            records, self.offset = iter_jsonl_records_from(self.path, self.offset, limit=400)
            for record, _line_end in records:
                if str(record.get("direction") or "") != "rx":
                    continue
                self.buffer += str(record.get("data") or "")
            for success in successes:
                if success in self.buffer:
                    self.buffer = self.buffer.split(success, 1)[1]
                    return success
            error_index = self.buffer.find(error_prefix)
            if error_index >= 0:
                detail = self.buffer[error_index + len(error_prefix):].splitlines()[0].strip()
                raise HardwareFileTransferError(f"board receiver failed: {detail or 'unknown error'}")
            if len(self.buffer) > 128 * 1024:
                self.buffer = self.buffer[-64 * 1024:]
            time.sleep(0.02)
        raise HardwareFileTransferError(f"timed out waiting for board response: {' or '.join(successes)}")


def receiver_script_bytes() -> int:
    return len(_RECEIVER_SCRIPT.encode("utf-8"))


def encode_octal_block(data: bytes) -> str:
    return "".join(f"\\0{value:03o}" for value in data)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _bootstrap_command(token: str) -> str:
    return (
        f"\ncat > {_RECEIVER_PATH} <<'{_RECEIVER_DELIMITER}'\n"
        f"{_RECEIVER_SCRIPT}"
        f"{_RECEIVER_DELIMITER}\n"
        f"chmod 700 {_RECEIVER_PATH} && : > {_RECEIVER_READY_PATH} "
        f"&& printf 'AHA-%s %s\\n' BOOT {shlex.quote(token)}\n"
    )


def _cache_probe_command(token: str) -> str:
    return (
        f"if [ -x {_RECEIVER_PATH} ] && [ -f {_RECEIVER_READY_PATH} ]; then "
        f"printf 'AHA-%s %s\\n' CACHE {shlex.quote(token)}; else "
        f"printf 'AHA-%s %s\\n' MISS {shlex.quote(token)}; fi\n"
    )


def send_file_via_shell(
    root: Path,
    device: str,
    source: Path,
    destination: str,
    *,
    send_text: Callable[[str], None],
    stream_path: Path | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    progress: Callable[[int, int], None] | None = None,
) -> HardwareFileTransferResult:
    """Bootstrap ``aha-recv`` and transfer one file through an interactive shell."""

    source = source.expanduser().resolve()
    if not source.is_file():
        raise HardwareFileTransferError(f"source file not found: {source}")
    remote = str(destination or "").strip()
    if not remote or "\x00" in remote or "\r" in remote or "\n" in remote:
        raise HardwareFileTransferError("destination must be a non-empty single-line path")
    safe_chunk_size = int(chunk_size)
    if safe_chunk_size < 1 or safe_chunk_size > MAX_CHUNK_SIZE:
        raise HardwareFileTransferError(f"chunk size must be between 1 and {MAX_CHUNK_SIZE}")
    safe_retries = max(0, int(retries))
    size = source.stat().st_size
    sha256 = _sha256_file(source)
    token = uuid.uuid4().hex[:16]
    reader = _ResponseReader(stream_path or device_stream_path(root, device))
    started = time.monotonic()
    retry_count = 0

    send_text(_cache_probe_command(token))
    cache_marker = reader.wait_for_any(
        [f"AHA-CACHE {token}", f"AHA-MISS {token}"],
        f"AHA-RECV ERR {token} ",
        timeout=max(timeout, 10.0),
    )
    if cache_marker.startswith("AHA-MISS"):
        bootstrap = _bootstrap_command(token)
        send_text(bootstrap)
        reader.wait_for(
            f"AHA-BOOT {token}",
            f"AHA-RECV ERR {token} ",
            timeout=max(timeout, len(bootstrap.encode("utf-8")) / 50.0 + 5.0),
        )
    command = " ".join([
        "sh",
        _RECEIVER_PATH,
        shlex.quote(remote),
        str(size),
        sha256,
        token,
    ])
    send_text(command + "\n")
    reader.wait_for(f"AHA-RECV READY {token}", f"AHA-RECV ERR {token} ", timeout=timeout)

    chunks = 0
    try:
        with source.open("rb") as handle:
            while data := handle.read(safe_chunk_size):
                line = f"D{chunks}:{encode_octal_block(data)}\n"
                for attempt in range(safe_retries + 1):
                    send_text(line)
                    try:
                        reader.wait_for(
                            f"AHA-RECV ACK {token} {chunks} ",
                            f"AHA-RECV ERR {token} ",
                            timeout=timeout,
                        )
                        break
                    except HardwareFileTransferError as exc:
                        if "timed out" not in str(exc) or attempt >= safe_retries:
                            raise
                        retry_count += 1
                chunks += 1
                if progress is not None:
                    progress(min(chunks * safe_chunk_size, size), size)
        send_text("E\n")
        reader.wait_for(
            f"AHA-RECV DONE {token} {size} {sha256}",
            f"AHA-RECV ERR {token} ",
            timeout=max(timeout, 30.0),
        )
    except Exception:
        try:
            send_text("Q\n")
        except Exception:
            pass
        raise

    return HardwareFileTransferResult(
        source=str(source),
        destination=remote,
        size=size,
        sha256=sha256,
        chunks=chunks,
        retries=retry_count,
        elapsed_seconds=time.monotonic() - started,
    )


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_RETRIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "HardwareFileTransferError",
    "HardwareFileTransferResult",
    "MAX_CHUNK_SIZE",
    "encode_octal_block",
    "receiver_script_bytes",
    "send_file_via_shell",
]
