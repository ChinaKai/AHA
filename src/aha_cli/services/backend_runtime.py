from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from aha_cli.domain.models import utc_now
from aha_cli.store.filesystem import (
    append_event,
    event_path,
    iter_jsonl_from,
    read_json,
    require_plan,
    run_dir,
    write_json,
)


def safe_target(target: str) -> str:
    return (target or "main").replace("/", "_")


def backend_state_path(root: Path, run_id: str, target: str = "main") -> Path:
    return run_dir(root, run_id) / "runtime" / f"backend-{safe_target(target)}.json"


def backend_log_path(root: Path, run_id: str, target: str = "main") -> Path:
    return run_dir(root, run_id) / "logs" / f"backend-{safe_target(target)}.log"


def pid_is_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    proc_stat = Path("/proc") / str(pid) / "stat"
    if proc_stat.exists():
        try:
            parts = proc_stat.read_text(encoding="utf-8").split()
            if len(parts) > 2 and parts[2] == "Z":
                return False
        except OSError:
            pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_state(root: Path, run_id: str, target: str) -> dict:
    path = backend_state_path(root, run_id, target)
    if not path.exists():
        return {}
    try:
        return read_json(path)
    except (OSError, ValueError):
        return {}


def _write_state(root: Path, run_id: str, target: str, state: dict) -> dict:
    write_json(backend_state_path(root, run_id, target), state)
    return state


def _event_time(event: dict) -> str:
    return str(event.get("ts", "") or "")


def _backend_activity(root: Path, run_id: str, target: str) -> dict:
    events, _ = iter_jsonl_from(event_path(root, run_id), 0)
    latest_started: dict | None = None
    latest_finished: dict | None = None
    latest_reply: dict | None = None
    latest_error: dict | None = None
    for event in events:
        data = event.get("data") or {}
        if event.get("type") == "agent_started" and data.get("target") == target:
            latest_started = event
        elif event.get("type") == "agent_finished" and data.get("target") == target:
            latest_finished = event
        elif event.get("type") == "agent_error" and data.get("target") == target:
            latest_error = event
        elif event.get("type") == "message" and data.get("sender") == target:
            latest_reply = event
    started_at = _event_time(latest_started or {})
    finished_at = _event_time(latest_finished or {})
    busy = bool(started_at and (not finished_at or started_at > finished_at))
    return {
        "busy": busy,
        "last_started_at": started_at or None,
        "last_finished_at": finished_at or None,
        "last_reply_at": _event_time(latest_reply or {}) or None,
        "last_error_at": _event_time(latest_error or {}) or None,
    }


def _discover_backend_process(run_id: str, target: str) -> int | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    current_pid = os.getpid()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == current_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        parts = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
        if "codex-chat" not in parts:
            continue
        index = parts.index("codex-chat")
        if (
            len(parts) > index + 2
            and parts[index + 1] == run_id
            and parts[index + 2] == target
            and pid_is_running(pid)
        ):
            return pid
    return None


def backend_status(root: Path, run_id: str, target: str = "main") -> dict:
    require_plan(root, run_id)
    target = target or "main"
    state = _read_state(root, run_id, target)
    pid = int(state.get("pid") or 0) or None
    managed = bool(state.get("managed")) if state else False
    running = pid_is_running(pid)
    discovered_pid = None if running else _discover_backend_process(run_id, target)
    if discovered_pid:
        pid = discovered_pid
        running = True
        managed = bool(state.get("managed")) if state and state.get("pid") == pid else False
    activity = _backend_activity(root, run_id, target)
    status = "busy" if running and activity["busy"] else "running" if running else "stopped"
    return {
        "target": target,
        "backend": state.get("backend", "codex-chat"),
        "status": status,
        "pid": pid if running else None,
        "last_pid": pid if not running else None,
        "managed": managed,
        "started_at": state.get("started_at"),
        "stopped_at": state.get("stopped_at"),
        "log_path": state.get("log_path") or str(backend_log_path(root, run_id, target)),
        "command": state.get("command", []),
        **activity,
    }


