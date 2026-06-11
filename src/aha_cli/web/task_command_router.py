from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from aha_cli.store.filesystem import append_event, append_message


@dataclass(frozen=True)
class SlashCommandHandlers:
    format_aha_command: Callable[[Path, str, str | None, str, str], str]
    format_agent_command: Callable[[Path, str, str | None, str | None, str], tuple[bool, str | None, str | None]]
    record_task_checkpoint: Callable[[Path, str, str | None, str], str]
    request_task_finalization: Callable[[Path, str, str | None, str], str]
    reopen_selected_task: Callable[[Path, str, str | None], str]
    interrupt_selected_agent: Callable[[Path, str, str | None, str], tuple[str, dict]]
    compact_reset_selected_agent: Callable[[Path, str, str | None, str], tuple[str, dict]]
    transition_selected_agent_phase: Callable[[Path, str, str | None, str, str], tuple[str, dict]]
    prepare_task_main_autostart: Callable[[Path, str, str | None], dict | None]
    append_message: Callable[..., dict]
    append_event: Callable[..., dict]


def default_slash_command_handlers() -> SlashCommandHandlers:
    from aha_cli.web.task_command_actions import (
        compact_reset_selected_agent,
        interrupt_selected_agent,
        record_task_checkpoint,
        request_task_finalization,
        reopen_selected_task,
        transition_selected_agent_phase,
    )
    from aha_cli.web.task_command_actions import prepare_task_main_autostart
    from aha_cli.web.task_command_format import format_agent_command, format_aha_command

    return SlashCommandHandlers(
        format_aha_command=format_aha_command,
        format_agent_command=format_agent_command,
        record_task_checkpoint=record_task_checkpoint,
        request_task_finalization=request_task_finalization,
        reopen_selected_task=reopen_selected_task,
        interrupt_selected_agent=interrupt_selected_agent,
        compact_reset_selected_agent=lambda root, run_id, task_id, target: compact_reset_selected_agent(
            root,
            run_id,
            task_id,
            target,
            restart=True,
        ),
        transition_selected_agent_phase=transition_selected_agent_phase,
        prepare_task_main_autostart=prepare_task_main_autostart,
        append_message=append_message,
        append_event=append_event,
    )


def selected_target(payload: dict) -> str:
    return str(payload.get("to_agent", "") or payload.get("target", "") or "main")


def handle_slash_command(
    root: Path,
    run_id: str,
    payload: dict,
    message: str,
    task_id: str | None,
    *,
    handlers: SlashCommandHandlers | None = None,
) -> tuple[bool, str | None, dict]:
    handlers = handlers or default_slash_command_handlers()
    sender = str(payload.get("sender", "browser") or "browser")
    stripped = message.strip()
    backend_autostart = None
    interrupt_payload = None
    compact_reset_payload = None
    phase_transition_payload = None
    target = selected_target(payload)

    if not stripped.startswith("/"):
        return False, message, {}
    if stripped == "/":
        reply = handlers.format_aha_command(root, run_id, task_id, "/aha help", target)
    elif stripped == "/agent" or stripped.startswith("/agent "):
        handled, agent_message, reply = handlers.format_agent_command(root, run_id, task_id, target, stripped)
        if not handled:
            if agent_message:
                return False, agent_message, {"command_namespace": "agent", "original_command": stripped}
            reply = reply or "Usage: /agent send <message>"
    elif stripped == "/aha" or stripped.startswith("/aha "):
        handlers.append_message(
            root,
            run_id,
            "aha",
            stripped,
            sender=sender,
            task_id=task_id,
            role="aha",
            from_agent=sender,
            to_agent="aha",
            agent_id=target,
        )
        parts = stripped.split()
        name = parts[1] if len(parts) > 1 else "help"
        if name == "final":
            backend_autostart = handlers.prepare_task_main_autostart(root, run_id, task_id)
            reply = handlers.request_task_finalization(root, run_id, task_id, stripped)
        elif name == "checkpoint":
            reply = handlers.record_task_checkpoint(root, run_id, task_id, stripped)
        elif name in {"reopen", "resume"}:
            reply = handlers.reopen_selected_task(root, run_id, task_id)
        elif name in {"interrupt", "stop"}:
            reply, interrupt_payload = handlers.interrupt_selected_agent(root, run_id, task_id, target)
        elif name == "session" and len(parts) > 2 and parts[2] == "compact-reset":
            reply, compact_reset_payload = handlers.compact_reset_selected_agent(root, run_id, task_id, target)
        elif name == "phase":
            reply, phase_transition_payload = handlers.transition_selected_agent_phase(root, run_id, task_id, target, stripped)
        else:
            reply = handlers.format_aha_command(root, run_id, task_id, stripped, target)
    else:
        reply = f"Unknown command: {stripped.split()[0]}. Use /aha help or /agent <command>."

    handlers.append_event(root, run_id, "aha_command_handled", {"task_id": task_id, "command": stripped})
    response = handlers.append_message(
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
    if interrupt_payload is not None:
        command_response["interrupt"] = interrupt_payload
    if compact_reset_payload is not None:
        command_response["compact_reset"] = compact_reset_payload
    if phase_transition_payload is not None:
        command_response["phase_transition"] = phase_transition_payload
    return True, None, command_response


__all__ = [
    "SlashCommandHandlers",
    "default_slash_command_handlers",
    "handle_slash_command",
    "selected_target",
]
