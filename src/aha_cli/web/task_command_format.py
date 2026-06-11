from __future__ import annotations

from pathlib import Path

from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.filesystem import task_snapshot


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
                "- /aha phase <phase> [summary]: checkpoint and start a fresh backend session for the selected agent phase",
                "- /aha final: ask task-main to generate the Final and complete the task",
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
    if name == "phase":
        return "Use `/aha phase <phase> [summary]` to checkpoint the selected agent and start the next phase in a fresh backend session."
    if name == "final":
        return "Use `/aha final` from the selected task conversation to ask task-main to generate the Final and complete the task."
    if name in {"reopen", "resume"}:
        return "Use `/aha reopen` from the selected task conversation to unlock the task for follow-up."
    if name == "session" and len(parts) > 2 and parts[2] == "compact-reset":
        return "Use `/aha session compact-reset` from the selected task conversation to archive the current backend session and start a fresh one."
    return f"Unknown AHA command: /aha {name}. Try /aha help."


def format_agent_command(root: Path, run_id: str, task_id: str | None, agent_id: str | None, command: str) -> tuple[bool, str | None, str | None]:
    del root, run_id, task_id, agent_id
    suffix = command.removeprefix("/agent").strip()
    if not suffix:
        return True, None, "Usage: /agent <command> routes /<command> to the selected agent. Example: /agent status -> /status"
    return False, suffix if suffix.startswith("/") else f"/{suffix}", None


def format_task_journal_for_prompt(rounds: list[dict]) -> str:
    if not rounds:
        return "Task journal (chronological ordered list):\n1. (empty)"
    lines = ["Task journal (chronological ordered list):"]
    for index, item in enumerate(rounds[-50:], start=1):
        lines.append(f"{index}. {item.get('summary')}")
        if item.get("journal_id"):
            lines.append(f"   - journal_id: {item.get('journal_id')}")
        lines.append(f"   - round_id: {item.get('round_id')}")
        lines.append(f"   - trigger: {item.get('trigger')}")
        if item.get("at"):
            lines.append(f"   - at: {item.get('at')}")
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


def format_finalization_context_for_prompt(context: dict | None) -> str:
    context = context or {}
    journal_ids = context.get("journal_ids") if isinstance(context.get("journal_ids"), list) else []
    round_ids = context.get("round_ids") if isinstance(context.get("round_ids"), list) else []
    return "\n".join(
        [
            "Final source range:",
            f"- source: {context.get('source') or 'task_journal'}",
            f"- from: {context.get('from_at') or '-'}",
            f"- to: {context.get('to_at') or '-'}",
            f"- journal_count: {context.get('journal_count', len(journal_ids))}",
            f"- journal_ids: {', '.join(str(item) for item in journal_ids) if journal_ids else '-'}",
            f"- round_ids: {', '.join(str(item) for item in round_ids) if round_ids else '-'}",
        ]
    )


def finalization_prompt(task_id: str, title: str, rounds: list[dict] | None = None, final_context: dict | None = None) -> str:
    return render_prompt_template(
        "finalization.md",
        task_id=task_id,
        title=title,
        final_context=format_finalization_context_for_prompt(final_context),
        task_journal=format_task_journal_for_prompt(rounds or []),
    )


def memo_completion_report_prompt(memo: dict, task: dict, rounds: list[dict] | None = None, report_context: dict | None = None) -> str:
    context = report_context or {}
    return render_prompt_template(
        "memo_completion_report.md",
        memo_id=memo.get("id") or "",
        memo_title=memo.get("title") or "",
        memo_status=memo.get("status") or "",
        memo_completed_at=memo.get("completed_at") or "",
        memo_description=memo.get("description") or "",
        task_id=task.get("id") or context.get("task_id") or "",
        task_title=task.get("title") or "",
        requested_at=context.get("requested_at") or "",
        attachment_dir=context.get("attachment_dir") or "-",
        task_journal=format_task_journal_for_prompt(rounds or []),
    )


__all__ = [
    "finalization_prompt",
    "format_finalization_context_for_prompt",
    "format_agent_command",
    "format_aha_command",
    "format_task_journal_for_prompt",
    "memo_completion_report_prompt",
]
