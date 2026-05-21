from __future__ import annotations

import os
from pathlib import Path
import time
import uuid

from aha_cli.backends.claude import claude_permission_mode, run_claude_exec
from aha_cli.backends.codex import codex_sandbox, run_codex_exec
from aha_cli.domain.models import utc_now
from aha_cli.services.backend_runtime import mark_backend_stopped, stop_task_backends
from aha_cli.services.commit_policy import commit_message_policy_prompt
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.services.orchestrator import (
    action_response_text,
    execute_actions,
    monitor_task_coordination,
    record_sub_agent_report,
    request_round_summary_if_ready,
    task_has_incomplete_sub_agents,
    waiting_for_subagents_message,
)
from aha_cli.services.proxy import proxy_env_for_agent
from aha_cli.store.filesystem import (
    append_event,
    append_message,
    append_task_round,
    ensure_session,
    event_path,
    inbox_path,
    iter_jsonl_from,
    iter_jsonl_records_from,
    iter_jsonl_reverse,
    load_config,
    mark_task_coordination,
    read_json,
    require_plan,
    run_dir,
    save_session,
    set_agent_status,
    set_task_status,
    status_snapshot,
    task_snapshot,
    write_json,
    write_task_result,
)
from aha_cli.services.messages import format_event


BLOCKED_REPLY_MARKERS = (
    "read-only sandbox",
    "只读沙箱",
    "writing is blocked",
    "写入被拦截",
    "文件没有落盘",
    "permission denied",
    "Permission denied",
    "Read-only file system",
)

TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}
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


def recent_prompt_events(root: Path, run_id: str, limit: int, task_id: str | None) -> list[dict]:
    if not task_id:
        return recent_run_events(root, run_id, limit)
    events: list[dict] = []
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        if _event_task_id(event) != task_id:
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
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        if task_id and _event_task_id(event) != task_id:
            continue
        data = event.get("data")
        if event.get("type") == "agent_finished" and isinstance(data, dict) and data.get("target") == target:
            break
        if event.get("type") in DELTA_PROMPT_SKIP_EVENT_TYPES:
            continue
        if _is_current_message_event(event, item, target):
            continue
        events.append(event)
        if len(events) >= limit:
            break
    return list(reversed(events))


def safe_target_name(target: str) -> str:
    return (target or "main").replace("/", "_")


def chat_offset_path(run: Path, target: str, task_id: str | None = None) -> Path:
    target_name = safe_target_name(target)
    if task_id:
        return run / "runtime" / f"chat-offset-{safe_target_name(task_id)}-{target_name}.json"
    return run / "runtime" / f"chat-offset-{target_name}.json"


def load_chat_offset(inbox: Path, offset_file: Path, from_start: bool) -> int:
    if from_start:
        return 0
    if offset_file.exists():
        try:
            offset = int(read_json(offset_file).get("offset") or 0)
            if not inbox.exists() or offset <= inbox.stat().st_size:
                return max(0, offset)
        except (OSError, TypeError, ValueError):
            pass
    _, offset = iter_jsonl_from(inbox, 0)
    return offset


def save_chat_offset(offset_file: Path, offset: int) -> None:
    write_json(offset_file, {"offset": offset, "updated_at": utc_now()})


def status_from_agent_result(exit_code: int, reply: str) -> str:
    if exit_code != 0:
        return "failed"
    if any(marker in reply for marker in BLOCKED_REPLY_MARKERS):
        return "blocked"
    return "completed"


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
        "agents_summary": [_agent_summary_for_prompt(agent) for agent in agents],
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


