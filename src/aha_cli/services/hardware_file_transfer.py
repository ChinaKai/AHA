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
DEFAULT_RAW_SERIAL_CHUNK_SIZE = 16 * 1024
DEFAULT_RAW_NETWORK_CHUNK_SIZE = 64 * 1024
MAX_RAW_CHUNK_SIZE = 128 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_RETRIES = 3
_RECEIVER_PATH = "/tmp/.aha-recv-v2"
_RECEIVER_READY_PATH = "/tmp/.aha-recv-v2.ready"
_RECEIVER_DELIMITER = "__AHA_RECV_V2__"
_RAW_RECEIVER_PATH = "/tmp/.aha-recv-v3"
_RAW_RECEIVER_READY_PATH = "/tmp/.aha-recv-v3.ready"
_RAW_RECEIVER_DELIMITER = "__AHA_RECV_V3__"
_RAW_TRAILER_PAD = "A" * 128

_RECEIVER_SCRIPT = r'''#!/bin/sh
dest=$1
size=$2
expect=$3
token=$4
tmp="${dest}.aha-part-${token}"
chunk="${tmp}.chunk"
next=0
total=0
saved_stty=$(stty -g 2>/dev/null || :)
restore_tty() {
    [ -n "$saved_stty" ] && stty "$saved_stty" 2>/dev/null || :
}
fail() {
    printf 'AHA-RECV ERR %s %s\n' "$token" "$1"
}
reject() {
    printf 'AHA-RECV NAK %s %s %s\n' "$token" "$next" "$1"
}
hash_file() {
    result=$(sha256sum "$1" 2>/dev/null)
    result=${result%% *}
    if [ -z "$result" ]; then
        result=$(busybox sha256sum "$1" 2>/dev/null)
        result=${result%% *}
    fi
    [ -n "$result" ] || return 1
    printf '%s\n' "$result"
}
cleanup() {
    rm -f "$chunk"
    restore_tty
}
trap 'cleanup' 0 1 2 15
: > "$tmp" || { fail open; exit 1; }
hash_file "$tmp" >/dev/null || { fail sha256-tool; exit 1; }
stty -echo 2>/dev/null || :
printf 'AHA-RECV READY %s\n' "$token"
while IFS= read -r line; do
    case "$line" in
        D*)
            rest=${line#D}
            case "$rest" in *:*:*) ;; *) reject format; continue ;; esac
            seq=${rest%%:*}
            rest=${rest#*:}
            block_expect=${rest%%:*}
            data=${rest#*:}
            case "$seq" in ''|*[!0-9]*) reject sequence; continue ;; esac
            if [ "$seq" -lt "$next" ]; then
                printf 'AHA-RECV ACK %s %s %s\n' "$token" "$seq" "$total"
                continue
            fi
            if [ "$seq" -ne "$next" ]; then
                reject sequence
                continue
            fi
            case "$block_expect" in *[!0-9a-fA-F]*) reject hash; continue ;; esac
            if [ "${#block_expect}" -ne 64 ]; then
                reject hash
                continue
            fi
            chars=${#data}
            if [ $((chars % 5)) -ne 0 ]; then
                reject encoding
                continue
            fi
            printf '%b' "$data" > "$chunk" || { fail write; exit 1; }
            block_actual=$(hash_file "$chunk") || { fail sha256-tool; exit 1; }
            if [ "$block_actual" != "$block_expect" ]; then
                reject sha256
                continue
            fi
            cat "$chunk" >> "$tmp" || { fail write; exit 1; }
            rm -f "$chunk"
            total=$((total + chars / 5))
            printf 'AHA-RECV ACK %s %s %s\n' "$token" "$seq" "$total"
            next=$((next + 1))
            ;;
        E)
            if [ "$total" -ne "$size" ]; then
                fail size
                continue
            fi
            actual=$(hash_file "$tmp") || { fail sha256-tool; exit 1; }
            if [ "$actual" != "$expect" ]; then
                fail sha256
                continue
            fi
            rm -f "$chunk"
            mv -f "$tmp" "$dest" || { fail rename; exit 1; }
            restore_tty
            trap - 0 1 2 15
            printf 'AHA-RECV DONE %s %s %s\n' "$token" "$total" "$actual"
            exit 0
            ;;
        Q)
            rm -f "$tmp" "$chunk"
            fail cancelled
            exit 1
            ;;
        *) reject command ;;
    esac
done
fail eof
exit 1
'''