def _codex_chat_command(
    run_id: str,
    target: str,
    *,
    codex_bin: str = "codex",
    model: str | None = None,
    sandbox: str = "workspace-write",
    approval: str = "never",
    interval: float = 1.0,
    from_start: bool = False,
    no_json: bool = False,
    extra_args: list[str] | None = None,
    prompt_prefix: str = "You are connected to AHA as the real backend agent.",
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "aha_cli",
        "codex-chat",
        run_id,
        target,
        "--sender",
        target,
        "--codex-bin",
        codex_bin,
        "--sandbox",
        sandbox,
        "--approval",
        approval,
        "--interval",
        str(interval),
        "--prompt-prefix",
        prompt_prefix,
    ]
    if model:
        command.extend(["--model", model])
    if from_start:
        command.append("--from-start")
    if no_json:
        command.append("--no-json")
    for item in extra_args or []:
        command.extend(["--extra-arg", item])
    return command


def start_backend(
    root: Path,
    run_id: str,
    target: str = "main",
    *,
    codex_bin: str = "codex",
    model: str | None = None,
    sandbox: str = "workspace-write",
    approval: str = "never",
    interval: float = 1.0,
    from_start: bool = False,
    no_json: bool = False,
    extra_args: list[str] | None = None,
    prompt_prefix: str = "You are connected to AHA as the real backend agent.",
) -> dict:
    current = backend_status(root, run_id, target)
    if current["status"] in {"running", "busy"}:
        current["already_running"] = True
        return current
    target = target or "main"
    command = _codex_chat_command(
        run_id,
        target,
        codex_bin=codex_bin,
        model=model,
        sandbox=sandbox,
        approval=approval,
        interval=interval,
        from_start=from_start,
        no_json=no_json,
        extra_args=extra_args,
        prompt_prefix=prompt_prefix,
    )
    log_path = backend_log_path(root, run_id, target)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log_file.close()
    state = {
        "target": target,
        "backend": "codex-chat",
        "status": "running",
        "pid": process.pid,
        "managed": True,
        "started_at": utc_now(),
        "stopped_at": None,
        "log_path": str(log_path),
        "command": command,
        "sandbox": sandbox,
        "approval": approval,
        "model": model,
        "from_start": from_start,
    }
    _write_state(root, run_id, target, state)
    append_event(root, run_id, "backend_started", {"target": target, "pid": process.pid, "log_path": str(log_path)})
    return backend_status(root, run_id, target) | {"started": True}


def stop_backend(root: Path, run_id: str, target: str = "main", *, timeout: float = 5.0) -> dict:
    current = backend_status(root, run_id, target)
    pid = current.get("pid")
    target = target or "main"
    if not pid or current["status"] == "stopped":
        current["already_stopped"] = True
        return current
    try:
        pgid = os.getpgid(int(pid))
        if pgid == int(pid):
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_is_running(int(pid)):
            break
        time.sleep(0.1)
    if pid_is_running(int(pid)):
        try:
            pgid = os.getpgid(int(pid))
            if pgid == int(pid):
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    state = _read_state(root, run_id, target)
    state.update(
        {
            "target": target,
            "backend": state.get("backend", "codex-chat"),
            "status": "stopped",
            "pid": pid,
            "managed": bool(state),
            "stopped_at": utc_now(),
            "log_path": state.get("log_path") or str(backend_log_path(root, run_id, target)),
            "command": state.get("command", []),
        }
    )
    _write_state(root, run_id, target, state)
    append_event(root, run_id, "backend_stopped", {"target": target, "pid": pid})
    return backend_status(root, run_id, target) | {"stopped": True}


def restart_backend(root: Path, run_id: str, target: str = "main", **kwargs) -> dict:
    stop_backend(root, run_id, target, timeout=float(kwargs.pop("timeout", 5.0)))
    return start_backend(root, run_id, target, **kwargs) | {"restarted": True}


def format_backend_status(state: dict) -> str:
    parts = [
        f"Backend: {state.get('status', 'stopped')}",
        f"Target: {state.get('target', 'main')}",
        f"Runner: {state.get('backend', 'codex-chat')}",
        f"PID: {state.get('pid') or '-'}",
        f"Managed: {'yes' if state.get('managed') else 'no'}",
    ]
    if state.get("last_reply_at"):
        parts.append(f"Last reply: {state['last_reply_at']}")
    if state.get("log_path"):
        parts.append(f"Log: {state['log_path']}")
    return "\n".join(parts)
