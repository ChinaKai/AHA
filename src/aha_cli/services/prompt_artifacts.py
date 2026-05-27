from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import threading
import uuid

from aha_cli.domain.models import utc_now
from aha_cli.store.filesystem import run_dir


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return safe or "prompt"


def _text_metrics(text: str) -> dict:
    return {
        "chars": len(text),
        "bytes": len(text.encode("utf-8")),
        "lines": text.count("\n") + 1 if text else 0,
    }


def save_prompt_artifact(root: Path, run_id: str, task_id: str | None, target: str, prompt: str) -> dict:
    created_at = utc_now()
    timestamp = _safe_name(created_at.replace(":", "").replace("+", "Z"))
    filename = f"{_safe_name(target)}-{timestamp}-{uuid.uuid4().hex[:8]}.md"
    if task_id:
        relative = PurePosixPath("tasks") / _safe_name(task_id) / "prompts" / filename
    else:
        relative = PurePosixPath("prompts") / filename
    path = run_dir(root, run_id) / Path(*relative.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(prompt, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {
        "path": relative.as_posix(),
        "created_at": created_at,
        **_text_metrics(prompt),
    }


def _safe_prompt_artifact_path(root: Path, run_id: str, ref: str) -> Path:
    rel = PurePosixPath(str(ref or "").strip())
    if not rel.parts or rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError("invalid prompt ref")
    if rel.parts[0] == "tasks":
        if len(rel.parts) < 4 or rel.parts[2] != "prompts":
            raise ValueError("invalid task prompt ref")
    elif rel.parts[0] == "prompts":
        if len(rel.parts) < 2:
            raise ValueError("invalid prompt ref")
    else:
        raise ValueError("invalid prompt ref")
    if Path(rel.name).suffix != ".md":
        raise ValueError("invalid prompt ref")
    base = run_dir(root, run_id).resolve()
    path = (base / Path(*rel.parts)).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError("invalid prompt ref") from exc
    return path


def read_prompt_artifact(root: Path, run_id: str, ref: str) -> dict:
    path = _safe_prompt_artifact_path(root, run_id, ref)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(ref)
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "prompt_ref": {"path": str(ref), **_text_metrics(text)},
        "prompt": text,
    }


__all__ = ["read_prompt_artifact", "save_prompt_artifact"]
