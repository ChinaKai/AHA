from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, replace
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Callable
from urllib.parse import quote
from urllib.request import urlopen
import uuid
import webbrowser

from aha_cli import platform, process_control
from aha_cli.constants import AHA_WEB_INSTANCE_ENV, AHA_WEB_SUPERVISED_ENV
from aha_cli.services.onebin import aha_cli_invocation, running_zipapp_path
from aha_cli.store.io import read_json, write_json
from aha_cli.web.auth import bind_host_exposes_network, normalize_auth_token

STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE = "AHA"


class WindowsTrayError(RuntimeError):
    pass


@dataclass(frozen=True)
class TraySettings:
    aha_home: Path | str
    bind: str
    port: int
    web_token: str = ""
    startup_task_name: str = ""

    def normalized(self) -> TraySettings:
        home_text = platform.expand_path(str(self.aha_home)).strip()
        if not home_text:
            raise WindowsTrayError("AHA_HOME 不能为空")
        bind = str(self.bind or "").strip()
        if not bind:
            raise WindowsTrayError("Bind 地址不能为空")
        try:
            port = int(self.port)
        except (TypeError, ValueError) as exc:
            raise WindowsTrayError("端口必须是数字") from exc
        if not 1 <= port <= 65535:
            raise WindowsTrayError("端口必须在 1 到 65535 之间")
        try:
            token = normalize_auth_token(self.web_token)
        except ValueError as exc:
            raise WindowsTrayError(str(exc)) from exc
        return TraySettings(
            Path(home_text).expanduser().resolve(),
            bind,
            port,
            token,
            str(self.startup_task_name or "").strip(),
        )


def default_tray_config_path() -> Path:
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "AHA" / "tray.json"


def tray_token_file(settings: TraySettings) -> Path:
    return Path(settings.aha_home) / "web-token"


def load_tray_settings(path: Path | None = None) -> TraySettings | None:
    config_path = path or default_tray_config_path()
    try:
        payload = read_json(config_path)
    except (OSError, ValueError):
        return None
    token = ""
    token_file = str(payload.get("web_token_file") or "").strip()
    if token_file:
        try:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    home_text = str(payload.get("aha_home") or "").strip()
    if not home_text:
        return None
    try:
        return TraySettings(
            Path(home_text),
            str(payload.get("bind") or "127.0.0.1"),
            int(payload.get("port", 8766)),
            token,
            str(payload.get("startup_task_name") or ""),
        ).normalized()
    except (OSError, WindowsTrayError, ValueError):
        return None


def save_tray_settings(settings: TraySettings, path: Path | None = None) -> Path:
    normalized = settings.normalized()
    config_path = path or default_tray_config_path()
    normalized.aha_home.mkdir(parents=True, exist_ok=True)
    token_file = tray_token_file(normalized)
    if normalized.web_token:
        token_file.write_text(normalized.web_token, encoding="utf-8")
    write_json(
        config_path,
        {
            "aha_home": str(normalized.aha_home),
            "bind": normalized.bind,
            "port": normalized.port,
            "web_token_file": str(token_file) if normalized.web_token else "",
            "startup_task_name": normalized.startup_task_name,
        },
    )
    return config_path


def materialize_tray_icon(config_path: Path | None = None) -> Path:
    target = (config_path or default_tray_config_path()).with_name("aha.ico")
    payload = resources.files("aha_cli").joinpath("assets", "aha.ico").read_bytes()
    try:
        current = target.read_bytes()
    except OSError:
        current = b""
    if current != payload:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return target


def _require_windows() -> None:
    if not platform.is_windows():
        raise WindowsTrayError("AHA tray is available on Windows only")


def pythonw_executable(executable: str | Path | None = None) -> str:
    """Return the console-free Python executable when it is available."""
    current = Path(executable or sys.executable)
    if current.name.lower() == "pythonw.exe":
        return str(current)
    candidate = current.with_name("pythonw.exe")
    if candidate.is_file():
        return str(candidate)
    return str(current)


