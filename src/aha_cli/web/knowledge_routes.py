"""HTTP API for the knowledge base Web console (Phase 4b).

Root-scoped JSON endpoints under /api/kb for browsing entries, reviewing the
pending curation queue (approve/reject), and reading/updating the knowledge
settings (enabled / path / git remote+branch+auto flags / curation gate).

These mirror the conventions in web/system_routes.py and are served from
web/server.py. The matching UI lives in web/static/knowledge.html.
"""

from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import default_knowledge_config, utc_now
from aha_cli.services.knowledge_git import auto_commit_after_change
from aha_cli.store.config import load_config
from aha_cli.store.io import read_json, write_json
from aha_cli.store.knowledge import (
    approve_candidate,
    delete_entry,
    entry_exists,
    find_entry,
    iter_all_entries,
    knowledge_status,
    list_pending,
    remove_pending,
    slugify,
    type_for_kind,
    update_entry,
)
from aha_cli.store.paths import config_path
from aha_cli.web.http_utils import http_response, json_response, parse_json_body


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
        "updated_at": meta.get("updated_at"),
        "path": entry.get("path"),
    }


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


def _apply_settings_patch(root: Path, payload: dict) -> dict:
    """Merge an allow-listed knowledge settings patch into config.json."""
    # Start from the raw on-disk config to avoid baking in every default.
    path = config_path(root)
    raw = read_json(path) if path.exists() else {}
    kb = raw.get("knowledge")
    if not isinstance(kb, dict):
        kb = default_knowledge_config()
    # Hand-written config may have non-dict git/curation; coerce instead of 500.
    if not isinstance(kb.get("git"), dict):
        kb["git"] = {}
    if not isinstance(kb.get("curation"), dict):
        kb["curation"] = {}

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

    raw["knowledge"] = kb
    write_json(path, raw)
    return _knowledge_settings(load_config(root))


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
        try:
            deleted_path = delete_entry(root, cfg, identifier)
        except FileNotFoundError:
            return json_response({"error": f"entry not found: {identifier}"}, "404 Not Found")
        git_result = auto_commit_after_change(root, f"chore(knowledge): delete '{identifier}'", cfg)
        return json_response({"ok": True, "deleted": identifier, "path": str(deleted_path), "git": git_result})

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
            slugify(candidate.get("title", "")),
        )
        entry_path = approve_candidate(root, cfg, cid)
        git_result = auto_commit_after_change(
            root, f"chore(knowledge): approve '{candidate.get('title', 'entry')}'", cfg
        )
        return json_response({
            "ok": True,
            "action": "updated" if existing else "created",
            "path": str(entry_path),
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

    return None
