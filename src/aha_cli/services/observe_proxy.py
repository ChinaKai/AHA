from __future__ import annotations

import argparse
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zlib

from aha_cli.backends.claude import claude_config_env
from aha_cli.backends.codex import (
    codex_config_env,
    codex_config_with_observe_provider_override,
    codex_litellm_responses_bridge_config,
)
from aha_cli.domain.models import normalize_integrations_config, normalize_task_observe_proxy, utc_now
from aha_cli.services.headroom_integration import codex_upstream_base_url
from aha_cli.services.proxy import DEFAULT_NO_PROXY, apply_proxy_environment
from aha_cli.store.filesystem import append_event_to_file
from aha_cli.store.io import iter_jsonl_from, iter_jsonl_reverse, read_json, write_json
from aha_cli.store.paths import aha_home_path, event_path, plan_path, run_dir

OBSERVE_HOST = "127.0.0.1"
OBSERVE_START_TIMEOUT_SECONDS = 8.0
OBSERVE_HEALTH_TIMEOUT_SECONDS = 1.0
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
CODEX_CHATGPT_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"
ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
SENSITIVE_HEADERS = {"authorization", "x-api-key", "api-key", "cookie", "set-cookie"}


def observe_proxy_config(config: dict | None) -> dict:
    integrations = normalize_integrations_config((config or {}).get("integrations"))
    return integrations.get("observe_proxy") or {}


def observe_proxy_base_url(port: int, backend: str) -> str:
    suffix = "/v1" if backend == "codex" else ""
    return f"http://{OBSERVE_HOST}:{port}{suffix}"


def observe_proxy_scope(run_id: object = None, task_id: object = None, agent_id: object = None) -> dict:
    return {
        "run_id": str(run_id or "run").strip() or "run",
        "task_id": str(task_id or "task").strip() or "task",
        "agent_id": str(agent_id or "agent").strip() or "agent",
    }


def _safe_scope_part(value: object, fallback: str) -> str:
    text = str(value or fallback).strip() or fallback
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in text)


def _scope_parts(scope: dict | None) -> tuple[str, str, str]:
    scope = scope or {}
    return (
        _safe_scope_part(scope.get("run_id"), "run"),
        _safe_scope_part(scope.get("task_id"), "task"),
        _safe_scope_part(scope.get("agent_id"), "agent"),
    )


def _runtime_dir(root: Path) -> Path:
    return aha_home_path(root) / "runtime" / "observe_proxy"


def _state_path(root: Path, scope: dict | None) -> Path:
    run_id, task_id, agent_id = _scope_parts(scope)
    return _runtime_dir(root) / run_id / task_id / f"{agent_id}.json"


def _log_path(root: Path, scope: dict | None) -> Path:
    run_id, task_id, agent_id = _scope_parts(scope)
    return aha_home_path(root) / "logs" / "observe_proxy" / run_id / task_id / f"{agent_id}.log"


def _read_state(root: Path, scope: dict | None) -> dict:
    path = _state_path(root, scope)
    if not path.exists():
        return {}
    try:
        state = read_json(path)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


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


def _health(port: int) -> bool:
    try:
        with urlopen(f"http://{OBSERVE_HOST}:{port}/livez", timeout=OBSERVE_HEALTH_TIMEOUT_SECONDS) as response:
            if not 200 <= int(response.status) < 300:
                return False
            return response.read(32).strip() == b"ok"
    except (OSError, URLError, ValueError):
        return False


def _state_process_healthy(state: dict) -> bool:
    try:
        port = int(state.get("port") or 0)
    except (TypeError, ValueError):
        return False
    return _process_alive(state.get("pid")) and port > 0 and _health(port)


def _port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((OBSERVE_HOST, port))
        return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((OBSERVE_HOST, 0))
        return int(sock.getsockname()[1])


def _state_matches_runtime(state: dict, backend: str, upstream_base_url: str) -> bool:
    return (
        str(state.get("backend") or "") == backend
        and str(state.get("upstream_base_url") or "") == upstream_base_url
    )


def _stop_state_process(root: Path, scope: dict | None) -> None:
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