class WindowsTrayMutex:
    def __init__(self, _root: Path, _port: int) -> None:
        executable = str(running_zipapp_path() or sys.executable).casefold().encode("utf-8")
        self.name = f"Local\\AHA.Tray.{hashlib.sha256(executable).hexdigest()[:20]}"
        self.handle: int | None = None

    def acquire(self) -> bool:
        _require_windows()
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.GetLastError.restype = wintypes.DWORD
        self.handle = kernel32.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise WindowsTrayError("failed to create the AHA tray single-instance lock")
        return int(kernel32.GetLastError()) != 183

    def close(self) -> None:
        if not self.handle:
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(self.handle)
        self.handle = None


def tray_invocation(
    root: Path,
    run_id: str,
    host: str,
    port: int,
    poll_interval: int,
    *,
    auth_token: str = "",
    auth_token_file: str = "",
    executable: str | Path | None = None,
) -> list[str]:
    """Build the persistent HKCU Run command without opening a browser."""
    pythonw = pythonw_executable(executable)
    zipapp = running_zipapp_path()
    command = [pythonw, str(zipapp)] if zipapp else [pythonw, "-m", "aha_cli"]
    command.extend(["--home", str(root), "tray"])
    if run_id:
        command.append(run_id)
    command.extend(["--host", host, "--port", str(port), "--poll-interval", str(poll_interval)])
    if auth_token:
        command.extend(["--auth-token", auth_token])
    if auth_token_file:
        command.extend(["--auth-token-file", auth_token_file])
    return command


def startup_command(*args, **kwargs) -> str:
    return subprocess.list2cmdline(tray_invocation(*args, **kwargs))


def _winreg():
    _require_windows()
    try:
        import winreg
    except ImportError as exc:  # pragma: no cover - defensive on a broken Windows runtime
        raise WindowsTrayError("the Windows registry module is unavailable") from exc
    return winreg


def installed_startup_command() -> str:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, _kind = winreg.QueryValueEx(key, STARTUP_VALUE)
    except FileNotFoundError:
        return ""
    return str(value or "").strip()


def startup_enabled(expected_command: str | None = None) -> bool:
    installed = installed_startup_command()
    if expected_command is None:
        return bool(installed)
    return installed.casefold() == expected_command.strip().casefold()


def set_startup_enabled(enabled: bool, command: str) -> None:
    winreg = _winreg()
    if enabled:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, STARTUP_VALUE, 0, winreg.REG_SZ, command)
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, STARTUP_VALUE)
    except FileNotFoundError:
        pass


def _scheduled_task_command(task_name: str, operation: str) -> subprocess.CompletedProcess[str]:
    _require_windows()
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return subprocess.run(
        ["schtasks.exe", f"/{operation}", "/TN", task_name],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        creationflags=creationflags,
    )


def start_scheduled_task(task_name: str) -> bool:
    return _scheduled_task_command(task_name, "Run").returncode == 0


def stop_scheduled_task(task_name: str) -> None:
    _scheduled_task_command(task_name, "End")


