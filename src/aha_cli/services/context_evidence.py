from __future__ import annotations

import re
from pathlib import Path

from aha_cli.domain.models import normalize_task_token_saving, utc_now
from aha_cli.store.filesystem import append_event, event_path, run_dir, task_snapshot
from aha_cli.store.io import append_jsonl, iter_jsonl_records_from


CONTEXT_EVIDENCE_FILE = "context_evidence.jsonl"
CONTEXT_PACK_EVIDENCE_METRIC_KEY = "context_pack_evidence"
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
        },
    )
    return {"record": record, "candidate": None}


def _pack_map_files(evidence: dict) -> list[str]:
    project_map = evidence.get("map") if isinstance(evidence.get("map"), dict) else {}
    return _ordered_unique([str(item) for item in (project_map.get("files") or [])], limit=24)


def _pack_map(evidence: dict) -> dict:
    return evidence.get("map") if isinstance(evidence.get("map"), dict) else {}


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
    if adopted:
        signals.append("context_hit_ok")
    if stale_refs:
        signals.append("nav_stale")
    if missing and referenced:
        signals.append("map_miss")
    if missing and not referenced:
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
    knowledge_entries = ((evidence.get("knowledge") or {}).get("entries") or []) if isinstance(evidence.get("knowledge"), dict) else []
    if exit_code != 0 and knowledge_entries:
        signals.append("entry_wrong")
    if not signals and actual:
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
    if status == "stale" or stale_refs:
        signals.append("map_stale_cache")
    if missing and referenced:
        signals.append("map_coverage_gap" if adopted else "map_ranking_gap")
    if actual and not referenced and _map_query_observed(project_map):
        signals.append("map_extractor_gap")
        resolution = project_map.get("resolution") if isinstance(project_map.get("resolution"), dict) else {}
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
    return {
        "status": str(project_map.get("status") or ""),
        "query": str(project_map.get("query") or ""),
        "resolved_query": str(project_map.get("resolved_query") or ""),
        "total_matches": project_map.get("total_matches"),
        "query_observed": _map_query_observed(project_map),
        "gap_signals": gap_signals,
        "referenced_files": referenced[:20],
        "actual_files": actual[:20],
        "adopted_files": adopted[:20],
        "missing_files": missing[:20],
        "stale_references": stale_refs[:20],
    }


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
    "record_context_pack_from_prompt_metrics",
    "task_context_evidence_enabled",
    "task_context_evidence_path",
]
