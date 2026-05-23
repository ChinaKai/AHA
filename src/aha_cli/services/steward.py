"""Rules-only steward layer for deterministic task coordination.

Steward is a status gatekeeper, not a semantic judge. It owns hard runtime
coordination checks and duplicate handoff prevention; any non-empty main reply
that requires interpreting intent is handed to the delegated browser-control
host as ``semantic_review``.
"""

from __future__ import annotations

from pathlib import Path

from aha_cli.store.filesystem import (
    append_event,
    iter_jsonl_reverse,
    run_dir,
    task_snapshot,
)


ACTIVE_AGENT_STATUSES = {"active", "pending", "running", "waiting", "starting"}
TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}
STEWARD_ALLOWED_APPLY_DECISIONS: set[str] = set()
STEWARD_SEMANTIC_HANDOFF_DECISION = "semantic_review"
STEWARDSHIP_BOUNDARY_RULES = {
    "steward_allowed_apply_decisions": sorted(STEWARD_ALLOWED_APPLY_DECISIONS),
    "semantic_handoff_decision": STEWARD_SEMANTIC_HANDOFF_DECISION,
    "semantic_decision_owner": "delegated_browser_control_host",
    "failure_fallback_owner": "supervision_host_or_user_confirmation",
    "status_channels": ["main_backend", "host_backend", "steward_decision"],
}


def _message_endpoint(item: dict, key: str, fallback: str) -> str:
    return str(item.get(key) or item.get(fallback) or "")


def recent_task_messages(root: Path, run_id: str, task_id: str, limit: int = 16) -> list[dict]:
    path = run_dir(root, run_id) / "tasks" / task_id / "messages.jsonl"
    rows: list[dict] = []
    for _offset, item in iter_jsonl_reverse(path) or ():
        rows.append(
            {
                "ts": item.get("ts"),
                "from": _message_endpoint(item, "from_agent", "sender"),
                "to": _message_endpoint(item, "to_agent", "target"),
                "sender": item.get("sender"),
                "target": item.get("target"),
                "coordination": item.get("coordination"),
                "message": str(item.get("message") or "")[:800],
            }
        )
        if len(rows) >= limit:
            break
    return list(reversed(rows))


def _latest_main_reply(messages: list[dict]) -> dict | None:
    for item in reversed(messages):
        if item.get("from") == "main" and item.get("to") == "browser":
            return item
    return None


def _latest_message(messages: list[dict]) -> dict:
    return messages[-1] if messages else {}


def _sub_agent_statuses(task: dict) -> list[dict]:
    return [
        {"id": agent.get("id"), "status": agent.get("status")}
        for agent in task.get("agents", [])
        if str(agent.get("role") or "") == "sub"
    ]


def _active_sub_agents(task: dict) -> list[dict]:
    return [agent for agent in _sub_agent_statuses(task) if str(agent.get("status") or "") in ACTIVE_AGENT_STATUSES]


def _decision(decision: str, reason: str, *, prompt_to_main: str = "", confidence: float = 0.8) -> dict:
    return {
        "decision": decision,
        "reason": reason,
        "confidence": confidence,
        "prompt_to_main": prompt_to_main,
        "source": "rules",
    }


def _semantic_review_already_queued(messages: list[dict]) -> bool:
    latest_main_reply_index = -1
    latest_main_reply_text = ""
    for index, item in enumerate(messages):
        if item.get("from") == "main" and item.get("to") == "browser":
            latest_main_reply_index = index
            latest_main_reply_text = str(item.get("message") or "").strip()
    if latest_main_reply_index < 0:
        return False
    for item in messages[latest_main_reply_index + 1 :]:
        if (
            item.get("from") == "main"
            and item.get("to") not in {"browser", "main"}
            and str(item.get("message") or "").strip() == latest_main_reply_text
        ):
            return True
    return False


def decide_steward_next(task: dict, messages: list[dict]) -> dict:
    task_id = str(task.get("id") or "")
    status = str(task.get("status") or "")
    coordination = task.get("coordination") if isinstance(task.get("coordination"), dict) else {}
    if status in TERMINAL_TASK_STATUSES:
        return _decision("noop", f"{task_id} is terminal: {status}", confidence=1.0)
    if coordination.get("round_summary_requested_at") and not coordination.get("round_summary_completed_at"):
        return _decision("noop", "round summary is already requested by existing sub-agent coordination", confidence=1.0)
    active_subs = _active_sub_agents(task)
    if active_subs:
        return _decision("wait", "active sub-agents are still owned by existing AHA coordination", confidence=1.0)

    last = _latest_message(messages)
    if last.get("from") == "browser" and last.get("to") == "main":
        return _decision("noop", "latest browser message is already queued for main", confidence=0.95)
    if last.get("from") == "sub" or str(last.get("from") or "").startswith("sub-"):
        return _decision("wait", "latest sub-agent message should be handled by existing coordination", confidence=0.9)

    main_reply = _latest_main_reply(messages)
    if not main_reply:
        return _decision("noop", "no main user-facing reply to steward", confidence=0.8)

    text = str(main_reply.get("message") or "").strip()
    if not text:
        return _decision("noop", "latest main reply is empty", confidence=0.8)
    return _decision(
        STEWARD_SEMANTIC_HANDOFF_DECISION,
        "latest main reply requires delegated browser-control semantic decision",
        confidence=0.7,
    )


def steward_decision_snapshot(root: Path, run_id: str, task_id: str, *, message_limit: int = 16) -> dict:
    task = task_snapshot(root, run_id, task_id)["task"]
    messages = recent_task_messages(root, run_id, task_id, limit=message_limit)
    return {
        "task_id": task_id,
        "status": task.get("status"),
        "boundary_rules": STEWARDSHIP_BOUNDARY_RULES,
        "coordination": task.get("coordination") or {},
        "sub_agents": _sub_agent_statuses(task),
        "latest_message": _latest_message(messages),
        "decision": decide_steward_next(task, messages),
        "recent_messages": messages,
    }


def apply_steward_decision(root: Path, run_id: str, task_id: str) -> dict:
    snapshot = steward_decision_snapshot(root, run_id, task_id)
    decision = snapshot["decision"]
    decision_name = str(decision.get("decision") or "")
    if decision_name == STEWARD_SEMANTIC_HANDOFF_DECISION:
        if _semantic_review_already_queued(snapshot.get("recent_messages") or []):
            append_event(
                root,
                run_id,
                "steward_decision_skipped",
                {
                    "task_id": task_id,
                    "decision": STEWARD_SEMANTIC_HANDOFF_DECISION,
                    "reason": "semantic_review already queued for latest main reply",
                },
            )
            return {"applied": False, "semantic_review": False, "snapshot": snapshot}
        append_event(
            root,
            run_id,
            "steward_semantic_review_requested",
            {
                "task_id": task_id,
                "reason": decision.get("reason"),
            },
        )
        return {"applied": False, "semantic_review": True, "snapshot": snapshot}
    if decision_name not in STEWARD_ALLOWED_APPLY_DECISIONS:
        append_event(
            root,
            run_id,
            "steward_decision_skipped",
            {
                "task_id": task_id,
                "decision": decision.get("decision"),
                "reason": decision.get("reason"),
            },
        )
        return {"applied": False, "snapshot": snapshot}

    return {"applied": False, "snapshot": snapshot}
