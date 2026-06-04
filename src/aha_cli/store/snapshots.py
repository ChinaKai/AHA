from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from aha_cli.domain.models import task_metadata_projection
from aha_cli.services.proxy import backend_has_proxy_config, backend_proxy_config
from aha_cli.store.config import load_config
from aha_cli.store.events import event_stream_position as default_event_stream_position
from aha_cli.store.io import read_json, text_tail_page
from aha_cli.store.journal import latest_final_artifact
from aha_cli.store.paths import run_dir
from aha_cli.store.rounds import list_task_lifecycle_rounds, list_task_rounds
from aha_cli.store.runs import require_plan
from aha_cli.store.sessions import ensure_session as default_ensure_session, list_sessions


def status_snapshot(
    root: Path,
    run_id: str,
    *,
    ensure_session_func: Callable[..., dict] = default_ensure_session,
) -> dict:
    plan = require_plan(root, run_id)
    cfg = load_config(root)

    def with_session(task: dict, agent: dict) -> dict:
        session = ensure_session_func(
            root,
            run_id,
            task["id"],
            agent["id"],
            agent.get("backend", task.get("preferred_backend", "codex")),
            model=agent.get("model"),
            workspace_path=agent.get("workspace_path") or task.get("workspace_path"),
        )
        merged = dict(agent)
        merged["sandbox"] = agent.get("sandbox") or task.get("preferred_sandbox")
        merged["approval"] = agent.get("approval") or task.get("preferred_approval")
        merged["proxy_enabled"] = bool(agent.get("proxy_enabled"))
        merged["session_id"] = session.get("id")
        merged["backend_session_id"] = session.get("backend_session_id")
        merged["session_scope"] = session.get("scope")
        merged["session_status"] = session.get("status")
        merged["session_updated_at"] = session.get("updated_at")
        return merged

    def task_status_item(task: dict) -> dict:
        proxy_config = backend_proxy_config(cfg, task.get("preferred_backend"), plan, task)
        return {
            "id": task["id"],
            "title": task["title"],
            "description": task.get("description", ""),
            **task_metadata_projection(task),
            "run_proxy_enabled": bool(proxy_config.get("enabled")),
            "run_proxy_configured": backend_has_proxy_config(cfg, task.get("preferred_backend"), plan, task),
            "status": task["status"],
            "exit_code": task["exit_code"],
            "started_at": task["started_at"],
            "finished_at": task["finished_at"],
            "current_round_id": task.get("current_round_id"),
            "round_sequence": task.get("round_sequence"),
            "last_final_round_id": task.get("last_final_round_id"),
            "last_final_at": task.get("last_final_at"),
            "coordination": task.get("coordination"),
            "hidden": bool(task.get("hidden")),
            "hidden_at": task.get("hidden_at"),
            "deleted_at": task.get("deleted_at"),
            "agents": [with_session(task, agent) for agent in task.get("agents", [])],
        }

    return {
        "run_id": run_id,
        "goal": plan["goal"],
        "mode": plan["mode"],
        "updated_at": plan["updated_at"],
        "selected_task_id": str((plan.get("ui") or {}).get("selected_task_id") or ""),
        "aha_root": str(root),
        "main_agent": plan.get("main_agent"),
        "proxy": backend_proxy_config(cfg, cfg.get("backend"), plan),
        "tasks": [task_status_item(task) for task in plan["tasks"] if not task.get("deleted_at")],
    }


def status_snapshot_projection(
    root: Path,
    run_id: str,
    *,
    lite: bool = False,
    selected_task_id: str | None = None,
    ensure_session_func: Callable[..., dict] = default_ensure_session,
    event_stream_position_func: Callable[[Path, str], int] = default_event_stream_position,
) -> dict:
    snapshot_event_id = event_stream_position_func(root, run_id)
    snapshot = status_snapshot(root, run_id, ensure_session_func=ensure_session_func)
    if lite and selected_task_id:
        for task in snapshot.get("tasks", []):
            agents = task.get("agents") or []
            task["agent_count"] = len(agents)
            if str(task.get("id") or "") != selected_task_id:
                task["agents"] = []
    snapshot["snapshot_event_id"] = snapshot_event_id
    return snapshot


