from __future__ import annotations

from pathlib import Path
import shlex

from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.filesystem import task_snapshot


SUPPORTED_SLASH_COMMANDS = "Supported slash commands: /aha kb <message>, /aha nav <message>, /aha map status|refresh|query <terms>, /aha complete, /aha reopen, /aha interrupt, /agent <command>."


def format_aha_command(root: Path, run_id: str, task_id: str | None, command: str, target: str = "main") -> str:
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
    if name == "map":
        return format_aha_map_command(root, run_id, task_id, command, target=target)
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


def _command_parts(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _option_value(parts: list[str], name: str) -> str | None:
    if name not in parts:
        return None
    index = parts.index(name)
    if len(parts) <= index + 1:
        return None
    return parts[index + 1]


def _task_workspace(root: Path, run_id: str, task_id: str | None) -> str | None:
    if not task_id:
        return None
    try:
        return str(task_snapshot(root, run_id, task_id)["task"].get("workspace_path") or "").strip() or None
    except KeyError:
        return None


def _map_workspace(root: Path, run_id: str, task_id: str | None, parts: list[str]) -> str | None:
    return _option_value(parts, "--workspace") or _task_workspace(root, run_id, task_id)


def _format_counts(counts: dict) -> str:
    by_kind = counts.get("by_kind") if isinstance(counts.get("by_kind"), dict) else {}
    kind_text = ", ".join(f"{key}={value}" for key, value in sorted(by_kind.items())) or "-"
    return (
        f"files={counts.get('files', 0)}, packages={counts.get('packages', 0)}, symbols={counts.get('symbols', 0)}, "
        f"build={counts.get('build', 0)}, configs={counts.get('configs', 0)}, "
        f"device_tree={counts.get('device_tree', 0)}, entry_points={counts.get('entry_points', 0)}, "
        f"extractor_errors={counts.get('extractor_errors', 0)}; kinds: {kind_text}"
    )


def _format_map_status(status: dict) -> str:
    lines = [
        f"Project map status: {status.get('status') or 'unknown'}",
        f"- workspace: `{status.get('workspace') or '-'}`",
        f"- project_key: `{status.get('project_key') or '-'}`",
        f"- workspace_id: `{status.get('workspace_id') or '-'}`",
    ]
    if status.get("generated_at"):
        lines.append(f"- generated_at: `{status.get('generated_at')}`")
    if status.get("flavors"):
        lines.append(f"- flavors: {', '.join(status.get('flavors') or [])}")
    if status.get("profiles"):
        lines.append(f"- profiles: {', '.join(status.get('profiles') or [])}")
    lines.append(f"- counts: {_format_counts(status.get('counts') or {})}")
    paths = status.get("paths") if isinstance(status.get("paths"), dict) else {}
    if paths.get("index"):
        lines.append(f"- index: `{paths.get('index')}`")
    if status.get("status") == "missing":
        lines.append("")
        lines.append("Run `/aha map refresh` to build the local project map.")
    elif status.get("status") == "stale":
        lines.append("")
        lines.append("Run `/aha map refresh` to update the stale local project map.")
    return "\n".join(lines)


def _format_map_query(result: dict, status: dict) -> str:
    files = result.get("files") if isinstance(result.get("files"), list) else []
    section_totals = result.get("section_totals") if isinstance(result.get("section_totals"), dict) else {}
    lines = [
        f"Project map query: `{result.get('query') or '-'}`",
        f"- status: {status.get('status') or 'unknown'}",
        f"- matches: {result.get('total_matches', 0)}",
    ]
    resolution = result.get("resolution") if isinstance(result.get("resolution"), dict) else {}
    if resolution.get("used_navigation"):
        routes = resolution.get("nav_routes") if isinstance(resolution.get("nav_routes"), list) else []
        route_text = " -> ".join(str(item.get("slug") or item.get("title") or "-") for item in routes[:3] if isinstance(item, dict))
        terms = resolution.get("expanded_terms") if isinstance(resolution.get("expanded_terms"), list) else []
        if route_text:
            lines.append(f"- nav route: {route_text}")
        if terms:
            lines.append(f"- expanded terms: {', '.join(str(term) for term in terms[:12])}")
    if section_totals:
        totals = ", ".join(f"{key}={value}" for key, value in sorted(section_totals.items()) if value)
        lines.append(f"- by section: {totals or '-'}")
    if not result.get("total_matches"):
        lines.append("No project map matches. Refresh the map or use broader terms.")
        return "\n".join(lines)
    if files:
        lines.extend(["", "Files:"])
        for item in files:
            lines.append(f"- `{item.get('path')}` ({item.get('kind')}, {item.get('size')} bytes)")
    for section, title in (
        ("packages", "Packages"),
        ("symbols", "Symbols"),
        ("configs", "Configs"),
        ("build", "Build"),
        ("device_tree", "Device tree"),
        ("entry_points", "Entry points"),
    ):
        records = result.get(section) if isinstance(result.get(section), list) else []
        if not records:
            continue
        lines.extend(["", f"{title}:"])
        for item in records:
            label = item.get("name") or item.get("value") or item.get("node") or "-"
            detail = item.get("kind") or section
            if section == "packages":
                deps = item.get("dependencies") if isinstance(item.get("dependencies"), list) else []
                enabled = item.get("enabled_in") if isinstance(item.get("enabled_in"), list) else []
                parts = [detail]
                if deps:
                    parts.append(f"deps={','.join(str(dep) for dep in deps[:4])}")
                if enabled:
                    parts.append(f"enabled={len(enabled)}")
                detail = "; ".join(parts)
            line = item.get("line")
            line_text = f":{line}" if line else ""
            lines.append(f"- `{label}` ({detail}) at `{item.get('path')}{line_text}`")
    return "\n".join(lines)


def format_aha_map_command(root: Path, run_id: str, task_id: str | None, command: str, *, target: str = "main") -> str:
    parts = _command_parts(command)
    subcommand = parts[2].lower() if len(parts) > 2 else ""
    if subcommand not in {"status", "refresh", "query"}:
        return "Usage: /aha map status | /aha map refresh | /aha map query <terms>"
    workspace = _map_workspace(root, run_id, task_id, parts)
    if not workspace:
        return "No workspace is available. Select a task or pass `--workspace <path>`."
    workspace_path = Path(workspace).expanduser()
    if not workspace_path.exists():
        return f"Workspace not found: {workspace}"
    try:
        from aha_cli.services.project_context_index import (
            build_project_context_index,
            format_project_context_reference,
            project_context_index_status,
            query_project_context_index_cache,
        )
        from aha_cli.store.config import load_config

        config = load_config(root)
        if subcommand == "refresh":
            result = build_project_context_index(root, workspace_path, config=config)
            status = {
                "status": result.get("status"),
                "workspace": result.get("workspace"),
                "project_key": result.get("project_key"),
                "workspace_id": result.get("workspace_id"),
                "paths": result.get("paths"),
                "counts": result.get("counts"),
                "flavors": result.get("index", {}).get("flavors") if isinstance(result.get("index"), dict) else [],
                "profiles": result.get("index", {}).get("profiles") if isinstance(result.get("index"), dict) else [],
                "generated_at": result.get("index", {}).get("generated_at") if isinstance(result.get("index"), dict) else None,
            }
            return "Project map refreshed.\n" + _format_map_status(status)
        status = project_context_index_status(root, workspace_path, config=config)
        if subcommand == "status":
            return _format_map_status(status)
        query = " ".join(part for part in parts[3:] if part != "--workspace" and part != _option_value(parts, "--workspace")).strip()
        if not query:
            return "Usage: /aha map query <terms>"
        query_result = query_project_context_index_cache(root, workspace_path, query, config=config)
        if not query_result:
            return _format_map_status(status)
        from aha_cli.services.context_evidence import record_project_map_query_result

        record_project_map_query_result(
            root,
            run_id,
            task_id=task_id,
            agent_id=target,
            command=command,
            query_result=query_result,
            status=status,
        )
        reference = format_project_context_reference(query_result)
        text = _format_map_query(query_result, status)
        if reference:
            text += "\n\nReference preview:\n" + reference
        return text
    except Exception as exc:  # noqa: BLE001 - slash help should not break chat.
        return f"Project map command failed: {type(exc).__name__}: {exc}"


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
    "format_aha_map_command",
    "format_aha_nav_command",
    "format_aha_command",
    "format_knowledge_feedback_context_for_prompt",
    "format_task_journal_for_prompt",
]
