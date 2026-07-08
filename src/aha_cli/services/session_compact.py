from __future__ import annotations

import json
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.backend_runtime import PROCESS_AGENT_BACKENDS, backend_status, start_backend, stop_backend
from aha_cli.services.chat import chat_offset_path, save_chat_offset
from aha_cli.services.messages import format_event
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.filesystem import (
    append_event,
    ensure_session,
    event_path,
    inbox_path,
    iter_jsonl_from,
    list_task_rounds,
    read_json,
    run_dir,
    save_session,
    session_path,
    task_snapshot,
)
from aha_cli.store.sessions import backend_session_usage_archive_fields, set_force_full_prompt_next_turn, usage_token_summary


def compact_summary_dir(root: Path, run_id: str, task_id: str) -> Path:
    return run_dir(root, run_id) / "tasks" / task_id / "compacts"


def compact_summary_relpath(root: Path, run_id: str, path: Path) -> str:
    try:
        return str(path.relative_to(run_dir(root, run_id)))
    except ValueError:
        return str(path)


def backend_session_jsonl_file(session: dict) -> Path | None:
    session_id = str(session.get("backend_session_id") or "").strip()
    backend = str(session.get("backend") or "").strip()
    if not session_id:
        return None
    home = Path.home()
    candidates = (
        list((home / ".claude" / "projects").glob(f"*/*{session_id}.jsonl"))
        if backend == "claude"
        else list((home / ".codex" / "sessions").glob(f"**/*{session_id}.jsonl"))
    )
    return candidates[0] if candidates else None


def session_jsonl_snapshot(session: dict) -> dict:
    path = backend_session_jsonl_file(session)
    if not path or not path.exists():
        return {"path": str(path) if path else "", "size_bytes": None, "exists": False}
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "size_bytes": None, "exists": False}
    return {"path": str(path), "size_bytes": stat.st_size, "modified_at": stat.st_mtime, "exists": True}


def _truncate(value: object, limit: int = 360) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _latest_task_events(root: Path, run_id: str, task_id: str, agent_id: str, limit: int = 20) -> list[dict]:
    path = event_path(root, run_id)
    if not path.exists():
        return []
    rows, _ = iter_jsonl_from(path, 0)
    selected = []
    for event in rows:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if data.get("task_id") != task_id:
            continue
        target = str(data.get("target") or data.get("agent_id") or "")
        if target and target != agent_id and event.get("type") not in {"message", "task_round_recorded", "task_journal_rendered"}:
            continue
        selected.append(event)
    return selected[-limit:]


def _latest_task_messages(root: Path, run_id: str, task_id: str, limit: int = 12) -> list[dict]:
    path = run_dir(root, run_id) / "tasks" / task_id / "messages.jsonl"
    if not path.exists():
        return []
    rows, _ = iter_jsonl_from(path, 0)
    return rows[-limit:]


def latest_event_payload(root: Path, run_id: str, task_id: str, agent_id: str, event_type: str) -> dict:
    for event in reversed(_latest_task_events(root, run_id, task_id, agent_id, limit=200)):
        if event.get("type") == event_type:
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            return data
    return {}


def build_compact_summary(root: Path, run_id: str, task_id: str, agent_id: str, session: dict, reason: str) -> str:
    detail = task_snapshot(root, run_id, task_id)
    task = detail["task"]
    agent = next((item for item in task.get("agents", []) if item.get("id") == agent_id), {})
    rounds = list_task_rounds(root, run_id, task_id)[-12:]
    messages = _latest_task_messages(root, run_id, task_id)
    events = _latest_task_events(root, run_id, task_id, agent_id)
    jsonl = session_jsonl_snapshot(session)
    usage = backend_session_usage_archive_fields(
        root,
        run_id,
        task_id,
        agent_id,
        backend_session_id=session.get("backend_session_id"),
        backend=session.get("backend"),
        history=session.get("history_backend_sessions") if isinstance(session.get("history_backend_sessions"), list) else [],
    ).get("last_usage", {})
    metrics = latest_event_payload(root, run_id, task_id, agent_id, "agent_prompt_metrics")

    task_journal = "\n".join(
        f"- `{item.get('round_id')}` [{item.get('trigger') or '-'}] {_truncate(item.get('summary'), 280)}"
        for item in rounds
    ) or "- none"
    recent_messages = "\n".join(
        f"- `{item.get('ts') or '-'}` {item.get('sender') or item.get('from_agent') or '-'} -> {item.get('to_agent') or item.get('target') or '-'}: {_truncate(item.get('message'), 320)}"
        for item in messages
    ) or "- none"
    recent_events = "\n".join(f"- {format_event(event)}" for event in events) or "- none"
    return render_prompt_template(
        "compact_summary.md",
        reason=reason,
        created_at=utc_now(),
        run_id=run_id,
        task_id=task_id,
        title=_truncate(task.get("title"), 220),
        original_request=_truncate(" ".join(str(task.get("description") or "").split()), 500) or "-",
        status=task.get("status") or "-",
        current_round_id=task.get("current_round_id") or "-",
        round_sequence=task.get("round_sequence") or "-",
        last_final_round_id=task.get("last_final_round_id") or "-",
        workspace=task.get("workspace_path") or "-",
        agent_id=agent_id,
        role=agent.get("role") or "-",
        backend=session.get("backend") or agent.get("backend") or task.get("preferred_backend") or "-",
        model=session.get("model") or agent.get("model") or task.get("preferred_model") or "-",
        sandbox=agent.get("sandbox") or task.get("preferred_sandbox") or "-",
        approval=agent.get("approval") or task.get("preferred_approval") or "-",
        backend_session_id=session.get("backend_session_id") or "-",
        jsonl_path=jsonl.get("path") or "-",
        jsonl_exists=jsonl.get("exists"),
        size_bytes=jsonl.get("size_bytes") if jsonl.get("size_bytes") is not None else "-",
        latest_usage=json.dumps(usage, ensure_ascii=False) if usage else "-",
        latest_prompt_mode=metrics.get("prompt_mode") or "-",
        task_journal=task_journal,
        recent_messages=recent_messages,
        recent_events=recent_events,
    )