def dashboard_url(host: str, port: int, *, auth_token: str = "", auth_token_file: str = "") -> str:
    browser_host = host.strip()
    if browser_host in {"", "0.0.0.0", "::"}:
        browser_host = "127.0.0.1"
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    token = auth_token.strip()
    if not token and auth_token_file:
        try:
            token = Path(auth_token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    suffix = f"?token={quote(token, safe='')}" if token else ""
    return f"http://{browser_host}:{port}/{suffix}"


def web_ui_command(
    root: Path,
    run_id: str,
    host: str,
    port: int,
    poll_interval: int,
    *,
    auth_token: str = "",
    auth_token_file: str = "",
) -> list[str]:
    command = [*aha_cli_invocation(), "--home", str(root), "ui"]
    if run_id:
        command.append(run_id)
    command.extend(["--host", host, "--port", str(port), "--poll-interval", str(poll_interval)])
    if auth_token:
        command.extend(["--auth-token", auth_token])
    if auth_token_file:
        command.extend(["--auth-token-file", auth_token_file])
    return command


class WebUiProcess:
    def __init__(
        self,
        command: list[str],
        *,
        supervise: bool = False,
        readiness_probe: Callable[[str], bool] | None = None,
    ) -> None:
        self.command = command
        self.process: subprocess.Popen | None = None
        self.instance_id = ""
        self._supervise = bool(supervise)
        self._readiness_probe = readiness_probe
        self._lock = threading.RLock()
        self._stop_requested = False

    def _spawn_locked(self) -> tuple[subprocess.Popen, str]:
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        instance_id = uuid.uuid4().hex
        env = dict(os.environ)
        if self._supervise:
            env[AHA_WEB_SUPERVISED_ENV] = "1"
            env[AHA_WEB_INSTANCE_ENV] = instance_id
        try:
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                env=env,
            )
            process_control.assign_parent_death(process)
        except OSError as exc:
            raise WindowsTrayError(f"failed to start AHA Web UI: {exc}") from exc
        self.process = process
        self.instance_id = instance_id
        if self._supervise:
            threading.Thread(
                target=self._watch,
                args=(process,),
                name="aha-web-supervisor",
                daemon=True,
            ).start()
        return process, instance_id

    def start(self) -> None:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return
            self._stop_requested = False
            process, instance_id = self._spawn_locked()
        if self._readiness_probe is not None and not self._readiness_probe(instance_id):
            self._stop_process(process)
            with self._lock:
                if self.process is process:
                    self.process = None
                    self.instance_id = ""
            raise WindowsTrayError("AHA Web UI started but did not become ready")
        if process.poll() is not None:
            raise WindowsTrayError(f"AHA Web UI exited during startup with code {process.returncode}")

    def _watch(self, process: subprocess.Popen) -> None:
        return_code = process.wait()
        with self._lock:
            if self.process is not process:
                return
            self.process = None
            self.instance_id = ""
            restart = not self._stop_requested and return_code == 75
        if not restart:
            return
        process_control.terminate_parent_death_children()
        for attempt in range(3):
            time.sleep(0.25 * (attempt + 1))
            with self._lock:
                if self._stop_requested or self.process is not None:
                    return
            try:
                self.start()
                return
            except WindowsTrayError:
                continue

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if platform.WIN:
            process_control.terminate_parent_death_children()
            if process.poll() is None:
                process_control.signal_process_group(process.pid, signal.SIGTERM)
        elif process.poll() is None:
            process.terminate()
        if process.poll() is not None:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            process = self.process
            self.process = None
            self.instance_id = ""
        if process is None:
            return
        self._stop_process(process)

    def restart(self) -> None:
        self.stop()
        self.start()


