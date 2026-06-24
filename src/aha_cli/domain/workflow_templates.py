from __future__ import annotations

from aha_cli.services.prompt_templates import render_prompt_template


def _workflow_guidance(template_id: str) -> str:
    return render_prompt_template(f"workflow_guidance_{template_id}.md").strip()


WORKFLOW_TEMPLATE_DEFINITIONS = (
    {
        "id": "auto",
        "label": "Auto detect",
        "description": "Automatically choose the fastest execution strategy from the task details.",
        "guidance": _workflow_guidance("auto"),
        "order": 0,
    },
    {
        "id": "bugfix",
        "label": "Bugfix",
        "description": "Diagnosis, fix, and regression verification strategy.",
        "guidance": _workflow_guidance("bugfix"),
        "order": 10,
    },
    {
        "id": "feature",
        "label": "Feature",
        "description": "Design, implementation, tests, and documentation strategy.",
        "guidance": _workflow_guidance("feature"),
        "order": 20,
    },
    {
        "id": "review",
        "label": "Review",
        "description": "Independent code, test, and risk review strategy.",
        "guidance": _workflow_guidance("review"),
        "order": 30,
    },
    {
        "id": "embedded-driver",
        "label": "Embedded driver",
        "description": "Datasheet/register analysis, driver work, and boundary-test strategy.",
        "guidance": _workflow_guidance("embedded-driver"),
        "order": 40,
    },
    {
        "id": "fault-debug",
        "label": "Fault debug",
        "description": "Crash/log analysis, recent-change review, and reproduction strategy.",
        "guidance": _workflow_guidance("fault-debug"),
        "order": 50,
    },
    {
        "id": "hil-regression",
        "label": "HIL regression",
        "description": "HIL test matrix, automation/log inspection, and regression-risk strategy.",
        "guidance": _workflow_guidance("hil-regression"),
        "order": 60,
    },
    {
        "id": "release",
        "label": "Release",
        "description": "Changelog/docs, build/package checks, and release risk review strategy.",
        "guidance": _workflow_guidance("release"),
        "order": 70,
    },
)

WORKFLOW_TEMPLATE_IDS = frozenset(item["id"] for item in WORKFLOW_TEMPLATE_DEFINITIONS)
WORKFLOW_TEMPLATE_GUIDANCE = {item["id"]: item["guidance"] for item in WORKFLOW_TEMPLATE_DEFINITIONS}


def workflow_template_metadata() -> list[dict]:
    return [dict(item) for item in sorted(WORKFLOW_TEMPLATE_DEFINITIONS, key=lambda item: item["order"])]


def workflow_template_ids() -> tuple[str, ...]:
    return tuple(item["id"] for item in workflow_template_metadata())


def normalize_workflow_template(value: object, default: str = "auto") -> str:
    template = str(value or default).strip().lower()
    return template if template in WORKFLOW_TEMPLATE_IDS else default


def workflow_template_guidance(value: object) -> str:
    return WORKFLOW_TEMPLATE_GUIDANCE[normalize_workflow_template(value)]


def is_workflow_template(value: object) -> bool:
    return str(value or "").strip().lower() in WORKFLOW_TEMPLATE_IDS
