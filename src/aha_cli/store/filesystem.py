from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import (
    DEFAULT_TASK_SANDBOX,
    default_tasks,
    make_agent,
    make_task,
    make_task_round,
    next_task_id,
    normalize_task_context_management,
    normalize_task_hardware_debug,
    normalize_task_skills,
    normalize_task_supervision,
    task_metadata_projection,
    task_prompt,
    utc_now,
    new_run_id,
)
from aha_cli.services.proxy import backend_has_proxy_config, backend_proxy_config, normalize_proxy_config, normalize_proxy_value, run_has_proxy_config, run_proxy_config
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
    update_run_lifecycle,
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


def _model_value(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def append_event(root: Path, run_id: str, event_type: str, data: dict) -> dict:
    event = _append_event(root, run_id, event_type, data, ts=utc_now())
    try:
        from aha_cli.services.weixin_notifications import notify_event

        notify_event(root, run_id, event)
    except Exception as exc:  # pragma: no cover - notification failures must not break core state writes.
        _append_event(
            root,
            run_id,
            "weixin_notification_failed",
            {
                "source_event_type": event_type,
                "source_event_id": event.get("event_id"),
                "error": str(exc),
            },
            ts=utc_now(),
        )
    return event


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
    final_context: dict | None = None,
    memo_report_context: dict | None = None,
    recovery_context: str | None = None,
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
    if final_context:
        payload["final_context"] = final_context
    if memo_report_context:
        payload["memo_report_context"] = memo_report_context
    if recovery_context:
        payload["recovery_context"] = recovery_context
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
    proxy_enabled: bool | None = None,
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
    collaboration_mode: str | None = None,
    workflow_template: str | None = None,
    create_default_tasks: bool = True,
) -> dict:
    run_id = new_run_id()
    titles = task_titles or (default_tasks(goal, agents, mode) if create_default_tasks else [])
    created = utc_now()
    cfg = load_config(root)
    backend_proxy = backend_proxy_config(cfg, backend)
    proxy_fields_provided = any(value is not None for value in (http_proxy, https_proxy, no_proxy))
    if proxy_enabled is None:
        proxy_enabled = bool(backend_proxy.get("enabled") if not proxy_fields_provided else normalize_proxy_value(http_proxy) or normalize_proxy_value(https_proxy))
    proxy_config = normalize_proxy_config(
        proxy_enabled,
        http_proxy,
        https_proxy,
        no_proxy,
    )
    proxy_enabled = bool(proxy_config["enabled"])
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
            collaboration_mode=collaboration_mode,
            workflow_template=workflow_template,
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
        "proxy": proxy_config,
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
    append_event(
        root,
        run_id,
        "plan_created",
        {"goal": goal, "mode": mode, "tasks": len(tasks), "proxy_enabled": proxy_enabled, "proxy_configured": backend_has_proxy_config(cfg, backend, plan)},
    )
    return plan


