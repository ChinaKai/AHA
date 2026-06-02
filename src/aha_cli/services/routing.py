from __future__ import annotations

ROUTE_TO_AGENT_SKIP_REASON = "missing target agent, message, or target is main"


def route_to_agent_request(task: dict, action: dict) -> dict:
    target_id = str(action.get("agent_id") or action.get("target") or "").strip()
    message = str(action.get("message") or action.get("prompt") or "").strip()
    target_agent = next((agent for agent in task.get("agents", []) if agent.get("id") == target_id), None)
    if not target_agent or not message or target_id == "main":
        return {
            "ok": False,
            "target": target_id,
            "reason": ROUTE_TO_AGENT_SKIP_REASON,
        }
    return {
        "ok": True,
        "target": target_id,
        "message": message,
        "agent": target_agent,
        "reason": str(action.get("reason") or ""),
    }


def route_to_agent_skip_event(task_id: str, request: dict) -> dict:
    return {
        "task_id": task_id,
        "type": "route_to_agent",
        "target": request.get("target", ""),
        "reason": str(request.get("reason") or ROUTE_TO_AGENT_SKIP_REASON),
    }


def route_to_agent_routed_event(task_id: str, request: dict) -> dict:
    return {
        "task_id": task_id,
        "target": request["target"],
        "reason": str(request.get("reason") or ""),
        "chars": len(str(request.get("message") or "")),
    }


def route_to_agent_result(request: dict) -> dict:
    return {"type": "route_to_agent", "agent": request["agent"]}
