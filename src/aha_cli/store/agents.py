from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from aha_cli.domain.models import make_agent, next_sub_id, normalize_task_supervision, utc_now
from aha_cli.store.events import append_event as default_append_event
from aha_cli.store.io import write_json
from aha_cli.store.paths import run_dir
from aha_cli.store.runs import locked_plan, require_plan, save_plan
from aha_cli.store.sessions import ensure_session as default_ensure_session

UNSET = object()


def _find_task(plan: dict, task_id: str, *, allow_deleted: bool = False) -> dict:
    task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
    if task is None or (task.get("deleted_at") and not allow_deleted):
        raise SystemExit(f"Task not found: {task_id}")
    return task


def _find_agent(task: dict, agent_id: str) -> dict:
    agent = next((item for item in task.get("agents", []) if item.get("id") == agent_id), None)
    if agent is None:
        raise SystemExit(f"Agent not found: {agent_id}")
    return agent


def _write_task(root: Path, run_id: str, task: dict) -> None:
    write_json(run_dir(root, run_id) / "tasks" / task["id"] / "task.json", task)


def _model_value(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def add_agent_to_task_dict(
    task: dict,
    backend: str = "codex",
    role: str = "sub",
    model: str | None = None,
    workspace_path: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    proxy_enabled: bool | None = None,
    created_by: str = "system",
    created_reason: str = "",
) -> dict:
    normalized_role = str(role or "sub").strip().lower()
    if normalized_role in {"main", "task-main"}:
        agent_id = "main"
        agent_role = "task-main"
    elif normalized_role in {"host", "supervision-host"}:
        existing = next(
            (agent for agent in task.get("agents", []) if agent.get("id") == "host" or agent.get("role") == "host"),
            None,
        )
        if existing:
            return existing
        agent_id = "host"
        agent_role = "host"
    else:
        agent_id = next_sub_id(task)
        agent_role = "sub"
    default_sandbox = "read-only" if agent_role == "host" else task.get("preferred_sandbox")
    default_approval = "never" if agent_role == "host" else task.get("preferred_approval")
    default_proxy_enabled = False if agent_role == "host" else bool(task.get("preferred_proxy_enabled"))
    agent = make_agent(
        agent_id,
        agent_role,
        backend,
        model=model,
        workspace_path=workspace_path or task.get("workspace_path"),
        sandbox=sandbox if sandbox is not None else default_sandbox,
        approval=approval if approval is not None else default_approval,
        proxy_enabled=default_proxy_enabled if proxy_enabled is None else bool(proxy_enabled),
        created_by=created_by,
        created_reason=created_reason,
    )
    task.setdefault("agents", []).append(agent)
    return agent


def ensure_task_supervision_host_agent(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    backend: str | None = None,
    model: object = UNSET,
    proxy_enabled: object = UNSET,
    now_func: Callable[[], str] = utc_now,
    append_event_func: Callable[[Path, str, str, dict], dict] = default_append_event,
    ensure_session_func: Callable[..., dict] = default_ensure_session,
) -> dict:
    created = False
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = _find_task(plan, task_id)
        raw_supervision = task.get("supervision") if isinstance(task.get("supervision"), dict) else {}
        supervision = normalize_task_supervision(raw_supervision)
        host_backend = str(backend or supervision.get("host_backend") or task.get("preferred_backend") or "codex")
        if host_backend == "stub":
            host_backend = str(task.get("preferred_backend") or "codex")
        model_provided = model is not UNSET
        model_configured = "host_model" in raw_supervision or "model" in raw_supervision
        host_model = _model_value(model) if model_provided else _model_value(supervision.get("host_model"))
        proxy_provided = proxy_enabled is not UNSET
        proxy_configured = "host_proxy_enabled" in raw_supervision or "proxy_enabled" in raw_supervision
        host_proxy_enabled = bool(proxy_enabled) if proxy_provided else bool(supervision.get("host_proxy_enabled"))
        host_agent_id = str(supervision.get("host_agent_id") or "host")
        agent = next(
            (
                item
                for item in task.get("agents", [])
                if item.get("id") == host_agent_id or item.get("id") == "host" or item.get("role") == "host"
            ),
            None,
        )
        if agent is None:
            agent = make_agent(
                "host",
                "host",
                host_backend,
                status="stopped",
                model=host_model,
                workspace_path=task.get("workspace_path"),
                sandbox="read-only",
                approval="never",
                proxy_enabled=host_proxy_enabled,
                created_by="supervision",
                created_reason="task supervision host agent",
            )
            task.setdefault("agents", []).append(agent)
            created = True
        else:
            agent["role"] = "host"
            agent["backend"] = host_backend
            if model_provided or model_configured:
                agent["model"] = host_model
            if proxy_provided or proxy_configured:
                agent["proxy_enabled"] = host_proxy_enabled
            agent["sandbox"] = agent.get("sandbox") or "read-only"
            agent["approval"] = agent.get("approval") or "never"
            agent.setdefault("created_by", "supervision")
            agent.setdefault("created_reason", "task supervision host agent")
        supervision.update(
            {
                "mode": "assisted",
                "host_backend": agent.get("backend") or host_backend,
                "host_model": agent.get("model"),
                "host_proxy_enabled": bool(agent.get("proxy_enabled")),
                "host_agent_id": agent.get("id") or "host",
                "real_agent_enabled": True,
            }
        )
        task["supervision"] = normalize_task_supervision(supervision)
        plan["updated_at"] = now_func()
        save_plan(root, plan)
        _write_task(root, run_id, task)
    ensure_session_func(
        root,
        run_id,
        task_id,
        agent["id"],
        agent.get("backend", host_backend),
        model=agent.get("model"),
        workspace_path=agent.get("workspace_path") or task.get("workspace_path"),
    )
    if created:
        append_event_func(
            root,
            run_id,
            "agent_created",
            {
                "task_id": task_id,
                "agent_id": agent["id"],
                "role": agent.get("role"),
                "backend": agent.get("backend"),
                "model": agent.get("model"),
                "sandbox": agent.get("sandbox"),
                "approval": agent.get("approval"),
                "proxy_enabled": agent.get("proxy_enabled"),
                "created_by": agent.get("created_by"),
                "created_reason": agent.get("created_reason"),
            },
        )
    append_event_func(
        root,
        run_id,
        "task_supervision_host_ready",
        {
            "task_id": task_id,
            "host_agent_id": agent.get("id"),
            "host_backend": agent.get("backend"),
            "host_model": agent.get("model"),
            "host_proxy_enabled": agent.get("proxy_enabled"),
            "created": created,
        },
    )
    return {"task": task, "agent": agent, "created": created}


def add_agent(
    root: Path,
    run_id: str,
    task_id: str,
    backend: str = "codex",
    role: str = "sub",
    model: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    proxy_enabled: bool | None = None,
    created_by: str = "system",
    created_reason: str = "",
    *,
    now_func: Callable[[], str] = utc_now,
    append_event_func: Callable[[Path, str, str, dict], dict] = default_append_event,
    ensure_session_func: Callable[..., dict] = default_ensure_session,
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = _find_task(plan, task_id, allow_deleted=True)
        agent = add_agent_to_task_dict(
            task,
            backend,
            role,
            model=model,
            workspace_path=task.get("workspace_path"),
            sandbox=sandbox,
            approval=approval,
            proxy_enabled=proxy_enabled,
            created_by=created_by,
            created_reason=created_reason,
        )
        plan["updated_at"] = now_func()
        save_plan(root, plan)
        _write_task(root, run_id, task)
        ensure_session_func(root, run_id, task_id, agent["id"], backend, model=model, workspace_path=task.get("workspace_path"))
    append_event_func(
        root,
        run_id,
        "agent_created",
        {
            "task_id": task_id,
            "agent_id": agent["id"],
            "backend": backend,
            "model": model,
            "sandbox": agent.get("sandbox"),
            "approval": agent.get("approval"),
            "proxy_enabled": agent.get("proxy_enabled"),
            "created_by": created_by,
            "created_reason": created_reason,
        },
    )
    return agent


def update_agent_config(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    sandbox: str | None = None,
    approval: str | None = None,
    proxy_enabled: bool | None = None,
    *,
    now_func: Callable[[], str] = utc_now,
    append_event_func: Callable[[Path, str, str, dict], dict] = default_append_event,
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = _find_task(plan, task_id)
        agent = _find_agent(task, agent_id)
        if sandbox is not None:
            agent["sandbox"] = sandbox
            if agent_id == "main":
                task["preferred_sandbox"] = sandbox
        if approval is not None:
            agent["approval"] = approval
            if agent_id == "main":
                task["preferred_approval"] = approval
        if proxy_enabled is not None:
            agent["proxy_enabled"] = bool(proxy_enabled)
            supervision = normalize_task_supervision(task.get("supervision"))
            if agent.get("role") == "host" or supervision.get("host_agent_id") == agent_id:
                supervision["host_proxy_enabled"] = bool(proxy_enabled)
                task["supervision"] = normalize_task_supervision(supervision)
        agent["last_active_at"] = now_func()
        plan["updated_at"] = now_func()
        save_plan(root, plan)
        _write_task(root, run_id, task)
    append_event_func(
        root,
        run_id,
        "agent_config_updated",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "sandbox": agent.get("sandbox"),
            "approval": agent.get("approval"),
            "proxy_enabled": agent.get("proxy_enabled"),
        },
    )
    return agent


def set_agent_status(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    status: str,
    exit_code: int | None = None,
    *,
    waiting_reason: str | None = None,
    now_func: Callable[[], str] = utc_now,
    append_event_func: Callable[[Path, str, str, dict], dict] = default_append_event,
) -> dict:
    now = now_func()
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = _find_task(plan, task_id)
        agent = _find_agent(task, agent_id)
        previous_status = agent.get("status")
        previous_waiting_reason = agent.get("waiting_reason")
        agent["status"] = status
        if status == "waiting" and waiting_reason:
            agent["waiting_reason"] = str(waiting_reason)
        else:
            agent.pop("waiting_reason", None)
        agent["last_active_at"] = now
        if previous_status != status or previous_waiting_reason != agent.get("waiting_reason") or not agent.get("status_started_at"):
            agent["status_started_at"] = now
        if status == "running":
            agent["started_at"] = now
            agent["finished_at"] = None
            agent["exit_code"] = None
        elif status in {"completed", "failed", "blocked", "interrupted", "stopped"}:
            agent["finished_at"] = now
            agent["exit_code"] = exit_code
        plan["updated_at"] = now
        save_plan(root, plan)
        _write_task(root, run_id, task)
    append_event_func(
        root,
        run_id,
        "agent_status_changed",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "status": status,
            "waiting_reason": agent.get("waiting_reason") or "",
            "exit_code": exit_code,
            "status_started_at": agent.get("status_started_at"),
        },
    )
    return agent


def update_agent_runtime(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    *,
    now_func: Callable[[], str] = utc_now,
    append_event_func: Callable[[Path, str, str, dict], dict] = default_append_event,
    **fields: object,
) -> dict:
    now = now_func()
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = _find_task(plan, task_id)
        agent = _find_agent(task, agent_id)
        for key, value in fields.items():
            agent[key] = value
        agent["last_active_at"] = now
        plan["updated_at"] = now
        save_plan(root, plan)
        _write_task(root, run_id, task)
    append_event_func(root, run_id, "agent_runtime_updated", {"task_id": task_id, "agent_id": agent_id, **fields})
    return agent
