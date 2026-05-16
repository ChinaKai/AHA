from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.backend_runtime import backend_status, start_backend
from aha_cli.services.commit_policy import commit_message_policy_prompt
from aha_cli.store.filesystem import (
    add_agent,
    append_event,
    append_message,
    mark_task_coordination,
    set_agent_status,
    set_task_status,
    status_snapshot,
    task_snapshot,
    update_agent_runtime,
    run_dir,
)

TERMINAL_AGENT_STATUSES = {"completed", "failed", "blocked"}
WATCHDOG_MAX_RECOVERY_ATTEMPTS = 3


def task_has_active_followup(task: dict) -> bool:
    if task.get("status") in TERMINAL_AGENT_STATUSES:
        return False
    coordination = task.get("coordination") or {}
    return bool(
        coordination.get("followup_started_at")
        and not coordination.get("final_summary_requested_at")
        and not coordination.get("final_summary_completed_at")
    )


def task_assignment_prompt(task: dict) -> str:
    commit_policy = textwrap.indent(commit_message_policy_prompt(str(task.get("id") or "<task-id>"), "<agent-id>").rstrip(), "        ")
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

        Commit ownership policy:
        - Treat commit, revert, and repository-change finalization requests as ownership-sensitive work.
        - If a commit request belongs to one existing sub-agent's assignment, route it to that sub-agent with `route_to_agent`.
        - If a commit spans multiple owners, route work per owner or coordinate one aggregate commit only after verifying file ownership.
        - Never ask a sub-agent to commit files outside its assignment.
        - Follow the AHA commit message policy below for every commit.

