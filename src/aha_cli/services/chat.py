from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
import traceback
import uuid

from aha_cli.backends.claude import claude_cli_model, claude_config_for_model, claude_permission_mode, claude_resolved_model, run_claude_exec
from aha_cli.backends.codex import codex_cli_model, codex_config_for_model, codex_resolved_model, codex_sandbox, run_codex_exec
from aha_cli.backends.registry import resolve_model
from aha_cli.domain.models import utc_now
from aha_cli.services.auto_context_compact import auto_compact_agent_context_after_turn
from aha_cli.services.backend_runtime import mark_backend_stopped, start_backend, stop_task_backends
from aha_cli.services.chat_offsets import chat_offset_path, load_chat_offset, save_chat_offset, worker_backend_should_exit_after_turn
from aha_cli.services.chat_prompt_context import chat_prompt, chat_prompt_with_metrics, model_family_for_guidance
from aha_cli.services.chat_supervision import (
    SUPERVISION_FAILURE_FALLBACK_STATUS,
    apply_supervision_host_decision,
    apply_supervision_real_host,
    is_task_supervision_host_agent,
)
from aha_cli.services.action_payloads import action_response_text, extract_action_payload, extract_action_payload_result, invalid_action_schema_reason
from aha_cli.services.commit_policy import generated_by_for_backend_model, validate_commit_message
from aha_cli.services.orchestrator import (
    append_visible_sub_agent_result,
    apply_supervision_stub,
    execute_actions,
    monitor_task_coordination,
    record_sub_agent_report,
    request_round_summary_if_ready,
    sub_agent_failure_message,
)
from aha_cli.services.progress_heartbeat import AgentProgressHeartbeat
from aha_cli.services.prompt_artifacts import save_prompt_artifact
from aha_cli.services.proxy import proxy_env_for_agent
from aha_cli.services.subagent_state import task_has_incomplete_sub_agents, waiting_for_subagents_message
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
from aha_cli.store.task_memos import update_task_memo
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
SUPERVISION_SKIP_COORDINATIONS = {
    "agent_recovery_notice",
    "backend_restart_status",
    "deployment_status",
    "service_restart_status",
    "stale_runtime_recovery_restart",
    "system_status",
    "verification_status",
}

AHA_ACTION_RETRY_SCHEMA = (
    '{"actions":[{"type":"record_task_update","summary":"...","changed_files":[],"verification":[],"risks":[]}],'
    '"response":"..."}'
)


def action_schema_retry_message(reason: str) -> str:
    return "\n".join(
        [
            AHA_ACTION_RETRY_SCHEMA,
            "",
            "AHA runtime rejected your previous reply before executing actions.",
            f"Reason: {reason}.",
            "Return exactly one JSON object with an `actions` array and `response` string.",
            "Allowed action types are `route_to_agent`, `spawn_sub`, and `record_task_update`.",
            "Do not use top-level `type` or `action`; do not wrap the JSON in Markdown.",
            "Continue from the latest active user request.",
        ]
    )


def task_update_required_retry_message() -> str:
    return "\n".join(
        [
            AHA_ACTION_RETRY_SCHEMA,
            "",
            "AHA runtime detected repository changes from your previous turn, but your reply did not include `record_task_update`.",
            "Before normal completion, return exactly one AHA JSON object with a `record_task_update` action.",
            "Fill `changed_files`, `verification`, and `risks` from the actual work you performed.",
            "Do not mix Markdown prose outside the JSON envelope.",
        ]
    )


def commit_policy_retry_message(errors: list[str], expected_generated_by: str) -> str:
    error_text = "; ".join(errors)
    return "\n".join(
        [
            AHA_ACTION_RETRY_SCHEMA,
            "",
            "AHA runtime detected a commit message policy violation from your previous turn.",
            f"Expected Generated-by trailer: {expected_generated_by}",
            f"Errors: {error_text}",
            "Amend or repair the commit before normal completion, then return an AHA JSON object with `record_task_update`.",
            "Do not add Co-Authored-By, AHA-Task, AHA-Agent, or AHA-Scope trailers.",
        ]
    )