class TrayRuntime:
    def __init__(
        self,
        settings: TraySettings,
        run_id: str,
        poll_interval: int,
        *,
        config_path: Path | None = None,
    ) -> None:
        self.settings = settings.normalized()
        self.run_id = run_id
        self.poll_interval = poll_interval
        self.config_path = config_path or default_tray_config_path()
        save_tray_settings(self.settings, self.config_path)
        self._scheduled_service_active = False
        self.web = WebUiProcess(
            self.web_command(),
            supervise=True,
            readiness_probe=lambda instance_id: wait_for_dashboard(
                self.settings.bind,
                self.settings.port,
                timeout_seconds=10.0,
                expected_instance_id=instance_id,
            ),
        )

    def auth_token_file(self) -> str:
        return str(tray_token_file(self.settings)) if self.settings.web_token else ""

    def web_command(self) -> list[str]:
        return web_ui_command(
            self.settings.aha_home,
            self.run_id,
            self.settings.bind,
            self.settings.port,
            self.poll_interval,
            auth_token_file=self.auth_token_file(),
        )

    def startup_command(self) -> str:
        return startup_command(
            self.settings.aha_home,
            self.run_id,
            self.settings.bind,
            self.settings.port,
            self.poll_interval,
            auth_token_file=self.auth_token_file(),
        )

    def login_startup_enabled(self) -> bool:
        expected_command = None if self.settings.startup_task_name else self.startup_command()
        return startup_enabled(expected_command)

    def dashboard_url(self) -> str:
        return dashboard_url(self.settings.bind, self.settings.port, auth_token=self.settings.web_token)

    def start(self) -> None:
        task_name = self.settings.startup_task_name
        if task_name:
            if wait_for_dashboard(self.settings.bind, self.settings.port, timeout_seconds=0.5):
                self._scheduled_service_active = True
                return
            if start_scheduled_task(task_name):
                self._scheduled_service_active = True
                if not wait_for_dashboard(self.settings.bind, self.settings.port, timeout_seconds=10.0):
                    stop_scheduled_task(task_name)
                    self._scheduled_service_active = False
                    raise WindowsTrayError("AHA 启动任务已运行，但 Web 服务未就绪")
                return
        self.web.command = self.web_command()
        self.web.start()
        self._scheduled_service_active = False

    def _stop_service(self) -> str:
        if self._scheduled_service_active and self.settings.startup_task_name:
            old_instance_id = dashboard_instance_id(self.settings.bind, self.settings.port)
            stop_scheduled_task(self.settings.startup_task_name)
            self._scheduled_service_active = False
            return old_instance_id
        old_instance_id = self.web.instance_id
        self.web.stop()
        return old_instance_id

    def restart(self) -> None:
        old_instance_id = self._stop_service()
        if old_instance_id and not wait_for_dashboard_shutdown(
            self.settings.bind,
            self.settings.port,
            old_instance_id,
        ):
            raise WindowsTrayError("旧 AHA Web 实例未完全退出，拒绝启动重叠进程")
        self.start()

    def apply_settings(self, settings: TraySettings) -> None:
        updated = replace(settings, startup_task_name=self.settings.startup_task_name).normalized()
        previous = self.settings
        previous_run_id = self.run_id
        startup_was_enabled = self.login_startup_enabled()
        old_instance_id = self._stop_service()
        if old_instance_id and not wait_for_dashboard_shutdown(
            previous.bind,
            previous.port,
            old_instance_id,
        ):
            raise WindowsTrayError("旧 AHA Web 实例未完全退出，设置未应用")
        if updated.aha_home != previous.aha_home:
            self.run_id = ""
        self.settings = updated
        try:
            save_tray_settings(updated, self.config_path)
            self.start()
            if self.web.process is not None and self.web.process.poll() is not None:
                raise WindowsTrayError("新设置下的 AHA Web 服务启动失败，请检查 bind 和端口")
            if startup_was_enabled and not updated.startup_task_name:
                set_startup_enabled(True, self.startup_command())
        except Exception:
            self._stop_service()
            self.settings = previous
            self.run_id = previous_run_id
            save_tray_settings(previous, self.config_path)
            self.start()
            raise


def _show_error(message: str) -> None:
    with contextlib.suppress(Exception):
        ctypes.windll.user32.MessageBoxW(None, message, "AHA", 0x10)


