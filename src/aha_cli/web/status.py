from __future__ import annotations

from pathlib import Path
import time

from aha_cli.domain.models import utc_now
from aha_cli.services.backend_runtime import backend_status
from aha_cli.store.filesystem import (
    append_event,
    append_message,
    event_path,
    iter_jsonl_reverse,
    set_agent_status,
    set_task_status,
    status_snapshot,
    task_snapshot,
    update_agent_runtime,
)

BACKEND_STATUS_CACHE_TTL_SECONDS = 0.75
TASK_OUTCOME_SCAN_LIMIT = 10000
TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}
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


def recover_stale_running_agent(root: Path, run_id: str, task: dict, agent: dict, backend_state: dict) -> bool:
    task_id = str(task.get("id") or "")
    agent_id = str(agent.get("id") or "main")
    agent_status = str(agent.get("status") or "")
    backend_process_status = str(backend_state.get("status") or "stopped").lower()
    if not task_id or not agent_id or agent_status != "running" or backend_process_status != "stopped":
        return False

    try:
        persisted_task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return False
    persisted_agent = next((item for item in persisted_task.get("agents", []) if str(item.get("id") or "") == agent_id), None)
    if (
        persisted_agent is None
        or str(persisted_task.get("status") or "") != "running"
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

    task_recovered = False
    other_agent_running = any(
        str(item.get("id") or "") != agent_id and str(item.get("status") or "") == "running"
        for item in persisted_task.get("agents", [])
    )
    if not other_agent_running:
        updated_task = set_task_status(root, run_id, task_id, "awaiting_user")
        for field in ("status", "exit_code", "started_at", "finished_at"):
            task[field] = updated_task.get(field)
        task_recovered = True

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
        },
    )
    return True


def web_status_snapshot(root: Path, run_id: str, *, lite: bool = False, selected_task_id: str | None = None) -> dict:
    snapshot = status_snapshot(root, run_id)
    task_ids = {str(task.get("id") or "") for task in snapshot.get("tasks", [])}
    outcomes = task_outcome_snapshots(root, run_id, task_ids)
    backend_cache: dict[tuple[str | None, str], dict] = {}
    for task in snapshot.get("tasks", []):
        raw_task_id = str(task.get("id") or "")
        task_id = raw_task_id or None
        include_all_agent_details = not lite or (selected_task_id is not None and raw_task_id == selected_task_id)
        agents = task.get("agents", [])
        task["agent_count"] = len(agents)
        for agent in agents:
            target = str(agent.get("id") or "main")
            agent_status = str(agent.get("status") or "").lower()
            if lite and not include_all_agent_details and agent_status != "running":
                continue
            key = (task_id, target)
            if key not in backend_cache:
                backend_cache[key] = cached_backend_status(root, run_id, target, task_id=task_id)
            state = backend_cache[key]
            recover_stale_running_agent(root, run_id, task, agent, state)
            agent["backend_process_status"] = state.get("status") or "stopped"
            agent["backend_process_pid"] = state.get("pid")
            agent["backend_process_last_reply_at"] = state.get("last_reply_at")
        current_status = str(task.get("status") or "pending")
        outcome = current_status if current_status in TERMINAL_TASK_STATUSES else outcomes.get(raw_task_id, {}).get("status")
        display_status = current_status if current_status in {"running", "awaiting_user"} else outcome or current_status
        task["current_status"] = current_status
        task["outcome_status"] = outcome
        task["activity_status"] = task_activity_status(task)
        task["display_status"] = display_status
        if lite and (not selected_task_id or raw_task_id != selected_task_id):
            task["agents"] = []
    return snapshot

__all__ = [
    "task_outcome_snapshots",
    "task_activity_status",
    "cached_backend_status",
    "agent_recovery_context",
    "sub_agent_recovery_notice",
    "append_recovery_context",
    "record_main_recovery_context",
    "consume_agent_recovery_context",
    "merge_recovery_context_message",
    "recover_stale_running_agent",
    "web_status_snapshot",
]
