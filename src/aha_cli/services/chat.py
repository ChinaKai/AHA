from __future__ import annotations

import os
from pathlib import Path
import textwrap
import time
import uuid

from aha_cli.backends.codex import codex_sandbox, run_codex_exec
from aha_cli.domain.models import utc_now
from aha_cli.services.backend_runtime import mark_backend_stopped, stop_task_backends
from aha_cli.services.commit_policy import commit_message_policy_prompt
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
    "写入失败",
    "文件没有落盘",
    "permission denied",
    "Permission denied",
    "Read-only file system",
)

TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}
PROMPT_REDACTED_PROXY_FIELDS = {"preferred_http_proxy", "preferred_https_proxy", "preferred_no_proxy"}


def recent_run_events(root: Path, run_id: str, limit: int) -> list[dict]:
    events: list[dict] = []
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
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


def chat_prompt(root: Path, run_id: str, target: str, item: dict, prefix: str) -> str:
    plan = require_plan(root, run_id)
    task_id = item.get("task_id")
    is_finalization = item.get("result_policy") == "finalize"
    is_agent_command = item.get("command_namespace") == "agent"
    task_context = ""
    if task_id:
        try:
            detail = task_snapshot(root, run_id, str(task_id))
            task = detail["task"]
            agent = next((entry for entry in task.get("agents", []) if entry.get("id") == target), None)
            if is_agent_command:
                command = str(item.get("message", "") or "")
                original_command = str(item.get("original_command", "") or "")
                return textwrap.dedent(
                    f"""\
                    {prefix}

                    You are the AHA backend agent for `{target}`.
                    The user sent an agent command. Treat it as a command for this agent, not as a task-status question.
                    Do not summarize previous task work or mention old task completion unless the command explicitly asks for task history.

                    Agent command:
                    - original: {original_command or command}
                    - routed: {command}

                    Agent metadata:
                    - task_id: {task_id}
                    - task_title: {task.get("title", "")}
                    - agent_id: {target}
                    - role: {(agent or {}).get("role") or item.get("role", "")}
                    - backend: {(agent or {}).get("backend") or task.get("preferred_backend") or "codex"}
                    - model: {(agent or {}).get("model") or task.get("preferred_model") or "default"}
                    - workspace: {(agent or {}).get("workspace_path") or task.get("workspace_path") or "-"}
                    - sandbox: {(agent or {}).get("sandbox") or task.get("preferred_sandbox") or "-"}
                    - approval: {(agent or {}).get("approval") or task.get("preferred_approval") or "-"}

                    Command semantics:
                    - /status: report this agent's runtime/session metadata only.
                    - /help: report supported agent command semantics briefly.
                    - Other slash commands: answer only if you can handle that command; otherwise say it is not supported in this backend mode.

                    Keep the reply concise and use the user's language.
                    """
                )
            existing_final = detail.get("result", "").strip()
            final_context = ""
            if is_finalization and existing_final:
                final_context = f"- existing Final chars: {len(existing_final)}\n"
            rounds = detail.get("rounds", [])
            journal_context = ""
            if rounds:
                recent_rounds = rounds[-10:]
                journal_lines = ["Task journal:"]
                for round_item in recent_rounds:
                    journal_lines.append(
                        f"- {round_item.get('round_id')} [{round_item.get('trigger')}] {round_item.get('summary')}"
                    )
                journal_context = textwrap.indent("\n".join(journal_lines), "                ")
            commit_policy = textwrap.indent(commit_message_policy_prompt(task_id, target).rstrip(), "                ")
            task_context = textwrap.dedent(
                f"""\
                Current task context:
                - task_id: {task_id}
                - title: {detail["task"].get("title", "")}
                - status: {detail["task"].get("status", "")}
                - role selected by user: {item.get("role", "")}
                - agents: {detail["task"].get("agents", [])}
                {final_context.rstrip()}
{journal_context}

                Ownership and routing policy:
                - Each sub-agent owns its assigned scope (`assignment` / `created_reason`).
                - If a user follow-up is about a scope owned by an existing sub-agent, do not handle that work yourself.
                - To route work or record a durable task update, return ONLY one JSON object with `actions` and `response`; do not wrap it in Markdown or mix it with prose.
                - Route format: `{{"type": "route_to_agent", "agent_id": "...", "message": "..."}}`.
                - Task update format: `{{"type": "record_task_update", "summary": "...", "changed_files": [], "verification": [], "risks": []}}`.
                - Use `record_task_update` only after concrete completed work, decisions, validation, commits, or meaningful follow-up state; do not record pure discussion or status chatter.
                - Handle the message yourself only when it is clearly task-main coordination, cross-agent summary, or no sub-agent owns the scope.

                Commit ownership policy:
                - Commit, revert, and repository-change finalization requests are ownership-sensitive.
                - When you are `task-main`, route commit work to the sub-agent that owns the changed scope when one exists.
                - When you are a sub-agent, commit only files covered by your `assignment` / `created_reason`; if the requested commit is outside your scope, report back to `task-main`.
                - Before any commit, inspect `git status`, avoid unrelated or user changes, and follow the AHA commit message policy below.

{commit_policy}
                """
            )
        except KeyError:
            task_context = f"Current task context: task_id={task_id} was referenced but not found.\n"
    event_limit = 80 if is_finalization else 20
    events = recent_run_events(root, run_id, event_limit)
    recent = "\n".join(format_event(event) for event in events) or "(no events)"
    status = redact_proxy_fields_for_prompt(status_snapshot(root, run_id))
    mode_instruction = (
        "You are generating the task Final. Return concise Markdown only. "
        "Use the Task journal as the primary source when available. Preserve the task's meaningful rounds under `## 任务轮次` as a chronological ordered list (`1.`, `2.`, ...), then summarize the stable outcome, changed files or decisions, verification, and remaining actionable risks. "
        "Do not echo noisy command chatter unless it affects the outcome."
        if is_finalization
        else "Reply directly to the user. Keep the answer concise and use the user's language."
    )
    return textwrap.dedent(
        f"""\
        {prefix}

        You are the AHA backend agent for `{target}`.
        {mode_instruction}
        Do not modify files unless the user explicitly asks for a repository change.

        Run goal:
        {plan["goal"]}

        Current status:
        {status}

        {task_context or "Current task context: none"}

        Recent events:
        {recent}

        User message from {item.get("sender", "browser")} at {item.get("ts", "")}:
        {item.get("message", "")}
        """
    )


