from __future__ import annotations

import os
from pathlib import Path
import time
import uuid

from aha_cli.backends.claude import claude_permission_mode, run_claude_exec
from aha_cli.backends.codex import codex_sandbox, run_codex_exec
from aha_cli.domain.models import utc_now
from aha_cli.services.backend_runtime import mark_backend_stopped, stop_task_backends
from aha_cli.services.chat_offsets import chat_offset_path, load_chat_offset, save_chat_offset, worker_backend_should_exit_after_turn
from aha_cli.services.chat_prompt_context import chat_prompt, chat_prompt_with_metrics
from aha_cli.services.chat_supervision import (
    SUPERVISION_FAILURE_FALLBACK_STATUS,
    apply_supervision_host_decision,
    apply_supervision_real_host,
    is_task_supervision_host_agent,
)
from aha_cli.services.orchestrator import (
    action_response_text,
    apply_supervision_stub,
    execute_actions,
    extract_action_payload,
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
    load_config,
    mark_task_coordination,
    require_plan,
    run_dir,
    save_session,
    set_agent_status,
    set_task_status,
    task_snapshot,
    write_task_result,
)
from aha_cli.services.native_subagents import text_claims_subagent_created


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


def status_from_agent_result(exit_code: int, reply: str) -> str:
    if exit_code != 0:
        return "failed"
    payload = extract_action_payload(reply)
    if payload and isinstance(payload.get("response"), str):
        return "completed"
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
                if item_task_id and is_task_supervision_host_agent(task, agent_id):
                    host_result = apply_supervision_host_decision(
                        root,
                        run_id,
                        item_task_id,
                        host_agent_id=agent_id,
                        host_reply=reply,
                        exit_code=exit_code,
                    )
                    if manages_task_status:
                        final_status = status_from_agent_result(exit_code, reply)
                        set_agent_status(root, run_id, item_task_id, agent_id, final_status, exit_code)
                        if host_result.get("backend_failed"):
                            set_task_status(root, run_id, item_task_id, SUPERVISION_FAILURE_FALLBACK_STATUS, exit_code)
                        elif host_result.get("routed_to_main"):
                            set_agent_status(root, run_id, item_task_id, "main", "pending")
                            set_task_status(root, run_id, item_task_id, "running")
                        elif host_result.get("waiting"):
                            set_agent_status(root, run_id, item_task_id, "main", "waiting")
                            set_task_status(root, run_id, item_task_id, "running")
                        elif host_result.get("routed_to_browser"):
                            set_task_status(root, run_id, item_task_id, "awaiting_user", exit_code)
                        elif host_result.get("executed"):
                            request_round_summary_if_ready(root, run_id, item_task_id)
                            set_task_status(root, run_id, item_task_id, "running")
                        else:
                            next_task_status = "awaiting_user" if final_status == "completed" else final_status
                            set_task_status(root, run_id, item_task_id, next_task_status, exit_code)
                    append_event(root, run_id, "agent_finished", {"source": source_name, "target": args.target, "task_id": item_task_id, "exit_code": exit_code})
                    print(f"{args.sender} supervision decision: {action_response_text(reply)}", flush=True)
                    if worker_task_id:
                        save_chat_offset(offset_file, item_offset)
                        mark_backend_stopped(root, run_id, args.target, task_id=worker_task_id, pid=os.getpid())
                        return exit_code
                    if args.once:
                        save_chat_offset(offset_file, item_offset)
                        return exit_code
                    continue
                supervision_routed_to_main = False
                supervision_waiting_for_host = False
                if exit_code == 0 and reply.strip():
                    executed = execute_actions(root, run_id, item_task_id, reply)
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
                    if agent_id == "main" and manages_task_status and not writes_task_final and not is_agent_command:
                        apply_supervision_stub(
                            root,
                            run_id,
                            item_task_id,
                            source_agent=agent_id,
                            reply_text=display_reply,
                        )
                        host_result = apply_supervision_real_host(
                            root,
                            run_id,
                            item_task_id,
                            source_agent=agent_id,
                            reply_text=display_reply,
                            cfg=cfg,
                            run=run,
                        )
                        if host_result:
                            executed.extend(host_result.get("executed", []))
                            supervision_routed_to_main = bool(host_result.get("routed_to_main"))
                            supervision_waiting_for_host = bool(host_result.get("routed_to_host"))
                    delegating_actions = [action for action in executed if action.get("type") in {"route_to_agent", "spawn_sub"}]
                    if (
                        agent_id == "main"
                        and item_task_id
                        and not any(action.get("type") == "spawn_sub" for action in executed)
                        and text_claims_subagent_created(reply)
                    ):
                        append_event(
                            root,
                            run_id,
                            "claimed_sub_without_aha_agent",
                            {
                                "task_id": item_task_id,
                                "target": agent_id,
                                "reason": "reply_claim_without_spawn_sub_action",
                                "text": display_reply,
                            },
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
                            elif supervision_routed_to_main:
                                set_agent_status(root, run_id, item_task_id, agent_id, "pending")
                                set_task_status(root, run_id, item_task_id, "running")
                            elif supervision_waiting_for_host:
                                set_agent_status(root, run_id, item_task_id, agent_id, final_status, exit_code)
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
                    empty_reply_waiting_for_subagents = False
                    if manages_task_status:
                        if agent_id != "main":
                            set_agent_status(root, run_id, item_task_id, agent_id, "failed", exit_code)
                            request_round_summary_if_ready(root, run_id, item_task_id)
                            set_task_status(root, run_id, item_task_id, "running")
                            exit_after_message = bool(worker_task_id)
                        else:
                            detail = task_snapshot(root, run_id, item_task_id)
                            if exit_code == 0 and task_has_incomplete_sub_agents(detail["task"]):
                                empty_reply_waiting_for_subagents = True
                                set_agent_status(root, run_id, item_task_id, agent_id, "waiting", exit_code)
                                set_task_status(root, run_id, item_task_id, "running")
                                append_event(
                                    root,
                                    run_id,
                                    "agent_message_skipped",
                                    {
                                        "source": source_name,
                                        "target": args.target,
                                        "task_id": item_task_id,
                                        "reason": "empty_reply_while_subagents_running",
                                    },
                                )
                            else:
                                set_agent_status(root, run_id, item_task_id, agent_id, "failed", exit_code)
                                if task_has_incomplete_sub_agents(detail["task"]):
                                    set_task_status(root, run_id, item_task_id, "running", exit_code)
                                else:
                                    set_task_status(root, run_id, item_task_id, "failed", exit_code)
                                    stop_task_backends(root, run_id, item_task_id, exclude_pid=os.getpid())
                                exit_after_message = bool(worker_task_id)
                    if not empty_reply_waiting_for_subagents:
                        append_event(root, run_id, "agent_error", {"source": source_name, "target": args.target, "task_id": item_task_id, "exit_code": exit_code})
                if worker_backend_should_exit_after_turn(root, run_id, item_task_id, worker_task_id, inbox, item_offset):
                    exit_after_message = True
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
