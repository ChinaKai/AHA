from __future__ import annotations

import hashlib
from pathlib import Path
import threading

from aha_cli.domain.models import (
    SERVICE_ASSISTANT_PURPOSE,
    SERVICE_ASSISTANT_TASK_KIND,
    SYSTEM_RUN_KIND,
    is_service_assistant_run,
    is_service_assistant_task,
    utc_now,
)
from aha_cli.services.tasks import create_task_and_dispatch
from aha_cli.store.config import load_config
from aha_cli.store.filesystem import create_plan
from aha_cli.store.io import write_json
from aha_cli.store.paths import aha_home_path, run_dir
from aha_cli.store.runs import list_run_summaries, locked_plan, require_plan, save_plan

SERVICE_ASSISTANT_RUN_TITLE = "AHA Service Assistant"
LEGACY_ASSISTANT_RUN_TITLE = "Feishu Assistant"
SERVICE_ASSISTANT_TASK_TITLE = "AHA Assistant"

_run_lock = threading.RLock()


def session_task_title(session_key: str) -> str:
    kind = "Group" if ":group:" in str(session_key or "").lower() else "DM"
    short_id = hashlib.sha256(str(session_key or "").encode("utf-8")).hexdigest()[:6]
    return f"{SERVICE_ASSISTANT_TASK_TITLE} · {kind} · {short_id}"


def _mark_system_run(root: Path, run_id: str, *, legacy: bool) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        plan["kind"] = SYSTEM_RUN_KIND
        plan["system_managed"] = True
        plan["system_owner"] = "aha"
        plan["system_purpose"] = SERVICE_ASSISTANT_PURPOSE
        plan["system_schema_version"] = 1
        main_agent = plan.get("main_agent") if isinstance(plan.get("main_agent"), dict) else {}
        main_agent.update(
            {
                "workspace_path": str(aha_home_path(root).resolve()),
                "sandbox": "read-only",
                "approval": "never",
            }
        )
        plan["main_agent"] = main_agent
        if legacy:
            for task in plan.get("tasks", []):
                if not isinstance(task, dict) or is_service_assistant_task(task):
                    continue
                task["assistant_legacy"] = True
                task["hidden"] = True
                task["hidden_at"] = task.get("hidden_at") or utc_now()
                task_path = run_dir(root, run_id) / "tasks" / str(task.get("id") or "") / "task.json"
                if task_path.parent.is_dir():
                    write_json(task_path, task)
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        return plan


def ensure_service_assistant_run(root: Path, defaults: dict[str, object]) -> str:
    with _run_lock:
        legacy_id = ""
        for summary in list_run_summaries(root):
            run_id = str(summary.get("id") or "")
            if not run_id:
                continue
            try:
                plan = require_plan(root, run_id)
            except SystemExit:
                continue
            if is_service_assistant_run(plan):
                _mark_system_run(root, run_id, legacy=False)
                return run_id
            if str(summary.get("goal") or "") == LEGACY_ASSISTANT_RUN_TITLE:
                legacy_id = run_id
        if legacy_id:
            _mark_system_run(root, legacy_id, legacy=True)
            return legacy_id

        config = load_config(root)
        backend = str(defaults.get("backend") or config.get("backend") or "codex")
        plan = create_plan(
            root,
            SERVICE_ASSISTANT_RUN_TITLE,
            1,
            "implementation",
            [],
            [],
            backend=backend,
            model=defaults.get("model"),
            reasoning_effort=defaults.get("reasoning_effort"),
            workspace_path=str(aha_home_path(root).resolve()),
            sandbox="read-only",
            approval="never",
            proxy_enabled=bool(defaults.get("proxy_enabled")),
            collaboration_mode="solo",
            workflow_template="auto",
            create_default_tasks=False,
        )
        run_id = str(plan.get("id") or "")
        _mark_system_run(root, run_id, legacy=False)
        return run_id


def ensure_service_assistant_task(
    root: Path,
    run_id: str,
    session_key: str,
    defaults: dict[str, object],
) -> dict:
    base_title = session_task_title(session_key)
    plan = require_plan(root, run_id)
    existing = [
        task
        for task in plan.get("tasks", [])
        if isinstance(task, dict)
        and is_service_assistant_task(task)
        and str(task.get("session_key_hash") or "") == hashlib.sha256(session_key.encode("utf-8")).hexdigest()
        and not task.get("deleted_at")
        and str(task.get("status") or "") not in {"completed", "failed", "blocked"}
    ]
    if existing:
        return existing[-1]
    titles = {str(task.get("title") or "") for task in plan.get("tasks", []) if isinstance(task, dict)}
    title = base_title
    generation = 2
    while title in titles:
        title = f"{base_title} #{generation}"
        generation += 1
    task = create_task_and_dispatch(
        root,
        run_id,
        title,
        backend=str(defaults.get("backend") or "codex"),
        model=defaults.get("model"),
        reasoning_effort=defaults.get("reasoning_effort"),
        proxy_enabled=bool(defaults.get("proxy_enabled")),
        workspace_path=str(aha_home_path(root).resolve()),
        sandbox="read-only",
        approval="never",
        collaboration_mode="solo",
        workflow_template="auto",
        delegation_policy="disabled",
        max_sub_agents=0,
        context_management={"auto_compact_enabled": True},
        token_saving={"enabled": False, "provider": "nav"},
        dispatch=False,
    )
    session_hash = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        stored = next(item for item in plan.get("tasks", []) if str(item.get("id") or "") == str(task.get("id") or ""))
        stored.update(
            {
                "kind": SERVICE_ASSISTANT_TASK_KIND,
                "system_managed": True,
                "system_owner": "aha",
                "channel": "feishu",
                "session_key_hash": session_hash,
                "system_schema_version": 1,
            }
        )
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / str(stored["id"]) / "task.json", stored)
        task = dict(stored)
    return task


__all__ = [
    "LEGACY_ASSISTANT_RUN_TITLE",
    "SERVICE_ASSISTANT_RUN_TITLE",
    "SERVICE_ASSISTANT_TASK_TITLE",
    "ensure_service_assistant_run",
    "ensure_service_assistant_task",
    "session_task_title",
]