def observe_proxy_status(root: Path, config: dict | None, *, scope: dict | None = None) -> dict:
    integration = observe_proxy_config(config)
    configured_port = int(integration.get("port") or 8797)
    state = _read_state(root, scope)
    port = int(state.get("port") or configured_port)
    state_pid_alive = _process_alive(state.get("pid"))
    healthy = _health(port)
    if state:
        healthy = state_pid_alive and healthy
    return {
        "enabled": bool(integration.get("enabled")),
        "port": port,
        "running": healthy,
        "healthy": healthy,
        "state_pid": state.get("pid"),
        "state_pid_alive": state_pid_alive,
        "scope": state.get("scope") or scope,
        "backend": state.get("backend"),
        "upstream_base_url": state.get("upstream_base_url"),
        "local_base_url": state.get("local_base_url"),
        "log_path": state.get("log_path") or str(_log_path(root, scope)),
    }


def _observe_proxy_enabled_plan_tasks(root: Path, run_id: str | None) -> list[dict]:
    if not run_id:
        return []
    path = plan_path(root, run_id)
    if not path.exists():
        return []
    try:
        plan = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    tasks = plan.get("tasks") if isinstance(plan, dict) else []
    return [
        task
        for task in tasks
        if isinstance(task, dict)
        and not task.get("deleted_at")
        and bool(normalize_task_observe_proxy(task.get("observe_proxy")).get("enabled"))
    ]


def _observe_proxy_task_agents(task: dict) -> list[str]:
    agents = task.get("agents")
    if not isinstance(agents, list):
        return ["main"]
    values = [
        str(agent.get("id") or "").strip()
        for agent in agents
        if isinstance(agent, dict) and str(agent.get("role") or "").strip() not in {"host", "supervision-host"}
    ]
    return [value for value in values if value] or ["main"]


def _observe_proxy_agent_row(agent_id: str) -> dict:
    return {
        "agent_id": agent_id,
        "ready_turns": 0,
        "skipped_turns": 0,
        "requests": 0,
        "responses": 0,
        "last_ready_at": "",
        "last_skipped_at": "",
        "last_network_at": "",
        "last_reason": "",
    }


def _observe_proxy_task_row(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "enabled": False,
        "ready_turns": 0,
        "skipped_turns": 0,
        "requests": 0,
        "responses": 0,
        "last_ready_at": "",
        "last_skipped_at": "",
        "last_network_at": "",
        "agents": {},
    }


def _observe_proxy_public_task(row: dict) -> dict:
    agents = sorted(
        row["agents"].values(),
        key=lambda item: (-int(item["responses"]), -int(item["requests"]), str(item["agent_id"])),
    )
    return {
        "task_id": row["task_id"],
        "enabled": bool(row.get("enabled")),
        "ready_turns": row["ready_turns"],
        "skipped_turns": row["skipped_turns"],
        "requests": row["requests"],
        "responses": row["responses"],
        "last_ready_at": row["last_ready_at"],
        "last_skipped_at": row["last_skipped_at"],
        "last_network_at": row["last_network_at"],
        "agents": agents,
    }


def _content_encoding(headers: object) -> str:
    if not isinstance(headers, dict):
        return ""
    for key, value in headers.items():
        if str(key).lower() == "content-encoding":
            return str(value or "").strip().lower()
    return ""


def _decompress_preview_data(data: bytes, encoding: str) -> tuple[bytes | None, str]:
    value = str(encoding or "").strip().lower()
    if not value or value in {"identity", "none"}:
        return data, ""
    encodings = [item.strip() for item in value.split(",") if item.strip()]
    decoded = data
    for item in reversed(encodings):
        try:
            if item in {"gzip", "x-gzip"}:
                decoded = gzip.decompress(decoded)
            elif item == "deflate":
                try:
                    decoded = zlib.decompress(decoded)
                except zlib.error:
                    decoded = zlib.decompress(decoded, -zlib.MAX_WBITS)
            elif item == "br":
                try:
                    import brotli  # type: ignore[import-not-found]
                except ImportError:
                    return None, "compressed br body; preview unavailable"
                decoded = brotli.decompress(decoded)
            elif item in {"zstd", "zstandard"}:
                try:
                    import zstandard as zstd  # type: ignore[import-not-found]
                except ImportError:
                    return None, "compressed zstd body; preview unavailable"
                decoded = zstd.ZstdDecompressor().decompress(decoded)
            else:
                return None, f"compressed {item} body; preview unavailable"
        except Exception as exc:
            return None, f"failed to decode {item} body: {exc}"
    return decoded, ""


