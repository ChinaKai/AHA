from __future__ import annotations


def crud_actions_for_signals(signals: list[str]) -> list[str]:
    actions: list[str] = []
    signal_set = set(signals)
    if "context_hit_ok" in signal_set:
        actions.append("read")
    if signal_set.intersection({"missing_nav", "missing_entry"}):
        actions.append("create")
    if "map_miss" in signal_set:
        actions.append("update")
    if signal_set.intersection({"map_coverage_gap", "map_ranking_gap", "map_extractor_gap", "map_query_expansion_gap"}):
        actions.append("update")
        actions.append("repair")
    if "map_stale_cache" in signal_set:
        actions.append("refresh")
        actions.append("repair")
    if signal_set.intersection({"nav_stale", "entry_wrong"}):
        actions.append("repair")
    if "nav_stale" in signal_set:
        actions.append("deprecate")
    return _ordered_unique(actions, limit=8)


def maintenance_suggestions_for_signals(
    *,
    signals: list[str],
    referenced: list[str],
    actual: list[str],
    stale_refs: list[str],
    adopted: list[str],
    missing: list[str],
    commands: list[str],
) -> list[dict]:
    del adopted
    suggestions: list[dict] = []
    signal_set = set(signals)
    if "missing_nav" in signal_set:
        suggestions.append(_suggestion(
            action="create",
            target="project_navigation",
            reason="missing_nav",
            files=actual or missing,
            commands=commands,
        ))
    if "map_miss" in signal_set:
        suggestions.append(_suggestion(
            action="update",
            target="project_navigation",
            reason="map_miss",
            files=missing or actual,
            commands=commands,
        ))
    if "nav_stale" in signal_set:
        suggestions.append(_suggestion(
            action="repair",
            target="project_navigation",
            reason="nav_stale",
            files=[*stale_refs, *actual],
            commands=commands,
        ))
    if "entry_wrong" in signal_set:
        suggestions.append(_suggestion(
            action="repair",
            target="project_solution",
            reason="entry_wrong",
            files=actual or referenced,
            commands=commands,
        ))
    if "missing_entry" in signal_set:
        suggestions.append(_suggestion(
            action="create",
            target="project_solution",
            reason="missing_entry",
            files=actual,
            commands=commands,
        ))
    if "map_stale_cache" in signal_set:
        suggestions.append(_suggestion(
            action="refresh",
            target="project_map_cache",
            reason="map_stale_cache",
            files=stale_refs or referenced,
        ))
    map_logic_signals = [
        signal
        for signal in ("map_coverage_gap", "map_ranking_gap", "map_extractor_gap", "map_query_expansion_gap")
        if signal in signal_set
    ]
    if map_logic_signals:
        suggestions.append(_suggestion(
            action="repair",
            target="project_map_logic",
            reason="+".join(map_logic_signals),
            files=missing or actual,
            commands=commands,
        ))
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for item in suggestions:
        key = (str(item.get("action") or ""), str(item.get("target") or ""), str(item.get("reason") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= 8:
            break
    return out


def maintenance_plan_for_suggestions(*, evidence: dict, suggestions: list[dict], signals: list[str]) -> list[dict]:
    plans: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        action = str(suggestion.get("action") or "").strip()
        target = str(suggestion.get("target") or "").strip()
        reason = str(suggestion.get("reason") or "").strip()
        if not action or not target:
            continue
        files = _ordered_unique([str(item) for item in (suggestion.get("files") or [])], limit=12)
        commands = _ordered_unique([str(item) for item in (suggestion.get("commands") or [])], limit=4)
        target_info = _maintenance_target_info(
            evidence=evidence,
            target=target,
            reason=reason,
            files=files,
        )
        related_signals = _related_maintenance_signals(target=target, reason=reason, signals=signals)
        item = {
            "action": action,
            "target": target,
            "target_kind": target_info["target_kind"],
            "target_path": target_info["target_path"],
            "reason": reason,
            "signals": related_signals,
            "source_files": files,
            "files": files,
            "commands": commands,
            "validation": target_info["validation"],
            "write_policy": target_info["write_policy"],
            "execution": _maintenance_execution_state(
                action=action,
                target=target,
                target_path=target_info["target_path"],
                write_policy=target_info["write_policy"],
            ),
        }
        target_paths = target_info.get("target_paths") or []
        if target_paths:
            item["target_paths"] = target_paths
        key = (action, target, reason, str(item.get("target_path") or ""))
        if key in seen:
            continue
        seen.add(key)
        plans.append(item)
        if len(plans) >= 8:
            break
    return plans


def routing_health_for_evidence(
    *,
    signals: list[str],
    referenced: list[str],
    actual: list[str],
    stale_refs: list[str],
    adopted: list[str],
    missing: list[str],
    map_diagnostics: dict,
) -> dict:
    signal_set = set(signals)
    stale_hints = [str(item) for item in (map_diagnostics.get("stale_path_hints") or []) if str(item).strip()]
    downrank_paths = _ordered_unique([*stale_refs, *stale_hints], limit=16)
    prioritize_paths = _ordered_unique([*missing, *actual], limit=16)
    if not signals:
        status = "unobserved"
    elif signal_set == {"context_hit_ok"}:
        status = "healthy"
    elif signal_set.intersection({"nav_stale", "map_stale_cache", "map_stale_nav_hint"}):
        status = "stale"
    elif signal_set.intersection({"map_miss", "missing_nav", "missing_entry", "entry_wrong"}):
        status = "needs_repair"
    else:
        status = "watch"
    adjustments: list[dict] = []
    for path in downrank_paths:
        adjustments.append({"path": path, "direction": "downrank", "reason": "stale_or_missing_reference"})
    for path in prioritize_paths:
        if path in downrank_paths:
            continue
        adjustments.append({"path": path, "direction": "prioritize", "reason": "verified_task_source"})
    return {
        "status": status,
        "signals": signals[:12],
        "downrank_paths": downrank_paths,
        "prioritize_paths": prioritize_paths,
        "adopted_files": adopted[:12],
        "score_adjustments": adjustments[:24],
    }


def kb_scope_policy() -> dict:
    return {
        "project_navigation": "direct_edit_approved_markdown_with_task_evidence",
        "project_solutions": "direct_edit_when_reusable_with_task_evidence",
        "generated_project_map_cache": "refresh_only_do_not_edit",
        "project_map_logic": "repair_source_when_evidence_is_about_map_logic",
        "general_personal_wiki": "manual_candidate_review_only",
    }


def _suggestion(
    *,
    action: str,
    target: str,
    reason: str,
    files: list[str] | None = None,
    commands: list[str] | None = None,
) -> dict:
    item = {
        "action": action,
        "target": target,
        "reason": reason,
    }
    if files:
        item["files"] = files[:12]
    if commands:
        item["commands"] = commands[:4]
    return item


def _maintenance_execution_state(*, action: str, target: str, target_path: str, write_policy: str) -> dict:
    if target == "project_navigation":
        return {
            "state": "ready",
            "mode": "direct_edit",
            "owner": "agent",
            "next_step": f"verify source files, then edit {target_path or 'project navigation'}",
        }
    if target == "project_solution":
        return {
            "state": "ready",
            "mode": "direct_edit_when_reusable",
            "owner": "agent",
            "next_step": "write a project solution only if the evidence is reusable beyond this task",
        }
    if target == "project_map_cache":
        return {
            "state": "ready",
            "mode": "refresh_command",
            "owner": "agent",
            "next_step": "run /aha map refresh when stale; do not edit generated cache files",
        }
    if target == "project_map_logic":
        return {
            "state": "ready",
            "mode": "source_repair",
            "owner": "agent",
            "next_step": "repair map extractor/resolver/ranking source and rerun focused tests",
        }
    return {
        "state": "blocked",
        "mode": "manual_review",
        "owner": "user",
        "next_step": f"manual review required for {write_policy or action}",
    }


def _maintenance_target_info(*, evidence: dict, target: str, reason: str, files: list[str]) -> dict:
    if target == "project_navigation":
        target_path = _navigation_target_path(evidence, files, reason=reason)
        return {
            "target_kind": "project_navigation",
            "target_path": target_path,
            "validation": _navigation_validation_commands(files),
            "write_policy": "direct_project_navigation_update",
        }
    if target == "project_solution":
        return {
            "target_kind": "project_solution",
            "target_path": _solution_target_path(evidence),
            "validation": ["re-run the task-specific verification command before writing the solution note"],
            "write_policy": "direct_project_solution_update_when_reusable",
        }
    if target == "project_map_cache":
        return {
            "target_kind": "generated_project_map_cache",
            "target_path": _map_cache_target_path(evidence),
            "validation": ["/aha map refresh", "/aha map status"],
            "write_policy": "refresh_only_do_not_edit_cache",
        }
    if target == "project_map_logic":
        paths = _map_logic_target_paths(reason)
        return {
            "target_kind": "project_map_logic",
            "target_path": paths[0] if paths else "src/aha_cli/services/project_context_index.py",
            "target_paths": paths,
            "validation": [
                "python3 -m pytest tests/test_project_context_index.py tests/test_knowledge_routes.py tests/test_context_evidence.py -q"
            ],
            "write_policy": "repair_source_logic_not_generated_cache",
        }
    return {
        "target_kind": target or "unknown",
        "target_path": "",
        "validation": [],
        "write_policy": "advisory_only",
    }


def _navigation_target_path(evidence: dict, files: list[str], *, reason: str) -> str:
    base = _navigation_base_path(evidence)
    route = _navigation_route_for_files(files, reason=reason)
    return f"{base}/{route}.md" if base else f"{route}.md"


def _navigation_base_path(evidence: dict) -> str:
    knowledge = evidence.get("knowledge") if isinstance(evidence.get("knowledge"), dict) else {}
    nav_index = str(knowledge.get("navigation_index") or "").strip()
    if "/navigation/" in nav_index:
        return nav_index.split("/navigation/", 1)[0].rstrip("/") + "/navigation"
    project_key = str(knowledge.get("project_key") or "").strip()
    if project_key:
        return f"projects/{project_key}/navigation"
    return "navigation"


def _navigation_route_for_files(files: list[str], *, reason: str) -> str:
    joined = " ".join(files).lower()
    reason_text = str(reason or "").lower()
    if "token-saving" in joined or "context_evidence" in joined or "context_planner" in joined:
        return "flows/token-saving"
    if "backend_context_pack" in joined or "chat_prompt_context" in joined:
        return "flows/token-saving"
    if "src/aha_cli/web/static/" in joined:
        return "modules/web-static"
    if "src/aha_cli/web/" in joined:
        return "modules/web-api"
    if "src/aha_cli/services/project_context_" in joined:
        return "flows/token-saving" if "map_" in reason_text else "modules/knowledge"
    if "src/aha_cli/services/" in joined:
        return "modules/services-orchestration"
    if "src/aha_cli/store/" in joined or "src/aha_cli/domain/" in joined:
        return "modules/domain-store"
    if "src/aha_cli/cli" in joined:
        return "modules/cli"
    if "docs/" in joined:
        return "index"
    return "index"


def _navigation_validation_commands(files: list[str]) -> list[str]:
    joined = " ".join(files)
    commands: list[str] = []
    if any(token in joined for token in ("context_evidence", "context_planner", "backend_context_pack", "chat_prompt")):
        commands.append(
            "python3 -m pytest tests/test_context_evidence.py tests/test_chat_prompt.py tests/test_web_task_routes.py -q"
        )
    if "src/aha_cli/web/static/" in joined:
        commands.append("python3 -m pytest tests/test_frontend_static.py tests/test_web_task_routes.py -q")
    if "src/aha_cli/web/" in joined and "src/aha_cli/web/static/" not in joined:
        commands.append("python3 -m pytest tests/test_web_task_routes.py tests/test_knowledge_routes.py -q")
    if not commands:
        commands.append("python3 -m pytest -q")
    return _ordered_unique(commands, limit=3)


def _solution_target_path(evidence: dict) -> str:
    knowledge = evidence.get("knowledge") if isinstance(evidence.get("knowledge"), dict) else {}
    for entry in knowledge.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug") or "").strip()
        kind = str(entry.get("kind") or entry.get("type") or "").strip()
        if slug and ("solution" in kind or slug.startswith("solutions/")):
            if slug.endswith(".md"):
                return slug
            return f"{slug}.md" if slug.startswith("projects/") else f"solutions/{slug}.md"
    project_key = str(knowledge.get("project_key") or "").strip()
    if project_key:
        return f"projects/{project_key}/solutions/"
    return "solutions/"


def _map_cache_target_path(evidence: dict) -> str:
    project_map = evidence.get("map") if isinstance(evidence.get("map"), dict) else {}
    map_index = str(project_map.get("map_index") or "").strip()
    if map_index:
        return map_index
    project_key = str(project_map.get("project_key") or "").strip()
    workspace_id = str(project_map.get("workspace_id") or "").strip()
    if project_key and workspace_id:
        return f"runtime/project_context/{project_key}/{workspace_id}/index.json"
    return "runtime/project_context/"


def _map_logic_target_paths(reason: str) -> list[str]:
    reason_text = str(reason or "")
    paths: list[str] = []
    if "map_extractor_gap" in reason_text:
        paths.append("src/aha_cli/services/project_context_index.py")
    if any(signal in reason_text for signal in ("map_query_expansion_gap", "map_ranking_gap", "map_coverage_gap")):
        paths.append("src/aha_cli/services/project_context_resolver.py")
    return _ordered_unique(paths, limit=4)


def _related_maintenance_signals(*, target: str, reason: str, signals: list[str]) -> list[str]:
    reason_parts = [part for part in str(reason or "").split("+") if part]
    if target == "project_map_logic":
        related = [signal for signal in signals if signal.startswith("map_")]
    elif target == "project_map_cache":
        related = [signal for signal in signals if signal in {"map_stale_cache", "nav_stale"}]
    elif target == "project_navigation":
        related = [signal for signal in signals if signal in {"missing_nav", "map_miss", "nav_stale"}]
    elif target == "project_solution":
        related = [signal for signal in signals if signal in {"missing_entry", "entry_wrong"}]
    else:
        related = []
    return _ordered_unique([*reason_parts, *related], limit=8)


def _ordered_unique(values: list[str], *, limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= limit:
            break
    return out
