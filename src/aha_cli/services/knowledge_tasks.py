from __future__ import annotations

import threading
import time
from pathlib import Path

from aha_cli.domain.models import (
    KNOWLEDGE_RUN_PURPOSE,
    KNOWLEDGE_TASK_KIND,
    SYSTEM_RUN_KIND,
    is_knowledge_run,
    utc_now,
)
from aha_cli.services.tasks import create_task_and_dispatch
from aha_cli.store.filesystem import append_event, append_message, create_plan, set_agent_status, set_task_status, write_task_result
from aha_cli.store.io import read_json, write_json
from aha_cli.store.knowledge import knowledge_root
from aha_cli.store.paths import event_path, run_dir
from aha_cli.store.runs import list_run_summaries, locked_plan, require_plan, save_plan

KNOWLEDGE_RUN_TITLE = "AHA Knowledge Manager"

_run_lock = threading.RLock()


class KnowledgeAgentTurnError(RuntimeError):
    """Raised when a standard task-scoped Knowledge agent turn fails."""


def resolve_knowledge_agent_config(
    config: dict | None,
    *,
    backend=None,
    model=None,
    reasoning_effort=None,
    proxy_enabled=None,
) -> dict:
    cfg = config if isinstance(config, dict) else {}
    kb_cfg = cfg.get("knowledge") if isinstance(cfg.get("knowledge"), dict) else {}
    agent_cfg = kb_cfg.get("agent") if isinstance(kb_cfg.get("agent"), dict) else {}
    requested_backend = str(backend or "").strip().lower()
    saved_backend = str(agent_cfg.get("backend") or "").strip().lower()
    global_backend = str(cfg.get("backend") or "").strip().lower()
    selected_backend = requested_backend or saved_backend or global_backend or "codex"
    if selected_backend not in {"codex", "claude"}:
        selected_backend = "codex"
    backend_cfg = cfg.get(selected_backend) if isinstance(cfg.get(selected_backend), dict) else {}
    proxy_cfg = backend_cfg.get("proxy") if isinstance(backend_cfg.get("proxy"), dict) else {}
    profile_cfg = agent_cfg if not requested_backend else {}
    saved_proxy = profile_cfg.get("proxy_enabled")
    selected_proxy = (
        bool(proxy_enabled)
        if proxy_enabled is not None
        else bool(saved_proxy)
        if isinstance(saved_proxy, bool)
        else bool(backend_cfg.get("proxy_enabled", proxy_cfg.get("enabled", False)))
    )
    return {
        "backend": selected_backend,
        "model": model or profile_cfg.get("model") or backend_cfg.get("model"),
        "reasoning_effort": reasoning_effort or profile_cfg.get("reasoning_effort") or backend_cfg.get("reasoning_effort"),
        "proxy_enabled": selected_proxy,
    }


_backend_defaults = resolve_knowledge_agent_config


def _mark_knowledge_run(root: Path, run_id: str, config: dict | None) -> dict:
    workspace = str(knowledge_root(root, config).resolve())
    defaults = resolve_knowledge_agent_config(config)
    renamed = False
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        if str(plan.get("goal") or "") != KNOWLEDGE_RUN_TITLE:
            plan["goal"] = KNOWLEDGE_RUN_TITLE
            renamed = True
        plan.update(
            {
                "kind": SYSTEM_RUN_KIND,
                "system_managed": True,
                "system_owner": "aha",
                "system_purpose": KNOWLEDGE_RUN_PURPOSE,
                "system_schema_version": 1,
            }
        )
        main_agent = plan.get("main_agent") if isinstance(plan.get("main_agent"), dict) else {}
        main_agent.update(
            {
                "backend": defaults["backend"],
                "model": defaults["model"],
                "reasoning_effort": defaults["reasoning_effort"],
                "proxy_enabled": defaults["proxy_enabled"],
                "workspace_path": workspace,
                "sandbox": "read-only",
                "approval": "never",
            }
        )
        plan["main_agent"] = main_agent
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        marked = dict(plan)
    if renamed:
        append_event(root, run_id, "run_renamed", {"name": KNOWLEDGE_RUN_TITLE})
    return marked


