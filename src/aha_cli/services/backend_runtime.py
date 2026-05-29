from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import zipfile

from aha_cli.backends.claude import apply_claude_environment, claude_cli_model, claude_config_for_model, claude_resolved_model
from aha_cli.backends.codex import apply_codex_environment, codex_cli_model, codex_config_for_model, codex_resolved_model
from aha_cli.backends.registry import resolve_model
from aha_cli.domain.models import utc_now
from aha_cli.services.commit_policy import generated_by_for_backend_model
from aha_cli.services.context_pressure import context_pressure
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.services.proxy import apply_proxy_environment, proxy_env_for_agent
from aha_cli.store.filesystem import (
    append_event,
    event_path,
    iter_jsonl_reverse,
    load_config,
    read_json,
    require_plan,
    run_dir,
    session_path,
    write_json,
)

BACKEND_ACTIVITY_SCAN_LIMIT = 5000
CODEX_CONTEXT_WINDOW_SCAN_LIMIT = 1000
PROCESS_AGENT_BACKENDS = {"codex", "claude"}


def safe_target(target: str) -> str:
    return (target or "main").replace("/", "_")


def backend_key(target: str = "main", task_id: str | None = None) -> str:
    target_name = safe_target(target)
    if task_id:
        return f"{safe_target(task_id)}-{target_name}"
    return target_name


def backend_state_path(root: Path, run_id: str, target: str = "main", task_id: str | None = None) -> Path:
    return run_dir(root, run_id) / "runtime" / f"backend-{backend_key(target, task_id)}.json"


def backend_log_path(root: Path, run_id: str, target: str = "main", task_id: str | None = None) -> Path:
    return run_dir(root, run_id) / "logs" / f"backend-{backend_key(target, task_id)}.log"


def backend_lock_path(root: Path, run_id: str, target: str = "main", task_id: str | None = None) -> Path:
    return run_dir(root, run_id) / "runtime" / f"backend-{backend_key(target, task_id)}.lock"


