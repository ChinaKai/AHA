from __future__ import annotations

import json
from pathlib import Path

from aha_cli.domain.models import (
    default_tasks,
    make_agent,
    make_task,
    make_task_round,
    next_task_id,
    normalize_task_supervision,
    task_prompt,
    utc_now,
    new_run_id,
)
from aha_cli.services.proxy import DEFAULT_NO_PROXY, normalize_proxy_value, task_has_proxy_config
from aha_cli.store.agents import (
    add_agent as _add_agent,
    add_agent_to_task_dict,
    ensure_task_supervision_host_agent as _ensure_task_supervision_host_agent,
    set_agent_status as _set_agent_status,
    update_agent_config as _update_agent_config,
    update_agent_runtime as _update_agent_runtime,
)
from aha_cli.store.config import load_config
from aha_cli.store.io import (
    append_jsonl,
    iter_jsonl_from,
    iter_jsonl_records_from,
    iter_jsonl_reverse,
    iter_text_lines_reverse,
    read_json,
    text_tail_page,
    write_json,
)
from aha_cli.store.events import (
    append_event as _append_event,
    append_event_to_file as _append_event_to_file,
    event_stream_page,
    event_stream_position,
    normalize_event_id,
    with_event_id,
)
from aha_cli.store.paths import (
    AHA_HOME_ENV,
    _normalized_path,
    aha_home_path,
    config_path,
    default_aha_home,
    event_path,
    find_aha_home,
    find_project_root,
    inbox_path,
    mark_aha_home,
    plan_path,
    run_dir,
    session_path,
    workspaces_dir,
)
from aha_cli.store.runs import (
    latest_run_id,
    list_run_summaries,
    locked_plan,
    require_plan,
    resolve_run_id,
    run_exists,
    run_summary,
    run_summary_from_plan,
    save_plan,
)
from aha_cli.store.sessions import (
    ensure_session as _ensure_session,
    list_sessions as _list_sessions,
    save_session as _save_session,
)
from aha_cli.store.tasks import (
    TERMINAL_TASK_STATUSES,
    delete_task as _delete_task,
    mark_task_coordination as _mark_task_coordination,
    set_task_hidden as _set_task_hidden,
    set_task_status as _set_task_status,
)
from aha_cli.store.workspaces import add_workspace, get_workspace, list_workspaces, resolve_workspace_path

UNSET = object()


def append_event(root: Path, run_id: str, event_type: str, data: dict) -> dict:
    return _append_event(root, run_id, event_type, data, ts=utc_now())


def append_event_to_file(events_file: Path | None, run_id: str, event_type: str, data: dict) -> dict:
    return _append_event_to_file(events_file, run_id, event_type, data, ts=utc_now())


def ensure_session(
    root: Path,
    run_id: str,
    task_id: str | None,
    agent_id: str,
    backend: str,
    model: str | None = None,
    workspace_path: str | None = None,
) -> dict:
    return _ensure_session(root, run_id, task_id, agent_id, backend, model=model, workspace_path=workspace_path, now_func=utc_now)


def save_session(root: Path, session: dict) -> None:
    _save_session(root, session)


def list_sessions(root: Path, run_id: str, task_id: str | None = None) -> list[dict]:
    return _list_sessions(root, run_id, task_id)


def _round_sequence_from_id(round_id: object) -> int | None:
    text = str(round_id or "")
    if text.startswith("round-"):
        try:
            return int(text.split("-", 1)[1])
        except ValueError:
            return None
    return None


def task_lifecycle_rounds_dir(root: Path, run_id: str, task_id: str) -> Path:
    return run_dir(root, run_id) / "tasks" / task_id / "rounds"


def task_lifecycle_round_dir(root: Path, run_id: str, task_id: str, round_id: str) -> Path:
    return task_lifecycle_rounds_dir(root, run_id, task_id) / round_id


def task_lifecycle_round_path(root: Path, run_id: str, task_id: str, round_id: str) -> Path:
    return task_lifecycle_round_dir(root, run_id, task_id, round_id) / "round.json"


def task_round_final_path(root: Path, run_id: str, task_id: str, round_id: str) -> Path:
    return task_lifecycle_round_dir(root, run_id, task_id, round_id) / "final.md"


def task_round_final_meta_path(root: Path, run_id: str, task_id: str, round_id: str) -> Path:
    return task_round_final_path(root, run_id, task_id, round_id).with_suffix(".meta.json")


def _run_relative_path(root: Path, run_id: str, path: Path) -> str:
    try:
        return str(path.relative_to(run_dir(root, run_id)))
    except ValueError:
        return str(path)


def _resolve_run_path(root: Path, run_id: str, value: object) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else run_dir(root, run_id) / path


def _task_round_started_at(task: dict) -> str:
    return str(task.get("started_at") or task.get("created_at") or utc_now())


def _ensure_task_round_record(root: Path, run_id: str, task: dict) -> dict:
    task_id = str(task["id"])
    sequence = int(task.get("round_sequence") or _round_sequence_from_id(task.get("current_round_id")) or 1)
    round_id = str(task.get("current_round_id") or f"round-{sequence:03d}")
    sequence = _round_sequence_from_id(round_id) or sequence
    task["current_round_id"] = round_id
    task["round_sequence"] = sequence
    task.setdefault("last_final_round_id", None)
    task.setdefault("last_final_at", None)

    path = task_lifecycle_round_path(root, run_id, task_id, round_id)
    if path.exists():
        record = read_json(path)
        changed = False
        for key, value in {"task_id": task_id, "round_id": round_id, "sequence": sequence}.items():
            if record.get(key) != value:
                record[key] = value
                changed = True
        record.setdefault("status", "active")
        record.setdefault("started_at", _task_round_started_at(task))
        record.setdefault("finalized_at", None)
        record.setdefault("final_path", None)
        record.setdefault("final_meta_path", None)
        record.setdefault("reopened_from_round_id", None)
        if changed:
            write_json(path, record)
        return record

    record = make_task_round(task_id, sequence, _task_round_started_at(task))
    write_json(path, record)
    return record