def task_lookup(root: Path, run_id: str, task_id: str) -> tuple[dict, dict, Path]:
    plan = require_plan(root, run_id)
    task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
    if task is None:
        raise KeyError(task_id)
    run = run_dir(root, run_id)
    return plan, task, run


def task_final_snapshot(root: Path, run_id: str, task_id: str) -> dict:
    _plan, task, run = task_lookup(root, run_id, task_id)
    output_file = run / task["output_file"]
    output_meta_file = output_file.with_suffix(".meta.json")
    result_meta = read_json(output_meta_file) if output_meta_file.exists() else {}
    result = ""
    lifecycle_rounds = list_task_lifecycle_rounds(root, run_id, task_id)
    finalized_rounds = [item for item in lifecycle_rounds if item.get("status") == "finalized" and item.get("final_path")]
    if output_file.exists() and result_meta.get("policy") in {"finalize", "journal"}:
        result = output_file.read_text(encoding="utf-8")
    else:
        latest_final, latest_final_text, latest_final_meta = latest_final_artifact(root, run_id, lifecycle_rounds)
        if latest_final:
            result = latest_final_text
            result_meta = latest_final_meta
    return {
        "task_id": task_id,
        "result": result,
        "result_meta": result_meta,
        "rounds": list_task_rounds(root, run_id, task_id),
        "current_round": next((item for item in lifecycle_rounds if item.get("round_id") == task.get("current_round_id")), None),
        "finals": finalized_rounds,
    }


def task_context_snapshot(root: Path, run_id: str, task_id: str) -> dict:
    plan, task, run = task_lookup(root, run_id, task_id)
    prompt_file = run / task["prompt_file"]
    return {
        "task": task,
        "prompt": prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else "",
        "sessions": list_sessions(root, run_id, task_id),
        "write_scopes": plan.get("write_scopes", []),
    }


def task_log_page(
    root: Path,
    run_id: str,
    task_id: str,
    limit: int = 200,
    before: int | None = None,
    source: str = "auto",
    *,
    task_event_log_page_func: Callable[..., dict],
) -> dict:
    _plan, task, run = task_lookup(root, run_id, task_id)
    log_file = run / task["log_file"]
    selected_source = source if source in {"auto", "file", "events"} else "auto"
    if selected_source == "events" or (selected_source == "auto" and (not log_file.exists() or log_file.stat().st_size == 0)):
        return {"task_id": task_id, **task_event_log_page_func(root, run_id, task_id, limit=limit, before=before)}
    page = text_tail_page(log_file, limit=limit, before=before)
    return {"task_id": task_id, "source": "file", "path": task.get("log_file"), **page}


def task_snapshot(root: Path, run_id: str, task_id: str) -> dict:
    plan, task, run = task_lookup(root, run_id, task_id)
    prompt_file = run / task["prompt_file"]
    output_file = run / task["output_file"]
    output_meta_file = output_file.with_suffix(".meta.json")
    log_file = run / task["log_file"]
    inbox_file = run / task["inbox_file"]
    task_messages = run / "tasks" / task_id / "messages.jsonl"
    result_meta = read_json(output_meta_file) if output_meta_file.exists() else {}
    result = ""
    lifecycle_rounds = list_task_lifecycle_rounds(root, run_id, task_id)
    _latest_final, latest_final_text, latest_final_meta = latest_final_artifact(root, run_id, lifecycle_rounds)
    if latest_final_text:
        result = latest_final_text
        result_meta = latest_final_meta
    elif output_file.exists() and result_meta.get("policy") in {"finalize", "journal"}:
        result = output_file.read_text(encoding="utf-8")
    return {
        "task": task,
        "prompt": prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else "",
        "result": result,
        "result_meta": result_meta,
        "rounds": list_task_rounds(root, run_id, task_id),
        "log": log_file.read_text(encoding="utf-8") if log_file.exists() else "",
        "inbox": inbox_file.read_text(encoding="utf-8") if inbox_file.exists() else "",
        "messages": task_messages.read_text(encoding="utf-8") if task_messages.exists() else "",
        "sessions": list_sessions(root, run_id, task_id),
        "write_scopes": plan.get("write_scopes", []),
    }