def _show_settings_dialog(parent: int, settings: TraySettings) -> TraySettings | None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    gdi32 = ctypes.windll.gdi32
    lresult = ctypes.c_ssize_t
    wndproc_type = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WndClass(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", wndproc_type),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WndClass)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = lresult
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = lresult
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.LoadCursorW.restype = wintypes.HANDLE
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.EnableWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.UpdateWindow.restype = wintypes.BOOL
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user32.MessageBoxW.restype = ctypes.c_int
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = lresult
    user32.IsDialogMessageW.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.MSG)]
    user32.IsDialogMessageW.restype = wintypes.BOOL
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    gdi32.GetStockObject.restype = wintypes.HANDLE

    save_id, cancel_id = 2001, 2002
    controls: dict[str, int] = {}
    result: list[TraySettings] = []

    def control_text(name: str) -> str:
        handle = controls[name]
        length = user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, len(buffer))
        return buffer.value

    @wndproc_type
    def dialog_proc(window, message, wparam, lparam):
        if message == 0x0111:
            control_id = int(wparam) & 0xFFFF
            if control_id == save_id:
                try:
                    updated = TraySettings(
                        Path(control_text("home")),
                        control_text("bind"),
                        int(control_text("port")),
                        control_text("token"),
                        settings.startup_task_name,
                    ).normalized()
                    if bind_host_exposes_network(updated.bind) and not updated.web_token:
                        confirmed = user32.MessageBoxW(
                            window,
                            "当前 Bind 会暴露到本机以外，但 Web Token 为空。确认继续吗？",
                            "AHA 安全提示",
                            0x00000004 | 0x00000030,
                        )
                        if confirmed != 6:
                            return 0
                except (OSError, ValueError, WindowsTrayError) as exc:
                    _show_error(str(exc))
                    return 0
                result.append(updated)
                user32.DestroyWindow(window)
                return 0
            if control_id == cancel_id:
                user32.DestroyWindow(window)
                return 0
        if message == 0x0010:
            user32.DestroyWindow(window)
            return 0
        return user32.DefWindowProcW(window, message, wparam, lparam)

    instance = kernel32.GetModuleHandleW(None)
    class_name = f"AHA.TraySettings.{os.getpid()}.{time.monotonic_ns()}"
    cursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
    window_class = WndClass(0, dialog_proc, 0, 0, instance, None, cursor, wintypes.HBRUSH(6), None, class_name)
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        raise WindowsTrayError("无法创建 AHA 托盘设置窗口")
    dialog = None
    try:
        width, height = 520, 300
        x = max(0, (user32.GetSystemMetrics(0) - width) // 2)
        y = max(0, (user32.GetSystemMetrics(1) - height) // 2)
        dialog = user32.CreateWindowExW(
            0,
            class_name,
            "AHA 托盘设置",
            0x00C00000 | 0x00080000,
            x,
            y,
            width,
            height,
            parent,
            None,
            instance,
            None,
        )
        if not dialog:
            raise WindowsTrayError("无法打开 AHA 托盘设置窗口")
        font = gdi32.GetStockObject(17)

        def add_control(kind: str, text: str, style: int, left: int, top: int, control_width: int, control_id: int = 0) -> int:
            handle = user32.CreateWindowExW(
                0,
                kind,
                text,
                style,
                left,
                top,
                control_width,
                25,
                dialog,
                ctypes.c_void_p(control_id) if control_id else None,
                instance,
                None,
            )
            if not handle:
                raise WindowsTrayError("无法创建 AHA 托盘设置控件")
            user32.SendMessageW(handle, 0x0030, font, True)
            return handle

        label_style = 0x50000000
        edit_style = 0x50810080
        add_control("STATIC", "AHA_HOME", label_style, 22, 25, 100)
        controls["home"] = add_control("EDIT", str(settings.aha_home), edit_style, 130, 22, 350)
        add_control("STATIC", "Bind", label_style, 22, 72, 100)
        controls["bind"] = add_control("EDIT", settings.bind, edit_style, 130, 69, 350)
        add_control("STATIC", "Port", label_style, 22, 119, 100)
        controls["port"] = add_control("EDIT", str(settings.port), edit_style, 130, 116, 350)
        add_control("STATIC", "Web Token", label_style, 22, 166, 100)
        controls["token"] = add_control("EDIT", settings.web_token, edit_style | 0x20, 130, 163, 350)
        add_control("BUTTON", "保存并重启", 0x50010001, 270, 218, 100, save_id)
        add_control("BUTTON", "取消", 0x50010000, 380, 218, 100, cancel_id)

        user32.EnableWindow(parent, False)
        user32.ShowWindow(dialog, 5)
        user32.UpdateWindow(dialog)
        message = wintypes.MSG()
        while user32.IsWindow(dialog):
            status = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if status <= 0:
                break
            if not user32.IsDialogMessageW(dialog, ctypes.byref(message)):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
    finally:
        user32.EnableWindow(parent, True)
        user32.SetForegroundWindow(parent)
        if dialog and user32.IsWindow(dialog):
            user32.DestroyWindow(dialog)
        user32.UnregisterClassW(class_name, instance)
    return result[0] if result else None


def _run_native_tray(
    *,
    runtime: TrayRuntime,
    icon_path: Path,
) -> None:
    """Run a small Win32 notification-area icon and message loop."""
    _require_windows()
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    lresult = ctypes.c_ssize_t
    wndproc_type = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WndClass(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", wndproc_type),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class Guid(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class NotifyIconData(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", Guid),
            ("hBalloonIcon", wintypes.HICON),
        ]

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WndClass)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.UnregisterClassW.restype = wintypes.BOOL
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = lresult
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        ctypes.POINTER(wintypes.RECT),
    ]
    user32.TrackPopupMenu.restype = wintypes.UINT
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.DestroyMenu.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user32.MessageBoxW.restype = ctypes.c_int
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.LoadIconW.restype = wintypes.HICON
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.DestroyIcon.restype = wintypes.BOOL
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = lresult
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NotifyIconData)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    callback_message = 0x0400 + 20
    taskbar_created_message = user32.RegisterWindowMessageW("TaskbarCreated")
    open_id, startup_id, settings_id, restart_id, exit_id = 1001, 1002, 1003, 1004, 1005
    hwnd: int | None = None
    notify_data: NotifyIconData | None = None

    def open_dashboard() -> None:
        webbrowser.open(runtime.dashboard_url())

    def toggle_startup() -> None:
        try:
            command = runtime.startup_command()
            set_startup_enabled(not runtime.login_startup_enabled(), command)
        except (OSError, WindowsTrayError) as exc:
            _show_error(f"无法更新开机自启动：{exc}")

    def edit_settings(window: int) -> None:
        try:
            updated = _show_settings_dialog(window, runtime.settings)
            if updated is None or updated == runtime.settings:
                return
            runtime.apply_settings(updated)
            user32.MessageBoxW(window, "设置已保存，AHA Web 服务已重启。", "AHA", 0x40)
        except (OSError, WindowsTrayError) as exc:
            _show_error(f"无法应用设置：{exc}")

    def show_menu(window: int) -> None:
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            checked = 0x00000008 if runtime.login_startup_enabled() else 0
        except (OSError, WindowsTrayError) as exc:
            checked = 0
            _show_error(f"无法读取开机自启动设置：{exc}")
        user32.AppendMenuW(menu, 0, open_id, "打开 AHA")
        user32.AppendMenuW(menu, 0x00000800, 0, None)
        startup_label = "登录后显示托盘" if runtime.settings.startup_task_name else "开机自启动"
        user32.AppendMenuW(menu, checked, startup_id, startup_label)
        user32.AppendMenuW(menu, 0, settings_id, "设置…")
        user32.AppendMenuW(menu, 0, restart_id, "重启 AHA 服务")
        user32.AppendMenuW(menu, 0x00000800, 0, None)
        user32.AppendMenuW(menu, 0, exit_id, "退出 AHA")
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(window)
        selected = user32.TrackPopupMenu(menu, 0x0100 | 0x0002, point.x, point.y, 0, window, None)
        user32.PostMessageW(window, 0, 0, 0)
        user32.DestroyMenu(menu)
        if selected == open_id:
            open_dashboard()
        elif selected == startup_id:
            toggle_startup()
        elif selected == settings_id:
            edit_settings(window)
        elif selected == restart_id:
            try:
                runtime.restart()
            except WindowsTrayError as exc:
                _show_error(str(exc))
        elif selected == exit_id:
            user32.DestroyWindow(window)

    @wndproc_type
    def wndproc(window, message, wparam, lparam):
        if message == taskbar_created_message and notify_data is not None:
            shell32.Shell_NotifyIconW(0x00000000, ctypes.byref(notify_data))
            return 0
        if message == callback_message:
            event = int(lparam) & 0xFFFF
            if event == 0x0203:
                open_dashboard()
            elif event in {0x0205, 0x007B}:
                show_menu(window)
            return 0
        if message == 0x0010:
            user32.DestroyWindow(window)
            return 0
        if message == 0x0002:
            if notify_data is not None:
                shell32.Shell_NotifyIconW(0x00000002, ctypes.byref(notify_data))
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(window, message, wparam, lparam)

    instance = kernel32.GetModuleHandleW(None)
    class_name = f"AHA.WindowsTray.{os.getpid()}"
    custom_icon = user32.LoadImageW(None, str(icon_path), 1, 32, 32, 0x00000010)
    icon = custom_icon or user32.LoadIconW(None, ctypes.c_void_p(32512))
    window_class = WndClass(0, wndproc, 0, 0, instance, icon, None, None, None, class_name)
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        raise WindowsTrayError("failed to register the AHA tray window")
    try:
        hwnd = user32.CreateWindowExW(0, class_name, "AHA", 0, 0, 0, 0, 0, None, None, instance, None)
        if not hwnd:
            raise WindowsTrayError("failed to create the AHA tray window")
        notify_data = NotifyIconData()
        notify_data.cbSize = ctypes.sizeof(NotifyIconData)
        notify_data.hWnd = hwnd
        notify_data.uID = 1
        notify_data.uFlags = 0x00000001 | 0x00000002 | 0x00000004
        notify_data.uCallbackMessage = callback_message
        notify_data.hIcon = icon
        notify_data.szTip = "AHA"
        if not shell32.Shell_NotifyIconW(0x00000000, ctypes.byref(notify_data)):
            raise WindowsTrayError("failed to add the AHA notification-area icon")
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    finally:
        if hwnd and user32.IsWindow(hwnd):
            user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(class_name, instance)
        if custom_icon:
            user32.DestroyIcon(custom_icon)


