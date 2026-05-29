from __future__ import annotations

from pathlib import Path

from aha_cli.backends.registry import agent_backend_names
from aha_cli.domain.models import normalize_task_supervision, utc_now
from aha_cli.services.backend_runtime import PROCESS_AGENT_BACKENDS, backend_status, start_backend, stop_backend
from aha_cli.services.session_compact import build_compact_summary, compact_summary_dir, compact_summary_relpath
from aha_cli.store.config import load_config
from aha_cli.store.filesystem import append_event, append_message, task_snapshot
from aha_cli.store.io import write_json
from aha_cli.store.paths import run_dir
from aha_cli.store.runs import locked_plan, require_plan, save_plan
from aha_cli.store.sessions import ensure_session, save_session

UNSET = object()


def _find_task(plan: dict, task_id: str) -> dict:
    task = next((item for item in plan.get("tasks", []) if item.get("id") == task_id), None)
    if task is None or task.get("deleted_at"):
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


def _handoff_summary_path(root: Path, run_id: str, task_id: str, agent_id: str, summary: str, switched_at: str) -> Path:
    summary_id = switched_at.replace(":", "").replace("+", "Z")
    path = compact_summary_dir(root, run_id, task_id) / f"{agent_id}-backend-switch-{summary_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(summary, encoding="utf-8")
    return path


def _handoff_message(
    *,
    agent_id: str,
    old_backend: str,
    new_backend: str,
    old_model: str | None,
    new_model: str | None,
    summary_path: str,
) -> str:
    return "\n".join(
        [
            "AHA backend handoff.",
            f"- agent: {agent_id}",
            f"- previous backend: {old_backend}",
            f"- new backend: {new_backend}",
            f"- previous model: {old_model or '-'}",
            f"- new model: {new_model or '-'}",
            f"- handoff summary: `{summary_path}`",
            "",
            "Read the handoff summary before continuing. Preserve the current task intent, decisions, and open work.",
        ]
    )


