from __future__ import annotations

import json
import re

AHA_ACTION_TYPES = {"route_to_agent", "spawn_sub", "record_task_update"}


def extract_action_payload(text: str) -> dict | None:
    stripped = text.strip()
    candidates: list[str] = []
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)
    fenced_match = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", stripped, flags=re.DOTALL)
    if fenced_match:
        candidates.append(fenced_match.group(1))
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def invalid_action_schema_reason(payload: dict) -> str | None:
    if "action" in payload:
        return "top-level action is not supported; use actions array"
    if payload.get("type") in AHA_ACTION_TYPES:
        return "top-level type is not supported; use actions array"
    if "actions" not in payload:
        return None
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return "actions must be a list"
    for action in actions:
        if not isinstance(action, dict):
            return "actions must contain objects"
        action_type = action.get("type")
        if not action_type:
            return "each action must include type"
        if action_type not in AHA_ACTION_TYPES:
            return f"unknown action type: {action_type}"
    return None


def invalid_action_schema_message(reason: str) -> str:
    return (
        "Invalid AHA action schema: "
        f"{reason}. Use {{\"actions\":[{{\"type\":\"route_to_agent\", ...}}], \"response\":\"...\"}}."
    )


def action_response_text(text: str) -> str:
    payload = extract_action_payload(text)
    if payload:
        reason = invalid_action_schema_reason(payload)
        if reason:
            return invalid_action_schema_message(reason)
    if payload and isinstance(payload.get("response"), str):
        return payload["response"].strip()
    return text.strip()
