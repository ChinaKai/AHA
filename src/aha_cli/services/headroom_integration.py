from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

from aha_cli.backends.codex import (
    codex_config_env,
    codex_config_for_model,
    codex_config_with_provider_override,
    codex_config_overrides,
    codex_litellm_responses_bridge_config,
)
from aha_cli.domain.models import (
    normalize_headroom_integration_config,
    normalize_integrations_config,
    utc_now,
)
from aha_cli.services.proxy import DEFAULT_NO_PROXY, apply_proxy_environment
from aha_cli.store.io import iter_jsonl_from, read_json, write_json
from aha_cli.store.paths import aha_home_path, event_path

HEADROOM_HOST = "127.0.0.1"
HEADROOM_HEALTH_TIMEOUT_SECONDS = 1.0
HEADROOM_START_TIMEOUT_SECONDS = 12.0


def headroom_scope(run_id: object = None, task_id: object = None, agent_id: object = None) -> dict:
    return {
        "run_id": str(run_id or "run").strip() or "run",
        "task_id": str(task_id or "task").strip() or "task",
        "agent_id": str(agent_id or "agent").strip() or "agent",
    }


def headroom_config(config: dict | None) -> dict:
    integrations = normalize_integrations_config((config or {}).get("integrations"))
    return normalize_headroom_integration_config(integrations.get("headroom"))


def headroom_proxy_base_url(port: int) -> str:
    return f"http://{HEADROOM_HOST}:{port}/v1"


def _runtime_dir(root: Path) -> Path:
    return aha_home_path(root) / "runtime"


def _safe_scope_part(value: object, fallback: str) -> str:
    text = str(value or fallback).strip() or fallback
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in text)


def _scope_parts(scope: dict | None) -> tuple[str, str, str] | None:
    if not scope:
        return None
    return (
        _safe_scope_part(scope.get("run_id"), "run"),
        _safe_scope_part(scope.get("task_id"), "task"),
        _safe_scope_part(scope.get("agent_id"), "agent"),
    )


def _state_path(root: Path, scope: dict | None = None) -> Path:
    parts = _scope_parts(scope)
    if not parts:
        return _runtime_dir(root) / "headroom-proxy.json"
    run_id, task_id, agent_id = parts
    return _runtime_dir(root) / "headroom" / run_id / task_id / f"{agent_id}.json"


def _log_path(root: Path, scope: dict | None = None) -> Path:
    parts = _scope_parts(scope)
    if not parts:
        return aha_home_path(root) / "logs" / "headroom-proxy.log"
    run_id, task_id, agent_id = parts
    return aha_home_path(root) / "logs" / "headroom" / run_id / task_id / f"{agent_id}.log"


def _read_state(root: Path, scope: dict | None = None) -> dict:
    path = _state_path(root, scope)
    if not path.exists():
        return {}
    try:
        state = read_json(path)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _read_scoped_states(root: Path) -> list[dict]:
    base = _runtime_dir(root) / "headroom"
    states: list[dict] = []
    if not base.exists():
        return states
    for path in base.glob("*/*/*.json"):
        try:
            state = read_json(path)
        except (OSError, ValueError):
            continue
        if isinstance(state, dict):
            states.append(state)
    return states


def _resolve_command(command: object) -> str | None:
    raw = str(command or "").strip()
    if not raw:
        return None
    if "/" in raw:
        path = Path(raw).expanduser()
        return str(path) if path.exists() and os.access(path, os.X_OK) else None
    return shutil.which(raw)


def _process_alive(pid: object) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _headroom_health(port: int) -> bool:
    try:
        with urlopen(f"http://{HEADROOM_HOST}:{port}/livez", timeout=HEADROOM_HEALTH_TIMEOUT_SECONDS) as response:
            return 200 <= int(response.status) < 300
    except (OSError, URLError, ValueError):
        return False