def auto_reply(root: Path, run_id: str, args) -> int:
    require_plan(root, run_id)
    inbox = inbox_path(root, run_id, args.target)
    offset = 0
    if not args.from_start:
        _, offset = iter_jsonl_from(inbox, 0)
    print(f"Auto-reply listening to {args.target} in run {run_id}. Ctrl-C to exit.")
    try:
        while True:
            messages, offset = iter_jsonl_from(inbox, offset)
            for item in messages:
                original_sender = str(item.get("sender", "") or "")
                if original_sender == args.sender:
                    continue
                original_message = str(item.get("message", "") or "")
                if not original_message:
                    continue
                reply_target = args.reply_target or original_sender or "browser"
                reply = args.template.format(
                    message=original_message,
                    sender=original_sender,
                    target=args.target,
                    run_id=run_id,
                    ts=item.get("ts", ""),
                )
                append_message(
                    root,
                    run_id,
                    reply_target,
                    reply,
                    sender=args.sender,
                    task_id=item.get("task_id"),
                    role=item.get("role"),
                    to_agent=reply_target,
                    from_agent=args.sender,
                )
                print(f"{args.sender} -> {reply_target}: {reply}", flush=True)
                if args.once:
                    return 0
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130


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
            commit_policy = commit_message_policy_prompt(task_id, target).rstrip()
            components.update(
                {
                    "task_agents": detail["task"].get("agents", []),
                    "task_journal": journal_context,
                    "commit_policy": commit_policy,
                    "compact_summary": compact_context,
                }
            )
            task_context = render_prompt_template(
                "backend_task_context.md",
                task_id=task_id,
                title=detail["task"].get("title", ""),
                status=detail["task"].get("status", ""),
                role=item.get("role", ""),
                agents=detail["task"].get("agents", []),
                final_context=final_context.rstrip(),
                task_journal=journal_context,
                compact_summary=compact_context.rstrip(),
                commit_policy=commit_policy,
            )
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
            sandbox=(agent or {}).get("sandbox") or (task or {}).get("preferred_sandbox") or "-",
            approval=(agent or {}).get("approval") or (task or {}).get("preferred_approval") or "-",
            session_policy=(agent or {}).get("session_policy") or "-",
            backend_session_id=(agent or {}).get("backend_session_id") or "-",
        )
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
        events = recent_prompt_events(root, run_id, event_limit, str(task_id) if task_id else None)
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


def codex_chat(root: Path, run_id: str, args) -> int:
    return agent_chat(root, run_id, args, backend_name="codex")


def claude_chat(root: Path, run_id: str, args) -> int:
    return agent_chat(root, run_id, args, backend_name="claude")


