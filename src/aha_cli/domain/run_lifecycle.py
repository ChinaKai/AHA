from __future__ import annotations

from collections.abc import Mapping

RUN_LIFECYCLE_ACTIVE = "active"
RUN_LIFECYCLE_HIDDEN = "hidden"
RUN_LIFECYCLE_ARCHIVED = "archived"
RUN_LIFECYCLE_CHOICES = (RUN_LIFECYCLE_ACTIVE, RUN_LIFECYCLE_HIDDEN, RUN_LIFECYCLE_ARCHIVED)
RUN_LIFECYCLE_STATUSES = set(RUN_LIFECYCLE_CHOICES)


def _lifecycle_dict(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalized_status(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key in ("status", "state"):
            status = _normalized_status(value.get(key))
            if status:
                return status
        return None
    status = str(value or "").strip().lower()
    return status if status in RUN_LIFECYCLE_STATUSES else None


def normalize_run_lifecycle_status(value: object) -> str:
    status = _normalized_status(value)
    if not status:
        raise ValueError(f"unknown run lifecycle status: {value}")
    return status


def _first_text(*values: object) -> str | None:
    for value in values:
        text = _text(value)
        if text is not None:
            return text
    return None


def run_lifecycle_projection(plan: Mapping[str, object]) -> dict[str, object]:
    lifecycle = _lifecycle_dict(plan.get("lifecycle"))
    run_lifecycle = _lifecycle_dict(plan.get("run_lifecycle"))
    explicit_status = (
        _normalized_status(plan.get("lifecycle"))
        or _normalized_status(plan.get("run_lifecycle"))
        or _normalized_status(plan.get("lifecycle_status"))
        or _normalized_status(plan.get("run_lifecycle_status"))
    )
    hidden_at = _first_text(plan.get("hidden_at"), lifecycle.get("hidden_at"), run_lifecycle.get("hidden_at"))
    archived_at = _first_text(plan.get("archived_at"), lifecycle.get("archived_at"), run_lifecycle.get("archived_at"))
    hidden = (
        _truthy(plan.get("hidden"))
        or _truthy(lifecycle.get("hidden"))
        or _truthy(run_lifecycle.get("hidden"))
        or bool(hidden_at)
        or explicit_status == RUN_LIFECYCLE_HIDDEN
    )
    archived = (
        _truthy(plan.get("archived"))
        or _truthy(lifecycle.get("archived"))
        or _truthy(run_lifecycle.get("archived"))
        or bool(archived_at)
        or explicit_status == RUN_LIFECYCLE_ARCHIVED
    )
    if archived:
        status = RUN_LIFECYCLE_ARCHIVED
    elif hidden:
        status = RUN_LIFECYCLE_HIDDEN
    else:
        status = explicit_status or RUN_LIFECYCLE_ACTIVE
    return {
        "status": status,
        "hidden": hidden,
        "hidden_at": hidden_at,
        "archived": archived,
        "archived_at": archived_at,
    }


def apply_run_lifecycle_status(plan: dict, status: object, *, timestamp: str) -> dict:
    normalized = normalize_run_lifecycle_status(status)
    hidden = normalized == RUN_LIFECYCLE_HIDDEN
    archived = normalized == RUN_LIFECYCLE_ARCHIVED
    hidden_at = timestamp if hidden else None
    archived_at = timestamp if archived else None
    lifecycle = {
        "status": normalized,
        "hidden": hidden,
        "hidden_at": hidden_at,
        "archived": archived,
        "archived_at": archived_at,
    }
    plan["lifecycle"] = lifecycle
    plan.pop("run_lifecycle", None)
    plan["lifecycle_status"] = normalized
    plan.pop("run_lifecycle_status", None)
    plan["hidden"] = hidden
    plan["hidden_at"] = hidden_at
    plan["archived"] = archived
    plan["archived_at"] = archived_at
    return lifecycle
