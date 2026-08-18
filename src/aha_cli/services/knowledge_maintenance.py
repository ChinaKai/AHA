"""KB maintenance agent: resolve sync conflicts with user-priority semantics.

When a knowledge sync pull hits a rebase conflict (``diverged`` local and
remote history) and ``knowledge.sync.resolve_conflicts == "agent"``, the rebase
is left in progress and a maintenance job is dispatched. This module:

- reads the per-file base/ours/theirs conflict detail,
- asks a real backend agent (claude/codex) for a resolution plan,
- applies the plan deterministically (ours/theirs/merge/archive),
- finishes the rebase and pushes, honoring ``user_priority`` when the agent
  produced no usable plan.

The resolution is applied by :mod:`aha_cli.services.knowledge_git`, not by the
agent itself: the agent runs read-only and returns a JSON plan, so a partial or
hostile backend reply can never corrupt the repo. State is persisted to
``<aha_home>/knowledge_sync_state.json`` so the Web UI and CLI can surface it.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.store.io import read_json, write_json
from aha_cli.store.knowledge import knowledge_config, knowledge_root
from aha_cli.store.paths import aha_home_path
from aha_cli.services.knowledge_git import (
    conflict_detail,
    default_resolutions,
    push,
    rebase_abort,
    rebase_continue,
    rebase_in_progress,
    resolve_unmerged,
    sync_status,
    unmerged_paths,
)

# Cap each conflict side in the agent prompt so a large entry cannot blow the
# context window; longer sides are truncated with a note.
_PROMPT_SIDE_LIMIT = 6000
_MAX_RESOLVE_PASSES = 12
_ARCHIVE_DIR = "conflicts"


def knowledge_sync_state_path(root: Path) -> Path:
    return aha_home_path(root) / "knowledge_sync_state.json"


def read_sync_state(root: Path) -> dict:
    path = knowledge_sync_state_path(root)
    state = read_json(path) if path.exists() else {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("schema", 1)
    state.setdefault("maintenance", {"status": "idle"})
    state.setdefault("loop", {})
    return state


def write_sync_state(root: Path, state: dict) -> dict:
    path = knowledge_sync_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, state)
    return state


def maintenance_record(root: Path) -> dict:
    return read_sync_state(root).get("maintenance") or {"status": "idle"}


# --------------------------------------------------------------------------- #
# Agent prompt + plan parsing
# --------------------------------------------------------------------------- #
def build_maintenance_prompt(root: Path, config: dict | None, detail: dict) -> str:
    sync = knowledge_config(config).get("sync")
    sync = sync if isinstance(sync, dict) else {}
    user_priority = bool(sync.get("user_priority", True))
    repo = detail.get("repo") or str(knowledge_root(root, config))
    lines: list[str] = [
        "You are maintaining the AHA knowledge base git repository at:",
        repo,
        "",
        "A sync between the local branch and the remote has hit merge conflicts.",
        "Your job is to produce a resolution plan for each conflicted file.",
        "",
        "RULE — user priority: a human user is the knowledge base owner. When a",
        "conflict is between a user-edited version and an agent-distilled version",
        "(frontmatter markers like `distilled_by`, `created_by`, or `source` set to",
        "agent/aha/auto), keep the USER version and discard the agent version",
        "unless the user version is clearly a broken partial edit. When both sides",
        "are agent-authored or both are user-authored, merge the valuable content",
        "from both sides. When you cannot decide, keep the local (ours) version.",
        "",
        "For each conflicted file, output ONE JSON object with fields:",
        '  {"path": "<repo-relative path>", "action": "local" | "remote" | "merge" | "archive", "content": "<full new content, only for merge/archive>"}',
        "",
        '- "local": keep this device\'s version',
        '- "remote": keep the remote version',
        '- "merge": write the merged `content` to the file',
        '- "archive": keep the remote version and save the local version for human review',
        "",
        "Reply with a single JSON array of these objects and nothing else (no prose).",
        "",
    ]
    conflicts = detail.get("conflicts") or []
    if not conflicts:
        lines.append("No conflicted files are currently unmerged.")
        return "\n".join(lines)
    lines.append(f"There are {len(conflicts)} conflicted file(s). For each, the base/local/remote versions follow:")
    lines.append("")
    for i, conflict in enumerate(conflicts, 1):
        path = conflict.get("path", "")
        lines.append(f"--- Conflict {i}: {path} ---")
        lines.append(f"agent-authored flags: local={bool(conflict.get('local_agent'))} remote={bool(conflict.get('remote_agent'))}")
        for label in ("base", "local", "remote"):
            content = str(conflict.get(label) or "")
            if len(content) > _PROMPT_SIDE_LIMIT:
                content = content[:_PROMPT_SIDE_LIMIT] + "\n...[truncated]"
            lines.append(f"[{label} ({len(str(conflict.get(label) or ''))} chars)]")
            lines.append(content)
            lines.append("")
    return "\n".join(lines)


def parse_resolution_plan(reply: str) -> list:
    """Extract the first balanced JSON array (or object) from an agent reply."""
    text = str(reply or "")
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = text.find(open_ch)
        while start != -1:
            depth = 0
            end = -1
            for i in range(start, len(text)):
                if text[i] == open_ch:
                    depth += 1
                elif text[i] == close_ch:
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end != -1:
                candidate = text[start : end + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(parsed, list):
                        return parsed
                    if isinstance(parsed, dict):
                        return [parsed]
            start = text.find(open_ch, start + 1)
    return []


def normalize_decisions(plan: list) -> dict:
    """Validate an agent plan into {path: {"action": ..., "content": ...}}."""
    decisions: dict[str, dict] = {}
    for item in plan:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        action = str(item.get("action") or "merge").strip().lower()
        if action in {"ours", "keep_local"}:
            action = "local"
        elif action in {"theirs", "keep_remote"}:
            action = "remote"
        elif action not in {"local", "remote", "merge", "archive"}:
            action = "merge"
        decision: dict = {"action": action}
        if item.get("content") is not None:
            decision["content"] = str(item["content"])
        decisions[path] = decision
    return decisions


def _apply_archives(root: Path, config: dict | None, decisions: dict) -> None:
    """Preserve the local version of archived paths outside the repo, then take remote."""
    detail = conflict_detail(root, config)
    by_path = {c["path"]: c for c in detail.get("conflicts", [])}
    conflicts_dir = root / _ARCHIVE_DIR
    for path, decision in decisions.items():
        if decision.get("action") != "archive":
            continue
        local = (by_path.get(path) or {}).get("local", "")
        target = conflicts_dir / f"{path}.local.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(local, encoding="utf-8")
        decision["action"] = "remote"


# --------------------------------------------------------------------------- #
# Agent seam
# --------------------------------------------------------------------------- #
def default_maintenance_agent(context: dict) -> str:
    """Run the conflict analysis through the existing backend exec chain.

    Reuses the capture-distill agent (read-only backend exec) with a custom
    prompt. Returns the raw reply text; the caller parses the JSON plan.
    """
    from aha_cli.services.knowledge_capture_distill import default_capture_agent

    return default_capture_agent(context)


# --------------------------------------------------------------------------- #
# Job
# --------------------------------------------------------------------------- #
def _fresh_record(root: Path, config: dict | None, *, backend: str | None) -> dict:
    return {
        "status": "running",
        "started_at": utc_now(),
        "finished_at": None,
        "conflict_files": unmerged_paths(knowledge_root(root, config)),
        "resolutions": {},
        "summary": "",
        "error": "",
        "pushed": False,
        "agent_backend": backend or "claude",
        "fallback_used": False,
    }


def _finish(root: Path, record: dict) -> dict:
    record["finished_at"] = utc_now()
    state = read_sync_state(root)
    state["maintenance"] = record
    write_sync_state(root, state)
    return record


def _resolution_summary(detail: dict, resolutions: dict, fallback_used: bool) -> str:
    if fallback_used:
        prefix = "Resolved by user-priority defaults (agent plan unavailable): "
    else:
        prefix = "Resolved by maintenance agent: "
    parts = [f"{path} ({action})" for path, action in (resolutions or {}).items()]
    if not parts:
        parts = [f"{path} (merge)" for path in (detail.get("unmerged") or [])]
    return prefix + ", ".join(parts) + "."


def run_kb_maintenance_job(
    root: Path,
    config: dict | None = None,
    *,
    backend: str | None = None,
    model: str | None = None,
    agent=None,
    progress_callback=None,
) -> dict:
    """Resolve an in-progress KB sync conflict. Returns the maintenance record."""
    repo = knowledge_root(root, config)
    record = _fresh_record(root, config, backend=backend)
    try:
        detail = conflict_detail(root, config)
        if not detail.get("unmerged"):
            cont = rebase_continue(root, config)
            if cont.get("ok"):
                record["status"] = "resolved"
                record["summary"] = "No conflicts to resolve; sync is clean."
            else:
                record["status"] = "failed"
                record["error"] = cont.get("error") or "rebase continue failed"
            return _finish(root, record)

        plan: list = []
        fallback_used = False
        if agent is None:
            agent = default_maintenance_agent
        try:
            prompt = build_maintenance_prompt(root, config, detail)
            reply = agent(
                {
                    "config": config,
                    "prompt": prompt,
                    "cwd": str(repo),
                    "backend": backend,
                    "model": model,
                    "progress_callback": progress_callback,
                }
            )
            plan = parse_resolution_plan(reply)
        except Exception as exc:  # noqa: BLE001 - agent failure falls back to defaults
            record["error"] = f"maintenance agent failed: {exc}"
        # Layer agent decisions over the deterministic user-priority baseline so
        # every unmerged path has a resolution even if the agent skipped some.
        decisions = default_resolutions(root, config)
        agent_decisions = normalize_decisions(plan)
        if agent_decisions:
            decisions.update(agent_decisions)
        else:
            fallback_used = True
            if not record["error"]:
                record["error"] = "agent produced no usable plan; used user-priority defaults"
        record["fallback_used"] = fallback_used
        record["resolutions"] = {p: d["action"] for p, d in decisions.items()}
        _apply_archives(root, config, decisions)

        # Resolve + continue the rebase, pass by pass, until it is finished.
        for _ in range(_MAX_RESOLVE_PASSES):
            if not unmerged_paths(repo) and not rebase_in_progress(repo):
                break
            if unmerged_paths(repo):
                result = resolve_unmerged(root, config, decisions=decisions)
                if not result.get("ok"):
                    record["status"] = "failed"
                    record["error"] = record["error"] or result.get("error") or "resolve failed"
                    rebase_abort(root, config)
                    return _finish(root, record)
            cont = rebase_continue(root, config)
            if not cont.get("ok"):
                record["status"] = "failed"
                record["error"] = record["error"] or cont.get("error") or "rebase continue failed"
                rebase_abort(root, config)
                return _finish(root, record)

        status = sync_status(root, config)
        if status.get("unmerged") or status.get("rebase_in_progress"):
            record["status"] = "failed"
            record["error"] = record["error"] or "conflict remains after resolution; aborted"
            rebase_abort(root, config)
            return _finish(root, record)

        git_cfg = knowledge_config(config).get("git")
        auto_push = bool(git_cfg.get("auto_push")) if isinstance(git_cfg, dict) else False
        if auto_push:
            pushed = push(root, config)
            record["pushed"] = bool(pushed.get("pushed"))
            if not pushed.get("ok"):
                record["error"] = (record["error"] or "") + f" push failed: {pushed.get('error')}"
        record["status"] = "resolved"
        record["summary"] = _resolution_summary(detail, record["resolutions"], fallback_used)
        return _finish(root, record)
    except Exception as exc:  # noqa: BLE001 - never raise out of a background job
        try:
            rebase_abort(root, config)
        except Exception:  # noqa: BLE001
            pass
        record["status"] = "failed"
        record["error"] = f"maintenance failed: {exc}"
        return _finish(root, record)


# --------------------------------------------------------------------------- #
# Dispatch seam (mirrors the distill job dispatch so tests can run synchronously)
# --------------------------------------------------------------------------- #
def _default_dispatch_maintenance_job(
    root: Path,
    config: dict | None,
    *,
    backend=None,
    model=None,
) -> None:
    """Run a KB maintenance job in a background daemon thread (non-blocking).

    Writes the "running" maintenance record synchronously so a concurrent sync
    (scheduled or manual) sees an in-flight job and skips, avoiding a race where
    a second ``pull`` aborts the rebase mid-resolution.
    """
    state = read_sync_state(root)
    state["maintenance"] = {
        "status": "running",
        "started_at": utc_now(),
        "finished_at": None,
        "conflict_files": unmerged_paths(knowledge_root(root, config)),
        "resolutions": {},
        "summary": "",
        "error": "",
        "pushed": False,
        "agent_backend": backend or "claude",
        "fallback_used": False,
    }
    write_sync_state(root, state)

    def _run() -> None:
        try:
            run_kb_maintenance_job(root, config, backend=backend, model=model)
        except Exception:  # noqa: BLE001 - background job must not raise
            pass

    threading.Thread(target=_run, daemon=True).start()


dispatch_maintenance_job = _default_dispatch_maintenance_job
