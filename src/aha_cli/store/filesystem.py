from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import threading
import uuid

from aha_cli.constants import CONFIG_DIR, CONFIG_FILE, EVENTS_FILE, PLAN_FILE, RUNS_DIR, WORKSPACES_DIR
from aha_cli.domain.models import (
    default_config,
    default_tasks,
    enrich_plan,
    make_agent,
    make_session,
    make_task,
    make_task_round,
    next_sub_id,
    next_task_id,
    task_prompt,
    utc_now,
    new_run_id,
)
from aha_cli.services.proxy import DEFAULT_NO_PROXY, normalize_proxy_value, task_has_proxy_config

PLAN_LOCK = threading.RLock()
EVENT_LOCK = threading.Lock()
TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}
AHA_HOME_ENV = "AHA_HOME"
_EXPLICIT_AHA_HOMES: set[str] = set()
UNSET = object()


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


def append_jsonl(path: Path, data: dict) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(data, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o666)
    try:
        with os.fdopen(fd, "ab", closefd=False) as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                payload = line.encode("utf-8")
                written = 0
                while written < len(payload):
                    count = os.write(f.fileno(), payload[written:])
                    if count == 0:
                        raise OSError(f"Unable to append JSONL record to {path}")
                    written += count
                return os.lseek(f.fileno(), 0, os.SEEK_CUR)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        os.close(fd)


def iter_jsonl_records_from(
    path: Path,
    start: int = 0,
    before: int | None = None,
    limit: int | None = None,
) -> tuple[list[tuple[dict, int]], int]:
    if not path.exists():
        return [], start
    file_size = path.stat().st_size
    end = file_size if before is None else max(0, min(before, file_size))
    records: list[tuple[dict, int]] = []
    offset = max(0, min(start, end))
    with path.open("rb") as f:
        f.seek(offset)
        while f.tell() < end and (limit is None or len(records) < limit):
            line_start = f.tell()
            line = f.readline(end - line_start if before is not None else -1)
            if not line:
                break
            line_end = f.tell()
            if before is not None and line_end >= end and not line.endswith(b"\n"):
                return records, line_start
            line = line.strip()
            if not line:
                offset = line_end
                continue
            try:
                records.append((json.loads(line.decode("utf-8")), line_end))
            except (UnicodeDecodeError, json.JSONDecodeError):
                records.append(({"ts": utc_now(), "type": "malformed_event", "data": {"line": line.decode("utf-8", errors="replace")}}, line_end))
            offset = line_end
        return records, offset


def _normalized_path(path: Path) -> Path:
    return path.expanduser().resolve()


def default_aha_home() -> Path:
    return _normalized_path(Path.home() / CONFIG_DIR)


def mark_aha_home(path: Path) -> Path:
    home = _normalized_path(path)
    _EXPLICIT_AHA_HOMES.add(str(home))
    return home


def aha_home_path(root: Path) -> Path:
    root = _normalized_path(root)
    env_home = os.environ.get(AHA_HOME_ENV)
    if env_home and _normalized_path(Path(env_home)) == root:
        return root
    if str(root) in _EXPLICIT_AHA_HOMES:
        return root
    if root.name == CONFIG_DIR:
        return root
    if (root / CONFIG_FILE).exists() or (root / RUNS_DIR).is_dir():
        return root
    return root / CONFIG_DIR


def find_aha_home(start: Path | None = None, explicit: str | Path | None = None) -> Path:
    if explicit:
        return mark_aha_home(Path(explicit))
    env_home = os.environ.get(AHA_HOME_ENV)
    if env_home:
        return mark_aha_home(Path(env_home))
    current = (start or Path.cwd()).resolve()
    for path in [current, *current.parents]:
        if (path / CONFIG_DIR).is_dir():
            return path / CONFIG_DIR
    return mark_aha_home(default_aha_home())


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for path in [current, *current.parents]:
        if (path / CONFIG_DIR).is_dir():
            return path
    return current


def config_path(root: Path) -> Path:
    return aha_home_path(root) / CONFIG_FILE


def load_config(root: Path) -> dict:
    defaults = default_config()
    path = config_path(root)
    if not path.exists():
        return defaults
    loaded = read_json(path)
    cfg = defaults | {key: value for key, value in loaded.items() if key not in {"codex", "claude"}}
    cfg["codex"] = defaults["codex"] | loaded.get("codex", {})
    cfg["claude"] = defaults["claude"] | loaded.get("claude", {})
    if cfg.get("runner_command") and cfg.get("backend") == "stub":
        cfg["backend"] = "command"
    return cfg


def run_dir(root: Path, run_id: str) -> Path:
    return aha_home_path(root) / RUNS_DIR / run_id


def workspaces_dir(root: Path) -> Path:
    return aha_home_path(root) / WORKSPACES_DIR


