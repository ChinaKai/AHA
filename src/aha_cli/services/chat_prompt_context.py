from __future__ import annotations

from pathlib import Path

from aha_cli.services.chat_supervision import (
    agents_visible_to_prompt,
    is_task_supervision_host_agent,
    prompt_event_visible_to_target,
    supervision_host_context,
    supervision_host_handoff_notes,
    supervision_host_notes,
)
from aha_cli.services.commit_policy import commit_message_policy_prompt
from aha_cli.services.messages import format_event
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.event_views import event_agent_refs
from aha_cli.store.filesystem import (
    event_path,
    iter_jsonl_reverse,
    require_plan,
    run_dir,
    status_snapshot,
    task_snapshot,
)


PROMPT_REDACTED_PROXY_FIELDS = {"preferred_http_proxy", "preferred_https_proxy", "preferred_no_proxy"}
DELTA_PROMPT_SKIP_EVENT_TYPES = {
    "agent_command_finished",
    "agent_command_started",
    "agent_finished",
    "agent_message",
    "agent_prompt_metrics",
    "agent_started",
    "agent_status_changed",
    "agent_thread",
    "agent_usage",
    "task_journal_rendered",
    "task_status_changed",
}


def recent_run_events(root: Path, run_id: str, limit: int) -> list[dict]:
    events: list[dict] = []
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        events.append(event)
        if len(events) >= limit:
            break
    return list(reversed(events))


def _event_task_id(event: dict) -> str | None:
    data = event.get("data")
    if isinstance(data, dict):
        task_id = data.get("task_id")
        if task_id:
            return str(task_id)
    return None


def _prompt_visibility_task(root: Path, run_id: str, task_id: str | None, target: str | None) -> dict | None:
    if not task_id or not target:
        return None
    try:
        tasks = status_snapshot(root, run_id).get("tasks", [])
    except (OSError, ValueError):
        return None
    if not isinstance(tasks, list):
        return None
    return next((task for task in tasks if task.get("id") == task_id), None)


def recent_prompt_events(root: Path, run_id: str, limit: int, task_id: str | None, target: str | None = None) -> list[dict]:
    if not task_id:
        return recent_run_events(root, run_id, limit)
    events: list[dict] = []
    visibility_task = _prompt_visibility_task(root, run_id, task_id, target)
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        if _event_task_id(event) != task_id:
            continue
        if target and target not in event_agent_refs(event):
            continue
        if target and not prompt_event_visible_to_target(event, target, visibility_task):
            continue
        events.append(event)
        if len(events) >= limit:
            break
    return list(reversed(events))


def _is_current_message_event(event: dict, item: dict, target: str) -> bool:
    data = event.get("data")
    if event.get("type") != "message" or not isinstance(data, dict):
        return False
    return (
        data.get("target") == target
        and data.get("sender") == item.get("sender")
        and data.get("message") == item.get("message")
        and data.get("ts") == item.get("ts")
    )


def recent_delta_prompt_events(root: Path, run_id: str, limit: int, task_id: str | None, target: str, item: dict) -> list[dict]:
    events: list[dict] = []
    visibility_task = _prompt_visibility_task(root, run_id, task_id, target)
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        if task_id and _event_task_id(event) != task_id:
            continue
        data = event.get("data")
        if event.get("type") == "agent_finished" and isinstance(data, dict) and data.get("target") == target:
            break
        if target not in event_agent_refs(event):
            continue
        if not prompt_event_visible_to_target(event, target, visibility_task):
            continue
        if event.get("type") in DELTA_PROMPT_SKIP_EVENT_TYPES:
            continue
        if _is_current_message_event(event, item, target):
            continue
        events.append(event)
        if len(events) >= limit:
            break
    return list(reversed(events))


