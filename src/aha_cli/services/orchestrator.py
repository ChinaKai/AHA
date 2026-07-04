from __future__ import annotations

from pathlib import Path

from aha_cli.backends.registry import normalize_model_selector
from aha_cli.domain.models import utc_now
from aha_cli.services.action_payloads import (
    AHA_ACTION_TYPES,
    action_response_text,
    extract_action_payload,
    extract_action_payload_result,
    invalid_action_schema_message,
    invalid_action_schema_reason,
)
from aha_cli.services.auto_context_compact import start_backend_after_auto_compact as start_backend
from aha_cli.services.backend_runtime import PROCESS_AGENT_BACKENDS, backend_status
from aha_cli.services.commit_policy import commit_message_policy_prompt
from aha_cli.services.hardware_debug import hardware_debug_context_for_prompt
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.services.routing import (
    route_to_agent_request,
    route_to_agent_result,
    route_to_agent_routed_event,
    route_to_agent_skip_event,
)
from aha_cli.services.subagent_state import (
    TERMINAL_AGENT_STATUSES,
    active_sub_agent_count,
    current_round_sub_agents,
    pending_current_round_sub_agents,
    pending_sub_agents,
    sub_agents,
    task_has_incomplete_sub_agents,
    waiting_for_subagents_message,
)
from aha_cli.services.task_skills import task_skills_context_for_prompt
from aha_cli.services.task_updates import handle_record_task_update_action
from aha_cli.store.config import load_config
from aha_cli.store.filesystem import (
    add_agent,
    append_event,
    append_message,
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
from aha_cli.store.sessions import backend_session_usage_archive_fields

REUSABLE_SUB_AGENT_STATUSES = ("interrupted", "failed", "completed", "stopped", "blocked")
WATCHDOG_MAX_RECOVERY_ATTEMPTS = 3
SUPERVISION_STUB_DECISION = "ask_user"
def task_has_active_followup(task: dict) -> bool:
    if task.get("status") in TERMINAL_AGENT_STATUSES:
        return False
    coordination = task.get("coordination") or {}
    return bool(
        coordination.get("followup_started_at")
        and not coordination.get("final_summary_requested_at")
        and not coordination.get("final_summary_completed_at")
    )


def task_assignment_prompt(task: dict, knowledge_context: str = "") -> str:
    return render_prompt_template(
        "task_assignment.md",
        task_title=task.get("title", ""),
        task_description=task.get("description", ""),
        workspace_path=task.get("workspace_path") or "(not set)",
        knowledge_context=(knowledge_context or "").rstrip(),
        collaboration_mode=str(task.get("collaboration_mode") or "auto"),
        workflow_template=str(task.get("workflow_template") or "auto"),
        delegation_policy=task.get("delegation_policy", "auto"),
        max_sub_agents=task.get("max_sub_agents", 0),
        preferred_sub_backend=task.get("preferred_sub_backend") or task.get("preferred_backend") or "codex",
        preferred_sub_model=task.get("preferred_sub_model") or task.get("preferred_model") or "default",
        sandbox=task.get("preferred_sandbox") or "process default",
        approval=task.get("preferred_approval") or "process default",
        task_skills_context=task_skills_context_for_prompt(task).rstrip(),
        hardware_debug_context=hardware_debug_context_for_prompt(task).rstrip(),
        commit_policy=commit_message_policy_prompt(
            str(task.get("id") or "<task-id>"),
            "<agent-id>",
            backend=task.get("preferred_backend"),
            model=task.get("preferred_model"),
        ).rstrip(),
    )


def dispatch_task_to_main(root: Path, run_id: str, task: dict) -> dict:
    # Lazy import avoids a cycle (knowledge_retrieval -> store) at module load.
    from aha_cli.services.knowledge_retrieval import knowledge_context_for_task

    payload = append_message(
        root,
        run_id,
        "main",
        task_assignment_prompt(task, knowledge_context_for_task(root, run_id, task)),
        sender="system",
        task_id=task["id"],
        role="main",
        from_agent="system",
        to_agent="main",
        reply_target="browser",
    )
    append_event(root, run_id, "task_dispatched", {"task_id": task["id"], "target": "main"})
    return payload


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
    model: str | None,
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
        } | backend_session_usage_archive_fields(
            root,
            run_id,
            task_id,
            agent_id,
            backend_session_id=old_backend_session_id,
            backend=session.get("backend"),
            history=history,
        )
        history.append(archive)
    session["backend"] = backend
    session["model"] = model
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
        return (
            f"AHA 没有分配 sub-agent：指定的 agent `{target_agent_id}` 不存在或不是 sub-agent。"
            "新建 sub-agent 时请省略 `agent_id`；只有复用已存在的 sub-agent 时才填写。"
        )
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
    notify_main: bool = False,
) -> None:
    message = spawn_sub_skipped_message(reason, max_sub_agents, target_agent_id)
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
        message,
        sender="aha",
        task_id=task_id,
        role="main",
        from_agent="aha",
        to_agent="browser",
        coordination="action_skipped",
    )
    if notify_main:
        append_message(
            root,
            run_id,
            "main",
            message,
            sender="aha",
            task_id=task_id,
            role="main",
            from_agent="aha",
            to_agent="main",
            reply_target="browser",
            coordination="action_skipped",
        )