def _reply_action_schema_error(reply: str) -> str | None:
    result = extract_action_payload_result(reply)
    if result.error:
        return result.error
    payload = result.payload
    if not payload:
        return None
    return invalid_action_schema_reason(payload)


def _reply_has_action_type(reply: str, action_type: str) -> bool:
    result = extract_action_payload_result(reply)
    if result.error:
        return False
    payload = result.payload
    if not payload or invalid_action_schema_reason(payload):
        return False
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return False
    return any(isinstance(action, dict) and action.get("type") == action_type for action in actions)


def _git_workspace_snapshot(workspace: Path) -> dict | None:
    try:
        root_result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if root_result.returncode != 0:
        return None
    try:
        head_result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status_result = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                ".",
                ":(exclude).aha",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if status_result.returncode != 0:
        return None
    head = head_result.stdout.strip() if head_result.returncode == 0 else ""
    return {"root": root_result.stdout.strip(), "head": head, "status": status_result.stdout}


def _git_workspace_changed(before: dict | None, after: dict | None) -> bool:
    return bool(before is not None and after is not None and before != after)


def _git_commit_messages(workspace: Path, before: dict | None, after: dict | None, *, force_head: bool = False) -> list[str]:
    if not after or not after.get("head"):
        return []
    before_head = str((before or {}).get("head") or "")
    after_head = str(after.get("head") or "")
    if before_head and before_head != after_head:
        revision = f"{before_head}..{after_head}"
    elif not before_head and after_head:
        revision = after_head
    elif force_head:
        revision = "-1"
    else:
        return []
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "log", "--format=%B%x1e", revision],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [message.strip() + "\n" for message in result.stdout.split("\x1e") if message.strip()]


def _git_commit_policy_errors(
    workspace: Path,
    before: dict | None,
    after: dict | None,
    *,
    expected_generated_by: str,
    force_head: bool = False,
) -> list[str]:
    errors: list[str] = []
    for message in _git_commit_messages(workspace, before, after, force_head=force_head):
        errors.extend(validate_commit_message(message, expected_generated_by=expected_generated_by))
    return errors


def append_agent_retry_request(
    root: Path,
    run_id: str,
    *,
    target: str,
    task_id: str | None,
    agent_id: str,
    item: dict,
    message: str,
    gate: str,
    reason: str,
    manages_task_status: bool,
) -> None:
    append_message(
        root,
        run_id,
        target,
        message,
        sender="aha",
        task_id=task_id,
        role=item.get("role") or "main",
        from_agent="aha",
        to_agent=agent_id,
        coordination=gate,
    )
    append_event(root, run_id, "agent_retry_requested", {"task_id": task_id, "target": agent_id, "gate": gate, "reason": reason})
    if manages_task_status and task_id:
        set_agent_status(root, run_id, task_id, agent_id, "pending")
        set_task_status(root, run_id, task_id, "running")


