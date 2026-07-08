from __future__ import annotations

import hashlib
from pathlib import Path

from aha_cli.domain.models import normalize_task_token_saving
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.filesystem import load_config
from aha_cli.store.knowledge import NAVIGATION_SLUG, entry_dir, knowledge_config, knowledge_root, project_key_aliases
from aha_cli.store.runs import require_plan


def task_context_planner_enabled(task: dict | None) -> bool:
    if not isinstance(task, dict):
        return False
    policy = normalize_task_token_saving(task.get("token_saving"), task.get("context_management"))
    return bool(policy.get("enabled") and policy.get("provider") == "nav")


def context_pack_for_turn(
    root: Path,
    run_id: str,
    task: dict | None,
    user_message: object,
) -> str:
    return str(context_pack_payload_for_turn(
        root,
        run_id,
        task,
        user_message,
    ).get("text") or "")


def context_pack_payload_for_turn(
    root: Path,
    run_id: str,
    task: dict | None,
    user_message: object,
) -> dict:
    """Build a stable KB/navigation pull contract for token-saving tasks.

    The pack is deliberately best-effort and read-only. It provides navigation
    entrypoints and maintenance rules, but does not retrieve keyword-matched KB
    entries, inject task history/evidence recap, summarize the user request, or
    scan the workspace during prompt assembly.
    """
    del user_message
    try:
        if not task_context_planner_enabled(task):
            return {}
        workspace = _task_workspace(task)
        if workspace is None:
            return {}
        config = load_config(root)
        knowledge = _knowledge_pull_reference(root, run_id, task or {}, config, workspace)
        if not knowledge.get("text"):
            return {}
        text = render_prompt_template(
            "backend_context_pack.md",
            knowledge_reference=knowledge.get("text") or "",
        ).rstrip()
        return {
            "text": text,
            "text_sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            "knowledge": {key: value for key, value in knowledge.items() if key != "text"},
        }
    except (Exception, SystemExit):
        return {}


def _task_workspace(task: dict | None) -> Path | None:
    workspace_text = str((task or {}).get("workspace_path") or "").strip()
    if not workspace_text:
        return None
    try:
        workspace = Path(workspace_text).expanduser().resolve()
    except OSError:
        return None
    return workspace if workspace.exists() else None


def _plan_goal(root: Path, run_id: str) -> str | None:
    try:
        return require_plan(root, run_id).get("goal")
    except (Exception, SystemExit):
        return None


def _knowledge_pull_reference(root: Path, run_id: str, task: dict, config: dict, workspace: Path) -> dict:
    cfg = knowledge_config(config)
    if not cfg.get("enabled"):
        return {}
    try:
        project_keys = project_key_aliases(workspace, goal=_plan_goal(root, run_id))
        kb_root = knowledge_root(root, config)
        nav_rel, nav_exists = _navigation_index_reference(kb_root, project_keys)
        worklog_rel, worklog_exists = _task_worklog_reference(kb_root, project_keys, task)
        text = "\n".join(
            [
                "Knowledge base entrypoints:",
                f"- kb_root: {kb_root}",
                f"- project_key: {project_keys[0]}",
                *([f"- project_key_aliases: {', '.join(project_keys[1:])}"] if len(project_keys) > 1 else []),
                f"- navigation_index: {nav_rel or '-'} ({'exists' if nav_exists else 'not found yet'})",
                *([f"- task_worklog: {worklog_rel} ({'exists' if worklog_exists else 'not found yet'})"] if worklog_rel else []),
                "- Start with navigation/index for broad orientation, then choose modules/* or flows/* yourself.",
                "- If navigation_index is not found yet, create a minimal evidence-based navigation/index.md during the task after verifying source entrypoints.",
                "- Read solutions/wiki only when the current task is semantically similar; skip irrelevant entries.",
                "- Maintain task_worklog throughout the task lifecycle when plans, progress, decisions, requirement changes, verification, or KB/nav updates have durable value.",
                "- Treat KB as routing memory, not truth. Read current source before analysis or edits.",
            ]
        ).rstrip()
        return {
            "text": text,
            "project_key": project_keys[0],
            "project_key_aliases": project_keys[1:],
            "kb_root": str(kb_root),
            "navigation_index": nav_rel,
            "navigation_index_exists": nav_exists,
            "task_worklog": worklog_rel,
            "task_worklog_exists": worklog_exists,
            "mode": "agent_pull",
            "entries": [],
        }
    except (Exception, SystemExit):
        return {}


def _navigation_index_reference(kb_root: Path, project_keys: list[str]) -> tuple[str, bool]:
    fallback = ""
    for key in project_keys:
        rel = entry_dir(kb_root, "project", "navigation", key).relative_to(kb_root) / f"{NAVIGATION_SLUG}.md"
        rel_text = rel.as_posix()
        if not fallback:
            fallback = rel_text
        if (kb_root / rel).exists():
            return rel_text, True
    return fallback, False


def _task_worklog_reference(kb_root: Path, project_keys: list[str], task: dict) -> tuple[str, bool]:
    task_id = str((task or {}).get("id") or "").strip()
    if not task_id:
        return "", False
    fallback = ""
    for key in project_keys:
        rel = entry_dir(kb_root, "project", "worklog", key).relative_to(kb_root) / "tasks" / f"{task_id}.md"
        rel_text = rel.as_posix()
        if not fallback:
            fallback = rel_text
        if (kb_root / rel).exists():
            return rel_text, True
    return fallback, False


__all__ = ["context_pack_for_turn", "context_pack_payload_for_turn", "task_context_planner_enabled"]