def _text_or_binary_preview(data: bytes, *, limit: int) -> tuple[str, bool]:
    preview_bytes = data[: max(0, int(limit))]
    text = preview_bytes.decode("utf-8", errors="replace")
    if not preview_bytes:
        return "", False
    replacement_count = text.count("\ufffd")
    allowed_controls = {"\n", "\r", "\t"}
    non_text_count = sum(1 for char in text if (ord(char) < 32 and char not in allowed_controls) or char == "\ufffd")
    if replacement_count > 0 or non_text_count / max(1, len(text)) > 0.05:
        return f"[binary body, {len(data)} B; preview unavailable]", False
    return text, len(data) > max(0, int(limit))


def _artifact_preview(root: Path, run_id: str, ref: object, *, limit: int = 2000, content_encoding: str | None = None) -> dict:
    ref_text = str(ref or "").strip()
    encoding = str(content_encoding or "").strip()
    result = {"ref": ref_text, "bytes": 0, "preview": "", "truncated": False, "content_encoding": encoding}
    if not ref_text:
        return result
    base = run_dir(root, run_id).resolve()
    path = (base / ref_text).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        return result
    if not path.exists() or not path.is_file():
        return result
    data = path.read_bytes()
    result["bytes"] = len(data)
    decoded, decode_error = _decompress_preview_data(data, encoding)
    if decoded is None:
        result["preview"] = f"[{decode_error}; {len(data)} B]"
        return result
    result["preview"], result["truncated"] = _text_or_binary_preview(decoded, limit=limit)
    return result