def ensure_current_task_round(root: Path, run_id: str, task_id: str) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        record = _ensure_task_round_record(root, run_id, task)
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    return record


def list_task_lifecycle_rounds(root: Path, run_id: str, task_id: str) -> list[dict]:
    base = task_lifecycle_rounds_dir(root, run_id, task_id)
    if not base.is_dir():
        return []
    rounds: list[dict] = []
    for path in sorted(base.glob("round-*/round.json")):
        try:
            rounds.append(read_json(path))
        except (OSError, ValueError):
            continue
    return sorted(rounds, key=lambda item: int(item.get("sequence") or _round_sequence_from_id(item.get("round_id")) or 0))


def append_message(
    root: Path,
    run_id: str,
    target: str,
    message: str,
    sender: str = "main",
    task_id: str | None = None,
    role: str | None = None,
    from_agent: str | None = None,
    to_agent: str | None = None,
    command_namespace: str | None = None,
    original_command: str | None = None,
    result_policy: str | None = None,
    reply_target: str | None = None,
    coordination: str | None = None,
    agent_id: str | None = None,
    display_sender: str | None = None,
    display_target: str | None = None,
) -> dict:
    payload = {
        "ts": utc_now(),
        "run_id": run_id,
        "target": target,
        "sender": sender,
        "message": message,
    }
    if task_id:
        payload["task_id"] = task_id
    if role:
        payload["role"] = role
    if from_agent:
        payload["from_agent"] = from_agent
    if to_agent:
        payload["to_agent"] = to_agent
    if command_namespace:
        payload["command_namespace"] = command_namespace
    if original_command:
        payload["original_command"] = original_command
    if result_policy:
        payload["result_policy"] = result_policy
    if reply_target:
        payload["reply_target"] = reply_target
    if coordination:
        payload["coordination"] = coordination
    if agent_id:
        payload["agent_id"] = agent_id
    if display_sender:
        payload["display_sender"] = display_sender
    if display_target:
        payload["display_target"] = display_target
    append_jsonl(inbox_path(root, run_id, target), payload)
    if task_id:
        append_jsonl(run_dir(root, run_id) / "tasks" / task_id / "messages.jsonl", payload)
    append_event(root, run_id, "message", payload)
    return payload


def format_event_log_line(event: dict) -> str:
    data = event.get("data") or {}
    ts = event.get("ts") or ""
    event_type = str(event.get("type") or "event")
    if event_type == "log":
        return f"[{ts}] {data.get('task_id') or '-'}: {data.get('line') or ''}"
    if event_type == "message":
        task = f" task={data['task_id']}" if data.get("task_id") else ""
        return f"[{ts}] message{task} {data.get('sender') or 'main'} -> {data.get('target') or '-'}: {data.get('message') or ''}"
    return f"[{ts}] {event_type}: {json.dumps(data, ensure_ascii=False)}"


def task_event_log_page(root: Path, run_id: str, task_id: str, limit: int = 200, before: int | None = None) -> dict:
    path = event_path(root, run_id)
    after_offset = path.stat().st_size if path.exists() else 0
    end_offset = after_offset if before is None else max(0, min(before, after_offset))
    safe_limit = max(1, min(limit, 1000))
    matches: list[dict] = []
    for offset, event in iter_jsonl_reverse(path, before=end_offset) or ():
        if event_task_id(event) == task_id:
            matches.append({"_cursor": offset, "text": format_event_log_line(event)})
            if len(matches) > safe_limit:
                break

    has_more = len(matches) > safe_limit
    page = list(reversed(matches[:safe_limit]))
    next_before_offset = page[0].get("_cursor") if has_more and page else None
    return {
        "source": "events",
        "path": "events.jsonl",
        "text": "\n".join(item["text"] for item in page),
        "lines": page,
        "before_offset": end_offset,
        "after_offset": after_offset,
        "next_before_offset": next_before_offset,
        "has_more": has_more,
        "count": len(page),
    }


TIMELINE_EVENT_TYPES = {
    "message",
    "task_dispatched",
    "task_started",
    "task_finished",
    "task_round_started",
    "task_round_recorded",
    "task_journal_rendered",
    "task_result_written",
    "task_final_requested",
    "task_round_summary_requested",
    "task_proxy_config_updated",
    "task_reopened",
    "task_completed",
    "task_waiting_for_subagents",
    "task_status_changed",
    "agent_started",
    "agent_status_changed",
    "agent_thread",
    "agent_command_started",
    "agent_command_finished",
    "agent_message",
    "agent_prompt_metrics",
    "agent_usage",
    "agent_error",
    "agent_context_overflow",
    "agent_delegated",
    "agent_message_routed",
    "claimed_sub_without_aha_agent",
    "native_subagent_tool_used",
    "sub_agent_reported",
    "sub_agent_report_ignored",
    "sub_agent_backend_recovered",
    "sub_agent_backend_failed",
    "agent_created",
    "agent_config_updated",
    "agent_finished",
    "main_reported_to_host",
    "host_decision",
    "main_applied_decision",
    "workspace_missing",
}

SUPERVISION_EVENT_TYPES = {
    "main_reported_to_host",
    "host_decision",
    "main_applied_decision",
}


def conversation_event_category(event_type: str) -> str:
    if event_type == "agent_message":
        return "chat"
    if event_type in {"agent_usage", "agent_prompt_metrics"}:
        return "usage"
    if event_type in {"agent_command_started", "agent_command_finished"}:
        return "commands"
    if event_type == "message":
        return "chat"
    return "runtime"