def agent_chat(root: Path, run_id: str, args, *, backend_name: str) -> int:
    require_plan(root, run_id)
    cfg = load_config(root)
    inbox = inbox_path(root, run_id, args.target)
    run = run_dir(root, run_id)
    events_file = event_path(root, run_id)
    worker_task_id = str(getattr(args, "task_id", "") or "") or None
    offset_file = chat_offset_path(run, args.target, worker_task_id)
    offset = load_chat_offset(inbox, offset_file, args.from_start)
    last_coordination_check = 0.0
    task_label = f" task={worker_task_id}" if worker_task_id else ""
    source_name = f"{backend_name}-chat"
    print(f"{backend_name.title()} chat backend listening to {args.target}{task_label} in run {run_id}. Ctrl-C to exit.")
    try:
        while True:
            if args.target == "main" and not worker_task_id and not args.once and time.monotonic() - last_coordination_check >= max(10.0, args.interval):
                monitor_task_coordination(root, run_id)
                last_coordination_check = time.monotonic()
            message_records, next_offset = iter_jsonl_records_from(inbox, offset)
            for item, item_offset in message_records:
                exit_after_message = False
                item_task_id = str(item.get("task_id", "") or "") or None
                if worker_task_id and item_task_id != worker_task_id:
                    continue
                original_sender = str(item.get("sender", "") or "")
                if original_sender == args.sender:
                    continue
                original_message = str(item.get("message", "") or "")
                if not original_message:
                    continue
                agent_id = args.target if args.target != "main" else "main"
                detail = task_snapshot(root, run_id, item_task_id) if item_task_id else None
                task = detail["task"] if detail else {}
                is_agent_command = item.get("command_namespace") == "agent"
                is_finalization = item.get("result_policy") == "finalize"
                manages_task_status = bool(item_task_id and not is_agent_command)
                writes_task_final = bool(item_task_id and is_finalization)
                agent = next((entry for entry in task.get("agents", []) if entry.get("id") == agent_id), None)
                if args.target == "main" and original_sender.startswith("sub-") and item_task_id:
                    result = record_sub_agent_report(root, run_id, item_task_id, original_sender, original_message)
                    if result.get("handled"):
                        if args.once:
                            save_chat_offset(offset_file, item_offset)
                            return 0
                        continue
                coordination = task.get("coordination") or {}
                task_locked = task.get("status") in TERMINAL_TASK_STATUSES
                if manages_task_status and task_locked and not is_finalization:
                    append_event(
                        root,
                        run_id,
                        "agent_message_skipped",
                        {
                            "source": source_name,
                            "target": args.target,
                            "task_id": item_task_id,
                            "sender": original_sender,
                            "reason": "task is completed; reopen required",
                        },
                    )
                    if worker_task_id:
                        save_chat_offset(offset_file, item_offset)
                        mark_backend_stopped(root, run_id, args.target, task_id=worker_task_id, pid=os.getpid())
                        return 0
                    continue
                if args.target.startswith("sub-") and coordination.get("final_summary_requested_at"):
                    append_event(
                        root,
                        run_id,
                        "agent_message_skipped",
                        {
                            "source": source_name,
                            "target": args.target,
                            "task_id": item_task_id,
                            "sender": original_sender,
                            "reason": "task final summary already requested",
                        },
                    )
                    if worker_task_id:
                        save_chat_offset(offset_file, item_offset)
                        mark_backend_stopped(root, run_id, args.target, task_id=worker_task_id, pid=os.getpid())
                        return 0
                    continue
                if agent and agent.get("backend") != backend_name:
                    append_event(
                        root,
                        run_id,
                        "agent_skipped",
                        {
                            "source": source_name,
                            "target": args.target,
                            "task_id": item_task_id,
                            "reason": f"agent backend is {agent.get('backend')}, not {backend_name}",
                        },
                    )
                    continue
                session = ensure_session(
                    root,
                    run_id,
                    item_task_id,
                    agent_id,
                    backend_name,
                    model=(agent or {}).get("model") or task.get("preferred_model"),
                    workspace_path=(agent or {}).get("workspace_path") or task.get("workspace_path"),
                )
                reply_target = args.reply_target or item.get("reply_target") or original_sender or "browser"
                output_file = run / "chat" / f"{args.target}-{uuid.uuid4().hex[:8]}.md"
                requested_sandbox = (agent or {}).get("sandbox") or task.get("preferred_sandbox") or args.sandbox
                requested_approval = (agent or {}).get("approval") or task.get("preferred_approval") or args.approval
                sandbox = codex_sandbox("research", requested_sandbox) if backend_name == "codex" else requested_sandbox
                workspace = Path(task.get("workspace_path") or root)
                if not workspace.exists():
                    append_event(root, run_id, "workspace_missing", {"task_id": item_task_id, "workspace_path": str(workspace)})
                    workspace = root
                if manages_task_status:
                    set_task_status(root, run_id, item_task_id, "running")
                    set_agent_status(root, run_id, item_task_id, agent_id, "running")
                append_event(
                    root,
                    run_id,
                    "agent_started",
                    {
                        "source": source_name,
                        "target": args.target,
                        "sender": original_sender,
                        "task_id": item_task_id,
                        "sandbox": sandbox,
                        "approval": requested_approval,
                        "proxy_enabled": bool((agent or {}).get("proxy_enabled")),
                    },
                )
                proxy_env = proxy_env_for_agent(agent or {}, task)
                prompt, prompt_metrics = chat_prompt_with_metrics(root, run_id, args.target, item, args.prompt_prefix)
                append_event(root, run_id, "agent_prompt_metrics", {"source": source_name, **prompt_metrics})
                model = args.model or (agent or {}).get("model") or task.get("preferred_model") or session.get("model")
                if backend_name == "claude":
                    exit_code, reply, session = run_claude_exec(
                        prompt,
                        cwd=workspace,
                        output_file=output_file,
                        claude_bin=getattr(args, "claude_bin", "claude"),
                        model=model,
                        permission_mode=claude_permission_mode("research", sandbox),
                        extra_args=args.extra_arg or [],
                        events_file=events_file,
                        run_id=run_id,
                        task_id=item_task_id,
                        source=source_name,
                        target=args.target,
                        session=session,
                        proxy_env=proxy_env,
                        claude_config=cfg.get("claude", {}),
                    )
                else:
                    exit_code, reply, session = run_codex_exec(
                        prompt,
                        cwd=workspace,
                        output_file=output_file,
                        codex_bin=args.codex_bin,
                        model=model,
                        sandbox=sandbox,
                        approval=requested_approval,
                        json_events=not getattr(args, "no_json", False),
                        extra_args=args.extra_arg or [],
                        events_file=events_file,
                        run_id=run_id,
                        task_id=item_task_id,
                        source=source_name,
                        target=args.target,
                        session=session,
                        proxy_env=proxy_env,
                    )
                if session:
                    save_session(root, session)
                if exit_code == 0 and reply.strip():
                    executed = execute_actions(root, run_id, item_task_id, reply)
                    delegating_actions = [action for action in executed if action.get("type") in {"route_to_agent", "spawn_sub"}]
                    display_reply = action_response_text(reply)
                    append_message(
                        root,
                        run_id,
                        reply_target,
                        display_reply,
                        sender=args.sender,
                        task_id=item_task_id,
                        role=item.get("role") or "main",
                        from_agent=args.sender,
                        to_agent=reply_target,
                    )
                    if delegating_actions:
                        append_event(root, run_id, "agent_delegated", {"task_id": item_task_id, "count": len(delegating_actions)})
                        detail = task_snapshot(root, run_id, item_task_id) if item_task_id else None
                        if detail:
                            append_message(
                                root,
                                run_id,
                                "browser",
                                waiting_for_subagents_message(detail["task"]),
                                sender="main",
                                task_id=item_task_id,
                                role="main",
                                from_agent="main",
                                to_agent="browser",
                                coordination="waiting_for_subagents",
                            )
                    if manages_task_status:
                        final_status = status_from_agent_result(exit_code, reply)
                        if writes_task_final and agent_id == "main":
                            write_task_result(root, run_id, item_task_id, reply.strip())
                            mark_task_coordination(root, run_id, item_task_id, final_summary_completed_at=utc_now())
                            set_agent_status(root, run_id, item_task_id, agent_id, final_status, exit_code)
                            set_task_status(root, run_id, item_task_id, final_status, exit_code)
                            if final_status in TERMINAL_TASK_STATUSES:
                                stop_task_backends(root, run_id, item_task_id, exclude_pid=os.getpid())
                                exit_after_message = bool(worker_task_id)
                        elif agent_id != "main":
                            set_agent_status(root, run_id, item_task_id, agent_id, final_status, exit_code)
                            request_round_summary_if_ready(root, run_id, item_task_id)
                            set_task_status(root, run_id, item_task_id, "running")
                            exit_after_message = bool(worker_task_id)
                        else:
                            detail = task_snapshot(root, run_id, item_task_id)
                            if item.get("coordination") == "subagents_complete":
                                sub_agents = [
                                    agent.get("id")
                                    for agent in detail["task"].get("agents", [])
                                    if agent.get("role") == "sub" and agent.get("id")
                                ]
                                append_task_round(
                                    root,
                                    run_id,
                                    item_task_id,
                                    {
                                        "trigger": "subagents_complete",
                                        "summary": display_reply,
                                        "agents": ["main", *sub_agents],
                                    },
                                )
                                mark_task_coordination(root, run_id, item_task_id, round_summary_completed_at=utc_now())
                                set_agent_status(root, run_id, item_task_id, agent_id, final_status, exit_code)
                                set_task_status(root, run_id, item_task_id, "awaiting_user")
                            elif delegating_actions or task_has_incomplete_sub_agents(detail["task"]):
                                set_agent_status(root, run_id, item_task_id, agent_id, "waiting")
                                set_task_status(root, run_id, item_task_id, "running")
                            else:
                                set_agent_status(root, run_id, item_task_id, agent_id, final_status, exit_code)
                                next_task_status = "awaiting_user" if final_status == "completed" else final_status
                                set_task_status(root, run_id, item_task_id, next_task_status, exit_code)
                                if next_task_status in TERMINAL_TASK_STATUSES:
                                    stop_task_backends(root, run_id, item_task_id, exclude_pid=os.getpid())
                                    exit_after_message = bool(worker_task_id)
                    print(f"{args.sender} -> {reply_target}: {display_reply}", flush=True)
                else:
                    if manages_task_status:
                        if agent_id != "main":
                            set_agent_status(root, run_id, item_task_id, agent_id, "failed", exit_code)
                            request_round_summary_if_ready(root, run_id, item_task_id)
                            set_task_status(root, run_id, item_task_id, "running")
                            exit_after_message = bool(worker_task_id)
                        else:
                            set_agent_status(root, run_id, item_task_id, agent_id, "failed", exit_code)
                            set_task_status(root, run_id, item_task_id, "failed", exit_code)
                            stop_task_backends(root, run_id, item_task_id, exclude_pid=os.getpid())
                            exit_after_message = bool(worker_task_id)
                    append_event(root, run_id, "agent_error", {"source": source_name, "target": args.target, "task_id": item_task_id, "exit_code": exit_code})
                append_event(root, run_id, "agent_finished", {"source": source_name, "target": args.target, "task_id": item_task_id, "exit_code": exit_code})
                if exit_after_message and worker_task_id:
                    save_chat_offset(offset_file, item_offset)
                    mark_backend_stopped(root, run_id, args.target, task_id=worker_task_id, pid=os.getpid())
                    return exit_code
                if args.once:
                    save_chat_offset(offset_file, item_offset)
                    return exit_code
            if message_records:
                offset = next_offset
                save_chat_offset(offset_file, offset)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