def observe_proxy_usage_summary(
    root: Path,
    run_id: str | None,
    *,
    task_limit: int = 8,
    event_limit: int = 20,
    preview_chars: int = 2000,
    include_recent: bool = True,
    recent_task_id: str | None = None,
) -> dict:
    summary = {
        "run_id": str(run_id or ""),
        "enabled_tasks": 0,
        "ready_turns": 0,
        "skipped_turns": 0,
        "requests": 0,
        "responses": 0,
        "tasks": [],
        "task_count": 0,
        "recent": [],
    }
    if not run_id:
        return summary
    tasks: dict[str, dict] = {}
    for task in _observe_proxy_enabled_plan_tasks(root, run_id):
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        summary["enabled_tasks"] += 1
        task_row = tasks.setdefault(task_id, _observe_proxy_task_row(task_id))
        task_row["enabled"] = True
        for agent_id in _observe_proxy_task_agents(task):
            task_row["agents"].setdefault(agent_id, _observe_proxy_agent_row(agent_id))

    events_file = event_path(root, run_id)
    requested_task_id = str(recent_task_id or "").strip()
    if events_file.exists():
        events, _ = iter_jsonl_from(events_file, 0)
        for event in events:
            event_type = str(event.get("type") or "")
            if event_type not in {"observe_proxy_ready", "observe_proxy_skipped", "agent_network_request", "agent_network_response"}:
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            task_id = str(data.get("task_id") or "").strip() or "run"
            agent_id = str(data.get("agent_id") or data.get("target") or "").strip() or "main"
            timestamp = str(event.get("ts") or data.get("ts") or "")
            task_row = tasks.setdefault(task_id, _observe_proxy_task_row(task_id))
            agent_row = task_row["agents"].setdefault(agent_id, _observe_proxy_agent_row(agent_id))
            if event_type == "observe_proxy_ready" and bool(data.get("ready", True)):
                summary["ready_turns"] += 1
                task_row["ready_turns"] += 1
                agent_row["ready_turns"] += 1
                task_row["last_ready_at"] = timestamp
                agent_row["last_ready_at"] = timestamp
            elif event_type == "observe_proxy_skipped":
                summary["skipped_turns"] += 1
                task_row["skipped_turns"] += 1
                agent_row["skipped_turns"] += 1
                task_row["last_skipped_at"] = timestamp
                agent_row["last_skipped_at"] = timestamp
                agent_row["last_reason"] = str(data.get("reason") or "")
            elif event_type == "agent_network_request":
                summary["requests"] += 1
                task_row["requests"] += 1
                agent_row["requests"] += 1
                task_row["last_network_at"] = timestamp
                agent_row["last_network_at"] = timestamp
            elif event_type == "agent_network_response":
                summary["responses"] += 1
                task_row["responses"] += 1
                agent_row["responses"] += 1
                task_row["last_network_at"] = timestamp
                agent_row["last_network_at"] = timestamp

        if include_recent and event_limit > 0:
            recent_by_id: dict[str, dict] = {}
            recent_order: list[str] = []
            scanned = 0
            for _offset, event in iter_jsonl_reverse(events_file) or ():
                scanned += 1
                event_type = str(event.get("type") or "")
                if event_type not in {"agent_network_request", "agent_network_response"}:
                    if len(recent_order) >= event_limit and scanned > event_limit * 20:
                        break
                    continue
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                request_id = str(data.get("request_id") or "").strip()
                if not request_id:
                    continue
                if event_type == "agent_network_response":
                    event_task_id = str(data.get("task_id") or "").strip() or "run"
                    if requested_task_id and event_task_id != requested_task_id:
                        continue
                    if request_id not in recent_by_id and len(recent_order) < max(0, int(event_limit)):
                        recent_by_id[request_id] = {"request_id": request_id, "response_event": event}
                        recent_order.append(request_id)
                    elif request_id in recent_by_id and "response_event" not in recent_by_id[request_id]:
                        recent_by_id[request_id]["response_event"] = event
                elif request_id in recent_by_id:
                    recent_by_id[request_id]["request_event"] = event
                if recent_order and len(recent_order) >= event_limit and all("request_event" in recent_by_id[item] for item in recent_order):
                    break
                if len(recent_order) >= event_limit and scanned > event_limit * 40:
                    break

            recent = []
            for request_id in recent_order:
                pair = recent_by_id.get(request_id) or {}
                response_event = pair.get("response_event") or {}
                request_event = pair.get("request_event") or {}
                response_data = response_event.get("data") if isinstance(response_event.get("data"), dict) else {}
                request_data = request_event.get("data") if isinstance(request_event.get("data"), dict) else {}
                recent.append(
                    {
                        "request_id": request_id,
                        "task_id": response_data.get("task_id") or request_data.get("task_id"),
                        "agent_id": response_data.get("agent_id") or request_data.get("agent_id") or response_data.get("target") or request_data.get("target"),
                        "backend": response_data.get("backend") or request_data.get("backend"),
                        "method": request_data.get("method"),
                        "path": request_data.get("path"),
                        "status": response_data.get("status"),
                        "duration_ms": response_data.get("duration_ms"),
                        "request_bytes": request_data.get("request_bytes"),
                        "response_bytes": response_data.get("response_bytes"),
                        "usage": response_data.get("usage"),
                        "ts": response_event.get("ts") or request_event.get("ts"),
                        "request": _artifact_preview(
                            root,
                            str(run_id),
                            request_data.get("request_ref"),
                            limit=preview_chars,
                            content_encoding=_content_encoding(request_data.get("headers")),
                        ),
                        "response": _artifact_preview(
                            root,
                            str(run_id),
                            response_data.get("response_ref"),
                            limit=preview_chars,
                            content_encoding=_content_encoding(response_data.get("headers")),
                        ),
                    }
                )
            summary["recent"] = recent

    task_rows = sorted(
        (_observe_proxy_public_task(row) for row in tasks.values()),
        key=lambda item: (-int(item["responses"]), not bool(item.get("enabled")), -int(item["requests"]), str(item["task_id"])),
    )
    summary["task_count"] = len(task_rows)
    summary["tasks"] = task_rows[: max(0, int(task_limit))]
    return summary


