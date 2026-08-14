"""Platform-aware HTTP forwarding to the AHA Web service.

A task agent may run inside WSL while AHA itself is installed on Windows. In
that case the agent's ``aha browser`` / ``aha hardware-*`` commands must operate
the *Windows* browser and serial ports (the AHA platform), not a WSL-side
bridge that has no Chromium and no COM ports. This module detects the AHA
platform from the shared runtime service.json and forwards such commands over
HTTP to the Windows Web service, which owns the platform resources.

Nothing here is hardcoded: the host, port, and auth token all come from the
AHA home runtime state, so a different install path, port, or token keeps
working.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from aha_cli.services.service_runtime import read_service_runtime
from aha_cli.store.paths import aha_home_path


class AhaHttpClientError(RuntimeError):
    pass


def running_in_wsl() -> bool:
    """Whether the current process is inside a WSL distro."""
    if sys.platform != "linux":
        return False
    return Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()


def aha_platform_is_windows(root: Path) -> bool:
    """Whether AHA's host platform is Windows (from the shared runtime state).

    The runtime service.json records the platform the Web service runs on.
    An agent in WSL reads the same AHA home (via /mnt/...) and sees
    ``platform: Windows`` when AHA is installed on Windows.
    """
    try:
        runtime = read_service_runtime(root)
    except Exception:  # noqa: BLE001 - discovery must never break a command
        return False
    return str(runtime.get("platform") or "").strip().lower() == "windows"


def should_forward_to_windows_web(root: Path) -> bool:
    """True when this agent should forward browser/hardware commands to the
    Windows Web service instead of running them locally.

    Only forwards when the agent runs inside WSL and AHA itself runs on
    Windows. A Windows-hosted agent or a WSL-hosted AHA keeps local execution.
    """
    return running_in_wsl() and aha_platform_is_windows(root)


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


def web_forward(
    root: Path,
    method: str,
    path: str,
    *,
    query: dict | None = None,
    payload: dict | None = None,
    web_token: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """Forward an HTTP request to the AHA Web service and return its JSON body.

    ``path`` is the URL path on the Web service (e.g. ``/api/task/task-001/browser-action``).
    The base host/port/token are resolved dynamically from the runtime state.
    """
    runtime = read_service_runtime(root)
    if runtime.get("status") not in {"running", "starting"}:
        raise AhaHttpClientError("AHA Web service is not running")
    port = str(runtime.get("bind_port") or "").strip()
    if not port.isdigit():
        raise AhaHttpClientError("AHA Web service port is unavailable")
    url = f"http://{_loopback_host(runtime.get('bind_host'))}:{port}{path}"
    if query:
        url += f"?{urlencode(query)}"
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
        raise AhaHttpClientError(message) from exc
    except (OSError, URLError, ValueError) as exc:
        raise AhaHttpClientError(f"AHA Web request failed: {exc}") from exc
    if not isinstance(result, dict):
        raise AhaHttpClientError("AHA Web returned a non-JSON response")
    return result


__all__ = [
    "AhaHttpClientError",
    "aha_platform_is_windows",
    "running_in_wsl",
    "should_forward_to_windows_web",
    "web_forward",
]
