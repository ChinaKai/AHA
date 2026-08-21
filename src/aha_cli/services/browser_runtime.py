"""Client, paths, and lifecycle helpers for the task-scoped browser bridge."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlparse
import uuid

from aha_cli import platform, process_control
from aha_cli.services import loopback_ipc
from aha_cli.domain.models import normalize_browser_profile_name, normalize_task_browser_control, utc_now
from aha_cli.services.hardware_bridge import pid_alive
from aha_cli.services.onebin import aha_cli_invocation, resolve_aha_python
from aha_cli.services.proxy import backend_proxy_config
from aha_cli.store.config import load_config
from aha_cli.store.filesystem import require_plan, task_snapshot
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import aha_home_path

TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}
MAX_BROWSER_FRAME_BYTES = 8 * 1024 * 1024
BROWSER_BRIDGE_START_TIMEOUT_SECONDS = 12.0
BROWSER_DESKTOP_CAPTURE_SCALE = 1.5
BROWSER_MOBILE_CAPTURE_SCALE = 3.0
BROWSER_DOCTOR_CHILD_ENV = "AHA_BROWSER_DOCTOR_CHILD"


class BrowserBridgeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def browser_bridge_launcher() -> list[str]:
    """Launch Browser Bridge with an interpreter that can import Playwright."""

    return aha_cli_invocation(required_module="playwright")


def _scope_key(value: object) -> str:
    text = str(value or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip(".-")
    if safe and len(safe) <= 80:
        return safe
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    return safe[:50] + "-" + digest if safe else digest


def browser_runtime_dir(root: Path, run_id: str, task_id: str) -> Path:
    return aha_home_path(root) / "runtime" / "browser" / _scope_key(run_id) / _scope_key(task_id)


def browser_bridge_state_path(root: Path, run_id: str, task_id: str) -> Path:
    return browser_runtime_dir(root, run_id, task_id) / "bridge.json"


def browser_bridge_socket_path(root: Path, run_id: str, task_id: str) -> Path:
    return browser_runtime_dir(root, run_id, task_id) / "browser.sock"


def _browser_bridge_socket_accepting(root: Path, run_id: str, task_id: str) -> bool:
    socket_path = browser_bridge_socket_path(root, run_id, task_id)
    if not socket_path.exists():
        return False
    return loopback_ipc.is_accepting(socket_path)


def browser_bridge_lock_path(root: Path, run_id: str, task_id: str) -> Path:
    return browser_runtime_dir(root, run_id, task_id) / "bridge.lock"


def browser_bridge_manual_stop_path(root: Path, run_id: str, task_id: str) -> Path:
    return browser_runtime_dir(root, run_id, task_id) / "manual-stop.json"


def browser_bridge_log_path(root: Path, run_id: str, task_id: str) -> Path:
    return aha_home_path(root) / "logs" / "browser" / _scope_key(run_id) / f"{_scope_key(task_id)}.log"


def browser_task_profile_dir(root: Path, run_id: str, task_id: str) -> Path:
    return aha_home_path(root) / "browser" / "profiles" / _scope_key(run_id) / _scope_key(task_id)


def browser_named_profiles_dir(root: Path) -> Path:
    return aha_home_path(root) / "browser" / "profiles" / "named"


def browser_named_profile_id(name: object) -> str:
    normalized = normalize_browser_profile_name(name)
    if not normalized:
        raise BrowserBridgeError("browser_profile_invalid", "Named browser profile requires a valid name.")
    return hashlib.sha256(normalized.casefold().encode("utf-8")).hexdigest()[:24]


def browser_named_profile_dir(root: Path, name: object) -> Path:
    return browser_named_profiles_dir(root) / browser_named_profile_id(name) / "data"


def ensure_named_browser_profile(root: Path, name: object) -> dict:
    normalized = normalize_browser_profile_name(name)
    profile_id = browser_named_profile_id(normalized)
    profile_root = browser_named_profiles_dir(root) / profile_id
    metadata_path = profile_root / "profile.json"
    created_at = utc_now()
    if metadata_path.is_file():
        try:
            metadata = read_json(metadata_path)
            created_at = str(metadata.get("created_at") or created_at)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    profile_root.mkdir(parents=True, exist_ok=True)
    (profile_root / "data").mkdir(parents=True, exist_ok=True)
    write_json(
        metadata_path,
        {
            "id": profile_id,
            "name": normalized,
            "created_at": created_at,
            "updated_at": utc_now(),
        },
    )
    return {"id": profile_id, "name": normalized, "created_at": created_at}


def list_named_browser_profiles(root: Path) -> list[dict]:
    profiles: list[dict] = []
    profiles_dir = browser_named_profiles_dir(root)
    if not profiles_dir.is_dir():
        return profiles
    for metadata_path in profiles_dir.glob("*/profile.json"):
        try:
            metadata = read_json(metadata_path)
        except (OSError, json.JSONDecodeError):
            continue
        name = normalize_browser_profile_name(metadata.get("name"))
        if not name:
            continue
        profiles.append(
            {
                "id": str(metadata.get("id") or metadata_path.parent.name),
                "name": name,
                "created_at": str(metadata.get("created_at") or ""),
            }
        )
    profiles.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    return profiles


class BrowserProfileLease:
    def __init__(
        self,
        path: Path,
        *,
        mode: str,
        name: str = "",
        lock_fd: int | None = None,
        cleanup: bool = False,
    ) -> None:
        self.path = path
        self.mode = mode
        self.name = name
        self.lock_fd = lock_fd
        self.cleanup = cleanup

    def close(self) -> None:
        if self.lock_fd is not None:
            from aha_cli import locking

            try:
                os.ftruncate(self.lock_fd, 0)
                locking.release(self.lock_fd)
            finally:
                os.close(self.lock_fd)
                self.lock_fd = None
        if self.cleanup:
            shutil.rmtree(self.path, ignore_errors=True)
            self.cleanup = False


def acquire_browser_profile(
    root: Path,
    run_id: str,
    task_id: str,
    browser_config: dict,
) -> BrowserProfileLease:
    mode = str(browser_config.get("profile") or "ephemeral")
    if mode == "task":
        path = browser_task_profile_dir(root, run_id, task_id)
        path.mkdir(parents=True, exist_ok=True)
        return BrowserProfileLease(path, mode=mode)
    if mode == "named":
        from aha_cli import locking

        profile = ensure_named_browser_profile(root, browser_config.get("profile_name"))
        path = browser_named_profile_dir(root, profile["name"])
        lock_path = browser_named_profiles_dir(root) / f".{profile['id']}.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            locking.acquire(lock_fd, blocking=False)
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise BrowserBridgeError(
                "browser_profile_in_use",
                f'Named browser profile "{profile["name"]}" is already in use by another task.',
            ) from exc
        owner = json.dumps(
            {"pid": os.getpid(), "run_id": run_id, "task_id": task_id},
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            os.ftruncate(lock_fd, 0)
            os.write(lock_fd, owner)
        except Exception:
            locking.release(lock_fd)
            os.close(lock_fd)
            raise
        return BrowserProfileLease(path, mode=mode, name=profile["name"], lock_fd=lock_fd)
    runtime_dir = browser_runtime_dir(root, run_id, task_id)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="profile-", dir=runtime_dir))
    return BrowserProfileLease(path, mode="ephemeral", cleanup=True)


def browser_artifacts_dir(root: Path, run_id: str, task_id: str) -> Path:
    return aha_home_path(root) / "runs" / run_id / "tasks" / task_id / "browser_artifacts"


def read_browser_bridge_state(root: Path, run_id: str, task_id: str) -> dict | None:
    path = browser_bridge_state_path(root, run_id, task_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def browser_bridge_status(root: Path, run_id: str, task_id: str) -> dict:
    state = read_browser_bridge_state(root, run_id, task_id)
    manually_stopped = browser_bridge_manual_stop_path(root, run_id, task_id).is_file()
    if not state or not pid_alive(state.get("pid")):
        result = {
            "run_id": run_id,
            "task_id": task_id,
            "status": "closed" if manually_stopped else "stopped",
            "alive": False,
            "manually_stopped": manually_stopped,
        }
        if state and state.get("error"):
            result["error"] = state["error"]
            result["error_code"] = state.get("error_code")
        return result
    return {
        **state,
        "run_id": run_id,
        "task_id": task_id,
        "alive": True,
        "manually_stopped": manually_stopped,
    }


def task_browser_config(root: Path, run_id: str, task_id: str) -> tuple[dict, dict]:
    task = task_snapshot(root, run_id, task_id)["task"]
    return task, normalize_task_browser_control(task.get("browser_control"))


def browser_initial_viewport(browser_config: dict) -> tuple[int, int, bool]:
    if browser_config.get("device_mode") == "mobile":
        return 360, 640, True
    return 1280, 720, False


def browser_capture_scale(mobile: bool) -> float:
    return BROWSER_MOBILE_CAPTURE_SCALE if mobile else BROWSER_DESKTOP_CAPTURE_SCALE


def browser_frame_size(width: int, height: int, *, mobile: bool) -> tuple[int, int]:
    scale = browser_capture_scale(mobile)
    return max(1, round(width * scale)), max(1, round(height * scale))


def task_browser_active(task: dict, config: dict) -> bool:
    return (
        config.get("mode") == "managed"
        and not task.get("deleted_at")
        and str(task.get("status") or "") not in TERMINAL_TASK_STATUSES
    )


def browser_native_display_available(
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    wslg_root: Path = Path("/mnt/wslg"),
) -> bool:
    platform_value = str(platform_name or sys.platform)
    if platform_value == "darwin" or platform_value.startswith("win"):
        return True
    environment = environ if environ is not None else os.environ
    return bool(
        str(environment.get("DISPLAY") or "").strip()
        or str(environment.get("WAYLAND_DISPLAY") or "").strip()
        or (
            platform_value.startswith("linux")
            and (wslg_root / ".X11-unix" / "X0").is_socket()
        )
    )


def browser_native_display_environment(
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    wslg_root: Path = Path("/mnt/wslg"),
) -> dict[str, str]:
    platform_value = str(platform_name or sys.platform)
    environment = environ if environ is not None else os.environ
    if (
        platform_value.startswith("linux")
        and not str(environment.get("DISPLAY") or "").strip()
        and (wslg_root / ".X11-unix" / "X0").is_socket()
    ):
        return {"DISPLAY": ":0"}
    return {}


def browser_display_status(
    browser_config: dict,
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    wslg_root: Path = Path("/mnt/wslg"),
) -> dict:
    requested = str(browser_config.get("display") or "native").strip().lower()
    if requested not in {"native", "embedded"}:
        requested = "native"
    native_available = browser_native_display_available(
        environ=environ,
        platform_name=platform_name,
        wslg_root=wslg_root,
    )
    active = "native" if requested == "native" and native_available else "embedded"
    return {
        "requested": requested,
        "active": active,
        "native_available": native_available,
        "fallback": requested == "native" and active != "native",
        "fallback_reason": (
            "native_display_unavailable"
            if requested == "native" and active != "native"
            else ""
        ),
    }


def _playwright_proxy_options(
    server: object,
    *,
    bypass: object = "",
    username: object = "",
    password: object = "",
) -> dict:
    parsed = urlparse(str(server or "").strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise BrowserBridgeError("browser_proxy_invalid", "Browser proxy port is invalid.") from exc
    if (
        parsed.scheme not in {"http", "https", "socks4", "socks5"}
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise BrowserBridgeError(
            "browser_proxy_invalid",
            "Browser proxy must be an HTTP(S) or SOCKS proxy URL.",
        )
    hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    endpoint = f"{parsed.scheme}://{hostname}{f':{port}' if port else ''}"
    result = {"server": endpoint}
    bypass_value = str(bypass or "").strip()
    username_value = str(username or "") or unquote(parsed.username or "")
    password_value = str(password or "") or unquote(parsed.password or "")
    if bypass_value:
        result["bypass"] = bypass_value
    if username_value:
        result["username"] = username_value
    if password_value:
        result["password"] = password_value
    return result


def browser_proxy_launch_options(
    root: Path,
    run_id: str,
    task: dict,
    browser_config: dict,
) -> dict | None:
    mode = str(browser_config.get("proxy_mode") or "direct")
    if mode == "direct":
        return None
    if mode == "inherit":
        if not bool(task.get("preferred_proxy_enabled")):
            return None
        plan = require_plan(root, run_id)
        inherited = backend_proxy_config(
            load_config(root),
            task.get("preferred_backend"),
            plan,
            task,
        )
        http_proxy = str(inherited.get("http_proxy") or "").strip()
        https_proxy = str(inherited.get("https_proxy") or "").strip()
        if http_proxy and https_proxy and http_proxy != https_proxy:
            raise BrowserBridgeError(
                "browser_proxy_conflict",
                "Inherited HTTP and HTTPS proxies must use the same server for Chromium.",
            )
        server = https_proxy or http_proxy
        if not server:
            return None
        return _playwright_proxy_options(server, bypass=inherited.get("no_proxy"))
    server = str(browser_config.get("proxy_server") or "").strip()
    if not server:
        raise BrowserBridgeError(
            "browser_proxy_required",
            "Custom browser proxy mode requires a proxy server.",
        )
    return _playwright_proxy_options(
        server,
        bypass=browser_config.get("proxy_bypass"),
        username=browser_config.get("proxy_username"),
        password=browser_config.get("proxy_password"),
    )


def browser_proxy_signature(options: dict | None) -> str:
    encoded = json.dumps(options or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def browser_launch_signature(
    browser_config: dict,
    proxy_options: dict | None,
    *,
    display_status: dict | None = None,
) -> str:
    display = display_status or browser_display_status(browser_config)
    payload = {
        "runtime": str(browser_config.get("runtime") or "playwright"),
        "profile": str(browser_config.get("profile") or "ephemeral"),
        "profile_name": str(browser_config.get("profile_name") or ""),
        "display_requested": str(display.get("requested") or "native"),
        "display_active": str(display.get("active") or "embedded"),
        "device_mode": str(browser_config.get("device_mode") or "desktop"),
        "downloads": str(browser_config.get("downloads") or "deny"),
        "proxy": proxy_options or {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def browser_context_launch_options(
    browser_config: dict,
    proxy_options: dict | None,
    *,
    display_status: dict,
    viewport_width: int,
    viewport_height: int,
) -> dict:
    native_active = display_status.get("active") == "native"
    options = {
        "headless": not native_active,
        "viewport": {"width": viewport_width, "height": viewport_height},
        "device_scale_factor": browser_capture_scale(
            str(browser_config.get("device_mode") or "desktop") == "mobile"
        ),
        "accept_downloads": browser_config.get("downloads") == "allow",
        "args": ["--disable-dev-shm-usage"],
    }
    if native_active:
        options["args"].append(
            f"--window-size={viewport_width},{viewport_height + 100}"
        )
    if proxy_options:
        options["proxy"] = proxy_options
    return options


def ensure_browser_bridge(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    launcher: list[str] | None = None,
    parent_bound: bool = False,
) -> dict:
    """Idempotently start the one browser bridge for a task."""

    from aha_cli import locking

    task, config = task_browser_config(root, run_id, task_id)
    if config.get("mode") != "managed":
        raise BrowserBridgeError("browser_disabled", "Browser control is disabled for this task.")
    if not task_browser_active(task, config):
        raise BrowserBridgeError("task_terminal", "The task is terminal; its browser session is closed.")
    if browser_bridge_manual_stop_path(root, run_id, task_id).is_file():
        raise BrowserBridgeError("browser_closed", "The task browser is closed. Start it from the Browser panel.")
    runtime_dir = browser_runtime_dir(root, run_id, task_id)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(browser_bridge_lock_path(root, run_id, task_id), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        locking.acquire(lock_fd)
        state = read_browser_bridge_state(root, run_id, task_id)
        if state and pid_alive(state.get("pid")):
            state_status = str(state.get("status") or "")
            if state_status in {"starting", "error"}:
                return browser_bridge_status(root, run_id, task_id)
            if state_status == "running" and _browser_bridge_socket_accepting(root, run_id, task_id):
                return browser_bridge_status(root, run_id, task_id)
            _stop_browser_bridge(root, run_id, task_id, timeout=2.0)
        cmd = [
            *(launcher or browser_bridge_launcher()),
            "--home",
            str(aha_home_path(root)),
            "browser-bridge",
            run_id,
            task_id,
        ]
        child_env = dict(os.environ)
        child_env.update(browser_native_display_environment(environ=child_env))
        child_env["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path) + (
            os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
        )
        log_path = browser_bridge_log_path(root, run_id, task_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log_file:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                preexec_fn=process_control.parent_death_preexec() if parent_bound else None,
                start_new_session=not parent_bound,
                env=child_env,
                **platform.hidden_subprocess_kwargs(),
            )
            if parent_bound:
                process_control.assign_parent_death(proc)
        state_path = browser_bridge_state_path(root, run_id, task_id)
        state_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "pid": proc.pid,
                    "status": "starting",
                    "updated_at": utc_now(),
                    "runtime": config.get("runtime"),
                    "profile": config.get("profile"),
                    "profile_name": config.get("profile_name"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return {**browser_bridge_status(root, run_id, task_id), "pid": proc.pid, "alive": True}
    finally:
        try:
            locking.release(lock_fd)
        finally:
            os.close(lock_fd)


def _browser_bridge_pid_matches_scope(pid: int, run_id: str, task_id: str) -> bool:
    if not pid_alive(pid):
        return False
    command_path = Path("/proc") / str(pid) / "cmdline"
    if not command_path.is_file():
        return True
    try:
        command = command_path.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return "browser-bridge" in command and run_id in command and task_id in command


def _stop_browser_bridge(root: Path, run_id: str, task_id: str, *, timeout: float = 15.0) -> dict:
    state = read_browser_bridge_state(root, run_id, task_id) or {}
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if not pid or not pid_alive(pid):
        return browser_bridge_status(root, run_id, task_id)
    if not _browser_bridge_pid_matches_scope(pid, run_id, task_id):
        raise BrowserBridgeError(
            "browser_pid_mismatch",
            "Refusing to stop a process that does not match this task Browser Bridge.",
        )
    try:
        process_control.send_signal(pid, signal.SIGTERM)
    except ProcessLookupError:
        return browser_bridge_status(root, run_id, task_id)
    except PermissionError as exc:
        raise BrowserBridgeError(
            "browser_stop_forbidden",
            "AHA does not have permission to stop this task Browser Bridge.",
        ) from exc
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.05)
    if pid_alive(pid):
        try:
            process_control.send_signal(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise BrowserBridgeError(
                "browser_stop_forbidden",
                "AHA does not have permission to terminate this task Browser Bridge.",
            ) from exc
        kill_deadline = time.monotonic() + 2.0
        while time.monotonic() < kill_deadline and pid_alive(pid):
            time.sleep(0.05)
        if pid_alive(pid):
            raise BrowserBridgeError(
                "browser_stop_timeout",
                "The task Browser Bridge did not stop after SIGTERM and SIGKILL.",
            )
    return browser_bridge_status(root, run_id, task_id)


def _wait_for_browser_bridge_ready(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    timeout: float = BROWSER_BRIDGE_START_TIMEOUT_SECONDS,
) -> dict:
    deadline = time.monotonic() + max(0.1, float(timeout))
    last_state: dict = {}
    while time.monotonic() < deadline:
        last_state = browser_bridge_status(root, run_id, task_id)
        if last_state.get("status") == "error":
            raise BrowserBridgeError(
                str(last_state.get("error_code") or "browser_start_failed"),
                str(last_state.get("error") or "Browser bridge failed to start."),
            )
        if (
            last_state.get("status") == "running"
            and last_state.get("alive")
            and _browser_bridge_socket_accepting(root, run_id, task_id)
        ):
            return last_state
        time.sleep(0.05)
    raise BrowserBridgeError(
        "browser_unavailable",
        "Browser bridge did not become ready before the startup timeout.",
    )


def browser_session_lifecycle(root: Path, run_id: str, task_id: str, action: object) -> dict:
    task, config = task_browser_config(root, run_id, task_id)
    if not task_browser_active(task, config):
        raise BrowserBridgeError("browser_disabled", "Browser control is not active for this task.")
    command = str(action or "").strip().lower()
    if command not in {"start", "restart", "close"}:
        raise BrowserBridgeError("browser_lifecycle_invalid", "Browser action must be start, restart, or close.")
    marker = browser_bridge_manual_stop_path(root, run_id, task_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    if command in {"restart", "close"}:
        write_json(marker, {"action": command, "updated_at": utc_now()})
        _stop_browser_bridge(root, run_id, task_id)
    if command == "close":
        return {"action": command, "bridge": browser_bridge_status(root, run_id, task_id)}
    marker.unlink(missing_ok=True)
    bridge = ensure_browser_bridge(root, run_id, task_id)
    if not (bridge.get("status") == "running" and bridge.get("alive")):
        bridge = _wait_for_browser_bridge_ready(root, run_id, task_id)
    return {"action": command, "bridge": bridge}


async def open_browser_bridge_ipc(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    ensure: bool = True,
    parent_bound: bool = False,
    timeout: float = BROWSER_BRIDGE_START_TIMEOUT_SECONDS,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict]:
    if ensure:
        ensure_browser_bridge(root, run_id, task_id, parent_bound=parent_bound)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, timeout)
    socket_path = browser_bridge_socket_path(root, run_id, task_id)
    last_error: Exception | None = None
    while loop.time() < deadline:
        state = browser_bridge_status(root, run_id, task_id)
        if state.get("status") == "error":
            raise BrowserBridgeError(
                str(state.get("error_code") or "browser_start_failed"),
                str(state.get("error") or "Browser bridge failed to start."),
            )
        try:
            reader, writer = await loopback_ipc.open_connection(socket_path, limit=MAX_BROWSER_FRAME_BYTES)
            ready = await asyncio.wait_for(read_browser_frame(reader), timeout=2.0)
            if not ready or ready.get("type") != "ready":
                writer.close()
                await writer.wait_closed()
                raise BrowserBridgeError("protocol_error", "Browser bridge did not send a ready frame.")
            return reader, writer, ready
        except (OSError, asyncio.TimeoutError, BrowserBridgeError) as exc:
            last_error = exc
            await asyncio.sleep(0.05)
    raise BrowserBridgeError("browser_unavailable", f"Browser bridge IPC unavailable: {last_error or socket_path}")


async def browser_bridge_request(
    root: Path,
    run_id: str,
    task_id: str,
    action: str,
    *,
    args: dict | None = None,
    source: str = "agent",
    agent_id: str = "main",
    timeout: float = 30.0,
    parent_bound: bool = False,
) -> dict:
    reader, writer, _ready = await open_browser_bridge_ipc(
        root,
        run_id,
        task_id,
        parent_bound=parent_bound,
    )
    request_id = uuid.uuid4().hex
    try:
        await write_browser_frame(
            writer,
            {
                "type": "command",
                "id": request_id,
                "action": action,
                "args": args or {},
                "source": source,
                "agent_id": agent_id,
            },
        )
        deadline = asyncio.get_running_loop().time() + max(0.1, timeout)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise BrowserBridgeError("timeout", f"Browser action timed out: {action}")
            payload = await asyncio.wait_for(read_browser_frame(reader), timeout=remaining)
            if payload is None:
                raise BrowserBridgeError("browser_disconnected", "Browser bridge disconnected.")
            if payload.get("type") != "result" or payload.get("id") != request_id:
                continue
            if payload.get("ok"):
                result = payload.get("result")
                return result if isinstance(result, dict) else {"value": result}
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            raise BrowserBridgeError(
                str(error.get("code") or "browser_action_failed"),
                str(error.get("message") or f"Browser action failed: {action}"),
            )
    finally:
        writer.close()
        await writer.wait_closed()


async def _browser_doctor_current() -> dict:
    result = {
        "ok": False,
        "playwright_installed": importlib.util.find_spec("playwright") is not None,
        "python_executable": sys.executable,
        "python_fallback": False,
        "chromium_path": "",
        "chromium_installed": False,
        "channel": "",
        "channel_product": "",
        "user_browser_path": "",
        "user_browser_product": "",
        "user_browser_available": False,
        "native_display_available": browser_native_display_available(),
        "error": "",
    }
    if not result["playwright_installed"]:
        result["error"] = "Python Playwright is not installed. Install the AHA browser extra (pip install \"aha-cli[browser]\")."
        return result
    executable_path = ""
    try:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            executable_path = str(playwright.chromium.executable_path or "")
            result["chromium_path"] = executable_path
            result["chromium_installed"] = bool(executable_path and Path(executable_path).is_file())
        finally:
            await playwright.stop()
    except Exception as exc:
        result["error"] = str(exc)
        return result
    from aha_cli.services.browser_external import detect_installed_browser_channel, resolve_user_browser_executable

    channel = detect_installed_browser_channel()
    result["channel"] = channel or ""
    result["channel_product"] = {"chrome": "Google Chrome", "msedge": "Microsoft Edge"}.get(channel, "")
    try:
        user_path, user_product = resolve_user_browser_executable(executable_path)
        result["user_browser_path"] = str(user_path)
        result["user_browser_product"] = user_product
        result["user_browser_available"] = True
    except Exception:
        pass
    has_browser = result["chromium_installed"] or bool(result["channel"])
    result["ok"] = bool(result["playwright_installed"] and has_browser)
    if not result["ok"]:
        result["error"] = (
            "No browser available. Install Google Chrome or Microsoft Edge, "
            "or run `python -m playwright install chromium`."
        )
    return result


def _browser_doctor_with_python(executable: str) -> dict:
    command = [
        *aha_cli_invocation(required_module="playwright"),
        "browser",
        "doctor",
    ]
    command[0] = executable
    child_env = dict(os.environ)
    child_env[BROWSER_DOCTOR_CHILD_ENV] = "1"
    child_env["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path) + (
        os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
    )
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=20,
            env=child_env,
            **platform.hidden_subprocess_kwargs(),
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "playwright_installed": True,
            "python_executable": executable,
            "python_fallback": True,
            "error": f"Failed to inspect the detected AHA browser environment: {exc}",
        }
    if not isinstance(payload, dict):
        payload = {"ok": False, "error": "AHA browser doctor returned an invalid response."}
    payload["python_executable"] = executable
    payload["python_fallback"] = True
    return payload


async def browser_doctor() -> dict:
    if str(os.environ.get(BROWSER_DOCTOR_CHILD_ENV) or "") != "1":
        executable = resolve_aha_python("playwright")
        if executable and os.path.normcase(os.path.abspath(executable)) != os.path.normcase(
            os.path.abspath(sys.executable)
        ):
            return await asyncio.to_thread(_browser_doctor_with_python, executable)
    return await _browser_doctor_current()


async def read_browser_frame(reader: asyncio.StreamReader) -> dict | None:
    try:
        raw = await reader.readline()
    except ValueError as exc:
        raise BrowserBridgeError("frame_too_large", "Browser IPC frame exceeded the size limit.") from exc
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrowserBridgeError("invalid_json", "Invalid browser IPC JSON.") from exc
    if not isinstance(payload, dict):
        raise BrowserBridgeError("invalid_frame", "Browser IPC frame must be an object.")
    return payload


async def write_browser_frame(writer: asyncio.StreamWriter, payload: dict) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_BROWSER_FRAME_BYTES:
        raise BrowserBridgeError("frame_too_large", "Browser IPC frame exceeded the size limit.")
    writer.write(encoded)
    await writer.drain()


def save_browser_screenshot(
    root: Path,
    run_id: str,
    task_id: str,
    payload: dict,
    output: Path | None = None,
) -> Path:
    encoded = str(payload.get("data") or "")
    if not encoded:
        raise BrowserBridgeError("missing_image", "Browser screenshot response did not include image data.")
    mime = str(payload.get("mime") or "image/png")
    suffix = ".jpg" if mime == "image/jpeg" else ".png"
    if output is None:
        target_dir = browser_artifacts_dir(root, run_id, task_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"screenshot-{int(time.time() * 1000)}{suffix}"
    else:
        target = output.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(encoded, validate=True))
    return target


__all__ = [
    "BROWSER_BRIDGE_START_TIMEOUT_SECONDS",
    "BROWSER_DESKTOP_CAPTURE_SCALE",
    "BROWSER_MOBILE_CAPTURE_SCALE",
    "BrowserBridgeError",
    "MAX_BROWSER_FRAME_BYTES",
    "browser_artifacts_dir",
    "browser_bridge_request",
    "browser_bridge_manual_stop_path",
    "browser_bridge_socket_path",
    "browser_bridge_state_path",
    "browser_bridge_status",
    "browser_named_profile_dir",
    "browser_named_profile_id",
    "browser_named_profiles_dir",
    "browser_context_launch_options",
    "browser_capture_scale",
    "browser_display_status",
    "browser_doctor",
    "browser_launch_signature",
    "browser_initial_viewport",
    "browser_frame_size",
    "browser_native_display_available",
    "browser_native_display_environment",
    "browser_proxy_launch_options",
    "browser_proxy_signature",
    "browser_runtime_dir",
    "browser_session_lifecycle",
    "browser_task_profile_dir",
    "acquire_browser_profile",
    "ensure_named_browser_profile",
    "list_named_browser_profiles",
    "ensure_browser_bridge",
    "open_browser_bridge_ipc",
    "read_browser_bridge_state",
    "read_browser_frame",
    "save_browser_screenshot",
    "task_browser_active",
    "task_browser_config",
    "write_browser_frame",
]
