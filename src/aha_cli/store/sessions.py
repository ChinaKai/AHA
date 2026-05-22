from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from aha_cli.domain.models import make_session, utc_now
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import run_dir, session_path


def ensure_session(
    root: Path,
    run_id: str,
    task_id: str | None,
    agent_id: str,
    backend: str,
    model: str | None = None,
    workspace_path: str | None = None,
    now_func: Callable[[], str] = utc_now,
) -> dict:
    path = session_path(root, run_id, task_id, agent_id)
    if path.exists():
        session = read_json(path)
        changed = False
        for key, value in {"model": model, "workspace_path": workspace_path}.items():
            if value is not None and session.get(key) != value:
                session[key] = value
                changed = True
        for key, value in {"history_backend_sessions": [], "compact_summary": None}.items():
            if key not in session:
                session[key] = value
                changed = True
        if changed:
            session["updated_at"] = now_func()
            write_json(path, session)
        return session
    session = make_session(run_id, task_id, agent_id, backend, model=model, workspace_path=workspace_path)
    write_json(path, session)
    return session


def save_session(root: Path, session: dict) -> None:
    write_json(session_path(root, session["run_id"], session.get("task_id"), session["agent_id"]), session)


def list_sessions(root: Path, run_id: str, task_id: str | None = None) -> list[dict]:
    base = run_dir(root, run_id) / ("sessions" if task_id is None else f"tasks/{task_id}/sessions")
    if not base.is_dir():
        return []
    return [read_json(path) for path in sorted(base.glob("*.json"))]