@contextmanager
def locked_backend(root: Path, run_id: str, target: str = "main", task_id: str | None = None):
    lock_path = backend_lock_path(root, run_id, target, task_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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


def _read_state(root: Path, run_id: str, target: str, task_id: str | None = None) -> dict:
    path = backend_state_path(root, run_id, target, task_id)
    if not path.exists():
        return {}
    try:
        return read_json(path)
    except (OSError, ValueError):
        return {}


def _write_state(root: Path, run_id: str, target: str, state: dict, task_id: str | None = None) -> dict:
    write_json(backend_state_path(root, run_id, target, task_id), state)
    return state


def _event_time(event: dict) -> str:
    return str(event.get("ts", "") or "")


def _backend_activity(root: Path, run_id: str, target: str, task_id: str | None = None) -> dict:
    latest_started: dict | None = None
    latest_finished: dict | None = None
    latest_reply: dict | None = None
    latest_error: dict | None = None
    scanned = 0
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        scanned += 1
        if scanned > BACKEND_ACTIVITY_SCAN_LIMIT:
            break
        data = event.get("data") or {}
        if task_id and data.get("task_id") != task_id:
            continue
        if latest_started is None and event.get("type") == "agent_started" and data.get("target") == target:
            latest_started = event
        elif latest_finished is None and event.get("type") == "agent_finished" and data.get("target") == target:
            latest_finished = event
        elif latest_error is None and event.get("type") == "agent_error" and data.get("target") == target:
            latest_error = event
        elif latest_reply is None and event.get("type") == "message" and data.get("sender") == target:
            latest_reply = event
        if latest_started and latest_finished and latest_reply and latest_error:
            break
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


def _latest_agent_usage(root: Path, run_id: str, target: str, task_id: str | None = None) -> dict:
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        if event.get("type") != "agent_usage":
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if data.get("target") != target:
            continue
        if task_id and data.get("task_id") != task_id:
            continue
        if task_id is None and data.get("task_id"):
            continue
        usage = data.get("usage")
        return usage if isinstance(usage, dict) else {}
    return {}


def _latest_agent_prompt_metrics(root: Path, run_id: str, target: str, task_id: str | None = None) -> dict:
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        if event.get("type") != "agent_prompt_metrics":
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if data.get("target") != target:
            continue
        if task_id and data.get("task_id") != task_id:
            continue
        if task_id is None and data.get("task_id"):
            continue
        return data
    return {}


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value).replace("_", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _codex_session_jsonl_path(session_id: str) -> Path | None:
    safe_id = str(session_id or "").strip()
    if not safe_id:
        return None
    candidates = list((Path.home() / ".codex" / "sessions").glob(f"**/*{safe_id}.jsonl"))
    return candidates[0] if candidates else None


def _codex_token_count_info(record: dict) -> dict:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    if payload.get("type") == "token_count" and info:
        return info
    if record.get("type") == "token_count":
        record_info = record.get("info") if isinstance(record.get("info"), dict) else {}
        return record_info or info or payload
    if info and (info.get("model_context_window") or info.get("last_token_usage")):
        return info
    if payload.get("model_context_window") or payload.get("last_token_usage"):
        return payload
    return {}


def _codex_runtime_context(root: Path, run_id: str, target: str, task_id: str | None = None) -> dict:
    session_file = session_path(root, run_id, task_id, target)
    if not session_file.exists():
        return {}
    try:
        session = read_json(session_file)
    except (OSError, ValueError):
        return {}
    path = _codex_session_jsonl_path(str(session.get("backend_session_id") or ""))
    if not path:
        return {}
    scanned = 0
    for _offset, record in iter_jsonl_reverse(path) or ():
        scanned += 1
        info = _codex_token_count_info(record)
        if info:
            usage = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
            return {
                "context_window": _positive_int(info.get("model_context_window")),
                "last_token_usage": {
                    key: value
                    for key, value in {
                        "input_tokens": _positive_int(usage.get("input_tokens")),
                        "cached_input_tokens": _positive_int(usage.get("cached_input_tokens")),
                        "output_tokens": _positive_int(usage.get("output_tokens")),
                        "reasoning_output_tokens": _positive_int(usage.get("reasoning_output_tokens")),
                        "total_tokens": _positive_int(usage.get("total_tokens")),
                    }.items()
                    if value is not None
                },
                "source": "runtime",
            }
        if scanned >= CODEX_CONTEXT_WINDOW_SCAN_LIMIT:
            break
    return {}


def _process_matches_task(parts: list[str], task_id: str | None) -> bool:
    if "--task-id" not in parts:
        return task_id is None
    if task_id is None:
        return False
    index = parts.index("--task-id")
    return len(parts) > index + 1 and parts[index + 1] == task_id


def _process_matches_home(parts: list[str], root: Path) -> bool:
    if "--home" not in parts:
        return False
    index = parts.index("--home")
    if len(parts) <= index + 1:
        return False
    try:
        process_home = Path(parts[index + 1]).expanduser().resolve()
        expected_home = root.expanduser().resolve()
    except OSError:
        return False
    return process_home == expected_home


def _backend_name_from_state(state: dict, fallback: str = "unknown") -> str:
    return str(state.get("backend") or fallback)


def _discover_backend_process(root: Path, run_id: str, target: str, task_id: str | None = None) -> tuple[int, str] | None:
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
        chat_commands = [command for command in ("codex-chat", "claude-chat") if command in parts]
        if not chat_commands:
            continue
        index = parts.index(chat_commands[0])
        if (
            len(parts) > index + 2
            and parts[index + 1] == run_id
            and parts[index + 2] == target
            and _process_matches_task(parts, task_id)
            and _process_matches_home(parts, root)
            and pid_is_running(pid)
        ):
            return pid, chat_commands[0]
    return None


def backend_status(root: Path, run_id: str, target: str = "main", task_id: str | None = None) -> dict:
    require_plan(root, run_id)
    target = target or "main"
    task_id = task_id or None
    state = _read_state(root, run_id, target, task_id)
    state_pid = int(state.get("pid") or 0) or None
    pid = None if state.get("status") == "stopped" else state_pid
    managed = bool(state.get("managed")) if state else False
    running = pid_is_running(pid)
    discovered_backend = None
    discovered = None if running else _discover_backend_process(root, run_id, target, task_id)
    if discovered:
        pid, discovered_backend = discovered
        running = True
        managed = bool(state.get("managed")) if state and state.get("pid") == pid else False
    activity = _backend_activity(root, run_id, target, task_id)
    status = "busy" if running and activity["busy"] else "running" if running else "stopped"
    backend_name = _backend_name_from_state(state, discovered_backend or "unknown")
    resolved_model = state.get("resolved_model") or state.get("model")
    latest_usage = _latest_agent_usage(root, run_id, target, task_id)
    latest_prompt_metrics = _latest_agent_prompt_metrics(root, run_id, target, task_id)
    runtime_context = (
        _codex_runtime_context(root, run_id, target, task_id)
        if str(backend_name).removesuffix("-chat") == "codex"
        else {}
    )
    runtime_context_window = (
        _positive_int(runtime_context.get("context_window"))
        or _positive_int(latest_usage.get("context_window"))
        or _positive_int(latest_usage.get("model_context_window"))
    )
    runtime_context_usage = runtime_context.get("last_token_usage") if isinstance(runtime_context.get("last_token_usage"), dict) else {}
    normalized_backend_name = str(backend_name).removesuffix("-chat")
    pressure_runtime_usage = runtime_context_usage or (latest_usage if normalized_backend_name == "claude" else {})
    return {
        "target": target,
        "task_id": task_id,
        "backend": backend_name,
        "status": status,
        "pid": pid if running else None,
        "last_pid": state_pid if not running else None,
        "managed": managed,
        "started_at": state.get("started_at"),
        "stopped_at": state.get("stopped_at"),
        "log_path": state.get("log_path") or str(backend_log_path(root, run_id, target, task_id)),
        "command": state.get("command", []),
        "model": state.get("model"),
        "requested_model": state.get("requested_model"),
        "resolved_model": state.get("resolved_model"),
        "runtime_context_window": runtime_context_window,
        "runtime_context_usage": pressure_runtime_usage,
        "latest_usage": latest_usage,
        "latest_prompt_metrics": latest_prompt_metrics,
        "context_pressure": context_pressure(
            backend_name,
            str(resolved_model) if resolved_model else None,
            latest_prompt_metrics,
            runtime_context_window=runtime_context_window,
            runtime_token_usage=pressure_runtime_usage,
            cfg=load_config(root),
        ),
        **activity,
    }


def mark_backend_stopped(root: Path, run_id: str, target: str = "main", *, task_id: str | None = None, pid: int | None = None) -> dict:
    task_id = task_id or None
    target = target or "main"
    with locked_backend(root, run_id, target, task_id):
        state = _read_state(root, run_id, target, task_id)
        state_pid = int(state.get("pid") or 0) or None
        previous_pid = int(pid or state_pid or 0) or None
        if pid and state_pid and state_pid != int(pid) and state.get("status") != "stopped":
            append_event(
                root,
                run_id,
                "backend_stop_ignored",
                {"target": target, "task_id": task_id, "pid": pid, "current_pid": state_pid},
            )
            return backend_status(root, run_id, target, task_id) | {"stale_stop_ignored": True}
        state.update(
            {
                "target": target,
                "task_id": task_id,
                "backend": _backend_name_from_state(state),
                "status": "stopped",
                "pid": previous_pid,
                "managed": bool(state),
                "stopped_at": utc_now(),
                "log_path": state.get("log_path") or str(backend_log_path(root, run_id, target, task_id)),
                "command": state.get("command", []),
            }
        )
        _write_state(root, run_id, target, state, task_id)
        append_event(root, run_id, "backend_stopped", {"target": target, "task_id": task_id, "pid": previous_pid})
        return backend_status(root, run_id, target, task_id) | {"stopped": True}

def stop_task_backends(root: Path, run_id: str, task_id: str, *, exclude_pid: int | None = None, timeout: float = 5.0) -> list[dict]:
    plan = require_plan(root, run_id)
    task = next((item for item in plan.get("tasks", []) if item.get("id") == task_id), None)
    if not task:
        return []
    stopped: list[dict] = []
    for agent in task.get("agents", []):
        target = str(agent.get("id") or "main")
        state = backend_status(root, run_id, target, task_id)
        pid = int(state.get("pid") or 0) or None
        if not pid or state.get("status") == "stopped":
            continue
        if exclude_pid and pid == int(exclude_pid):
            continue
        stopped.append(stop_backend(root, run_id, target, task_id=task_id, timeout=timeout))
    if stopped:
        append_event(
            root,
            run_id,
            "task_backends_stopped",
            {
                "task_id": task_id,
                "count": len(stopped),
                "targets": [item.get("target") for item in stopped],
            },
        )
    return stopped


def _running_zipapp_path() -> Path | None:
    raw_path = sys.argv[0] if sys.argv else ""
    if not raw_path:
        return None
    try:
        candidate = Path(raw_path).expanduser().resolve()
    except OSError:
        return None
    try:
        if candidate.is_file() and zipfile.is_zipfile(candidate):
            return candidate
    except OSError:
        return None
    return None


def _aha_cli_invocation() -> list[str]:
    zipapp_path = _running_zipapp_path()
    if zipapp_path:
        return [sys.executable, str(zipapp_path)]
    return [sys.executable, "-m", "aha_cli"]


def _agent_chat_command(
    run_id: str,
    target: str,
    *,
    backend: str = "codex",
    aha_home: Path,
    codex_bin: str = "codex",
    claude_bin: str = "claude",
    model: str | None = None,
    sandbox: str = "workspace-write",
    approval: str = "never",
    interval: float = 1.0,
    from_start: bool = False,
    no_json: bool = False,
    extra_args: list[str] | None = None,
    prompt_prefix: str = render_prompt_template("backend_prompt_prefix.md").strip(),
    task_id: str | None = None,
) -> list[str]:
    if backend not in PROCESS_AGENT_BACKENDS:
        raise ValueError(f"backend {backend} does not have a chat process")
    command_model = resolve_model(backend, model)
    command = [
        *_aha_cli_invocation(),
        "--home",
        str(aha_home),
        f"{backend}-chat",
        run_id,
        target,
        "--sender",
        target,
        "--sandbox",
        sandbox,
        "--approval",
        approval,
        "--interval",
        str(interval),
        "--prompt-prefix",
        prompt_prefix,
    ]
    if backend == "codex":
        command.extend(["--codex-bin", codex_bin])
    else:
        command.extend(["--claude-bin", claude_bin])
    if task_id:
        command.extend(["--task-id", task_id])
    if command_model:
        command.extend(["--model", command_model])
        if backend == "codex" and not model:
            command.extend(["--requested-model", ""])
    if from_start:
        command.append("--from-start")
    if no_json and backend == "codex":
        command.append("--no-json")
    for item in extra_args or []:
        command.extend(["--extra-arg", item])
    return command


def _backend_proxy_env(root: Path, run_id: str, target: str, task_id: str | None) -> dict[str, str] | None:
    if not task_id:
        return None
    try:
        plan = require_plan(root, run_id)
    except SystemExit:
        return None
    task = next((item for item in plan.get("tasks", []) if item.get("id") == task_id), None)
    if not task:
        return None
    agent = next((item for item in task.get("agents", []) if item.get("id") == target), None)
    if not agent:
        return None
    return proxy_env_for_agent(agent, task)


def _backend_process_env(
    proxy_env: dict[str, str] | None = None,
    claude_config: dict | None = None,
    codex_config: dict | None = None,
    aha_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    if pythonpath:
        cwd = Path.cwd()
        env["PYTHONPATH"] = os.pathsep.join(
            str((cwd / item).resolve()) if item and not Path(item).is_absolute() else item
            for item in pythonpath.split(os.pathsep)
        )
    _add_user_backend_paths(env)
    apply_codex_environment(env, codex_config)
    apply_claude_environment(env, claude_config)
    apply_proxy_environment(env, proxy_env)
    if aha_env:
        env.update({key: value for key, value in aha_env.items() if value})
    return env


def _add_user_backend_paths(env: dict[str, str]) -> None:
    home = Path.home()
    candidates = [
        home / ".local" / "bin",
        home / ".npm-global" / "bin",
    ]
    nvm_root = home / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        candidates.extend(sorted(nvm_root.glob("*/bin"), reverse=True))

    existing = [item for item in env.get("PATH", "").split(os.pathsep) if item]
    merged: list[str] = []
    seen: set[str] = set()
    for path in [str(candidate) for candidate in candidates if candidate.is_dir()] + existing:
        if path in seen:
            continue
        seen.add(path)
        merged.append(path)
    if merged:
        env["PATH"] = os.pathsep.join(merged)


def start_backend(
    root: Path,
    run_id: str,
    target: str = "main",
    *,
    backend: str = "codex",
    codex_bin: str = "codex",
    claude_bin: str = "claude",
    model: str | None = None,
    sandbox: str = "workspace-write",
    approval: str = "never",
    interval: float = 1.0,
    from_start: bool = False,
    no_json: bool = False,
    extra_args: list[str] | None = None,
    prompt_prefix: str = render_prompt_template("backend_prompt_prefix.md").strip(),
    task_id: str | None = None,
) -> dict:
    task_id = task_id or None
    target = target or "main"
    if backend not in PROCESS_AGENT_BACKENDS:
        raise ValueError(f"backend {backend} does not have a chat process")
    cfg = load_config(root)
    if backend == "codex" and not model:
        model = (cfg.get("codex", {}) or {}).get("model")
    if backend == "claude" and not model:
        model = (cfg.get("claude", {}) or {}).get("model")
    requested_model = model
    codex_config = codex_config_for_model((cfg.get("codex", {}) or {}), model) if backend == "codex" else None
    claude_config = claude_config_for_model((cfg.get("claude", {}) or {}), model) if backend == "claude" else None
    command_model = (
        claude_cli_model(model)
        if backend == "claude"
        else codex_cli_model(codex_config, model)
        if backend == "codex"
        else model
    )
    resolved_model = claude_resolved_model(claude_config, model) if backend == "claude" else codex_resolved_model(codex_config, model) if backend == "codex" else resolve_model(backend, command_model)
    with locked_backend(root, run_id, target, task_id):
        current = backend_status(root, run_id, target, task_id)
        if current["status"] in {"running", "busy"}:
            current["already_running"] = True
            return current
        command = _agent_chat_command(
            run_id,
            target,
            backend=backend,
            aha_home=root,
            codex_bin=codex_bin,
            claude_bin=claude_bin,
            model=model,
            sandbox=sandbox,
            approval=approval,
            interval=interval,
            from_start=from_start,
            no_json=no_json,
            extra_args=extra_args,
            prompt_prefix=prompt_prefix,
            task_id=task_id,
        )
        log_path = backend_log_path(root, run_id, target, task_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("ab")
        proxy_env = _backend_proxy_env(root, run_id, target, task_id)
        aha_env = {
            "AHA_ROOT": str(root),
            "AHA_RUN_ID": run_id,
            "AHA_AGENT_ID": target,
            "AHA_BACKEND": backend,
            "AHA_MODEL": resolved_model or "",
            "AHA_GENERATED_BY": generated_by_for_backend_model(backend, resolved_model),
        }
        if task_id:
            aha_env["AHA_TASK_ID"] = task_id
        try:
            process = subprocess.Popen(
                command,
                cwd=root,
                env=_backend_process_env(proxy_env, claude_config, codex_config, aha_env),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            log_file.close()
        state = {
            "target": target,
            "task_id": task_id,
            "backend": f"{backend}-chat",
            "status": "running",
            "pid": process.pid,
            "managed": True,
            "started_at": utc_now(),
            "stopped_at": None,
            "log_path": str(log_path),
            "command": command,
            "sandbox": sandbox,
            "approval": approval,
            "model": resolved_model,
            "requested_model": requested_model,
            "resolved_model": resolved_model,
            "from_start": from_start,
            "proxy_enabled": proxy_env is not None and bool(proxy_env),
        }
        _write_state(root, run_id, target, state, task_id)
        append_event(
            root,
            run_id,
            "backend_started",
            {
                "target": target,
                "task_id": task_id,
                "pid": process.pid,
                "log_path": str(log_path),
                "requested_model": requested_model,
                "resolved_model": resolved_model,
            },
        )
        return backend_status(root, run_id, target, task_id) | {"started": True}


def stop_backend(root: Path, run_id: str, target: str = "main", *, task_id: str | None = None, timeout: float = 5.0) -> dict:
    task_id = task_id or None
    target = target or "main"
    with locked_backend(root, run_id, target, task_id):
        current = backend_status(root, run_id, target, task_id)
        pid = current.get("pid")
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
        state = _read_state(root, run_id, target, task_id)
        state.update(
            {
                "target": target,
                "task_id": task_id,
                "backend": _backend_name_from_state(state),
                "status": "stopped",
                "pid": pid,
                "managed": bool(state),
                "stopped_at": utc_now(),
                "log_path": state.get("log_path") or str(backend_log_path(root, run_id, target, task_id)),
                "command": state.get("command", []),
            }
        )
        _write_state(root, run_id, target, state, task_id)
        append_event(root, run_id, "backend_stopped", {"target": target, "task_id": task_id, "pid": pid})
        return backend_status(root, run_id, target, task_id) | {"stopped": True}
