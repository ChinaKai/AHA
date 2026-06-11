from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import threading
import uuid

from aha_cli.domain.models import utc_now


def _safe_name(value: object, fallback: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return safe or fallback


def _text_metrics(text: str) -> dict:
    return {
        "chars": len(text),
        "bytes": len(text.encode("utf-8")),
        "lines": text.count("\n") + 1 if text else 0,
    }


def save_command_output_artifact(
    events_file: Path | None,
    *,
    task_id: str | None,
    target: str | None,
    output: str,
) -> dict | None:
    if not events_file or not output:
        return None
    created_at = utc_now()
    timestamp = _safe_name(created_at.replace(":", "").replace("+", "Z"), "output")
    filename = f"{_safe_name(target, 'agent')}-{timestamp}-{uuid.uuid4().hex[:8]}.txt"
    if task_id:
        relative = PurePosixPath("tasks") / _safe_name(task_id, "task") / "artifacts" / "command-output" / filename
    else:
        relative = PurePosixPath("artifacts") / "command-output" / filename
    run_path = events_file.parent
    path = run_path / Path(*relative.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(output, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {
        "kind": "command_output",
        "path": relative.as_posix(),
        "created_at": created_at,
        **_text_metrics(output),
    }


__all__ = ["save_command_output_artifact"]