def redact_proxy_fields_for_prompt(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key in PROMPT_REDACTED_PROXY_FIELDS:
                redacted[key] = "<set>" if item else None
            else:
                redacted[key] = redact_proxy_fields_for_prompt(item)
        return redacted
    if isinstance(value, list):
        return [redact_proxy_fields_for_prompt(item) for item in value]
    return value


def _task_counts_for_prompt(tasks: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _agent_summary_for_prompt(agent: dict) -> dict:
    return {
        "id": agent.get("id"),
        "role": agent.get("role"),
        "backend": agent.get("backend"),
        "model": agent.get("model"),
        "sandbox": agent.get("sandbox"),
        "approval": agent.get("approval"),
        "status": agent.get("status"),
        "session_policy": agent.get("session_policy"),
        "session_id": agent.get("session_id"),
        "backend_session_id": agent.get("backend_session_id"),
        "session_status": agent.get("session_status"),
    }


def _task_summary_for_prompt(task: dict, target: str) -> dict:
    agents = task.get("agents") if isinstance(task.get("agents"), list) else []
    visible_agents = agents_visible_to_prompt(task, target)
    current_agent = next((agent for agent in agents if agent.get("id") == target), None)
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "workspace_path": task.get("workspace_path"),
        "preferred_backend": task.get("preferred_backend"),
        "preferred_model": task.get("preferred_model"),
        "preferred_sandbox": task.get("preferred_sandbox"),
        "preferred_approval": task.get("preferred_approval"),
        "preferred_proxy_enabled": bool(task.get("preferred_proxy_enabled")),
        "preferred_http_proxy": task.get("preferred_http_proxy"),
        "preferred_https_proxy": task.get("preferred_https_proxy"),
        "preferred_no_proxy": task.get("preferred_no_proxy"),
        "collaboration_mode": task.get("collaboration_mode"),
        "delegation_policy": task.get("delegation_policy"),
        "max_sub_agents": task.get("max_sub_agents"),
        "status": task.get("status"),
        "exit_code": task.get("exit_code"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "current_round_id": task.get("current_round_id"),
        "round_sequence": task.get("round_sequence"),
        "last_final_round_id": task.get("last_final_round_id"),
        "last_final_at": task.get("last_final_at"),
        "coordination": task.get("coordination"),
        "hidden": bool(task.get("hidden")),
        "current_agent": _agent_summary_for_prompt(current_agent) if current_agent else None,
        "agents_summary": [_agent_summary_for_prompt(agent) for agent in visible_agents],
    }


def prompt_status_snapshot(root: Path, run_id: str, task_id: str | None, target: str) -> dict:
    snapshot = status_snapshot(root, run_id)
    tasks = snapshot.get("tasks") if isinstance(snapshot.get("tasks"), list) else []
    current_task = next((task for task in tasks if task.get("id") == task_id), None) if task_id else None
    compact = {
        "run_id": snapshot.get("run_id"),
        "goal": snapshot.get("goal"),
        "mode": snapshot.get("mode"),
        "updated_at": snapshot.get("updated_at"),
        "aha_root": snapshot.get("aha_root"),
        "main_agent": snapshot.get("main_agent"),
        "task_counts": _task_counts_for_prompt(tasks),
        "task_total": len(tasks),
        "hidden_task_count": sum(1 for task in tasks if task.get("hidden")),
        "current_task": _task_summary_for_prompt(current_task, target) if current_task else None,
    }
    return redact_proxy_fields_for_prompt(compact)


def prompt_delta_status_snapshot(root: Path, run_id: str, task_id: str | None, target: str) -> dict:
    snapshot = status_snapshot(root, run_id)
    tasks = snapshot.get("tasks") if isinstance(snapshot.get("tasks"), list) else []
    current_task = next((task for task in tasks if task.get("id") == task_id), None) if task_id else None
    current_agent = None
    if current_task:
        agents = current_task.get("agents") if isinstance(current_task.get("agents"), list) else []
        current_agent = next((agent for agent in agents if agent.get("id") == target), None)
    compact_task = None
    if current_task:
        compact_task = {
            "id": current_task.get("id"),
            "title": current_task.get("title"),
            "status": current_task.get("status"),
            "current_round_id": current_task.get("current_round_id"),
            "round_sequence": current_task.get("round_sequence"),
            "last_final_round_id": current_task.get("last_final_round_id"),
            "last_final_at": current_task.get("last_final_at"),
            "hidden": bool(current_task.get("hidden")),
            "current_agent": _agent_summary_for_prompt(current_agent) if current_agent else None,
        }
    compact = {
        "run_id": snapshot.get("run_id"),
        "mode": snapshot.get("mode"),
        "updated_at": snapshot.get("updated_at"),
        "task_counts": _task_counts_for_prompt(tasks),
        "task_total": len(tasks),
        "hidden_task_count": sum(1 for task in tasks if task.get("hidden")),
        "current_task": compact_task,
    }
    return redact_proxy_fields_for_prompt(compact)


def _text_metrics(value) -> dict:
    text = "" if value is None else str(value)
    return {
        "chars": len(text),
        "bytes": len(text.encode("utf-8")),
        "lines": text.count("\n") + 1 if text else 0,
    }


def _fill_prompt_metrics(
    metrics: dict | None,
    prompt: str,
    *,
    target: str,
    item: dict,
    components: dict,
    is_finalization: bool,
    is_agent_command: bool,
    event_limit: int | None = None,
    prompt_mode: str = "full",
) -> None:
    if metrics is None:
        return
    metrics.clear()
    metrics.update(
        {
            "target": target,
            "task_id": item.get("task_id"),
            "sender": item.get("sender"),
            "is_finalization": is_finalization,
            "is_agent_command": is_agent_command,
            "prompt_mode": prompt_mode,
            "total": _text_metrics(prompt),
            "components": {name: _text_metrics(value) for name, value in components.items()},
        }
    )
    if event_limit is not None:
        metrics["event_limit"] = event_limit


def chat_prompt_with_metrics(root: Path, run_id: str, target: str, item: dict, prefix: str) -> tuple[str, dict]:
    metrics: dict = {}
    prompt = chat_prompt(root, run_id, target, item, prefix, metrics=metrics)
    return prompt, metrics


def compact_summary_context(root: Path, run_id: str, session: dict | None) -> str:
    summary_meta = session.get("compact_summary") if isinstance(session, dict) else None
    if not isinstance(summary_meta, dict):
        return ""
    relpath = str(summary_meta.get("path") or "").strip()
    if not relpath:
        return ""
    path = run_dir(root, run_id) / relpath
    if not path.exists():
        return f"Backend compact summary: `{relpath}` was referenced but not found.\n"
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return ""
    return f"Backend compact summary from previous session:\n{text}\n"


def chat_prompt(root: Path, run_id: str, target: str, item: dict, prefix: str, *, metrics: dict | None = None) -> str:
    plan = require_plan(root, run_id)
    task_id = item.get("task_id")
    is_finalization = item.get("result_policy") == "finalize"
    is_agent_command = item.get("command_namespace") == "agent"
    task = None
    agent = None
    sticky_delta = False
    components: dict = {
        "prefix": prefix,
        "run_goal": plan.get("goal", ""),
        "user_message": item.get("message", ""),
    }
    task_context = ""
    if task_id:
        try:
            detail = task_snapshot(root, run_id, str(task_id))
            task = detail["task"]
            agent = next((entry for entry in task.get("agents", []) if entry.get("id") == target), None)
            session = next((entry for entry in detail.get("sessions", []) if entry.get("agent_id") == target), None)
            if session:
                merged_agent = dict(agent or {})
                merged_agent["session_id"] = session.get("id")
                merged_agent["backend_session_id"] = session.get("backend_session_id")
                merged_agent["session_status"] = session.get("status")
                agent = merged_agent
            sticky_delta = bool(
                not is_finalization
                and (agent or {}).get("session_policy") == "sticky"
                and (agent or {}).get("backend_session_id")
            )
            if is_agent_command:
                command = str(item.get("message", "") or "")
                original_command = str(item.get("original_command", "") or "")
                agent_metadata = render_prompt_template(
                    "backend_agent_metadata.md",
                    task_id=task_id,
                    task_title=task.get("title", ""),
                    agent_id=target,
                    role=(agent or {}).get("role") or item.get("role", ""),
                    backend=(agent or {}).get("backend") or task.get("preferred_backend") or "codex",
                    model=(agent or {}).get("model") or task.get("preferred_model") or "default",
                    workspace=(agent or {}).get("workspace_path") or task.get("workspace_path") or "-",
                    sandbox=(agent or {}).get("sandbox") or task.get("preferred_sandbox") or "-",
                    approval=(agent or {}).get("approval") or task.get("preferred_approval") or "-",
                ).rstrip()
                components.update(
                    {
                        "agent_command": command,
                        "original_agent_command": original_command,
                        "agent_metadata": agent_metadata,
                    }
                )
                prompt = render_prompt_template(
                    "backend_agent_command.md",
                    prefix=prefix,
                    target=target,
                    original_command=original_command or command,
                    command=command,
                    agent_metadata=agent_metadata,
                )
                _fill_prompt_metrics(
                    metrics,
                    prompt,
                    target=target,
                    item=item,
                    components=components,
                    is_finalization=is_finalization,
                    is_agent_command=is_agent_command,
                )
                return prompt
            existing_final = detail.get("result", "").strip()
            final_context = ""
            if is_finalization and existing_final:
                final_context = f"- existing Final chars: {len(existing_final)}\n"
            compact_context = compact_summary_context(root, run_id, session)
            rounds = detail.get("rounds", [])
            journal_context = ""
            if rounds:
                recent_rounds = rounds[-10:]
                journal_lines = ["Task journal:"]
                for round_item in recent_rounds:
                    journal_lines.append(
                        f"- {round_item.get('round_id')} [{round_item.get('trigger')}] {round_item.get('summary')}"
                    )
                journal_context = "\n".join(journal_lines)
            commit_policy = commit_message_policy_prompt(
                task_id,
                target,
                backend=(session or {}).get("backend") or (agent or {}).get("backend") or task.get("preferred_backend"),
                model=(
                    (session or {}).get("resolved_model")
                    or (session or {}).get("model")
                    or (agent or {}).get("model")
                    or task.get("preferred_model")
                ),
            ).rstrip()
            visible_agents = agents_visible_to_prompt(detail["task"], target)
            components.update(
                {
                    "task_agents": visible_agents,
                    "task_journal": journal_context,
                    "commit_policy": commit_policy,
                    "compact_summary": compact_context,
                }
            )
            task_context = render_prompt_template(
                "backend_task_context.md",
                task_id=task_id,
                title=detail["task"].get("title", ""),
                description=detail["task"].get("description", ""),
                status=detail["task"].get("status", ""),
                role=item.get("role", ""),
                collaboration_mode=detail["task"].get("collaboration_mode", "auto"),
                delegation_policy=detail["task"].get("delegation_policy", "auto"),
                max_sub_agents=detail["task"].get("max_sub_agents", 0),
                agents=visible_agents,
                final_context=final_context.rstrip(),
                task_journal=journal_context,
                compact_summary=compact_context.rstrip(),
                commit_policy=commit_policy,
            )
            if is_task_supervision_host_agent(detail["task"], target):
                supervision_context = supervision_host_context(
                    detail["task"],
                    supervision_host_notes(root, run_id, str(task_id), target),
                    supervision_host_handoff_notes(root, run_id, str(task_id)),
                )
                task_context = f"{task_context.rstrip()}\n\n{supervision_context}\n"
                components["supervision_host_context"] = supervision_context
            components["task_context"] = task_context
        except KeyError:
            task_context = f"Current task context: task_id={task_id} was referenced but not found.\n"
            components["task_context"] = task_context
    mode_instruction = render_prompt_template(
        "mode_instruction_final.md" if is_finalization else "mode_instruction_default.md"
    ).strip()
    event_limit = 0 if is_finalization else 20
    if sticky_delta:
        event_limit = 8
        events = recent_delta_prompt_events(root, run_id, event_limit, str(task_id) if task_id else None, target, item)
        recent = "\n".join(format_event(event) for event in events) or "(no external AHA events since previous backend turn)"
        status = prompt_delta_status_snapshot(root, run_id, str(task_id) if task_id else None, target)
        sticky_context = render_prompt_template(
            "backend_sticky_context.md",
            task_id=task_id,
            title=(task or {}).get("title", ""),
            status=(task or {}).get("status", ""),
            agent_id=target,
            role=item.get("role", ""),
            backend=(agent or {}).get("backend") or (task or {}).get("preferred_backend") or "codex",
            workspace=(agent or {}).get("workspace_path") or (task or {}).get("workspace_path") or "-",
            collaboration_mode=(task or {}).get("collaboration_mode") or "auto",
            max_sub_agents=(task or {}).get("max_sub_agents") if task else "-",
            sandbox=(agent or {}).get("sandbox") or (task or {}).get("preferred_sandbox") or "-",
            approval=(agent or {}).get("approval") or (task or {}).get("preferred_approval") or "-",
            session_policy=(agent or {}).get("session_policy") or "-",
            backend_session_id=(agent or {}).get("backend_session_id") or "-",
        )
        if task and is_task_supervision_host_agent(task, target):
            supervision_context = supervision_host_context(
                task,
                supervision_host_notes(root, run_id, str(task_id), target),
                supervision_host_handoff_notes(root, run_id, str(task_id)),
            )
            sticky_context = f"{sticky_context.rstrip()}\n\n{supervision_context}\n"
            components["supervision_host_context"] = supervision_context
        for stale_component in ("task_context", "task_agents", "task_journal", "commit_policy"):
            components.pop(stale_component, None)
        components.update(
            {
                "mode_instruction": mode_instruction,
                "delta_status": status,
                "external_events": recent,
                "sticky_context": sticky_context,
            }
        )
        prompt = render_prompt_template(
            "backend_chat_delta.md",
            prefix=prefix,
            target=target,
            mode_instruction=mode_instruction,
            run_goal=plan["goal"],
            status=status,
            sticky_context=sticky_context.rstrip(),
            recent_events=recent,
            sender=item.get("sender", "browser"),
            ts=item.get("ts", ""),
            message=item.get("message", ""),
        )
        _fill_prompt_metrics(
            metrics,
            prompt,
            target=target,
            item=item,
            components=components,
            is_finalization=is_finalization,
            is_agent_command=is_agent_command,
            event_limit=event_limit,
            prompt_mode="sticky_delta",
        )
        return prompt
    if is_finalization:
        recent = "(omitted for finalization; use the Task journal, compact summary, and current finalization request)"
    else:
        events = recent_prompt_events(root, run_id, event_limit, str(task_id) if task_id else None, target)
        recent = "\n".join(format_event(event) for event in events) or "(no events)"
    status = prompt_status_snapshot(root, run_id, str(task_id) if task_id else None, target)
    components.update(
        {
            "mode_instruction": mode_instruction,
            "status_snapshot": status,
            "recent_events": recent,
            "task_context": task_context or "Current task context: none",
        }
    )
    prompt = render_prompt_template(
        "backend_chat_full.md",
        prefix=prefix,
        target=target,
        mode_instruction=mode_instruction,
        run_goal=plan["goal"],
        status=status,
        task_context=task_context or "Current task context: none",
        recent_events=recent,
        sender=item.get("sender", "browser"),
        ts=item.get("ts", ""),
        message=item.get("message", ""),
    )
    _fill_prompt_metrics(
        metrics,
        prompt,
        target=target,
        item=item,
        components=components,
        is_finalization=is_finalization,
        is_agent_command=is_agent_command,
        event_limit=event_limit,
    )
    return prompt


__all__ = [
    "chat_prompt",
    "chat_prompt_with_metrics",
    "compact_summary_context",
    "prompt_delta_status_snapshot",
    "prompt_status_snapshot",
    "recent_delta_prompt_events",
    "recent_prompt_events",
    "recent_run_events",
    "redact_proxy_fields_for_prompt",
]
