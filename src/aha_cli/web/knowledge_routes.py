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
from pathlib import Path

from aha_cli.domain.models import default_knowledge_config, utc_now
from aha_cli.services.knowledge_git import auto_commit_after_change
from aha_cli.services.knowledge_navigation import (
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
    knowledge_status,
    list_pending,
    project_key as derive_project_key,
    remove_pending,
    slugify,
    type_for_kind,
    update_entry,
    write_entry,
)
from aha_cli.store import knowledge_capture as capture
from aha_cli.store.paths import config_path
from aha_cli.web.http_utils import http_response, json_response, parse_json_body


def _default_dispatch_distill_job(root: Path, cfg: dict, note_id: str, backend, model, proxy_enabled=None) -> None:
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
        kwargs={"backend": backend, "model": model, "proxy_enabled": proxy_enabled},
        daemon=True,
    ).start()


def _nav_agent_log_event(stage: str, message: str, **extra) -> dict:
    event = {"at": utc_now(), "stage": stage, "message": message}
    event.update({key: value for key, value in extra.items() if value not in (None, "", [])})
    return event


def _append_nav_agent_log(root: Path, cfg: dict, draft_id: str, stage: str, message: str, **extra) -> None:
    draft = nav_drafts.read_draft(root, cfg, draft_id)
    if draft is None:
        return
    log = draft.get("agent_log") if isinstance(draft.get("agent_log"), list) else []
    nav_drafts.update_draft(
        root,
        cfg,
        draft_id,
        agent_log=[*log, _nav_agent_log_event(stage, message, **extra)],
    )


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
    agent=None,
) -> dict:
    _append_nav_agent_log(
        root,
        cfg,
        draft_id,
        "running",
        "Preparing workspace context and calling project navigation agent",
        backend=backend,
        model=model,
        proxy_enabled=proxy_enabled,
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
            agent=agent,
        )
    except Exception as exc:  # noqa: BLE001 - background job must be recorded, not crash the server
        result = {"ok": False, "error": str(exc), "candidates": 0}

    current = nav_drafts.read_draft(root, cfg, draft_id)
    if current is None or current.get("status") == "rejected":
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


def _entry_summary(entry: dict) -> dict:
    meta = entry.get("meta", {})
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
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "path": entry.get("path"),
    }


