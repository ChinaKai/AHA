from __future__ import annotations

import json
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.backend_runtime import (
    PROCESS_AGENT_BACKENDS,
    backend_status,
    start_backend,
    stop_backend,
)
from aha_cli.services.chat import chat_offset_path, save_chat_offset
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.services.session_compact import compact_reset_backend_session
from aha_cli.store.filesystem import (
    append_event,
    append_message,
    append_task_round,
    inbox_path,
    list_task_rounds,
    mark_task_coordination,
    reopen_task,
    run_dir,
    set_agent_status,
    set_task_status,
    task_snapshot,
)
from aha_cli.web.status import (
    TERMINAL_TASK_STATUSES,
    consume_agent_recovery_context,
    merge_recovery_context_message,
)


def parse_optional_bool(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{field_name} must be a boolean")


def realtime_debug_log(source: str, **fields: object) -> None:
    root = fields.pop("_root", None)
    run_id = str(fields.get("run_id") or "")
    payload = {"ts": utc_now(), "source": source, **fields}
    line = "[aha realtime] " + json.dumps(payload, ensure_ascii=False, default=str)
    print(line, flush=True)
    if isinstance(root, Path) and run_id:
        try:
            log_dir = run_dir(root, run_id) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "realtime-debug.log").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


def format_aha_command(root: Path, run_id: str, task_id: str | None, command: str, target: str = "main") -> str:
    parts = command.split()
    name = parts[1] if len(parts) > 1 else "help"
    if name == "help":
        return "\n".join(
            [
                "AHA commands:",
                "- /aha help: show AHA commands",
                "- /aha status: show selected task status",
                "- /aha agents: list selected task agents",
                "- /aha checkpoint <summary>: record a task journal checkpoint",
                "- /aha final: ask task-main to generate the Final and complete the task",
                "- /aha finalize: alias for /aha final",
                "- /aha complete: alias for /aha final",
                "- /aha reopen: cancel completion and allow follow-up messages",
                "- /aha interrupt: interrupt the selected agent's current turn",
                "- /aha session compact-reset: compact and reset selected agent backend session",
                "",
                "Agent command:",
                "- /agent <command>: route /<command> to the selected agent",
            ]
        )
    if not task_id:
        return "No task is selected."
    try:
        detail = task_snapshot(root, run_id, task_id)
    except KeyError:
        return f"Task not found: {task_id}"
    task = detail["task"]
    if name == "status":
        return "\n".join(
            [
                f"Task: {task['id']} {task['title']}",
                f"Status: {task.get('status')} exit={task.get('exit_code')}",
                f"Backend: {task.get('preferred_backend')} model={task.get('preferred_model') or 'default'}",
                f"Workspace: {task.get('workspace_path') or '-'}",
            ]
        )
    if name == "agents":
        lines = ["Agents:"]
        for agent in task.get("agents", []):
            lines.append(
                f"- {agent.get('id')} role={agent.get('role')} backend={agent.get('backend')} "
                f"sandbox={agent.get('sandbox') or task.get('preferred_sandbox') or '-'} "
                f"approval={agent.get('approval') or task.get('preferred_approval') or '-'} "
                f"proxy={'on' if agent.get('proxy_enabled') else 'off'} "
                f"assignment={agent.get('assignment') or agent.get('created_reason') or '-'}"
            )
        return "\n".join(lines)
    if name == "checkpoint":
        return "Use `/aha checkpoint <summary>` from the selected task conversation to record a journal checkpoint."
    if name in {"final", "finalize"}:
        return "Use `/aha final` from the selected task conversation to ask task-main to generate the Final and complete the task."
    if name in {"complete", "done"}:
        return "Use `/aha complete` as an alias for `/aha final`."
    if name in {"reopen", "resume"}:
        return "Use `/aha reopen` from the selected task conversation to unlock the task for follow-up."
    if name == "session" and len(parts) > 2 and parts[2] == "compact-reset":
        return "Use `/aha session compact-reset` from the selected task conversation to archive the current backend session and start a fresh one."
    return f"Unknown AHA command: /aha {name}. Try /aha help."


