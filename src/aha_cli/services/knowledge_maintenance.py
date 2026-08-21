"""KB maintenance agent: resolve sync conflicts with user-priority semantics.

When a knowledge sync pull hits a rebase conflict (``diverged`` local and
remote history), AHA leaves the rebase in progress and dispatches a visible
maintenance Task rooted at the Knowledge Git workspace. The task Agent inspects
and resolves the repository directly, including subsequent rebase conflict
passes, then returns a concise result. AHA verifies the final Git state, pushes,
and persists status to ``<aha_home>/knowledge_sync_state.json``.
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
    pull,
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
    knowledge_agent_execution_context,
    public_knowledge_task,
    start_knowledge_task,
)

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


def clear_finished_maintenance(root: Path) -> bool:
    """Clear a completed maintenance banner after a later successful sync."""
    state = read_sync_state(root)
    maintenance = state.get("maintenance") if isinstance(state.get("maintenance"), dict) else {}
    if maintenance.get("status") not in {"resolved", "failed"}:
        return False
    state["maintenance"] = {"status": "idle"}
    write_sync_state(root, state)
    return True


# --------------------------------------------------------------------------- #
# Agent prompt + plan parsing
# --------------------------------------------------------------------------- #
def build_maintenance_prompt(root: Path, config: dict | None, detail: dict) -> str:
    sync = knowledge_config(config).get("sync")
    sync = sync if isinstance(sync, dict) else {}
    user_priority = bool(sync.get("user_priority", True))
    repo = detail.get("repo") or str(knowledge_root(root, config))
    conflict_paths = [
        str(item.get("path") or "")
        for item in (detail.get("conflicts") or [])
        if item.get("path")
    ]
    lines: list[str] = [
        f"Knowledge Git workspace: {repo}",
        "",
        "A rebase is already in progress and has conflicts.",
        "Resolve the conflicts directly in the current workspace.",
        "",
        "Required workflow:",
        "- Inspect `git status`, `git diff --cc`, and Git index stages as needed.",
        "- Preserve valuable content from both sides. Human-authored content has priority over generated content.",
        "- Edit conflicted files, run `git add`, then continue the rebase.",
        "- Continue through every subsequent conflict until no rebase is in progress.",
        "- Use `git -c maintenance.auto=false -c gc.auto=0 rebase --continue` to avoid auto-maintenance interference.",
        "- Do not push, force-push, reset --hard, delete unrelated changes, or abort the rebase.",
        "- Return a concise Markdown summary of files resolved and the final `git status`.",
        "",
        "Initially conflicted paths:",
        *(f"- {path}" for path in conflict_paths),
    ]
    if user_priority:
        lines.extend(["", "User-priority policy is enabled."])
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
    from aha_cli.services.knowledge_tasks import knowledge_agent_execution_context, resolve_knowledge_agent_config

    resolved_backend = str(
        backend
        or (task_context or {}).get("backend")
        or resolve_knowledge_agent_config(config)["backend"]
    )
    record = {
        "status": "running",
        "started_at": utc_now(),
        "finished_at": None,
        "conflict_files": unmerged_paths(knowledge_root(root, config)),
        "resolutions": {},
        "summary": "",
        "error": "",
        "pushed": False,
        "agent_backend": resolved_backend,
        "fallback_used": False,
    }
    management_task = public_knowledge_task(task_context, root=root)
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


def _run_direct_agent_resolution(
    root: Path,
    config: dict | None,
    record: dict,
    detail: dict,
    *,
    agent,
    task_context: dict,
    backend: str,
    model: str | None,
    reasoning_effort: str | None,
    proxy_enabled: bool,
    progress_callback,
    do_push: bool,
) -> dict:
    """Let the visible Knowledge Task Agent resolve the repository directly."""

    repo = knowledge_root(root, config)
    conflict_paths = [str(path) for path in (detail.get("unmerged") or [])]
    try:
        reply = agent(
            knowledge_agent_execution_context(
                root,
                task_context,
                {
                    "config": config,
                    "prompt": build_maintenance_prompt(root, config, detail),
                    "cwd": str(repo),
                    "backend": backend,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "proxy_enabled": proxy_enabled,
                    "sandbox": task_context.get("sandbox") or "danger-full-access",
                    "progress_callback": progress_callback,
                },
            )
        )
        record["diagnosis"] = str(reply or "").strip()
    except Exception as exc:  # noqa: BLE001 - preserve the repository and visible task
        record["status"] = "failed"
        record["summary"] = "KB Agent 未能完成冲突处理。"
        record["error"] = f"maintenance agent failed: {exc}"
        rebase_abort(root, config)
        return _finish(root, record)

    status = sync_status(root, config, check_remote=True)
    incomplete: list[str] = []
    if status.get("rebase_in_progress"):
        incomplete.append("rebase is still in progress")
    if status.get("unmerged"):
        incomplete.append("unmerged files remain: " + ", ".join(status.get("unmerged") or []))
    if status.get("dirty"):
        incomplete.append("working tree is dirty")
    if int(status.get("behind") or 0) > 0:
        incomplete.append(f"branch is still behind origin by {int(status.get('behind') or 0)} commit(s)")
    if not status.get("ok"):
        incomplete.append(str(status.get("remote_error") or "git status verification failed"))
    if incomplete:
        record["status"] = "failed"
        record["summary"] = "KB Agent 返回后仓库仍未完成冲突处理。"
        record["error"] = "; ".join(incomplete)
        rebase_abort(root, config)
        return _finish(root, record)

    record["fallback_used"] = False
    record["resolutions"] = {path: "agent" for path in conflict_paths}
    if do_push:
        pushed = push(root, config)
        record["pushed"] = bool(pushed.get("pushed"))
        if not pushed.get("ok"):
            record["status"] = "failed"
            record["summary"] = "冲突已由 KB Agent 解决，但推送到远端失败。"
            record["error"] = f"push failed: {pushed.get('error')}"
            return _finish(root, record)
    record["status"] = "resolved"
    record["summary"] = "KB Agent 已直接解决 Git 冲突并完成 rebase。"
    return _finish(root, record)


def run_kb_maintenance_job(
    root: Path,
    config: dict | None = None,
    *,
    backend: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    proxy_enabled: bool | None = None,
    agent=None,
    progress_callback=None,
    task_context: dict | None = None,
    do_push: bool = True,
) -> dict:
    """Resolve an in-progress KB sync conflict. Returns the maintenance record."""
    from aha_cli.services.knowledge_tasks import resolve_knowledge_agent_config

    resolved_agent = resolve_knowledge_agent_config(
        config,
        backend=backend,
        model=model,
        reasoning_effort=reasoning_effort,
        proxy_enabled=proxy_enabled,
    )
    backend = resolved_agent["backend"]
    model = resolved_agent["model"]
    reasoning_effort = resolved_agent["reasoning_effort"]
    proxy_enabled = resolved_agent["proxy_enabled"]
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

        if isinstance(task_context, dict):
            return _run_direct_agent_resolution(
                root,
                config,
                record,
                detail,
                agent=agent or default_maintenance_agent,
                task_context=task_context,
                backend=backend,
                model=model,
                reasoning_effort=reasoning_effort,
                proxy_enabled=proxy_enabled,
                progress_callback=progress_callback,
                do_push=do_push,
            )

        plan: list = []
        fallback_used = False
        if agent is None:
            agent = default_maintenance_agent
        try:
            prompt = build_maintenance_prompt(root, config, detail)
            reply = agent(
                knowledge_agent_execution_context(root, task_context, {
                    "config": config,
                    "prompt": prompt,
                    "cwd": str(repo),
                    "backend": backend,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "proxy_enabled": proxy_enabled,
                    "progress_callback": progress_callback,
                })
            )
            plan = parse_resolution_plan(reply)
        except Exception as exc:  # noqa: BLE001 - task records the backend failure
            record["error"] = f"maintenance agent failed: {exc}"
        decisions = default_resolutions(root, config, safe_only=True)
        agent_decisions = {
            path: decision
            for path, decision in normalize_decisions(plan).items()
            if path in set(detail.get("unmerged") or [])
            and not (decision.get("action") == "merge" and "content" not in decision)
        }
        if agent_decisions:
            decisions.update(agent_decisions)
        else:
            fallback_used = True
            if not record["error"]:
                record["error"] = "agent produced no usable conflict plan"
        unresolved = [path for path in (detail.get("unmerged") or []) if path not in decisions]
        if unresolved:
            record["status"] = "failed"
            record["fallback_used"] = fallback_used
            record["summary"] = "KB Agent 不可用或方案不完整，用户双端冲突已保留，等待用户处理。"
            record["error"] = (record["error"] + "; " if record["error"] else "") + (
                "unresolved user-owned conflicts: " + ", ".join(unresolved)
            )
            rebase_abort(root, config)
            return _finish(root, record)
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
    reasoning_effort: str | None = None,
    proxy_enabled: bool | None = None,
    agent=None,
    progress_callback=None,
    task_context: dict | None = None,
) -> dict:
    from aha_cli.services.knowledge_tasks import resolve_knowledge_agent_config

    resolved_agent = resolve_knowledge_agent_config(
        config,
        backend=backend,
        model=model,
        reasoning_effort=reasoning_effort,
        proxy_enabled=proxy_enabled,
    )
    backend = resolved_agent["backend"]
    model = resolved_agent["model"]
    reasoning_effort = resolved_agent["reasoning_effort"]
    proxy_enabled = resolved_agent["proxy_enabled"]
    repo = knowledge_root(root, config)
    if sync_result.get("conflict") and not unmerged_paths(repo) and not rebase_in_progress(repo):
        prepared_pull = pull(root, config, keep_rebase_on_conflict=True)
        if prepared_pull.get("ok"):
            record = _fresh_record(root, config, backend=backend, task_context=task_context)
            record["status"] = "resolved"
            record["summary"] = "同步重试后未再出现冲突。"
            record["retry"] = prepared_pull
            return _finish(root, record)
        sync_result = prepared_pull
    if sync_result.get("conflict") or unmerged_paths(repo):
        return run_kb_maintenance_job(
            root,
            config,
            backend=backend,
            model=model,
            reasoning_effort=reasoning_effort,
            proxy_enabled=proxy_enabled,
            agent=agent,
            progress_callback=progress_callback,
            task_context=task_context,
        )
    record = _fresh_record(root, config, backend=backend, task_context=task_context)
    record["conflict_files"] = []
    try:
        agent_fn = agent or default_maintenance_agent
        reply = agent_fn(
            knowledge_agent_execution_context(root, task_context, {
                "config": config,
                "prompt": build_sync_recovery_prompt(root, config, sync_result),
                "cwd": str(knowledge_root(root, config)),
                "backend": backend,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "proxy_enabled": proxy_enabled,
                "progress_callback": progress_callback,
            })
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
                reasoning_effort=reasoning_effort,
                proxy_enabled=proxy_enabled,
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
    task_context, is_conflict, state = _prepare_sync_maintenance_task(
        root,
        config,
        backend=backend,
        model=model,
        sync_result=sync_result,
        source=source,
    )
    resolved_backend = task_context.get("backend")
    resolved_model = task_context.get("model")
    resolved_reasoning_effort = task_context.get("reasoning_effort")
    resolved_proxy_enabled = task_context.get("proxy_enabled")

    def _run() -> None:
        _execute_sync_maintenance_task(
            root,
            config,
            task_context,
            is_conflict=is_conflict,
            sync_result=sync_result,
            backend=resolved_backend,
            model=resolved_model,
            reasoning_effort=resolved_reasoning_effort,
            proxy_enabled=resolved_proxy_enabled,
        )

    threading.Thread(target=_run, daemon=True).start()
    return {
        "maintenance": state["maintenance"],
        "management_task": public_knowledge_task(task_context, root=root),
    }


def _prepare_sync_maintenance_task(
    root: Path,
    config: dict | None,
    *,
    backend=None,
    model=None,
    sync_result: dict | None = None,
    source: str = "sync",
) -> tuple[dict, bool, dict]:
    is_conflict = bool((sync_result or {}).get("conflict") or unmerged_paths(knowledge_root(root, config)))
    operation = "sync_conflict" if is_conflict else "sync_failure"
    title = "知识库同步冲突处理" if is_conflict else "知识库同步故障处理"
    description = (
        "知识库同步发生 Git 冲突。KB Agent 将在 Knowledge workspace 中直接检查并解决全部 rebase 冲突，然后返回处理结果。"
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
        reasoning_effort=None,
        proxy_enabled=None,
        metadata={"source": source, "sync_result": sync_result or {}},
        reuse_operation=True,
    )
    task_context["operation"] = operation
    resolved_backend = task_context.get("backend")
    resolved_model = task_context.get("model")
    resolved_reasoning_effort = task_context.get("reasoning_effort")
    resolved_proxy_enabled = task_context.get("proxy_enabled")
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
        "agent_backend": resolved_backend,
        "fallback_used": False,
        "management_task": public_knowledge_task(task_context, root=root),
    }
    write_sync_state(root, state)
    return task_context, is_conflict, state


def _execute_sync_maintenance_task(
    root: Path,
    config: dict | None,
    task_context: dict,
    *,
    is_conflict: bool,
    sync_result: dict | None,
    backend,
    model,
    reasoning_effort,
    proxy_enabled,
) -> dict:
    try:
        start_knowledge_task(root, task_context)
        record = run_kb_sync_recovery_job(
            root,
            config,
            sync_result or {"ok": False, "conflict": is_conflict},
            backend=backend,
            model=model,
            reasoning_effort=reasoning_effort,
            proxy_enabled=proxy_enabled,
            progress_callback=None,
            task_context=task_context,
        )
        diagnosis = str(record.get("diagnosis") or "").strip()
        summary = str(record.get("summary") or record.get("error") or "同步维护结束。")
        error = str(record.get("error") or "").strip()
        final = "\n\n".join(part for part in (summary, error, diagnosis) if part).strip()
        finish_knowledge_task(root, task_context, final, ok=record.get("status") == "resolved")
        return record
    except Exception as exc:  # noqa: BLE001 - background jobs must always settle their visible task
        message = f"同步维护任务异常：{exc}"
        finish_knowledge_task(root, task_context, message, ok=False)
        record = maintenance_record(root)
        record.update({"status": "failed", "summary": message, "error": str(exc)})
        return _finish(root, record)


def run_sync_agent_task(
    root: Path,
    config: dict | None,
    sync_result: dict,
    *,
    source: str,
    backend=None,
    model=None,
) -> dict:
    """Run sync recovery synchronously while preserving a visible management task."""
    task_context, is_conflict, _state = _prepare_sync_maintenance_task(
        root,
        config,
        backend=backend,
        model=model,
        sync_result=sync_result,
        source=source,
    )
    record = _execute_sync_maintenance_task(
        root,
        config,
        task_context,
        is_conflict=is_conflict,
        sync_result=sync_result,
        backend=task_context.get("backend"),
        model=task_context.get("model"),
        reasoning_effort=task_context.get("reasoning_effort"),
        proxy_enabled=task_context.get("proxy_enabled"),
    )
    return {
        "maintenance": record,
        "management_task": public_knowledge_task(task_context, root=root),
    }


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
