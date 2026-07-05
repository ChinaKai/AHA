from __future__ import annotations

import re
from pathlib import Path

from aha_cli.domain.models import normalize_task_token_saving, utc_now
from aha_cli.store.filesystem import append_event, event_path, run_dir, task_snapshot
from aha_cli.store.io import append_jsonl, iter_jsonl_records_from


CONTEXT_EVIDENCE_FILE = "context_evidence.jsonl"
CONTEXT_PACK_EVIDENCE_METRIC_KEY = "context_pack_evidence"
CONTEXT_MAP_QUERY_EVENT = "context_map_query_recorded"
_PATH_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)+)(?![A-Za-z0-9_.-])")
_IGNORED_PATH_PREFIXES = (".aha/", "task_memo_assets/")


def task_context_evidence_enabled(task: dict | None) -> bool:
    if not isinstance(task, dict):
        return False
    policy = normalize_task_token_saving(task.get("token_saving"), task.get("context_management"))
    return bool(policy.get("enabled") and policy.get("provider") == "map")


def task_context_evidence_path(root: Path, run_id: str, task_id: str) -> Path:
    return run_dir(root, run_id) / "tasks" / task_id / CONTEXT_EVIDENCE_FILE


def append_task_context_evidence(root: Path, run_id: str, task_id: str, record: dict) -> dict:
    payload = dict(record)
    payload["task_id"] = task_id
    payload.setdefault("created_at", utc_now())
    path = task_context_evidence_path(root, run_id, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    event_id = append_jsonl(path, payload)
    payload["evidence_id"] = event_id
    return payload


def list_task_context_evidence(root: Path, run_id: str, task_id: str) -> list[dict]:
    path = task_context_evidence_path(root, run_id, task_id)
    if not path.exists():
        return []
    records, _ = iter_jsonl_records_from(path, 0)
    return [dict(record) | {"evidence_id": offset} for record, offset in records]


def record_project_map_query_result(
    root: Path,
    run_id: str,
    *,
    task_id: str | None,
    agent_id: str,
    command: str,
    query_result: dict,
    status: dict,
    source: str = "aha-map-command",
) -> dict | None:
    if not task_id:
        return None
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return None
    if not task_context_evidence_enabled(task):
        return None
    project_map = _compact_project_map_query_result(query_result, status)
    record = append_task_context_evidence(
        root,
        run_id,
        task_id,
        {
            "type": "project_map_query",
            "agent_id": agent_id,
            "source": source,
            "command": _clip(command, 600),
            "map": project_map,
        },
    )
    append_event(
        root,
        run_id,
        CONTEXT_MAP_QUERY_EVENT,
        {
            "task_id": task_id,
            "target": agent_id,
            "evidence_id": record.get("evidence_id"),
            "command": _clip(command, 600),
            "map": project_map,
            "query": project_map.get("query"),
            "total_matches": project_map.get("total_matches"),
            "files": project_map.get("files") or [],
        },
    )
    return record


def record_context_pack_from_prompt_metrics(
    root: Path,
    run_id: str,
    *,
    task_id: str | None,
    agent_id: str,
    source: str,
    user_message: object,
    prompt_event: dict,
    prompt_metrics: dict,
) -> dict | None:
    if not task_id:
        return None
    evidence = prompt_metrics.get(CONTEXT_PACK_EVIDENCE_METRIC_KEY)
    if not isinstance(evidence, dict) or not evidence:
        return None
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return None
    if not task_context_evidence_enabled(task):
        return None
    record = append_task_context_evidence(
        root,
        run_id,
        task_id,
        {
            "type": "context_pack",
            "agent_id": agent_id,
            "source": source,
            "prompt_event_id": prompt_event.get("event_id"),
            "prompt_ref": prompt_metrics.get("prompt_ref"),
            "user_message": _clip(
                str(evidence.get("request") or " ".join(str(user_message or "").split())),
                600,
            ),
            "evidence": evidence,
        },
    )
    append_event(
        root,
        run_id,
        "context_pack_recorded",
        {
            "task_id": task_id,
            "target": agent_id,
            "evidence_id": record.get("evidence_id"),
            "prompt_event_id": prompt_event.get("event_id"),
            "map_files": _pack_map_files(evidence)[:8],
            "knowledge_entries": len(((evidence.get("knowledge") or {}).get("entries") or [])),
        },
    )
    return record


def distill_context_evidence_after_turn(
    root: Path,
    run_id: str,
    *,
    task_id: str | None,
    agent_id: str,
    source: str,
    prompt_event: dict | None,
    prompt_metrics: dict,
    reply: str,
    exit_code: int,
    workspace: Path,
) -> dict | None:
    if not task_id:
        return None
    evidence = prompt_metrics.get(CONTEXT_PACK_EVIDENCE_METRIC_KEY)
    if not isinstance(evidence, dict) or not evidence:
        return None
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return None
    if not task_context_evidence_enabled(task):
        return None
    event_start = int((prompt_event or {}).get("event_id") or 0)
    evidence = _merge_project_map_query_events(
        root,
        run_id,
        evidence,
        event_start,
        task_id=task_id,
        agent_id=agent_id,
    )
    command_records = _command_records_since(root, run_id, event_start, task_id=task_id, agent_id=agent_id)
    command_paths = _paths_from_commands(command_records)
    dirty_paths = _git_dirty_paths(Path(workspace))
    map_files = _pack_map_files(evidence)
    referenced = [path for path in map_files if not _ignored_path(path)]
    actual = _ordered_unique([*command_paths, *dirty_paths], limit=40)
    stale_refs = [path for path in referenced if not (Path(workspace) / path).exists()]
    adopted = [path for path in actual if path in set(referenced)]
    missing = [path for path in actual if path not in set(referenced)]
    signals = _signals_for(
        evidence=evidence,
        exit_code=exit_code,
        referenced=referenced,
        actual=actual,
        stale_refs=stale_refs,
        adopted=adopted,
        missing=missing,
    )
    map_diagnostics = _map_diagnostics(
        evidence=evidence,
        signals=signals,
        referenced=referenced,
        actual=actual,
        stale_refs=stale_refs,
        adopted=adopted,
        missing=missing,
    )
    maintenance_suggestions = _maintenance_suggestions_for_signals(
        signals=signals,
        referenced=referenced,
        actual=actual,
        stale_refs=stale_refs,
        adopted=adopted,
        missing=missing,
        commands=[item.get("command") for item in command_records if item.get("command")],
    )
    maintenance_plan = _maintenance_plan_for_suggestions(
        evidence=evidence,
        suggestions=maintenance_suggestions,
        signals=signals,
    )
    routing_health = _routing_health_for_evidence(
        signals=signals,
        referenced=referenced,
        actual=actual,
        stale_refs=stale_refs,
        adopted=adopted,
        missing=missing,
        map_diagnostics=map_diagnostics,
    )
    kb_scope_policy = _kb_scope_policy()
    record = append_task_context_evidence(
        root,
        run_id,
        task_id,
        {
            "type": "context_evidence_result",
            "agent_id": agent_id,
            "source": source,
            "prompt_event_id": event_start or None,
            "exit_code": exit_code,
            "signals": signals,
            "crud_actions": _crud_actions_for_signals(signals),
            "referenced_files": referenced[:20],
            "actual_files": actual[:20],
            "stale_references": stale_refs[:20],
            "map_diagnostics": map_diagnostics,
            "routing_health": routing_health,
            "kb_scope_policy": kb_scope_policy,
            "maintenance_suggestions": maintenance_suggestions,
            "maintenance_plan": maintenance_plan,
            "commands": [item.get("command") for item in command_records[:12] if item.get("command")],
            "reply_excerpt": _clip(reply, 600),
        },
    )
    append_event(
        root,
        run_id,
        "context_evidence_recorded",
        {
            "task_id": task_id,
            "target": agent_id,
            "evidence_id": record.get("evidence_id"),
            "signals": signals,
            "referenced_files": referenced[:8],
            "actual_files": actual[:8],
            "map_gap_signals": map_diagnostics.get("gap_signals") or [],
            "routing_health": routing_health,
            "kb_scope_policy": kb_scope_policy,
            "maintenance_suggestions": maintenance_suggestions[:6],
            "maintenance_plan": maintenance_plan[:6],
        },
    )
    return {"record": record, "candidate": None}


def _pack_map_files(evidence: dict) -> list[str]:
    project_map = evidence.get("map") if isinstance(evidence.get("map"), dict) else {}
    return _ordered_unique([str(item) for item in (project_map.get("files") or [])], limit=24)


def _pack_map(evidence: dict) -> dict:
    return evidence.get("map") if isinstance(evidence.get("map"), dict) else {}


def _compact_project_map_query_result(query_result: dict, status: dict) -> dict:
    files = query_result.get("files") if isinstance(query_result.get("files"), list) else []
    resolution = query_result.get("resolution") if isinstance(query_result.get("resolution"), dict) else {}
    compact_resolution = {
        "used_navigation": bool(resolution.get("used_navigation")),
        "expanded_terms": _ordered_unique([str(item) for item in (resolution.get("expanded_terms") or [])], limit=16),
        "path_hints": _ordered_unique([str(item) for item in (resolution.get("path_hints") or [])], limit=16),
        "stale_path_hints": _ordered_unique([str(item) for item in (resolution.get("stale_path_hints") or [])], limit=16),
    }
    routes = resolution.get("nav_routes") if isinstance(resolution.get("nav_routes"), list) else []
    compact_routes: list[dict] = []
    for item in routes[:6]:
        if not isinstance(item, dict):
            continue
        compact_routes.append({
            "slug": str(item.get("slug") or ""),
            "title": str(item.get("title") or ""),
        })
    if compact_routes:
        compact_resolution["nav_routes"] = compact_routes
    return {
        "status": str(status.get("status") or query_result.get("status") or ""),
        "query": str(query_result.get("query") or ""),
        "resolved_query": str(query_result.get("resolved_query") or ""),
        "total_matches": query_result.get("total_matches"),
        "files": _ordered_unique([str(item.get("path") or "") for item in files if isinstance(item, dict)], limit=24),
        "resolution": compact_resolution,
    }


def _merge_project_map_query_events(
    root: Path,
    run_id: str,
    evidence: dict,
    start_event_id: int,
    *,
    task_id: str,
    agent_id: str,
) -> dict:
    records, _ = iter_jsonl_records_from(event_path(root, run_id), start_event_id)
    query_maps: list[dict] = []
    for event, _offset in records:
        if event.get("type") != CONTEXT_MAP_QUERY_EVENT:
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if str(data.get("task_id") or "") != task_id:
            continue
        if data.get("target") and str(data.get("target")) != agent_id:
            continue
        project_map = data.get("map") if isinstance(data.get("map"), dict) else {}
        if project_map:
            query_maps.append(project_map)
    if not query_maps:
        return evidence
    merged = dict(evidence)
    base_map = dict(_pack_map(evidence))
    files = _ordered_unique(
        [
            *[str(item) for item in (base_map.get("files") or [])],
            *[str(item) for query_map in query_maps for item in (query_map.get("files") or [])],
        ],
        limit=24,
    )
    latest = dict(query_maps[-1])
    latest["files"] = files
    latest["queries"] = [
        {
            "query": str(item.get("query") or ""),
            "total_matches": item.get("total_matches"),
            "files": [str(path) for path in (item.get("files") or [])][:8],
        }
        for item in query_maps[-6:]
    ]
    merged["map"] = {**base_map, **latest}
    return merged


def _signals_for(
    *,
    evidence: dict,
    exit_code: int,
    referenced: list[str],
    actual: list[str],
    stale_refs: list[str],
    adopted: list[str],
    missing: list[str],
) -> list[str]:
    signals: list[str] = []
    project_map = _pack_map(evidence)
    resolution = project_map.get("resolution") if isinstance(project_map.get("resolution"), dict) else {}
    stale_nav_hints = [str(item) for item in (resolution.get("stale_path_hints") or []) if str(item).strip()]
    if adopted:
        signals.append("context_hit_ok")
    if stale_refs or stale_nav_hints:
        signals.append("nav_stale")
    if missing and referenced:
        signals.append("map_miss")
    knowledge = evidence.get("knowledge") if isinstance(evidence.get("knowledge"), dict) else {}
    nav_index_missing = knowledge.get("navigation_index_exists") is False
    if missing and not referenced and (_map_query_observed(project_map) or nav_index_missing):
        signals.append("missing_nav")
    signals.extend(
        _map_gap_signals(
            evidence=evidence,
            referenced=referenced,
            actual=actual,
            stale_refs=stale_refs,
            adopted=adopted,
            missing=missing,
        )
    )
    knowledge_entries = (knowledge.get("entries") or []) if isinstance(knowledge, dict) else []
    if exit_code != 0 and knowledge_entries:
        signals.append("entry_wrong")
    if not signals and actual and knowledge_entries:
        signals.append("missing_entry")
    return _ordered_unique(signals, limit=12)


def _map_gap_signals(
    *,
    evidence: dict,
    referenced: list[str],
    actual: list[str],
    stale_refs: list[str],
    adopted: list[str],
    missing: list[str],
) -> list[str]:
    project_map = _pack_map(evidence)
    if not project_map:
        return []
    signals: list[str] = []
    status = str(project_map.get("status") or "").strip().lower()
    resolution = project_map.get("resolution") if isinstance(project_map.get("resolution"), dict) else {}
    if status == "stale" or stale_refs:
        signals.append("map_stale_cache")
    if resolution.get("stale_path_hints"):
        signals.append("map_stale_nav_hint")
    if missing and referenced:
        signals.append("map_coverage_gap" if adopted else "map_ranking_gap")
    if actual and not referenced and _map_query_observed(project_map):
        signals.append("map_extractor_gap")
        if not resolution.get("used_navigation"):
            signals.append("map_query_expansion_gap")
    return _ordered_unique(signals, limit=8)


def _map_query_observed(project_map: dict) -> bool:
    return bool(
        str(project_map.get("query") or "").strip()
        or str(project_map.get("resolved_query") or "").strip()
        or "total_matches" in project_map
        or isinstance(project_map.get("resolution"), dict)
    )


def _map_diagnostics(
    *,
    evidence: dict,
    signals: list[str],
    referenced: list[str],
    actual: list[str],
    stale_refs: list[str],
    adopted: list[str],
    missing: list[str],
) -> dict:
    project_map = _pack_map(evidence)
    gap_signals = [signal for signal in signals if signal.startswith("map_") and signal != "map_miss"]
    resolution = project_map.get("resolution") if isinstance(project_map.get("resolution"), dict) else {}
    return {
        "status": str(project_map.get("status") or ""),
        "query": str(project_map.get("query") or ""),
        "resolved_query": str(project_map.get("resolved_query") or ""),
        "total_matches": project_map.get("total_matches"),
        "query_observed": _map_query_observed(project_map),
        "gap_signals": gap_signals,
        "gap_reasons": _map_gap_reasons(
            project_map=project_map,
            referenced=referenced,
            actual=actual,
            stale_refs=stale_refs,
            adopted=adopted,
            missing=missing,
        ),
        "stale_path_hints": _ordered_unique([str(item) for item in (resolution.get("stale_path_hints") or [])], limit=20),
        "referenced_files": referenced[:20],
        "actual_files": actual[:20],
        "adopted_files": adopted[:20],
        "missing_files": missing[:20],
        "stale_references": stale_refs[:20],
    }


def _map_gap_reasons(
    *,
    project_map: dict,
    referenced: list[str],
    actual: list[str],
    stale_refs: list[str],
    adopted: list[str],
    missing: list[str],
) -> list[dict]:
    resolution = project_map.get("resolution") if isinstance(project_map.get("resolution"), dict) else {}
    reasons: list[dict] = []
    stale_hints = _ordered_unique([str(item) for item in (resolution.get("stale_path_hints") or [])], limit=8)
    if stale_refs:
        reasons.append({"reason": "referenced_file_missing", "paths": stale_refs[:8]})
    if stale_hints:
        reasons.append({"reason": "navigation_path_hint_missing", "paths": stale_hints})
    if missing and referenced:
        reasons.append({
            "reason": "map_returned_related_but_missed_actual" if adopted else "map_ranked_wrong_files",
            "paths": missing[:8],
        })
    if actual and not referenced and _map_query_observed(project_map):
        total = project_map.get("total_matches")
        if total == 0:
            reasons.append({"reason": "map_query_returned_no_matches", "paths": actual[:8]})
        elif not resolution.get("used_navigation"):
            reasons.append({"reason": "navigation_not_used_for_query", "paths": actual[:8]})
        else:
            reasons.append({"reason": "map_query_did_not_surface_actual_files", "paths": actual[:8]})
    return reasons[:8]


def _crud_actions_for_signals(signals: list[str]) -> list[str]:
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


def _maintenance_suggestions_for_signals(
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


def _maintenance_plan_for_suggestions(*, evidence: dict, suggestions: list[dict], signals: list[str]) -> list[dict]:
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
            "state": "advisory",
            "mode": "direct_edit_when_reusable",
            "owner": "agent",
            "next_step": "write a project solution only if the evidence is reusable beyond this task",
        }
    if target == "project_map_cache":
        return {
            "state": "ready",
            "mode": "refresh_command",
            "owner": "agent_or_user",
            "next_step": "run /aha map refresh; do not edit generated cache files",
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


def _routing_health_for_evidence(
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


def _kb_scope_policy() -> dict:
    return {
        "project_navigation": "direct_edit_approved_markdown_with_task_evidence",
        "project_solutions": "advisory_direct_edit_only_when_reusable",
        "generated_project_map_cache": "refresh_only_do_not_edit",
        "project_map_logic": "repair_source_when_evidence_is_about_map_logic",
        "general_personal_wiki": "manual_candidate_review_only",
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
            "write_policy": "advisory_project_solution_update",
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
    project_map = _pack_map(evidence)
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


def _command_records_since(root: Path, run_id: str, start_event_id: int, *, task_id: str, agent_id: str) -> list[dict]:
    records, _ = iter_jsonl_records_from(event_path(root, run_id), start_event_id)
    commands: list[dict] = []
    for event, _offset in records:
        if event.get("type") not in {"agent_command_started", "agent_command_finished"}:
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if str(data.get("task_id") or "") != task_id:
            continue
        if data.get("target") and str(data.get("target")) != agent_id:
            continue
        command = str(data.get("command") or "").strip()
        if not command:
            continue
        commands.append({"event_type": event.get("type"), "command": command, "exit_code": data.get("exit_code")})
    return commands


def _paths_from_commands(commands: list[dict]) -> list[str]:
    paths: list[str] = []
    for item in commands:
        command = str(item.get("command") or "")
        for match in _PATH_TOKEN_RE.findall(command):
            clean = match.strip("'\"`.,;:)")
            if _ignored_path(clean):
                continue
            paths.append(clean)
    return _ordered_unique(paths, limit=40)


def _git_dirty_paths(workspace: Path) -> list[str]:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain=v1", "--untracked-files=all", "--", ".", ":(exclude).aha"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    paths: list[str] = []
    for raw in result.stdout.splitlines():
        path_text = raw[3:].strip()
        if " -> " in path_text:
            path_text = path_text.rsplit(" -> ", 1)[-1]
        if path_text and not _ignored_path(path_text):
            paths.append(path_text)
    return _ordered_unique(paths, limit=40)


def _ignored_path(path: str) -> bool:
    clean = str(path or "").strip()
    return not clean or clean.startswith(_IGNORED_PATH_PREFIXES)


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


def _clip(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 2)].rstrip() + " …"


__all__ = [
    "append_task_context_evidence",
    "distill_context_evidence_after_turn",
    "list_task_context_evidence",
    "record_project_map_query_result",
    "record_context_pack_from_prompt_metrics",
    "task_context_evidence_enabled",
    "task_context_evidence_path",
]
