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
DEFAULT_CONTEXT_PACK_WITH_EVIDENCE_TARGET_CHARS = DEFAULT_CONTEXT_PACK_HARD_LIMIT


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
        task_evidence = _task_evidence_reference(root, run_id, task or {})
        if not knowledge.get("text") and not project_map.get("text") and not task_evidence.get("text"):
            return {}
        text = render_prompt_template(
            "backend_context_pack.md",
            request=_clip_single_line(message, 360),
            knowledge_reference=knowledge.get("text") or "",
            map_reference=project_map.get("text") or "",
            evidence_reference=task_evidence.get("text") or "",
        ).rstrip()
        budget = max(
            1,
            min(
                int(target_chars or DEFAULT_CONTEXT_PACK_TARGET_CHARS),
                int(hard_limit or DEFAULT_CONTEXT_PACK_HARD_LIMIT),
            ),
        )
        if task_evidence.get("text"):
            budget = min(
                int(hard_limit or DEFAULT_CONTEXT_PACK_HARD_LIMIT),
                max(budget, DEFAULT_CONTEXT_PACK_WITH_EVIDENCE_TARGET_CHARS),
            )
        text = _clip_block(text, budget)
        return {
            "text": text,
            "request": _clip_single_line(message, 360),
            "text_sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            "knowledge": {key: value for key, value in knowledge.items() if key != "text"},
            "map": {key: value for key, value in project_map.items() if key != "text"},
            "task_evidence": {key: value for key, value in task_evidence.items() if key != "text"},
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


def _ordered_unique(values: list[str], *, limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        clean = _clean_text(value)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
        if len(out) >= limit:
            break
    return out


def _compact_list(values: list[str], *, limit: int = 8, item_chars: int = 96) -> list[str]:
    return [_clip_single_line(value, item_chars) for value in _ordered_unique(values, limit=limit)]


def _format_compact_list(values: list[str], *, empty: str = "-") -> str:
    return ", ".join(values) if values else empty


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


def _task_evidence_reference(root: Path, run_id: str, task: dict) -> dict:
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return {}
    try:
        from aha_cli.services.context_evidence import list_task_context_evidence

        records = list_task_context_evidence(root, run_id, task_id)
    except (Exception, SystemExit):
        return {}
    if not records:
        return {}
    result_records = [record for record in records if record.get("type") == "context_evidence_result"]
    query_records = [record for record in records if record.get("type") == "project_map_query"]
    latest = result_records[-1] if result_records else {}
    recent_results = result_records[-4:]
    signals = _compact_list(
        [
            str(signal)
            for record in recent_results
            for signal in (record.get("signals") or [])
            if signal
        ],
        limit=10,
        item_chars=48,
    )
    actual_files = _compact_list(
        [
            str(path)
            for record in recent_results
            for path in (record.get("actual_files") or [])
            if path
        ],
        limit=8,
    )
    referenced_files = _compact_list(
        [
            str(path)
            for record in recent_results
            for path in (record.get("referenced_files") or [])
            if path
        ],
        limit=8,
    )
    diagnostics = latest.get("map_diagnostics") if isinstance(latest.get("map_diagnostics"), dict) else {}
    gap_signals = _compact_list([str(item) for item in (diagnostics.get("gap_signals") or [])], limit=8, item_chars=48)
    missing_files = _compact_list([str(item) for item in (diagnostics.get("missing_files") or [])], limit=8)
    stale_hints = _compact_list([str(item) for item in (diagnostics.get("stale_path_hints") or [])], limit=6)
    routing_health = _compact_routing_health(latest)
    kb_scope_policy = _compact_kb_scope_policy(latest)
    kb_growth_state = _compact_kb_growth_state(latest)
    suggestions = _compact_suggestions(recent_results)
    maintenance_plan = _compact_maintenance_plan(recent_results)
    map_queries = _compact_map_queries(query_records)
    if not any([
        signals,
        actual_files,
        referenced_files,
        gap_signals,
        missing_files,
        stale_hints,
        routing_health,
        kb_scope_policy,
        kb_growth_state,
        suggestions,
        maintenance_plan,
        map_queries,
    ]):
        return {}
    lines = ["Current task evidence recap:"]
    if signals:
        lines.append(f"- signals: {_format_compact_list(signals)}")
    if actual_files:
        lines.append(f"- actual_files: {_format_compact_list(actual_files)}")
    if referenced_files:
        lines.append(f"- referenced_files: {_format_compact_list(referenced_files)}")
    if gap_signals:
        lines.append(f"- map_gap_signals: {_format_compact_list(gap_signals)}")
    if missing_files:
        lines.append(f"- map_missing_files: {_format_compact_list(missing_files)}")
    if stale_hints:
        lines.append(f"- stale_path_hints: {_format_compact_list(stale_hints)}")
    if routing_health:
        lines.append(f"- routing_health: {routing_health['summary']}")
    if kb_scope_policy:
        lines.append(f"- kb_scope_policy: {kb_scope_policy['summary']}")
    if kb_growth_state:
        lines.append(f"- kb_growth_state: {kb_growth_state['summary']}")
    if map_queries:
        lines.append(f"- recent_map_queries: {' | '.join(item['summary'] for item in map_queries)}")
    if maintenance_plan:
        lines.append(f"- maintenance_plan: {' | '.join(item['summary'] for item in maintenance_plan)}")
    if suggestions:
        lines.append(f"- maintenance_actions: {' | '.join(item['summary'] for item in suggestions)}")
    lines.append("- Treat this as task-local observation, not source truth. Re-check current source before edits.")
    return {
        "text": "\n".join(lines).rstrip(),
        "signals": signals,
        "actual_files": actual_files,
        "referenced_files": referenced_files,
        "map_gap_signals": gap_signals,
        "map_missing_files": missing_files,
        "stale_path_hints": stale_hints,
        "routing_health": routing_health,
        "kb_scope_policy": kb_scope_policy,
        "kb_growth_state": kb_growth_state,
        "map_queries": map_queries,
        "maintenance_plan": maintenance_plan,
        "maintenance_suggestions": suggestions,
    }


def _compact_suggestions(result_records: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    suggestions: list[dict] = []
    for record in reversed(result_records):
        for item in record.get("maintenance_suggestions") or []:
            if not isinstance(item, dict):
                continue
            action = _clip_single_line(str(item.get("action") or ""), 32)
            target = _clip_single_line(str(item.get("target") or ""), 64)
            reason = _clip_single_line(str(item.get("reason") or ""), 96)
            key = (action, target, reason)
            if not action or not target or key in seen:
                continue
            seen.add(key)
            files = _compact_list([str(path) for path in (item.get("files") or [])], limit=4)
            file_text = f" files={_format_compact_list(files)}" if files else ""
            summary = _clip_single_line(f"{action} {target} ({reason}){file_text}", 180)
            suggestions.append({
                "action": action,
                "target": target,
                "reason": reason,
                "files": files,
                "summary": summary,
            })
            if len(suggestions) >= 4:
                return suggestions
    return suggestions


def _compact_maintenance_plan(result_records: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str, str]] = set()
    plans: list[dict] = []
    for record in reversed(result_records):
        for item in record.get("maintenance_plan") or []:
            if not isinstance(item, dict):
                continue
            action = _clip_single_line(str(item.get("action") or ""), 32)
            target = _clip_single_line(str(item.get("target") or ""), 64)
            target_path = _clip_single_line(str(item.get("target_path") or ""), 120)
            policy = _clip_single_line(str(item.get("write_policy") or ""), 72)
            reason = _clip_single_line(str(item.get("reason") or ""), 96)
            key = (action, target, target_path, reason)
            if not action or not target or key in seen:
                continue
            seen.add(key)
            source_files = _compact_list([str(path) for path in (item.get("source_files") or item.get("files") or [])], limit=3)
            validations = _compact_list([str(command) for command in (item.get("validation") or [])], limit=2, item_chars=110)
            execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
            execution_state = _clip_single_line(str(execution.get("state") or ""), 48)
            target_text = f" -> {target_path}" if target_path else ""
            policy_text = f" policy={policy}" if policy else ""
            execution_text = f" state={execution_state}" if execution_state else ""
            file_text = f" files={_format_compact_list(source_files)}" if source_files else ""
            summary = _clip_single_line(
                f"{action} {target}{target_text} ({reason}){policy_text}{execution_text}{file_text}",
                180,
            )
            plans.append({
                "action": action,
                "target": target,
                "target_path": target_path,
                "write_policy": policy,
                "execution_state": execution_state,
                "reason": reason,
                "source_files": source_files,
                "validation": validations,
                "summary": summary,
            })
            if len(plans) >= 4:
                return plans
    return plans


def _compact_routing_health(latest_result: dict) -> dict:
    health = latest_result.get("routing_health") if isinstance(latest_result.get("routing_health"), dict) else {}
    if not health:
        return {}
    status = _clip_single_line(str(health.get("status") or ""), 48)
    downrank = _compact_list([str(path) for path in (health.get("downrank_paths") or [])], limit=4)
    prioritize = _compact_list([str(path) for path in (health.get("prioritize_paths") or [])], limit=4)
    parts = [status or "unknown"]
    if downrank:
        parts.append(f"downrank={_format_compact_list(downrank)}")
    if prioritize:
        parts.append(f"prioritize={_format_compact_list(prioritize)}")
    return {
        "status": status,
        "downrank_paths": downrank,
        "prioritize_paths": prioritize,
        "summary": _clip_single_line(" ".join(parts), 180),
    }


def _compact_kb_scope_policy(latest_result: dict) -> dict:
    policy = latest_result.get("kb_scope_policy") if isinstance(latest_result.get("kb_scope_policy"), dict) else {}
    if not policy:
        return {}
    non_project_policy = str(policy.get("general_personal_wiki") or "")
    non_project_summary = "manual_review" if non_project_policy == "manual_candidate_review_only" else non_project_policy
    summary = "; ".join(
        [
            f"project_navigation={policy.get('project_navigation') or '-'}",
            f"non_project={non_project_summary or '-'}",
        ]
    )
    return {
        "project_navigation": str(policy.get("project_navigation") or ""),
        "general_personal_wiki": str(policy.get("general_personal_wiki") or ""),
        "summary": _clip_single_line(summary, 200),
    }


def _compact_kb_growth_state(latest_result: dict) -> dict:
    state = latest_result.get("kb_growth_state") if isinstance(latest_result.get("kb_growth_state"), dict) else {}
    if not state:
        return {}
    status = _clip_single_line(str(state.get("status") or ""), 48)
    pending = state.get("pending") if isinstance(state.get("pending"), list) else []
    applied = state.get("applied") if isinstance(state.get("applied"), list) else []
    pending_paths = _compact_list([str(item.get("target_path") or "") for item in pending if isinstance(item, dict)], limit=4)
    applied_paths = _compact_list([str(item.get("target_path") or "") for item in applied if isinstance(item, dict)], limit=4)
    parts = [status or "unknown"]
    if pending_paths:
        parts.append(f"pending={_format_compact_list(pending_paths)}")
    if applied_paths:
        parts.append(f"applied={_format_compact_list(applied_paths)}")
    return {
        "status": status,
        "required_count": int(state.get("required_count") or 0),
        "applied_count": int(state.get("applied_count") or 0),
        "pending_count": int(state.get("pending_count") or 0),
        "pending_paths": pending_paths,
        "applied_paths": applied_paths,
        "summary": _clip_single_line(" ".join(parts), 220),
    }


def _compact_map_queries(query_records: list[dict]) -> list[dict]:
    queries: list[dict] = []
    for record in query_records[-4:]:
        project_map = record.get("map") if isinstance(record.get("map"), dict) else {}
        query = _clip_single_line(str(project_map.get("query") or project_map.get("resolved_query") or ""), 80)
        if not query:
            continue
        files = _compact_list([str(path) for path in (project_map.get("files") or [])], limit=4)
        matches = project_map.get("total_matches")
        match_text = "unknown" if matches is None else str(matches)
        file_text = f" files={_format_compact_list(files)}" if files else ""
        summary = _clip_single_line(f"{query} ({match_text} matches){file_text}", 180)
        queries.append({
            "query": query,
            "total_matches": matches,
            "files": files,
            "summary": summary,
        })
    return queries


__all__ = ["context_pack_for_turn", "context_pack_payload_for_turn", "task_context_planner_enabled"]
