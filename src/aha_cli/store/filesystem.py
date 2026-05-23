from __future__ import annotations

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
from aha_cli.store.event_views import (
    conversation_event_category as _conversation_event_category,
    conversation_events_page as _conversation_events_page,
    event_agent_refs as _event_agent_refs,
    event_task_id as _event_task_id,
    format_event_log_line as _format_event_log_line,
    task_event_log_page as _task_event_log_page,
)
from aha_cli.store.finals import write_task_result as _write_task_result
from aha_cli.store.journal import (
    append_task_round as _append_task_round,
    render_task_overview_markdown,
    render_task_overview_result as _render_task_overview_result,
    render_task_rounds_markdown,
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
from aha_cli.store.rounds import (
    ensure_current_task_round as _ensure_current_task_round,
    ensure_task_round_record as _ensure_task_round_record,
    list_task_lifecycle_rounds,
    list_task_rounds,
    round_sequence_from_id as _round_sequence_from_id,
    task_lifecycle_round_path,
)
from aha_cli.store.sessions import (
    ensure_session as _ensure_session,
    list_sessions as _list_sessions,
    save_session as _save_session,
)
from aha_cli.store.snapshots import (
    status_snapshot as _status_snapshot,
    status_snapshot_projection as _status_snapshot_projection,
    task_context_snapshot as _task_context_snapshot,
    task_final_snapshot as _task_final_snapshot,
    task_log_page as _task_log_page,
    task_lookup,
    task_snapshot as _task_snapshot,
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


def ensure_current_task_round(root: Path, run_id: str, task_id: str) -> dict:
    return _ensure_current_task_round(root, run_id, task_id, now_func=utc_now)


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
    return _format_event_log_line(event)


def task_event_log_page(root: Path, run_id: str, task_id: str, limit: int = 200, before: int | None = None) -> dict:
    return _task_event_log_page(root, run_id, task_id, limit=limit, before=before)


def conversation_event_category(event_type: str) -> str:
    return _conversation_event_category(event_type)


def event_task_id(event: dict) -> str | None:
    return _event_task_id(event)


def event_agent_refs(event: dict) -> set[str]:
    return _event_agent_refs(event)


def conversation_events_page(
    root: Path,
    run_id: str,
    task_id: str,
    target: str,
    limit: int = 50,
    before: int | None = None,
    categories: set[str] | None = None,
) -> dict:
    return _conversation_events_page(root, run_id, task_id, target, limit=limit, before=before, categories=categories)


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
    ask_user_gates: object = UNSET,
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
        if ask_user_gates is not UNSET:
            supervision["ask_user_gates"] = ask_user_gates
        task["supervision"] = normalize_task_supervision(supervision)
        should_ensure_host = bool(
            task["supervision"].get("mode") == "assisted"
            and task["supervision"].get("real_agent_enabled")
            and task["supervision"].get("host_backend") != "stub"
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
            "ask_user_gates": task["supervision"].get("ask_user_gates"),
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
    return _write_task_result(
        root,
        run_id,
        task_id,
        content,
        policy,
        now_func=utc_now,
        append_event_func=append_event,
        render_overview_func=render_task_overview_result_if_needed,
    )


def render_task_overview_result(root: Path, run_id: str, task_id: str, policy: str = "journal", force: bool = False) -> Path:
    return _render_task_overview_result(
        root,
        run_id,
        task_id,
        policy=policy,
        force=force,
        now_func=utc_now,
        append_event_func=append_event,
    )


def render_task_overview_result_if_needed(root: Path, run_id: str, task_id: str, policy: str = "journal") -> Path:
    return render_task_overview_result(root, run_id, task_id, policy=policy, force=False)


def render_task_journal_result(root: Path, run_id: str, task_id: str) -> Path:
    return render_task_overview_result(root, run_id, task_id, policy="journal", force=True)


def append_task_round(root: Path, run_id: str, task_id: str, entry: dict) -> dict:
    return _append_task_round(
        root,
        run_id,
        task_id,
        entry,
        now_func=utc_now,
        append_event_func=append_event,
        render_journal_func=render_task_journal_result,
    )


def status_snapshot(root: Path, run_id: str) -> dict:
    return _status_snapshot(root, run_id, ensure_session_func=ensure_session)


def status_snapshot_projection(root: Path, run_id: str, *, lite: bool = False, selected_task_id: str | None = None) -> dict:
    return _status_snapshot_projection(
        root,
        run_id,
        lite=lite,
        selected_task_id=selected_task_id,
        ensure_session_func=ensure_session,
        event_stream_position_func=event_stream_position,
    )


def task_final_snapshot(root: Path, run_id: str, task_id: str) -> dict:
    return _task_final_snapshot(root, run_id, task_id)


def task_context_snapshot(root: Path, run_id: str, task_id: str) -> dict:
    return _task_context_snapshot(root, run_id, task_id)


def task_log_page(root: Path, run_id: str, task_id: str, limit: int = 200, before: int | None = None, source: str = "auto") -> dict:
    return _task_log_page(root, run_id, task_id, limit=limit, before=before, source=source, task_event_log_page_func=task_event_log_page)


def task_snapshot(root: Path, run_id: str, task_id: str) -> dict:
    return _task_snapshot(root, run_id, task_id)
