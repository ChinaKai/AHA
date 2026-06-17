from __future__ import annotations

import json
import re
from typing import NamedTuple

AHA_ACTION_TYPES = {"route_to_agent", "spawn_sub", "record_task_update"}


class ActionPayloadExtraction(NamedTuple):
    payload: dict | None
    recovered: bool = False
    error: str | None = None


def _template_value(value: object) -> bool:
    if isinstance(value, str):
        return value.strip() == "..."
    if isinstance(value, list):
        return any(_template_value(item) for item in value)
    if isinstance(value, dict):
        return any(_template_value(item) for item in value.values())
    return False


def _loads_payload(candidate: str) -> dict | None:
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or _template_value(payload):
        return None
    return payload


def _action_like_payload(payload: dict) -> bool:
    return "actions" in payload or "action" in payload or payload.get("type") in AHA_ACTION_TYPES


def _payload_candidates(text: str) -> list[tuple[dict, bool]]:
    stripped = text.strip()
    candidates: list[tuple[str, bool]] = []
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append((stripped, False))
    fenced_match = re.fullmatch(r"```\s*(?:json)?\s*(\{.*\})\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fenced_match:
        candidates.append((fenced_match.group(1), True))
    for match in re.finditer(r"```\s*(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL | re.IGNORECASE):
        candidates.append((match.group(1), True))

    payloads: list[tuple[dict, bool]] = []
    seen: set[str] = set()
    for candidate, recovered in candidates:
        payload = _loads_payload(candidate)
        if payload is None:
            continue
        key = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        payloads.append((payload, recovered))
    return payloads


def extract_action_payload_result(text: str) -> ActionPayloadExtraction:
    candidates = [(payload, recovered) for payload, recovered in _payload_candidates(text) if _action_like_payload(payload)]
    if len(candidates) > 1:
        return ActionPayloadExtraction(None, error="multiple action payloads found")
    if candidates:
        payload, recovered = candidates[0]
        return ActionPayloadExtraction(payload, recovered=recovered)
    return ActionPayloadExtraction(None)


def extract_action_payload(text: str) -> dict | None:
    return extract_action_payload_result(text).payload


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