def compact_reset_selected_agent(root: Path, run_id: str, task_id: str | None, target: str, *, restart: bool = True) -> tuple[str, dict]:
    if not task_id:
        return "No task is selected.", {"ok": False, "reason": "no_task"}
    try:
        payload = compact_reset_backend_session(root, run_id, task_id, target or "main", reason="manual", restart=restart)
    except KeyError as exc:
        return f"Task or agent not found: {exc}", {"ok": False, "reason": "not_found"}
    except ValueError as exc:
        return str(exc), {"ok": False, "reason": "invalid"}
    return (
        f"Compact-reset completed for {task_id}/{target or 'main'}. "
        f"Archived `{payload.get('old_backend_session_id')}` and wrote `{payload.get('summary_path')}`.",
        payload,
    )


def format_task_journal_for_prompt(rounds: list[dict]) -> str:
    if not rounds:
        return "Task journal (chronological ordered list):\n1. (empty)"
    lines = ["Task journal (chronological ordered list):"]
    for index, item in enumerate(rounds[-50:], start=1):
        lines.append(f"{index}. {item.get('summary')}")
        lines.append(f"   - round_id: {item.get('round_id')}")
        lines.append(f"   - trigger: {item.get('trigger')}")
        changed_files = item.get("changed_files") or []
        verification = item.get("verification") or []
        risks = item.get("risks") or []
        if changed_files:
            lines.append(f"   - files: {', '.join(str(path) for path in changed_files)}")
        if verification:
            lines.append(f"   - verification: {'; '.join(str(check) for check in verification)}")
        if risks:
            lines.append(f"   - risks: {'; '.join(str(risk) for risk in risks)}")
    return "\n".join(lines)


def finalization_prompt(task_id: str, title: str, rounds: list[dict] | None = None) -> str:
    return render_prompt_template(
        "finalization.md",
        task_id=task_id,
        title=title,
        task_journal=format_task_journal_for_prompt(rounds or []),
    )


def request_task_finalization(root: Path, run_id: str, task_id: str | None, command: str) -> str:
    if not task_id:
        return "No task is selected."
    try:
        detail = task_snapshot(root, run_id, task_id)
    except KeyError:
        return f"Task not found: {task_id}"
    task = detail["task"]
    rounds = list_task_rounds(root, run_id, task_id)
    mark_task_coordination(root, run_id, task_id, final_summary_requested_at=utc_now(), final_summary_completed_at="")
    if task.get("status") not in TERMINAL_TASK_STATUSES:
        set_task_status(root, run_id, task_id, "running")
    append_message(
        root,
        run_id,
        "main",
        finalization_prompt(task_id, str(task.get("title", "")), rounds),
        sender="aha",
        task_id=task_id,
        role="main",
        from_agent="aha",
        to_agent="main",
        command_namespace="aha",
        original_command=command,
        result_policy="finalize",
    )
    append_event(root, run_id, "task_final_requested", {"task_id": task_id, "target": "main", "policy": "finalize"})
    return f"Finalization requested for {task_id}. Task-main will write the Final when it finishes."


def prepare_task_main_autostart(root: Path, run_id: str, task_id: str | None) -> dict | None:
    if not task_id:
        return None
    autostart = message_backend_autostart_config(root, run_id, task_id, "main")
    if autostart:
        ensure_chat_offset_before_message(root, run_id, task_id, "main")
    return autostart


def start_prepared_backend(root: Path, run_id: str, autostart: dict | None) -> dict | None:
    if not autostart:
        return None
    return start_backend(
        root,
        run_id,
        autostart["target"],
        backend=autostart["backend"],
        model=autostart["model"],
        sandbox=autostart["sandbox"],
        approval=autostart["approval"],
        from_start=False,
        task_id=autostart["task_id"],
    )