def _navigation_summaries(root: Path, cfg: dict, project_key_value: str | None = None) -> list[dict]:
    entries: list[dict] = []
    for entry in iter_all_entries(root, cfg):
        meta = entry.get("meta", {})
        if meta.get("type") != "navigation":
            continue
        if meta.get("slug") != NAVIGATION_SLUG:
            continue
        if project_key_value and meta.get("project_key") != project_key_value:
            continue
        entries.append(_entry_summary(entry))
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

    if method in {"GET", "HEAD"} and path == "/api/kb/entries":
        scope = str(query.get("scope", [""])[0] or "").strip() or None
        project = str(query.get("project", [""])[0] or "").strip() or None
        search = str(query.get("q", [""])[0] or "").strip()
        kind = str(query.get("kind", [""])[0] or "").strip() or None
        want_type = type_for_kind(kind) if kind in ("solutions", "wiki", "navigation") else None
        entries = []
        for entry in iter_all_entries(root, cfg):
            meta = entry.get("meta", {})
            if meta.get("type") == "navigation":
                continue
            if scope and meta.get("scope") != scope:
                continue
            if project and project.lower() not in str(meta.get("project_key") or "").lower():
                continue
            if want_type and meta.get("type") != want_type:
                continue
            if search and not _entry_matches_query(entry, search):
                continue
            entries.append(_entry_summary(entry))
        return _ok(method, {"entries": entries, "count": len(entries)})

    if method in {"GET", "HEAD"} and path == "/api/kb/entry":
        identifier = str(query.get("id", [""])[0] or query.get("slug", [""])[0] or "").strip()
        if not identifier:
            return json_response({"error": "id or slug required"}, "400 Bad Request")
        entry = find_entry(root, cfg, identifier)
        if entry is None:
            return json_response({"error": f"entry not found: {identifier}"}, "404 Not Found")
        return _ok(method, entry)

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
        draft = nav_drafts.create_draft(root, cfg, {
            "status": "running",
            "workspace_path": context["workspace_path"],
            "project_key": project_key_value,
            "run_id": context.get("run_id"),
            "task_id": context.get("task_id"),
            "backend": backend,
            "model": model,
            "proxy_enabled": proxy_enabled,
            "summary": "Project nav generation running",
            "agent_log": [
                _nav_agent_log_event(
                    "queued",
                    "Project nav generation queued",
                    backend=backend,
                    model=model,
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
        return _ok(method, {"pending": list_pending(root, cfg), "count": len(list_pending(root, cfg))})

    if method == "POST" and path == "/api/kb/approve":
        payload = parse_json_body(body) if body.strip() else {}
        cid = str(payload.get("candidate_id") or "").strip()
        if not cid:
            return json_response({"error": "candidate_id required"}, "400 Bad Request")
        candidate = next((c for c in list_pending(root, cfg) if c.get("id") == cid), None)
        if candidate is None:
            return json_response({"error": f"no pending candidate: {cid}"}, "404 Not Found")
        existing = entry_exists(
            root, cfg,
            candidate.get("scope", "project"),
            candidate.get("kind", "solutions"),
            candidate.get("project_key"),
            candidate.get("slug") or slugify(candidate.get("title", "")),
        )
        entry_path = approve_candidate(root, cfg, cid)
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
            "git": git_result,
        })

    if method == "POST" and path == "/api/kb/reject":
        payload = parse_json_body(body) if body.strip() else {}
        cid = str(payload.get("candidate_id") or "").strip()
        if not cid:
            return json_response({"error": "candidate_id required"}, "400 Bad Request")
        if not remove_pending(root, cfg, cid):
            return json_response({"error": f"no pending candidate: {cid}"}, "404 Not Found")
        return json_response({"ok": True, "rejected": cid})

    if method in {"GET", "HEAD"} and path == "/api/kb/config":
        return _ok(method, _knowledge_settings(cfg))

    if method == "PATCH" and path == "/api/kb/config":
        payload = parse_json_body(body) if body.strip() else {}
        try:
            settings = _apply_settings_patch(root, payload)
        except ValueError as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        return json_response({"ok": True, "knowledge": settings})

    # --- Capture inbox (raw notes) ------------------------------------------ #
    if method == "POST" and path == "/api/kb/capture/distill":
        payload = parse_json_body(body) if body.strip() else {}
        note_id = str(payload.get("id") or "").strip()
        note = capture.read_note(root, cfg, note_id)
        if note is None:
            return json_response({"error": f"capture note not found: {note_id}"}, "404 Not Found")
        try:
            proxy_enabled = _optional_bool(payload.get("proxy_enabled"), "proxy_enabled")
        except ValueError as exc:
            return json_response({"error": str(exc)}, "400 Bad Request")
        # Mark distilling synchronously so an immediate poll observes the job,
        # then run the slow model call off the request thread.
        capture.update_note(root, cfg, note_id, status="distilling", last_error="")
        dispatch_distill_job(root, cfg, note_id, payload.get("backend"), payload.get("model"), proxy_enabled)
        return json_response({"ok": True, "id": note_id, "status": "distilling"})

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
        if note_id:
            note = _note_view(capture.read_note(root, cfg, note_id))
            if note is None:
                return json_response({"error": f"capture note not found: {note_id}"}, "404 Not Found")
            return _ok(method, note)
        notes = [_note_view(n) for n in capture.list_notes(root, cfg)]
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
            image = capture.add_note_image(root, cfg, note_id, data=data, filename=str(payload.get("filename") or "image"))
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
