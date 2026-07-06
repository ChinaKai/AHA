from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import normalize_task_token_saving, utc_now
from aha_cli.services.context_evidence_maintenance import (
    crud_actions_for_signals,
    kb_scope_policy as context_kb_scope_policy,
    maintenance_plan_for_suggestions,
    maintenance_suggestions_for_signals,
    routing_health_for_evidence,
)
from aha_cli.services.context_evidence_growth import kb_growth_state_for_plan
from aha_cli.services.context_evidence_paths import (
    command_path_observations,
    ignored_path,
    sanitize_context_evidence_record,
)
from aha_cli.store.filesystem import append_event, event_path, run_dir, task_snapshot
from aha_cli.store.io import append_jsonl, iter_jsonl_records_from


CONTEXT_EVIDENCE_FILE = "context_evidence.jsonl"
CONTEXT_PACK_EVIDENCE_METRIC_KEY = "context_pack_evidence"
CONTEXT_MAP_QUERY_EVENT = "context_map_query_recorded"


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
    workspace = _task_workspace(root, run_id, task_id)
    return [
        sanitize_context_evidence_record(
            dict(record) | {"evidence_id": offset},
            root=root,
            workspace=workspace,
        )
        for record, offset in records
    ]


def _task_workspace(root: Path, run_id: str, task_id: str) -> Path | None:
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return None
    workspace = str(task.get("workspace_path") or "").strip()
    return Path(workspace).expanduser() if workspace else None


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


def record_agent_kb_feedback(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    agent_id: str,
    feedback: object,
    source: str = "record_task_update",
) -> dict | None:
    if not isinstance(feedback, dict) or not feedback:
        return None
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return None
    if not task_context_evidence_enabled(task):
        return None
    compact = _compact_agent_kb_feedback(feedback)
    if not compact:
        return None
    record = append_task_context_evidence(
        root,
        run_id,
        task_id,
        {
            "type": "agent_kb_feedback",
            "agent_id": agent_id,
            "source": source,
            "feedback": compact,
        },
    )
    append_event(
        root,
        run_id,
        "context_agent_kb_feedback_recorded",
        {
            "task_id": task_id,
            "target": agent_id,
            "evidence_id": record.get("evidence_id"),
            "feedback": compact,
        },
    )
    return record


def _compact_agent_kb_feedback(feedback: dict) -> dict:
    allowed = ("helped", "stale", "missed", "updated", "pending")
    compact: dict[str, list[str]] = {}
    for key in allowed:
        values = feedback.get(key)
        if values is None:
            continue
        if isinstance(values, str):
            raw_items = [values]
        elif isinstance(values, list):
            raw_items = []
            for item in values:
                if isinstance(item, dict):
                    text = item.get("path") or item.get("target") or item.get("summary") or item.get("reason")
                    if text:
                        raw_items.append(str(text))
                else:
                    raw_items.append(str(item))
        else:
            raw_items = [str(values)]
        items = _ordered_unique([_clip(item, 160) for item in raw_items if str(item).strip()], limit=12)
        if items:
            compact[key] = items
    return compact


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
    workspace_path = Path(workspace)
    command_paths = command_path_observations(command_records, workspace=workspace_path, root=root)
    dirty_paths = _git_dirty_paths(workspace_path)
    map_files = _pack_map_files(evidence)
    referenced = [path for path in map_files if not ignored_path(path)]
    actual = _ordered_unique([*command_paths["workspace_files"], *dirty_paths], limit=40)
    knowledge_files = _ordered_unique(command_paths["knowledge_files"], limit=20)
    ignored_command_paths = _ordered_unique(command_paths["ignored_paths"], limit=20)
    stale_refs = [path for path in referenced if not (workspace_path / path).exists()]
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
        knowledge_files=knowledge_files,
        ignored_command_paths=ignored_command_paths,
    )
    maintenance_suggestions = maintenance_suggestions_for_signals(
        signals=signals,
        referenced=referenced,
        actual=actual,
        stale_refs=stale_refs,
        adopted=adopted,
        missing=missing,
        commands=[item.get("command") for item in command_records if item.get("command")],
    )
    maintenance_plan = maintenance_plan_for_suggestions(
        evidence=evidence,
        suggestions=maintenance_suggestions,
        signals=signals,
    )
    prior_records = list_task_context_evidence(root, run_id, task_id)
    kb_growth_state = kb_growth_state_for_plan(
        maintenance_plan=maintenance_plan,
        prior_records=prior_records,
        dirty_paths=dirty_paths,
    )
    routing_health = routing_health_for_evidence(
        signals=signals,
        referenced=referenced,
        actual=actual,
        stale_refs=stale_refs,
        adopted=adopted,
        missing=missing,
        map_diagnostics=map_diagnostics,
    )
    kb_scope_policy = context_kb_scope_policy()
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
            "crud_actions": crud_actions_for_signals(signals),
            "referenced_files": referenced[:20],
            "actual_files": actual[:20],
            "knowledge_files": knowledge_files[:20],
            "ignored_command_paths": ignored_command_paths[:20],
            "stale_references": stale_refs[:20],
            "map_diagnostics": map_diagnostics,
            "routing_health": routing_health,
            "kb_scope_policy": kb_scope_policy,
            "maintenance_suggestions": maintenance_suggestions,
            "maintenance_plan": maintenance_plan,
            "kb_growth_state": kb_growth_state,
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
            "knowledge_files": knowledge_files[:8],
            "map_gap_signals": map_diagnostics.get("gap_signals") or [],
            "routing_health": routing_health,
            "kb_scope_policy": kb_scope_policy,
            "maintenance_suggestions": maintenance_suggestions[:6],
            "maintenance_plan": maintenance_plan[:6],
            "kb_growth_state": kb_growth_state,
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
    knowledge_files: list[str],
    ignored_command_paths: list[str],
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
        "knowledge_files": knowledge_files[:20],
        "ignored_command_paths": ignored_command_paths[:20],
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
        if path_text and not ignored_path(path_text):
            paths.append(path_text)
    return _ordered_unique(paths, limit=40)


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
    "record_agent_kb_feedback",
    "record_project_map_query_result",
    "record_context_pack_from_prompt_metrics",
    "task_context_evidence_enabled",
    "task_context_evidence_path",
]
