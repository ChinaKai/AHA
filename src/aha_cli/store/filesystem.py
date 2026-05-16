from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import threading
import uuid

from aha_cli.constants import CONFIG_DIR, CONFIG_FILE, EVENTS_FILE, PLAN_FILE, RUNS_DIR
from aha_cli.domain.models import (
    default_config,
    default_tasks,
    enrich_plan,
    make_agent,
    make_session,
    make_task,
    next_sub_id,
    next_task_id,
    task_prompt,
    utc_now,
    new_run_id,
)

PLAN_LOCK = threading.RLock()
EVENT_LOCK = threading.Lock()


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def append_jsonl(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.write("\n")


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for path in [current, *current.parents]:
        if (path / CONFIG_DIR).is_dir():
            return path
    return current


def config_path(root: Path) -> Path:
    return root / CONFIG_DIR / CONFIG_FILE


def load_config(root: Path) -> dict:
    defaults = default_config()
    path = config_path(root)
    if not path.exists():
        return defaults
    loaded = read_json(path)
    cfg = defaults | {key: value for key, value in loaded.items() if key != "codex"}
    cfg["codex"] = defaults["codex"] | loaded.get("codex", {})
    if cfg.get("runner_command") and cfg.get("backend") == "stub":
        cfg["backend"] = "command"
    return cfg


def run_dir(root: Path, run_id: str) -> Path:
    return root / CONFIG_DIR / RUNS_DIR / run_id


@contextmanager
def locked_plan(root: Path, run_id: str):
    lock_path = run_dir(root, run_id) / "runtime" / "plan.lock"
    with PLAN_LOCK:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def plan_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / PLAN_FILE


def event_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / EVENTS_FILE


def inbox_path(root: Path, run_id: str, target: str) -> Path:
    safe_target = target.replace("/", "_")
    return run_dir(root, run_id) / "inbox" / f"{safe_target}.jsonl"


def session_path(root: Path, run_id: str, task_id: str | None, agent_id: str) -> Path:
    if task_id:
        return run_dir(root, run_id) / "tasks" / task_id / "sessions" / f"{agent_id}.json"
    return run_dir(root, run_id) / "sessions" / f"{agent_id}.json"


def require_plan(root: Path, run_id: str) -> dict:
    path = plan_path(root, run_id)
    if not path.exists():
        raise SystemExit(f"Run not found: {run_id}")
    return enrich_plan(read_json(path), load_config(root).get("backend", "codex"))


def save_plan(root: Path, plan: dict) -> None:
    write_json(plan_path(root, plan["id"]), plan)


def latest_run_id(root: Path) -> str | None:
    runs = root / CONFIG_DIR / RUNS_DIR
    if not runs.is_dir():
        return None
    candidates = sorted(p.name for p in runs.iterdir() if (p / PLAN_FILE).exists())
    return candidates[-1] if candidates else None


def resolve_run_id(root: Path, run_id: str | None) -> str:
    if run_id:
        return run_id
    latest = latest_run_id(root)
    if not latest:
        raise SystemExit("No runs found")
    return latest


def append_event(root: Path, run_id: str, event_type: str, data: dict) -> dict:
    event = {
        "ts": utc_now(),
        "run_id": run_id,
        "type": event_type,
        "data": data,
    }
    with EVENT_LOCK:
        append_jsonl(event_path(root, run_id), event)
    return event


def append_event_to_file(events_file: Path | None, run_id: str, event_type: str, data: dict) -> dict:
    event = {
        "ts": utc_now(),
        "run_id": run_id,
        "type": event_type,
        "data": data,
    }
    if events_file is not None:
        append_jsonl(events_file, event)
    return event


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
    append_jsonl(inbox_path(root, run_id, target), payload)
    if task_id:
        append_jsonl(run_dir(root, run_id) / "tasks" / task_id / "messages.jsonl", payload)
    append_event(root, run_id, "message", payload)
    return payload


def iter_jsonl_from(path: Path, start: int = 0) -> tuple[list[dict], int]:
    if not path.exists():
        return [], start
    events: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        f.seek(start)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"ts": utc_now(), "type": "malformed_event", "data": {"line": line}})
        return events, f.tell()


def iter_jsonl_reverse(path: Path, before: int | None = None, chunk_size: int = 65536):
    if not path.exists():
        return
    file_size = path.stat().st_size
    end = file_size if before is None else max(0, min(before, file_size))
    if end <= 0:
        return
    with path.open("rb") as f:
        carry = b""
        position = end
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            f.seek(position)
            data = f.read(read_size) + carry
            parts = data.split(b"\n")
            if position > 0:
                carry = parts[0]
                line_parts = parts[1:]
                line_start = position + len(parts[0]) + 1
            else:
                carry = b""
                line_parts = parts
                line_start = 0

            records: list[tuple[int, bytes]] = []
            cursor = line_start
            for part in line_parts:
                start = cursor
                cursor += len(part) + 1
                if part.strip():
                    records.append((start, part))

            for start, line in reversed(records):
                try:
                    yield start, json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    yield start, {"ts": utc_now(), "type": "malformed_event", "data": {"line": line.decode("utf-8", errors="replace")}}