{commit_policy}

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
            }},
            {{
              "type": "route_to_agent",
              "agent_id": "sub-001",
              "message": "follow-up for the agent that owns this scope",
              "reason": "why this existing sub-agent owns the follow-up"
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


def action_response_text(text: str) -> str:
    payload = extract_action_payload(text)
    if payload and isinstance(payload.get("response"), str):
        return payload["response"].strip()
    return text.strip()


def chat_offset_exists(root: Path, run_id: str, target: str, task_id: str | None = None) -> bool:
    safe_target = target.replace("/", "_")
    if task_id:
        safe_task = task_id.replace("/", "_")
        return (run_dir(root, run_id) / "runtime" / f"chat-offset-{safe_task}-{safe_target}.json").exists()
    return (run_dir(root, run_id) / "runtime" / f"chat-offset-{safe_target}.json").exists()


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
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")
        if action_type == "route_to_agent":
            target_id = str(action.get("agent_id") or action.get("target") or "").strip()
            message = str(action.get("message") or action.get("prompt") or "").strip()
            target_agent = next((agent for agent in task.get("agents", []) if agent.get("id") == target_id), None)
            if not target_agent or not message or target_id == "main":
                append_event(
                    root,
                    run_id,
                    "action_skipped",
                    {
                        "task_id": task_id,
                        "type": "route_to_agent",
                        "target": target_id,
                        "reason": "missing target agent, message, or target is main",
                    },
                )
                continue
            mark_task_coordination(
                root,
                run_id,
                task_id,
                final_summary_requested_at="",
                final_summary_completed_at="",
                round_summary_requested_at="",
                round_summary_completed_at="",
                followup_started_at=utc_now(),
            )
            set_task_status(root, run_id, task_id, "running")
            set_agent_status(root, run_id, task_id, target_id, "pending")
            append_message(
                root,
                run_id,
                target_id,
                message,
                sender="main",
                task_id=task_id,
                role="sub",
                from_agent="main",
                to_agent=target_id,
                coordination="routed_by_main",
            )
            append_event(
                root,
                run_id,
                "agent_message_routed",
                {
                    "task_id": task_id,
                    "target": target_id,
                    "reason": str(action.get("reason") or ""),
                    "chars": len(message),
                },
            )
            if target_agent.get("backend") == "codex":
                start_backend(
                    root,
                    run_id,
                    target_id,
                    model=target_agent.get("model"),
                    sandbox=target_agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
                    approval=target_agent.get("approval") or task.get("preferred_approval") or "never",
                    from_start=not chat_offset_exists(root, run_id, target_id, task_id),
                    task_id=task_id,
                )
            executed.append({"type": "route_to_agent", "agent": target_agent})
            continue
        if action_type != "spawn_sub":
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
        mark_task_coordination(
            root,
            run_id,
            task_id,
            final_summary_requested_at="",
            final_summary_completed_at="",
            round_summary_requested_at="",
            round_summary_completed_at="",
            followup_started_at=utc_now(),
        )
        set_task_status(root, run_id, task_id, "running")
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
        agent = update_agent_runtime(root, run_id, task_id, agent["id"], assignment=assignment)
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
        if agent.get("backend") == "codex":
            start_backend(
                root,
                run_id,
                agent["id"],
                model=agent.get("model"),
                sandbox=agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
                approval=agent.get("approval") or task.get("preferred_approval") or "never",
                from_start=True,
                task_id=task_id,
            )
        executed.append({"type": "spawn_sub", "agent": agent})
    if executed:
        append_event(root, run_id, "actions_executed", {"task_id": task_id, "actions": executed})
    return executed


def sub_agents(task: dict) -> list[dict]:
    return [agent for agent in task.get("agents", []) if agent.get("role") == "sub"]


def pending_sub_agents(task: dict) -> list[dict]:
    return [agent for agent in sub_agents(task) if agent.get("status") not in TERMINAL_AGENT_STATUSES]


def task_has_incomplete_sub_agents(task: dict) -> bool:
    return bool(pending_sub_agents(task))


def waiting_for_subagents_message(task: dict) -> str:
    pending = pending_sub_agents(task)
    if not pending:
        return "所有子 agent 已完成，等待 task-main 做本轮汇总。"
    names = ", ".join(agent.get("id", "-") for agent in pending)
    done = len(sub_agents(task)) - len(pending)
    return f"等待子 agent 完成：{names}。当前进度 {done}/{len(sub_agents(task))}。"


def request_round_summary_if_ready(root: Path, run_id: str, task_id: str) -> bool:
    task = task_snapshot(root, run_id, task_id)["task"]
    agents = sub_agents(task)
    coordination = task.get("coordination") or {}
    if (
        coordination.get("final_summary_requested_at")
        or coordination.get("final_summary_completed_at")
        or coordination.get("round_summary_requested_at")
        or coordination.get("round_summary_completed_at")
    ):
        return False
    if not agents or pending_sub_agents(task):
        append_event(
            root,
            run_id,
            "task_waiting_for_subagents",
            {
                "task_id": task_id,
                "pending": [agent.get("id") for agent in pending_sub_agents(task)],
                "completed": [agent.get("id") for agent in agents if agent.get("status") in TERMINAL_AGENT_STATUSES],
            },
        )
        return False
    if task.get("status") in TERMINAL_AGENT_STATUSES:
        return False
    task = mark_task_coordination(root, run_id, task_id, round_summary_requested_at=utc_now())
    prompt = textwrap.dedent(
        f"""\
        All sub-agents for {task_id} have completed this round.

        Produce a concise user-facing round summary now. Include:
        - what each sub-agent completed,
        - validation performed,
        - remaining risks or follow-up work.

        Do not mark the task complete or final. The task remains open for follow-up.
        Do not send acknowledgements back to sub-agents.
        """
    )
    append_message(
        root,
        run_id,
        "main",
        prompt,
        sender="aha",
        task_id=task_id,
        role="main",
        from_agent="aha",
        to_agent="main",
        reply_target="browser",
        coordination="subagents_complete",
    )
    event_data = {"task_id": task_id, "target": "main", "reason": "subagents_complete", "policy": "round_summary"}
    append_event(root, run_id, "task_round_summary_requested", event_data)
    append_event(root, run_id, "task_final_requested", event_data)
    return True


def request_final_summary_if_ready(root: Path, run_id: str, task_id: str) -> bool:
    return request_round_summary_if_ready(root, run_id, task_id)


def record_sub_agent_report(
    root: Path,
    run_id: str,
    task_id: str | None,
    agent_id: str,
    message: str,
    status: str = "completed",
    exit_code: int | None = 0,
) -> dict:
    if not task_id:
        return {"handled": False}
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return {"handled": False}
    if task.get("status") in TERMINAL_AGENT_STATUSES:
        append_event(root, run_id, "sub_agent_report_ignored", {"task_id": task_id, "agent_id": agent_id, "reason": "task already terminal"})
        return {"handled": True, "ignored": True}
    current = next((agent for agent in sub_agents(task) if agent.get("id") == agent_id), None)
    if current is None:
        return {"handled": False}
    if current.get("status") in TERMINAL_AGENT_STATUSES:
        append_event(root, run_id, "sub_agent_report_ignored", {"task_id": task_id, "agent_id": agent_id, "reason": "agent already terminal"})
        request_round_summary_if_ready(root, run_id, task_id)
        return {"handled": True, "ignored": True}
    set_agent_status(root, run_id, task_id, agent_id, status, exit_code)
    append_event(root, run_id, "sub_agent_reported", {"task_id": task_id, "agent_id": agent_id, "status": status, "chars": len(message)})
    round_summary_requested = request_round_summary_if_ready(root, run_id, task_id)
    return {"handled": True, "round_summary_requested": round_summary_requested, "final_requested": False}


def monitor_task_coordination(root: Path, run_id: str) -> list[dict]:
    actions: list[dict] = []
    snapshot = status_snapshot(root, run_id)
    for task in snapshot.get("tasks", []):
        if task.get("status") in TERMINAL_AGENT_STATUSES:
            continue
        agents = sub_agents(task)
        if not agents:
            continue
        if pending_sub_agents(task) and task.get("status") != "running":
            set_task_status(root, run_id, task["id"], "running")
            actions.append({"type": "task_running", "task_id": task["id"]})
        for agent in pending_sub_agents(task):
            if agent.get("backend") != "codex":
                continue
            state = backend_status(root, run_id, agent["id"], task_id=task["id"])
            if state.get("status") != "stopped":
                continue
            attempts = int(agent.get("recovery_attempts") or 0)
            if attempts >= WATCHDOG_MAX_RECOVERY_ATTEMPTS:
                set_agent_status(root, run_id, task["id"], agent["id"], "failed", 1)
                append_event(
                    root,
                    run_id,
                    "sub_agent_backend_failed",
                    {"task_id": task["id"], "agent_id": agent["id"], "attempts": attempts},
                )
                actions.append({"type": "agent_failed", "task_id": task["id"], "agent_id": agent["id"]})
                continue
            start_backend(
                root,
                run_id,
                agent["id"],
                model=agent.get("model"),
                sandbox=agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
                approval=agent.get("approval") or task.get("preferred_approval") or "never",
                from_start=False,
                task_id=task["id"],
            )
            update_agent_runtime(
                root,
                run_id,
                task["id"],
                agent["id"],
                recovery_attempts=attempts + 1,
                last_recovery_at=utc_now(),
            )
            append_event(
                root,
                run_id,
                "sub_agent_backend_recovered",
                {"task_id": task["id"], "agent_id": agent["id"], "attempt": attempts + 1},
            )
            actions.append({"type": "agent_recovered", "task_id": task["id"], "agent_id": agent["id"]})
        fresh_task = task_snapshot(root, run_id, task["id"])["task"]
        if not pending_sub_agents(fresh_task) and request_round_summary_if_ready(root, run_id, task["id"]):
            actions.append({"type": "round_summary_requested", "task_id": task["id"]})
    return actions