def finish_retry_turn(
    root: Path,
    run_id: str,
    args,
    *,
    worker_task_id: str | None,
    offset_file: Path,
    item_offset: int,
    backend_name: str,
    model: str | None,
    sandbox: str,
    approval: str,
    gate: str,
) -> None:
    save_chat_offset(offset_file, item_offset)
    if not worker_task_id:
        return
    mark_backend_stopped(root, run_id, args.target, task_id=worker_task_id, pid=os.getpid())
    if args.once:
        return
    try:
        start_backend(
            root,
            run_id,
            args.target,
            backend=backend_name,
            model=model,
            sandbox=sandbox,
            approval=approval,
            from_start=False,
            task_id=worker_task_id,
        )
    except Exception as exc:
        append_event(
            root,
            run_id,
            "agent_retry_backend_start_failed",
            {
                "task_id": worker_task_id,
                "target": args.target,
                "gate": gate,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


def append_agent_completion_blocked(
    root: Path,
    run_id: str,
    *,
    task_id: str | None,
    agent_id: str,
    item: dict,
    reason: str,
    manages_task_status: bool,
) -> None:
    notice = f"AHA runtime blocked normal completion for `{agent_id}`: {reason}"
    append_event(root, run_id, "agent_completion_blocked", {"task_id": task_id, "target": agent_id, "reason": reason})
    append_message(
        root,
        run_id,
        "browser",
        notice,
        sender="aha",
        task_id=task_id,
        role=item.get("role") or "main",
        from_agent="aha",
        to_agent="browser",
        coordination="completion_blocked",
        agent_id=agent_id,
    )
    if manages_task_status and task_id:
        set_agent_status(root, run_id, task_id, agent_id, "failed", 1)
        set_task_status(root, run_id, task_id, "running", 1)


def message_triggers_supervision(item: dict) -> bool:
    coordination = str(item.get("coordination") or "").strip().lower()
    if coordination in SUPERVISION_SKIP_COORDINATIONS:
        return False
    return True


def status_from_agent_result(exit_code: int, reply: str) -> str:
    if exit_code != 0:
        return "failed"
    payload = extract_action_payload(reply)
    if payload and isinstance(payload.get("response"), str):
        return "completed"
    if any(marker in reply for marker in BLOCKED_REPLY_MARKERS):
        return "blocked"
    return "completed"


def agent_error_notice(target: str, exit_code: int, reply: str) -> str:
    detail = reply.strip()
    if detail:
        return f"AHA runtime 检测到 `{target}` agent 后端异常退出（exit={exit_code}）。\n\n{detail}"
    return f"AHA runtime 检测到 `{target}` agent 没有返回有效回复（exit={exit_code}）。请检查 Runtime 日志或后端连通性。"


def append_agent_error_notice(
    root: Path,
    run_id: str,
    *,
    source: str,
    target: str,
    task_id: str | None,
    agent_id: str,
    exit_code: int,
    reply: str,
    role: str,
) -> None:
    message = agent_error_notice(target, exit_code, reply)
    append_event(
        root,
        run_id,
        "agent_error",
        {
            "source": source,
            "target": target,
            "task_id": task_id,
            "exit_code": exit_code,
            "message": message,
        },
    )
    append_message(
        root,
        run_id,
        "browser",
        message,
        sender="system",
        task_id=task_id,
        role=role,
        from_agent="system",
        to_agent="browser",
        coordination="agent_error",
        agent_id=agent_id,
    )


def write_memo_report_result(root: Path, run_id: str, item: dict, reply: str, exit_code: int) -> dict | None:
    context = item.get("memo_report_context") if isinstance(item.get("memo_report_context"), dict) else {}
    memo_id = str(context.get("memo_id") or "").strip()
    task_id = str(context.get("task_id") or item.get("task_id") or "").strip()
    if not memo_id:
        append_event(root, run_id, "task_memo_report_failed", {"task_id": task_id, "reason": "missing memo_id"})
        return None
    now = utc_now()
    if exit_code == 0 and reply.strip():
        memo = update_task_memo(
            root,
            run_id,
            memo_id,
            {
                "report_status": "ready",
                "completion_report": reply.strip(),
                "report_task_id": task_id,
                "report_completed_at": now,
                "report_error": "",
            },
        )
        append_event(
            root,
            run_id,
            "task_memo_report_completed",
            {"memo_id": memo_id, "task_id": task_id, "chars": len(reply.strip()), "completed_at": now},
        )
        return memo
    error = agent_error_notice("main", exit_code, reply)
    memo = update_task_memo(
        root,
        run_id,
        memo_id,
        {
            "report_status": "failed",
            "report_task_id": task_id,
            "report_completed_at": now,
            "report_error": error,
        },
    )
    append_event(root, run_id, "task_memo_report_failed", {"memo_id": memo_id, "task_id": task_id, "error": error})
    return memo


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
                plan = require_plan(root, run_id) if item_task_id else {}
                task = detail["task"] if detail else {}
                is_agent_command = item.get("command_namespace") == "agent"
                result_policy = str(item.get("result_policy") or "")
                is_finalization = result_policy == "finalize"
                is_memo_report = result_policy == "memo_report"
                manages_task_status = bool(item_task_id and not is_agent_command and not is_memo_report)
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
                configured_model = args.model or (agent or {}).get("model") or task.get("preferred_model")
                if backend_name == "codex" and not configured_model:
                    configured_model = (cfg.get("codex", {}) or {}).get("model")
                if backend_name == "claude" and not configured_model:
                    configured_model = (cfg.get("claude", {}) or {}).get("model")
                model = configured_model or session.get("model")
                raw_requested_model = getattr(args, "requested_model", None)
                requested_model = None if raw_requested_model == "" else raw_requested_model
                if raw_requested_model is None:
                    requested_model = configured_model if configured_model is not None else session.get("requested_model", model)
                codex_config = codex_config_for_model((cfg.get("codex", {}) or {}), model) if backend_name == "codex" else None
                claude_config = claude_config_for_model((cfg.get("claude", {}) or {}), model) if backend_name == "claude" else None
                command_model = claude_cli_model(model) if backend_name == "claude" else codex_cli_model(codex_config, model) if backend_name == "codex" else model
                resolved_model = claude_resolved_model(claude_config, model) if backend_name == "claude" else codex_resolved_model(codex_config, model) if backend_name == "codex" else resolve_model(backend_name, command_model)
                session["requested_model"] = requested_model
                session["resolved_model"] = resolved_model
                session["model"] = resolved_model or command_model or model
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
                        "requested_model": requested_model,
                        "resolved_model": resolved_model,
                        "proxy_enabled": bool((agent or {}).get("proxy_enabled")),
                    },
                )
                proxy_env = proxy_env_for_agent(agent or {}, task, plan, cfg)
                prompt, prompt_metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    args.target,
                    item,
                    args.prompt_prefix,
                    backend=backend_name,
                    requested_model=requested_model,
                    resolved_model=resolved_model,
                )
                try:
                    prompt_metrics["prompt_ref"] = save_prompt_artifact(root, run_id, item_task_id, args.target, prompt)
                except OSError as exc:
                    prompt_metrics["prompt_ref_error"] = str(exc)
                append_event(root, run_id, "agent_prompt_metrics", {"source": source_name, **prompt_metrics})
                gate_model_family = model_family_for_guidance(backend_name, requested_model, resolved_model)
                git_before = _git_workspace_snapshot(workspace) if gate_model_family else None
                progress_heartbeat = (
                    AgentProgressHeartbeat(
                        root,
                        run_id,
                        task_id=item_task_id,
                        agent_id=agent_id,
                        role=item.get("role") or "main",
                        model_family=gate_model_family,
                    )
                    if gate_model_family and manages_task_status and item_task_id
                    else None
                )
                try:
                    runner_session = session
                    if backend_name == "claude":
                        exit_code, reply, returned_session = run_claude_exec(
                            prompt,
                            cwd=workspace,
                            output_file=output_file,
                            claude_bin=getattr(args, "claude_bin", "claude"),
                            model=command_model,
                            permission_mode=claude_permission_mode("research", sandbox),
                            extra_args=args.extra_arg or [],
                            events_file=events_file,
                            run_id=run_id,
                            task_id=item_task_id,
                            source=source_name,
                            target=args.target,
                            session=session,
                            proxy_env=proxy_env,
                            claude_config=claude_config,
                            event_callback=progress_heartbeat.handle_event if progress_heartbeat else None,
                        )
                    else:
                        exit_code, reply, returned_session = run_codex_exec(
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
                            codex_config=codex_config,
                        )
                    session = returned_session if returned_session is not None else runner_session
                except Exception as exc:
                    traceback.print_exc()
                    exit_code = 1
                    reply = f"{backend_name.title()} backend crashed while handling agent turn: {type(exc).__name__}: {exc}"
                if session:
                    save_session(root, session)
                if is_memo_report:
                    try:
                        write_memo_report_result(root, run_id, item, reply, exit_code)
                    except (KeyError, ValueError, SystemExit) as exc:
                        append_event(
                            root,
                            run_id,
                            "task_memo_report_failed",
                            {"task_id": item_task_id, "error": str(exc), "reason": "writeback_failed"},
                        )
                    if exit_code != 0 or not reply.strip():
                        append_agent_error_notice(
                            root,
                            run_id,
                            source=source_name,
                            target=args.target,
                            task_id=item_task_id,
                            agent_id=agent_id,
                            exit_code=exit_code,
                            reply=reply,
                            role=item.get("role") or "main",
                        )
                    append_event(root, run_id, "agent_finished", {"source": source_name, "target": args.target, "task_id": item_task_id, "exit_code": exit_code})
                    print(f"{args.sender} -> aha: MEMO report {'generated' if exit_code == 0 and reply.strip() else 'failed'}", flush=True)
                    if worker_task_id:
                        save_chat_offset(offset_file, item_offset)
                        mark_backend_stopped(root, run_id, args.target, task_id=worker_task_id, pid=os.getpid())
                        return exit_code
                    if args.once:
                        save_chat_offset(offset_file, item_offset)
                        return exit_code
                    continue
                if exit_code == 0 and reply.strip():
                    schema_error = _reply_action_schema_error(reply)
                    if schema_error and item.get("coordination") != "action_schema_retry":
                        append_agent_retry_request(
                            root,
                            run_id,
                            target=args.target,
                            task_id=item_task_id,
                            agent_id=agent_id,
                            item=item,
                            message=action_schema_retry_message(schema_error),
                            gate="action_schema_retry",
                            reason=schema_error,
                            manages_task_status=manages_task_status,
                        )
                        append_event(root, run_id, "agent_finished", {"source": source_name, "target": args.target, "task_id": item_task_id, "exit_code": exit_code})
                        print(f"AHA requested {args.target} retry: invalid action schema ({schema_error})", flush=True)
                        if worker_task_id:
                            finish_retry_turn(
                                root,
                                run_id,
                                args,
                                worker_task_id=worker_task_id,
                                offset_file=offset_file,
                                item_offset=item_offset,
                                backend_name=backend_name,
                                model=configured_model or requested_model or model,
                                sandbox=sandbox,
                                approval=requested_approval,
                                gate="action_schema_retry",
                            )
                            return exit_code
                        if args.once:
                            save_chat_offset(offset_file, item_offset)
                            return exit_code
                        continue
                    git_after = _git_workspace_snapshot(workspace) if gate_model_family else None
                    commit_policy_retry_active = item.get("coordination") == "commit_policy_retry"
                    expected_generated_by = generated_by_for_backend_model(backend_name, resolved_model or requested_model)
                    commit_errors = (
                        _git_commit_policy_errors(
                            workspace,
                            git_before,
                            git_after,
                            expected_generated_by=expected_generated_by,
                            force_head=commit_policy_retry_active,
                        )
                        if gate_model_family
                        else []
                    )
                    if commit_errors:
                        reason = "; ".join(commit_errors)
                        if commit_policy_retry_active:
                            append_agent_completion_blocked(
                                root,
                                run_id,
                                task_id=item_task_id,
                                agent_id=agent_id,
                                item=item,
                                reason=f"commit message policy violation: {reason}",
                                manages_task_status=manages_task_status,
                            )
                        else:
                            append_agent_retry_request(
                                root,
                                run_id,
                                target=args.target,
                                task_id=item_task_id,
                                agent_id=agent_id,
                                item=item,
                                message=commit_policy_retry_message(commit_errors, expected_generated_by),
                                gate="commit_policy_retry",
                                reason=reason,
                                manages_task_status=manages_task_status,
                            )
                        append_event(root, run_id, "agent_finished", {"source": source_name, "target": args.target, "task_id": item_task_id, "exit_code": exit_code})
                        print(f"AHA blocked {args.target} normal completion: commit message policy violation", flush=True)
                        if worker_task_id:
                            if commit_policy_retry_active:
                                save_chat_offset(offset_file, item_offset)
                                mark_backend_stopped(root, run_id, args.target, task_id=worker_task_id, pid=os.getpid())
                            else:
                                finish_retry_turn(
                                    root,
                                    run_id,
                                    args,
                                    worker_task_id=worker_task_id,
                                    offset_file=offset_file,
                                    item_offset=item_offset,
                                    backend_name=backend_name,
                                    model=configured_model or requested_model or model,
                                    sandbox=sandbox,
                                    approval=requested_approval,
                                    gate="commit_policy_retry",
                                )
                            return exit_code
                        if args.once:
                            save_chat_offset(offset_file, item_offset)
                            return exit_code
                        continue
                    task_update_retry_active = item.get("coordination") == "task_update_required_retry"
                    task_update_required = bool(
                        gate_model_family
                        and manages_task_status
                        and not _reply_has_action_type(reply, "record_task_update")
                        and (_git_workspace_changed(git_before, git_after) or task_update_retry_active)
                    )
                    if task_update_required:
                        reason = "repository changes require a record_task_update action before normal completion"
                        if task_update_retry_active:
                            append_agent_completion_blocked(
                                root,
                                run_id,
                                task_id=item_task_id,
                                agent_id=agent_id,
                                item=item,
                                reason=reason,
                                manages_task_status=manages_task_status,
                            )
                        else:
                            append_agent_retry_request(
                                root,
                                run_id,
                                target=args.target,
                                task_id=item_task_id,
                                agent_id=agent_id,
                                item=item,
                                message=task_update_required_retry_message(),
                                gate="task_update_required_retry",
                                reason=reason,
                                manages_task_status=manages_task_status,
                            )
                        append_event(root, run_id, "agent_finished", {"source": source_name, "target": args.target, "task_id": item_task_id, "exit_code": exit_code})
                        print(f"AHA blocked {args.target} normal completion: {reason}", flush=True)
                        if worker_task_id:
                            if task_update_retry_active:
                                save_chat_offset(offset_file, item_offset)
                                mark_backend_stopped(root, run_id, args.target, task_id=worker_task_id, pid=os.getpid())
                            else:
                                finish_retry_turn(
                                    root,
                                    run_id,
                                    args,
                                    worker_task_id=worker_task_id,
                                    offset_file=offset_file,
                                    item_offset=item_offset,
                                    backend_name=backend_name,
                                    model=configured_model or requested_model or model,
                                    sandbox=sandbox,
                                    approval=requested_approval,
                                    gate="task_update_required_retry",
                                )
                            return exit_code
                        if args.once:
                            save_chat_offset(offset_file, item_offset)
                            return exit_code
                        continue
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
                        elif host_result.get("waiting"):
                            set_agent_status(root, run_id, item_task_id, "main", "waiting", waiting_reason="subagents")
                            set_task_status(root, run_id, item_task_id, "running")
                        elif host_result.get("routed_to_main"):
                            set_agent_status(root, run_id, item_task_id, "main", "pending")
                            set_task_status(root, run_id, item_task_id, "running")
                        elif host_result.get("routed_to_browser"):
                            set_task_status(root, run_id, item_task_id, "awaiting_user", exit_code)
                        elif host_result.get("executed"):
                            request_round_summary_if_ready(root, run_id, item_task_id)
                            set_task_status(root, run_id, item_task_id, "running")
                        else:
                            next_task_status = "awaiting_user" if final_status == "completed" else final_status
                            set_task_status(root, run_id, item_task_id, next_task_status, exit_code)
                    if exit_code != 0:
                        append_agent_error_notice(
                            root,
                            run_id,
                            source=source_name,
                            target=args.target,
                            task_id=item_task_id,
                            agent_id=agent_id,
                            exit_code=exit_code,
                            reply=reply,
                            role=item.get("role") or "main",
                        )
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
                    delegating_actions = [action for action in executed if action.get("type") in {"route_to_agent", "spawn_sub"}]
                    defer_supervision_for_subagents = False
                    if (
                        agent_id == "main"
                        and manages_task_status
                        and not writes_task_final
                        and not is_agent_command
                        and message_triggers_supervision(item)
                    ):
                        try:
                            supervision_detail = task_snapshot(root, run_id, item_task_id) if item_task_id else None
                        except KeyError:
                            supervision_detail = None
                        defer_supervision_for_subagents = bool(
                            delegating_actions
                            or (
                                supervision_detail is not None
                                and task_has_incomplete_sub_agents(supervision_detail["task"])
                            )
                        )
                        if defer_supervision_for_subagents:
                            append_event(
                                root,
                                run_id,
                                "supervision_deferred",
                                {
                                    "task_id": item_task_id,
                                    "target": agent_id,
                                    "reason": "subagents",
                                    "delegating_actions": [action.get("type") for action in delegating_actions],
                                },
                            )
                        else:
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
                                source_message=item,
                                cfg=cfg,
                                run=run,
                            )
                            if host_result:
                                executed.extend(host_result.get("executed", []))
                                supervision_routed_to_main = bool(host_result.get("routed_to_main"))
                                supervision_waiting_for_host = bool(host_result.get("routed_to_host"))
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
                            final_context = item.get("final_context") if isinstance(item.get("final_context"), dict) else None
                            write_task_result(root, run_id, item_task_id, reply.strip(), final_context=final_context)
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
                                if supervision_waiting_for_host:
                                    set_agent_status(
                                        root,
                                        run_id,
                                        item_task_id,
                                        agent_id,
                                        "waiting",
                                        waiting_reason="host",
                                    )
                                    set_task_status(root, run_id, item_task_id, "running")
                                else:
                                    set_agent_status(root, run_id, item_task_id, agent_id, final_status, exit_code)
                                    set_task_status(root, run_id, item_task_id, "awaiting_user")
                            elif delegating_actions or task_has_incomplete_sub_agents(detail["task"]):
                                set_agent_status(root, run_id, item_task_id, agent_id, "waiting", waiting_reason="subagents")
                                set_task_status(root, run_id, item_task_id, "running")
                            elif supervision_routed_to_main:
                                set_agent_status(root, run_id, item_task_id, agent_id, "pending")
                                set_task_status(root, run_id, item_task_id, "running")
                            elif supervision_waiting_for_host:
                                set_agent_status(root, run_id, item_task_id, agent_id, "waiting", waiting_reason="host")
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
                            append_visible_sub_agent_result(
                                root,
                                run_id,
                                item_task_id,
                                agent_id,
                                sub_agent_failure_message(agent_id, "agent_execution_failed"),
                                coordination="sub_agent_failed",
                            )
                            request_round_summary_if_ready(root, run_id, item_task_id)
                            set_task_status(root, run_id, item_task_id, "running")
                            exit_after_message = bool(worker_task_id)
                        else:
                            detail = task_snapshot(root, run_id, item_task_id)
                            if exit_code == 0 and task_has_incomplete_sub_agents(detail["task"]):
                                empty_reply_waiting_for_subagents = True
                                set_agent_status(root, run_id, item_task_id, agent_id, "waiting", exit_code, waiting_reason="subagents")
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
                        append_agent_error_notice(
                            root,
                            run_id,
                            source=source_name,
                            target=args.target,
                            task_id=item_task_id,
                            agent_id=agent_id,
                            exit_code=exit_code,
                            reply=reply,
                            role=item.get("role") or "main",
                        )
                if worker_backend_should_exit_after_turn(root, run_id, item_task_id, worker_task_id, inbox, item_offset, target=args.target):
                    exit_after_message = True
                append_event(root, run_id, "agent_finished", {"source": source_name, "target": args.target, "task_id": item_task_id, "exit_code": exit_code})
                if item_task_id and backend_name in {"codex", "claude"}:
                    auto_compact_agent_context_after_turn(root, run_id, item_task_id, agent_id)
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
            elif worker_backend_should_exit_after_turn(root, run_id, worker_task_id, worker_task_id, inbox, offset, target=args.target):
                mark_backend_stopped(root, run_id, args.target, task_id=worker_task_id, pid=os.getpid())
                return 0
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
