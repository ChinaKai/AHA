from __future__ import annotations

import threading
from pathlib import Path

from aha_cli.domain.models import (
    KNOWLEDGE_RUN_PURPOSE,
    KNOWLEDGE_TASK_KIND,
    SYSTEM_RUN_KIND,
    is_knowledge_run,
    utc_now,
)
from aha_cli.services.knowledge_agent_progress import summarize_agent_progress
from aha_cli.services.tasks import create_task_and_dispatch
from aha_cli.store.filesystem import append_message, create_plan, set_agent_status, set_task_status, write_task_result
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import knowledge_root
from aha_cli.store.paths import run_dir
from aha_cli.store.runs import list_run_summaries, locked_plan, require_plan, save_plan

KNOWLEDGE_RUN_TITLE = "KB 管理"

_run_lock = threading.RLock()


def _backend_defaults(config: dict | None, *, backend=None, model=None, reasoning_effort=None, proxy_enabled=None) -> dict:
    cfg = config if isinstance(config, dict) else {}
    selected_backend = str(backend or cfg.get("backend") or "codex").strip().lower()
    if selected_backend not in {"codex", "claude"}:
        selected_backend = "codex"
    backend_cfg = cfg.get(selected_backend) if isinstance(cfg.get(selected_backend), dict) else {}
    proxy_cfg = backend_cfg.get("proxy") if isinstance(backend_cfg.get("proxy"), dict) else {}
    return {
        "backend": selected_backend,
        "model": model or backend_cfg.get("model"),
        "reasoning_effort": reasoning_effort or backend_cfg.get("reasoning_effort"),
        "proxy_enabled": bool(backend_cfg.get("proxy_enabled", proxy_cfg.get("enabled", False)))
        if proxy_enabled is None
        else bool(proxy_enabled),
    }


def _mark_knowledge_run(root: Path, run_id: str, config: dict | None) -> dict:
    workspace = str(knowledge_root(root, config).resolve())
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
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
        main_agent.update({"workspace_path": workspace, "sandbox": "read-only", "approval": "never"})
        plan["main_agent"] = main_agent
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        return plan


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
    task = create_task_and_dispatch(
        root,
        run_id,
        title,
        backend=defaults["backend"],
        model=defaults["model"],
        reasoning_effort=defaults["reasoning_effort"],
        proxy_enabled=defaults["proxy_enabled"],
        workspace_path=str(knowledge_root(root, config).resolve()),
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
        "browser",
        description,
        sender="system",
        task_id=task_id,
        role="system",
        display_sender="AHA",
        display_target="KB Agent",
    )
    return {"run_id": run_id, "task_id": task_id, "task": task, **defaults}


def start_knowledge_task(root: Path, context: dict, message: str = "KB Agent 已开始处理。") -> None:
    run_id = str(context["run_id"])
    task_id = str(context["task_id"])
    set_task_status(root, run_id, task_id, "running")
    set_agent_status(root, run_id, task_id, "main", "running")
    append_knowledge_task_message(root, context, message)


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
    last_message = {"value": ""}

    def _progress(event_type: str, data: dict | None = None) -> None:
        if callable(existing):
            existing(event_type, data)
        summary = summarize_agent_progress(event_type, data)
        if not summary:
            return
        message = str(summary.get("message") or "").strip()
        if not message or message == last_message["value"]:
            return
        last_message["value"] = message
        append_knowledge_task_message(root, context, message)

    return _progress


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
