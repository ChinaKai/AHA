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

import inspect
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
from aha_cli.services.knowledge_tasks import (
    create_knowledge_task,
    finish_knowledge_task,
    knowledge_task_progress_callback,
    public_knowledge_task,
    start_knowledge_task,
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
def _fresh_record(root: Path, config: dict | None, *, backend: str | None, task_context: dict | None = None) -> dict:
    record = {
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
    management_task = public_knowledge_task(task_context)
    if management_task:
        record["management_task"] = management_task
    return record


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
    task_context: dict | None = None,
    do_push: bool = True,
) -> dict:
    """Resolve an in-progress KB sync conflict. Returns the maintenance record."""
    repo = knowledge_root(root, config)
    record = _fresh_record(root, config, backend=backend, task_context=task_context)
    try:
        detail = conflict_detail(root, config)
        if not detail.get("unmerged"):
            if not rebase_in_progress(repo):
                record["status"] = "resolved"
                record["summary"] = "No conflicts to resolve; sync is clean."
            else:
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

        if do_push:
            pushed = push(root, config)
            record["pushed"] = bool(pushed.get("pushed"))
            if not pushed.get("ok"):
                record["status"] = "failed"
                record["summary"] = "冲突已解决，但推送到远端失败。"
                record["error"] = (record["error"] or "") + f" push failed: {pushed.get('error')}"
                return _finish(root, record)
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


def _sync_failure_errors(result: dict) -> list[str]:
    errors: list[str] = []
    for step in (result.get("steps") or {}).values():
        if not isinstance(step, dict):
            continue
        error = str(step.get("error") or "").strip()
        if error:
            errors.append(error)
    error = str(result.get("error") or "").strip()
    if error:
        errors.append(error)
    return errors


def should_dispatch_sync_agent(result: dict) -> bool:
    return bool(isinstance(result, dict) and not result.get("ok") and (result.get("conflict") or _sync_failure_errors(result)))


def build_sync_recovery_prompt(root: Path, config: dict | None, sync_result: dict) -> str:
    status = sync_status(root, config, check_remote=False)
    errors = _sync_failure_errors(sync_result)
    return "\n".join(
        [
            "You are diagnosing an AHA Knowledge Git synchronization failure.",
            "The repository must remain safe: do not recommend force-push, deleting local changes, or rewriting history.",
            "Analyze the external/environmental cause, identify safe automatic recovery steps, and clearly state any user action required.",
            "Reply in concise Markdown with sections: Root cause, Safe handling, User action.",
            "",
            f"Knowledge root: {knowledge_root(root, config)}",
            f"Git status: {json.dumps(status, ensure_ascii=False)}",
            f"Sync errors: {json.dumps(errors, ensure_ascii=False)}",
            f"Sync result: {json.dumps(sync_result, ensure_ascii=False)}",
        ]
    )


def run_kb_sync_recovery_job(
    root: Path,
    config: dict | None,
    sync_result: dict,
    *,
    backend: str | None = None,
    model: str | None = None,
    agent=None,
    progress_callback=None,
    task_context: dict | None = None,
) -> dict:
    if sync_result.get("conflict") or unmerged_paths(knowledge_root(root, config)):
        return run_kb_maintenance_job(
            root,
            config,
            backend=backend,
            model=model,
            agent=agent,
            progress_callback=progress_callback,
            task_context=task_context,
        )
    record = _fresh_record(root, config, backend=backend, task_context=task_context)
    record["conflict_files"] = []
    try:
        agent_fn = agent or default_maintenance_agent
        reply = agent_fn(
            {
                "config": config,
                "prompt": build_sync_recovery_prompt(root, config, sync_result),
                "cwd": str(knowledge_root(root, config)),
                "backend": backend,
                "model": model,
                "progress_callback": progress_callback,
            }
        )
        from aha_cli.services.knowledge_git import sync as knowledge_sync

        retry = knowledge_sync(
            root,
            config,
            message=f"chore(knowledge): agent-assisted sync retry {utc_now()}",
            do_pull=True,
            do_push=True,
        )
        record["diagnosis"] = str(reply or "").strip()
        record["retry"] = retry
        if retry.get("ok"):
            record["status"] = "resolved"
            record["summary"] = "同步故障经 Agent 分析后重试成功。"
        elif retry.get("conflict"):
            return run_kb_maintenance_job(
                root,
                config,
                backend=backend,
                model=model,
                agent=agent_fn,
                progress_callback=progress_callback,
                task_context=task_context,
            )
        else:
            record["status"] = "failed"
            record["summary"] = "Agent 已完成诊断，但同步仍需用户处理。"
            record["error"] = "; ".join(_sync_failure_errors(retry)) or "sync retry failed"
        return _finish(root, record)
    except Exception as exc:  # noqa: BLE001
        record["status"] = "failed"
        record["error"] = f"sync recovery agent failed: {exc}"
        record["summary"] = "同步故障处理失败。"
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
    sync_result: dict | None = None,
    source: str = "sync",
) -> dict:
    """Run a KB maintenance job in a background daemon thread (non-blocking).

    Writes the "running" maintenance record synchronously so a concurrent sync
    (scheduled or manual) sees an in-flight job and skips, avoiding a race where
    a second ``pull`` aborts the rebase mid-resolution.
    """
    is_conflict = bool((sync_result or {}).get("conflict") or unmerged_paths(knowledge_root(root, config)))
    operation = "sync_conflict" if is_conflict else "sync_failure"
    title = "知识库同步冲突处理" if is_conflict else "知识库同步故障处理"
    description = (
        "知识库同步发生 Git 冲突。KB Agent 将分析冲突，按用户优先原则生成方案，并由 AHA 确定性应用。"
        if is_conflict
        else "知识库同步因网络、认证、远端状态或本机 Git 环境失败。KB Agent 将诊断原因并执行一次安全重试。"
    )
    task_context = create_knowledge_task(
        root,
        config,
        operation=operation,
        title=title,
        description=description,
        backend=backend,
        model=model,
        metadata={"source": source, "sync_result": sync_result or {}},
    )
    task_context["operation"] = operation
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
        "management_task": public_knowledge_task(task_context),
    }
    write_sync_state(root, state)

    def _run() -> None:
        try:
            start_knowledge_task(root, task_context, "KB Agent 正在读取同步状态。")
            progress = knowledge_task_progress_callback(root, task_context)
            record = run_kb_sync_recovery_job(
                root,
                config,
                sync_result or {"ok": False, "conflict": is_conflict},
                backend=backend,
                model=model,
                progress_callback=progress,
                task_context=task_context,
            )
            diagnosis = str(record.get("diagnosis") or "").strip()
            summary = str(record.get("summary") or record.get("error") or "同步维护结束。")
            final = f"{summary}\n\n{diagnosis}".strip()
            finish_knowledge_task(root, task_context, final, ok=record.get("status") == "resolved")
        except Exception as exc:  # noqa: BLE001 - background job must not raise
            finish_knowledge_task(root, task_context, f"同步维护任务异常：{exc}", ok=False)

    threading.Thread(target=_run, daemon=True).start()
    return {"maintenance": state["maintenance"], "management_task": public_knowledge_task(task_context)}


dispatch_maintenance_job = _default_dispatch_maintenance_job


def dispatch_sync_agent(root: Path, config: dict | None, sync_result: dict, *, source: str) -> dict:
    """Dispatch through the public seam while preserving older two-argument test/plugin hooks."""
    parameters = inspect.signature(dispatch_maintenance_job).parameters.values()
    supports_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
    names = {parameter.name for parameter in parameters}
    if supports_kwargs or {"sync_result", "source"}.issubset(names):
        result = dispatch_maintenance_job(root, config, sync_result=sync_result, source=source)
    else:
        result = dispatch_maintenance_job(root, config)
    return result if isinstance(result, dict) else {}