def codex_chat(root: Path, run_id: str, args) -> int:
    require_plan(root, run_id)
    inbox = inbox_path(root, run_id, args.target)
    run = run_dir(root, run_id)
    events_file = event_path(root, run_id)
    worker_task_id = str(getattr(args, "task_id", "") or "") or None
    offset_file = chat_offset_path(run, args.target, worker_task_id)
    offset = load_chat_offset(inbox, offset_file, args.from_start)
    last_coordination_check = 0.0
    task_label = f" task={worker_task_id}" if worker_task_id else ""
    print(f"Codex chat backend listening to {args.target}{task_label} in run {run_id}. Ctrl-C to exit.")
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
                            "source": "codex-chat",
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
                            "source": "codex-chat",
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
                if agent and agent.get("backend") != "codex":
                    append_event(
                        root,
                        run_id,
                        "agent_skipped",
                        {
                            "source": "codex-chat",
                            "target": args.target,
                            "task_id": item_task_id,
                            "reason": f"agent backend is {agent.get('backend')}, not codex",
                        },
                    )
                    continue
                session = ensure_session(
                    root,
                    run_id,
                    item_task_id,
                    agent_id,
                    "codex",
                    model=(agent or {}).get("model") or task.get("preferred_model"),
                    workspace_path=(agent or {}).get("workspace_path") or task.get("workspace_path"),
                )
                reply_target = args.reply_target or item.get("reply_target") or original_sender or "browser"
                output_file = run / "chat" / f"{args.target}-{uuid.uuid4().hex[:8]}.md"
                requested_sandbox = (agent or {}).get("sandbox") or task.get("preferred_sandbox") or args.sandbox
                requested_approval = (agent or {}).get("approval") or task.get("preferred_approval") or args.approval
                sandbox = codex_sandbox("research", requested_sandbox)
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
                        "source": "codex-chat",
                        "target": args.target,
                        "sender": original_sender,
                        "task_id": item_task_id,
                        "sandbox": sandbox,
                        "approval": requested_approval,
                        "proxy_enabled": bool((agent or {}).get("proxy_enabled")),
                    },
                )
                proxy_env = proxy_env_for_agent(agent or {}, task)
                exit_code, reply, session = run_codex_exec(
                    chat_prompt(root, run_id, args.target, item, args.prompt_prefix),
                    cwd=workspace,
                    output_file=output_file,
                    codex_bin=args.codex_bin,
                    model=args.model or (agent or {}).get("model") or task.get("preferred_model") or session.get("model"),
                    sandbox=sandbox,
                    approval=requested_approval,
                    json_events=not args.no_json,
                    extra_args=args.extra_arg or [],
                    events_file=events_file,
                    run_id=run_id,
                    task_id=item_task_id,
                    source="codex-chat",
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
                    append_event(root, run_id, "agent_error", {"source": "codex-chat", "target": args.target, "task_id": item_task_id, "exit_code": exit_code})
                append_event(root, run_id, "agent_finished", {"source": "codex-chat", "target": args.target, "task_id": item_task_id, "exit_code": exit_code})
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
