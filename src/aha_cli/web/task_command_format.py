from __future__ import annotations

from pathlib import Path

from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.filesystem import task_snapshot


SUPPORTED_SLASH_COMMANDS = "Supported slash commands: /aha kb <message>, /aha nav <message>, /aha complete, /aha reopen, /aha interrupt, /agent <command>."


def format_aha_command(root: Path, run_id: str, task_id: str | None, command: str, target: str = "main") -> str:
    del target
    parts = command.split()
    name = parts[1] if len(parts) > 1 else ""
    if not name:
        return SUPPORTED_SLASH_COMMANDS
    if not task_id:
        return "No task is selected."
    try:
        task_snapshot(root, run_id, task_id)
    except KeyError:
        return f"Task not found: {task_id}"
    if name == "kb":
        return "Use `/aha kb <message>` from the selected task conversation to ask the current agent to emit knowledge-base candidates from its sticky session context."
    if name == "nav":
        return "Use `/aha nav <message>` from the selected task conversation to ask the current agent to emit project navigation candidates from its sticky session context."
    if name == "complete":
        return "Use `/aha complete` from the selected task conversation to mark the task completed."
    if name == "reopen":
        return "Use `/aha reopen` from the selected task conversation to unlock the task for follow-up."
    if name == "interrupt":
        return "Use `/aha interrupt` from the selected task conversation to interrupt the selected agent's current turn."
    return f"Unsupported AHA command: /aha {name}. {SUPPORTED_SLASH_COMMANDS}"


def format_agent_command(root: Path, run_id: str, task_id: str | None, agent_id: str | None, command: str) -> tuple[bool, str | None, str | None]:
    del root, run_id, task_id, agent_id
    suffix = command.removeprefix("/agent").strip()
    if not suffix:
        return True, None, "Usage: /agent <command> routes /<command> to the selected agent. Example: /agent status -> /status"
    return False, suffix if suffix.startswith("/") else f"/{suffix}", None


def format_aha_kb_command(command: str) -> tuple[bool, str | None, str | None]:
    parts = command.split(maxsplit=2)
    suffix = parts[2].strip() if len(parts) > 2 and parts[0] == "/aha" and parts[1] == "kb" else ""
    if not suffix:
        return True, None, "Usage: /aha kb <message> asks the current agent to generate knowledge-base candidates from its sticky session context."
    prompt = render_prompt_template("knowledge_command.md", instruction=suffix).rstrip()
    return False, prompt, None


def format_aha_nav_command(command: str) -> tuple[bool, str | None, str | None]:
    parts = command.split(maxsplit=2)
    suffix = parts[2].strip() if len(parts) > 2 and parts[0] == "/aha" and parts[1] == "nav" else ""
    if not suffix:
        return True, None, "Usage: /aha nav <message> asks the current agent to generate project navigation candidates from its sticky session context."
    prompt = render_prompt_template("navigation_command.md", instruction=suffix).rstrip()
    return False, prompt, None


def format_task_journal_for_prompt(rounds: list[dict]) -> str:
    if not rounds:
        return render_prompt_template("finalization_task_journal_empty.md").rstrip()
    items: list[str] = []
    for index, item in enumerate(rounds[-50:], start=1):
        metadata: list[str] = []
        if item.get("journal_id"):
            metadata.append(_format_journal_field("journal_id", item.get("journal_id")))
        metadata.append(_format_journal_field("round_id", item.get("round_id")))
        metadata.append(_format_journal_field("trigger", item.get("trigger")))
        if item.get("at"):
            metadata.append(_format_journal_field("at", item.get("at")))
        changed_files = item.get("changed_files") or []
        verification = item.get("verification") or []
        risks = item.get("risks") or []
        if changed_files:
            metadata.append(_format_journal_field("files", ", ".join(str(path) for path in changed_files)))
        if verification:
            metadata.append(_format_journal_field("verification", "; ".join(str(check) for check in verification)))
        if risks:
            metadata.append(_format_journal_field("risks", "; ".join(str(risk) for risk in risks)))
        items.append(
            render_prompt_template(
                "finalization_task_journal_item.md",
                index=index,
                summary=item.get("summary"),
                metadata="\n".join(metadata),
            ).rstrip()
        )
    return render_prompt_template("finalization_task_journal.md", items="\n".join(items)).rstrip()


def _format_journal_field(name: str, value: object) -> str:
    return render_prompt_template("finalization_task_journal_field.md", field_name=name, value=value).rstrip()


def format_finalization_context_for_prompt(context: dict | None) -> str:
    context = context or {}
    journal_ids = context.get("journal_ids") if isinstance(context.get("journal_ids"), list) else []
    round_ids = context.get("round_ids") if isinstance(context.get("round_ids"), list) else []
    return render_prompt_template(
        "finalization_source_context.md",
        source=context.get("source") or "task_journal",
        from_at=context.get("from_at") or "-",
        to_at=context.get("to_at") or "-",
        journal_count=context.get("journal_count", len(journal_ids)),
        journal_ids=", ".join(str(item) for item in journal_ids) if journal_ids else "-",
        round_ids=", ".join(str(item) for item in round_ids) if round_ids else "-",
    ).rstrip()


def format_knowledge_feedback_context_for_prompt(context: dict | None) -> str:
    context = context or {}
    knowledge_enabled = bool(context.get("knowledge_enabled"))
    project_nav_enabled = bool(context.get("project_nav_enabled"))
    project_nav_index_exists = bool(context.get("project_nav_index_exists"))
    project_key_value = str(context.get("project_key") or "-")
    if knowledge_enabled and project_nav_enabled and project_nav_index_exists:
        return render_prompt_template(
            "finalization_knowledge_feedback_enabled.md",
            project_key_value=project_key_value,
            workspace_path=context.get("workspace_path") or "-",
        ).rstrip()
    return render_prompt_template(
        "finalization_knowledge_feedback_disabled.md",
        knowledge_enabled=str(knowledge_enabled).lower(),
        project_nav_enabled=str(project_nav_enabled).lower(),
        project_nav_index_exists=str(project_nav_index_exists).lower(),
        project_key_value=project_key_value,
    ).rstrip()


def finalization_prompt(
    task_id: str,
    title: str,
    rounds: list[dict] | None = None,
    final_context: dict | None = None,
    knowledge_feedback_context: str | None = None,
) -> str:
    del rounds, final_context
    return render_prompt_template(
        "finalization.md",
        task_id=task_id,
        title=title,
        knowledge_feedback_context=knowledge_feedback_context or "",
    )


__all__ = [
    "finalization_prompt",
    "format_finalization_context_for_prompt",
    "format_agent_command",
    "format_aha_kb_command",
    "format_aha_nav_command",
    "format_aha_command",
    "format_knowledge_feedback_context_for_prompt",
    "format_task_journal_for_prompt",
]