def request_task_finalization_with_backend(
    root: Path,
    run_id: str,
    task_id: str | None,
    command: str,
    *,
    autostart_backend: bool = True,
) -> dict:
    autostart = prepare_task_main_autostart(root, run_id, task_id) if autostart_backend else None
    message = request_task_finalization(root, run_id, task_id, command)
    payload: dict = {"message": message}
    backend = start_prepared_backend(root, run_id, autostart)
    if backend:
        payload["backend"] = backend
    return payload


def parse_task_proxy_fields(payload: dict) -> dict[str, object]:
    fields: dict[str, object] = {}
    if "proxy_enabled" in payload:
        fields["proxy_enabled"] = parse_optional_bool(payload["proxy_enabled"], "proxy_enabled")
    if "http_proxy" in payload:
        fields["http_proxy"] = str(payload.get("http_proxy", "") or "")
    if "https_proxy" in payload:
        fields["https_proxy"] = str(payload.get("https_proxy", "") or "")
    if "no_proxy" in payload:
        fields["no_proxy"] = str(payload.get("no_proxy", "") or "")
    return fields


def parse_task_supervision_fields(payload: dict) -> dict[str, object]:
    fields: dict[str, object] = {}
    if "mode" in payload:
        fields["mode"] = str(payload.get("mode", "") or "")
    if "host_backend" in payload:
        fields["host_backend"] = str(payload.get("host_backend", "") or "")
    if "real_agent_enabled" in payload:
        fields["real_agent_enabled"] = parse_optional_bool(payload["real_agent_enabled"], "real_agent_enabled")
    if "max_rounds" in payload:
        try:
            fields["max_rounds"] = int(payload.get("max_rounds") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_rounds must be an integer") from exc
    return fields


def record_task_checkpoint(root: Path, run_id: str, task_id: str | None, command: str) -> str:
    if not task_id:
        return "No task is selected."
    parts = command.split(maxsplit=2)
    summary = parts[2].strip() if len(parts) > 2 else ""
    if not summary:
        return "Usage: /aha checkpoint <summary>"
    try:
        record = append_task_round(root, run_id, task_id, {"trigger": "manual", "summary": summary, "agents": ["browser"]})
    except KeyError:
        return f"Task not found: {task_id}"
    return f"Checkpoint recorded for {task_id}: {record['round_id']}"


def complete_selected_task(root: Path, run_id: str, task_id: str | None) -> str:
    return request_task_finalization(root, run_id, task_id, "/aha complete")


def reopen_selected_task(root: Path, run_id: str, task_id: str | None) -> str:
    if not task_id:
        return "No task is selected."
    try:
        reopen_task(root, run_id, task_id)
    except SystemExit:
        return f"Task not found: {task_id}"
    return f"{task_id} reopened. Follow-up messages are allowed again."


def interrupt_selected_agent(root: Path, run_id: str, task_id: str | None, target: str) -> tuple[str, dict]:
    if not task_id:
        return "No task is selected.", {"interrupted": False, "reason": "no_task"}
    try:
        detail = task_snapshot(root, run_id, task_id)
    except KeyError:
        return f"Task not found: {task_id}", {"interrupted": False, "reason": "task_not_found"}
    task = detail["task"]
    agent_id = target or "main"
    if not any(str(agent.get("id") or "") == agent_id for agent in task.get("agents", [])):
        return f"Agent not found: {agent_id}", {"interrupted": False, "reason": "agent_not_found", "agent_id": agent_id}
    state = backend_status(root, run_id, agent_id, task_id=task_id)
    if state.get("status") != "busy":
        return (
            f"No active turn to interrupt for {agent_id} on {task_id}.",
            {"interrupted": False, "reason": "not_busy", "agent_id": agent_id, "task_id": task_id, "backend": state},
        )
    stopped = stop_backend(root, run_id, agent_id, task_id=task_id, timeout=2.0)
    offset_file = chat_offset_path(run_dir(root, run_id), agent_id, task_id)
    inbox = inbox_path(root, run_id, agent_id)
    save_chat_offset(offset_file, inbox.stat().st_size if inbox.exists() else 0)
    set_agent_status(root, run_id, task_id, agent_id, "interrupted")
    set_task_status(root, run_id, task_id, "awaiting_user")
    append_event(
        root,
        run_id,
        "agent_interrupted",
        {"task_id": task_id, "agent_id": agent_id, "target": agent_id, "backend": stopped},
    )
    return (
        f"Interrupted {agent_id} on {task_id}. Pending user messages were not sent automatically.",
        {"interrupted": True, "agent_id": agent_id, "task_id": task_id, "backend": stopped},
    )


def format_agent_command(root: Path, run_id: str, task_id: str | None, agent_id: str | None, command: str) -> tuple[bool, str | None, str | None]:
    del root, run_id, task_id, agent_id
    suffix = command.removeprefix("/agent").strip()
    if not suffix:
        return True, None, "Usage: /agent <command> routes /<command> to the selected agent. Example: /agent status -> /status"
    return False, suffix if suffix.startswith("/") else f"/{suffix}", None


def handle_slash_command(root: Path, run_id: str, payload: dict, message: str, task_id: str | None) -> tuple[bool, str | None, dict]:
    sender = str(payload.get("sender", "browser") or "browser")
    stripped = message.strip()
    backend_autostart = None
    if not stripped.startswith("/"):
        return False, message, {}
    if stripped == "/":
        reply = format_aha_command(root, run_id, task_id, "/aha help", str(payload.get("to_agent") or payload.get("target") or "main"))
    elif stripped == "/agent" or stripped.startswith("/agent "):
        handled, agent_message, reply = format_agent_command(root, run_id, task_id, str(payload.get("to_agent") or payload.get("target") or "main"), stripped)
        if not handled:
            if agent_message:
                return False, agent_message, {"command_namespace": "agent", "original_command": stripped}
            reply = reply or "Usage: /agent send <message>"
    elif stripped == "/aha" or stripped.startswith("/aha "):
        target = str(payload.get("to_agent", "") or payload.get("target", "") or "main")
        append_message(root, run_id, "aha", stripped, sender=sender, task_id=task_id, role="aha", from_agent=sender, to_agent="aha", agent_id=target)
        parts = stripped.split()
        name = parts[1] if len(parts) > 1 else "help"
        if name in {"final", "finalize"}:
            backend_autostart = prepare_task_main_autostart(root, run_id, task_id)
            reply = request_task_finalization(root, run_id, task_id, stripped)
        elif name == "checkpoint":
            reply = record_task_checkpoint(root, run_id, task_id, stripped)
        elif name in {"complete", "done"}:
            backend_autostart = prepare_task_main_autostart(root, run_id, task_id)
            reply = complete_selected_task(root, run_id, task_id)
        elif name in {"reopen", "resume"}:
            reply = reopen_selected_task(root, run_id, task_id)
        elif name in {"interrupt", "stop"}:
            reply, interrupt_payload = interrupt_selected_agent(root, run_id, task_id, target)
        elif name == "session" and len(parts) > 2 and parts[2] == "compact-reset":
            reply, compact_reset_payload = compact_reset_selected_agent(root, run_id, task_id, target, restart=True)
        else:
            reply = format_aha_command(root, run_id, task_id, stripped, target)
    else:
        reply = f"Unknown command: {stripped.split()[0]}. Use /aha help or /agent <command>."

    append_event(root, run_id, "aha_command_handled", {"task_id": task_id, "command": stripped})
    response = append_message(
        root,
        run_id,
        "browser",
        reply,
        sender="AHA",
        task_id=task_id,
        role="aha",
        from_agent="aha",
        to_agent="browser",
        agent_id=target if stripped.startswith("/aha") else None,
    )
    command_response = {"message": response}
    if backend_autostart:
        command_response["backend_autostart"] = backend_autostart
    if "interrupt_payload" in locals():
        command_response["interrupt"] = interrupt_payload
    if "compact_reset_payload" in locals():
        command_response["compact_reset"] = compact_reset_payload
    return True, None, command_response


def message_backend_autostart_config(root: Path, run_id: str, task_id: str | None, target_id: str) -> dict | None:
    if not task_id or not target_id:
        return None
    try:
        detail = task_snapshot(root, run_id, task_id)
    except KeyError:
        return None
    task = detail["task"]
    if is_task_supervision_host_target(task, target_id):
        return None
    agent = next((item for item in task.get("agents", []) if item.get("id") == target_id), None)
    if not agent:
        return None
    backend = str(agent.get("backend") or task.get("preferred_backend") or "codex")
    if backend not in PROCESS_AGENT_BACKENDS:
        return None
    state = backend_status(root, run_id, target_id, task_id=task_id)
    if state.get("status") != "stopped":
        return None
    return {
        "backend": backend,
        "target": target_id,
        "task_id": task_id,
        "model": agent.get("model") or task.get("preferred_model"),
        "sandbox": agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
        "approval": agent.get("approval") or task.get("preferred_approval") or "never",
    }


def is_task_supervision_host_target(task: dict, target_id: str | None) -> bool:
    supervision = task.get("supervision") if isinstance(task.get("supervision"), dict) else {}
    host_agent_id = str(supervision.get("host_agent_id") or "host")
    return bool(
        target_id
        and target_id == host_agent_id
        and supervision.get("mode") == "assisted"
        and supervision.get("real_agent_enabled")
        and supervision.get("host_backend") != "stub"
    )


def is_supervision_host_message(root: Path, run_id: str, task_id: str | None, target_id: str) -> bool:
    if not task_id:
        return False
    try:
        return is_task_supervision_host_target(task_snapshot(root, run_id, task_id)["task"], target_id)
    except KeyError:
        return False


def save_chat_offset_after_message(root: Path, run_id: str, task_id: str, target_id: str) -> None:
    inbox = inbox_path(root, run_id, target_id)
    offset_file = chat_offset_path(run_dir(root, run_id), target_id, task_id)
    save_chat_offset(offset_file, inbox.stat().st_size if inbox.exists() else 0)


def ensure_chat_offset_before_message(root: Path, run_id: str, task_id: str, target_id: str) -> None:
    offset_file = chat_offset_path(run_dir(root, run_id), target_id, task_id)
    if offset_file.exists():
        return
    inbox = inbox_path(root, run_id, target_id)
    save_chat_offset(offset_file, inbox.stat().st_size if inbox.exists() else 0)


def task_locked_for_messages(root: Path, run_id: str, task_id: str | None) -> str | None:
    if not task_id:
        return None
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return None
    status = str(task.get("status") or "")
    return status if status in TERMINAL_TASK_STATUSES else None


def handle_send_payload(root: Path, run_id: str, payload: dict) -> dict:
    message = str(payload.get("message", "")).strip()
    task_id = str(payload.get("task_id", "")).strip() or None
    role = str(payload.get("role", "")).strip() or None
    target_id = str(payload.get("target", "")).strip()
    if not target_id:
        target_id = task_id if role == "sub" and task_id else "main"
    if not message:
        raise ValueError("message cannot be empty")

    realtime_debug_log(
        "api.send",
        _root=root,
        phase="request",
        run_id=run_id,
        task_id=task_id or "",
        target=target_id,
        role=role or "",
        sender=str(payload.get("sender", "") or ""),
        from_agent=str(payload.get("from_agent", "") or ""),
        to_agent=str(payload.get("to_agent", "") or ""),
        message_len=len(message),
        is_command=message.startswith("/"),
    )
    handled, agent_message, command_payload = handle_slash_command(root, run_id, payload, message, task_id)
    if handled:
        backend_autostart = command_payload.pop("backend_autostart", None)
        backend = start_prepared_backend(root, run_id, backend_autostart)
        if backend:
            command_payload["backend"] = backend
        realtime_debug_log(
            "api.send",
            _root=root,
            phase="handled_command",
            run_id=run_id,
            task_id=task_id or "",
            target=target_id,
            backend_started=bool(backend),
            reply_keys=sorted(command_payload.keys()),
        )
        return {"ok": True, "handled_by": "aha", **command_payload}

    locked_status = task_locked_for_messages(root, run_id, task_id)
    if locked_status:
        raise ValueError(f"task {task_id} is {locked_status}; use /aha reopen before sending follow-up messages")

    supervision_host_message = is_supervision_host_message(root, run_id, task_id, target_id)
    autostart = message_backend_autostart_config(root, run_id, task_id, target_id)
    if autostart and task_id:
        ensure_chat_offset_before_message(root, run_id, task_id, target_id)

    message = agent_message or message
    recovery_context = consume_agent_recovery_context(root, run_id, task_id, target_id)
    if recovery_context:
        message = merge_recovery_context_message(recovery_context, message)
    sent = append_message(
        root,
        run_id,
        target_id,
        message,
        str(payload.get("sender", "browser") or "browser"),
        task_id=task_id,
        role=role,
        from_agent=str(payload.get("from_agent", "") or "") or None,
        to_agent=str(payload.get("to_agent", "") or "") or None,
        command_namespace=str(command_payload.get("command_namespace", "") or "") or None,
        original_command=str(command_payload.get("original_command", "") or "") or None,
        result_policy=str(command_payload.get("result_policy", "") or "") or None,
    )
    response = {"ok": True, "message": sent}
    if supervision_host_message and task_id:
        save_chat_offset_after_message(root, run_id, task_id, target_id)
    if autostart:
        response["backend"] = start_backend(
            root,
            run_id,
            autostart["target"],
            backend=autostart["backend"],
            model=autostart["model"],
            sandbox=autostart["sandbox"],
            approval=autostart["approval"],
            from_start=False,
            task_id=autostart["task_id"],
        )
    realtime_debug_log(
        "api.send",
        _root=root,
        phase="stored",
        run_id=run_id,
        task_id=task_id or "",
        target=target_id,
        backend_started=bool(response.get("backend")),
        response_keys=sorted(response.keys()),
    )
    return response


def start_dispatched_task_backend(root: Path, run_id: str, task: dict, dispatch: bool) -> dict | None:
    if not dispatch:
        return None
    task_id = str(task.get("id") or "")
    autostart = message_backend_autostart_config(root, run_id, task_id, "main")
    if not autostart:
        return None
    return start_backend(
        root,
        run_id,
        "main",
        backend=autostart["backend"],
        model=autostart["model"],
        sandbox=autostart["sandbox"],
        approval=autostart["approval"],
        from_start=True,
        task_id=task_id,
    )

__all__ = [
    "parse_optional_bool",
    "realtime_debug_log",
    "format_aha_command",
    "compact_reset_selected_agent",
    "format_task_journal_for_prompt",
    "finalization_prompt",
    "request_task_finalization",
    "prepare_task_main_autostart",
    "start_prepared_backend",
    "request_task_finalization_with_backend",
    "parse_task_proxy_fields",
    "parse_task_supervision_fields",
    "record_task_checkpoint",
    "complete_selected_task",
    "reopen_selected_task",
    "interrupt_selected_agent",
    "format_agent_command",
    "handle_slash_command",
    "message_backend_autostart_config",
    "is_task_supervision_host_target",
    "is_supervision_host_message",
    "save_chat_offset_after_message",
    "ensure_chat_offset_before_message",
    "task_locked_for_messages",
    "handle_send_payload",
    "start_dispatched_task_backend",
]
