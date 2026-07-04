from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from aha_cli.domain.models import make_session, utc_now
from aha_cli.store.io import iter_jsonl_reverse, read_json, write_json
from aha_cli.store.paths import event_path, run_dir, session_path

SESSION_RESET_EVENT_TYPES = {"backend_session_reset", "backend_session_compact_reset"}


def _non_negative_int(value: object) -> int:
    try:
        parsed = int(str(value).replace("_", "").replace(",", ""))
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def usage_token_summary(usage: dict | None) -> dict:
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = _non_negative_int(usage.get("input_tokens"))
    output_tokens = _non_negative_int(usage.get("output_tokens"))
    cached_tokens = (
        _non_negative_int(usage.get("cached_input_tokens") or usage.get("cache_read_input_tokens"))
        + _non_negative_int(usage.get("cache_creation_input_tokens"))
    )
    total_tokens = input_tokens + output_tokens
    if not total_tokens:
        total_tokens = _non_negative_int(usage.get("total_tokens"))
    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _event_data(event: dict) -> dict:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _event_backend_session_id(data: dict) -> str:
    return str(data.get("backend_session_id") or data.get("thread_id") or data.get("session_id") or "").strip()


def _event_matches_agent(data: dict, task_id: str | None, agent_id: str) -> bool:
    if task_id is not None and data.get("task_id") != task_id:
        return False
    target = str(data.get("target") or data.get("agent_id") or "")
    return not target or target == agent_id


def _history_has_usage(history: list | None) -> bool:
    if not isinstance(history, list):
        return False
    for item in history:
        if not isinstance(item, dict):
            continue
        summary = item.get("token_summary") if isinstance(item.get("token_summary"), dict) else {}
        if _non_negative_int(summary.get("total_tokens")):
            return True
        if usage_token_summary(item.get("last_usage") if isinstance(item.get("last_usage"), dict) else {}).get("total_tokens"):
            return True
    return False


def _usage_from_data(data: dict) -> dict:
    usage = data.get("usage")
    return usage if isinstance(usage, dict) else {}


def _event_starts_backend_session(event: dict, backend_session_id: str) -> bool:
    return event.get("type") == "agent_thread" and _event_backend_session_id(_event_data(event)) == backend_session_id


def latest_agent_usage(
    root: Path,
    run_id: str,
    task_id: str | None,
    agent_id: str,
    *,
    backend_session_id: str | None = None,
    history: list | None = None,
) -> dict:
    path = event_path(root, run_id)
    if not path.exists():
        return {}
    session_id = str(backend_session_id or "").strip()
    legacy_global_fallback = not session_id or not _history_has_usage(history)
    unscoped_candidate: dict | None = None
    for _offset, event in iter_jsonl_reverse(path) or ():
        data = _event_data(event)
        if not _event_matches_agent(data, task_id, agent_id):
            continue
        event_type = event.get("type")
        if event_type == "agent_usage":
            usage_session_id = _event_backend_session_id(data)
            if session_id and usage_session_id:
                if usage_session_id == session_id:
                    return unscoped_candidate or _usage_from_data(data)
                continue
            if unscoped_candidate is None:
                unscoped_candidate = _usage_from_data(data)
            if not session_id:
                return unscoped_candidate
            continue
        if not session_id:
            continue
        if _event_starts_backend_session(event, session_id):
            return unscoped_candidate or {}
        if event_type in SESSION_RESET_EVENT_TYPES:
            return unscoped_candidate or {}
    if unscoped_candidate is not None and legacy_global_fallback:
        return unscoped_candidate
    return {}


def backend_session_usage_archive_fields(
    root: Path,
    run_id: str,
    task_id: str | None,
    agent_id: str,
    *,
    backend_session_id: str | None = None,
    history: list | None = None,
) -> dict:
    usage = latest_agent_usage(root, run_id, task_id, agent_id, backend_session_id=backend_session_id, history=history)
    if not usage:
        return {}
    return {
        "last_usage": usage,
        "token_summary": usage_token_summary(usage),
    }


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
        if session.get("backend") != backend:
            previous_backend_session_id = session.get("backend_session_id")
            if previous_backend_session_id:
                history = session.get("history_backend_sessions")
                if not isinstance(history, list):
                    history = []
                history.append(
                    {
                        "backend_session_id": previous_backend_session_id,
                        "backend": session.get("backend"),
                        "model": session.get("model"),
                        "started_at": session.get("created_at"),
                        "archived_at": now_func(),
                        "reason": "backend_changed",
                    }
                    | backend_session_usage_archive_fields(
                        root,
                        run_id,
                        task_id,
                        agent_id,
                        backend_session_id=previous_backend_session_id,
                        history=history,
                    )
                )
                session["history_backend_sessions"] = history
            session["backend"] = backend
            session["backend_session_id"] = None
            session["status"] = "reset"
            session["compact_summary"] = None
            changed = True
        for key, value in {"model": model, "workspace_path": workspace_path}.items():
            if value is not None and session.get(key) != value:
                session[key] = value
                changed = True
        for key, value in {"history_backend_sessions": [], "compact_summary": None, "delivered_context_fingerprints": {}}.items():
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