def append_visible_sub_agent_result(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    message: str,
    *,
    coordination: str = "sub_agent_result",
) -> None:
    text = str(message or "").strip()
    if not text:
        return
    append_message(
        root,
        run_id,
        "browser",
        text,
        sender=agent_id,
        task_id=task_id,
        role="sub",
        from_agent=agent_id,
        to_agent="main",
        agent_id=agent_id,
        display_sender=agent_id,
        display_target="main",
        coordination=coordination,
    )


def sub_agent_failure_message(agent_id: str, reason: str) -> str:
    if reason == "backend_process_stopped":
        return f"{agent_id} backend 已停止。"
    if reason == "backend_start_failed":
        return f"{agent_id} 后端启动失败。"
    if reason == "backend_recovery_failed":
        return f"{agent_id} backend 多次恢复失败。"
    return f"{agent_id} 执行失败。"


def backend_start_result_failed(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or "").strip().lower()
    return status in {"failed", "error", "stopped"}


def handle_sub_agent_start_failure(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    *,
    reason: str = "backend_start_failed",
    request_summary: bool = True,
) -> None:
    set_agent_status(root, run_id, task_id, agent_id, "failed", 1)
    append_visible_sub_agent_result(
        root,
        run_id,
        task_id,
        agent_id,
        sub_agent_failure_message(agent_id, reason),
        coordination="sub_agent_failed",
    )
    append_event(root, run_id, "sub_agent_backend_failed", {"task_id": task_id, "agent_id": agent_id, "reason": reason})
    if request_summary:
        request_round_summary_if_ready(root, run_id, task_id)