def event_task_id(event: dict) -> str | None:
    data = event.get("data") or {}
    if data.get("task_id"):
        return str(data["task_id"])
    target = str(data.get("target") or "")
    if event.get("type") == "message" and target.startswith("task-") and target[5:].isdigit():
        return target
    return None


def event_agent_refs(event: dict) -> set[str]:
    data = event.get("data") or {}
    refs: set[str] = set()
    event_type = str(event.get("type") or "")
    if event_type == "message":
        target = str(data.get("target") or "").strip()
        sender = str(data.get("sender") or "").strip()
        private_target = bool(target and target.lower() not in {"browser", "system", "aha", "main"})
        if sender == "AHA" and private_target:
            return set()

    def add(value: object) -> None:
        text = str(value or "").strip()
        if text and text.lower() not in {"browser", "system", "aha"}:
            refs.add(text)

    add(data.get("target"))
    add(data.get("to_agent"))
    add(data.get("from_agent"))
    add(data.get("agent_id"))
    if event.get("type") == "message":
        add(data.get("sender"))
        if any(str(data.get(key) or "").lower() == "aha" for key in ("role", "from_agent", "to_agent", "sender", "target")):
            refs.add("main")
    if not refs and (
        event_type.startswith("agent_")
        or event_type.startswith("task_")
        or event_type in SUPERVISION_EVENT_TYPES
        or event_type == "workspace_missing"
    ):
        refs.add("main")
    return refs


def conversation_events_page(
    root: Path,
    run_id: str,
    task_id: str,
    target: str,
    limit: int = 50,
    before: int | None = None,
    categories: set[str] | None = None,
) -> dict:
    path = event_path(root, run_id)
    after_offset = path.stat().st_size if path.exists() else 0
    end_offset = after_offset if before is None else max(0, min(before, after_offset))
    safe_limit = max(1, min(limit, 200))
    allowed_categories = categories
    matches: list[dict] = []
    for offset, event in iter_jsonl_reverse(path, before=end_offset) or ():
        event_type = str(event.get("type") or "")
        if allowed_categories is not None and conversation_event_category(event_type) not in allowed_categories:
            continue
        if (
            event_type in TIMELINE_EVENT_TYPES
            and event_task_id(event) == task_id
            and (target or "main") in event_agent_refs(event)
        ):
            item = dict(event)
            item["_cursor"] = offset
            matches.append(item)
            if len(matches) > safe_limit:
                break

    has_more = len(matches) > safe_limit
    page = list(reversed(matches[:safe_limit]))
    next_before_offset = page[0].get("_cursor") if has_more and page else None
    return {
        "events": page,
        "before_offset": end_offset,
        "after_offset": after_offset,
        "next_before_offset": next_before_offset,
        "before": next_before_offset,
        "has_more": has_more,
        "count": len(page),
    }


def create_plan(
    root: Path,
    goal: str,
    agents: int,
    mode: str,
    task_titles: list[str],
    write_scopes: list[str],
    backend: str = "codex",
    model: str | None = None,
    workspace_path: str | None = None,
    workspace_id: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    proxy_enabled: bool = False,
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
) -> dict:
    run_id = new_run_id()
    titles = task_titles or default_tasks(goal, agents, mode)
    created = utc_now()
    http_proxy = normalize_proxy_value(http_proxy)
    https_proxy = normalize_proxy_value(https_proxy)
    no_proxy = normalize_proxy_value(no_proxy) or (DEFAULT_NO_PROXY if (http_proxy or https_proxy) else None)
    proxy_enabled = bool(proxy_enabled or http_proxy or https_proxy)
    tasks = [
        make_task(
            f"task-{idx:03d}",
            title,
            created,
            backend,
            model=model,
            workspace_path=workspace_path or str(root),
            workspace_id=workspace_id,
            sandbox=sandbox,
            approval=approval,
            proxy_enabled=proxy_enabled,
            http_proxy=http_proxy,
            https_proxy=https_proxy,
            no_proxy=no_proxy,
        )
        for idx, title in enumerate(titles, start=1)
    ]
    plan = {
        "id": run_id,
        "goal": goal,
        "mode": mode,
        "created_at": created,
        "updated_at": created,
        "write_scopes": write_scopes,
        "main_agent": make_agent(
            "main",
            "run-main",
            backend,
            status="active",
            workspace_path=workspace_path,
            sandbox=sandbox,
            approval=approval,
            proxy_enabled=proxy_enabled,
        ),
        "tasks": tasks,
    }
    base = run_dir(root, run_id)
    for task in tasks:
        write_task_artifacts(root, plan, task)
        ensure_session(root, run_id, task["id"], "main", backend, model=model, workspace_path=task.get("workspace_path"))
    ensure_session(root, run_id, None, "main", backend, model=model, workspace_path=workspace_path or str(root))
    save_plan(root, plan)
    append_event(root, run_id, "plan_created", {"goal": goal, "mode": mode, "tasks": len(tasks)})
    return plan


def write_task_artifacts(root: Path, plan: dict, task: dict) -> None:
    base = run_dir(root, plan["id"])
    prompt_file = base / task["prompt_file"]
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(task_prompt(plan["goal"], plan["mode"], task, plan.get("write_scopes", [])), encoding="utf-8")
    inbox_file = base / task["inbox_file"]
    inbox_file.parent.mkdir(parents=True, exist_ok=True)
    inbox_file.touch()
    task_dir = base / "tasks" / task["id"]
    task_dir.mkdir(parents=True, exist_ok=True)
    write_json(task_dir / "task.json", task)
    _ensure_task_round_record(root, plan["id"], task)
    (task_dir / "messages.jsonl").touch()