def iter_text_lines_reverse(path: Path, before: int | None = None, chunk_size: int = 65536):
    if not path.exists():
        return
    file_size = path.stat().st_size
    end = file_size if before is None else max(0, min(before, file_size))
    if end <= 0:
        return
    with path.open("rb") as f:
        carry = b""
        position = end
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            f.seek(position)
            data = f.read(read_size) + carry
            parts = data.split(b"\n")
            if position > 0:
                carry = parts[0]
                line_parts = parts[1:]
                line_start = position + len(parts[0]) + 1
            else:
                carry = b""
                line_parts = parts
                line_start = 0

            records: list[tuple[int, bytes]] = []
            cursor = line_start
            for part in line_parts:
                start = cursor
                cursor += len(part) + 1
                if part:
                    records.append((start, part))

            for start, line in reversed(records):
                yield start, line.decode("utf-8", errors="replace")


def text_tail_page(path: Path, limit: int = 200, before: int | None = None) -> dict:
    file_size = path.stat().st_size if path.exists() else 0
    end_offset = file_size if before is None else max(0, min(before, file_size))
    safe_limit = max(1, min(limit, 1000))
    matches: list[dict] = []
    for offset, line in iter_text_lines_reverse(path, before=end_offset) or ():
        matches.append({"_cursor": offset, "text": line})
        if len(matches) > safe_limit:
            break

    has_more = len(matches) > safe_limit
    page = list(reversed(matches[:safe_limit]))
    next_before_offset = page[0].get("_cursor") if has_more and page else None
    return {
        "text": "\n".join(item["text"] for item in page),
        "lines": page,
        "before_offset": end_offset,
        "after_offset": file_size,
        "next_before_offset": next_before_offset,
        "has_more": has_more,
        "count": len(page),
    }


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
    "task_result_written",
    "task_final_requested",
    "task_waiting_for_subagents",
    "task_status_changed",
    "agent_started",
    "agent_status_changed",
    "agent_thread",
    "agent_command_started",
    "agent_command_finished",
    "agent_message",
    "agent_usage",
    "agent_error",
    "agent_delegated",
    "agent_message_routed",
    "sub_agent_reported",
    "sub_agent_report_ignored",
    "sub_agent_backend_recovered",
    "sub_agent_backend_failed",
    "agent_created",
    "agent_config_updated",
    "agent_finished",
    "workspace_missing",
}


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

    def add(value: object) -> None:
        text = str(value or "").strip()
        if text and text not in {"browser", "system", "aha"}:
            refs.add(text)

    add(data.get("target"))
    add(data.get("to_agent"))
    add(data.get("from_agent"))
    add(data.get("agent_id"))
    if event.get("type") == "message":
        add(data.get("sender"))
    event_type = str(event.get("type") or "")
    if not refs and (event_type.startswith("agent_") or event_type.startswith("task_") or event_type == "workspace_missing"):
        refs.add("main")
    return refs


