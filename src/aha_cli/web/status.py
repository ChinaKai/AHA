from __future__ import annotations

from pathlib import Path
import time

from aha_cli.domain.models import utc_now
from aha_cli.services.app_version import aha_version
from aha_cli.services.backend_runtime import backend_status
from aha_cli.store.filesystem import (
    append_event,
    append_message,
    event_path,
    iter_jsonl_reverse,
    set_agent_status,
    set_task_status,
    status_snapshot,
    status_snapshot_projection,
    task_snapshot,
    update_agent_runtime,
)
from aha_cli.store.runs import require_plan

BACKEND_STATUS_CACHE_TTL_SECONDS = 0.75
TASK_OUTCOME_SCAN_LIMIT = 10000
TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}
STALE_RECOVERABLE_TASK_STATUSES = {"running", "awaiting_user"}
_BACKEND_STATUS_CACHE: dict[tuple[str, str, str, str], tuple[float, dict]] = {}


def task_outcome_snapshots(root: Path, run_id: str, task_ids: set[str] | None = None) -> dict[str, dict]:
    outcomes: dict[str, dict] = {}
    wanted = {task_id for task_id in (task_ids or set()) if task_id}
    scanned = 0
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        scanned += 1
        if event.get("type") != "task_status_changed":
            if scanned >= TASK_OUTCOME_SCAN_LIMIT:
                break
            continue
        data = event.get("data") or {}
        task_id = str(data.get("task_id") or "")
        status = str(data.get("status") or "")
        if task_id and task_id not in outcomes and status in TERMINAL_TASK_STATUSES:
            outcomes[task_id] = {
                "status": status,
                "exit_code": data.get("exit_code"),
                "updated_at": event.get("ts"),
            }
            if wanted and wanted.issubset(outcomes):
                break
        if scanned >= TASK_OUTCOME_SCAN_LIMIT:
            break
    return outcomes


def task_activity_status(task: dict) -> str:
    process_statuses = {
        str(agent.get("backend_process_status") or "stopped").lower()
        for agent in task.get("agents", [])
    }
    if "busy" in process_statuses:
        return "busy"
    agent_statuses = {
        str(agent.get("status") or "").lower()
        for agent in task.get("agents", [])
    }
    if "running" in agent_statuses:
        return "running"
    if str(task.get("status") or "").lower() == "running":
        return "running"
    return "idle"


def cached_backend_status(root: Path, run_id: str, target: str, task_id: str | None) -> dict:
    key = (str(root), run_id, task_id or "", target or "main")
    now = time.monotonic()
    cached = _BACKEND_STATUS_CACHE.get(key)
    if cached and now - cached[0] <= BACKEND_STATUS_CACHE_TTL_SECONDS:
        return dict(cached[1])
    state = backend_status(root, run_id, target, task_id=task_id)
    _BACKEND_STATUS_CACHE[key] = (now, dict(state))
    return state


def invalidate_backend_status_cache(root: Path, run_id: str, target: str | None = None, task_id: str | None = None) -> None:
    root_key = str(root)
    target_key = target or None
    task_key = task_id or None
    for key in list(_BACKEND_STATUS_CACHE):
        key_root, key_run_id, key_task_id, key_target = key
        if key_root != root_key or key_run_id != run_id:
            continue
        if target_key is not None and key_target != target_key:
            continue
        if task_key is not None and key_task_id != task_key:
            continue
        _BACKEND_STATUS_CACHE.pop(key, None)


def agent_recovery_context(agent_id: str, reason: str) -> str:
    return "\n".join(
        [
            f"上一轮 agent `{agent_id}` 工作异常中断，AHA 已检测到 backend 停止并恢复任务状态。",
            f"异常原因：{reason}。",
            "继续前请注意：当前工作区、任务消息、子代理和命令可能已有部分副作用；请先基于当前实际状态检查已有进展，再决定继续、等待、补救或重新创建子代理。",
        ]
    )


def sub_agent_recovery_notice(agent_id: str, reason: str) -> str:
    return "\n".join(
        [
            f"AHA 检测到你创建的子代理 `{agent_id}` backend 已停止，并已把该子代理标记为 interrupted。",
            f"异常原因：{reason}。",
            "不要假设它已经完成；它可能已经产生部分文件、日志或分析结论。继续前请检查该子代理的状态、消息和相关输出，再决定重启子代理、接手处理、等待其他子代理，或汇总已有结果。",
        ]
    )