def add_task(
    root: Path,
    run_id: str,
    title: str,
    backend: str = "codex",
    sub_agents: int = 0,
    model: str | None = None,
    workspace_path: str | None = None,
    workspace_id: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    proxy_enabled: bool = False,
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
    delegation_policy: str = "auto",
    max_sub_agents: int = 3,
    preferred_sub_backend: str | None = None,
    preferred_sub_model: str | None = None,
    description: str | None = None,
    supervision: dict | None = None,
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        http_proxy = normalize_proxy_value(http_proxy)
        https_proxy = normalize_proxy_value(https_proxy)
        no_proxy = normalize_proxy_value(no_proxy) or (DEFAULT_NO_PROXY if (http_proxy or https_proxy) else None)
        proxy_enabled = bool(proxy_enabled or http_proxy or https_proxy)
        task = make_task(
            next_task_id(plan["tasks"]),
            title,
            utc_now(),
            backend,
            model=model,
            workspace_path=workspace_path or str(root),
            workspace_id=workspace_id,
            sandbox=sandbox,
            approval=approval,
            proxy_enabled=proxy_enabled,
            http_proxy=http_proxy,
            https_proxy=https_proxy,
            no_proxy=no_proxy,
            delegation_policy=delegation_policy,
            max_sub_agents=max_sub_agents,
            preferred_sub_backend=preferred_sub_backend,
            preferred_sub_model=preferred_sub_model,
            description=description,
            supervision=supervision,
        )
        for _ in range(max(0, sub_agents)):
            add_agent_to_task_dict(
                task,
                preferred_sub_backend or backend,
                model=preferred_sub_model if preferred_sub_model is not None else model,
                workspace_path=workspace_path or str(root),
                sandbox=sandbox,
                approval=approval,
                proxy_enabled=proxy_enabled,
                created_by="system",
                created_reason="task creation requested initial sub-agent",
            )
        plan["tasks"].append(task)
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_task_artifacts(root, plan, task)
        ensure_session(root, run_id, task["id"], "main", backend, model=model, workspace_path=task.get("workspace_path"))
        for agent in task.get("agents", []):
            ensure_session(
                root,
                run_id,
                task["id"],
                agent["id"],
                agent.get("backend", backend),
                model=agent.get("model"),
                workspace_path=agent.get("workspace_path") or task.get("workspace_path"),
            )
    append_event(
        root,
        run_id,
        "task_created",
        {
            "task_id": task["id"],
            "title": title,
            "backend": backend,
            "model": model,
            "sandbox": sandbox,
            "approval": approval,
            "proxy_enabled": proxy_enabled,
            "proxy_configured": task_has_proxy_config(task),
            "workspace_id": task.get("workspace_id"),
            "workspace_path": task.get("workspace_path"),
            "delegation_policy": delegation_policy,
            "max_sub_agents": max_sub_agents,
            "supervision": task.get("supervision"),
        },
    )
    return task


def ensure_task_supervision_host_agent(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    backend: str | None = None,
) -> dict:
    return _ensure_task_supervision_host_agent(
        root,
        run_id,
        task_id,
        backend=backend,
        now_func=utc_now,
        append_event_func=append_event,
        ensure_session_func=ensure_session,
    )


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
) -> dict:
    return _add_agent(
        root,
        run_id,
        task_id,
        backend=backend,
        role=role,
        model=model,
        sandbox=sandbox,
        approval=approval,
        proxy_enabled=proxy_enabled,
        created_by=created_by,
        created_reason=created_reason,
        now_func=utc_now,
        append_event_func=append_event,
        ensure_session_func=ensure_session,
    )


def update_agent_config(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    sandbox: str | None = None,
    approval: str | None = None,
    proxy_enabled: bool | None = None,
) -> dict:
    return _update_agent_config(
        root,
        run_id,
        task_id,
        agent_id,
        sandbox=sandbox,
        approval=approval,
        proxy_enabled=proxy_enabled,
        now_func=utc_now,
        append_event_func=append_event,
    )


