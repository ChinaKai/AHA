from __future__ import annotations

from pathlib import Path
import re

from aha_cli.domain.models import utc_now
from aha_cli.services.session_compact import compact_reset_backend_session
from aha_cli.store.filesystem import append_event, append_task_round, ensure_session, save_session, task_snapshot


def normalize_phase(value: object) -> str:
    text = str(value or "").strip().lower()
    phase = re.sub(r"[^a-z0-9_.-]+", "-", text).strip(".-")
    if not phase:
        raise ValueError("phase is required")
    return phase


def transition_agent_phase(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    phase: str,
    *,
    summary: str = "",
    restart: bool = False,
) -> dict:
    phase = normalize_phase(phase)
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
    old_phase = str(session.get("phase") or "").strip()
    compact_payload = None
    if session.get("backend_session_id"):
        compact_payload = compact_reset_backend_session(
            root,
            run_id,
            task_id,
            agent_id,
            reason=f"phase:{phase}",
            restart=restart,
            stop_backend_before_reset=False,
        )
        session = compact_payload["session"]

    changed_at = utc_now()
    history = session.get("phase_history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "from": old_phase or None,
            "to": phase,
            "at": changed_at,
            "summary": summary,
            "compact_summary_path": (compact_payload or {}).get("summary_path"),
        }
    )
    session["phase"] = phase
    session["phase_updated_at"] = changed_at
    session["phase_history"] = history
    save_session(root, session)

    checkpoint_summary = summary.strip() or f"Phase changed from {old_phase or '-'} to {phase}."
    checkpoint = append_task_round(
        root,
        run_id,
        task_id,
        {
            "trigger": "phase_transition",
            "summary": checkpoint_summary,
            "agents": [agent_id],
            "phase": phase,
        },
    )
    event = append_event(
        root,
        run_id,
        "backend_session_phase_changed",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "old_phase": old_phase or None,
            "phase": phase,
            "summary": summary,
            "compact_reset": bool(compact_payload),
            "summary_path": (compact_payload or {}).get("summary_path"),
        },
    )
    return {
        "ok": True,
        "task_id": task_id,
        "agent_id": agent_id,
        "old_phase": old_phase or None,
        "phase": phase,
        "summary": summary,
        "compact_reset": compact_payload,
        "checkpoint": checkpoint,
        "event": event,
        "session": session,
    }


__all__ = ["normalize_phase", "transition_agent_phase"]