def compact_reset_backend_session(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    *,
    reason: str = "manual",
    restart: bool = False,
    dry_run: bool = False,
    stop_backend_before_reset: bool = True,
) -> dict:
    detail = task_snapshot(root, run_id, task_id)
    task = detail["task"]
    agent = next((item for item in task.get("agents", []) if item.get("id") == agent_id), None)
    if not agent:
        raise KeyError(f"Agent not found: {agent_id}")
    session = ensure_session(
        root,
        run_id,
        task_id,
        agent_id,
        str(agent.get("backend") or task.get("preferred_backend") or "codex"),
        model=agent.get("model") or task.get("preferred_model"),
        workspace_path=agent.get("workspace_path") or task.get("workspace_path"),
    )
    old_backend_session_id = session.get("backend_session_id")
    if not old_backend_session_id:
        raise ValueError(f"No backend session to compact/reset for {task_id}/{agent_id}")

    created_at = utc_now()
    summary = build_compact_summary(root, run_id, task_id, agent_id, session, reason)
    summary_id = f"{agent_id}-{created_at.replace(':', '').replace('+', 'Z')}"
    summary_dir = compact_summary_dir(root, run_id, task_id)
    summary_path = summary_dir / f"{summary_id}.md"
    meta_path = summary_dir / f"{summary_id}.json"
    jsonl = session_jsonl_snapshot(session)
    usage_archive_fields = backend_session_usage_archive_fields(
        root,
        run_id,
        task_id,
        agent_id,
        backend_session_id=old_backend_session_id,
        backend=session.get("backend"),
        history=session.get("history_backend_sessions") if isinstance(session.get("history_backend_sessions"), list) else [],
    )
    latest_usage = usage_archive_fields.get("last_usage", {})
    latest_metrics = latest_event_payload(root, run_id, task_id, agent_id, "agent_prompt_metrics")
    archive = {
        "backend_session_id": old_backend_session_id,
        "backend": session.get("backend"),
        "model": session.get("model"),
        "started_at": session.get("created_at"),
        "ended_at": created_at,
        "reason": reason,
        "summary_id": summary_id,
        "summary_ref": compact_summary_relpath(root, run_id, summary_path),
        "jsonl_path": jsonl.get("path"),
        "size_bytes": jsonl.get("size_bytes"),
        "last_usage": latest_usage,
        "token_summary": usage_archive_fields.get("token_summary", usage_token_summary(latest_usage, backend=session.get("backend"))),
        "metrics_snapshot": latest_metrics,
    }
    result = {
        "ok": True,
        "task_id": task_id,
        "agent_id": agent_id,
        "reason": reason,
        "old_backend_session_id": old_backend_session_id,
        "summary_id": summary_id,
        "summary_path": compact_summary_relpath(root, run_id, summary_path),
        "archive": archive,
        "restart": restart,
        "dry_run": dry_run,
    }
    if dry_run:
        result["summary_preview"] = summary
        return result

    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary, encoding="utf-8")
    meta_path.write_text(json.dumps({"archive": archive, "created_at": created_at}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if stop_backend_before_reset and backend_status(root, run_id, agent_id, task_id=task_id).get("status") != "stopped":
        result["stopped_backend"] = stop_backend(root, run_id, agent_id, task_id=task_id, timeout=3.0)

    history = session.get("history_backend_sessions")
    if not isinstance(history, list):
        history = []
    history.append(archive)
    session["history_backend_sessions"] = history
    session["backend_session_id"] = None
    session["status"] = "reset"
    session["updated_at"] = created_at
    session["compact_summary"] = {
        "id": summary_id,
        "path": compact_summary_relpath(root, run_id, summary_path),
        "created_at": created_at,
        "reason": reason,
        "chars": len(summary),
        "archived_backend_session_id": old_backend_session_id,
    }
    set_force_full_prompt_next_turn(
        session,
        "backend_session_compact_reset",
        detected_at=created_at,
        trigger=reason,
        summary_path=compact_summary_relpath(root, run_id, summary_path),
    )
    save_session(root, session)

    offset_file = chat_offset_path(run_dir(root, run_id), agent_id, task_id)
    if not offset_file.exists():
        inbox = inbox_path(root, run_id, agent_id)
        save_chat_offset(offset_file, inbox.stat().st_size if inbox.exists() else 0)
    append_event(
        root,
        run_id,
        "backend_session_compact_reset",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "reason": reason,
            "old_backend_session_id": old_backend_session_id,
            "summary_id": summary_id,
            "summary_path": compact_summary_relpath(root, run_id, summary_path),
            "restart": restart,
        },
    )
    if restart and str(session.get("backend") or "") in PROCESS_AGENT_BACKENDS:
        result["backend"] = start_backend(
            root,
            run_id,
            agent_id,
            backend=str(session.get("backend") or "codex"),
            model=session.get("model") or agent.get("model") or task.get("preferred_model"),
            sandbox=agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
            approval=agent.get("approval") or task.get("preferred_approval") or "never",
            task_id=task_id,
        )
    result["session"] = read_json(session_path(root, run_id, task_id, agent_id))
    return result
