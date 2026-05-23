from __future__ import annotations

import json
import re
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.backend_runtime import PROCESS_AGENT_BACKENDS, backend_status, start_backend
from aha_cli.services.commit_policy import commit_message_policy_prompt
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.filesystem import (
    add_agent,
    append_event,
    append_message,
    append_task_round,
    ensure_session,
    inbox_path,
    mark_task_coordination,
    save_session,
    set_agent_status,
    set_task_status,
    status_snapshot,
    task_snapshot,
    update_agent_runtime,
    run_dir,
    write_json,
)

TERMINAL_AGENT_STATUSES = {"completed", "failed", "blocked", "interrupted", "stopped"}
REUSABLE_SUB_AGENT_STATUSES = ("interrupted", "failed", "completed", "stopped", "blocked")
WATCHDOG_MAX_RECOVERY_ATTEMPTS = 3
AHA_ACTION_TYPES = {"route_to_agent", "spawn_sub", "record_task_update"}
SUPERVISION_STUB_DECISION = "ask_user"
COLLABORATION_GUIDANCE = {
    "auto": (
        "Auto: spawn sub-agents only when expected parallel speedup is greater than startup, "
        "coordination, and merge cost. Stay solo for small or tightly coupled work."
    ),
    "solo": "Solo: do not spawn sub-agents. Handle the work directly as task-main.",
    "pair": (
        "Pair: use at most one sub-agent for a genuinely parallel responsibility, such as "
        "implementation, research, or review. Keep task-main as lead and merger."
    ),
    "team": (
        "Team: use up to two sub-agents for parallel responsibility areas. Prefer disjoint "
        "scopes such as coder plus viewer/verifier while task-main leads and merges."
    ),
}


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
    collaboration_mode = str(task.get("collaboration_mode") or "auto")
    return render_prompt_template(
        "task_assignment.md",
        task_title=task.get("title", ""),
        task_description=task.get("description", ""),
        workspace_path=task.get("workspace_path") or "(not set)",
        collaboration_mode=collaboration_mode,
        collaboration_guidance=COLLABORATION_GUIDANCE.get(collaboration_mode, COLLABORATION_GUIDANCE["auto"]),
        delegation_policy=task.get("delegation_policy", "auto"),
        max_sub_agents=task.get("max_sub_agents", 0),
        preferred_sub_backend=task.get("preferred_sub_backend") or task.get("preferred_backend") or "codex",
        sandbox=task.get("preferred_sandbox") or "process default",
        approval=task.get("preferred_approval") or "process default",
        commit_policy=commit_message_policy_prompt(str(task.get("id") or "<task-id>"), "<agent-id>").rstrip(),
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
        reply_target="browser",
    )
    append_event(root, run_id, "task_dispatched", {"task_id": task["id"], "target": "main"})
    return payload


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


def chat_offset_exists(root: Path, run_id: str, target: str, task_id: str | None = None) -> bool:
    safe_target = safe_chat_key(target)
    if task_id:
        safe_task = safe_chat_key(task_id)
        return (run_dir(root, run_id) / "runtime" / f"chat-offset-{safe_task}-{safe_target}.json").exists()
    return (run_dir(root, run_id) / "runtime" / f"chat-offset-{safe_target}.json").exists()


def safe_chat_key(value: str) -> str:
    return (value or "main").replace("/", "_")


def chat_offset_path(root: Path, run_id: str, target: str, task_id: str | None = None) -> Path:
    safe_target = safe_chat_key(target)
    if task_id:
        return run_dir(root, run_id) / "runtime" / f"chat-offset-{safe_chat_key(task_id)}-{safe_target}.json"
    return run_dir(root, run_id) / "runtime" / f"chat-offset-{safe_target}.json"


def save_chat_offset(root: Path, run_id: str, target: str, task_id: str | None = None) -> None:
    inbox = inbox_path(root, run_id, target)
    offset = inbox.stat().st_size if inbox.exists() else 0
    write_json(chat_offset_path(root, run_id, target, task_id), {"offset": offset, "updated_at": utc_now()})


def find_sub_agent(task: dict, agent_id: str) -> dict | None:
    return next((agent for agent in sub_agents(task) if agent.get("id") == agent_id), None)


def active_sub_agent_count(task: dict) -> int:
    return sum(1 for agent in sub_agents(task) if agent.get("status") not in TERMINAL_AGENT_STATUSES)


def reusable_sub_agent(task: dict, exclude_ids: set[str] | None = None) -> dict | None:
    excluded = exclude_ids or set()
    for status in REUSABLE_SUB_AGENT_STATUSES:
        for agent in sub_agents(task):
            if agent.get("id") not in excluded and agent.get("status") == status:
                return agent
    return None