def append_recovery_context(existing: object, notice: str) -> str:
    current = str(existing or "").strip()
    text = notice.strip()
    if not text:
        return current
    if text in current:
        return current
    return f"{current}\n\n{text}".strip() if current else text


def record_main_recovery_context(root: Path, run_id: str, task_id: str, task: dict, notice: str, reason: str) -> None:
    main_agent = next((item for item in task.get("agents", []) if str(item.get("id") or "") == "main"), None)
    if not main_agent:
        return
    if str(main_agent.get("status") or "") == "running":
        append_message(
            root,
            run_id,
            "main",
            notice,
            sender="aha",
            task_id=task_id,
            role="main",
            from_agent="aha",
            to_agent="main",
            reply_target="browser",
            coordination="agent_recovery_notice",
        )
    else:
        update_agent_runtime(
            root,
            run_id,
            task_id,
            "main",
            recovery_context=append_recovery_context(main_agent.get("recovery_context"), notice),
            recovery_context_reason=reason,
            recovery_context_at=utc_now(),
            recovery_context_consumed_at="",
        )
    append_event(
        root,
        run_id,
        "task_recovery_context_recorded",
        {"task_id": task_id, "target_agent_id": "main", "reason": reason},
    )


def consume_agent_recovery_context(root: Path, run_id: str, task_id: str | None, agent_id: str) -> str:
    if not task_id or not agent_id:
        return ""
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return ""
    agent = next((item for item in task.get("agents", []) if str(item.get("id") or "") == agent_id), None)
    if not agent:
        return ""
    context = str(agent.get("recovery_context") or "").strip()
    if not context:
        return ""
    update_agent_runtime(
        root,
        run_id,
        task_id,
        agent_id,
        recovery_context="",
        recovery_context_reason="",
        recovery_context_at="",
        recovery_context_consumed_at=utc_now(),
    )
    append_event(
        root,
        run_id,
        "agent_recovery_context_consumed",
        {"task_id": task_id, "agent_id": agent_id},
    )
    return context


def merge_recovery_context_message(recovery_context: str, message: str) -> str:
    context = recovery_context.strip()
    text = message.strip()
    if not context or context in text:
        return text
    return "\n".join([context, "", "用户当前发送的新消息：", text]).strip()


def _is_supervision_host_agent(task: dict, agent_id: str) -> bool:
    supervision = task.get("supervision") if isinstance(task.get("supervision"), dict) else {}
    host_agent_id = str(supervision.get("host_agent_id") or "host")
    agent = next((item for item in task.get("agents", []) if str(item.get("id") or "") == agent_id), None)
    return agent_id == host_agent_id or str((agent or {}).get("role") or "") == "host"


def _clear_main_host_wait_if_needed(root: Path, run_id: str, task_id: str, task: dict, recovered_agent_id: str) -> bool:
    if not _is_supervision_host_agent(task, recovered_agent_id):
        return False
    main_agent = next((item for item in task.get("agents", []) if str(item.get("id") or "") == "main"), None)
    if not (
        main_agent
        and str(main_agent.get("status") or "").lower() == "waiting"
        and str(main_agent.get("waiting_reason") or "").lower() == "host"
    ):
        return False
    set_agent_status(root, run_id, task_id, "main", "completed")
    return True


