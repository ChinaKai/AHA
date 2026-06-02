from __future__ import annotations

WORKFLOW_TEMPLATE_DEFINITIONS = (
    {
        "id": "auto",
        "label": "Auto detect",
        "description": "Automatically choose the fastest execution strategy from the task details.",
        "guidance": (
            "Auto detect: choose the fastest execution path from the task details. Use sub-agents only "
            "when independent tracks can reduce elapsed time."
        ),
        "order": 0,
    },
    {
        "id": "bugfix",
        "label": "Bugfix",
        "description": "Diagnosis, fix, and regression verification strategy.",
        "guidance": (
            "Bugfix: split only if diagnosis, implementation, and regression checks can proceed in "
            "parallel with low coordination cost."
        ),
        "order": 10,
    },
    {
        "id": "feature",
        "label": "Feature",
        "description": "Design, implementation, tests, and documentation strategy.",
        "guidance": (
            "Feature: keep main responsible for design and integration; use sub-agents for isolated "
            "implementation, tests, or documentation tracks when that shortens the critical path."
        ),
        "order": 20,
    },
    {
        "id": "review",
        "label": "Review",
        "description": "Independent code, test, and risk review strategy.",
        "guidance": (
            "Review: use sub-agents for independent risk, test, or code review passes; main merges "
            "findings and decides what matters."
        ),
        "order": 30,
    },
    {
        "id": "embedded-driver",
        "label": "Embedded driver",
        "description": "Datasheet/register analysis, driver work, and boundary-test strategy.",
        "guidance": (
            "Embedded driver: separate datasheet/register analysis, driver implementation, and "
            "boundary-test work when those tracks can run independently."
        ),
        "order": 40,
    },
    {
        "id": "fault-debug",
        "label": "Fault debug",
        "description": "Crash/log analysis, recent-change review, and reproduction strategy.",
        "guidance": (
            "Fault debug: separate crash/log analysis, recent-change review, and reproduction or "
            "verification planning when available evidence supports parallel work."
        ),
        "order": 50,
    },
    {
        "id": "hil-regression",
        "label": "HIL regression",
        "description": "HIL test matrix, automation/log inspection, and regression-risk strategy.",
        "guidance": (
            "HIL regression: split test-matrix preparation, automation/log inspection, and regression "
            "risk review when hardware or logs make those tracks independent."
        ),
        "order": 60,
    },
    {
        "id": "release",
        "label": "Release",
        "description": "Changelog/docs, build/package checks, and release risk review strategy.",
        "guidance": (
            "Release: separate changelog/docs, build/package checks, and final risk review while main "
            "keeps the release decision centralized."
        ),
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