def _port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((HEADROOM_HOST, port))
        return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HEADROOM_HOST, 0))
        return int(sock.getsockname()[1])


def _no_proxy_parts(value: object) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def merge_no_proxy_values(*values: object) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for value in (DEFAULT_NO_PROXY, *values):
        for item in _no_proxy_parts(value):
            key = item.lower()
            if key not in seen:
                seen.add(key)
                merged.append(item)
    return ",".join(merged)


def codex_proxy_env_for_headroom(proxy_env: dict[str, str] | None, local_base_url: str | None = None) -> dict[str, str]:
    env = dict(proxy_env or {})
    merged_no_proxy = merge_no_proxy_values(env.get("NO_PROXY"), env.get("no_proxy"))
    env["NO_PROXY"] = merged_no_proxy
    env["no_proxy"] = merged_no_proxy
    if local_base_url:
        env["OPENAI_BASE_URL"] = local_base_url
    return env


def _headroom_process_env(
    proxy_env: dict[str, str] | None,
    upstream_base_url: str | None,
    provider_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    apply_proxy_environment(env, codex_proxy_env_for_headroom(proxy_env))
    env.update({key: value for key, value in (provider_env or {}).items() if value})
    if upstream_base_url:
        env["OPENAI_TARGET_API_URL"] = upstream_base_url
    return env


def _headroom_command_args(command_path: str, config: dict, upstream_base_url: str | None) -> list[str]:
    args = [
        command_path,
        "proxy",
        "--host",
        HEADROOM_HOST,
        "--port",
        str(config.get("port") or 8787),
        "--mode",
        str(config.get("mode") or "token"),
        "--no-subscription-tracking",
    ]
    if not config.get("ccr_enabled"):
        args.extend(["--no-ccr-inject-tool", "--no-ccr-marker"])
    if upstream_base_url:
        args.extend(["--openai-api-url", upstream_base_url])
    return args


def _stop_state_process(root: Path, scope: dict | None = None) -> None:
    state = _read_state(root, scope)
    pid = state.get("pid")
    if not _process_alive(pid):
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, TypeError, ValueError):
        return
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.1)


def _state_matches_runtime(state: dict, config: dict, upstream_base_url: str | None) -> bool:
    if not state:
        return False
    return (
        str(state.get("mode") or "") == str(config.get("mode") or "")
        and bool(state.get("ccr_enabled")) == bool(config.get("ccr_enabled"))
        and str(state.get("command") or "") == str(config.get("command") or "")
        and str(state.get("upstream_base_url") or "") == str(upstream_base_url or "")
    )


def _healthy_state(root: Path, config_port: int, scope: dict | None) -> tuple[dict, bool, int]:
    state = _read_state(root, scope)
    port = int(state.get("port") or config_port)
    return state, _headroom_health(port), port


def _aggregate_state(root: Path, config_port: int) -> tuple[dict, bool, int, int]:
    states = _read_scoped_states(root)
    for state in states:
        port = int(state.get("port") or 0)
        if port and _headroom_health(port):
            return state, True, port, len(states)
    legacy = _read_state(root)
    legacy_port = int(legacy.get("port") or config_port)
    return legacy, _headroom_health(legacy_port), legacy_port, len(states)


def headroom_status(root: Path, config: dict | None, *, scope: dict | None = None) -> dict:
    integration = headroom_config(config)
    command_path = _resolve_command(integration.get("command"))
    config_port = int(integration.get("port") or 8787)
    if scope:
        state, healthy, port = _healthy_state(root, config_port, scope)
        scope_count = 1 if state else 0
    else:
        state, healthy, port, scope_count = _aggregate_state(root, config_port)
    state_pid = state.get("pid")
    return {
        "enabled": bool(integration.get("enabled")),
        "package": integration.get("package"),
        "command": integration.get("command"),
        "command_path": command_path,
        "installed": bool(command_path),
        "port": port,
        "mode": integration.get("mode"),
        "ccr_enabled": bool(integration.get("ccr_enabled")),
        "running": healthy,
        "healthy": healthy,
        "state_pid": state_pid,
        "state_pid_alive": _process_alive(state_pid),
        "scope": state.get("scope") or scope,
        "scope_count": scope_count,
        "log_path": state.get("log_path") or str(_log_path(root, scope)),
    }