def recover_stale_running_agent(root: Path, run_id: str, task: dict, agent: dict, backend_state: dict) -> bool:
    task_id = str(task.get("id") or "")
    agent_id = str(agent.get("id") or "main")
    agent_status = str(agent.get("status") or "")
    backend_process_status = str(backend_state.get("status") or "stopped").lower()
    if not task_id or not agent_id or agent_status != "running" or backend_process_status != "stopped":
        return False

    fresh_backend_state = backend_status(root, run_id, agent_id, task_id=task_id)
    fresh_backend_status = str(fresh_backend_state.get("status") or "stopped").lower()
    if fresh_backend_status != "stopped":
        invalidate_backend_status_cache(root, run_id, agent_id, task_id)
        backend_state.clear()
        backend_state.update(fresh_backend_state)
        return False

    try:
        persisted_task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return False
    persisted_agent = next((item for item in persisted_task.get("agents", []) if str(item.get("id") or "") == agent_id), None)
    if (
        persisted_agent is None
        or str(persisted_task.get("status") or "") not in STALE_RECOVERABLE_TASK_STATUSES
        or str(persisted_agent.get("status") or "") != "running"
    ):
        return False

    recovery_reason = "backend_process_stopped"
    updated_agent = set_agent_status(root, run_id, task_id, agent_id, "interrupted")
    updated_agent = update_agent_runtime(
        root,
        run_id,
        task_id,
        agent_id,
        recovery_context=agent_recovery_context(agent_id, recovery_reason),
        recovery_context_reason=recovery_reason,
        recovery_context_at=utc_now(),
        recovery_context_consumed_at="",
    )
    agent.update(updated_agent)
    cleared_main_host_wait = _clear_main_host_wait_if_needed(root, run_id, task_id, persisted_task, agent_id)
    if agent_id != "main":
        fresh_task = task_snapshot(root, run_id, task_id)["task"]
        record_main_recovery_context(
            root,
            run_id,
            task_id,
            fresh_task,
            sub_agent_recovery_notice(agent_id, recovery_reason),
            recovery_reason,
        )
        from aha_cli.services.orchestrator import (
            append_visible_sub_agent_result,
            request_round_summary_if_ready,
            sub_agent_failure_message,
        )

        append_visible_sub_agent_result(
            root,
            run_id,
            task_id,
            agent_id,
            sub_agent_failure_message(agent_id, recovery_reason),
            coordination="sub_agent_failed",
        )
        main_agent = next((item for item in fresh_task.get("agents", []) if str(item.get("id") or "") == "main"), None)
        main_waiting_for_subagents = (
            main_agent is not None
            and str(main_agent.get("status") or "") == "waiting"
            and str(main_agent.get("waiting_reason") or "") == "subagents"
        )
        round_summary_requested = request_round_summary_if_ready(root, run_id, task_id) if main_waiting_for_subagents else False
    else:
        round_summary_requested = False

    task_recovered = False
    other_agent_running = any(
        str(item.get("id") or "") != agent_id and str(item.get("status") or "") == "running"
        for item in persisted_task.get("agents", [])
    )
    if not other_agent_running and not round_summary_requested:
        if str(persisted_task.get("status") or "") != "awaiting_user":
            updated_task = set_task_status(root, run_id, task_id, "awaiting_user")
            for field in ("status", "exit_code", "started_at", "finished_at"):
                task[field] = updated_task.get(field)
            task_recovered = True
        else:
            task["status"] = "awaiting_user"

    append_event(
        root,
        run_id,
        "agent_status_recovered",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "from_status": "running",
            "status": "interrupted",
            "reason": recovery_reason,
            "backend": {"status": backend_process_status, "pid": backend_state.get("pid")},
            "task_recovered": task_recovered,
            "cleared_main_host_wait": cleared_main_host_wait,
        },
    )
    return True


def decorate_task_status(task: dict, outcomes: dict[str, dict] | None = None) -> None:
    raw_task_id = str(task.get("id") or "")
    agents = task.get("agents", [])
    task["agent_count"] = task.get("agent_count", len(agents))
    current_status = str(task.get("status") or "pending")
    outcome = (
        current_status
        if current_status in TERMINAL_TASK_STATUSES
        else (outcomes or {}).get(raw_task_id, {}).get("status")
    )
    display_status = current_status if current_status in {"running", "awaiting_user"} else outcome or current_status
    task["current_status"] = current_status
    task["outcome_status"] = outcome
    task["activity_status"] = task_activity_status(task)
    task["display_status"] = display_status


def web_tasks_snapshot(
    root: Path,
    run_id: str,
    *,
    lite: bool = False,
    selected_task_id: str | None = None,
    include_outcomes: bool = False,
    task_limit: int | None = None,
    task_offset: int = 0,
    task_filter: str | None = None,
) -> dict:
    snapshot = status_snapshot_projection(
        root,
        run_id,
        lite=lite,
        selected_task_id=selected_task_id,
        task_limit=task_limit,
        task_offset=task_offset,
        task_filter=task_filter,
    )
    snapshot["aha_version"] = aha_version(root)
    task_ids = {str(task.get("id") or "") for task in snapshot.get("tasks", [])}
    outcomes = task_outcome_snapshots(root, run_id, task_ids) if include_outcomes else {}
    for task in snapshot.get("tasks", []):
        raw_task_id = str(task.get("id") or "")
        decorate_task_status(task, outcomes)
    return snapshot


