"""Distill a raw capture note into pending knowledge candidates (Phase 3).

This is the middle stage of the capture channel:

    raw note --[THIS: one-shot agent]--> pending candidate --[approve]--> entry

A user dumps messy raw material into the capture inbox; on demand, an agent is
asked to split / classify / structure / dedup it into 0..N reusable knowledge
candidates, emitted as the same ``<aha_knowledge_candidates>`` JSON sidecar the
task final/memo path uses. Candidates always land in the manual review queue
(``.pending``) — raw dumps are inherently unvetted.

AHA exposes no stable in-process "prompt -> reply" API (backends run as
subprocess CLIs), so the model call is isolated behind a narrow seam: the
``agent`` callable ``CaptureAgent``. The default seam wraps the existing
``run_claude_exec`` / ``run_codex_exec`` chain (no new dependency); tests inject
a deterministic stub so the parse -> normalize -> enqueue -> mark-distilled
pipeline is fully covered without invoking a real model.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.knowledge_agent_progress import agent_log_event, summarize_agent_progress, trim_agent_log
from aha_cli.services.knowledge_distill import (
    filter_project_nav_candidates,
    normalize_sidecar_candidates,
    navigation_distill_summary,
    ensure_navigation_parent_entries,
)
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.services.proxy import backend_proxy_config, normalize_proxy_value
from aha_cli.store.knowledge import (
    enqueue_candidate,
    init_knowledge_base,
    iter_all_entries,
    kind_for_type,
    list_pending,
    remove_pending,
)
from aha_cli.store.knowledge_capture import create_distill_log, read_distill_log, read_note, update_distill_log, update_note
from aha_cli.store.knowledge_sidecar import split_knowledge_sidecar

# Seam: raw context -> agent reply text (expected to contain the sidecar block).
CaptureAgent = Callable[[dict], str]


class CaptureAgentError(RuntimeError):
    """Raised when the real capture agent cannot produce a reply."""


def _image_manifest(note: dict) -> str:
    """A text-only manifest of attached images.

    The backend exec chain (build_claude_exec_command / build_codex_exec_command)
    has no image-input flag, so images are NOT visually analyzed here. We list
    them as evidence the user attached, and say so plainly — never pretend the
    model saw the pixels.
    """
    images = note.get("images") or []
    if not images:
        return ""
    lines: list[str] = []
    for img in images:
        lines.append(
            f"- {img.get('original') or img.get('name')} "
            f"({img.get('mime')}, {int(img.get('size') or 0)} bytes, path: {img.get('path')})"
        )
    return "\n" + render_prompt_template("knowledge_capture_image_manifest.md", images="\n".join(lines)).rstrip()


def build_capture_prompt(note: dict) -> str:
    """Prompt instructing an agent to turn one raw note into KB candidates."""
    scope_hint = str(note.get("scope_hint") or "personal")
    title = str(note.get("title") or "").strip()
    text = str(note.get("text") or "")
    manifest = _image_manifest(note)
    return render_prompt_template(
        "knowledge_capture_prompt.md",
        scope_hint=scope_hint,
        title=title,
        body=text,
        image_manifest=manifest,
    )


def _effective_proxy_enabled(config: dict | None, backend: str, proxy_enabled: bool | None) -> bool:
    if proxy_enabled is not None:
        return bool(proxy_enabled)
    return bool(backend_proxy_config(config, backend).get("enabled"))


def _backend_proxy_env(config: dict | None, backend: str, proxy_enabled: bool | None = None) -> dict[str, str]:
    proxy = backend_proxy_config(config, backend)
    if not _effective_proxy_enabled(config, backend, proxy_enabled):
        return {}
    values = {
        "HTTP_PROXY": normalize_proxy_value(proxy.get("http_proxy")),
        "HTTPS_PROXY": normalize_proxy_value(proxy.get("https_proxy")),
        "NO_PROXY": normalize_proxy_value(proxy.get("no_proxy")),
    }
    env: dict[str, str] = {}
    for key, value in values.items():
        if value:
            env[key] = value
            env[key.lower()] = value
    return env


def default_capture_agent(context: dict) -> str:
    """Real seam: run the note through the existing backend exec chain.

    Best-effort and dependency-free (reuses run_claude_exec / run_codex_exec).
    Kept replaceable: callers may pass their own ``agent`` to skip this.
    """
    model = context.get("model")
    config = context.get("config") if isinstance(context.get("config"), dict) else {}
    backend = _effective_backend(config, context.get("backend"))
    if backend == "codex" and not model:
        model = ((config.get("codex") or {}) if isinstance(config, dict) else {}).get("model")
    if backend == "claude" and not model:
        model = ((config.get("claude") or {}) if isinstance(config, dict) else {}).get("model")
    prompt = str(context.get("prompt") or "")
    cwd = Path(context.get("cwd") or Path.cwd())
    proxy_env = _backend_proxy_env(config, backend, context.get("proxy_enabled"))
    progress_callback = context.get("progress_callback")
    if callable(progress_callback):
        progress_callback(
            "backend_started",
            {"backend": backend, "model": model, "cwd": str(cwd), "proxy_enabled": context.get("proxy_enabled")},
        )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "capture_reply.txt"
            if backend == "codex":
                from aha_cli.backends.codex import codex_config_for_model, run_codex_exec

                raw_codex_config = (config.get("codex") or {}) if isinstance(config, dict) else {}
                codex_config = codex_config_for_model(raw_codex_config, model)
                codex_bin = str(raw_codex_config.get("bin") or "codex")
                _, reply, _ = run_codex_exec(
                    prompt, cwd=cwd, output_file=output_file,
                    codex_bin=codex_bin, model=model, sandbox="read-only",
                    approval="never", codex_config=codex_config, proxy_env=proxy_env,
                    event_callback=progress_callback if callable(progress_callback) else None,
                    start_new_session=True,
                )
            else:
                from aha_cli.backends.claude import claude_cli_model, claude_config_for_model, run_claude_exec

                raw_claude_config = (config.get("claude") or {}) if isinstance(config, dict) else {}
                claude_bin = str(raw_claude_config.get("bin") or "claude")
                claude_config = claude_config_for_model(raw_claude_config, model)
                command_model = claude_cli_model(model, claude_config)
                _, reply, _ = run_claude_exec(
                    prompt, cwd=cwd, output_file=output_file,
                    claude_bin=claude_bin, model=command_model, permission_mode="plan",
                    claude_config=claude_config, proxy_env=proxy_env,
                    event_callback=progress_callback if callable(progress_callback) else None,
                    start_new_session=True,
                )
            if callable(progress_callback):
                progress_callback("backend_finished", {"reply_chars": len(reply or "")})
            return reply or ""
    except Exception as exc:  # noqa: BLE001 - surface as a typed error to the caller
        raise CaptureAgentError(str(exc)) from exc


def _downgrade_unbound_project(candidate: dict) -> dict:
    """A capture candidate marked `project` but lacking a key has no project to
    bind to, so file it under `personal` rather than create an invalid entry."""
    if candidate.get("scope") == "project" and not candidate.get("project_key"):
        candidate["scope"] = "personal"
        candidate["project_key"] = None
    return candidate


def _candidate_source_note_id(candidate: dict) -> str:
    source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    return str(candidate.get("source_note_id") or source.get("note_id") or "").strip()


def _remove_pending_for_note(root: Path, config: dict | None, note: dict) -> None:
    """Drop every pending candidate produced by this note, including older
    records from before note.candidate_ids was reliable."""
    note_id = str(note.get("id") or "").strip()
    old_ids = {str(item) for item in (note.get("candidate_ids") or []) if str(item)}
    for candidate in list_pending(root, config):
        candidate_id = str(candidate.get("id") or "").strip()
        if candidate_id in old_ids or (note_id and _candidate_source_note_id(candidate) == note_id):
            remove_pending(root, config, candidate_id)


def _entries_for_note(root: Path, config: dict | None, note_id: str) -> list[dict]:
    note_id = str(note_id or "").strip()
    if not note_id:
        return []
    return [
        entry for entry in iter_all_entries(root, config)
        if str((entry.get("meta") or {}).get("source_note_id") or "").strip() == note_id
    ]


def _entry_kind(entry: dict) -> str:
    return kind_for_type((entry.get("meta") or {}).get("type"))


def _candidate_matches_entry(candidate: dict, entry: dict) -> bool:
    meta = entry.get("meta") or {}
    if str(candidate.get("kind") or "solutions") != _entry_kind(entry):
        return False
    if str(candidate.get("scope") or "project") != str(meta.get("scope") or "project"):
        return False
    if (candidate.get("project_key") or None) != (meta.get("project_key") or None):
        return False
    cand_slug = str(candidate.get("slug") or "").strip()
    return not cand_slug or cand_slug == str(meta.get("slug") or "").strip()


def _bind_candidates_to_existing_entries(candidates: list[dict], existing_entries: list[dict]) -> list[dict]:
    """Make redistill candidates update existing entries from the same note
    instead of creating a parallel pending item with a new slug."""
    if not candidates or not existing_entries:
        return candidates
    used: set[str] = set()
    for candidate in candidates:
        match: dict | None = None
        for entry in existing_entries:
            entry_id = str((entry.get("meta") or {}).get("id") or entry.get("path") or "")
            if entry_id in used:
                continue
            if _candidate_matches_entry(candidate, entry):
                match = entry
                break
        if match is None and len(candidates) == 1 and len(existing_entries) == 1:
            match = existing_entries[0]
        if match is None:
            continue
        meta = match.get("meta") or {}
        entry_id = str(meta.get("id") or match.get("path") or "")
        used.add(entry_id)
        candidate["scope"] = str(meta.get("scope") or candidate.get("scope") or "project")
        candidate["kind"] = _entry_kind(match)
        candidate["project_key"] = meta.get("project_key") if candidate["scope"] == "project" else None
        candidate["slug"] = meta.get("slug")
        candidate["action"] = "update"
        if meta.get("id"):
            candidate["updates_entry_id"] = meta.get("id")
    return candidates


def _effective_backend(config: dict | None, backend: str | None) -> str:
    allowed = {"codex", "claude"}
    clean = str(backend or "").strip().lower()
    if clean in allowed:
        return clean
    if isinstance(config, dict):
        configured = str(config.get("backend") or "").strip().lower()
        if configured in allowed:
            return configured
    return "claude"


def _effective_model(config: dict | None, backend: str, model: str | None) -> str | None:
    if model:
        return model
    if isinstance(config, dict):
        backend_cfg = config.get(backend)
        if isinstance(backend_cfg, dict):
            return backend_cfg.get("model")
    return None


def _append_distill_agent_log(root: Path, config: dict | None, note_id: str, log_id: str, event: dict) -> None:
    try:
        log = read_distill_log(root, config, note_id, log_id)
        if log is None:
            return
        current = log.get("agent_log") if isinstance(log.get("agent_log"), list) else []
        update_distill_log(root, config, note_id, log_id, agent_log=trim_agent_log([*current, event]))
    except FileNotFoundError:
        return


def _progress_logger(root: Path, config: dict | None, note_id: str, log_id: str):
    def _log(event_type: str, data: dict | None = None) -> None:
        summary = summarize_agent_progress(event_type, data)
        if summary is None:
            return
        _append_distill_agent_log(
            root,
            config,
            note_id,
            log_id,
            agent_log_event(str(summary.pop("stage")), str(summary.pop("message")), **summary),
        )

    return _log


def distill_note(
    root: Path,
    config: dict | None,
    note_id: str,
    *,
    backend: str | None = None,
    model: str | None = None,
    proxy_enabled: bool | None = None,
    agent: CaptureAgent | None = None,
) -> dict:
    """Distill one raw note into pending candidates; mark the note distilled.

    Re-running replaces the note's previously enqueued candidates so the inbox
    stays idempotent per note. Returns a result dict (never raises for a missing
    note or an agent failure — those come back as ``ok: False``).
    """
    note = read_note(root, config, note_id)
    if note is None:
        return {"ok": False, "error": f"capture note not found: {note_id}"}

    prompt = build_capture_prompt(note)
    effective_backend = _effective_backend(config, backend)
    effective_model = _effective_model(config, effective_backend, model)
    effective_proxy_enabled = _effective_proxy_enabled(config, effective_backend, proxy_enabled)
    log = create_distill_log(root, config, note_id, {
        "backend": effective_backend,
        "model": effective_model,
        "proxy_enabled": effective_proxy_enabled,
        "status": "running",
        "prompt": prompt,
        "started_at": utc_now(),
        "agent_log": [
            agent_log_event(
                "running",
                "Capture distill queued for agent",
                backend=effective_backend,
                model=effective_model,
                proxy_enabled=effective_proxy_enabled,
            )
        ],
    })
    log_id = log["id"]
    agent_fn = agent or default_capture_agent
    progress_callback = _progress_logger(root, config, note_id, log_id)
    try:
        reply = agent_fn({
            "prompt": prompt,
            "note": note,
            "backend": effective_backend,
            "model": effective_model,
            "proxy_enabled": effective_proxy_enabled,
            "config": config,
            "cwd": root,
            "progress_callback": progress_callback,
        })
    except CaptureAgentError as exc:
        error = f"capture agent failed: {exc}"
        _append_distill_agent_log(root, config, note_id, log_id, agent_log_event("failed", error))
        update_distill_log(root, config, note_id, log_id, status="error", error=error, finished_at=utc_now())
        return {"ok": False, "error": error, "log_id": log_id}

    _, raw_candidates, sidecar_error = split_knowledge_sidecar(reply or "")
    if raw_candidates is None:
        error = sidecar_error or "no knowledge candidates in agent reply"
        _append_distill_agent_log(root, config, note_id, log_id, agent_log_event("failed", error))
        update_distill_log(root, config, note_id, log_id, status="error", reply=reply or "", error=error, finished_at=utc_now())
        return {"ok": False, "error": error, "log_id": log_id}

    scope_hint = str(note.get("scope_hint") or "personal")
    for raw in raw_candidates:
        raw.setdefault("scope", scope_hint)
    source = {"source_type": "capture_note", "note_id": note_id}
    normalized = []
    for c in normalize_sidecar_candidates({"project_key": None, "source": source}, raw_candidates):
        c = _downgrade_unbound_project(c)
        # Explicit candidate->note link so approve can promote the note's assets
        # (a reverse lookup via note.candidate_ids remains as a compat path).
        c["source_note_id"] = note_id
        normalized.append(c)
    normalized = _bind_candidates_to_existing_entries(normalized, _entries_for_note(root, config, note_id))
    normalized, skipped_navigation = filter_project_nav_candidates(root, config, normalized, {"source_type": "capture_note"})
    normalized = ensure_navigation_parent_entries(root, config, normalized, {"source": source})

    init_knowledge_base(root, config)
    # Re-run replacement: drop every pending candidate from this note, including
    # older records whose ids are no longer in note.candidate_ids.
    _remove_pending_for_note(root, config, note)

    enqueued_ids: list[str] = []
    enqueued_paths: list[str] = []
    for cand in normalized:
        path = enqueue_candidate(root, config, cand)
        enqueued_paths.append(str(path))
        enqueued_ids.append(Path(path).stem)
    navigation = navigation_distill_summary(
        normalized,
        gate="manual",
        enqueued=enqueued_paths,
        skipped=skipped_navigation,
    )

    _append_distill_agent_log(
        root,
        config,
        note_id,
        log_id,
        agent_log_event("completed", f"{len(enqueued_ids)} candidate(s) ready for review", candidates=len(enqueued_ids)),
    )
    update_note(root, config, note_id, status="distilled", candidate_ids=enqueued_ids, last_error="")
    update_distill_log(
        root, config, note_id, log_id,
        status="distilled",
        reply=reply or "",
        raw_candidates=raw_candidates,
        candidate_ids=enqueued_ids,
        navigation=navigation,
        finished_at=utc_now(),
    )
    return {
        "ok": True,
        "note_id": note_id,
        "candidates": len(enqueued_ids),
        "candidate_ids": enqueued_ids,
        "log_id": log_id,
        "navigation": navigation,
    }


def run_distill_job(
    root: Path,
    config: dict | None,
    note_id: str,
    *,
    backend: str | None = None,
    model: str | None = None,
    proxy_enabled: bool | None = None,
    agent: CaptureAgent | None = None,
) -> dict:
    """Run distillation as a job that drives the note's status state machine.

    ``raw -> distilling -> distilled | error``. This is what the web background
    thread calls so a slow model call never blocks the HTTP handler; the note's
    ``status`` is the pollable job record (no separate job store needed).
    """
    try:
        update_note(root, config, note_id, status="distilling", last_error="")
    except FileNotFoundError:
        return {"ok": False, "error": f"capture note not found: {note_id}"}
    result = distill_note(root, config, note_id, backend=backend, model=model, proxy_enabled=proxy_enabled, agent=agent)
    if not result.get("ok"):
        try:
            update_note(root, config, note_id, status="error", last_error=result.get("error", "distill failed"))
        except FileNotFoundError:
            pass
    return result
