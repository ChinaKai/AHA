from __future__ import annotations

from aha_cli.domain.models import TASK_COLLABORATION_MODES
from aha_cli.domain.workflow_templates import is_workflow_template, normalize_workflow_template


def optional_int_payload(payload: dict, key: str) -> int | None:
    if key not in payload or payload.get(key) in (None, ""):
        return None
    return int(payload.get(key))


def parse_execution_fields(
    payload: dict,
    *,
    default_collaboration_mode: str | None = None,
    include_legacy_controls: bool = False,
) -> dict:
    raw_collaboration_mode = payload.get("collaboration_mode")
    if raw_collaboration_mode in (None, ""):
        raw_collaboration_mode = default_collaboration_mode
    collaboration_mode = str(raw_collaboration_mode or "").strip() or None
    if collaboration_mode and collaboration_mode not in TASK_COLLABORATION_MODES:
        raise ValueError(f"unknown collaboration mode: {collaboration_mode}")
    raw_workflow_template = str(payload.get("workflow_template", "auto") or "auto")
    if not is_workflow_template(raw_workflow_template):
        raise ValueError(f"unknown workflow template: {raw_workflow_template}")
    workflow_template = normalize_workflow_template(raw_workflow_template)
    fields = {
        "collaboration_mode": collaboration_mode,
        "workflow_template": workflow_template,
    }
    if include_legacy_controls:
        fields["delegation_policy"] = str(payload.get("delegation_policy", "") or "") or None
        fields["max_sub_agents"] = optional_int_payload(payload, "max_sub_agents")
    return fields