def dispatch_spawn_to_existing_sub_agent(
    root: Path,
    run_id: str,
    task_id: str,
    task: dict,
    action: dict,
    agent: dict,
    assignment: str,
    *,
    config: dict | None = None,
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
    model = action.get("model") if action.get("model") is not None else agent.get("model")
    if not same_scope and action.get("model") is None and task.get("preferred_sub_model") is not None:
        model = task.get("preferred_sub_model")
    model = normalize_model_selector(backend, model, config)
    runtime_fields = {
        "assignment": assignment,
        "assignment_id": assignment_id,
        "scope_id": scope_id,
        "scope_explicit": scope_explicit,
        "generation": generation,
        "backend": backend,
        "model": model,
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
            model=model,
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
        try:
            start_result = start_backend(
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
        except Exception:
            handle_sub_agent_start_failure(root, run_id, task_id, agent_id, request_summary=False)
        else:
            if backend_start_result_failed(start_result):
                handle_sub_agent_start_failure(root, run_id, task_id, agent_id, request_summary=False)
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
    extraction = extract_action_payload_result(text)
    payload = extraction.payload
    if not payload:
        return []
    invalid_reason = invalid_action_schema_reason(payload)
    if invalid_reason:
        append_event(root, run_id, "invalid_action_schema", {"task_id": task_id, "reason": invalid_reason})
        return []
    if extraction.recovered:
        append_event(
            root,
            run_id,
            "action_payload_recovered",
            {
                "task_id": task_id,
                "action_count": len(payload.get("actions", [])) if isinstance(payload.get("actions"), list) else 0,
                "agent_update": extraction.agent_update,
            },
        )
    executed: list[dict] = []
    used_sub_agent_ids: set[str] = set()
    followup_started_at: str | None = None
    config = load_config(root)

    def ensure_followup_round_started() -> None:
        nonlocal followup_started_at
        if followup_started_at is not None:
            return
        followup_started_at = utc_now()
        mark_task_coordination(
            root,
            run_id,
            task_id,
            final_summary_requested_at="",
            final_summary_completed_at="",
            round_summary_requested_at="",
            round_summary_completed_at="",
            followup_started_at=followup_started_at,
        )

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
            result = handle_record_task_update_action(root, run_id, task_id, action)
            if result:
                executed.append(result)
            continue
        if action_type == "route_to_agent":
            route_request = route_to_agent_request(task, action)
            if not route_request.get("ok"):
                append_event(root, run_id, "action_skipped", route_to_agent_skip_event(task_id, route_request))
                continue
            target_id = route_request["target"]
            message = route_request["message"]
            target_agent = route_request["agent"]
            ensure_followup_round_started()
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
            append_event(root, run_id, "agent_message_routed", route_to_agent_routed_event(task_id, route_request))
            backend = str(target_agent.get("backend") or task.get("preferred_backend") or "codex")
            if backend in PROCESS_AGENT_BACKENDS:
                try:
                    start_result = start_backend(
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
                except Exception:
                    handle_sub_agent_start_failure(root, run_id, task_id, target_id, request_summary=False)
                else:
                    if backend_start_result_failed(start_result):
                        handle_sub_agent_start_failure(root, run_id, task_id, target_id, request_summary=False)
            executed.append(route_to_agent_result(route_request))
            continue
        if action_type != "spawn_sub":
            continue
        if task.get("delegation_policy") == "disabled":
            append_spawn_sub_skipped(root, run_id, task_id, reason="delegation disabled", max_sub_agents=max_sub_agents)
            continue
        assignment = str(action.get("title") or action.get("prompt") or "Assist task-main with this task.")
        ensure_followup_round_started()
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
                    notify_main=True,
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
                config=config,
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
                config=config,
                reason="spawn_sub reused idle sub-agent slot",
            )
            used_sub_agent_ids.add(agent_id)
            executed.append({"type": "spawn_sub", "agent": agent, "reused": True})
            continue
        backend = str(action.get("backend") or task.get("preferred_sub_backend") or task.get("preferred_backend") or "codex")
        model = normalize_model_selector(
            backend,
            action.get("model") if action.get("model") is not None else task.get("preferred_sub_model"),
            config,
        )
        agent = add_agent(
            root,
            run_id,
            task_id,
            backend=backend,
            role="sub",
            model=model,
            sandbox=action.get("sandbox") if action.get("sandbox") is not None else task.get("preferred_sandbox"),
            approval=action.get("approval") if action.get("approval") is not None else task.get("preferred_approval"),
            created_by="main",
            created_reason=str(action.get("reason") or action.get("title") or "main requested sub-agent"),
        )
        used_sub_agent_ids.add(str(agent["id"]))
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
            try:
                start_result = start_backend(
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
            except Exception:
                handle_sub_agent_start_failure(root, run_id, task_id, agent["id"], request_summary=False)
            else:
                if backend_start_result_failed(start_result):
                    handle_sub_agent_start_failure(root, run_id, task_id, agent["id"], request_summary=False)
        executed.append({"type": "spawn_sub", "agent": agent})
    if executed:
        try:
            fresh_task = task_snapshot(root, run_id, task_id)["task"]
        except KeyError:
            fresh_task = {}
        if fresh_task and current_round_sub_agents(fresh_task) and not pending_current_round_sub_agents(fresh_task):
            request_round_summary_if_ready(root, run_id, task_id)
        append_event(root, run_id, "actions_executed", {"task_id": task_id, "actions": executed})
    return executed


def start_task_main_backend_if_stopped(root: Path, run_id: str, task_id: str, task: dict) -> bool:
    main_agent = next((agent for agent in task.get("agents", []) if agent.get("id") == "main"), {})
    backend = str(main_agent.get("backend") or task.get("preferred_backend") or "codex")
    if backend not in PROCESS_AGENT_BACKENDS:
        return False
    state = backend_status(root, run_id, "main", task_id=task_id)
    if state.get("status") != "stopped":
        return False
    start_backend(
        root,
        run_id,
        "main",
        backend=backend,
        model=main_agent.get("model"),
        sandbox=main_agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
        approval=main_agent.get("approval") or task.get("preferred_approval") or "never",
        from_start=False,
        task_id=task_id,
    )
    append_event(
        root,
        run_id,
        "main_backend_recovered",
        {"task_id": task_id, "target": "main", "reason": "round_summary"},
    )
    return True


def request_round_summary_if_ready(root: Path, run_id: str, task_id: str) -> bool:
    task = task_snapshot(root, run_id, task_id)["task"]
    agents = current_round_sub_agents(task)
    pending_agents = pending_current_round_sub_agents(task)
    coordination = task.get("coordination") or {}
    if (
        coordination.get("final_summary_requested_at")
        or coordination.get("final_summary_completed_at")
        or coordination.get("round_summary_requested_at")
        or coordination.get("round_summary_completed_at")
    ):
        return False
    if not agents:
        return False
    if pending_agents:
        append_event(
            root,
            run_id,
            "task_waiting_for_subagents",
            {
                "task_id": task_id,
                "pending": [agent.get("id") for agent in pending_agents],
                "completed": [agent.get("id") for agent in agents if agent.get("status") in TERMINAL_AGENT_STATUSES],
            },
        )
        return False
    if task.get("status") in TERMINAL_AGENT_STATUSES:
        return False
    task = mark_task_coordination(root, run_id, task_id, round_summary_requested_at=utc_now())
    prompt = render_prompt_template("task_round_summary.md", task_id=task_id)
    save_chat_offset(root, run_id, "main", task_id)
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
    start_task_main_backend_if_stopped(root, run_id, task_id, task)
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
    if status != "completed" or (exit_code is not None and exit_code != 0):
        append_visible_sub_agent_result(
            root,
            run_id,
            task_id,
            agent_id,
            sub_agent_failure_message(agent_id, "agent_execution_failed"),
            coordination="sub_agent_failed",
        )
    append_event(root, run_id, "sub_agent_reported", {"task_id": task_id, "agent_id": agent_id, "status": status, "chars": len(message)})
    round_summary_requested = request_round_summary_if_ready(root, run_id, task_id)
    return {"handled": True, "round_summary_requested": round_summary_requested, "final_requested": False}


def monitor_task_coordination(root: Path, run_id: str) -> list[dict]:
    actions: list[dict] = []
    snapshot = status_snapshot(root, run_id)
    for task in snapshot.get("tasks", []):
        round_summary_requested_for_task = False
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
                append_visible_sub_agent_result(
                    root,
                    run_id,
                    task["id"],
                    agent["id"],
                    sub_agent_failure_message(agent["id"], "backend_recovery_failed"),
                    coordination="sub_agent_failed",
                )
                append_event(
                    root,
                    run_id,
                    "sub_agent_backend_failed",
                    {"task_id": task["id"], "agent_id": agent["id"], "attempts": attempts},
                )
                round_summary_requested_for_task = request_round_summary_if_ready(root, run_id, task["id"]) or round_summary_requested_for_task
                actions.append({"type": "agent_failed", "task_id": task["id"], "agent_id": agent["id"]})
                continue
            try:
                start_result = start_backend(
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
            except Exception:
                handle_sub_agent_start_failure(root, run_id, task["id"], agent["id"])
                actions.append({"type": "agent_failed", "task_id": task["id"], "agent_id": agent["id"]})
                continue
            if backend_start_result_failed(start_result):
                handle_sub_agent_start_failure(root, run_id, task["id"], agent["id"])
                actions.append({"type": "agent_failed", "task_id": task["id"], "agent_id": agent["id"]})
                continue
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
        if (
            not round_summary_requested_for_task
            and not pending_current_round_sub_agents(fresh_task)
            and request_round_summary_if_ready(root, run_id, task["id"])
        ):
            round_summary_requested_for_task = True
            actions.append({"type": "round_summary_requested", "task_id": task["id"]})
            continue
        coordination = fresh_task.get("coordination") or {}
        if (
            not round_summary_requested_for_task
            and
            coordination.get("round_summary_requested_at")
            and not coordination.get("round_summary_completed_at")
            and not pending_sub_agents(fresh_task)
            and start_task_main_backend_if_stopped(root, run_id, task["id"], fresh_task)
        ):
            actions.append({"type": "main_recovered", "task_id": task["id"]})
    return actions
