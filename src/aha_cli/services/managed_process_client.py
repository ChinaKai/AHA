"""Loopback client for AHA Web-owned managed processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from aha_cli.services.service_runtime import read_service_runtime
from aha_cli.store.paths import aha_home_path


class ManagedProcessClientError(RuntimeError):
    pass


def _loopback_host(bind_host: object) -> str:
    host = str(bind_host or "").strip().strip("[]")
    if host in {"", "0.0.0.0", "::"}:
        return "127.0.0.1"
    if ":" in host:
        return f"[{host}]"
    return host


def _web_token(root: Path, explicit: str | None = None) -> str:
    token = str(explicit or os.environ.get("AHA_WEB_TOKEN") or "").strip()
    if token:
        return token
    path = aha_home_path(root) / "web-token"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def managed_process_request(
    root: Path,
    method: str,
    *,
    run_id: str,
    task_id: str,
    agent_id: str,
    name: str | None = None,
    command: list[str] | None = None,
    cwd: str | None = None,
    web_token: str | None = None,
    timeout: float = 10.0,
) -> dict:
    runtime = read_service_runtime(root)
    if runtime.get("status") not in {"running", "starting"}:
        raise ManagedProcessClientError("AHA Web service is not running")
    port = str(runtime.get("bind_port") or "").strip()
    if not port.isdigit():
        raise ManagedProcessClientError("AHA Web service port is unavailable")
    query = urlencode(
        {
            "run_id": run_id,
            "task_id": task_id,
            "agent_id": agent_id,
            **({"name": name} if name else {}),
        }
    )
    url = f"http://{_loopback_host(runtime.get('bind_host'))}:{port}/api/managed-processes?{query}"
    payload = None
    if method in {"POST", "DELETE"}:
        payload = {
            "run_id": run_id,
            "task_id": task_id,
            "agent_id": agent_id,
            **({"name": name} if name else {}),
        }
        if command is not None:
            payload["command"] = command
        if cwd:
            payload["cwd"] = cwd
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    token = _web_token(root, web_token)
    if token:
        headers["X-AHA-Token"] = token
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=max(0.1, float(timeout))) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            message = str(error_payload.get("error") or exc.reason)
        except (OSError, ValueError, AttributeError):
            message = str(exc.reason)
        raise ManagedProcessClientError(message) from exc
    except (OSError, URLError, ValueError) as exc:
        raise ManagedProcessClientError(f"managed process request failed: {exc}") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise ManagedProcessClientError(str((result or {}).get("error") or "managed process request failed"))
    return result


__all__ = ["ManagedProcessClientError", "managed_process_request"]