def list_workspaces(root: Path) -> list[dict]:
    base = workspaces_dir(root)
    if not base.is_dir():
        return []
    workspaces: list[dict] = []
    for path in sorted(base.glob("*.json")):
        try:
            workspace = read_json(path)
        except (OSError, ValueError):
            continue
        if workspace.get("id") and workspace.get("path"):
            workspaces.append(workspace)
    return sorted(workspaces, key=lambda item: (str(item.get("name") or ""), str(item.get("id") or "")))


def get_workspace(root: Path, workspace_id: str) -> dict | None:
    if not workspace_id:
        return None
    path = workspaces_dir(root) / f"{workspace_id}.json"
    if path.exists():
        return read_json(path)
    return next((workspace for workspace in list_workspaces(root) if workspace.get("id") == workspace_id), None)


def _next_workspace_id(root: Path) -> str:
    used: set[int] = set()
    for workspace in list_workspaces(root):
        workspace_id = str(workspace.get("id") or "")
        if workspace_id.startswith("ws-") and workspace_id[3:].isdigit():
            used.add(int(workspace_id[3:]))
    index = 1
    while index in used:
        index += 1
    return f"ws-{index:03d}"


def add_workspace(root: Path, workspace_path: str | Path, name: str | None = None) -> dict:
    path = _normalized_path(Path(workspace_path))
    if not path.is_dir():
        raise ValueError(f"workspace path is not a directory: {path}")
    now = utc_now()
    for workspace in list_workspaces(root):
        if _normalized_path(Path(str(workspace.get("path")))) == path:
            if name and workspace.get("name") != name:
                workspace["name"] = name
            workspace["last_used_at"] = now
            write_json(workspaces_dir(root) / f"{workspace['id']}.json", workspace)
            return workspace
    workspace = {
        "id": _next_workspace_id(root),
        "name": name or path.name,
        "path": str(path),
        "created_at": now,
        "last_used_at": now,
    }
    write_json(workspaces_dir(root) / f"{workspace['id']}.json", workspace)
    return workspace


def resolve_workspace_path(
    root: Path,
    workspace_id: str | None = None,
    workspace_path: str | Path | None = None,
    default: str | Path | None = None,
) -> tuple[str, str | None]:
    if workspace_path:
        resolved = _normalized_path(Path(workspace_path))
        if workspace_id:
            workspace = get_workspace(root, workspace_id)
            if workspace is None:
                raise ValueError(f"workspace not found: {workspace_id}")
            if _normalized_path(Path(str(workspace["path"]))) != resolved:
                raise ValueError(f"workspace path does not match registered workspace: {workspace_id}")
        return str(resolved), workspace_id
    if workspace_id:
        workspace = get_workspace(root, workspace_id)
        if workspace is None:
            raise ValueError(f"workspace not found: {workspace_id}")
        return str(workspace["path"]), str(workspace["id"])
    fallback = _normalized_path(Path(default)) if default is not None else _normalized_path(Path.cwd())
    return str(fallback), None


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


def event_stream_position(root: Path, run_id: str) -> int:
    path = event_path(root, run_id)
    return path.stat().st_size if path.exists() else 0


def normalize_event_id(event_id: object, default: int = 0) -> int:
    if event_id is None or event_id == "":
        return default
    try:
        return max(0, int(event_id))
    except (TypeError, ValueError):
        return default


def with_event_id(event: dict, event_id: int) -> dict:
    item = dict(event)
    item.setdefault("event_id", event_id)
    return item


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
    runs = aha_home_path(root) / RUNS_DIR
    if not runs.is_dir():
        return None
    candidates = sorted(p.name for p in runs.iterdir() if (p / PLAN_FILE).exists())
    return candidates[-1] if candidates else None


def run_exists(root: Path, run_id: str) -> bool:
    return bool(run_id) and plan_path(root, run_id).exists()


def run_summary_from_plan(root: Path, plan: dict) -> dict:
    tasks = [task for task in plan.get("tasks", []) if not task.get("deleted_at")]
    completed = sum(1 for task in tasks if task.get("status") == "completed")
    failed = any(task.get("status") == "failed" for task in tasks)
    blocked = any(task.get("status") == "blocked" for task in tasks)
    running = any(task.get("status") in {"running", "awaiting_user"} for task in tasks)
    if failed:
        status = "failed"
    elif blocked:
        status = "blocked"
    elif tasks and completed == len(tasks):
        status = "completed"
    elif running:
        status = "running"
    else:
        status = "pending"
    return {
        "id": plan["id"],
        "goal": plan.get("goal", ""),
        "mode": plan.get("mode", ""),
        "status": status,
        "created_at": plan.get("created_at"),
        "updated_at": plan.get("updated_at"),
        "task_count": len(tasks),
        "completed_count": completed,
        "hidden_count": sum(1 for task in tasks if task.get("hidden")),
        "path": str(plan_path(root, plan["id"])),
    }


def run_summary(root: Path, run_id: str) -> dict:
    plan = enrich_plan(read_json(plan_path(root, run_id)), load_config(root).get("backend", "codex"))
    return run_summary_from_plan(root, plan)


