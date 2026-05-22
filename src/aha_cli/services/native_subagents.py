from __future__ import annotations

CLAUDE_NATIVE_SUBAGENT_TOOLS = ("Agent", "Task", "TaskCreate")

SUBAGENT_CLAIM_SUBJECTS = (
    "sub agent",
    "sub-agent",
    "subagent",
    "sub agents",
    "sub-agents",
    "subagents",
    "子 agent",
    "子agent",
)

SUBAGENT_CLAIM_ACTIONS = (
    "已",
    "created",
    "started",
    "launched",
    "spawned",
)


def claude_disallowed_subagent_tools_arg() -> str:
    return ",".join(CLAUDE_NATIVE_SUBAGENT_TOOLS)


def text_claims_subagent_created(text: object) -> bool:
    lower = str(text or "").lower()
    if not any(subject in lower for subject in SUBAGENT_CLAIM_SUBJECTS):
        return False
    return any(action in lower for action in SUBAGENT_CLAIM_ACTIONS)
