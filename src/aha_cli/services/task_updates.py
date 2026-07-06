from __future__ import annotations

from pathlib import Path

from aha_cli.store.filesystem import append_event, append_task_round


def task_update_round_payload(action: dict) -> dict | None:
    summary = str(action.get("summary") or "").strip()
    if not summary:
        return None
    return {
        "trigger": str(action.get("trigger") or "main_turn"),
        "summary": summary,
        "changed_files": action.get("changed_files") or action.get("files"),
        "verification": action.get("verification") or action.get("checks"),
        "risks": action.get("risks"),
        "agents": action.get("agents") or ["main"],
    }


def handle_record_task_update_action(root: Path, run_id: str, task_id: str, action: dict) -> dict | None:
    payload = task_update_round_payload(action)
    if payload is None:
        append_event(
            root,
            run_id,
            "action_skipped",
            {"task_id": task_id, "type": "record_task_update", "reason": "missing summary"},
        )
        return None
    record = append_task_round(root, run_id, task_id, payload)
    feedback = action.get("kb_feedback") or action.get("knowledge_feedback")
    if feedback:
        from aha_cli.services.context_evidence import record_agent_kb_feedback

        agents = payload.get("agents") if isinstance(payload.get("agents"), list) else []
        agent_id = str(agents[0] if agents else action.get("agent_id") or "main")
        record_agent_kb_feedback(
            root,
            run_id,
            task_id,
            agent_id=agent_id,
            feedback=feedback,
            source="record_task_update",
        )
    return {"type": "record_task_update", "round_id": record["round_id"]}