def explicit_scope_id(action: dict) -> str:
    return str(action.get("scope_id") or action.get("scope") or "").strip()


def next_generation(agent: dict | None) -> int:
    if not agent:
        return 1
    try:
        return max(1, int(agent.get("generation") or 0) + 1)
    except (TypeError, ValueError):
        return 1


def assignment_id_for(agent_id: str, generation: int) -> str:
    return f"{agent_id}:gen-{generation:03d}"


def scope_id_for(action: dict, assignment_id: str) -> tuple[str, bool]:
    scope_id = explicit_scope_id(action)
    return (scope_id, True) if scope_id else (assignment_id, False)


def reset_backend_session_for_fresh_scope(
    root: Path,
    run_id: str,
    task_id: str,
    task: dict,
    agent: dict,
    *,
    assignment_id: str,
    backend: str,
    scope_id: str,
    reason: str,
) -> dict:
    agent_id = str(agent.get("id") or "")
    session = ensure_session(
        root,
        run_id,
        task_id,
        agent_id,
        backend,
        model=agent.get("model") or task.get("preferred_model"),
        workspace_path=agent.get("workspace_path") or task.get("workspace_path"),
    )
    old_backend_session_id = session.get("backend_session_id")
    reset_at = utc_now()
    history = session.get("history_backend_sessions")
    if not isinstance(history, list):
        history = []
    archive = None
    if old_backend_session_id:
        archive = {
            "backend_session_id": old_backend_session_id,
            "backend": session.get("backend"),
            "model": session.get("model"),
            "started_at": session.get("created_at"),
            "ended_at": reset_at,
            "reason": "fresh_scope_reuse",
            "assignment_id": assignment_id,
            "scope_id": scope_id,
            "detail": reason,
        }
        history.append(archive)
    session["backend"] = backend
    session["history_backend_sessions"] = history
    session["backend_session_id"] = None
    session["status"] = "reset"
    session["updated_at"] = reset_at
    session["compact_summary"] = None
    save_session(root, session)
    append_event(
        root,
        run_id,
        "backend_session_reset",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "old_backend_session_id": old_backend_session_id,
            "assignment_id": assignment_id,
            "scope_id": scope_id,
            "reason": "fresh_scope_reuse",
            "archived": bool(archive),
        },
    )
    return session


def spawn_sub_skipped_message(reason: str, max_sub_agents: int, target_agent_id: str = "") -> str:
    if reason == "delegation disabled":
        return "AHA 没有创建新的 sub-agent：当前任务已禁用 delegation。"
    if reason == "target sub-agent not found":
        return f"AHA 没有分配 sub-agent：指定的 agent `{target_agent_id}` 不存在或不是 sub-agent。"
    if reason == "target sub-agent busy":
        return f"AHA 没有分配 sub-agent：指定的 agent `{target_agent_id}` 当前仍在工作，不能覆盖它的任务。"
    if reason == "target sub-agent already used":
        return f"AHA 没有分配 sub-agent：指定的 agent `{target_agent_id}` 在本轮已经接收过一个新任务。"
    return (
        "AHA 没有创建新的 sub-agent："
        f"当前活跃 sub-agent 已达到 max_sub_agents={max_sub_agents}。"
        "请先等待已有 sub-agent 完成，或提高 max_sub_agents。"
    )


def append_spawn_sub_skipped(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    reason: str,
    max_sub_agents: int,
    target_agent_id: str = "",
) -> None:
    append_event(
        root,
        run_id,
        "action_skipped",
        {
            "task_id": task_id,
            "type": "spawn_sub",
            "reason": reason,
            "max_sub_agents": max_sub_agents,
            "target_agent_id": target_agent_id,
        },
    )
    append_message(
        root,
        run_id,
        "browser",
        spawn_sub_skipped_message(reason, max_sub_agents, target_agent_id),
        sender="aha",
        task_id=task_id,
        role="main",
        from_agent="aha",
        to_agent="browser",
        coordination="action_skipped",
    )