def wait_for_dashboard(
    host: str,
    port: int,
    timeout_seconds: float = 5.0,
    *,
    expected_instance_id: str = "",
) -> bool:
    health_url = f"{dashboard_url(host, port).rstrip('/')}/api/health"
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        try:
            with urlopen(health_url, timeout=0.25) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if int(getattr(response, "status", 0)) == 200 and (
                    not expected_instance_id or payload.get("instance_id") == expected_instance_id
                ):
                    return True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    return False


def dashboard_instance_id(host: str, port: int) -> str:
    health_url = f"{dashboard_url(host, port).rstrip('/')}/api/health"
    try:
        with urlopen(health_url, timeout=0.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            status = int(getattr(response, "status", 0))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if status != 200:
        return ""
    return str(payload.get("instance_id") or "").strip()


def wait_for_dashboard_shutdown(
    host: str,
    port: int,
    instance_id: str,
    timeout_seconds: float = 5.0,
) -> bool:
    health_url = f"{dashboard_url(host, port).rstrip('/')}/api/health"
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        try:
            with urlopen(health_url, timeout=0.25) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if payload.get("instance_id") != instance_id:
                    return True
        except OSError:
            return True
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    return False


def run_windows_tray(
    root: Path,
    run_id: str,
    host: str,
    port: int,
    poll_interval: int,
    *,
    auth_token: str = "",
    auth_token_file: str = "",
    open_browser: bool = False,
    enable_startup: bool = False,
    config_path: Path | None = None,
) -> None:
    _require_windows()
    token = str(auth_token or "").strip()
    if not token and auth_token_file:
        token = Path(auth_token_file).expanduser().read_text(encoding="utf-8").strip()
    stored = load_tray_settings(config_path)
    startup_task_name = stored.startup_task_name if stored is not None else ""
    settings = TraySettings(root, host, port, token, startup_task_name).normalized()
    runtime = TrayRuntime(settings, run_id, poll_interval, config_path=config_path)
    icon_path = materialize_tray_icon(runtime.config_path)
    if enable_startup:
        set_startup_enabled(True, runtime.startup_command())
    mutex = WindowsTrayMutex(settings.aha_home, settings.port)
    if not mutex.acquire():
        mutex.close()
        webbrowser.open(runtime.dashboard_url())
        return
    try:
        runtime.start()
        if open_browser:
            wait_for_dashboard(settings.bind, settings.port)
            webbrowser.open(runtime.dashboard_url())
        _run_native_tray(runtime=runtime, icon_path=icon_path)
    finally:
        runtime.web.stop()
        mutex.close()
