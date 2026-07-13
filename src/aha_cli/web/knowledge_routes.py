"""HTTP API for the knowledge base Web console (Phase 4b).

Root-scoped JSON endpoints under /api/kb for browsing entries, reviewing the
pending curation queue (approve/reject), and reading/updating the knowledge
settings (enabled / path / git remote+branch+auto flags / project navigation /
curation gate).

These mirror the conventions in web/system_routes.py and are served from
web/server.py. The matching UI lives in web/static/knowledge.html.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import os
from pathlib import Path
import signal

from aha_cli.backends.registry import normalize_reasoning_effort
from aha_cli.domain.models import default_knowledge_config, utc_now
from aha_cli.services.knowledge_agent_progress import agent_log_event, summarize_agent_progress, trim_agent_log
from aha_cli.services.knowledge_git import auto_commit_after_change
from aha_cli.services.knowledge_git import changed_paths as knowledge_changed_paths
from aha_cli.services.knowledge_git import sync_status as knowledge_sync_status
from aha_cli.services.knowledge_git import sync as knowledge_sync
from aha_cli.services.knowledge_navigation import (
    build_navigation_bootstrap_prompt,
    prepare_project_navigation,
    validate_navigation_candidates,
)
from aha_cli.store.config import load_config
from aha_cli.store.io import read_json, write_json
from aha_cli.store import knowledge_nav_drafts as nav_drafts
from aha_cli.store.runs import require_plan
from aha_cli.store.knowledge import (
    NAVIGATION_SLUG,
    approve_candidate,
    delete_entry,
    delete_project_navigation,
    entry_exists,
    find_entry,
    init_knowledge_base,
    iter_all_entries,
    iter_all_entry_summary_records,
    iter_all_entry_summaries,
    knowledge_status,
    knowledge_root,
    list_pending,
    project_key as derive_project_key,
    remove_pending,
    slugify,
    type_for_kind,
    update_entry,
    write_entry,
)
from aha_cli.store.knowledge_assets import EntryImageRejected, add_entry_image, read_entry_image
from aha_cli.store import knowledge_capture as capture
from aha_cli.store.paths import config_path
from aha_cli.web.http_utils import http_response, json_response, parse_json_body


def _default_dispatch_distill_job(
    root: Path,
    cfg: dict,
    note_id: str,
    backend,
    model,
    proxy_enabled=None,
    mode=None,
    reasoning_effort=None,
) -> None:
    """Run a capture distill as a background daemon thread (non-blocking).

    Seam: tests replace ``dispatch_distill_job`` to run synchronously with a
    stub agent. The note's ``status`` (distilling/distilled/error) is the
    pollable job record, so no separate job store is needed.
    """
    import threading

    from aha_cli.services.knowledge_capture_distill import run_distill_job

    threading.Thread(
        target=run_distill_job,
        args=(root, cfg, note_id),
        kwargs={
            "backend": backend,
            "model": model,
            "proxy_enabled": proxy_enabled,
            "mode": mode or "organize",
            "reasoning_effort": reasoning_effort,
        },
        daemon=True,
    ).start()


def _nav_agent_log_event(stage: str, message: str, **extra) -> dict:
    return agent_log_event(stage, message, **extra)


def _append_nav_agent_log(root: Path, cfg: dict, draft_id: str, stage: str, message: str, **extra) -> None:
    draft = nav_drafts.read_draft(root, cfg, draft_id)
    if draft is None:
        return
    log = draft.get("agent_log") if isinstance(draft.get("agent_log"), list) else []
    nav_drafts.update_draft(
        root,
        cfg,
        draft_id,
        agent_log=trim_agent_log([*log, _nav_agent_log_event(stage, message, **extra)]),
    )


def _nav_progress_logger(root: Path, cfg: dict, draft_id: str):
    def _log(event_type: str, data: dict | None = None) -> None:
        summary = summarize_agent_progress(event_type, data)
        if summary is None:
            return
        if event_type == "backend_process_started" and isinstance(data, dict):
            updates = {}
            if data.get("pid"):
                updates["agent_pid"] = data.get("pid")
            if data.get("process_group"):
                updates["agent_process_group"] = data.get("process_group")
            if updates:
                try:
                    nav_drafts.update_draft(root, cfg, draft_id, **updates)
                except FileNotFoundError:
                    pass
        _append_nav_agent_log(
            root,
            cfg,
            draft_id,
            str(summary.pop("stage")),
            str(summary.pop("message")),
            **summary,
        )

    return _log


def _stop_nav_agent_process(draft: dict) -> dict:
    pid = int(draft.get("agent_pid") or 0)
    pgid = int(draft.get("agent_process_group") or 0)
    if not pid and not pgid:
        return {"stopped": False, "reason": "no process recorded"}
    try:
        if pgid:
            os.killpg(pgid, signal.SIGTERM)
            return {"stopped": True, "process_group": pgid, "signal": "SIGTERM"}
        os.kill(pid, signal.SIGTERM)
        return {"stopped": True, "pid": pid, "signal": "SIGTERM"}
    except ProcessLookupError:
        return {"stopped": False, "already_exited": True, "pid": pid or None, "process_group": pgid or None}
    except OSError as exc:
        return {"stopped": False, "error": str(exc), "pid": pid or None, "process_group": pgid or None}


def _nav_prompt_excerpt(text: str, *, limit: int = 4000) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ..."


def run_project_nav_draft_job(
    root: Path,
    cfg: dict,
    draft_id: str,
    *,
    workspace_path: str,
    goal: str | None = None,
    project_key_value: str | None = None,
    backend: str | None = None,
    model: str | None = None,
    proxy_enabled: bool | None = None,
    reasoning_effort: str | None = None,
    agent=None,
) -> dict:
    prompt = build_navigation_bootstrap_prompt(
        {},
        workspace_path=workspace_path,
        project_key_value=project_key_value or "",
    )
    try:
        nav_drafts.update_draft(
            root,
            cfg,
            draft_id,
            agent={"status": "prompt_ready", "prompt_excerpt": _nav_prompt_excerpt(prompt)},
        )
    except FileNotFoundError:
        return {"ok": False, "skipped": "draft missing"}
    _append_nav_agent_log(
        root,
        cfg,
        draft_id,
        "running",
        "Preparing workspace context and calling project navigation agent",
        backend=backend,
        model=model,
        proxy_enabled=proxy_enabled,
        reasoning_effort=reasoning_effort,
    )
    try:
        result = prepare_project_navigation(
            root,
            cfg,
            workspace_path=workspace_path,
            goal=goal,
            project_key_value=project_key_value,
            backend=backend,
            model=model,
            proxy_enabled=proxy_enabled,
            reasoning_effort=reasoning_effort,
            agent=agent,
            progress_callback=_nav_progress_logger(root, cfg, draft_id),
        )
    except Exception as exc:  # noqa: BLE001 - background job must be recorded, not crash the server
        result = {"ok": False, "error": str(exc), "candidates": 0}

    current = nav_drafts.read_draft(root, cfg, draft_id)
    if current is None or current.get("status") in {"rejected", "stopped"}:
        return {"ok": False, "skipped": "draft rejected or missing"}
    if result.get("ok"):
        status = "completed"
        summary = result.get("skipped") or (
            f"{result.get('candidates', 0)} navigation entries ready for review"
        )
        _append_nav_agent_log(
            root,
            cfg,
            draft_id,
            status,
            summary,
            candidates=result.get("candidates"),
        )
        return nav_drafts.update_draft(
            root,
            cfg,
            draft_id,
            status=status,
            completed_at=utc_now(),
            summary=summary,
            skipped=result.get("skipped"),
            project_key=result.get("project_key") or project_key_value,
            candidates=result.get("candidate_items", []),
            validation=result.get("validation"),
            agent=result.get("agent"),
            error="",
        )
    _append_nav_agent_log(
        root,
        cfg,
        draft_id,
        "failed",
        result.get("error") or "project nav generation failed",
    )
    return nav_drafts.update_draft(
        root,
        cfg,
        draft_id,
        status="failed",
        completed_at=utc_now(),
        summary="Project nav generation failed",
        error=result.get("error") or "project nav generation failed",
        candidates=[],
        validation=result.get("validation"),
        agent=result.get("agent"),
    )


def _default_dispatch_project_nav_job(root: Path, cfg: dict, draft_id: str, **kwargs) -> None:
    import threading

    threading.Thread(
        target=run_project_nav_draft_job,
        args=(root, cfg, draft_id),
        kwargs=kwargs,
        daemon=True,
    ).start()


# Module-level seam so tests can substitute synchronous execution.
dispatch_distill_job = _default_dispatch_distill_job
project_navigation_agent = None
dispatch_project_nav_job = _default_dispatch_project_nav_job


def _note_view(note: dict | None) -> dict | None:
    if note is None:
        return None
    view = dict(note)
    view.pop("_path", None)
    return view


def _candidate_source_note_id(candidate: dict) -> str:
    source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    return str(candidate.get("source_note_id") or source.get("note_id") or "").strip()


def _pending_for_note(root: Path, cfg: dict, note_id: str, note: dict | None = None) -> list[dict]:
    note_id = str(note_id or "").strip()
    if not note_id:
        return []
    candidate_ids = {str(item) for item in ((note or {}).get("candidate_ids") or []) if str(item)}
    return [
        candidate
        for candidate in list_pending(root, cfg)
        if str(candidate.get("id") or "") in candidate_ids or _candidate_source_note_id(candidate) == note_id
    ]


def _set_note_remaining_candidates(
    root: Path,
    cfg: dict,
    note_id: str,
    *,
    empty_status: str,
) -> dict:
    note_id = str(note_id or "").strip()
    if not note_id:
        return {"source_note_id": None, "source_note_exists": False, "candidate_ids": []}
    note = capture.read_note(root, cfg, note_id)
    if note is None:
        return {"source_note_id": note_id, "source_note_exists": False, "candidate_ids": []}
    remaining = _pending_for_note(root, cfg, note_id, note)
    remaining_ids = [str(candidate.get("id") or "") for candidate in remaining if str(candidate.get("id") or "")]
    if remaining_ids:
        capture.update_note(root, cfg, note_id, status="distilled", candidate_ids=remaining_ids, last_error="")
        return {
            "source_note_id": note_id,
            "source_note_exists": True,
            "status": "distilled",
            "candidate_ids": remaining_ids,
        }
    capture.update_note(root, cfg, note_id, status=empty_status, candidate_ids=[], last_error="")
    return {
        "source_note_id": note_id,
        "source_note_exists": True,
        "status": empty_status,
        "candidate_ids": [],
    }


def _capture_note_summary(note: dict | None) -> dict | None:
    if note is None:
        return None
    text = str(note.get("text") or "")
    title = str(note.get("title") or "").strip()
    if not title:
        title = text.splitlines()[0][:80] if text.splitlines() else str(note.get("id") or "")
    return {
        "id": note.get("id"),
        "title": title,
        "scope_hint": note.get("scope_hint"),
        "status": note.get("status"),
    }


def _candidate_summary(candidate: dict) -> dict:
    return {
        "id": candidate.get("id"),
        "title": candidate.get("title"),
        "scope": candidate.get("scope"),
        "kind": candidate.get("kind"),
        "project_key": candidate.get("project_key"),
        "slug": candidate.get("slug"),
        "status": candidate.get("status"),
    }


def _candidate_view(candidate: dict, notes_by_id: dict[str, dict] | None = None) -> dict:
    view = dict(candidate)
    source_note_id = _candidate_source_note_id(candidate)
    if source_note_id:
        view["source_note_id"] = source_note_id
        if notes_by_id is not None:
            note = notes_by_id.get(source_note_id)
            if note is not None:
                view["source_note"] = _capture_note_summary(note)
    return view


def _note_with_refs(note: dict, pending: list[dict], entries: list[dict]) -> dict:
    view = _note_view(note) or {}
    note_id = str(note.get("id") or "")
    candidate_ids = {str(item) for item in (note.get("candidate_ids") or []) if str(item)}
    candidate_refs = [
        _candidate_summary(candidate)
        for candidate in pending
        if str(candidate.get("id") or "") in candidate_ids or _candidate_source_note_id(candidate) == note_id
    ]
    entry_refs = [
        _entry_summary(entry)
        for entry in entries
        if str((entry.get("meta") or {}).get("source_note_id") or "") == note_id
    ]
    view["candidate_refs"] = candidate_refs
    view["entry_refs"] = entry_refs
    if view.get("status") == "distilled" and candidate_ids and not candidate_refs and not entry_refs:
        view["status"] = "raw"
        view["candidate_ids"] = []
    return view


def _capture_note_matches_query(note: dict, query: str) -> bool:
    needle = (query or "").strip().lower()
    if not needle:
        return True
    fields: list[str] = [
        note.get("id"),
        note.get("title"),
        note.get("text"),
        note.get("scope_hint"),
        note.get("status"),
        " ".join(str(item) for item in (note.get("candidate_ids") or [])),
    ]
    for candidate in note.get("candidate_refs") or []:
        fields.extend([candidate.get("id"), candidate.get("title"), candidate.get("scope"), candidate.get("kind")])
    for entry in note.get("entry_refs") or []:
        fields.extend([entry.get("id"), entry.get("slug"), entry.get("title"), entry.get("scope"), entry.get("type")])
    return needle in " ".join(str(item or "") for item in fields).lower()


def _entry_summary(entry: dict) -> dict:
    meta = entry.get("meta", {})
    size_bytes = entry.get("size_bytes")
    if size_bytes is None and entry.get("path"):
        try:
            size_bytes = Path(str(entry.get("path"))).stat().st_size
        except OSError:
            size_bytes = None
    return {
        "id": meta.get("id"),
        "slug": meta.get("slug"),
        "title": meta.get("title"),
        "scope": meta.get("scope"),
        "type": meta.get("type"),
        "project_key": meta.get("project_key"),
        "tags": meta.get("tags", []),
        "status": meta.get("status", "active"),
        "review_after": meta.get("review_after"),
        "source_note_id": meta.get("source_note_id"),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "path": entry.get("path"),
        "size_bytes": size_bytes,
    }


def _entry_relative_path(entry: dict, repo: Path) -> str:
    path = str(entry.get("path") or "").strip()
    if not path:
        return ""
    try:
        return Path(path).resolve().relative_to(repo.resolve()).as_posix()
    except (OSError, ValueError):
        return Path(path).as_posix()


def _dirty_entry_paths(root: Path, cfg: dict) -> set[str]:
    return {str(path).replace("\\", "/") for path in knowledge_changed_paths(root, cfg)}


def _mark_dirty_entries(entries: list[dict], root: Path, cfg: dict) -> None:
    dirty_paths = _dirty_entry_paths(root, cfg)
    if not dirty_paths:
        return
    repo = knowledge_root(root, cfg)
    for entry in entries:
        rel = _entry_relative_path(entry, repo)
        if rel and rel in dirty_paths:
            entry["dirty"] = True


def _entry_identity(entry: dict) -> str:
    return str(entry.get("id") or entry.get("slug") or entry.get("path") or "").strip()


def _entry_public_updated_value(entry: dict) -> tuple[dt.datetime, str] | None:
    for key in ("updated_at", "created_at"):
        value = str(entry.get(key) or "").strip()
        parsed = _parse_entry_time(value)
        if parsed is not None:
            return parsed, value
    path = entry.get("path")
    if path:
        try:
            updated = dt.datetime.fromtimestamp(Path(str(path)).stat().st_mtime, tz=dt.timezone.utc).replace(microsecond=0)
        except OSError:
            return None
        return updated, updated.isoformat()
    return None


def _prefer_entry(existing: dict, candidate: dict) -> dict:
    existing_updated = _entry_public_updated_value(existing)
    candidate_updated = _entry_public_updated_value(candidate)
    if candidate_updated and (not existing_updated or candidate_updated[0] >= existing_updated[0]):
        return candidate
    return existing


def _dedupe_entries_by_identity(entries: list[dict]) -> list[dict]:
    deduped: dict[str, dict] = {}
    order: list[str] = []
    for entry in entries:
        key = _entry_identity(entry)
        if not key:
            key = str(len(order))
        if key not in deduped:
            deduped[key] = entry
            order.append(key)
            continue
        deduped[key] = _prefer_entry(deduped[key], entry)
    return [deduped[key] for key in order]


def _parse_entry_time(value: object) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _query_bool_param(query: dict[str, list[str]], key: str) -> bool:
    return str(query.get(key, [""])[0] or "").strip().lower() in {"1", "true", "yes", "on"}


def _entry_list_summary_matches(entry: dict, *, scope: str | None, project: str | None, want_type: str | None) -> bool:
    meta = entry.get("meta", {})
    if meta.get("type") == "navigation":
        return False
    if scope and meta.get("scope") != scope:
        return False
    if project and project.lower() not in str(meta.get("project_key") or "").lower():
        return False
    if want_type and meta.get("type") != want_type:
        return False
    return True


def _fast_entry_summary_page(
    root: Path,
    cfg: dict,
    *,
    scope: str | None,
    project: str | None,
    want_type: str | None,
    offset: int,
    limit: int,
) -> dict:
    entries: list[dict] = []
    skipped = 0
    has_more = False
    for entry in iter_all_entry_summary_records(root, cfg):
        if not _entry_list_summary_matches(entry, scope=scope, project=project, want_type=want_type):
            continue
        if skipped < offset:
            skipped += 1
            continue
        if len(entries) >= limit:
            has_more = True
            break
        entries.append(_entry_summary(entry))
    _mark_dirty_entries(entries, root, cfg)
    entries = _dedupe_entries_by_identity(entries)
    count = offset + len(entries) + (1 if has_more else 0)
    return {
        "entries": entries,
        "count": count,
        "returned": len(entries),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "count_exact": not has_more,
    }


def _entry_updated_value(entry: dict) -> tuple[dt.datetime, str] | None:
    meta = entry.get("meta", {})
    for key in ("updated_at", "created_at"):
        value = str(meta.get(key) or "").strip()
        parsed = _parse_entry_time(value)
        if parsed is not None:
            return parsed, value
    path = entry.get("path")
    if path:
        try:
            updated = dt.datetime.fromtimestamp(Path(str(path)).stat().st_mtime, tz=dt.timezone.utc).replace(microsecond=0)
        except OSError:
            return None
        return updated, updated.isoformat()
    return None


def _prefer_full_entry(existing: dict, candidate: dict) -> dict:
    existing_updated = _entry_updated_value(existing)
    candidate_updated = _entry_updated_value(candidate)
    if candidate_updated and (not existing_updated or candidate_updated[0] >= existing_updated[0]):
        return candidate
    return existing


def _find_entry_for_read(root: Path, cfg: dict, identifier: str) -> dict | None:
    slug_match: dict | None = None
    id_match: dict | None = None
    for entry in iter_all_entries(root, cfg):
        meta = entry.get("meta", {})
        if identifier == meta.get("slug"):
            slug_match = entry if slug_match is None else _prefer_full_entry(slug_match, entry)
        if identifier == meta.get("id"):
            id_match = entry if id_match is None else _prefer_full_entry(id_match, entry)
    return slug_match or id_match


def _navigation_summaries(root: Path, cfg: dict, project_key_value: str | None = None) -> list[dict]:
    index_entries: list[dict] = []
    latest_by_project: dict[str, tuple[dt.datetime, str]] = {}
    for entry in iter_all_entries(root, cfg):
        meta = entry.get("meta", {})
        if meta.get("type") != "navigation":
            continue
        if project_key_value and meta.get("project_key") != project_key_value:
            continue
        project_key = str(meta.get("project_key") or "")
        updated = _entry_updated_value(entry)
        if updated and (project_key not in latest_by_project or updated[0] > latest_by_project[project_key][0]):
            latest_by_project[project_key] = updated
        if meta.get("slug") == NAVIGATION_SLUG:
            index_entries.append(entry)
    entries: list[dict] = []
    for entry in index_entries:
        summary = _entry_summary(entry)
        project_key = str(summary.get("project_key") or "")
        latest = latest_by_project.get(project_key)
        if latest and latest[1]:
            summary["index_updated_at"] = summary.get("updated_at")
            summary["nav_updated_at"] = latest[1]
            summary["updated_at"] = latest[1]
        entries.append(summary)
    return sorted(
        entries,
        key=lambda item: (
            str(item.get("project_key") or ""),
            0 if item.get("slug") == NAVIGATION_SLUG else 1,
            str(item.get("slug") or ""),
        ),
    )


def _public_nav_draft(draft: dict) -> dict:
    item = dict(draft)
    item.pop("candidates", None)
    item.pop("_path", None)
    return item


def _visible_nav_draft(draft: dict) -> bool:
    return draft.get("status") not in {"accepted", "rejected"}


def _blocking_project_nav_draft(
    root: Path,
    cfg: dict,
    *,
    project_key_value: str,
    workspace_path: str,
) -> dict | None:
    for draft in nav_drafts.list_drafts(root, cfg, project_key_value):
        if draft.get("status") not in {"running", "completed"}:
            continue
        if draft.get("workspace_path") != workspace_path:
            continue
        return draft
    return None


def _is_navigation_child(entry: dict) -> bool:
    meta = entry.get("meta", {})
    return meta.get("type") == "navigation" and meta.get("slug") != NAVIGATION_SLUG


def _is_project_navigation_index(entry: dict | None) -> bool:
    if not entry:
        return False
    meta = entry.get("meta", {})
    return (
        meta.get("type") == "navigation"
        and meta.get("scope") == "project"
        and meta.get("slug") == NAVIGATION_SLUG
        and bool(meta.get("project_key"))
    )


def _entry_matches_query(entry: dict, query: str) -> bool:
    needle = (query or "").strip().lower()
    if not needle:
        return True
    meta = entry.get("meta", {})
    haystack = " ".join(
        [
            str(meta.get("title") or ""),
            str(meta.get("id") or ""),
            str(meta.get("slug") or ""),
            str(meta.get("project_key") or ""),
            " ".join(str(tag) for tag in (meta.get("tags") or [])),
            str(entry.get("body") or ""),
        ]
    ).lower()
    return needle in haystack


def _ok(method: str, data: dict, status: str = "200 OK") -> bytes:
    if method == "HEAD":
        return http_response(status, b"", "application/json; charset=utf-8")
    return json_response(data, status)


def _knowledge_settings(cfg: dict) -> dict:
    kb = cfg.get("knowledge") if isinstance(cfg.get("knowledge"), dict) else default_knowledge_config()
    git = kb.get("git", {}) if isinstance(kb.get("git"), dict) else {}
    curation = kb.get("curation", {}) if isinstance(kb.get("curation"), dict) else {}
    project_nav = kb.get("project_nav", {}) if isinstance(kb.get("project_nav"), dict) else {}
    return {
        "enabled": bool(kb.get("enabled")),
        "path": kb.get("path"),
        "git": {
            "enabled": bool(git.get("enabled")),
            "remote": git.get("remote"),
            "branch": git.get("branch"),
            "auto_pull": bool(git.get("auto_pull")),
            "auto_commit": bool(git.get("auto_commit")),
            "auto_push": bool(git.get("auto_push")),
        },
        "project_nav": {
            "enabled": bool(project_nav.get("enabled", True)),
            "maintain_during_task": bool(project_nav.get("maintain_during_task", True)),
        },
        "curation": {"gate": curation.get("gate")},
    }


def _as_string_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


_ALLOWED_GATES = {"manual", "auto", "off"}
_ALLOWED_ENTRY_STATUSES = {"active", "stale", "deprecated"}
def _optional_bool(value, field: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    clean = str(value).strip().lower()
    if clean in {"1", "true", "yes", "on"}:
        return True
    if clean in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{field} must be a boolean")


def _agent_backend_for_options(cfg: dict, backend: object) -> str:
    allowed = {"codex", "claude"}
    clean = str(backend or "").strip().lower()
    if clean in allowed:
        return clean
    configured = str(cfg.get("backend") or "").strip().lower() if isinstance(cfg, dict) else ""
    return configured if configured in allowed else "claude"


def _apply_settings_patch(root: Path, payload: dict) -> dict:
    """Merge an allow-listed knowledge settings patch into config.json."""
    # Start from the raw on-disk config to avoid baking in every default.
    path = config_path(root)
    raw = read_json(path) if path.exists() else {}
    kb = raw.get("knowledge")
    if not isinstance(kb, dict):
        kb = default_knowledge_config()
    # Hand-written config may have non-dict nested blocks; coerce instead of 500.
    if not isinstance(kb.get("git"), dict):
        kb["git"] = {}
    if not isinstance(kb.get("curation"), dict):
        kb["curation"] = {}
    if not isinstance(kb.get("project_nav"), dict):
        kb["project_nav"] = {}

    if "enabled" in payload:
        kb["enabled"] = bool(payload["enabled"])
    if "path" in payload:
        value = payload["path"]
        kb["path"] = str(value).strip() or None if value is not None else None

    git_patch = payload.get("git")
    if isinstance(git_patch, dict):
        if "enabled" in git_patch:
            kb["git"]["enabled"] = bool(git_patch["enabled"])
        if "remote" in git_patch:
            remote = git_patch["remote"]
            kb["git"]["remote"] = str(remote).strip() or None if remote is not None else None
        if "branch" in git_patch:
            branch = str(git_patch.get("branch") or "").strip()
            if branch:
                kb["git"]["branch"] = branch
        for flag in ("auto_pull", "auto_commit", "auto_push"):
            if flag in git_patch:
                kb["git"][flag] = bool(git_patch[flag])

    curation_patch = payload.get("curation")
    if isinstance(curation_patch, dict) and "gate" in curation_patch:
        gate = str(curation_patch["gate"]).strip().lower()
        if gate not in _ALLOWED_GATES:
            raise ValueError(f"curation gate must be one of {sorted(_ALLOWED_GATES)}")
        kb["curation"]["gate"] = gate

    project_nav_patch = payload.get("project_nav")
    if isinstance(project_nav_patch, dict):
        for flag in ("enabled", "maintain_during_task"):
            if flag in project_nav_patch:
                value = _optional_bool(project_nav_patch.get(flag), f"project_nav.{flag}")
                kb["project_nav"][flag] = True if value is None else value

    raw["knowledge"] = kb
    write_json(path, raw)
    return _knowledge_settings(load_config(root))


def _payload_value(payload: dict, query: dict[str, list[str]], *names: str) -> str:
    for name in names:
        if name in payload and payload.get(name) is not None:
            value = str(payload.get(name) or "").strip()
            if value:
                return value
        if name in query and query.get(name):
            value = str(query.get(name, [""])[0] or "").strip()
            if value:
                return value
    return ""


def _query_int_param(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = str(query.get(key, [""])[0] or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _project_nav_bootstrap_context(root: Path, payload: dict, query: dict[str, list[str]]) -> dict:
    workspace_path = _payload_value(payload, query, "workspace_path", "workspace")
    project_key_value = _payload_value(payload, query, "project_key", "project") or None
    goal = _payload_value(payload, query, "goal") or None
    run_id = _payload_value(payload, query, "run_id", "run")
    task_id = _payload_value(payload, query, "task_id", "task")

    if run_id and (not workspace_path or not goal):
        try:
            plan = require_plan(root, run_id)
        except SystemExit as exc:
            raise FileNotFoundError(f"run not found: {run_id}") from exc
        goal = goal or str(plan.get("goal") or "")
        tasks = [task for task in plan.get("tasks", []) if not task.get("deleted_at")]
        task = next((item for item in tasks if item.get("id") == task_id), None) if task_id else None
        task = task or next((item for item in tasks if item.get("workspace_path")), None)
        workspace_path = workspace_path or str((task or {}).get("workspace_path") or "")
        if not workspace_path:
            workspace_path = str((plan.get("main_agent") or {}).get("workspace_path") or "")

    if not workspace_path:
        raise ValueError("workspace_path or run_id required")
    workspace = Path(workspace_path).expanduser()
    if not workspace.is_dir():
        raise ValueError(f"workspace_path is not a directory: {workspace_path}")
    return {
        "workspace_path": str(workspace),
        "project_key": project_key_value,
        "goal": goal,
        "run_id": run_id or None,
        "task_id": task_id or None,
    }


def knowledge_route_response(
    root: Path,
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> bytes | None:
    if not path.startswith("/api/kb/"):
        return None
    cfg = load_config(root)

    if method in {"GET", "HEAD"} and path == "/api/kb/status":
        return _ok(method, knowledge_status(root, cfg))

    if method in {"GET", "HEAD"} and path == "/api/kb/sync-status":
        return _ok(method, knowledge_sync_status(root, cfg, check_remote=_query_bool_param(query, "remote")))

    if method in {"GET", "HEAD"} and path == "/api/kb/entries":
        scope = str(query.get("scope", [""])[0] or "").strip() or None
        project = str(query.get("project", [""])[0] or "").strip() or None
        search = str(query.get("q", [""])[0] or "").strip()
        kind = str(query.get("kind", [""])[0] or "").strip() or None
        want_type = type_for_kind(kind) if kind in ("solutions", "wiki", "navigation", "worklog") else None
        try:
            limit = _query_int_param(query, "limit", 0)
            offset = max(0, _query_int_param(query, "offset", 0))
        except ValueError as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        if limit < 0:
            return json_response({"error": "limit must be non-negative"}, "400 Bad Request")
        if _query_bool_param(query, "fast") and limit and not search:
            payload = _fast_entry_summary_page(
                root,
                cfg,
                scope=scope,
                project=project,
                want_type=want_type,
                offset=offset,
                limit=limit,
            )
            return _ok(method, payload)
        all_entries = iter_all_entries(root, cfg) if search else iter_all_entry_summaries(root, cfg)
        entries = []
        for entry in all_entries:
            if not _entry_list_summary_matches(entry, scope=scope, project=project, want_type=want_type):
                continue
            if search and not _entry_matches_query(entry, search):
                continue
            entries.append(_entry_summary(entry))
        entries = _dedupe_entries_by_identity(entries)
        total_entries = len(entries)
        page_entries = entries[offset : offset + limit] if limit else entries[offset:]
        _mark_dirty_entries(page_entries, root, cfg)
        source_note_ids = {
            str(entry.get("source_note_id") or "")
            for entry in page_entries
            if entry.get("source_note_id")
        }
        if source_note_ids:
            existing_note_ids = {str(note.get("id") or "") for note in capture.list_notes(root, cfg)}
            for entry in page_entries:
                source_note_id = str(entry.get("source_note_id") or "")
                if source_note_id:
                    entry["source_note_exists"] = source_note_id in existing_note_ids
        payload = {
            "entries": page_entries,
            "count": total_entries,
            "returned": len(page_entries),
            "offset": offset,
            "limit": limit,
            "has_more": bool(offset + len(page_entries) < total_entries),
            "count_exact": True,
        }
        if search:
            pending = list_pending(root, cfg)
            capture_notes = [
                note
                for note in (_note_with_refs(n, pending, all_entries) for n in capture.list_notes(root, cfg))
                if _capture_note_matches_query(note, search)
            ]
            payload["capture_notes"] = capture_notes
            payload["capture_count"] = len(capture_notes)
        return _ok(method, payload)

    if method in {"GET", "HEAD"} and path == "/api/kb/entry":
        identifier = str(query.get("id", [""])[0] or query.get("slug", [""])[0] or "").strip()
        if not identifier:
            return json_response({"error": "id or slug required"}, "400 Bad Request")
        entry = _find_entry_for_read(root, cfg, identifier)
        if entry is None:
            return json_response({"error": f"entry not found: {identifier}"}, "404 Not Found")
        source_note_id = str((entry.get("meta") or {}).get("source_note_id") or "").strip()
        if source_note_id:
            entry = dict(entry)
            meta = dict(entry.get("meta") or {})
            source_note_exists = capture.read_note(root, cfg, source_note_id) is not None
            meta["source_note_exists"] = source_note_exists
            entry["meta"] = meta
            entry["source_note_exists"] = source_note_exists
        _mark_dirty_entries([entry], root, cfg)
        return _ok(method, entry)

    if method in {"GET", "HEAD"} and path == "/api/kb/entry/image":
        identifier = str(query.get("id", [""])[0] or query.get("slug", [""])[0] or "").strip()
        asset_path = str(query.get("path", [""])[0] or query.get("name", [""])[0] or "").strip()
        if not identifier or not asset_path:
            return json_response({"error": "id and path required"}, "400 Bad Request")
        found = read_entry_image(root, cfg, identifier, asset_path)
        if found is None:
            return json_response({"error": "image not found"}, "404 Not Found")
        data, mime = found
        return http_response("200 OK", data if method == "GET" else b"", content_type=mime)

    if method == "POST" and path == "/api/kb/entry/image":
        payload = parse_json_body(body) if body.strip() else {}
        identifier = str(payload.get("id") or payload.get("slug") or "").strip()
        if not identifier:
            return json_response({"error": "id or slug required"}, "400 Bad Request")
        raw = str(payload.get("data") or payload.get("data_url") or "")
        if "," in raw and raw.strip().lower().startswith("data:"):
            raw = raw.split(",", 1)[1]
        try:
            data = base64.b64decode(raw, validate=False)
        except (ValueError, binascii.Error):
            return json_response({"error": "invalid base64 image data"}, "400 Bad Request")
        try:
            entry, image = add_entry_image(root, cfg, identifier, data=data, filename=str(payload.get("filename") or "image"))
        except FileNotFoundError:
            return json_response({"error": f"entry not found: {identifier}"}, "404 Not Found")
        except EntryImageRejected as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        return json_response({"ok": True, "entry": entry, "image": image})

    if method == "PATCH" and path == "/api/kb/entry":
        payload = parse_json_body(body) if body.strip() else {}
        identifier = str(payload.get("id") or payload.get("slug") or "").strip()
        if not identifier:
            return json_response({"error": "id or slug required"}, "400 Bad Request")
        status = payload.get("status")
        if status is not None:
            status = str(status).strip().lower()
            if status not in _ALLOWED_ENTRY_STATUSES:
                return json_response({"error": f"status must be one of {sorted(_ALLOWED_ENTRY_STATUSES)}"}, "400 Bad Request")
        review_after = None
        if payload.get("mark_stale"):
            review_after = utc_now()
        elif "review_after" in payload:
            review_after = str(payload.get("review_after") or "").strip()
        invalid_when = None
        if "invalid_when" in payload:
            invalid_when = str(payload.get("invalid_when") or "").strip()
        try:
            entry = update_entry(
                root,
                cfg,
                identifier,
                title=str(payload["title"]).strip() if "title" in payload else None,
                body=str(payload["body"]) if "body" in payload else None,
                tags=_as_string_list(payload["tags"]) if "tags" in payload else None,
                related_files=_as_string_list(payload["related_files"]) if "related_files" in payload else None,
                status=status,
                review_after=review_after,
                invalid_when=invalid_when,
            )
        except FileNotFoundError:
            return json_response({"error": f"entry not found: {identifier}"}, "404 Not Found")
        git_result = auto_commit_after_change(
            root, f"chore(knowledge): update '{entry.get('meta', {}).get('title', 'entry')}'", cfg
        )
        return json_response({"ok": True, "entry": entry, "git": git_result})

    if method == "DELETE" and path == "/api/kb/entry":
        payload = parse_json_body(body) if body.strip() else {}
        identifier = str(payload.get("id") or payload.get("slug") or query.get("id", [""])[0] or "").strip()
        if not identifier:
            return json_response({"error": "id or slug required"}, "400 Bad Request")
        entry = find_entry(root, cfg, identifier)
        if entry is None:
            return json_response({"error": f"entry not found: {identifier}"}, "404 Not Found")
        if _is_project_navigation_index(entry):
            project_key_value = str(entry.get("meta", {}).get("project_key") or "")
            deleted_paths = delete_project_navigation(root, cfg, project_key_value)
            git_result = auto_commit_after_change(
                root, f"chore(knowledge): reset project nav '{project_key_value}'", cfg
            )
            return json_response({
                "ok": True,
                "deleted": identifier,
                "reset_project_nav": True,
                "project_key": project_key_value,
                "deleted_count": len(deleted_paths),
                "paths": [str(path) for path in deleted_paths],
                "git": git_result,
            })
        try:
            deleted_path = delete_entry(root, cfg, identifier)
        except FileNotFoundError:
            return json_response({"error": f"entry not found: {identifier}"}, "404 Not Found")
        git_result = auto_commit_after_change(root, f"chore(knowledge): delete '{identifier}'", cfg)
        return json_response({"ok": True, "deleted": identifier, "path": str(deleted_path), "git": git_result})

    if method in {"GET", "HEAD"} and path == "/api/kb/project-nav":
        context: dict = {}
        project_key_value = _payload_value({}, query, "project_key", "project") or None
        wants_context = any(name in query for name in ("workspace_path", "workspace", "run_id", "run", "task_id", "task"))
        if not project_key_value and wants_context:
            try:
                context = _project_nav_bootstrap_context(root, {}, query)
            except FileNotFoundError as exc:
                return json_response({"error": str(exc)}, "404 Not Found")
            except ValueError as exc:
                return json_response({"error": str(exc)}, "400 Bad Request")
            project_key_value = str(
                context.get("project_key") or derive_project_key(Path(context["workspace_path"]), goal=context.get("goal"))
            )
        entries = _navigation_summaries(root, cfg, project_key_value)
        return _ok(method, {
            "entries": entries,
            "count": len(entries),
            "project_key": project_key_value,
            "workspace_path": context.get("workspace_path"),
            "run_id": context.get("run_id"),
            "task_id": context.get("task_id"),
        })

    if method in {"GET", "HEAD"} and path == "/api/kb/project-nav/drafts":
        context: dict = {}
        project_key_value = _payload_value({}, query, "project_key", "project") or None
        wants_context = any(name in query for name in ("workspace_path", "workspace", "run_id", "run", "task_id", "task"))
        if not project_key_value and wants_context:
            try:
                context = _project_nav_bootstrap_context(root, {}, query)
            except FileNotFoundError as exc:
                return json_response({"error": str(exc)}, "404 Not Found")
            except ValueError as exc:
                return json_response({"error": str(exc)}, "400 Bad Request")
            project_key_value = str(
                context.get("project_key") or derive_project_key(Path(context["workspace_path"]), goal=context.get("goal"))
            )
        drafts = [
            _public_nav_draft(draft)
            for draft in nav_drafts.list_drafts(root, cfg, project_key_value)
            if _visible_nav_draft(draft)
        ]
        return _ok(method, {
            "drafts": drafts,
            "count": len(drafts),
            "project_key": project_key_value,
            "workspace_path": context.get("workspace_path"),
        })

    if method in {"GET", "HEAD"} and path == "/api/kb/project-nav/draft":
        draft_id = str(query.get("id", [""])[0] or query.get("draft_id", [""])[0] or "").strip()
        if not draft_id:
            return json_response({"error": "draft_id required"}, "400 Bad Request")
        draft = nav_drafts.read_draft(root, cfg, draft_id)
        if draft is None:
            return json_response({"error": f"navigation draft not found: {draft_id}"}, "404 Not Found")
        draft.pop("_path", None)
        return _ok(method, {"draft": draft})

    if method == "POST" and path == "/api/kb/project-nav":
        payload = parse_json_body(body) if body.strip() else {}
        try:
            context = _project_nav_bootstrap_context(root, payload, query)
        except FileNotFoundError as exc:
            return json_response({"error": str(exc)}, "404 Not Found")
        except ValueError as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        try:
            proxy_enabled = _optional_bool(payload.get("proxy_enabled"), "proxy_enabled")
        except ValueError as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        backend = _payload_value(payload, query, "backend") or None
        model = _payload_value(payload, query, "model") or None
        try:
            reasoning_effort = normalize_reasoning_effort(
                _payload_value(payload, query, "reasoning_effort"),
                _agent_backend_for_options(cfg, backend),
            )
        except ValueError as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        project_key_value = context.get("project_key") or derive_project_key(Path(context["workspace_path"]), goal=context.get("goal"))
        if entry_exists(root, cfg, "project", "navigation", project_key_value, NAVIGATION_SLUG):
            return json_response({
                "ok": False,
                "status": "already_exists",
                "already_exists": True,
                "error": "project navigation already exists; reset it before generating again",
                "project_key": project_key_value,
                "workspace_path": context["workspace_path"],
            }, "409 Conflict")
        blocking_draft = _blocking_project_nav_draft(
            root,
            cfg,
            project_key_value=project_key_value,
            workspace_path=context["workspace_path"],
        )
        if blocking_draft is not None:
            status = str(blocking_draft.get("status") or "")
            code = "already_running" if status == "running" else "already_has_draft"
            return json_response({
                "ok": False,
                "status": code,
                code: True,
                "error": (
                    "project navigation generation is already running"
                    if code == "already_running"
                    else "project navigation draft already exists; view, accept, or reject it first"
                ),
                "draft_id": blocking_draft.get("id"),
                "draft": _public_nav_draft(blocking_draft),
                "project_key": project_key_value,
                "workspace_path": context["workspace_path"],
            }, "409 Conflict")
        prompt = build_navigation_bootstrap_prompt(
            {},
            workspace_path=context["workspace_path"],
            project_key_value=project_key_value,
        )
        draft = nav_drafts.create_draft(root, cfg, {
            "status": "running",
            "workspace_path": context["workspace_path"],
            "project_key": project_key_value,
            "run_id": context.get("run_id"),
            "task_id": context.get("task_id"),
            "backend": backend,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "proxy_enabled": proxy_enabled,
            "summary": "Project nav generation running",
            "agent": {"status": "prompt_ready", "prompt_excerpt": _nav_prompt_excerpt(prompt)},
            "agent_log": [
                _nav_agent_log_event(
                    "queued",
                    "Project nav generation queued",
                    backend=backend,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    proxy_enabled=proxy_enabled,
                )
            ],
        })
        dispatch_project_nav_job(
            root,
            cfg,
            draft["id"],
            workspace_path=context["workspace_path"],
            goal=context.get("goal"),
            project_key_value=project_key_value,
            backend=backend,
            model=model,
            reasoning_effort=reasoning_effort,
            proxy_enabled=proxy_enabled,
            agent=project_navigation_agent,
        )
        draft.pop("_path", None)
        return json_response({
            "ok": True,
            "status": "running",
            "draft_id": draft["id"],
            "draft": draft,
            "workspace_path": context["workspace_path"],
            "project_key": project_key_value,
            "run_id": context.get("run_id"),
            "task_id": context.get("task_id"),
        }, "202 Accepted")

    if method == "POST" and path == "/api/kb/project-nav/draft/accept":
        payload = parse_json_body(body) if body.strip() else {}
        draft_id = str(payload.get("draft_id") or payload.get("id") or "").strip()
        if not draft_id:
            return json_response({"error": "draft_id required"}, "400 Bad Request")
        draft = nav_drafts.read_draft(root, cfg, draft_id)
        if draft is None:
            return json_response({"error": f"navigation draft not found: {draft_id}"}, "404 Not Found")
        if draft.get("status") != "completed":
            return json_response({"error": "only completed navigation drafts can be accepted"}, "400 Bad Request")
        candidates = draft.get("candidates") if isinstance(draft.get("candidates"), list) else []
        validation = validate_navigation_candidates(root, cfg, candidates)
        if not validation["ok"]:
            return json_response({"error": "navigation draft validation failed", "validation": validation}, "400 Bad Request")
        init_knowledge_base(root, cfg)
        written: list[str] = []
        for candidate in candidates:
            path_written = write_entry(
                root,
                config=cfg,
                scope=str(candidate.get("scope") or "project"),
                kind="navigation",
                project_key_value=candidate.get("project_key") or draft.get("project_key"),
                title=str(candidate.get("title") or candidate.get("slug") or "Project navigation"),
                body=str(candidate.get("body") or ""),
                slug=str(candidate.get("slug") or ""),
                meta=candidate.get("meta") if isinstance(candidate.get("meta"), dict) else {"type": "navigation"},
            )
            written.append(str(path_written))
        accepted_at = utc_now()
        accepted_draft = {
            "id": draft_id,
            "status": "accepted",
            "accepted_at": accepted_at,
            "project_key": draft.get("project_key"),
            "workspace_path": draft.get("workspace_path"),
            "summary": f"Accepted {len(written)} navigation entries",
            "written_paths": written,
        }
        nav_drafts.delete_draft(root, cfg, draft_id)
        git_result = auto_commit_after_change(
            root, f"chore(knowledge): accept project nav '{draft.get('project_key') or draft_id}'", cfg
        )
        return json_response({
            "ok": True,
            "draft": accepted_draft,
            "draft_deleted": True,
            "written_count": len(written),
            "paths": written,
            "git": git_result,
        })

    if method == "POST" and path == "/api/kb/project-nav/draft/stop":
        payload = parse_json_body(body) if body.strip() else {}
        draft_id = str(payload.get("draft_id") or payload.get("id") or "").strip()
        if not draft_id:
            return json_response({"error": "draft_id required"}, "400 Bad Request")
        draft = nav_drafts.read_draft(root, cfg, draft_id)
        if draft is None:
            return json_response({"error": f"navigation draft not found: {draft_id}"}, "404 Not Found")
        if draft.get("status") != "running":
            return json_response({"error": "only running navigation drafts can be stopped"}, "400 Bad Request")
        stop_result = _stop_nav_agent_process(draft)
        _append_nav_agent_log(root, cfg, draft_id, "stopped", "Project nav generation stopped", **stop_result)
        updated = nav_drafts.update_draft(
            root,
            cfg,
            draft_id,
            status="stopped",
            stopped_at=utc_now(),
            summary="Project nav generation stopped",
            stop=stop_result,
            candidates=[],
        )
        updated.pop("_path", None)
        updated.pop("candidates", None)
        return json_response({"ok": True, "draft": updated, "stop": stop_result})

    if method == "POST" and path == "/api/kb/project-nav/draft/reject":
        payload = parse_json_body(body) if body.strip() else {}
        draft_id = str(payload.get("draft_id") or payload.get("id") or "").strip()
        if not draft_id:
            return json_response({"error": "draft_id required"}, "400 Bad Request")
        draft = nav_drafts.read_draft(root, cfg, draft_id)
        if draft is None:
            return json_response({"error": f"navigation draft not found: {draft_id}"}, "404 Not Found")
        updated = nav_drafts.update_draft(
            root,
            cfg,
            draft_id,
            status="rejected",
            rejected_at=utc_now(),
            summary="Rejected project nav draft",
            candidates=[],
        )
        updated.pop("_path", None)
        updated.pop("candidates", None)
        return json_response({"ok": True, "draft": updated})

    if method == "DELETE" and path == "/api/kb/project-nav":
        payload = parse_json_body(body) if body.strip() else {}
        context: dict = {}
        project_key_value = str(
            payload.get("project_key")
            or payload.get("project")
            or query.get("project_key", [""])[0]
            or query.get("project", [""])[0]
            or ""
        ).strip()
        if not project_key_value:
            try:
                context = _project_nav_bootstrap_context(root, payload, query)
            except FileNotFoundError as exc:
                return json_response({"error": str(exc)}, "404 Not Found")
            except ValueError as exc:
                return json_response({"error": str(exc)}, "400 Bad Request")
            project_key_value = str(
                context.get("project_key") or derive_project_key(Path(context["workspace_path"]), goal=context.get("goal"))
            )
        try:
            deleted_paths = delete_project_navigation(root, cfg, project_key_value)
        except ValueError as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        git_result = auto_commit_after_change(
            root, f"chore(knowledge): reset project nav '{project_key_value}'", cfg
        )
        return json_response({
            "ok": True,
            "reset_project_nav": True,
            "project_key": project_key_value,
            "workspace_path": context.get("workspace_path"),
            "run_id": context.get("run_id"),
            "task_id": context.get("task_id"),
            "deleted_count": len(deleted_paths),
            "paths": [str(path) for path in deleted_paths],
            "git": git_result,
        })

    if method in {"GET", "HEAD"} and path == "/api/kb/pending":
        notes_by_id = {str(note.get("id")): note for note in capture.list_notes(root, cfg)}
        pending = [_candidate_view(candidate, notes_by_id) for candidate in list_pending(root, cfg)]
        return _ok(method, {"pending": pending, "count": len(pending)})

    if method == "POST" and path == "/api/kb/approve":
        payload = parse_json_body(body) if body.strip() else {}
        cid = str(payload.get("candidate_id") or "").strip()
        if not cid:
            return json_response({"error": "candidate_id required"}, "400 Bad Request")
        candidate = next((c for c in list_pending(root, cfg) if c.get("id") == cid), None)
        if candidate is None:
            return json_response({"error": f"no pending candidate: {cid}"}, "404 Not Found")
        source_note_id = _candidate_source_note_id(candidate)
        existing = entry_exists(
            root, cfg,
            candidate.get("scope", "project"),
            candidate.get("kind", "solutions"),
            candidate.get("project_key"),
            candidate.get("slug") or slugify(candidate.get("title", "")),
        )
        entry_path = approve_candidate(root, cfg, cid)
        source_note = {"source_note_id": source_note_id or None, "source_note_deleted": False, "candidate_ids": []}
        if source_note_id:
            note = capture.read_note(root, cfg, source_note_id)
            if note is not None:
                remaining = _pending_for_note(root, cfg, source_note_id, note)
                remaining_ids = [
                    str(item.get("id") or "") for item in remaining if str(item.get("id") or "")
                ]
                if remaining_ids:
                    capture.update_note(
                        root,
                        cfg,
                        source_note_id,
                        status="distilled",
                        candidate_ids=remaining_ids,
                        last_error="",
                    )
                    source_note.update({
                        "source_note_exists": True,
                        "source_note_deleted": False,
                        "candidate_ids": remaining_ids,
                    })
                else:
                    source_note.update({
                        "source_note_exists": False,
                        "source_note_deleted": capture.delete_note(root, cfg, source_note_id),
                        "candidate_ids": [],
                    })
            else:
                source_note["source_note_exists"] = False
        git_result = auto_commit_after_change(
            root, f"chore(knowledge): approve '{candidate.get('title', 'entry')}'", cfg
        )
        return json_response({
            "ok": True,
            "action": "updated" if existing else "created",
            "path": str(entry_path),
            "candidate": {
                "id": cid,
                "kind": candidate.get("kind", "solutions"),
                "scope": candidate.get("scope", "project"),
                "project_key": candidate.get("project_key"),
                "slug": candidate.get("slug") or slugify(candidate.get("title", "")),
                "title": candidate.get("title"),
            },
            "source_note": source_note,
            "git": git_result,
        })

    if method == "POST" and path == "/api/kb/reject":
        payload = parse_json_body(body) if body.strip() else {}
        cid = str(payload.get("candidate_id") or "").strip()
        if not cid:
            return json_response({"error": "candidate_id required"}, "400 Bad Request")
        candidate = next((c for c in list_pending(root, cfg) if c.get("id") == cid), None)
        if candidate is None:
            return json_response({"error": f"no pending candidate: {cid}"}, "404 Not Found")
        source_note_id = _candidate_source_note_id(candidate)
        remove_pending(root, cfg, cid)
        source_note = _set_note_remaining_candidates(root, cfg, source_note_id, empty_status="raw")
        if source_note.get("source_note_id"):
            source_note["source_note_kept"] = bool(source_note.get("source_note_exists"))
        return json_response({"ok": True, "rejected": cid, "source_note": source_note})

    if method in {"GET", "HEAD"} and path == "/api/kb/config":
        return _ok(method, _knowledge_settings(cfg))

    if method == "PATCH" and path == "/api/kb/config":
        payload = parse_json_body(body) if body.strip() else {}
        try:
            settings = _apply_settings_patch(root, payload)
        except ValueError as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        return json_response({"ok": True, "knowledge": settings})

    if method == "POST" and path == "/api/kb/sync":
        payload = parse_json_body(body) if body.strip() else {}
        message = str(payload.get("message") or f"chore(knowledge): manual web sync {utc_now()}").strip()
        try:
            pull_value = _optional_bool(payload.get("pull", True), "pull")
            push_value = _optional_bool(payload.get("push", True), "push")
        except ValueError as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        do_pull = True if pull_value is None else pull_value
        do_push = True if push_value is None else push_value
        result = knowledge_sync(root, cfg, message=message, do_pull=do_pull, do_push=do_push)
        return json_response({"ok": bool(result.get("ok")), "sync": result})

    # --- Capture inbox (raw notes) ------------------------------------------ #
    if method == "POST" and path == "/api/kb/capture/distill":
        from aha_cli.services.knowledge_capture_distill import normalize_distill_mode

        payload = parse_json_body(body) if body.strip() else {}
        note_id = str(payload.get("id") or "").strip()
        note = capture.read_note(root, cfg, note_id)
        if note is None:
            return json_response({"error": f"capture note not found: {note_id}"}, "404 Not Found")
        try:
            proxy_enabled = _optional_bool(payload.get("proxy_enabled"), "proxy_enabled")
        except ValueError as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        try:
            distill_mode = normalize_distill_mode(payload.get("distill_mode") or payload.get("mode"))
        except ValueError as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        try:
            reasoning_effort = normalize_reasoning_effort(
                payload.get("reasoning_effort"),
                _agent_backend_for_options(cfg, payload.get("backend")),
            )
        except ValueError as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        # Mark distilling synchronously so an immediate poll observes the job,
        # then run the slow model call off the request thread.
        capture.update_note(root, cfg, note_id, status="distilling", last_error="")
        dispatch_distill_job(
            root,
            cfg,
            note_id,
            payload.get("backend"),
            payload.get("model"),
            proxy_enabled,
            distill_mode,
            reasoning_effort,
        )
        return json_response({
            "ok": True,
            "id": note_id,
            "status": "distilling",
            "distill_mode": distill_mode,
            "reasoning_effort": reasoning_effort,
        })

    if method in {"GET", "HEAD"} and path == "/api/kb/capture/distill-log":
        note_id = str(query.get("id", [""])[0] or "").strip()
        log_id = str(query.get("log_id", [""])[0] or "").strip() or None
        log = capture.read_distill_log(root, cfg, note_id, log_id)
        if log is None:
            return json_response({"error": "distill log not found"}, "404 Not Found")
        view = dict(log)
        view.pop("_path", None)
        return _ok(method, {"log": view})

    if method in {"GET", "HEAD"} and path == "/api/kb/capture":
        note_id = str(query.get("id", [""])[0] or "").strip()
        pending = list_pending(root, cfg)
        entries = iter_all_entries(root, cfg)
        if note_id:
            raw_note = capture.read_note(root, cfg, note_id)
            note = _note_with_refs(raw_note, pending, entries) if raw_note is not None else None
            if note is None:
                return json_response({"error": f"capture note not found: {note_id}"}, "404 Not Found")
            return _ok(method, note)
        search = str(query.get("q", [""])[0] or "").strip()
        notes = [_note_with_refs(n, pending, entries) for n in capture.list_notes(root, cfg)]
        if search:
            notes = [note for note in notes if _capture_note_matches_query(note, search)]
        return _ok(method, {"notes": notes, "count": len(notes)})

    if method == "POST" and path == "/api/kb/capture":
        payload = parse_json_body(body) if body.strip() else {}
        text = str(payload.get("text") or "")
        if not text.strip():
            return json_response({"error": "text is required"}, "400 Bad Request")
        note = capture.create_note(
            root, cfg, text=text,
            scope_hint=str(payload.get("scope_hint") or "personal"),
            title=payload.get("title"),
        )
        return json_response({"ok": True, "note": _note_view(note)})

    if method == "PATCH" and path == "/api/kb/capture":
        payload = parse_json_body(body) if body.strip() else {}
        note_id = str(payload.get("id") or "").strip()
        try:
            note = capture.update_note(
                root, cfg, note_id,
                text=payload.get("text"),
                scope_hint=payload.get("scope_hint"),
                title=payload.get("title"),
            )
        except FileNotFoundError:
            return json_response({"error": f"capture note not found: {note_id}"}, "404 Not Found")
        return json_response({"ok": True, "note": _note_view(note)})

    if method == "DELETE" and path == "/api/kb/capture":
        payload = parse_json_body(body) if body.strip() else {}
        note_id = str(payload.get("id") or "").strip()
        if not capture.delete_note(root, cfg, note_id):
            return json_response({"error": f"capture note not found: {note_id}"}, "404 Not Found")
        return json_response({"ok": True, "deleted": note_id})

    # --- Capture note images (Phase 5a) ------------------------------------- #
    if method in {"GET", "HEAD"} and path == "/api/kb/capture/image":
        note_id = str(query.get("id", [""])[0] or "").strip()
        name = str(query.get("name", [""])[0] or "").strip()
        found = capture.read_note_image(root, cfg, note_id, name)
        if found is None:
            return json_response({"error": "image not found"}, "404 Not Found")
        data, mime = found
        return http_response("200 OK", data if method == "GET" else b"", content_type=mime)

    if method == "POST" and path == "/api/kb/capture/image":
        payload = parse_json_body(body) if body.strip() else {}
        note_id = str(payload.get("id") or "").strip()
        raw = str(payload.get("data") or payload.get("data_url") or "")
        if "," in raw and raw.strip().lower().startswith("data:"):
            raw = raw.split(",", 1)[1]
        try:
            data = base64.b64decode(raw, validate=False)
        except (ValueError, binascii.Error):
            return json_response({"error": "invalid base64 image data"}, "400 Bad Request")
        try:
            image = capture.add_note_image(
                root,
                cfg,
                note_id,
                data=data,
                filename=str(payload.get("filename") or "image"),
                append_ref=payload.get("append") is not False,
            )
        except FileNotFoundError:
            return json_response({"error": f"capture note not found: {note_id}"}, "404 Not Found")
        except capture.ImageRejected as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        return json_response({"ok": True, "image": image})

    if method == "DELETE" and path == "/api/kb/capture/image":
        payload = parse_json_body(body) if body.strip() else {}
        note_id = str(payload.get("id") or "").strip()
        name = str(payload.get("name") or "").strip()
        if not capture.remove_note_image(root, cfg, note_id, name):
            return json_response({"error": "image not found"}, "404 Not Found")
        return json_response({"ok": True, "deleted": name})

    return None
