from __future__ import annotations

from pathlib import Path
import textwrap
import time
import uuid

from aha_cli.backends.codex import codex_sandbox, run_codex_exec
from aha_cli.services.orchestrator import execute_actions
from aha_cli.store.filesystem import (
    append_event,
    append_message,
    ensure_session,
    event_path,
    inbox_path,
    iter_jsonl_from,
    require_plan,
    run_dir,
    save_session,
    set_task_status,
    status_snapshot,
    task_snapshot,
    write_task_result,
)
from aha_cli.services.messages import format_event


BLOCKED_REPLY_MARKERS = (
    "read-only sandbox",
    "只读沙箱",
    "writing is blocked",
    "写入被拦截",
    "写入失败",
    "写入 `",
    "文件没有落盘",
    "permission denied",
    "Permission denied",
    "Read-only file system",
)


def status_from_agent_result(exit_code: int, reply: str) -> str:
    if exit_code != 0:
        return "failed"
    if any(marker in reply for marker in BLOCKED_REPLY_MARKERS):
        return "blocked"
    return "completed"


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
            task_context = textwrap.dedent(
                f"""\
                Current task context:
                - task_id: {task_id}
                - title: {detail["task"].get("title", "")}
                - status: {detail["task"].get("status", "")}
                - role selected by user: {item.get("role", "")}
                - agents: {detail["task"].get("agents", [])}
                {final_context.rstrip()}
                """
            )
        except KeyError:
            task_context = f"Current task context: task_id={task_id} was referenced but not found.\n"
    events, _ = iter_jsonl_from(event_path(root, run_id), 0)
    event_limit = 80 if is_finalization else 20
    recent = "\n".join(format_event(event) for event in events[-event_limit:]) or "(no events)"
    status = status_snapshot(root, run_id)
    mode_instruction = (
        "You are generating the task Final. Return concise Markdown only. "
        "Summarize the stable outcome, changed files or decisions, verification, and remaining actionable risks. "
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
    offset = 0
    if not args.from_start:
        _, offset = iter_jsonl_from(inbox, 0)
    print(f"Codex chat backend listening to {args.target} in run {run_id}. Ctrl-C to exit.")
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
                item_task_id = str(item.get("task_id", "") or "") or None
                agent_id = args.target if args.target != "main" else "main"
                detail = task_snapshot(root, run_id, item_task_id) if item_task_id else None
                task = detail["task"] if detail else {}
                is_agent_command = item.get("command_namespace") == "agent"
                is_finalization = item.get("result_policy") == "finalize"
                manages_task_status = bool(item_task_id and not is_agent_command)
                writes_task_final = bool(item_task_id and is_finalization)
                agent = next((entry for entry in task.get("agents", []) if entry.get("id") == agent_id), None)
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
                reply_target = args.reply_target or original_sender or "browser"
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
                    },
                )
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
                    session=session,
                )
                if session:
                    save_session(root, session)
                if exit_code == 0 and reply.strip():
                    executed = execute_actions(root, run_id, item_task_id, reply)
                    append_message(
                        root,
                        run_id,
                        reply_target,
                        reply.strip(),
                        sender=args.sender,
                        task_id=item_task_id,
                        role=item.get("role") or "main",
                        from_agent=args.sender,
                        to_agent=reply_target,
                    )
                    if executed:
                        append_event(root, run_id, "agent_delegated", {"task_id": item_task_id, "count": len(executed)})
                    if manages_task_status:
                        final_status = status_from_agent_result(exit_code, reply)
                        if writes_task_final and agent_id == "main":
                            write_task_result(root, run_id, item_task_id, reply.strip())
                        set_task_status(root, run_id, item_task_id, final_status, exit_code)
                    print(f"{args.sender} -> {reply_target}: {reply.strip()}", flush=True)
                else:
                    if manages_task_status:
                        set_task_status(root, run_id, item_task_id, "failed", exit_code)
                    append_event(root, run_id, "agent_error", {"source": "codex-chat", "target": args.target, "task_id": item_task_id, "exit_code": exit_code})
                append_event(root, run_id, "agent_finished", {"source": "codex-chat", "target": args.target, "task_id": item_task_id, "exit_code": exit_code})
                if args.once:
                    return exit_code
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