_RAW_RECEIVER_SCRIPT = rf'''#!/bin/sh
dest=$1
size=$2
expect=$3
token=$4
tmp="${{dest}}.aha-part-${{token}}"
chunk="${{tmp}}.chunk"
next=0
total=0
complete=0
fullblock=0
saved_stty=$(stty -g 2>/dev/null || :)
restore_tty() {{
    [ -n "$saved_stty" ] && stty "$saved_stty" 2>/dev/null || :
}}
fail() {{
    printf 'AHA-RAW ERR %s %s\n' "$token" "$1"
}}
reject() {{
    printf 'AHA-RAW NAK %s %s %s\n' "$token" "$1" "$2"
}}
hash_file() {{
    result=$(sha256sum "$1" 2>/dev/null)
    result=${{result%% *}}
    if [ -z "$result" ]; then
        result=$(busybox sha256sum "$1" 2>/dev/null)
        result=${{result%% *}}
    fi
    [ -n "$result" ] || return 1
    printf '%s\n' "$result"
}}
cleanup() {{
    rm -f "$chunk"
    [ "$complete" -eq 1 ] || rm -f "$tmp"
    restore_tty
}}
read_payload() {{
    rm -f "$chunk"
    if [ "$fullblock" -eq 1 ]; then
        dd iflag=fullblock bs="$frame_size" count=1 of="$chunk" 2>/dev/null
    else
        dd bs=1 count="$frame_size" of="$chunk" 2>/dev/null
    fi
}}
trap 'cleanup' 0 1 2 15
: > "$tmp" || {{ fail open; exit 1; }}
hash_file "$tmp" >/dev/null || {{ fail sha256-tool; exit 1; }}
if dd if=/dev/null of=/dev/null bs=1 count=0 iflag=fullblock 2>/dev/null; then
    fullblock=1
fi
if [ -t 0 ]; then
    stty raw -echo 2>/dev/null || {{ fail stty; exit 1; }}
fi
printf 'AHA-RAW READY %s\n' "$token"
while IFS=' ' read -r kind seq frame_size frame_expect extra; do
    case "$kind" in
        F)
            if [ -n "$extra" ]; then
                reject "${{seq:-0}}" format
                continue
            fi
            case "$seq" in ''|*[!0-9]*) reject 0 sequence; continue ;; esac
            case "$frame_size" in ''|*[!0-9]*) reject "$seq" size; continue ;; esac
            if [ "$frame_size" -lt 1 ] || [ "$frame_size" -gt {MAX_RAW_CHUNK_SIZE} ]; then
                reject "$seq" size
                continue
            fi
            case "$frame_expect" in *[!0-9a-fA-F]*) reject "$seq" hash; continue ;; esac
            if [ "${{#frame_expect}}" -ne 64 ]; then
                reject "$seq" hash
                continue
            fi
            if [ "$seq" -lt "$next" ]; then
                printf 'AHA-RAW ACK %s %s %s\n' "$token" "$seq" "$total"
                continue
            fi
            if [ "$seq" -ne "$next" ]; then
                reject "$seq" sequence
                continue
            fi
            if [ $((total + frame_size)) -gt "$size" ]; then
                reject "$seq" size
                continue
            fi
            printf 'AHA-RAW DATA %s %s\n' "$token" "$seq"
            read_payload || {{ fail read; exit 1; }}
            actual_size=$(wc -c < "$chunk" 2>/dev/null)
            if [ "$actual_size" -ne "$frame_size" ]; then
                reject "$seq" short
                continue
            fi
            IFS= read -r trailer || {{ fail eof; exit 1; }}
            if [ "$trailer" != "T:${{token}}:${{seq}}:{_RAW_TRAILER_PAD}" ]; then
                reject "$seq" framing
                continue
            fi
            block_actual=$(hash_file "$chunk") || {{ fail sha256-tool; exit 1; }}
            if [ "$block_actual" != "$frame_expect" ]; then
                reject "$seq" sha256
                continue
            fi
            cat "$chunk" >> "$tmp" || {{ fail write; exit 1; }}
            rm -f "$chunk"
            total=$((total + frame_size))
            printf 'AHA-RAW ACK %s %s %s\n' "$token" "$seq" "$total"
            next=$((next + 1))
            ;;
        E)
            if [ "$total" -ne "$size" ]; then
                fail size
                continue
            fi
            actual=$(hash_file "$tmp") || {{ fail sha256-tool; exit 1; }}
            if [ "$actual" != "$expect" ]; then
                fail sha256
                continue
            fi
            mv -f "$tmp" "$dest" || {{ fail rename; exit 1; }}
            complete=1
            restore_tty
            trap - 0 1 2 15
            printf 'AHA-RAW DONE %s %s %s\n' "$token" "$total" "$actual"
            exit 0
            ;;
        Q)
            fail cancelled
            exit 1
            ;;
        *) reject "${{seq:-0}}" command ;;
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
            matches = [(self.buffer.find(success), success) for success in successes]
            matches = [(index, success) for index, success in matches if index >= 0]
            error_index = self.buffer.find(error_prefix)
            if error_index >= 0 and (not matches or error_index < min(index for index, _success in matches)):
                detail = self.buffer[error_index + len(error_prefix):].splitlines()[0].strip()
                raise HardwareFileTransferError(f"board receiver failed: {detail or 'unknown error'}")
            if matches:
                index, success = min(matches, key=lambda item: item[0])
                self.buffer = self.buffer[index + len(success):]
                return success
            if len(self.buffer) > 128 * 1024:
                self.buffer = self.buffer[-64 * 1024:]
            time.sleep(0.02)
        raise HardwareFileTransferError(f"timed out waiting for board response: {' or '.join(successes)}")


def receiver_script_bytes() -> int:
    return len(_RECEIVER_SCRIPT.encode("utf-8"))


def raw_receiver_script_bytes() -> int:
    return len(_RAW_RECEIVER_SCRIPT.encode("utf-8"))


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


def _raw_bootstrap_command(token: str) -> str:
    return (
        f"\ncat > {_RAW_RECEIVER_PATH} <<'{_RAW_RECEIVER_DELIMITER}'\n"
        f"{_RAW_RECEIVER_SCRIPT}"
        f"{_RAW_RECEIVER_DELIMITER}\n"
        f"chmod 700 {_RAW_RECEIVER_PATH} && : > {_RAW_RECEIVER_READY_PATH} "
        f"&& printf 'AHA-%s %s\\n' RAWBOOT {shlex.quote(token)}\n"
    )


def _raw_cache_probe_command(token: str) -> str:
    return (
        f"if [ -x {_RAW_RECEIVER_PATH} ] && [ -f {_RAW_RECEIVER_READY_PATH} ]; then "
        f"printf 'AHA-%s %s\\n' RAWCACHE {shlex.quote(token)}; else "
        f"printf 'AHA-%s %s\\n' RAWMISS {shlex.quote(token)}; fi\n"
    )


def _raw_frame_trailer(token: str, sequence: int) -> bytes:
    return f"T:{token}:{sequence}:{_RAW_TRAILER_PAD}\n".encode("ascii")


def send_file_via_raw_shell(
    root: Path,
    device: str,
    source: Path,
    destination: str,
    *,
    send_text: Callable[[str], None],
    send_bytes: Callable[[bytes], None],
    stream_path: Path | None = None,
    chunk_size: int = DEFAULT_RAW_SERIAL_CHUNK_SIZE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    progress: Callable[[int, int], None] | None = None,
) -> HardwareFileTransferResult:
    """Bootstrap a raw Shell receiver and transfer binary frames with SHA-256."""

    source = source.expanduser().resolve()
    if not source.is_file():
        raise HardwareFileTransferError(f"source file not found: {source}")
    remote = str(destination or "").strip()
    if not remote or "\x00" in remote or "\r" in remote or "\n" in remote:
        raise HardwareFileTransferError("destination must be a non-empty single-line path")
    safe_chunk_size = int(chunk_size)
    if safe_chunk_size < 1 or safe_chunk_size > MAX_RAW_CHUNK_SIZE:
        raise HardwareFileTransferError(f"raw chunk size must be between 1 and {MAX_RAW_CHUNK_SIZE}")
    safe_retries = max(0, int(retries))
    size = source.stat().st_size
    sha256 = _sha256_file(source)
    token = uuid.uuid4().hex[:16]
    reader = _ResponseReader(stream_path or device_stream_path(root, device))
    started = time.monotonic()
    retry_count = 0

    send_text(_raw_cache_probe_command(token))
    cache_marker = reader.wait_for_any(
        [f"AHA-RAWCACHE {token}", f"AHA-RAWMISS {token}"],
        f"AHA-RAW ERR {token} ",
        timeout=max(timeout, 10.0),
    )
    if cache_marker.startswith("AHA-RAWMISS"):
        bootstrap = _raw_bootstrap_command(token)
        send_text(bootstrap)
        reader.wait_for(
            f"AHA-RAWBOOT {token}",
            f"AHA-RAW ERR {token} ",
            timeout=max(timeout, len(bootstrap.encode("utf-8")) / 50.0 + 5.0),
        )
    command = " ".join([
        "sh",
        _RAW_RECEIVER_PATH,
        shlex.quote(remote),
        str(size),
        sha256,
        token,
    ])
    send_text(command + "\n")
    reader.wait_for(f"AHA-RAW READY {token}", f"AHA-RAW ERR {token} ", timeout=timeout)

    chunks = 0
    transferred = 0
    try:
        with source.open("rb") as handle:
            while data := handle.read(safe_chunk_size):
                block_sha256 = hashlib.sha256(data).hexdigest()
                header = f"F {chunks} {len(data)} {block_sha256}\n"
                for attempt in range(safe_retries + 1):
                    send_text(header)
                    try:
                        response = reader.wait_for_any(
                            [
                                f"AHA-RAW DATA {token} {chunks}",
                                f"AHA-RAW ACK {token} {chunks} ",
                                f"AHA-RAW NAK {token} {chunks} ",
                            ],
                            f"AHA-RAW ERR {token} ",
                            timeout=timeout,
                        )
                        if response.startswith("AHA-RAW ACK"):
                            break
                        if response.startswith("AHA-RAW NAK"):
                            retry_count += 1
                            if attempt >= safe_retries:
                                raise HardwareFileTransferError(
                                    f"board receiver rejected raw block {chunks} after {safe_retries + 1} attempts"
                                )
                            continue
                        send_bytes(data + _raw_frame_trailer(token, chunks))
                        response = reader.wait_for_any(
                            [
                                f"AHA-RAW ACK {token} {chunks} ",
                                f"AHA-RAW NAK {token} {chunks} ",
                            ],
                            f"AHA-RAW ERR {token} ",
                            timeout=timeout,
                        )
                        if response.startswith("AHA-RAW ACK"):
                            break
                        retry_count += 1
                        if attempt >= safe_retries:
                            raise HardwareFileTransferError(
                                f"board receiver rejected raw block {chunks} after {safe_retries + 1} attempts"
                            )
                    except HardwareFileTransferError as exc:
                        if "timed out" not in str(exc) or attempt >= safe_retries:
                            raise
                        retry_count += 1
                chunks += 1
                transferred += len(data)
                if progress is not None:
                    progress(transferred, size)
        send_text("E\n")
        reader.wait_for(
            f"AHA-RAW DONE {token} {size} {sha256}",
            f"AHA-RAW ERR {token} ",
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
                block_sha256 = hashlib.sha256(data).hexdigest()
                line = f"D{chunks}:{block_sha256}:{encode_octal_block(data)}\n"
                for attempt in range(safe_retries + 1):
                    send_text(line)
                    try:
                        response = reader.wait_for_any(
                            [
                                f"AHA-RECV ACK {token} {chunks} ",
                                f"AHA-RECV NAK {token} {chunks} ",
                            ],
                            f"AHA-RECV ERR {token} ",
                            timeout=timeout,
                        )
                        if response.startswith("AHA-RECV ACK"):
                            break
                        retry_count += 1
                        if attempt >= safe_retries:
                            raise HardwareFileTransferError(
                                f"board receiver rejected block {chunks} after {safe_retries + 1} attempts"
                            )
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
    "DEFAULT_RAW_NETWORK_CHUNK_SIZE",
    "DEFAULT_RAW_SERIAL_CHUNK_SIZE",
    "DEFAULT_RETRIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "HardwareFileTransferError",
    "HardwareFileTransferResult",
    "MAX_CHUNK_SIZE",
    "MAX_RAW_CHUNK_SIZE",
    "encode_octal_block",
    "raw_receiver_script_bytes",
    "receiver_script_bytes",
    "send_file_via_raw_shell",
    "send_file_via_shell",
]