def _no_proxy_parts(value: object) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _merge_no_proxy_values(*values: object) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for value in (DEFAULT_NO_PROXY, *values):
        for item in _no_proxy_parts(value):
            key = item.lower()
            if key not in seen:
                seen.add(key)
                merged.append(item)
    return ",".join(merged)


def _with_local_no_proxy(env: dict[str, str] | None) -> dict[str, str]:
    result = dict(env or {})
    merged = _merge_no_proxy_values(result.get("NO_PROXY"), result.get("no_proxy"))
    result["NO_PROXY"] = merged
    result["no_proxy"] = merged
    return result


def codex_proxy_env_for_observe(proxy_env: dict[str, str] | None, local_base_url: str | None = None) -> dict[str, str]:
    env = _with_local_no_proxy(proxy_env)
    if local_base_url:
        env["OPENAI_BASE_URL"] = local_base_url
    return env


def claude_proxy_env_for_observe(proxy_env: dict[str, str] | None, local_base_url: str | None = None) -> dict[str, str]:
    env = _with_local_no_proxy(proxy_env)
    if local_base_url:
        env["ANTHROPIC_BASE_URL"] = local_base_url
    return env


def _current_aha_command() -> list[str]:
    executable = str(sys.argv[0] or "").strip()
    if executable and Path(executable).exists():
        return [executable]
    resolved = shutil.which("aha")
    if resolved:
        return [resolved]
    return [sys.executable, "-m", "aha_cli"]


def _observe_proxy_command_args(
    root: Path,
    run_id: str,
    task_id: str | None,
    agent_id: str,
    backend: str,
    host: str,
    port: int,
    upstream_base_url: str,
) -> list[str]:
    args = [
        *_current_aha_command(),
        "--home",
        str(aha_home_path(root)),
        "observe-proxy",
        "--run-id",
        run_id,
        "--agent-id",
        agent_id,
        "--backend",
        backend,
        "--host",
        host,
        "--port",
        str(port),
        "--upstream-base-url",
        upstream_base_url,
    ]
    if task_id:
        args.extend(["--task-id", task_id])
    return args


def ensure_observe_proxy(
    root: Path,
    config: dict,
    *,
    backend: str,
    upstream_base_url: str,
    run_id: str,
    task_id: str | None,
    agent_id: str,
    proxy_env: dict[str, str] | None = None,
    provider_env: dict[str, str] | None = None,
    scope: dict | None = None,
    workspace: Path | None = None,
) -> dict:
    integration = observe_proxy_config({"integrations": {"observe_proxy": config}})
    status = observe_proxy_status(root, {"integrations": {"observe_proxy": integration}}, scope=scope)
    if not integration.get("enabled"):
        return {**status, "ready": False, "reason": "disabled"}

    preferred_port = int(integration.get("port") or 8797)
    state = _read_state(root, scope)
    if state:
        state_port = int(state.get("port") or preferred_port)
        if _state_process_healthy(state) and _state_matches_runtime(state, backend, upstream_base_url):
            return {
                **observe_proxy_status(root, {"integrations": {"observe_proxy": {**integration, "port": state_port}}}, scope=scope),
                "ready": True,
                "started": False,
            }
        _stop_state_process(root, scope)

    port = preferred_port if _port_available(preferred_port) else _free_port()
    runtime_config = {**integration, "port": port}
    local_base_url = observe_proxy_base_url(port, backend)
    log_path = _log_path(root, scope)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = _observe_proxy_command_args(root, run_id, task_id, agent_id, backend, OBSERVE_HOST, port, upstream_base_url)
    env = os.environ.copy()
    apply_proxy_environment(env, _with_local_no_proxy(proxy_env))
    env.update({key: value for key, value in (provider_env or {}).items() if value})
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
        "host": OBSERVE_HOST,
        "port": port,
        "backend": backend,
        "upstream_base_url": upstream_base_url,
        "local_base_url": local_base_url,
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

    deadline = time.monotonic() + OBSERVE_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _health(port):
            return {
                **observe_proxy_status(root, {"integrations": {"observe_proxy": runtime_config}}, scope=scope),
                "ready": True,
                "started": True,
                "pid": process.pid,
                "local_base_url": local_base_url,
            }
        exit_code = process.poll()
        if exit_code is not None:
            return {**status, "ready": False, "reason": "exited", "exit_code": exit_code, "pid": process.pid}
        time.sleep(0.2)
    return {**status, "ready": False, "reason": "not_ready", "pid": process.pid}