def _task_option_title(task: dict) -> str:
    for key in ("title", "summary", "objective"):
        value = str(task.get(key) or "").strip()
        if value:
            return value
    return str(task.get("id") or "").strip()


def _task_option_payload(task: dict) -> dict:
    raw_task_id = str(task.get("id") or "").strip()
    return {
        "id": raw_task_id,
        "title": _task_option_title(task),
        "status": str(task.get("status") or "pending"),
        "current_status": str(task.get("current_status") or task.get("status") or "pending"),
        "outcome_status": task.get("outcome_status"),
        "display_status": str(task.get("display_status") or task.get("status") or "pending"),
        "hidden": bool(task.get("hidden")),
    }


def _task_option_matches_filter(option: dict, status_filter: str) -> bool:
    status = str(option.get("display_status") or option.get("status") or "pending").lower()
    if status_filter == "all":
        return True
    if status_filter == "running":
        return status == "running"
    if status_filter == "completed":
        return status == "completed"
    return not option.get("hidden") and status not in TERMINAL_TASK_STATUSES


def _task_option_matches_query(option: dict, query_text: str) -> bool:
    if not query_text:
        return True
    query = query_text.lower()
    values = [
        option.get("id"),
        option.get("title"),
        option.get("display_status"),
        option.get("current_status"),
    ]
    return any(query in str(value or "").lower() for value in values)


def web_task_options_snapshot(
    root: Path,
    run_id: str,
    *,
    q: str = "",
    status_filter: str = "active",
    include_id: str = "",
    limit: int = 100,
) -> dict:
    plan = require_plan(root, run_id)
    options: list[dict] = []
    included: dict | None = None
    normalized_filter = status_filter if status_filter in {"active", "running", "completed", "all"} else "active"
    query_text = str(q or "").strip()
    requested_id = str(include_id or "").strip()
    for task in plan.get("tasks", []):
        if task.get("deleted_at"):
            continue
        raw_task_id = str(task.get("id") or "").strip()
        if not raw_task_id:
            continue
        decorate_task_status(task)
        option = _task_option_payload(task)
        if requested_id and raw_task_id == requested_id:
            included = option
        if not _task_option_matches_filter(option, normalized_filter):
            continue
        if not _task_option_matches_query(option, query_text):
            continue
        options.append(option)
        if len(options) >= limit:
            break
    if included:
        options = [option for option in options if option.get("id") != included.get("id")]
        options.insert(0, included)
    return {"tasks": options[: max(1, limit)]}


def backend_runtime_payload(state: dict, *, task_id: str | None, agent_id: str) -> dict:
    return {
        "id": agent_id,
        "target": state.get("target") or agent_id,
        "task_id": state.get("task_id") or task_id,
        "status": state.get("status") or "stopped",
        "pid": state.get("pid"),
        "last_reply_at": state.get("last_reply_at"),
        "resolved_model": state.get("resolved_model"),
        "runtime_context_window": state.get("runtime_context_window"),
        "runtime_context_usage": state.get("runtime_context_usage"),
        "context_pressure": state.get("context_pressure"),
        "latest_usage": state.get("latest_usage"),
        "latest_prompt_metrics": state.get("latest_prompt_metrics"),
    }


def apply_backend_runtime(agent: dict, state: dict) -> None:
    agent["backend_process_status"] = state.get("status") or "stopped"
    agent["backend_process_pid"] = state.get("pid")
    agent["backend_process_last_reply_at"] = state.get("last_reply_at")
    agent["backend_resolved_model"] = state.get("resolved_model")
    agent["backend_runtime_context_window"] = state.get("runtime_context_window")
    agent["backend_runtime_context_usage"] = state.get("runtime_context_usage")
    agent["backend_context_pressure"] = state.get("context_pressure")
    agent["backend_latest_usage"] = state.get("latest_usage")
    agent["backend_latest_prompt_metrics"] = state.get("latest_prompt_metrics")