def _headroom_usage_agent_row(agent_id: str) -> dict:
    return {
        "agent_id": agent_id,
        "ready_turns": 0,
        "skipped_turns": 0,
        "last_ready_at": "",
        "last_skipped_at": "",
        "last_reason": "",
    }


def _headroom_usage_task_row(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "enabled": False,
        "ready_turns": 0,
        "skipped_turns": 0,
        "last_ready_at": "",
        "last_skipped_at": "",
        "agents": {},
    }


def _headroom_usage_public_task(row: dict) -> dict:
    agents = sorted(
        row["agents"].values(),
        key=lambda item: (-int(item["ready_turns"]), -int(item["skipped_turns"]), str(item["agent_id"])),
    )
    return {
        "task_id": row["task_id"],
        "enabled": bool(row.get("enabled")),
        "ready_turns": row["ready_turns"],
        "skipped_turns": row["skipped_turns"],
        "last_ready_at": row["last_ready_at"],
        "last_skipped_at": row["last_skipped_at"],
        "agents": agents,
    }


def _headroom_enabled_plan_tasks(root: Path, run_id: str | None) -> list[dict]:
    del root, run_id
    return []


def _headroom_usage_task_agents(task: dict) -> list[str]:
    raw_agents = task.get("agents")
    if not isinstance(raw_agents, list):
        return ["main"]
    agents = [
        str(agent.get("id") or "").strip()
        for agent in raw_agents
        if isinstance(agent, dict) and str(agent.get("role") or "").strip() not in {"host", "supervision-host"}
    ]
    return [agent_id for agent_id in agents if agent_id] or ["main"]


def headroom_usage_summary(root: Path, run_id: str | None, *, task_limit: int = 8) -> dict:
    summary = {
        "run_id": str(run_id or ""),
        "enabled_tasks": 0,
        "ready_turns": 0,
        "skipped_turns": 0,
        "tasks": [],
        "task_count": 0,
    }
    if not run_id:
        return summary
    tasks: dict[str, dict] = {}
    for task in _headroom_enabled_plan_tasks(root, run_id):
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        summary["enabled_tasks"] += 1
        task_row = tasks.setdefault(task_id, _headroom_usage_task_row(task_id))
        task_row["enabled"] = True
        for agent_id in _headroom_usage_task_agents(task):
            task_row["agents"].setdefault(agent_id, _headroom_usage_agent_row(agent_id))
    events_file = event_path(root, run_id)
    if not events_file.exists():
        task_rows = sorted(
            (_headroom_usage_public_task(row) for row in tasks.values()),
            key=lambda item: (-int(item["ready_turns"]), not bool(item.get("enabled")), -int(item["skipped_turns"]), str(item["task_id"])),
        )
        summary["task_count"] = len(task_rows)
        summary["tasks"] = task_rows[: max(0, int(task_limit))]
        return summary
    events, _ = iter_jsonl_from(events_file, 0)
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type not in {"headroom_integration_ready", "headroom_integration_skipped"}:
            continue
        raw_data = event.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        task_id = str(data.get("task_id") or "").strip() or "run"
        agent_id = str(data.get("agent_id") or data.get("target") or "").strip() or "main"
        timestamp = str(event.get("ts") or data.get("ts") or "")
        reason = str(data.get("reason") or "")
        task_row = tasks.setdefault(task_id, _headroom_usage_task_row(task_id))
        agent_row = task_row["agents"].setdefault(agent_id, _headroom_usage_agent_row(agent_id))
        if event_type == "headroom_integration_ready" and bool(data.get("ready", True)):
            summary["ready_turns"] += 1
            task_row["ready_turns"] += 1
            agent_row["ready_turns"] += 1
            task_row["last_ready_at"] = timestamp
            agent_row["last_ready_at"] = timestamp
        else:
            summary["skipped_turns"] += 1
            task_row["skipped_turns"] += 1
            agent_row["skipped_turns"] += 1
            task_row["last_skipped_at"] = timestamp
            agent_row["last_skipped_at"] = timestamp
            agent_row["last_reason"] = reason
    task_rows = sorted(
        (_headroom_usage_public_task(row) for row in tasks.values()),
        key=lambda item: (-int(item["ready_turns"]), not bool(item.get("enabled")), -int(item["skipped_turns"]), str(item["task_id"])),
    )
    summary["task_count"] = len(task_rows)
    summary["tasks"] = task_rows[: max(0, int(task_limit))]
    return summary