def conversation_events_page(
    root: Path,
    run_id: str,
    task_id: str,
    target: str,
    limit: int = 50,
    before: int | None = None,
) -> dict:
    path = event_path(root, run_id)
    after_offset = path.stat().st_size if path.exists() else 0
    end_offset = after_offset if before is None else max(0, min(before, after_offset))
    safe_limit = max(1, min(limit, 200))
    matches: list[dict] = []
    for offset, event in iter_jsonl_reverse(path, before=end_offset) or ():
        if (
            event.get("type") in TIMELINE_EVENT_TYPES
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


def ensure_session(
    root: Path,
    run_id: str,
    task_id: str | None,
    agent_id: str,
    backend: str,
    model: str | None = None,
    workspace_path: str | None = None,
) -> dict:
    path = session_path(root, run_id, task_id, agent_id)
    if path.exists():
        session = read_json(path)
        changed = False
        for key, value in {"model": model, "workspace_path": workspace_path}.items():
            if value is not None and session.get(key) != value:
                session[key] = value
                changed = True
        if changed:
            session["updated_at"] = utc_now()
            write_json(path, session)
        return session
    session = make_session(run_id, task_id, agent_id, backend, model=model, workspace_path=workspace_path)
    write_json(path, session)
    return session


def save_session(root: Path, session: dict) -> None:
    write_json(session_path(root, session["run_id"], session.get("task_id"), session["agent_id"]), session)


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
    sandbox: str | None = None,
    approval: str | None = None,
) -> dict:
    run_id = new_run_id()
    titles = task_titles or default_tasks(goal, agents, mode)
    created = utc_now()
    tasks = [
        make_task(
            f"task-{idx:03d}",
            title,
            created,
            backend,
            model=model,
            workspace_path=workspace_path or str(root),
            sandbox=sandbox,
            approval=approval,
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
        "main_agent": make_agent("main", "run-main", backend, status="active", sandbox=sandbox, approval=approval),
        "tasks": tasks,
    }
    base = run_dir(root, run_id)
    for task in tasks:
        write_task_artifacts(root, plan, task)
        ensure_session(root, run_id, task["id"], "main", backend, model=model, workspace_path=task.get("workspace_path"))
    ensure_session(root, run_id, None, "main", backend, model=model, workspace_path=str(root))
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
    (task_dir / "messages.jsonl").touch()


def add_task(
    root: Path,
    run_id: str,
    title: str,
    backend: str = "codex",
    sub_agents: int = 0,
    model: str | None = None,
    workspace_path: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    delegation_policy: str = "auto",
    max_sub_agents: int = 3,
    preferred_sub_backend: str | None = None,
    preferred_sub_model: str | None = None,
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = make_task(
            next_task_id(plan["tasks"]),
            title,
            utc_now(),
            backend,
            model=model,
            workspace_path=workspace_path or str(root),
            sandbox=sandbox,
            approval=approval,
            delegation_policy=delegation_policy,
            max_sub_agents=max_sub_agents,
            preferred_sub_backend=preferred_sub_backend,
            preferred_sub_model=preferred_sub_model,
        )
        for _ in range(max(0, sub_agents)):
            add_agent_to_task_dict(
                task,
                preferred_sub_backend or backend,
                model=preferred_sub_model if preferred_sub_model is not None else model,
                workspace_path=workspace_path or str(root),
                sandbox=sandbox,
                approval=approval,
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
            "workspace_path": task.get("workspace_path"),
            "delegation_policy": delegation_policy,
            "max_sub_agents": max_sub_agents,
        },
    )
    return task


def add_agent_to_task_dict(
    task: dict,
    backend: str = "codex",
    role: str = "sub",
    model: str | None = None,
    workspace_path: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    created_by: str = "system",
    created_reason: str = "",
) -> dict:
    agent_id = "main" if role in {"main", "task-main"} else next_sub_id(task)
    agent = make_agent(
        agent_id,
        "task-main" if agent_id == "main" else "sub",
        backend,
        model=model,
        workspace_path=workspace_path or task.get("workspace_path"),
        sandbox=sandbox if sandbox is not None else task.get("preferred_sandbox"),
        approval=approval if approval is not None else task.get("preferred_approval"),
        created_by=created_by,
        created_reason=created_reason,
    )
    task.setdefault("agents", []).append(agent)
    return agent


def add_agent(
    root: Path,
    run_id: str,
    task_id: str,
    backend: str = "codex",
    role: str = "sub",
    model: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    created_by: str = "system",
    created_reason: str = "",
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None:
            raise SystemExit(f"Task not found: {task_id}")
        agent = add_agent_to_task_dict(
            task,
            backend,
            role,
            model=model,
            workspace_path=task.get("workspace_path"),
            sandbox=sandbox,
            approval=approval,
            created_by=created_by,
            created_reason=created_reason,
        )
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
        ensure_session(root, run_id, task_id, agent["id"], backend, model=model, workspace_path=task.get("workspace_path"))
    append_event(
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
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        agent = next((item for item in task.get("agents", []) if item.get("id") == agent_id), None)
        if agent is None:
            raise SystemExit(f"Agent not found: {agent_id}")
        if sandbox is not None:
            agent["sandbox"] = sandbox
            if agent_id == "main":
                task["preferred_sandbox"] = sandbox
        if approval is not None:
            agent["approval"] = approval
            if agent_id == "main":
                task["preferred_approval"] = approval
        agent["last_active_at"] = utc_now()
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event(
        root,
        run_id,
        "agent_config_updated",
        {"task_id": task_id, "agent_id": agent_id, "sandbox": agent.get("sandbox"), "approval": agent.get("approval")},
    )
    return agent


def set_agent_status(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    status: str,
    exit_code: int | None = None,
) -> dict:
    now = utc_now()
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        agent = next((item for item in task.get("agents", []) if item.get("id") == agent_id), None)
        if agent is None:
            raise SystemExit(f"Agent not found: {agent_id}")
        agent["status"] = status
        agent["last_active_at"] = now
        if status == "running":
            agent["started_at"] = now
            agent["finished_at"] = None
            agent["exit_code"] = None
        elif status in {"completed", "failed", "blocked"}:
            agent["finished_at"] = now
            agent["exit_code"] = exit_code
        plan["updated_at"] = now
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event(
        root,
        run_id,
        "agent_status_changed",
        {"task_id": task_id, "agent_id": agent_id, "status": status, "exit_code": exit_code},
    )
    return agent


def update_agent_runtime(root: Path, run_id: str, task_id: str, agent_id: str, **fields: object) -> dict:
    now = utc_now()
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        agent = next((item for item in task.get("agents", []) if item.get("id") == agent_id), None)
        if agent is None:
            raise SystemExit(f"Agent not found: {agent_id}")
        for key, value in fields.items():
            agent[key] = value
        agent["last_active_at"] = now
        plan["updated_at"] = now
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event(root, run_id, "agent_runtime_updated", {"task_id": task_id, "agent_id": agent_id, **fields})
    return agent


def mark_task_coordination(root: Path, run_id: str, task_id: str, **fields: object) -> dict:
    now = utc_now()
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        coordination = task.setdefault("coordination", {})
        for key, value in fields.items():
            if value is not None:
                coordination[key] = value
        coordination["updated_at"] = now
        plan["updated_at"] = now
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event(root, run_id, "task_coordination_updated", {"task_id": task_id, **fields})
    return task


def set_task_hidden(root: Path, run_id: str, task_id: str, hidden: bool) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        task["hidden"] = hidden
        task["hidden_at"] = utc_now() if hidden else None
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event(root, run_id, "task_hidden" if hidden else "task_restored", {"task_id": task_id})
    return task


def delete_task(root: Path, run_id: str, task_id: str) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None:
            raise SystemExit(f"Task not found: {task_id}")
        task["hidden"] = True
        task["hidden_at"] = task.get("hidden_at") or utc_now()
        task["deleted_at"] = task.get("deleted_at") or utc_now()
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event(root, run_id, "task_deleted", {"task_id": task_id})
    return task


def set_task_status(root: Path, run_id: str, task_id: str, status: str, exit_code: int | None = None) -> dict:
    now = utc_now()
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        task["status"] = status
        if status == "running":
            task["started_at"] = task.get("started_at") or now
            task["finished_at"] = None
            task["exit_code"] = None
        elif status in {"completed", "failed", "blocked"}:
            if not task.get("started_at"):
                task["started_at"] = now
            task["finished_at"] = now
            task["exit_code"] = exit_code
        plan["updated_at"] = now
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event(root, run_id, "task_status_changed", {"task_id": task_id, "status": status, "exit_code": exit_code})
    return task


def write_task_result(root: Path, run_id: str, task_id: str, content: str, policy: str = "finalize") -> Path:
    plan = require_plan(root, run_id)
    task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
    if task is None or task.get("deleted_at"):
        raise SystemExit(f"Task not found: {task_id}")
    path = run_dir(root, run_id) / task["output_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    write_json(path.with_suffix(".meta.json"), {"task_id": task_id, "policy": policy, "updated_at": utc_now()})
    append_event(root, run_id, "task_result_written", {"task_id": task_id, "path": str(path), "chars": len(content), "policy": policy})
    return path


def list_sessions(root: Path, run_id: str, task_id: str | None = None) -> list[dict]:
    base = run_dir(root, run_id) / ("sessions" if task_id is None else f"tasks/{task_id}/sessions")
    if not base.is_dir():
        return []
    return [read_json(path) for path in sorted(base.glob("*.json"))]


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
                "workspace_path": task.get("workspace_path"),
                "preferred_backend": task.get("preferred_backend"),
                "preferred_model": task.get("preferred_model"),
                "preferred_sandbox": task.get("preferred_sandbox"),
                "preferred_approval": task.get("preferred_approval"),
                "delegation_policy": task.get("delegation_policy", "auto"),
                "max_sub_agents": task.get("max_sub_agents", 3),
                "status": task["status"],
                "exit_code": task["exit_code"],
                "started_at": task["started_at"],
                "finished_at": task["finished_at"],
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
    if output_file.exists() and result_meta.get("policy") == "finalize":
        result = output_file.read_text(encoding="utf-8")
    return {"task_id": task_id, "result": result, "result_meta": result_meta}


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
    if output_file.exists() and result_meta.get("policy") == "finalize":
        result = output_file.read_text(encoding="utf-8")
    return {
        "task": task,
        "prompt": prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else "",
        "result": result,
        "result_meta": result_meta,
        "log": log_file.read_text(encoding="utf-8") if log_file.exists() else "",
        "inbox": inbox_file.read_text(encoding="utf-8") if inbox_file.exists() else "",
        "messages": task_messages.read_text(encoding="utf-8") if task_messages.exists() else "",
        "sessions": list_sessions(root, run_id, task_id),
        "write_scopes": plan.get("write_scopes", []),
    }
