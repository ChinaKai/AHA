"""Task-scoped background processes owned by the AHA Web runtime."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import uuid

from aha_cli import platform, process_control
from aha_cli.domain.models import utc_now
from aha_cli.store.filesystem import append_event, read_json, run_dir, task_snapshot, write_json


MANAGED_PROCESS_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_OWNER_INSTANCE = uuid.uuid4().hex
_REGISTRY_LOCK = threading.RLock()


class ManagedProcessError(RuntimeError):
    """A managed process request is invalid or unsafe to execute."""


class ManagedProcessConflict(ManagedProcessError):
    """A process name is already owned by a live command."""


@dataclass
class _OwnedProcess:
    process: subprocess.Popen[bytes]
    log_file: object
    stop_requested: bool = False


_OWNED_PROCESSES: dict[str, _OwnedProcess] = {}
TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}


def _safe_component(value: str) -> str:
    return str(value or "").replace("/", "_").replace("\\", "_")


def validate_managed_process_name(name: str) -> str:
    value = str(name or "").strip()
    if not MANAGED_PROCESS_NAME_RE.fullmatch(value):
        raise ManagedProcessError("process name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
    return value


def managed_process_state_path(root: Path, run_id: str, task_id: str, agent_id: str, name: str) -> Path:
    safe_name = validate_managed_process_name(name)
    filename = "managed-process-{}-{}-{}.json".format(
        _safe_component(task_id),
        _safe_component(agent_id),
        safe_name,
    )
    return run_dir(root, run_id) / "runtime" / filename


def managed_process_log_path(root: Path, run_id: str, task_id: str, agent_id: str, name: str) -> Path:
    safe_name = validate_managed_process_name(name)
    filename = "managed-process-{}-{}-{}.log".format(
        _safe_component(task_id),
        _safe_component(agent_id),
        safe_name,
    )
    return run_dir(root, run_id) / "logs" / filename


def _scope(root: Path, run_id: str, task_id: str, agent_id: str) -> tuple[dict, Path]:
    try:
        detail = task_snapshot(root, run_id, task_id)
    except (FileNotFoundError, KeyError, SystemExit) as exc:
        raise ManagedProcessError(f"task not found: {task_id}") from exc
    task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
    agents = task.get("agents") if isinstance(task.get("agents"), list) else []
    valid_agents = {str(item.get("id") or "") for item in agents if isinstance(item, dict)}
    valid_agents.add("main")
    if agent_id not in valid_agents:
        raise ManagedProcessError(f"agent is not assigned to {task_id}: {agent_id}")
    workspace = Path(str(task.get("workspace_path") or "")).expanduser()
    if not workspace.is_absolute():
        workspace = workspace.resolve()
    return task, workspace.resolve()


def _managed_cwd(workspace: Path, cwd: str | Path | None) -> Path:
    candidate = workspace if not str(cwd or "").strip() else Path(str(cwd)).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise ManagedProcessError("managed process cwd must stay inside the task workspace")
    if not resolved.is_dir():
        raise ManagedProcessError(f"managed process cwd does not exist: {resolved}")
    return resolved


def _read_state(path: Path) -> dict:
    try:
        state = read_json(path)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(path: Path, state: dict) -> dict:
    state["updated_at"] = utc_now()
    write_json(path, state)
    return state


def _state_key(path: Path) -> str:
    return str(path.resolve())


def _public_state(state: dict, *, alive: bool | None = None) -> dict:
    result = dict(state)
    if alive is not None:
        result["alive"] = bool(alive)
    return result


def _refresh_state(path: Path) -> dict:
    state = _read_state(path)
    if not state:
        return {}
    key = _state_key(path)
    owned = _OWNED_PROCESSES.get(key)
    if owned is not None:
        exit_code = owned.process.poll()
        if exit_code is None:
            return _public_state(state, alive=True)
        state["status"] = "stopped"
        state["exit_code"] = int(exit_code)
        state["finished_at"] = state.get("finished_at") or utc_now()
        state["stop_reason"] = state.get("stop_reason") or ("requested" if owned.stop_requested else "exited")
        _write_state(path, state)
        _OWNED_PROCESSES.pop(key, None)
        return _public_state(state, alive=False)
    pid = int(state.get("pid") or 0)
    alive = process_control.process_exists(pid)
    if not alive and state.get("status") in {"starting", "running", "stopping"}:
        state["status"] = "stopped"
        state["finished_at"] = state.get("finished_at") or utc_now()
        state["stop_reason"] = state.get("stop_reason") or "owner_or_process_exited"
        _write_state(path, state)
    elif alive and state.get("owner_instance") != _OWNER_INSTANCE:
        state = {**state, "status": "unmanaged", "diagnostic": "process is not owned by this AHA Web instance"}
    return _public_state(state, alive=alive)


def managed_process_status(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    name: str,
) -> dict:
    _scope(root, run_id, task_id, agent_id)
    path = managed_process_state_path(root, run_id, task_id, agent_id, name)
    with _REGISTRY_LOCK:
        state = _refresh_state(path)
    if not state:
        raise FileNotFoundError(f"managed process not found: {name}")
    return state


def list_managed_processes(root: Path, run_id: str, task_id: str, agent_id: str) -> list[dict]:
    _scope(root, run_id, task_id, agent_id)
    runtime_dir = run_dir(root, run_id) / "runtime"
    pattern = "managed-process-{}-{}-*.json".format(_safe_component(task_id), _safe_component(agent_id))
    with _REGISTRY_LOCK:
        states = [_refresh_state(path) for path in sorted(runtime_dir.glob(pattern))]
    return [state for state in states if state]


def _monitor_process(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    name: str,
    path: Path,
    owned: _OwnedProcess,
) -> None:
    exit_code = owned.process.wait()
    try:
        owned.log_file.close()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass
    with _REGISTRY_LOCK:
        key = _state_key(path)
        current = _OWNED_PROCESSES.get(key)
        if current is not owned:
            return
        state = _read_state(path)
        state.update(
            {
                "status": "stopped",
                "alive": False,
                "exit_code": int(exit_code),
                "finished_at": utc_now(),
                "stop_reason": "requested" if owned.stop_requested else "exited",
            }
        )
        _write_state(path, state)
        _OWNED_PROCESSES.pop(key, None)
    append_event(
        root,
        run_id,
        "managed_process_finished",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "name": name,
            "pid": owned.process.pid,
            "exit_code": int(exit_code),
            "stop_reason": "requested" if owned.stop_requested else "exited",
        },
    )


def start_managed_process(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    name: str,
    command: list[str],
    *,
    cwd: str | Path | None = None,
) -> dict:
    safe_name = validate_managed_process_name(name)
    argv = [str(item) for item in command if str(item)]
    if not argv:
        raise ManagedProcessError("managed process command is required")
    _task, workspace = _scope(root, run_id, task_id, agent_id)
    process_cwd = _managed_cwd(workspace, cwd)
    state_path = managed_process_state_path(root, run_id, task_id, agent_id, safe_name)
    log_path = managed_process_log_path(root, run_id, task_id, agent_id, safe_name)
    key = _state_key(state_path)
    with _REGISTRY_LOCK:
        previous = _refresh_state(state_path)
        if previous.get("alive"):
            if previous.get("command") == argv and previous.get("cwd") == str(process_cwd):
                return {**previous, "already_running": True}
            raise ManagedProcessConflict(f"managed process is already running: {safe_name}")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("ab", buffering=0)
        creationflags = 0
        if platform.is_windows():
            creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
                getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        launched_command = platform.spawn_command(argv)
        try:
            process = subprocess.Popen(
                launched_command,
                cwd=str(process_cwd),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
                # A managed process is intentionally long-lived and independent
                # of its spawning runtime (it survives Web restarts and turn
                # boundaries), so it must NOT be armed with PR_SET_PDEATHSIG:
                # that signal is for short-lived bridge processes and fires the
                # moment the spawning thread exits (e.g. a per-request asyncio
                # loop in tests), killing the managed process before it prints
                # anything. Session isolation (start_new_session) + explicit
                # stop_managed_process is the lifecycle contract instead.
                start_new_session=not platform.is_windows(),
                creationflags=creationflags,
            )
        except OSError:
            log_file.close()
            raise
        state = {
            "schema_version": 1,
            "run_id": run_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "name": safe_name,
            "status": "running",
            "alive": True,
            "pid": process.pid,
            "command": argv,
            "cwd": str(process_cwd),
            "log_path": str(log_path),
            "owner": "aha-web",
            "owner_pid": os.getpid(),
            "owner_instance": _OWNER_INSTANCE,
            "started_at": utc_now(),
            "exit_code": None,
        }
        _write_state(state_path, state)
        owned = _OwnedProcess(process=process, log_file=log_file)
        _OWNED_PROCESSES[key] = owned
        threading.Thread(
            target=_monitor_process,
            args=(root, run_id, task_id, agent_id, safe_name, state_path, owned),
            name=f"aha-managed-{safe_name}",
            daemon=True,
        ).start()
    append_event(
        root,
        run_id,
        "managed_process_started",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "name": safe_name,
            "pid": process.pid,
            "command": argv,
            "cwd": str(process_cwd),
            "log_path": str(log_path),
        },
    )
    return {**state, "started": True, "already_running": False}


def stop_managed_process(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    name: str,
    *,
    timeout: float = 3.0,
) -> dict:
    _scope(root, run_id, task_id, agent_id)
    safe_name = validate_managed_process_name(name)
    path = managed_process_state_path(root, run_id, task_id, agent_id, safe_name)
    key = _state_key(path)
    with _REGISTRY_LOCK:
        state = _refresh_state(path)
        if not state:
            raise FileNotFoundError(f"managed process not found: {safe_name}")
        owned = _OWNED_PROCESSES.get(key)
        if owned is None:
            if not state.get("alive"):
                return {**state, "already_stopped": True}
            raise ManagedProcessConflict("live process is not owned by this AHA Web instance; refusing to kill a reused PID")
        owned.stop_requested = True
        state["status"] = "stopping"
        state["stop_requested_at"] = utc_now()
        state["stop_reason"] = "requested"
        _write_state(path, state)
        pid = owned.process.pid
    append_event(
        root,
        run_id,
        "managed_process_stop_requested",
        {"task_id": task_id, "agent_id": agent_id, "name": safe_name, "pid": pid},
    )
    try:
        process_control.signal_process_group(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError, PermissionError):
        pass
    try:
        owned.process.wait(timeout=max(0.0, float(timeout)))
    except subprocess.TimeoutExpired:
        try:
            process_control.signal_process_group(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError, PermissionError):
            pass
        try:
            owned.process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
    with _REGISTRY_LOCK:
        result = _refresh_state(path)
    return result


def stop_all_managed_processes(root: Path, *, timeout: float = 3.0) -> int:
    """Stop process trees owned by this Web instance during service shutdown."""

    root_text = str(root.resolve())
    with _REGISTRY_LOCK:
        owned_items = [
            (key, owned)
            for key, owned in _OWNED_PROCESSES.items()
            if key == root_text or key.startswith(root_text + os.sep)
        ]
        for _key, owned in owned_items:
            owned.stop_requested = True
    for _key, owned in owned_items:
        try:
            process_control.signal_process_group(owned.process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError, PermissionError):
            pass
    for _key, owned in owned_items:
        try:
            owned.process.wait(timeout=max(0.0, float(timeout)))
        except subprocess.TimeoutExpired:
            try:
                process_control.signal_process_group(owned.process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError, PermissionError):
                pass
    return len(owned_items)


def reconcile_managed_processes(root: Path) -> int:
    """Stop Web-owned processes whose task has reached a terminal state."""

    with _REGISTRY_LOCK:
        paths = [Path(key) for key in _OWNED_PROCESSES]
    stopped = 0
    for path in paths:
        state = _read_state(path)
        run_id = str(state.get("run_id") or "")
        task_id = str(state.get("task_id") or "")
        agent_id = str(state.get("agent_id") or "main")
        name = str(state.get("name") or "")
        if not run_id or not task_id or not name:
            continue
        try:
            detail = task_snapshot(root, run_id, task_id)
            status = str((detail.get("task") or {}).get("status") or "")
        except (FileNotFoundError, KeyError, OSError, ValueError, SystemExit):
            status = "missing"
        if status not in TERMINAL_TASK_STATUSES and status != "missing":
            continue
        try:
            stop_managed_process(root, run_id, task_id, agent_id, name)
        except (FileNotFoundError, ManagedProcessError, OSError):
            continue
        stopped += 1
    return stopped


__all__ = [
    "ManagedProcessConflict",
    "ManagedProcessError",
    "list_managed_processes",
    "managed_process_log_path",
    "managed_process_state_path",
    "managed_process_status",
    "reconcile_managed_processes",
    "start_managed_process",
    "stop_all_managed_processes",
    "stop_managed_process",
    "validate_managed_process_name",
]