def observe_proxy_should_wrap(config: dict | None, task: dict | None, backend_name: str) -> bool:
    del config
    task_policy = normalize_task_observe_proxy((task or {}).get("observe_proxy"))
    return bool(task_policy.get("enabled")) and str(backend_name or "").strip().lower() in {"codex", "claude"}


def _local_url(value: str) -> bool:
    return value.startswith(f"http://{OBSERVE_HOST}:") or value.startswith("http://localhost:")


def claude_upstream_base_url(claude_config: dict | None = None) -> str:
    value = str(claude_config_env(claude_config).get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or "").strip()
    if value and not _local_url(value):
        return value
    return ANTHROPIC_DEFAULT_BASE_URL


def _codex_auth_mode() -> str:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    auth_path = codex_home / "auth.json"
    try:
        data = read_json(auth_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("auth_mode") or "").strip().lower()


def codex_observe_upstream_base_url(codex_config: dict | None = None) -> str:
    explicit = codex_upstream_base_url(codex_config)
    if explicit:
        return explicit
    if _codex_auth_mode() == "chatgpt":
        return CODEX_CHATGPT_DEFAULT_BASE_URL
    return OPENAI_DEFAULT_BASE_URL


def prepare_observe_codex_runtime(
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
    if not observe_proxy_should_wrap(config, task, backend_name):
        return codex_config, proxy_env, {"enabled": False, "ready": False, "reason": "disabled"}

    selected_model = None
    if isinstance(codex_config, dict):
        selected_model = codex_config.get("model")
        if not selected_model and str(codex_config.get("env_active") or "").strip():
            selected_model = f"env:{str(codex_config.get('env_active') or '').strip()}"
    if codex_litellm_responses_bridge_config(codex_config, str(selected_model or "").strip() or None):
        return codex_config, proxy_env, {"enabled": True, "ready": False, "reason": "litellm_bridge_provider"}

    integration = {**observe_proxy_config(config), "enabled": True}
    scope = observe_proxy_scope(run_id, task_id, agent_id)
    upstream_base_url = codex_observe_upstream_base_url(codex_config)
    provider_env = codex_config_env(codex_config)
    status = ensure_observe_proxy(
        root,
        integration,
        backend="codex",
        upstream_base_url=upstream_base_url,
        run_id=str(run_id or ""),
        task_id=task_id,
        agent_id=str(agent_id or "main"),
        proxy_env=proxy_env,
        provider_env=provider_env,
        scope=scope,
        workspace=workspace,
    )
    if not status.get("ready"):
        return codex_config, proxy_env, {**status, "enabled": True, "scope": scope, "upstream_base_url": upstream_base_url}

    local_base_url = observe_proxy_base_url(int(status.get("port") or integration.get("port") or 8797), "codex")
    return (
        codex_config_with_observe_provider_override(
            codex_config,
            provider_id="aha_observe",
            name="AHA Observe Proxy",
            base_url=local_base_url,
            wire_api="responses",
            model=str(selected_model or "").strip() or None,
        ),
        codex_proxy_env_for_observe(proxy_env, local_base_url),
        {**status, "enabled": True, "ready": True, "scope": scope, "upstream_base_url": upstream_base_url, "local_base_url": local_base_url},
    )


def prepare_observe_claude_runtime(
    root: Path,
    *,
    config: dict,
    task: dict,
    backend_name: str,
    claude_config: dict | None,
    proxy_env: dict[str, str] | None,
    run_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    workspace: Path | None = None,
) -> tuple[dict[str, str] | None, dict]:
    if not observe_proxy_should_wrap(config, task, backend_name):
        return proxy_env, {"enabled": False, "ready": False, "reason": "disabled"}

    integration = {**observe_proxy_config(config), "enabled": True}
    scope = observe_proxy_scope(run_id, task_id, agent_id)
    upstream_base_url = claude_upstream_base_url(claude_config)
    provider_env = claude_config_env(claude_config)
    status = ensure_observe_proxy(
        root,
        integration,
        backend="claude",
        upstream_base_url=upstream_base_url,
        run_id=str(run_id or ""),
        task_id=task_id,
        agent_id=str(agent_id or "main"),
        proxy_env=proxy_env,
        provider_env=provider_env,
        scope=scope,
        workspace=workspace,
    )
    if not status.get("ready"):
        return proxy_env, {**status, "enabled": True, "scope": scope, "upstream_base_url": upstream_base_url}

    local_base_url = observe_proxy_base_url(int(status.get("port") or integration.get("port") or 8797), "claude")
    return (
        claude_proxy_env_for_observe(proxy_env, local_base_url),
        {**status, "enabled": True, "ready": True, "scope": scope, "upstream_base_url": upstream_base_url, "local_base_url": local_base_url},
    )


def _join_upstream_url(base_url: str, request_path: str) -> str:
    base = str(base_url or "").rstrip("/")
    path = request_path if request_path.startswith("/") else f"/{request_path}"
    if base.endswith("/backend-api/codex") and (path == "/v1" or path.startswith("/v1/")):
        path = path[3:] or "/"
    if base.endswith("/v1") and path.startswith("/v1"):
        return f"{base[:-3]}{path}"
    return f"{base}{path}"


def _artifact_dir(root: Path, run_id: str, task_id: str | None, agent_id: str) -> Path:
    task_part = _safe_scope_part(task_id, "run")
    agent_part = _safe_scope_part(agent_id, "main")
    return run_dir(root, run_id) / "network_io" / task_part / agent_part


def _artifact_ref(root: Path, run_id: str, path: Path) -> str:
    try:
        return str(path.relative_to(run_dir(root, run_id)))
    except ValueError:
        return str(path)


def _write_artifact(root: Path, run_id: str, task_id: str | None, agent_id: str, name: str, body: bytes) -> str:
    path = _artifact_dir(root, run_id, task_id, agent_id) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return _artifact_ref(root, run_id, path)


def _headers_metadata(headers) -> dict:
    metadata: dict[str, str] = {}
    for key, value in headers.items():
        lowered = str(key).lower()
        if lowered in SENSITIVE_HEADERS:
            metadata[str(key)] = "<redacted>"
        elif lowered in {"content-type", "content-encoding", "anthropic-version", "openai-beta", "user-agent"}:
            metadata[str(key)] = str(value)
    return metadata


def _maybe_json_usage(body: bytes) -> dict:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        return response["usage"]
    return {}


class ObserveProxyHandler(BaseHTTPRequestHandler):
    server_version = "AHAObserveProxy/0.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/livez":
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def _proxy(self) -> None:
        context = self.server.context  # type: ignore[attr-defined]
        request_id = uuid.uuid4().hex
        start = time.monotonic()
        content_length = int(self.headers.get("content-length") or 0)
        request_body = self.rfile.read(content_length) if content_length > 0 else b""
        request_ref = _write_artifact(
            context["root"],
            context["run_id"],
            context.get("task_id"),
            context["agent_id"],
            f"{utc_now().replace(':', '').replace('+', 'Z')}-{request_id}-request.body",
            request_body,
        )
        upstream_url = _join_upstream_url(context["upstream_base_url"], self.path)
        append_event_to_file(
            event_path(context["root"], context["run_id"]),
            context["run_id"],
            "agent_network_request",
            {
                "source": "observe-proxy",
                "task_id": context.get("task_id"),
                "target": context["agent_id"],
                "agent_id": context["agent_id"],
                "backend": context["backend"],
                "request_id": request_id,
                "method": self.command,
                "path": self.path,
                "upstream_url": upstream_url,
                "request_ref": request_ref,
                "request_bytes": len(request_body),
                "headers": _headers_metadata(self.headers),
            },
        )

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() not in {"host", "accept-encoding"}
        }
        headers["Accept-Encoding"] = "identity"
        if context["backend"] == "codex" and not any(key.lower() == "authorization" for key in headers):
            api_key = context.get("openai_api_key") or ""
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        if context["backend"] == "claude" and not any(key.lower() == "x-api-key" for key in headers):
            api_key = context.get("anthropic_api_key") or ""
            if api_key:
                headers["x-api-key"] = api_key

        response_body = bytearray()
        status = 502
        response_headers = {}
        error = ""
        client_disconnected = False
        try:
            request = Request(upstream_url, data=request_body if request_body or self.command not in {"GET", "HEAD"} else None, headers=headers, method=self.command)
            with urlopen(request, timeout=None) as response:
                status = int(response.status)
                response_headers = dict(response.headers.items())
                self.send_response(status)
                for key, value in response.headers.items():
                    if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-length":
                        self.send_header(key, value)
                self.end_headers()
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    response_body.extend(chunk)
                    if client_disconnected:
                        continue
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError) as exc:
                        client_disconnected = True
                        error = f"client_disconnected: {exc}"
                        break
        except HTTPError as exc:
            status = int(exc.code)
            response_headers = dict(exc.headers.items())
            body = exc.read()
            response_body.extend(body)
            try:
                self.send_response(status)
                for key, value in exc.headers.items():
                    if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-length":
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError) as write_exc:
                error = f"client_disconnected: {write_exc}"
        except Exception as exc:  # pragma: no cover - exercised via integration smoke tests.
            error = str(exc)
            body = json.dumps({"error": "observe_proxy_upstream_failed", "message": error}, ensure_ascii=False).encode("utf-8")
            response_body.extend(body)
            try:
                self.send_response(502)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError) as write_exc:
                error = f"{error}; client_disconnected: {write_exc}"

        response_ref = _write_artifact(
            context["root"],
            context["run_id"],
            context.get("task_id"),
            context["agent_id"],
            f"{utc_now().replace(':', '').replace('+', 'Z')}-{request_id}-response.body",
            bytes(response_body),
        )
        append_event_to_file(
            event_path(context["root"], context["run_id"]),
            context["run_id"],
            "agent_network_response",
            {
                "source": "observe-proxy",
                "task_id": context.get("task_id"),
                "target": context["agent_id"],
                "agent_id": context["agent_id"],
                "backend": context["backend"],
                "request_id": request_id,
                "status": status,
                "duration_ms": int((time.monotonic() - start) * 1000),
                "response_ref": response_ref,
                "response_bytes": len(response_body),
                "headers": _headers_metadata(response_headers),
                "usage": _maybe_json_usage(bytes(response_body)),
                **({"error": error} if error else {}),
            },
        )


def run_observe_proxy_server(args: argparse.Namespace) -> int:
    root = aha_home_path(Path(args.home or "."))
    context = {
        "root": root,
        "run_id": args.run_id,
        "task_id": args.task_id,
        "agent_id": args.agent_id,
        "backend": args.backend,
        "upstream_base_url": args.upstream_base_url,
        "openai_api_key": os.environ.get("OPENAI_API_KEY") or "",
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY") or "",
    }
    server = ThreadingHTTPServer((args.host, int(args.port)), ObserveProxyHandler)
    server.context = context  # type: ignore[attr-defined]
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    return 0


__all__ = [
    "claude_proxy_env_for_observe",
    "claude_upstream_base_url",
    "codex_proxy_env_for_observe",
    "ensure_observe_proxy",
    "observe_proxy_base_url",
    "observe_proxy_config",
    "observe_proxy_scope",
    "observe_proxy_should_wrap",
    "observe_proxy_status",
    "observe_proxy_usage_summary",
    "prepare_observe_claude_runtime",
    "prepare_observe_codex_runtime",
    "run_observe_proxy_server",
]