def dispatch_spawn_to_existing_sub_agent(
    root: Path,
    run_id: str,
    task_id: str,
    task: dict,
    action: dict,
    agent: dict,
    assignment: str,
    *,
    reason: str,
) -> dict:
    previous_status = str(agent.get("status") or "")
    agent_id = str(agent.get("id") or "")
    previous_scope_id = str(agent.get("scope_id") or "")
    generation = next_generation(agent)
    assignment_id = assignment_id_for(agent_id, generation)
    scope_id, scope_explicit = scope_id_for(action, assignment_id)
    same_scope = scope_explicit and scope_id == previous_scope_id
    set_agent_status(root, run_id, task_id, agent_id, "pending")
    backend = str(action.get("backend") or agent.get("backend") or task.get("preferred_sub_backend") or task.get("preferred_backend") or "codex")
    runtime_fields = {
        "assignment": assignment,
        "assignment_id": assignment_id,
        "scope_id": scope_id,
        "scope_explicit": scope_explicit,
        "generation": generation,
        "backend": backend,
        "model": action.get("model") if action.get("model") is not None else agent.get("model"),
        "sandbox": action.get("sandbox") if action.get("sandbox") is not None else agent.get("sandbox") or task.get("preferred_sandbox"),
        "approval": action.get("approval") if action.get("approval") is not None else agent.get("approval") or task.get("preferred_approval"),
        "created_by": "main",
        "created_reason": str(action.get("reason") or action.get("title") or "main requested sub-agent recovery"),
        "recovery_attempts": 0,
        "last_recovery_at": "",
        "reused_at": utc_now(),
        "reused_from_status": previous_status,
        "previous_assignment_id": agent.get("assignment_id") or "",
        "previous_scope_id": previous_scope_id,
    }
    if not same_scope:
        reset_backend_session_for_fresh_scope(
            root,
            run_id,
            task_id,
            task,
            agent,
            assignment_id=assignment_id,
            backend=backend,
            scope_id=scope_id,
            reason=reason,
        )
        runtime_fields.update(
            {
                "session_id": None,
                "backend_session_id": None,
                "last_usage": None,
                "recovery_context": "",
                "recovery_context_reason": "",
                "recovery_context_at": "",
                "recovery_context_consumed_at": "",
            }
        )
    updated_agent = update_agent_runtime(
        root,
        run_id,
        task_id,
        agent_id,
        **runtime_fields,
    )
    save_chat_offset(root, run_id, agent_id, task_id)
    append_message(
        root,
        run_id,
        agent_id,
        assignment,
        sender="main",
        task_id=task_id,
        role="sub",
        from_agent="main",
        to_agent=agent_id,
    )
    append_event(
        root,
        run_id,
        "sub_agent_reused",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "previous_status": previous_status,
            "previous_scope_id": previous_scope_id,
            "assignment_id": assignment_id,
            "scope_id": scope_id,
            "generation": generation,
            "same_scope": same_scope,
            "reason": reason,
        },
    )
    if backend in PROCESS_AGENT_BACKENDS:
        start_backend(
            root,
            run_id,
            agent_id,
            backend=backend,
            model=updated_agent.get("model"),
            sandbox=updated_agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
            approval=updated_agent.get("approval") or task.get("preferred_approval") or "never",
            from_start=False,
            task_id=task_id,
        )
    return updated_agent


def apply_supervision_stub(
    root: Path,
    run_id: str,
    task_id: str | None,
    *,
    source_agent: str,
    reply_text: str,
) -> dict | None:
    if not task_id or source_agent != "main":
        return None
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return None
    supervision = task.get("supervision") if isinstance(task.get("supervision"), dict) else {}
    if (
        supervision.get("mode") != "assisted"
        or supervision.get("host_backend") != "stub"
        or supervision.get("real_agent_enabled")
    ):
        return None
    append_event(
        root,
        run_id,
        "main_reported_to_host",
        {
            "task_id": task_id,
            "host_backend": "stub",
            "host_agent_id": supervision.get("host_agent_id"),
            "channel": supervision.get("channel") or "main_only",
            "reply_chars": len(reply_text),
        },
    )
    decision = SUPERVISION_STUB_DECISION
    result = {
        "task_id": task_id,
        "host_backend": "stub",
        "decision": decision,
        "reason": "stub supervision only records the main->host->main decision path; no real host agent is attached",
    }
    append_event(root, run_id, "host_decision", result)
    append_event(
        root,
        run_id,
        "main_applied_decision",
        {
            "task_id": task_id,
            "decision": decision,
            "applied": decision == "ask_user",
            "effect": "await_user" if decision == "ask_user" else "noop",
            "reason": "real host agent disabled; main keeps user-facing control",
        },
    )
    return result


