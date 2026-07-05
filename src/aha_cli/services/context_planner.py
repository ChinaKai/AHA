from __future__ import annotations

import hashlib
from pathlib import Path

from aha_cli.domain.models import normalize_task_token_saving
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.filesystem import load_config
from aha_cli.store.knowledge import NAVIGATION_SLUG, entry_dir, knowledge_config, knowledge_root, project_key_aliases
from aha_cli.store.runs import require_plan

DEFAULT_CONTEXT_PACK_TARGET_CHARS = 2500
DEFAULT_CONTEXT_PACK_HARD_LIMIT = 4000


def task_context_planner_enabled(task: dict | None) -> bool:
    if not isinstance(task, dict):
        return False
    policy = normalize_task_token_saving(task.get("token_saving"), task.get("context_management"))
    return bool(policy.get("enabled") and policy.get("provider") == "map")


def context_pack_for_turn(
    root: Path,
    run_id: str,
    task: dict | None,
    user_message: object,
    *,
    target_chars: int = DEFAULT_CONTEXT_PACK_TARGET_CHARS,
    hard_limit: int = DEFAULT_CONTEXT_PACK_HARD_LIMIT,
) -> str:
    return str(context_pack_payload_for_turn(
        root,
        run_id,
        task,
        user_message,
        target_chars=target_chars,
        hard_limit=hard_limit,
    ).get("text") or "")


def context_pack_payload_for_turn(
    root: Path,
    run_id: str,
    task: dict | None,
    user_message: object,
    *,
    target_chars: int = DEFAULT_CONTEXT_PACK_TARGET_CHARS,
    hard_limit: int = DEFAULT_CONTEXT_PACK_HARD_LIMIT,
) -> dict:
    """Build a bounded per-turn KB/Map pull contract for token-saving tasks.

    The pack is deliberately best-effort and read-only. It does not retrieve
    keyword-matched KB entries, does not run map queries, and never builds or
    refreshes the project map during prompt assembly.
    """
    try:
        if not task_context_planner_enabled(task):
            return {}
        message = _request_summary(task or {}, user_message)
        if not message:
            return {}
        workspace = _task_workspace(task)
        if workspace is None:
            return {}
        config = load_config(root)
        knowledge = _knowledge_pull_reference(root, run_id, task or {}, config, workspace)
        project_map = _map_pull_reference(root, workspace, config)
        if not knowledge.get("text") and not project_map.get("text"):
            return {}
        text = render_prompt_template(
            "backend_context_pack.md",
            request=_clip_single_line(message, 360),
            knowledge_reference=knowledge.get("text") or "",
            map_reference=project_map.get("text") or "",
        ).rstrip()
        budget = max(1, min(int(target_chars or DEFAULT_CONTEXT_PACK_TARGET_CHARS), int(hard_limit or DEFAULT_CONTEXT_PACK_HARD_LIMIT)))
        text = _clip_block(text, budget)
        return {
            "text": text,
            "request": _clip_single_line(message, 360),
            "text_sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            "knowledge": {key: value for key, value in knowledge.items() if key != "text"},
            "map": {key: value for key, value in project_map.items() if key != "text"},
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


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _request_summary(task: dict, user_message: object) -> str:
    message = _clean_text(user_message)
    title = _clean_text(task.get("title"))
    description = _clean_text(task.get("description"))
    is_assignment = (
        message.startswith("You are now running in AHA mode.")
        or "You are the task-main agent for this task." in message[:240]
    )
    if is_assignment and (title or description):
        parts = []
        if title:
            parts.append(f"Task: {title}")
        if description:
            parts.append(f"Details: {description}")
        return _clean_text(" ".join(parts))
    if message:
        return message
    return _clean_text(" ".join(part for part in [title, description] if part))


def _clip_single_line(text: str, limit: int) -> str:
    clean = _clean_text(text)
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 2)].rstrip() + " …"


def _clip_block(text: str, limit: int) -> str:
    clean = str(text or "").strip()
    if len(clean) <= limit:
        return clean
    suffix = "\n\n(Context Pack clipped to budget.)"
    return clean[: max(0, limit - len(suffix))].rstrip() + suffix


def _plan_goal(root: Path, run_id: str) -> str | None:
    try:
        return require_plan(root, run_id).get("goal")
    except (Exception, SystemExit):
        return None


def _knowledge_pull_reference(root: Path, run_id: str, task: dict, config: dict, workspace: Path) -> dict:
    del task
    cfg = knowledge_config(config)
    if not cfg.get("enabled"):
        return {}
    try:
        project_keys = project_key_aliases(workspace, goal=_plan_goal(root, run_id))
        kb_root = knowledge_root(root, config)
        nav_rel, nav_exists = _navigation_index_reference(kb_root, project_keys)
        text = "\n".join(
            [
                "Knowledge base entrypoints:",
                f"- kb_root: {kb_root}",
                f"- project_key: {project_keys[0]}",
                *([f"- project_key_aliases: {', '.join(project_keys[1:])}"] if len(project_keys) > 1 else []),
                f"- navigation_index: {nav_rel or '-'} ({'exists' if nav_exists else 'not found yet'})",
                "- Start with navigation/index for broad orientation, then choose modules/* or flows/* yourself.",
                "- Read solutions/wiki only when the current task is semantically similar; skip irrelevant entries.",
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


def _map_pull_reference(root: Path, workspace: Path, config: dict) -> dict:
    try:
        from aha_cli.services.project_context_index import project_context_index_status

        status = project_context_index_status(root, workspace, config=config, verify_worktree=False)
        if not status.get("exists"):
            return {}
        paths = status.get("paths") if isinstance(status.get("paths"), dict) else {}
        map_index = str(paths.get("index") or "").strip()
        if not map_index:
            return {}
        text = "\n".join(
            [
                "Project map entrypoints:",
                f"- map_index: {map_index}",
                f"- project_key: {status.get('project_key') or '-'}",
                f"- workspace_id: {status.get('workspace_id') or '-'}",
                f"- status: {status.get('status') or '-'}",
                f"- generated_at: {status.get('generated_at') or '-'}",
                "- Use `/aha map query <focused natural-language terms>` when navigation/source search needs help.",
                "- Map results are hints only. Read exact source files before analysis or edits.",
                "- Do not edit generated map cache files. Refresh stale cache, and repair extractor/resolver/ranking logic when map evidence proves the logic is wrong.",
                "- Do not refresh/build map during prompt assembly; request refresh only when evidence shows stale/missing cache.",
            ]
        ).rstrip()
        return {
            "text": text,
            "mode": "agent_pull",
            "map_index": map_index,
            "project_key": str(status.get("project_key") or ""),
            "workspace_id": str(status.get("workspace_id") or ""),
            "status": str(status.get("status") or ""),
            "generated_at": str(status.get("generated_at") or ""),
            "files": [],
        }
    except (Exception, SystemExit):
        return {}


__all__ = ["context_pack_for_turn", "context_pack_payload_for_turn", "task_context_planner_enabled"]