def ensure_headroom_proxy(
    root: Path,
    config: dict,
    *,
    upstream_base_url: str | None = None,
    proxy_env: dict[str, str] | None = None,
    provider_env: dict[str, str] | None = None,
    scope: dict | None = None,
    workspace: Path | None = None,
) -> dict:
    integration = normalize_headroom_integration_config(config)
    status = headroom_status(root, {"integrations": {"headroom": integration}}, scope=scope)
    if not integration.get("enabled"):
        return {**status, "ready": False, "reason": "disabled"}
    command_path = status.get("command_path")
    if not command_path:
        return {**status, "ready": False, "reason": "command_not_found"}

    preferred_port = int(integration.get("port") or 8787)
    state = _read_state(root, scope)
    if state:
        state_port = int(state.get("port") or preferred_port)
        state_healthy = _headroom_health(state_port)
        if state_healthy and _state_matches_runtime(state, integration, upstream_base_url):
            return {
                **headroom_status(root, {"integrations": {"headroom": {**integration, "port": state_port}}}, scope=scope),
                "ready": True,
                "started": False,
            }
        _stop_state_process(root, scope)

    port = preferred_port if _port_available(preferred_port) else _free_port()
    runtime_config = {**integration, "port": port}
    status = headroom_status(root, {"integrations": {"headroom": runtime_config}}, scope=scope)

    log_path = _log_path(root, scope)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = _headroom_command_args(str(command_path), runtime_config, upstream_base_url)
    env = _headroom_process_env(proxy_env, upstream_base_url, provider_env)
    cwd = workspace if workspace and workspace.exists() else root
    try:
        log_handle = log_path.open("a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_handle.close()
    except OSError as exc:
        return {**status, "ready": False, "reason": "start_failed", "error": str(exc)}

    state = {
        "pid": process.pid,
        "host": HEADROOM_HOST,
        "port": port,
        "mode": runtime_config.get("mode"),
        "ccr_enabled": bool(runtime_config.get("ccr_enabled")),
        "upstream_base_url": upstream_base_url,
        "command": str(runtime_config.get("command") or ""),
        "command_path": str(command_path),
        "args": cmd,
        "cwd": str(cwd),
        "log_path": str(log_path),
        "scope": scope,
        "started_at": utc_now(),
    }
    try:
        _state_path(root, scope).parent.mkdir(parents=True, exist_ok=True)
        write_json(_state_path(root, scope), state)
    except OSError:
        pass

    deadline = time.monotonic() + HEADROOM_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _headroom_health(port):
            return {
                **headroom_status(root, {"integrations": {"headroom": runtime_config}}, scope=scope),
                "ready": True,
                "started": True,
                "pid": process.pid,
            }
        exit_code = process.poll()
        if exit_code is not None:
            return {**status, "ready": False, "reason": "exited", "exit_code": exit_code, "pid": process.pid}
        time.sleep(0.2)
    return {**status, "ready": False, "reason": "not_ready", "pid": process.pid}


def codex_upstream_base_url(codex_config: dict | None = None) -> str | None:
    selected_config = codex_config
    if isinstance(codex_config, dict) and not codex_config_overrides(codex_config):
        selected_model = codex_config.get("model")
        if not selected_model and str(codex_config.get("env_active") or "").strip():
            selected_model = f"env:{str(codex_config.get('env_active') or '').strip()}"
        selected_config = codex_config_for_model(codex_config, str(selected_model or "").strip() or None)
    for item in codex_config_overrides(selected_config):
        prefix = "model_providers."
        suffix = ".base_url="
        if item.startswith(prefix) and suffix in item:
            raw = item.split(suffix, 1)[1]
            try:
                value = str(json.loads(raw)).strip()
            except (TypeError, ValueError):
                value = raw.strip().strip('"')
            if value.startswith(f"http://{HEADROOM_HOST}:") or value.startswith("http://localhost:"):
                return None
            return value or None
    value = str(os.environ.get("OPENAI_BASE_URL") or "").strip()
    if value.startswith(f"http://{HEADROOM_HOST}:") or value.startswith(f"http://localhost:"):
        return None
    return value or None


def headroom_should_wrap_codex(config: dict | None, task: dict | None, backend_name: str) -> bool:
    del config, task, backend_name
    return False


def prepare_headroom_codex_runtime(
    root: Path,
    *,
    config: dict,
    task: dict,
    backend_name: str,
    codex_config: dict | None,
    proxy_env: dict[str, str] | None,
    run_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    workspace: Path | None = None,
) -> tuple[dict | None, dict[str, str] | None, dict]:
    if not headroom_should_wrap_codex(config, task, backend_name):
        return codex_config, proxy_env, {"enabled": False, "ready": False, "reason": "not_selected"}

    integration = headroom_config(config)
    scope = headroom_scope(run_id, task_id or (task or {}).get("id"), agent_id)
    selected_model = None
    if isinstance(codex_config, dict):
        selected_model = codex_config.get("model")
        if not selected_model and str(codex_config.get("env_active") or "").strip():
            selected_model = f"env:{str(codex_config.get('env_active') or '').strip()}"
    if codex_litellm_responses_bridge_config(codex_config, str(selected_model or "").strip() or None):
        return (
            codex_config,
            proxy_env,
            {
                "enabled": True,
                "ready": False,
                "reason": "litellm_bridge_provider",
                "scope": scope,
            },
        )
    upstream_base_url = codex_upstream_base_url(codex_config)
    provider_env = codex_config_env(codex_config)
    status = ensure_headroom_proxy(
        root,
        integration,
        upstream_base_url=upstream_base_url,
        proxy_env=proxy_env,
        provider_env=provider_env,
        scope=scope,
        workspace=workspace,
    )
    if not status.get("ready"):
        return codex_config, proxy_env, {**status, "enabled": True, "scope": scope, "upstream_base_url": upstream_base_url}

    local_base_url = headroom_proxy_base_url(int(status.get("port") or integration.get("port") or 8787))
    return (
        codex_config_with_provider_override(
            codex_config,
            provider_id="aha_headroom",
            name="AHA Headroom",
            base_url=local_base_url,
            wire_api="responses",
            requires_openai_auth=False,
        ),
        codex_proxy_env_for_headroom(proxy_env, local_base_url),
        {**status, "enabled": True, "ready": True, "scope": scope, "upstream_base_url": upstream_base_url, "local_base_url": local_base_url},
    )


__all__ = [
    "codex_proxy_env_for_headroom",
    "codex_upstream_base_url",
    "ensure_headroom_proxy",
    "headroom_config",
    "headroom_proxy_base_url",
    "headroom_scope",
    "headroom_should_wrap_codex",
    "headroom_status",
    "headroom_usage_summary",
    "merge_no_proxy_values",
    "prepare_headroom_codex_runtime",
]