def execute_actions(root: Path, run_id: str, task_id: str | None, text: str) -> list[dict]:
    if not task_id:
        return []
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return []
    payload = extract_action_payload(text)
    if not payload:
        return []
    invalid_reason = invalid_action_schema_reason(payload)
    if invalid_reason:
        append_event(root, run_id, "invalid_action_schema", {"task_id": task_id, "reason": invalid_reason})
        return []
    executed: list[dict] = []
    used_sub_agent_ids: set[str] = set()
    for action in payload.get("actions", []):
        if not isinstance(action, dict):
            continue
        try:
            task = task_snapshot(root, run_id, task_id)["task"]
        except KeyError:
            return executed
        max_sub_agents = int(task.get("max_sub_agents", 0) or 0)
        current_active_sub_agents = active_sub_agent_count(task)
        action_type = action.get("type")
        if action_type == "record_task_update":
            summary = str(action.get("summary") or "").strip()
            if not summary:
                append_event(
                    root,
                    run_id,
                    "action_skipped",
                    {"task_id": task_id, "type": "record_task_update", "reason": "missing summary"},
                )
                continue
            record = append_task_round(
                root,
                run_id,
                task_id,
                {
                    "trigger": str(action.get("trigger") or "main_turn"),
                    "summary": summary,
                    "changed_files": action.get("changed_files") or action.get("files"),
                    "verification": action.get("verification") or action.get("checks"),
                    "risks": action.get("risks"),
                    "agents": action.get("agents") or ["main"],
                },
            )
            executed.append({"type": "record_task_update", "round_id": record["round_id"]})
            continue
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
            backend = str(target_agent.get("backend") or task.get("preferred_backend") or "codex")
            if backend in PROCESS_AGENT_BACKENDS:
                start_backend(
                    root,
                    run_id,
                    target_id,
                    backend=backend,
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
        if task.get("delegation_policy") == "disabled":
            append_spawn_sub_skipped(root, run_id, task_id, reason="delegation disabled", max_sub_agents=max_sub_agents)
            continue
        assignment = str(action.get("title") or action.get("prompt") or "Assist task-main with this task.")
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
        requested_agent_id = str(action.get("agent_id") or action.get("target") or "").strip()
        if requested_agent_id:
            reusable_agent = find_sub_agent(task, requested_agent_id)
            if reusable_agent is None:
                append_spawn_sub_skipped(
                    root,
                    run_id,
                    task_id,
                    reason="target sub-agent not found",
                    max_sub_agents=max_sub_agents,
                    target_agent_id=requested_agent_id,
                )
                continue
            if requested_agent_id in used_sub_agent_ids:
                append_spawn_sub_skipped(
                    root,
                    run_id,
                    task_id,
                    reason="target sub-agent already used",
                    max_sub_agents=max_sub_agents,
                    target_agent_id=requested_agent_id,
                )
                continue
            if reusable_agent.get("status") not in TERMINAL_AGENT_STATUSES:
                append_spawn_sub_skipped(
                    root,
                    run_id,
                    task_id,
                    reason="target sub-agent busy",
                    max_sub_agents=max_sub_agents,
                    target_agent_id=requested_agent_id,
                )
                continue
            if current_active_sub_agents >= max_sub_agents:
                append_spawn_sub_skipped(root, run_id, task_id, reason="max_sub_agents reached", max_sub_agents=max_sub_agents)
                continue
            agent = dispatch_spawn_to_existing_sub_agent(
                root,
                run_id,
                task_id,
                task,
                action,
                reusable_agent,
                assignment,
                reason="spawn_sub assigned to requested sub-agent",
            )
            used_sub_agent_ids.add(requested_agent_id)
            executed.append({"type": "spawn_sub", "agent": agent, "reused": True, "requested_agent_id": requested_agent_id})
            continue
        if current_active_sub_agents >= max_sub_agents:
            append_spawn_sub_skipped(root, run_id, task_id, reason="max_sub_agents reached", max_sub_agents=max_sub_agents)
            continue
        reusable_agent = reusable_sub_agent(task, used_sub_agent_ids) if max_sub_agents > 0 else None
        if reusable_agent is not None:
            agent_id = str(reusable_agent.get("id") or "")
            agent = dispatch_spawn_to_existing_sub_agent(
                root,
                run_id,
                task_id,
                task,
                action,
                reusable_agent,
                assignment,
                reason="spawn_sub reused idle sub-agent slot",
            )
            used_sub_agent_ids.add(agent_id)
            executed.append({"type": "spawn_sub", "agent": agent, "reused": True})
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
        generation = 1
        assignment_id = assignment_id_for(agent["id"], generation)
        scope_id, scope_explicit = scope_id_for(action, assignment_id)
        agent = update_agent_runtime(
            root,
            run_id,
            task_id,
            agent["id"],
            assignment=assignment,
            assignment_id=assignment_id,
            scope_id=scope_id,
            scope_explicit=scope_explicit,
            generation=generation,
        )
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
        backend = str(agent.get("backend") or task.get("preferred_backend") or "codex")
        if backend in PROCESS_AGENT_BACKENDS:
            start_backend(
                root,
                run_id,
                agent["id"],
                backend=backend,
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
    prompt = render_prompt_template("task_round_summary.md", task_id=task_id)
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
            backend = str(agent.get("backend") or task.get("preferred_backend") or "codex")
            if backend not in PROCESS_AGENT_BACKENDS:
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
                backend=backend,
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