def ensure_knowledge_run(
    root: Path,
    config: dict | None,
    *,
    backend=None,
    model=None,
    reasoning_effort=None,
    proxy_enabled=None,
) -> str:
    defaults = _backend_defaults(
        config,
        backend=backend,
        model=model,
        reasoning_effort=reasoning_effort,
        proxy_enabled=proxy_enabled,
    )
    with _run_lock:
        for summary in list_run_summaries(root):
            run_id = str(summary.get("id") or "")
            if not run_id:
                continue
            try:
                plan = require_plan(root, run_id)
            except SystemExit:
                continue
            if is_knowledge_run(plan):
                _mark_knowledge_run(root, run_id, config)
                return run_id
        plan = create_plan(
            root,
            KNOWLEDGE_RUN_TITLE,
            1,
            "implementation",
            [],
            [],
            backend=defaults["backend"],
            model=defaults["model"],
            reasoning_effort=defaults["reasoning_effort"],
            workspace_path=str(knowledge_root(root, config).resolve()),
            sandbox="read-only",
            approval="never",
            proxy_enabled=defaults["proxy_enabled"],
            collaboration_mode="solo",
            workflow_template="auto",
            create_default_tasks=False,
        )
        run_id = str(plan.get("id") or "")
        _mark_knowledge_run(root, run_id, config)
        return run_id


def create_knowledge_task(
    root: Path,
    config: dict | None,
    *,
    operation: str,
    title: str,
    description: str,
    backend=None,
    model=None,
    reasoning_effort=None,
    proxy_enabled=None,
    workspace_path: str | None = None,
    metadata: dict | None = None,
) -> dict:
    defaults = _backend_defaults(
        config,
        backend=backend,
        model=model,
        reasoning_effort=reasoning_effort,
        proxy_enabled=proxy_enabled,
    )
    run_id = ensure_knowledge_run(root, config, **defaults)
    task_workspace = str(workspace_path or knowledge_root(root, config).resolve())
    task = create_task_and_dispatch(
        root,
        run_id,
        title,
        backend=defaults["backend"],
        model=defaults["model"],
        reasoning_effort=defaults["reasoning_effort"],
        proxy_enabled=defaults["proxy_enabled"],
        workspace_path=task_workspace,
        sandbox="read-only",
        approval="never",
        collaboration_mode="solo",
        workflow_template="auto",
        delegation_policy="disabled",
        max_sub_agents=0,
        description=description,
        context_management={"auto_compact_enabled": True},
        token_saving={"enabled": False, "provider": "nav"},
        dispatch=False,
    )
    task_id = str(task.get("id") or "")
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        stored = next(item for item in plan.get("tasks", []) if str(item.get("id") or "") == task_id)
        stored.update(
            {
                "kind": KNOWLEDGE_TASK_KIND,
                "system_managed": True,
                "system_owner": "aha",
                "knowledge_operation": operation,
                "knowledge_metadata": dict(metadata or {}),
                "backend": defaults["backend"],
                "model": defaults["model"],
                "reasoning_effort": defaults["reasoning_effort"],
                "proxy_enabled": defaults["proxy_enabled"],
                "system_schema_version": 1,
            }
        )
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", stored)
        task = dict(stored)
    append_message(
        root,
        run_id,
        "main",
        description,
        sender="browser",
        task_id=task_id,
        role="main",
        from_agent="browser",
        to_agent="main",
        display_sender="browser",
        display_target="main",
    )
    return {"run_id": run_id, "task_id": task_id, "task": task, **defaults}


def start_knowledge_task(root: Path, context: dict) -> None:
    run_id = str(context["run_id"])
    task_id = str(context["task_id"])
    set_task_status(root, run_id, task_id, "running")
    set_agent_status(root, run_id, task_id, "main", "running")


def append_knowledge_task_message(root: Path, context: dict, message: str) -> None:
    text = str(message or "").strip()
    if not text:
        return
    append_message(
        root,
        str(context["run_id"]),
        "browser",
        text,
        sender="main",
        task_id=str(context["task_id"]),
        role="main",
        from_agent="main",
        to_agent="browser",
        display_sender="KB Agent",
    )


def knowledge_task_progress_callback(root: Path, context: dict, existing=None):
    def _progress(event_type: str, data: dict | None = None) -> None:
        if callable(existing):
            existing(event_type, data)

    return _progress


def knowledge_agent_execution_context(root: Path, task_context: dict | None, context: dict | None = None) -> dict:
    prepared = dict(context or {})
    if not isinstance(task_context, dict):
        return prepared
    run_id = str(task_context["run_id"])
    task_id = str(task_context["task_id"])
    prepared.update(
        {
            "events_file": event_path(root, run_id),
            "run_id": run_id,
            "task_id": task_id,
            "source": "main",
            "target": "main",
            "knowledge_root": root,
            "knowledge_task_context": task_context,
        }
    )
    prepared["progress_callback"] = knowledge_task_progress_callback(
        root,
        task_context,
        prepared.get("progress_callback"),
    )
    return prepared