def list_run_summaries(root: Path) -> list[dict]:
    runs = aha_home_path(root) / RUNS_DIR
    if not runs.is_dir():
        return []
    summaries: list[dict] = []
    for path in sorted(runs.glob(f"*/{PLAN_FILE}"), reverse=True):
        try:
            plan = enrich_plan(read_json(path), load_config(root).get("backend", "codex"))
            summaries.append(run_summary_from_plan(root, plan))
        except (OSError, ValueError, KeyError):
            continue
    return summaries


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
        event_id = append_jsonl(event_path(root, run_id), event)
    return with_event_id(event, event_id)


def append_event_to_file(events_file: Path | None, run_id: str, event_type: str, data: dict) -> dict:
    event = {
        "ts": utc_now(),
        "run_id": run_id,
        "type": event_type,
        "data": data,
    }
    if events_file is not None:
        event_id = append_jsonl(events_file, event)
        return with_event_id(event, event_id)
    return event


def event_stream_page(
    root: Path,
    run_id: str,
    last_event_id: object = 0,
    limit: int | None = None,
    snapshot_event_id: object | None = None,
) -> dict:
    path = event_path(root, run_id)
    snapshot_id = normalize_event_id(snapshot_event_id, event_stream_position(root, run_id))
    start_id = normalize_event_id(last_event_id)
    if not path.exists():
        return {
            "events": [],
            "last_event_id": snapshot_id,
            "snapshot_event_id": snapshot_id,
            "has_more": False,
            "limit": limit,
        }
    records, next_id = iter_jsonl_records_from(path, start_id, before=snapshot_id, limit=limit)
    return {
        "events": [with_event_id(event, line_end) for event, line_end in records],
        "last_event_id": next_id,
        "snapshot_event_id": snapshot_id,
        "has_more": next_id < snapshot_id,
        "limit": limit,
    }


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
    append_jsonl(inbox_path(root, run_id, target), payload)
    if task_id:
        append_jsonl(run_dir(root, run_id) / "tasks" / task_id / "messages.jsonl", payload)
    append_event(root, run_id, "message", payload)
    return payload


def iter_jsonl_from(path: Path, start: int = 0, before: int | None = None, limit: int | None = None) -> tuple[list[dict], int]:
    records, offset = iter_jsonl_records_from(path, start, before=before, limit=limit)
    return [item for item, _line_end in records], offset


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
    proxy_enabled: bool | None = None,
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
        proxy_enabled=bool(task.get("preferred_proxy_enabled")) if proxy_enabled is None else bool(proxy_enabled),
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
    proxy_enabled: bool | None = None,
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
            proxy_enabled=proxy_enabled,
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
        if proxy_enabled is not None:
            agent["proxy_enabled"] = bool(proxy_enabled)
        agent["last_active_at"] = utc_now()
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event(
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
        previous_status = agent.get("status")
        agent["status"] = status
        agent["last_active_at"] = now
        if previous_status != status or not agent.get("status_started_at"):
            agent["status_started_at"] = now
        if status == "running":
            agent["started_at"] = now
            agent["finished_at"] = None
            agent["exit_code"] = None
        elif status in {"completed", "failed", "blocked", "interrupted"}:
            agent["finished_at"] = now
            agent["exit_code"] = exit_code
        plan["updated_at"] = now
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event(
        root,
        run_id,
        "agent_status_changed",
        {"task_id": task_id, "agent_id": agent_id, "status": status, "exit_code": exit_code, "status_started_at": agent.get("status_started_at")},
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


def set_task_status(
    root: Path,
    run_id: str,
    task_id: str,
    status: str,
    exit_code: int | None = None,
    *,
    allow_terminal_transition: bool = False,
) -> dict:
    now = utc_now()
    should_append = True
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        if task.get("status") in TERMINAL_TASK_STATUSES and not allow_terminal_transition:
            should_append = False
        else:
            task["status"] = status
        if status == "running":
            if should_append:
                task["started_at"] = task.get("started_at") or now
                task["finished_at"] = None
                task["exit_code"] = None
        elif status == "awaiting_user":
            if should_append:
                task["started_at"] = task.get("started_at") or now
                task["finished_at"] = None
                task["exit_code"] = None
        elif status in TERMINAL_TASK_STATUSES:
            if should_append:
                if not task.get("started_at"):
                    task["started_at"] = now
                task["finished_at"] = now
                task["exit_code"] = exit_code
        plan["updated_at"] = now
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    if should_append:
        append_event(root, run_id, "task_status_changed", {"task_id": task_id, "status": status, "exit_code": exit_code})
        if status in {"awaiting_user", *TERMINAL_TASK_STATUSES}:
            render_task_overview_result_if_needed(root, run_id, task_id, policy="journal")
    return task


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


def status_snapshot_projection(root: Path, run_id: str) -> dict:
    snapshot_event_id = event_stream_position(root, run_id)
    snapshot = status_snapshot(root, run_id)
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
