from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import threading
import uuid

from aha_cli.domain.models import utc_now


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def append_jsonl(path: Path, data: dict) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(data, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o666)
    try:
        with os.fdopen(fd, "ab", closefd=False) as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                payload = line.encode("utf-8")
                written = 0
                while written < len(payload):
                    count = os.write(f.fileno(), payload[written:])
                    if count == 0:
                        raise OSError(f"Unable to append JSONL record to {path}")
                    written += count
                return os.lseek(f.fileno(), 0, os.SEEK_CUR)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        os.close(fd)


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