def switch_agent_backend(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    *,
    backend: str,
    model: object = UNSET,
    restart_if_active: bool = True,
) -> dict:
    new_backend = str(backend or "").strip()
    if new_backend not in agent_backend_names():
        raise ValueError(f"unknown agent backend: {new_backend}")

    detail = task_snapshot(root, run_id, task_id)
    task = detail["task"]
    agent = _find_agent(task, agent_id)
    old_backend = str(agent.get("backend") or task.get("preferred_backend") or "codex")
    old_model = _model_value(agent.get("model") or (task.get("preferred_model") if agent_id == "main" else None))
    model_provided = model is not UNSET
    new_model = _model_value(model) if model_provided else (None if new_backend != old_backend else old_model)
    backend_changed = new_backend != old_backend
    model_changed = model_provided and new_model != old_model
    if not backend_changed and not model_changed:
        return {"ok": True, "changed": False, "agent": agent}
    switch_reason = "backend_switch" if backend_changed else "model_switch"

    old_session = ensure_session(
        root,
        run_id,
        task_id,
        agent_id,
        old_backend,
        model=old_model,
        workspace_path=agent.get("workspace_path") or task.get("workspace_path"),
    )
    old_backend_session_id = old_session.get("backend_session_id")
    old_state = backend_status(root, run_id, agent_id, task_id=task_id)
    was_active = str(old_state.get("status") or "stopped") in {"running", "busy"}
    stopped_backend = None
    if was_active:
        stopped_backend = stop_backend(root, run_id, agent_id, task_id=task_id, timeout=3.0)

    switched_at = utc_now()
    summary = build_compact_summary(root, run_id, task_id, agent_id, old_session, switch_reason)
    summary_path = _handoff_summary_path(root, run_id, task_id, agent_id, summary, switched_at)
    summary_ref = compact_summary_relpath(root, run_id, summary_path)

    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = _find_task(plan, task_id)
        agent = _find_agent(task, agent_id)
        agent["backend"] = new_backend
        if new_model:
            agent["model"] = new_model
        else:
            agent.pop("model", None)
        agent["last_active_at"] = switched_at
        if agent_id == "main":
            task["preferred_backend"] = new_backend
            if new_model:
                task["preferred_model"] = new_model
            else:
                task.pop("preferred_model", None)
        supervision = normalize_task_supervision(task.get("supervision"))
        if agent.get("role") == "host" or supervision.get("host_agent_id") == agent_id:
            supervision["host_backend"] = new_backend
            supervision["real_agent_enabled"] = new_backend != "stub"
            task["supervision"] = normalize_task_supervision(supervision)
        plan["updated_at"] = switched_at
        save_plan(root, plan)
        _write_task(root, run_id, task)

    session = ensure_session(
        root,
        run_id,
        task_id,
        agent_id,
        new_backend,
        model=new_model,
        workspace_path=agent.get("workspace_path") or task.get("workspace_path"),
    )
    if model_changed and session.get("backend_session_id"):
        history = session.get("history_backend_sessions")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "backend_session_id": session.get("backend_session_id"),
                "backend": session.get("backend"),
                "model": old_model,
                "started_at": session.get("created_at"),
                "archived_at": switched_at,
                "reason": "model_changed",
            }
        )
        session["history_backend_sessions"] = history
        session["backend_session_id"] = None
        session["status"] = "reset"
    session["backend"] = new_backend
    session["model"] = new_model
    session["updated_at"] = switched_at
    session["compact_summary"] = {
        "id": summary_path.stem,
        "path": summary_ref,
        "created_at": switched_at,
        "reason": switch_reason,
        "chars": len(summary),
        "archived_backend_session_id": old_backend_session_id,
    }
    save_session(root, session)

    append_event(
        root,
        run_id,
        "backend_session_reset",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "old_backend_session_id": old_backend_session_id,
            "old_backend": old_backend,
            "new_backend": new_backend,
            "reason": switch_reason,
            "summary_path": summary_ref,
        },
    )
    append_event(
        root,
        run_id,
        "agent_backend_switched",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "old_backend": old_backend,
            "new_backend": new_backend,
            "old_model": old_model,
            "new_model": new_model,
            "was_active": was_active,
            "summary_path": summary_ref,
        },
    )
    append_event(
        root,
        run_id,
        "agent_config_updated",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "backend": new_backend,
            "model": new_model,
            "sandbox": agent.get("sandbox"),
            "approval": agent.get("approval"),
            "proxy_enabled": agent.get("proxy_enabled"),
        },
    )
    append_message(
        root,
        run_id,
        agent_id,
        _handoff_message(
            agent_id=agent_id,
            old_backend=old_backend,
            new_backend=new_backend,
            old_model=old_model,
            new_model=new_model,
            summary_path=summary_ref,
        ),
        sender="aha",
        task_id=task_id,
        role=str(agent.get("role") or ""),
        from_agent="aha",
        to_agent=agent_id,
        agent_id=agent_id,
        coordination="backend_switch",
        display_sender="AHA",
        display_target=agent_id,
    )

    backend_state = None
    if restart_if_active and was_active and new_backend in PROCESS_AGENT_BACKENDS:
        cfg = load_config(root)
        backend_state = start_backend(
            root,
            run_id,
            agent_id,
            backend=new_backend,
            model=new_model,
            sandbox=agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
            approval=agent.get("approval") or task.get("preferred_approval") or "never",
            codex_bin=(cfg.get("codex", {}) or {}).get("bin") or "codex",
            claude_bin=(cfg.get("claude", {}) or {}).get("bin") or "claude",
            from_start=False,
            task_id=task_id,
        )

    return {
        "ok": True,
        "changed": True,
        "agent": agent,
        "session": session,
        "summary_path": summary_ref,
        "old_backend": old_backend,
        "new_backend": new_backend,
        "stopped_backend": stopped_backend,
        "backend": backend_state,
    }


def restart_agent_backend(root: Path, run_id: str, task_id: str, agent_id: str) -> dict:
    detail = task_snapshot(root, run_id, task_id)
    task = detail["task"]
    agent = _find_agent(task, agent_id)
    backend = str(agent.get("backend") or task.get("preferred_backend") or "codex")
    if backend not in PROCESS_AGENT_BACKENDS:
        raise ValueError(f"backend {backend} does not have a chat process")
    state = backend_status(root, run_id, agent_id, task_id=task_id)
    stopped_backend = None
    if str(state.get("status") or "stopped") != "stopped":
        stopped_backend = stop_backend(root, run_id, agent_id, task_id=task_id, timeout=3.0)
    cfg = load_config(root)
    backend_state = start_backend(
        root,
        run_id,
        agent_id,
        backend=backend,
        model=agent.get("model") or task.get("preferred_model"),
        sandbox=agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
        approval=agent.get("approval") or task.get("preferred_approval") or "never",
        codex_bin=(cfg.get("codex", {}) or {}).get("bin") or "codex",
        claude_bin=(cfg.get("claude", {}) or {}).get("bin") or "claude",
        from_start=False,
        task_id=task_id,
    )
    append_event(
        root,
        run_id,
        "agent_backend_restarted",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "backend": backend,
            "stopped": bool(stopped_backend),
        },
    )
    return {"ok": True, "agent": agent, "stopped_backend": stopped_backend, "backend": backend_state}


__all__ = ["restart_agent_backend", "switch_agent_backend"]