def attach_backend_runtime(
    root: Path,
    run_id: str,
    snapshot: dict,
    *,
    recover_stale: bool = False,
) -> dict:
    backend_cache: dict[tuple[str | None, str], dict] = {}
    for task in snapshot.get("tasks", []):
        raw_task_id = str(task.get("id") or "")
        task_id = raw_task_id or None
        agents = task.get("agents", [])
        for agent in agents:
            target = str(agent.get("id") or "main")
            key = (task_id, target)
            if key not in backend_cache:
                backend_cache[key] = cached_backend_status(root, run_id, target, task_id=task_id)
            state = backend_cache[key]
            if recover_stale:
                recover_stale_running_agent(root, run_id, task, agent, state)
            apply_backend_runtime(agent, state)
        task["activity_status"] = task_activity_status(task)
    return snapshot


def web_agents_runtime_snapshot(root: Path, run_id: str, task_id: str) -> dict:
    plan = require_plan(root, run_id)
    task = next(
        (
            item
            for item in plan.get("tasks", [])
            if str(item.get("id") or "") == task_id and not item.get("deleted_at")
        ),
        None,
    )
    if task is None:
        raise KeyError(task_id)
    runtime_agents = []
    activity_task = {"status": task.get("status"), "agents": []}
    for agent in task.get("agents", []):
        agent_id = str(agent.get("id") or "main")
        state = cached_backend_status(root, run_id, agent_id, task_id=task_id)
        payload = backend_runtime_payload(state, task_id=task_id, agent_id=agent_id)
        runtime_agents.append(payload)
        activity_agent = dict(agent)
        apply_backend_runtime(activity_agent, payload)
        activity_task["agents"].append(activity_agent)
    return {
        "run_id": run_id,
        "task_id": task_id,
        "agent_count": len(task.get("agents", [])),
        "activity_status": task_activity_status(activity_task),
        "agents": runtime_agents,
    }


def recover_stale_running_agents(
    root: Path,
    run_id: str,
    *,
    task_id: str | None = None,
    target: str | None = None,
) -> dict:
    snapshot = status_snapshot(root, run_id)
    checked = 0
    recovered: list[dict] = []
    for task in snapshot.get("tasks", []):
        current_task_id = str(task.get("id") or "")
        if task_id and current_task_id != task_id:
            continue
        for agent in task.get("agents", []):
            agent_id = str(agent.get("id") or "main")
            if target and agent_id != target:
                continue
            if str(agent.get("status") or "") != "running":
                continue
            checked += 1
            state = cached_backend_status(root, run_id, agent_id, task_id=current_task_id or None)
            if recover_stale_running_agent(root, run_id, task, agent, state):
                invalidate_backend_status_cache(root, run_id, agent_id, current_task_id)
                recovered.append({"task_id": current_task_id, "agent_id": agent_id})
    return {
        "run_id": run_id,
        "task_id": task_id,
        "target": target,
        "checked": checked,
        "recovered_count": len(recovered),
        "recovered": recovered,
    }


def web_status_snapshot(
    root: Path,
    run_id: str,
    *,
    lite: bool = False,
    selected_task_id: str | None = None,
    task_limit: int | None = None,
    task_offset: int = 0,
    task_filter: str | None = None,
) -> dict:
    snapshot = web_tasks_snapshot(
        root,
        run_id,
        lite=lite,
        selected_task_id=selected_task_id,
        include_outcomes=True,
        task_limit=task_limit,
        task_offset=task_offset,
        task_filter=task_filter,
    )
    if lite:
        return snapshot
    return attach_backend_runtime(root, run_id, snapshot, recover_stale=False)

__all__ = [
    "task_outcome_snapshots",
    "task_activity_status",
    "cached_backend_status",
    "invalidate_backend_status_cache",
    "STALE_RECOVERABLE_TASK_STATUSES",
    "agent_recovery_context",
    "sub_agent_recovery_notice",
    "append_recovery_context",
    "record_main_recovery_context",
    "consume_agent_recovery_context",
    "merge_recovery_context_message",
    "recover_stale_running_agent",
    "recover_stale_running_agents",
    "decorate_task_status",
    "backend_runtime_payload",
    "apply_backend_runtime",
    "web_tasks_snapshot",
    "web_agents_runtime_snapshot",
    "attach_backend_runtime",
    "web_status_snapshot",
]