def run_knowledge_agent_turn(root: Path, task_context: dict, context: dict) -> str:
    """Execute a Knowledge request through the normal task Chat backend.

    Knowledge jobs need the raw reply synchronously so they can parse their
    sidecar, but the model invocation itself must use the same inbox, prompt,
    session, runtime, log, usage, and command-event path as an ordinary Task.
    """
    from aha_cli.services.backend_runtime import backend_status, start_backend, stop_backend
    from aha_cli.services.chat_offsets import advance_chat_offset_to_inbox_end, chat_offset_path, chat_turn_checkpoint_path

    run_id = str(task_context.get("run_id") or "").strip()
    task_id = str(task_context.get("task_id") or "").strip()
    prompt = str(context.get("prompt") or "")
    if not run_id or not task_id:
        raise KnowledgeAgentTurnError("knowledge task context is incomplete")
    if not prompt.strip():
        raise KnowledgeAgentTurnError("knowledge agent prompt is empty")

    target = str(context.get("target") or "main").strip() or "main"
    backend = str(context.get("backend") or task_context.get("backend") or "codex").strip().lower()
    model = context.get("model") or task_context.get("model")
    reasoning_effort = context.get("reasoning_effort") or task_context.get("reasoning_effort")
    task_dir = run_dir(root, run_id)
    offset_file = chat_offset_path(task_dir, target, task_id)
    if not offset_file.exists():
        advance_chat_offset_to_inbox_end(root, run_id, target, task_id)

    append_message(
        root,
        run_id,
        target,
        prompt,
        sender="browser",
        task_id=task_id,
        role="main",
        from_agent="browser",
        to_agent=target,
        reply_target="browser",
        display_sender="Knowledge",
        display_target=target,
    )
    checkpoint_path = chat_turn_checkpoint_path(task_dir, target, task_id)
    try:
        start_backend(
            root,
            run_id,
            target,
            backend=backend,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox="read-only",
            approval="never",
            from_start=False,
            task_id=task_id,
        )
    except Exception as exc:  # noqa: BLE001 - convert runtime failures to the Knowledge seam error
        raise KnowledgeAgentTurnError(f"failed to start {backend} task backend: {exc}") from exc

    timeout_seconds = max(1.0, float(context.get("timeout_seconds") or 3600.0))
    deadline = time.monotonic() + timeout_seconds
    last_status: dict = {}
    while time.monotonic() < deadline:
        if checkpoint_path.exists():
            try:
                checkpoint = read_json(checkpoint_path)
            except (OSError, ValueError):
                checkpoint = {}
            if checkpoint.get("phase") == "finished":
                reply = str(checkpoint.get("reply") or "")
                exit_code = int(checkpoint.get("exit_code") or 0)
                if exit_code != 0 or not reply.strip():
                    raise KnowledgeAgentTurnError(
                        f"{backend} task backend failed with exit code {exit_code}: {reply.strip() or 'empty reply'}"
                    )
                return reply
        last_status = backend_status(root, run_id, target, task_id)
        if last_status.get("status") == "stopped" and checkpoint_path.exists():
            try:
                checkpoint = read_json(checkpoint_path)
            except (OSError, ValueError):
                checkpoint = {}
            if checkpoint.get("phase") == "finished":
                reply = str(checkpoint.get("reply") or "")
                exit_code = int(checkpoint.get("exit_code") or 0)
                if exit_code == 0 and reply.strip():
                    return reply
            raise KnowledgeAgentTurnError(f"{backend} task backend stopped before producing a reply")
        time.sleep(0.1)

    try:
        stop_backend(root, run_id, target, task_id=task_id, timeout=2.0)
    except Exception:  # noqa: BLE001 - timeout is already the primary failure
        pass
    raise KnowledgeAgentTurnError(
        f"timed out after {timeout_seconds:g}s waiting for {backend} task reply"
        + (f" ({last_status.get('status')})" if last_status.get("status") else "")
    )


def finish_knowledge_task(root: Path, context: dict, result: str, *, ok: bool) -> None:
    run_id = str(context["run_id"])
    task_id = str(context["task_id"])
    text = str(result or ("处理完成。" if ok else "处理失败。"))
    append_knowledge_task_message(root, context, text)
    write_task_result(
        root,
        run_id,
        task_id,
        text,
        final_context={"skip_knowledge_distill": True, "knowledge_operation": context.get("operation")},
    )
    status = "completed" if ok else "failed"
    exit_code = 0 if ok else 1
    set_agent_status(root, run_id, task_id, "main", status, exit_code)
    set_task_status(root, run_id, task_id, status, exit_code)


def public_knowledge_task(context: dict | None) -> dict | None:
    if not isinstance(context, dict):
        return None
    return {
        "run_id": str(context.get("run_id") or ""),
        "task_id": str(context.get("task_id") or ""),
        "title": str((context.get("task") or {}).get("title") or ""),
    }