def rename_run(root: Path, run_id: str, name: str) -> dict:
    run_name = str(name or "").strip()
    if not run_name:
        raise ValueError("run name cannot be empty")
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        plan["goal"] = run_name
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        append_event(root, run_id, "run_renamed", {"name": run_name})
        return run_summary_from_plan(root, plan)


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
    proxy_enabled: bool | None = None,
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
    collaboration_mode: str | None = None,
    workflow_template: str | None = None,
    delegation_policy: str | None = "auto",
    max_sub_agents: int | None = 3,
    preferred_sub_backend: str | None = None,
    preferred_sub_model: str | None = None,
    description: str | None = None,
    supervision: dict | None = None,
    context_management: dict | None = None,
    task_skills: dict | None = None,
    hardware_debug: dict | None = None,
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        proxy_fields_provided = any(value is not None for value in (http_proxy, https_proxy, no_proxy))
        if proxy_fields_provided:
            current_proxy = run_proxy_config(plan)
            plan["proxy"] = normalize_proxy_config(
                proxy_enabled if proxy_enabled is not None else bool(http_proxy or https_proxy or current_proxy["enabled"]),
                http_proxy if http_proxy is not None else current_proxy["http_proxy"],
                https_proxy if https_proxy is not None else current_proxy["https_proxy"],
                no_proxy if no_proxy is not None else current_proxy["no_proxy"],
            )
        backend_proxy = backend_proxy_config(load_config(root), backend, plan)
        task_proxy_enabled = bool(backend_proxy["enabled"] if proxy_enabled is None else proxy_enabled)
        sandbox = sandbox or DEFAULT_TASK_SANDBOX
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
            proxy_enabled=task_proxy_enabled,
            collaboration_mode=collaboration_mode,
            workflow_template=workflow_template,
            delegation_policy=delegation_policy,
            max_sub_agents=max_sub_agents,
            preferred_sub_backend=preferred_sub_backend,
            preferred_sub_model=preferred_sub_model,
            description=description,
            supervision=supervision,
            context_management=context_management,
            task_skills=task_skills,
            hardware_debug=hardware_debug,
        )
        for _ in range(max(0, sub_agents)):
            add_agent_to_task_dict(
                task,
                preferred_sub_backend or backend,
                model=preferred_sub_model if preferred_sub_model is not None else model,
                workspace_path=workspace_path or str(root),
                sandbox=sandbox,
                approval=approval,
                proxy_enabled=task_proxy_enabled,
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
    event_data = {
        "task_id": task["id"],
        "title": title,
        "backend": backend,
        "model": model,
        "sandbox": sandbox,
        "approval": approval,
        "proxy_enabled": task_proxy_enabled,
        "proxy_configured": backend_has_proxy_config(load_config(root), task.get("preferred_backend"), plan, task),
    }
    event_data.update(task_metadata_projection(task))
    append_event(root, run_id, "task_created", event_data)
    return task


def ensure_task_supervision_host_agent(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    backend: str | None = None,
    model: object = UNSET,
    proxy_enabled: object = UNSET,
) -> dict:
    return _ensure_task_supervision_host_agent(
        root,
        run_id,
        task_id,
        backend=backend,
        model=model,
        proxy_enabled=proxy_enabled,
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


def _apply_run_proxy_update(
    plan: dict,
    *,
    proxy_enabled: object = UNSET,
    http_proxy: object = UNSET,
    https_proxy: object = UNSET,
    no_proxy: object = UNSET,
) -> dict[str, object]:
    current = run_proxy_config(plan)
    next_http_proxy = current["http_proxy"] if http_proxy is UNSET else normalize_proxy_value(http_proxy)
    next_https_proxy = current["https_proxy"] if https_proxy is UNSET else normalize_proxy_value(https_proxy)
    next_no_proxy = current["no_proxy"] if no_proxy is UNSET else normalize_proxy_value(no_proxy)
    if proxy_enabled is UNSET:
        next_proxy_enabled = current["enabled"]
        if http_proxy is not UNSET or https_proxy is not UNSET:
            next_proxy_enabled = bool(next_http_proxy or next_https_proxy)
    else:
        next_proxy_enabled = bool(proxy_enabled)
    plan["proxy"] = normalize_proxy_config(next_proxy_enabled, next_http_proxy, next_https_proxy, next_no_proxy)
    return plan["proxy"]


def update_run_proxy_config(
    root: Path,
    run_id: str,
    *,
    proxy_enabled: object = UNSET,
    http_proxy: object = UNSET,
    https_proxy: object = UNSET,
    no_proxy: object = UNSET,
) -> dict[str, object]:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        proxy_config = _apply_run_proxy_update(
            plan,
            proxy_enabled=proxy_enabled,
            http_proxy=http_proxy,
            https_proxy=https_proxy,
            no_proxy=no_proxy,
        )
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
    append_event(
        root,
        run_id,
        "run_proxy_config_updated",
        {
            "proxy_enabled": proxy_config.get("enabled"),
            "http_proxy_configured": bool(proxy_config.get("http_proxy")),
            "https_proxy_configured": bool(proxy_config.get("https_proxy")),
            "no_proxy_configured": bool(proxy_config.get("no_proxy")),
        },
    )
    return proxy_config


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
        proxy_fields_provided = http_proxy is not UNSET or https_proxy is not UNSET or no_proxy is not UNSET
        if proxy_fields_provided:
            _apply_run_proxy_update(
                plan,
                proxy_enabled=proxy_enabled,
                http_proxy=http_proxy,
                https_proxy=https_proxy,
                no_proxy=no_proxy,
            )
        for agent in task.get("agents", []):
            if "proxy_enabled" not in agent:
                agent["proxy_enabled"] = bool(task.get("preferred_proxy_enabled"))
        if proxy_enabled is not UNSET:
            task["preferred_proxy_enabled"] = bool(proxy_enabled)
        if proxy_enabled is UNSET and (http_proxy is not UNSET or https_proxy is not UNSET):
            task["preferred_proxy_enabled"] = bool(run_proxy_config(plan).get("enabled"))
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
        proxy_config = run_proxy_config(plan, task)
    append_event(
        root,
        run_id,
        "task_proxy_config_updated",
        {
            "task_id": task_id,
            "proxy_enabled": task.get("preferred_proxy_enabled"),
            "http_proxy_configured": bool(proxy_config.get("http_proxy")),
            "https_proxy_configured": bool(proxy_config.get("https_proxy")),
            "no_proxy_configured": bool(proxy_config.get("no_proxy")),
        },
    )
    if proxy_fields_provided:
        append_event(
            root,
            run_id,
            "run_proxy_config_updated",
            {
                "proxy_enabled": proxy_config.get("enabled"),
                "http_proxy_configured": bool(proxy_config.get("http_proxy")),
                "https_proxy_configured": bool(proxy_config.get("https_proxy")),
                "no_proxy_configured": bool(proxy_config.get("no_proxy")),
                "source": "task_proxy_compat",
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
    host_model: object = UNSET,
    host_proxy_enabled: object = UNSET,
    real_agent_enabled: object = UNSET,
    max_rounds: object = UNSET,
    ask_user_gates: object = UNSET,
) -> dict:
    should_ensure_host = False
    cleared_main_host_wait = False
    cleared_at = ""
    host_agent_id = "host"
    previous_host_backend = ""
    previous_host_model: str | None = None
    previous_host_proxy_enabled = False
    desired_host_backend = ""
    desired_host_model: str | None = None
    desired_host_proxy_enabled = False
    host_agent_exists = False
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        raw_supervision = task.get("supervision") if isinstance(task.get("supervision"), dict) else {}
        host_model_configured = "host_model" in raw_supervision or "model" in raw_supervision
        host_proxy_configured = "host_proxy_enabled" in raw_supervision or "proxy_enabled" in raw_supervision
        supervision = normalize_task_supervision(raw_supervision)
        previous_supervision_host_backend = str(supervision.get("host_backend") or "")
        host_backend_changed_in_payload = False
        if mode is not UNSET:
            supervision["mode"] = str(mode or "").strip().lower()
        if host_backend is not UNSET:
            next_host_backend = str(host_backend or "").strip().lower()
            supervision["host_backend"] = next_host_backend
            host_backend_changed_in_payload = next_host_backend != previous_supervision_host_backend
            if host_model is UNSET and host_backend_changed_in_payload:
                supervision["host_model"] = None
        if host_model is not UNSET:
            supervision["host_model"] = _model_value(host_model)
        if host_proxy_enabled is not UNSET:
            supervision["host_proxy_enabled"] = bool(host_proxy_enabled)
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
        if not should_ensure_host:
            main_agent = next((agent for agent in task.get("agents", []) if agent.get("id") == "main"), None)
            if (
                main_agent
                and main_agent.get("status") == "waiting"
                and str(main_agent.get("waiting_reason") or "").lower() == "host"
            ):
                cleared_at = utc_now()
                main_agent["status"] = "completed"
                main_agent.pop("waiting_reason", None)
                main_agent["last_active_at"] = cleared_at
                main_agent["status_started_at"] = cleared_at
                main_agent["finished_at"] = cleared_at
                main_agent["exit_code"] = None
                if task.get("status") == "running":
                    task["status"] = "awaiting_user"
                    task["started_at"] = task.get("started_at") or cleared_at
                    task["finished_at"] = None
                    task["exit_code"] = None
                cleared_main_host_wait = True
        host_agent_id = str(task["supervision"].get("host_agent_id") or "host")
        desired_host_backend = str(task["supervision"].get("host_backend") or "")
        desired_host_model = _model_value(task["supervision"].get("host_model"))
        desired_host_proxy_enabled = bool(task["supervision"].get("host_proxy_enabled"))
        host_agent = next(
            (
                agent
                for agent in task.get("agents", [])
                if agent.get("id") == host_agent_id or agent.get("id") == "host" or agent.get("role") == "host"
            ),
            None,
        )
        host_agent_exists = host_agent is not None
        previous_host_backend = str((host_agent or {}).get("backend") or "")
        previous_host_model = _model_value((host_agent or {}).get("model"))
        previous_host_proxy_enabled = bool((host_agent or {}).get("proxy_enabled"))
        if host_agent_exists and host_model is UNSET and not host_model_configured and not host_backend_changed_in_payload:
            task["supervision"]["host_model"] = previous_host_model
            desired_host_model = previous_host_model
        if host_agent_exists and host_proxy_enabled is UNSET and not host_proxy_configured:
            task["supervision"]["host_proxy_enabled"] = previous_host_proxy_enabled
            desired_host_proxy_enabled = previous_host_proxy_enabled
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    if should_ensure_host:
        backend_changed = host_agent_exists and previous_host_backend and previous_host_backend != desired_host_backend
        model_changed = host_agent_exists and host_model is not UNSET and previous_host_model != desired_host_model
        proxy_changed = host_agent_exists and host_proxy_enabled is not UNSET and previous_host_proxy_enabled != desired_host_proxy_enabled
        if backend_changed or model_changed:
            from aha_cli.services.agent_backend_switch import switch_agent_backend

            switch_agent_backend(root, run_id, task_id, host_agent_id, backend=desired_host_backend, model=desired_host_model)
            task = task_snapshot(root, run_id, task_id)["task"]
        if proxy_changed:
            update_agent_config(root, run_id, task_id, host_agent_id, proxy_enabled=desired_host_proxy_enabled)
            task = task_snapshot(root, run_id, task_id)["task"]
        if not host_agent_exists:
            task = ensure_task_supervision_host_agent(
                root,
                run_id,
                task_id,
                backend=desired_host_backend or "codex",
                model=desired_host_model,
                proxy_enabled=desired_host_proxy_enabled,
            )["task"]
    append_event(
        root,
        run_id,
        "task_supervision_config_updated",
        {
            "task_id": task_id,
            "mode": task["supervision"].get("mode"),
            "host_backend": task["supervision"].get("host_backend"),
            "host_model": task["supervision"].get("host_model"),
            "host_proxy_enabled": task["supervision"].get("host_proxy_enabled"),
            "host_agent_id": task["supervision"].get("host_agent_id"),
            "real_agent_enabled": task["supervision"].get("real_agent_enabled"),
            "max_rounds": task["supervision"].get("max_rounds"),
            "ask_user_gates": task["supervision"].get("ask_user_gates"),
        },
    )
    if cleared_main_host_wait:
        append_event(
            root,
            run_id,
            "task_supervision_host_wait_cleared",
            {
                "task_id": task_id,
                "reason": "supervision_disabled",
            },
        )
        append_event(
            root,
            run_id,
            "agent_status_changed",
            {
                "task_id": task_id,
                "agent_id": "main",
                "status": "completed",
                "waiting_reason": "",
                "exit_code": None,
                "status_started_at": cleared_at,
            },
        )
        append_event(
            root,
            run_id,
            "task_status_changed",
            {
                "task_id": task_id,
                "status": task.get("status"),
                "exit_code": task.get("exit_code"),
            },
        )
    return task


def update_task_context_management_config(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    auto_compact_enabled: object = UNSET,
    auto_compact_threshold_percent: object = UNSET,
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        context_management = normalize_task_context_management(task.get("context_management"))
        if auto_compact_enabled is not UNSET:
            context_management["auto_compact_enabled"] = bool(auto_compact_enabled)
        if auto_compact_threshold_percent is not UNSET:
            context_management["auto_compact_threshold_percent"] = auto_compact_threshold_percent
        task["context_management"] = normalize_task_context_management(context_management)
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event(
        root,
        run_id,
        "task_context_management_config_updated",
        {
            "task_id": task_id,
            "auto_compact_enabled": task["context_management"].get("auto_compact_enabled"),
            "auto_compact_threshold_percent": task["context_management"].get("auto_compact_threshold_percent"),
        },
    )
    return task


def update_task_hardware_debug_config(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    channels: object = UNSET,
    enabled: object = UNSET,
    devices: object = UNSET,
    operation_skill_path: object = UNSET,
    permissions: object = UNSET,
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        hardware_debug = normalize_task_hardware_debug(task.get("hardware_debug"))
        if channels is not UNSET:
            hardware_debug["channels"] = channels
        elif any(item is not UNSET for item in (devices, operation_skill_path, permissions)):
            existing_uart = next(
                (channel for channel in hardware_debug.get("channels") or [] if isinstance(channel, dict) and channel.get("type") == "uart"),
                {},
            )
            legacy_device = existing_uart.get("settings") if isinstance(existing_uart.get("settings"), dict) else {}
            if devices is not UNSET:
                if isinstance(devices, list) and devices:
                    legacy_device = devices[0]
                elif isinstance(devices, dict):
                    legacy_device = devices
                else:
                    legacy_device = {}
            hardware_debug["channels"] = [
                {
                    "type": "uart",
                    "settings": legacy_device,
                    "operation_skill_path": (
                        operation_skill_path
                        if operation_skill_path is not UNSET
                        else existing_uart.get("operation_skill_path", "")
                    ),
                    "permissions": permissions if permissions is not UNSET else existing_uart.get("permissions", {}),
                }
            ]
        # The master switch only controls visibility/activation; it never discards
        # configured channels so toggling it back on restores the previous setup.
        if enabled is not UNSET:
            hardware_debug["enabled"] = bool(enabled)
        task["hardware_debug"] = normalize_task_hardware_debug(hardware_debug)
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    channels = task["hardware_debug"].get("channels") or []
    append_event(
        root,
        run_id,
        "task_hardware_debug_config_updated",
        {
            "task_id": task_id,
            "channel_count": len(channels),
            "channel_types": [str(channel.get("type") or "") for channel in channels if isinstance(channel, dict)],
        },
    )
    return task


def update_task_skills_config(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    enabled_paths: object = UNSET,
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        task_skills = normalize_task_skills(task.get("task_skills"))
        if enabled_paths is not UNSET:
            task_skills["enabled_paths"] = enabled_paths
        task["task_skills"] = normalize_task_skills(task_skills)
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event(
        root,
        run_id,
        "task_skills_config_updated",
        {
            "task_id": task_id,
            "skill_count": len(task["task_skills"].get("enabled_paths") or []),
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
    waiting_reason: str | None = None,
) -> dict:
    return _set_agent_status(
        root,
        run_id,
        task_id,
        agent_id,
        status,
        exit_code,
        waiting_reason=waiting_reason,
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


def write_task_result(
    root: Path,
    run_id: str,
    task_id: str,
    content: str,
    policy: str = "finalize",
    final_context: dict | None = None,
) -> Path:
    return _write_task_result(
        root,
        run_id,
        task_id,
        content,
        policy,
        final_context=final_context,
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
