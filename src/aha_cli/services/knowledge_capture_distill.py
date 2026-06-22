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
from aha_cli.services.knowledge_distill import (
    filter_project_nav_candidates,
    normalize_sidecar_candidates,
    navigation_distill_summary,
    ensure_navigation_parent_entries,
)
from aha_cli.services.proxy import backend_proxy_config, normalize_proxy_value
from aha_cli.store.knowledge import enqueue_candidate, init_knowledge_base, remove_pending
from aha_cli.store.knowledge_capture import create_distill_log, read_note, update_distill_log, update_note
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
    lines = ["", "--- ATTACHED IMAGES (NOT visually analyzed; backend has no image input yet) ---"]
    for img in images:
        lines.append(
            f"- {img.get('original') or img.get('name')} "
            f"({img.get('mime')}, {int(img.get('size') or 0)} bytes, path: {img.get('path')})"
        )
    lines.append(
        "Do NOT invent image contents. You may reference an image by filename only "
        "if the user's text already explains it."
    )
    return "\n".join(lines)


def build_capture_prompt(note: dict) -> str:
    """Prompt instructing an agent to turn one raw note into KB candidates."""
    scope_hint = str(note.get("scope_hint") or "personal")
    text = str(note.get("text") or "")
    manifest = _image_manifest(note)
    return (
        "You are organizing a user's raw, messy note into reusable knowledge "
        "candidates for a knowledge base. Split it into 0..N independent, "
        "reusable items; drop chatter and one-off noise; deduplicate.\n\n"
        f"Default scope for these candidates is `{scope_hint}` unless an item is "
        "clearly cross-project (`general`) or tied to a specific project "
        "(`project`, only with a project_key). Personal/general items carry no "
        "project_key. Use `wiki` only for non-project tutorials/reference docs; "
        "project-specific structure, module responsibilities, entry points, key "
        "source files, reusable diagnostic paths, stale/missing nav links, or "
        "constraints belong in `navigation`. Read-only diagnostics count when "
        "they reveal where future agents should start.\n\n"
        "Reply with a short human summary, then exactly one machine-readable block:\n"
        "`<aha_knowledge_candidates>[...]</aha_knowledge_candidates>`.\n"
        "Each candidate: "
        '`{"kind":"solutions|wiki|navigation","scope":"...","title":"...","body":"...","tags":[],"related_files":[],"confidence":0.6}`.\n'
        "For `kind=solutions` body sections: `## 适用场景`, `## 问题 / 触发信号`, "
        "`## 推荐做法`, `## 关键位置`, `## 验证方式`, `## 失效条件 / 适用边界`.\n"
        "For `kind=wiki` body sections: `## 结论`, `## 适用范围`, `## 规则 / 约定`, "
        "`## 示例`, `## 相关位置`, `## 更新条件`.\n"
        "For `kind=navigation`, use slug `index` for the small project entry, "
        "`modules/<module-slug>` / `modules/<module>/<child-slug>` for module docs, "
        "or `flows/<flow-slug>` / `flows/<flow>/<child-slug>` for flow docs. Each nav doc owns one link layer only; "
        "AHA bootstraps a missing root index from the workspace when possible, and otherwise adds a minimal direct-parent link candidate when a child doc has no reachable parent entry.\n"
        "Prefer fields such as `responsibility`, `related_files`, `entry_points`, "
        "`diagnostic_paths`, and `navigation_reason` for project navigation deltas.\n"
        "If nothing is reusable, use `<aha_knowledge_candidates>[]</aha_knowledge_candidates>`.\n\n"
        "--- RAW NOTE ---\n"
        f"{text}\n"
        "--- END RAW NOTE ---\n"
        f"{manifest}\n"
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
    try:
        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "capture_reply.txt"
            if backend == "codex":
                from aha_cli.backends.codex import run_codex_exec

                codex_config = (config.get("codex") or {}) if isinstance(config, dict) else {}
                codex_bin = str(codex_config.get("bin") or "codex")
                _, reply, _ = run_codex_exec(
                    prompt, cwd=cwd, output_file=output_file,
                    codex_bin=codex_bin, model=model, sandbox="read-only",
                    approval="never", codex_config=codex_config, proxy_env=proxy_env,
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
                )
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
    })
    log_id = log["id"]
    agent_fn = agent or default_capture_agent
    try:
        reply = agent_fn({
            "prompt": prompt,
            "note": note,
            "backend": effective_backend,
            "model": effective_model,
            "proxy_enabled": effective_proxy_enabled,
            "config": config,
            "cwd": root,
        })
    except CaptureAgentError as exc:
        error = f"capture agent failed: {exc}"
        update_distill_log(root, config, note_id, log_id, status="error", error=error, finished_at=utc_now())
        return {"ok": False, "error": error, "log_id": log_id}

    _, raw_candidates, sidecar_error = split_knowledge_sidecar(reply or "")
    if raw_candidates is None:
        error = sidecar_error or "no knowledge candidates in agent reply"
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
    normalized, skipped_navigation = filter_project_nav_candidates(root, config, normalized, {"source_type": "capture_note"})
    normalized = ensure_navigation_parent_entries(root, config, normalized, {"source": source})

    init_knowledge_base(root, config)
    # Re-run replacement: drop candidates this note enqueued previously.
    for old_id in note.get("candidate_ids") or []:
        remove_pending(root, config, old_id)

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
