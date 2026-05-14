from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

from aha_cli.store.filesystem import add_agent, append_event, append_message, task_snapshot


def task_assignment_prompt(task: dict) -> str:
    return textwrap.dedent(
        f"""\
        You are now running in AHA mode.

        You are the task-main agent for this task.

        Task:
        {task.get("title", "")}

        Workspace path:
        {task.get("workspace_path") or "(not set)"}

        Delegation policy:
        {task.get("delegation_policy", "auto")}

        Max sub-agents:
        {task.get("max_sub_agents", 0)}

        Preferred sub-agent backend:
        {task.get("preferred_sub_backend") or task.get("preferred_backend") or "codex"}

        Default Codex permission:
        - sandbox: {task.get("preferred_sandbox") or "process default"}
        - approval: {task.get("preferred_approval") or "process default"}

        Responsibilities:
        1. Understand the task.
        2. Inspect the workspace if needed.
        3. Judge task complexity.
        4. Decide whether sub-agents are needed.
        5. If sub-agents are needed, return structured spawn_sub actions.
        6. If no sub-agent is needed, solve the task directly.
        7. Keep this task context isolated from other tasks.

        Return a concise response. If you need AHA to create sub-agents, include a JSON object
        in your response with this shape:

        {{
          "complexity": "simple|medium|complex",
          "actions": [
            {{
              "type": "spawn_sub",
              "title": "sub-agent assignment",
              "backend": "codex",
              "model": null,
              "sandbox": null,
              "approval": null,
              "reason": "why this sub-agent is needed"
            }}
          ],
          "response": "short user-facing summary"
        }}

        Do not pretend a sub-agent exists before AHA creates it.
        """
    )


def dispatch_task_to_main(root: Path, run_id: str, task: dict) -> dict:
    payload = append_message(
        root,
        run_id,
        "main",
        task_assignment_prompt(task),
        sender="system",
        task_id=task["id"],
        role="main",
        from_agent="system",
        to_agent="main",
    )
    append_event(root, run_id, "task_dispatched", {"task_id": task["id"], "target": "main"})
    return payload


def extract_action_payload(text: str) -> dict | None:
    candidates: list[str] = []
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL):
        candidates.append(match.group(1))
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def execute_actions(root: Path, run_id: str, task_id: str | None, text: str) -> list[dict]:
    if not task_id:
        return []
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return []
    max_sub_agents = int(task.get("max_sub_agents", 0) or 0)
    current_sub_agents = sum(1 for agent in task.get("agents", []) if agent.get("role") == "sub")
    payload = extract_action_payload(text)
    if not payload:
        return []
    executed: list[dict] = []
    for action in payload.get("actions", []):
        if not isinstance(action, dict) or action.get("type") != "spawn_sub":
            continue
        if task.get("delegation_policy") == "disabled" or current_sub_agents >= max_sub_agents:
            append_event(
                root,
                run_id,
                "action_skipped",
                {
                    "task_id": task_id,
                    "type": "spawn_sub",
                    "reason": "delegation disabled or max_sub_agents reached",
                    "max_sub_agents": max_sub_agents,
                },
            )
            continue
        agent = add_agent(
            root,
            run_id,
            task_id,
            backend=str(action.get("backend") or task.get("preferred_sub_backend") or task.get("preferred_backend") or "codex"),
            role="sub",
            model=action.get("model") if action.get("model") is not None else task.get("preferred_sub_model"),
            sandbox=action.get("sandbox") if action.get("sandbox") is not None else task.get("preferred_sandbox"),
            approval=action.get("approval") if action.get("approval") is not None else task.get("preferred_approval"),
            created_by="main",
            created_reason=str(action.get("reason") or action.get("title") or "main requested sub-agent"),
        )
        current_sub_agents += 1
        assignment = str(action.get("title") or action.get("prompt") or "Assist task-main with this task.")
        append_message(
            root,
            run_id,
            agent["id"],
            assignment,
            sender="main",
            task_id=task_id,
            role="sub",
            from_agent="main",
            to_agent=agent["id"],
        )
        executed.append({"type": "spawn_sub", "agent": agent})
    if executed:
        append_event(root, run_id, "actions_executed", {"task_id": task_id, "actions": executed})
    return executed