def update_task_proxy_config(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    proxy_enabled: object = UNSET,
    http_proxy: object = UNSET,
    https_proxy: object = UNSET,
    no_proxy: object = UNSET,
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        for agent in task.get("agents", []):
            if "proxy_enabled" not in agent:
                agent["proxy_enabled"] = bool(task.get("preferred_proxy_enabled"))
        if proxy_enabled is not UNSET:
            task["preferred_proxy_enabled"] = bool(proxy_enabled)
        if http_proxy is not UNSET:
            task["preferred_http_proxy"] = normalize_proxy_value(http_proxy)
        if https_proxy is not UNSET:
            task["preferred_https_proxy"] = normalize_proxy_value(https_proxy)
        if no_proxy is not UNSET:
            task["preferred_no_proxy"] = normalize_proxy_value(no_proxy)
        if (
            not task.get("preferred_no_proxy")
            and (task.get("preferred_http_proxy") or task.get("preferred_https_proxy"))
        ):
            task["preferred_no_proxy"] = DEFAULT_NO_PROXY
        if proxy_enabled is UNSET and (http_proxy is not UNSET or https_proxy is not UNSET):
            task["preferred_proxy_enabled"] = bool(task.get("preferred_http_proxy") or task.get("preferred_https_proxy"))
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event(
        root,
        run_id,
        "task_proxy_config_updated",
        {
            "task_id": task_id,
            "proxy_enabled": task.get("preferred_proxy_enabled"),
            "http_proxy_configured": bool(task.get("preferred_http_proxy")),
            "https_proxy_configured": bool(task.get("preferred_https_proxy")),
            "no_proxy_configured": bool(task.get("preferred_no_proxy")),
        },
    )
    return task


def update_task_supervision_config(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    mode: object = UNSET,
    host_backend: object = UNSET,
    real_agent_enabled: object = UNSET,
    max_rounds: object = UNSET,
) -> dict:
    should_ensure_host = False
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        supervision = normalize_task_supervision(task.get("supervision"))
        if mode is not UNSET:
            supervision["mode"] = str(mode or "").strip().lower()
        if host_backend is not UNSET:
            supervision["host_backend"] = str(host_backend or "").strip().lower()
        if real_agent_enabled is not UNSET:
            supervision["real_agent_enabled"] = bool(real_agent_enabled)
        elif supervision.get("mode") == "assisted" and supervision.get("host_backend") != "stub":
            supervision["real_agent_enabled"] = True
        if max_rounds is not UNSET:
            supervision["max_rounds"] = max_rounds
        task["supervision"] = normalize_task_supervision(supervision)
        should_ensure_host = bool(
            task["supervision"].get("mode") == "assisted"
            and task["supervision"].get("real_agent_enabled")
            and task["supervision"].get("host_backend") != "stub"
            and not task["supervision"].get("host_agent_id")
        )
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    if should_ensure_host:
        task = ensure_task_supervision_host_agent(
            root,
            run_id,
            task_id,
            backend=str(task["supervision"].get("host_backend") or "codex"),
        )["task"]
    append_event(
        root,
        run_id,
        "task_supervision_config_updated",
        {
            "task_id": task_id,
            "mode": task["supervision"].get("mode"),
            "host_backend": task["supervision"].get("host_backend"),
            "host_agent_id": task["supervision"].get("host_agent_id"),
            "real_agent_enabled": task["supervision"].get("real_agent_enabled"),
            "max_rounds": task["supervision"].get("max_rounds"),
        },
    )
    return task


def set_agent_status(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    status: str,
    exit_code: int | None = None,
) -> dict:
    return _set_agent_status(
        root,
        run_id,
        task_id,
        agent_id,
        status,
        exit_code,
        now_func=utc_now,
        append_event_func=append_event,
    )


def update_agent_runtime(root: Path, run_id: str, task_id: str, agent_id: str, **fields: object) -> dict:
    return _update_agent_runtime(
        root,
        run_id,
        task_id,
        agent_id,
        now_func=utc_now,
        append_event_func=append_event,
        **fields,
    )


def mark_task_coordination(root: Path, run_id: str, task_id: str, **fields: object) -> dict:
    return _mark_task_coordination(root, run_id, task_id, now_func=utc_now, append_event_func=append_event, **fields)


def _start_reopen_round_if_needed(root: Path, run_id: str, task_id: str, started_at: str) -> dict | None:
    new_round: dict | None = None
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        previous = _ensure_task_round_record(root, run_id, task)
        if previous.get("status") != "finalized":
            save_plan(root, plan)
            write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
            return None
        previous_sequence = int(previous.get("sequence") or _round_sequence_from_id(previous.get("round_id")) or 1)
        sequence = max(int(task.get("round_sequence") or 1), previous_sequence) + 1
        new_round = make_task_round(
            task_id,
            sequence,
            started_at,
            reopened_from_round_id=str(previous.get("round_id") or task.get("current_round_id")),
        )
        task["current_round_id"] = new_round["round_id"]
        task["round_sequence"] = sequence
        plan["updated_at"] = started_at
        write_json(task_lifecycle_round_path(root, run_id, task_id, new_round["round_id"]), new_round)
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    if new_round:
        append_event(
            root,
            run_id,
            "task_round_started",
            {
                "task_id": task_id,
                "round_id": new_round["round_id"],
                "reopened_from_round_id": new_round.get("reopened_from_round_id"),
            },
        )
    return new_round


def reopen_task(root: Path, run_id: str, task_id: str) -> dict:
    now = utc_now()
    task = set_task_status(root, run_id, task_id, "awaiting_user", allow_terminal_transition=True)
    _start_reopen_round_if_needed(root, run_id, task_id, now)
    task = mark_task_coordination(
        root,
        run_id,
        task_id,
        final_summary_requested_at="",
        final_summary_completed_at="",
        round_summary_requested_at="",
        round_summary_completed_at="",
        followup_started_at=now,
        reopened_at=now,
    )
    render_task_overview_result(root, run_id, task_id, policy="journal", force=True)
    append_event(root, run_id, "task_reopened", {"task_id": task_id, "round_id": task.get("current_round_id")})
    return task


def complete_task(root: Path, run_id: str, task_id: str, exit_code: int | None = 0) -> dict:
    now = utc_now()
    task = set_task_status(root, run_id, task_id, "completed", exit_code)
    task = mark_task_coordination(root, run_id, task_id, completion_marked_at=now)
    append_event(root, run_id, "task_completed", {"task_id": task_id, "exit_code": exit_code})
    return task


def set_task_hidden(root: Path, run_id: str, task_id: str, hidden: bool) -> dict:
    return _set_task_hidden(root, run_id, task_id, hidden, now_func=utc_now, append_event_func=append_event)


def delete_task(root: Path, run_id: str, task_id: str) -> dict:
    return _delete_task(root, run_id, task_id, now_func=utc_now, append_event_func=append_event)


def set_task_status(
    root: Path,
    run_id: str,
    task_id: str,
    status: str,
    exit_code: int | None = None,
    *,
    allow_terminal_transition: bool = False,
) -> dict:
    return _set_task_status(
        root,
        run_id,
        task_id,
        status,
        exit_code,
        allow_terminal_transition=allow_terminal_transition,
        now_func=utc_now,
        append_event_func=append_event,
        render_overview_func=render_task_overview_result_if_needed,
    )


def write_task_result(root: Path, run_id: str, task_id: str, content: str, policy: str = "finalize") -> Path:
    now = utc_now()
    body = content.rstrip() + "\n"
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        path = run_dir(root, run_id) / task["output_file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        meta = {"task_id": task_id, "policy": policy, "updated_at": now}
        if policy == "finalize":
            round_record = _ensure_task_round_record(root, run_id, task)
            round_id = str(round_record["round_id"])
            final_path = task_round_final_path(root, run_id, task_id, round_id)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_text(body, encoding="utf-8")
            final_meta_path = task_round_final_meta_path(root, run_id, task_id, round_id)
            meta |= {
                "round_id": round_id,
                "round_sequence": round_record.get("sequence"),
                "final_path": _run_relative_path(root, run_id, final_path),
            }
            write_json(final_meta_path, meta)
            round_record["status"] = "finalized"
            round_record["finalized_at"] = now
            round_record["final_path"] = _run_relative_path(root, run_id, final_path)
            round_record["final_meta_path"] = _run_relative_path(root, run_id, final_meta_path)
            write_json(task_lifecycle_round_path(root, run_id, task_id, round_id), round_record)
            task["last_final_round_id"] = round_id
            task["last_final_at"] = now
        write_json(path.with_suffix(".meta.json"), meta)
        plan["updated_at"] = now
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event(
        root,
        run_id,
        "task_result_written",
        {"task_id": task_id, "path": str(path), "chars": len(content), "policy": policy, "round_id": meta.get("round_id")},
    )
    if policy == "finalize":
        render_task_overview_result_if_needed(root, run_id, task_id, policy=policy)
    return path


def task_rounds_path(root: Path, run_id: str, task_id: str) -> Path:
    return run_dir(root, run_id) / "tasks" / task_id / "rounds.jsonl"


def list_task_rounds(root: Path, run_id: str, task_id: str) -> list[dict]:
    rounds, _ = iter_jsonl_from(task_rounds_path(root, run_id, task_id), 0)
    return rounds


def _string_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def render_task_rounds_markdown(task: dict, rounds: list[dict]) -> str:
    title = str(task.get("title") or task.get("id") or "Task")
    lines = ["# Final", "", f"Task: {title}", "", "## 任务轮次"]
    if not rounds:
        lines.append("")
        lines.append("_暂无任务轮次记录。_")
        return "\n".join(lines).rstrip() + "\n"
    for index, item in enumerate(rounds, start=1):
        heading = str(item.get("summary") or "").strip() or "(no summary)"
        prefix = str(item.get("round_id") or f"round-{item.get('sequence') or '?'}")
        trigger = str(item.get("trigger") or "manual")
        lines.append("")
        lines.append(f"{index}. **{heading}**")
        lines.append(f"   - 轮次：`{prefix}`")
        lines.append(f"   - 触发：`{trigger}`")
        changed_files = _string_list(item.get("changed_files"))
        verification = _string_list(item.get("verification"))
        risks = _string_list(item.get("risks"))
        agents = _string_list(item.get("agents"))
        if changed_files:
            lines.append(f"   - 文件：{', '.join(changed_files)}")
        if verification:
            lines.append(f"   - 验证：{'; '.join(verification)}")
        if risks:
            lines.append(f"   - 风险：{'; '.join(risks)}")
        if agents:
            lines.append(f"   - Agent：{', '.join(agents)}")
    return "\n".join(lines).rstrip() + "\n"


def _collect_unique_strings(entries: list[dict], key: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        for value in _string_list(entry.get(key)):
            if value not in seen:
                values.append(value)
                seen.add(value)
    return values


def _read_round_final(root: Path, run_id: str, round_record: dict) -> tuple[str, Path | None]:
    final_path = round_record.get("final_path")
    if not final_path:
        return "", None
    path = _resolve_run_path(root, run_id, final_path)
    if not path.exists():
        return "", path
    return path.read_text(encoding="utf-8"), path


def _latest_final_artifact(root: Path, run_id: str, lifecycle_rounds: list[dict]) -> tuple[dict | None, str, dict]:
    finalized_rounds = [item for item in lifecycle_rounds if item.get("status") == "finalized" and item.get("final_path")]
    if not finalized_rounds:
        return None, "", {}
    latest = finalized_rounds[-1]
    final_text, final_file = _read_round_final(root, run_id, latest)
    final_meta: dict = {}
    meta_path = latest.get("final_meta_path")
    if meta_path:
        final_meta_file = _resolve_run_path(root, run_id, meta_path)
        if final_meta_file.exists():
            final_meta = read_json(final_meta_file)
    elif final_file is not None:
        final_meta_file = final_file.with_suffix(".meta.json")
        if final_meta_file.exists():
            final_meta = read_json(final_meta_file)
    return latest, final_text, final_meta


def _task_output_has_overview(root: Path, run_id: str, task: dict) -> bool:
    output_file = run_dir(root, run_id) / task["output_file"]
    output_meta_file = output_file.with_suffix(".meta.json")
    if not output_meta_file.exists():
        return False
    try:
        return read_json(output_meta_file).get("format") == "task_overview"
    except (OSError, ValueError):
        return False


def _should_render_task_overview(
    root: Path,
    run_id: str,
    task: dict,
    lifecycle_rounds: list[dict],
    journal_entries: list[dict],
) -> bool:
    return (
        _task_output_has_overview(root, run_id, task)
        or len(lifecycle_rounds) > 1
        or bool(journal_entries)
        or any(item.get("reopened_from_round_id") for item in lifecycle_rounds)
    )


def _overview_inline_text(value: object) -> str:
    text = " ".join(str(value or "").split())
    for prefix in ("###### ", "##### ", "#### ", "### ", "## ", "# "):
        text = text.replace(prefix, "")
    return text


def _compact_summary(value: object, limit: int = 180) -> str:
    text = _overview_inline_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _entries_by_round(entries: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        round_id = str(entry.get("round_id") or f"round-{entry.get('round_sequence') or '?'}")
        grouped.setdefault(round_id, []).append(entry)
    return grouped


def _round_overview_sentence(round_record: dict, entries: list[dict]) -> str:
    status = str(round_record.get("status") or "unknown")
    if not entries:
        return f"状态 `{status}`。"
    first = _compact_summary(entries[0].get("summary"), 110)
    latest = _compact_summary(entries[-1].get("summary"), 110)
    if len(entries) == 1 or first == latest:
        return first or f"状态 `{status}`。"
    return f"共 {len(entries)} 条进展；起点：{first}；最新：{latest}"


def _append_limited_section(lines: list[str], title: str, items: list[str], empty: str, limit: int = 6) -> None:
    lines.extend(["", f"## {title}"])
    if not items:
        lines.append(f"- {empty}")
        return
    for item in items[:limit]:
        lines.append(f"- {_overview_inline_text(item)}")
    remaining = len(items) - limit
    if remaining > 0:
        lines.append(f"- 另有 {remaining} 项，详见任务 journal。")


def render_task_overview_markdown(
    root: Path,
    run_id: str,
    task: dict,
    lifecycle_rounds: list[dict],
    journal_entries: list[dict],
) -> str:
    title = str(task.get("title") or task.get("id") or "Task")
    task_id = str(task.get("id") or "")
    latest_final, _latest_final_text, _latest_meta = _latest_final_artifact(root, run_id, lifecycle_rounds)
    verification = _collect_unique_strings(journal_entries, "verification")
    risks = _collect_unique_strings(journal_entries, "risks")
    grouped_entries = _entries_by_round(journal_entries)

    lines = [
        "# Task Overview",
        "",
        f"Task: {title}",
        f"Task ID: `{task_id}`",
        f"Status: `{task.get('status') or 'unknown'}`",
        f"Current round: `{task.get('current_round_id') or '-'}`",
    ]
    if task.get("last_final_round_id"):
        lines.append(f"Last final round: `{task.get('last_final_round_id')}`")
    if task.get("last_final_at"):
        lines.append(f"Last final at: `{task.get('last_final_at')}`")
    if task.get("started_at"):
        lines.append(f"Started at: `{task.get('started_at')}`")
    if task.get("finished_at"):
        lines.append(f"Finished at: `{task.get('finished_at')}`")

    lines.extend(["", "## 任务轮次"])
    if lifecycle_rounds:
        for index, item in enumerate(lifecycle_rounds, start=1):
            round_id = str(item.get("round_id") or f"round-{item.get('sequence') or '?'}")
            summary = _round_overview_sentence(item, grouped_entries.get(round_id, []))
            lines.extend(["", f"{index}. `{round_id}` {summary}"])
            lines.append(f"   - 状态：`{item.get('status') or 'unknown'}`")
            if item.get("started_at"):
                lines.append(f"   - 开始：`{item.get('started_at')}`")
            if item.get("finalized_at"):
                lines.append(f"   - Final：`{item.get('finalized_at')}`")
            if item.get("reopened_from_round_id"):
                lines.append(f"   - Reopened from：`{item.get('reopened_from_round_id')}`")
    elif journal_entries:
        for index, entry in enumerate(journal_entries, start=1):
            round_id = str(entry.get("round_id") or f"round-{entry.get('round_sequence') or '?'}")
            summary = _compact_summary(entry.get("summary")) or "(no summary)"
            lines.extend(["", f"{index}. `{round_id}` {summary}"])
    else:
        lines.extend(["", "_暂无任务轮次记录。_"])

    lines.extend(["", "## 结果"])
    lines.append(f"- 当前状态：`{task.get('status') or 'unknown'}`")
    lines.append(f"- 当前轮次：`{task.get('current_round_id') or '-'}`")
    if journal_entries:
        lines.append(f"- Journal 记录：{len(journal_entries)} 条。")
    if lifecycle_rounds:
        finalized_count = sum(1 for item in lifecycle_rounds if item.get("status") == "finalized")
        lines.append(f"- Lifecycle round：{len(lifecycle_rounds)} 轮，其中 {finalized_count} 轮已有 Final 快照。")
    if latest_final:
        lines.append(f"- 最新 raw Final：`{latest_final.get('round_id')}`。")
    elif not journal_entries:
        lines.append("- 尚无 Final。")
    if journal_entries:
        latest_summaries = [_compact_summary(item.get("summary"), 120) for item in journal_entries[-2:]]
        latest_summaries = [item for item in latest_summaries if item]
        if latest_summaries:
            lines.append("- 最新进展：" + "；".join(latest_summaries))

    _append_limited_section(lines, "验证", verification, "暂无明确验证记录。")
    _append_limited_section(lines, "剩余风险", risks, "暂无明确剩余风险。")

    lines.extend(["", "## 详细快照索引"])
    finalized = [item for item in lifecycle_rounds if item.get("status") == "finalized" and item.get("final_path")]
    if not finalized:
        lines.extend(["", "_暂无 Final 快照。_"])
    for item in finalized:
        round_id = str(item.get("round_id") or f"round-{item.get('sequence') or '?'}")
        lines.extend(["", f"### `{round_id}`"])
        _final_text, final_path = _read_round_final(root, run_id, item)
        if final_path is not None:
            lines.append(f"- Raw final: `{_run_relative_path(root, run_id, final_path)}`")
        if item.get("finalized_at"):
            lines.append(f"- Finalized at: `{item.get('finalized_at')}`")
        round_entries = grouped_entries.get(round_id, [])
        if round_entries:
            lines.append(f"- Journal entries: {len(round_entries)}")

    return "\n".join(lines).rstrip() + "\n"


def render_task_overview_result(root: Path, run_id: str, task_id: str, policy: str = "journal", force: bool = False) -> Path:
    _plan, task, run = task_lookup(root, run_id, task_id)
    lifecycle_rounds = list_task_lifecycle_rounds(root, run_id, task_id)
    journal_entries = list_task_rounds(root, run_id, task_id)
    path = run / task["output_file"]
    if not force and not _should_render_task_overview(root, run_id, task, lifecycle_rounds, journal_entries):
        return path
    content = render_task_overview_markdown(root, run_id, task, lifecycle_rounds, journal_entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    write_json(
        path.with_suffix(".meta.json"),
        {
            "task_id": task_id,
            "policy": policy,
            "format": "task_overview",
            "updated_at": utc_now(),
            "round_count": len(lifecycle_rounds),
            "journal_count": len(journal_entries),
            "current_round_id": task.get("current_round_id"),
            "last_final_round_id": task.get("last_final_round_id"),
        },
    )
    append_event(
        root,
        run_id,
        "task_journal_rendered",
        {"task_id": task_id, "path": str(path), "round_count": len(journal_entries), "format": "task_overview"},
    )
    return path


def render_task_overview_result_if_needed(root: Path, run_id: str, task_id: str, policy: str = "journal") -> Path:
    return render_task_overview_result(root, run_id, task_id, policy=policy, force=False)


def render_task_journal_result(root: Path, run_id: str, task_id: str) -> Path:
    return render_task_overview_result(root, run_id, task_id, policy="journal", force=True)


def append_task_round(root: Path, run_id: str, task_id: str, entry: dict) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise KeyError(task_id)
        lifecycle_round = _ensure_task_round_record(root, run_id, task)
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    rounds = list_task_rounds(root, run_id, task_id)
    journal_sequence = len(rounds) + 1
    round_id = str(entry.get("round_id") or lifecycle_round.get("round_id") or task.get("current_round_id") or "round-001")
    round_sequence = int(_round_sequence_from_id(round_id) or lifecycle_round.get("sequence") or 1)
    payload = {
        "task_id": task_id,
        "round_id": round_id,
        "round_sequence": round_sequence,
        "sequence": round_sequence,
        "journal_id": str(entry.get("journal_id") or f"journal-{journal_sequence:03d}"),
        "journal_sequence": journal_sequence,
        "at": str(entry.get("at") or utc_now()),
        "trigger": str(entry.get("trigger") or "manual"),
        "summary": str(entry.get("summary") or "").strip(),
        "changed_files": _string_list(entry.get("changed_files")),
        "verification": _string_list(entry.get("verification")),
        "risks": _string_list(entry.get("risks")),
        "agents": _string_list(entry.get("agents")),
    }
    if not payload["summary"]:
        raise ValueError("Task round summary is required")
    append_jsonl(task_rounds_path(root, run_id, task_id), payload)
    render_task_journal_result(root, run_id, task_id)
    append_event(
        root,
        run_id,
        "task_round_recorded",
        {
            "task_id": task_id,
            "round_id": payload["round_id"],
            "journal_id": payload["journal_id"],
            "trigger": payload["trigger"],
            "chars": len(payload["summary"]),
        },
    )
    return payload


def status_snapshot(root: Path, run_id: str) -> dict:
    plan = require_plan(root, run_id)
    def with_session(task: dict, agent: dict) -> dict:
        session = ensure_session(
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

    return {
        "run_id": run_id,
        "goal": plan["goal"],
        "mode": plan["mode"],
        "updated_at": plan["updated_at"],
        "aha_root": str(root),
        "main_agent": plan.get("main_agent"),
        "tasks": [
            {
                "id": task["id"],
                "title": task["title"],
                "description": task.get("description", ""),
                "workspace_path": task.get("workspace_path"),
                "preferred_backend": task.get("preferred_backend"),
                "preferred_model": task.get("preferred_model"),
                "preferred_sandbox": task.get("preferred_sandbox"),
                "preferred_approval": task.get("preferred_approval"),
                "preferred_proxy_enabled": bool(task.get("preferred_proxy_enabled")),
                "preferred_http_proxy": task.get("preferred_http_proxy"),
                "preferred_https_proxy": task.get("preferred_https_proxy"),
                "preferred_no_proxy": task.get("preferred_no_proxy"),
                "delegation_policy": task.get("delegation_policy", "auto"),
                "max_sub_agents": task.get("max_sub_agents", 3),
                "supervision": normalize_task_supervision(task.get("supervision")),
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
            for task in plan["tasks"]
            if not task.get("deleted_at")
        ],
    }


def status_snapshot_projection(root: Path, run_id: str, *, lite: bool = False, selected_task_id: str | None = None) -> dict:
    snapshot_event_id = event_stream_position(root, run_id)
    snapshot = status_snapshot(root, run_id)
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
        latest_final, latest_final_text, latest_final_meta = _latest_final_artifact(root, run_id, lifecycle_rounds)
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


def task_log_page(root: Path, run_id: str, task_id: str, limit: int = 200, before: int | None = None, source: str = "auto") -> dict:
    _plan, task, run = task_lookup(root, run_id, task_id)
    log_file = run / task["log_file"]
    selected_source = source if source in {"auto", "file", "events"} else "auto"
    if selected_source == "events" or (selected_source == "auto" and (not log_file.exists() or log_file.stat().st_size == 0)):
        return {"task_id": task_id, **task_event_log_page(root, run_id, task_id, limit=limit, before=before)}
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
    _latest_final, latest_final_text, latest_final_meta = _latest_final_artifact(root, run_id, lifecycle_rounds)
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
