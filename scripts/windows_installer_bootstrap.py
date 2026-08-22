from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import locale
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
from typing import Callable
import zipfile


tk = None
ttk = None
messagebox = None


CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
SEE_MASK_NOCLOSEPROCESS = 0x00000040
INFINITE = 0xFFFFFFFF
STARTUP_TASK_NAME = r"\AHA Web"
BUILD_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)\.(\d{8})\.([A-Za-z0-9_-]+)$")
BUILD_VERSION_ASSIGNMENT_RE = re.compile(r"BUILD_VERSION\s*=\s*['\"]([^'\"]+)['\"]")
INSTALL_STAGE_PREFIX = "AHA_INSTALL_STAGE|"


TEXT = {
    "en": {
        "title": "AHA Setup",
        "subtitle": "Agent Help Agent · Local AI agent workbench",
        "intro": "Install or upgrade AHA for the current Windows user.",
        "mode": "Installation mode",
        "backend": "Agent backend",
        "install_path": "Program path",
        "home_path": "Data path",
        "port": "Web port",
        "options": "Options",
        "chromium": "Download Playwright Chromium",
        "startup": "Start Web service before sign-in",
        "shortcut": "Create Start Menu shortcut",
        "start": "Start AHA after installation",
        "strict": "Fail when an optional dependency fails",
        "repair": "Repair existing installation",
        "action_install": "Next: Install {version}",
        "action_upgrade": "Next: Upgrade {current} → {target}",
        "action_repair": "Next: Repair {version}",
        "action_downgrade": "Next: Downgrade {current} → {target}",
        "uninstall": "Uninstall",
        "close": "Close",
        "ready": "Ready",
        "running": "Installing…",
        "uninstalling": "Uninstalling…",
        "done": "AHA installation completed.",
        "removed": "AHA installed files were removed. AHA data was retained.",
        "failed": "Installation failed with exit code {code}.",
        "uac": "Administrator approval is required for the startup task.",
        "invalid_port": "Web port must be between 1 and 65535.",
        "busy_close": "Wait for the installation process to finish before closing this window.",
        "confirm_uninstall": "Remove AHA installed files and startup entries? AHA data will be retained.",
        "confirm_downgrade": "Downgrade AHA from {current} to {target}? This requires explicit confirmation.",
        "registered_path": "This Windows user already has one registered AHA installation. Its program path is locked.",
        "versions": "Installed: {current} · Package: {target}",
        "details": "Installation details",
        "stage_preflight": "Checking installation ownership and settings",
        "stage_runtime": "Resolving Python runtime",
        "stage_runtime_ready": "Python runtime is ready",
        "stage_core": "Validating and installing AHA core",
        "stage_core_ready": "AHA core is installed",
        "stage_modules": "Installing optional modules and agent tools",
        "stage_modules_ready": "Optional modules are processed",
        "stage_configuration": "Writing configuration and Web token",
        "stage_service": "Configuring startup and service integration",
        "stage_integration": "Creating shortcuts and final integration",
        "stage_launch": "Starting AHA",
        "stage_uninstall": "Removing registered startup integration",
        "stage_uac": "Waiting for administrator approval",
        "stage_complete": "Completed",
    },
    "zh": {
        "title": "AHA 安装向导",
        "subtitle": "Agent Help Agent · 本地 AI Agent 工作台",
        "intro": "为当前 Windows 用户安装或升级 AHA。",
        "mode": "安装模式",
        "backend": "Agent Backend",
        "install_path": "程序路径",
        "home_path": "数据路径",
        "port": "Web 端口",
        "options": "安装选项",
        "chromium": "下载 Playwright Chromium",
        "startup": "登录前启动 Web 服务",
        "shortcut": "创建开始菜单快捷方式",
        "start": "安装后启动 AHA",
        "strict": "可选依赖失败时终止安装",
        "repair": "修复现有安装",
        "action_install": "下一步：安装 {version}",
        "action_upgrade": "下一步：升级 {current} → {target}",
        "action_repair": "下一步：修复 {version}",
        "action_downgrade": "下一步：降级 {current} → {target}",
        "uninstall": "卸载",
        "close": "关闭",
        "ready": "准备就绪",
        "running": "正在安装…",
        "uninstalling": "正在卸载…",
        "done": "AHA 安装完成。",
        "removed": "AHA 程序和启动项已移除，用户数据已保留。",
        "failed": "安装失败，退出码：{code}。",
        "uac": "配置开机任务需要管理员授权。",
        "invalid_port": "Web 端口必须在 1 到 65535 之间。",
        "busy_close": "请等待安装流程结束后再关闭窗口。",
        "confirm_uninstall": "确定移除 AHA 程序和启动项吗？用户数据会保留。",
        "confirm_downgrade": "确定将 AHA 从 {current} 降级到 {target} 吗？降级必须显式确认。",
        "registered_path": "当前 Windows 用户已有一个登记的 AHA 安装，程序路径已锁定。",
        "versions": "已安装：{current} · 安装包：{target}",
        "details": "安装详情",
        "stage_preflight": "正在检查安装归属和设置",
        "stage_runtime": "正在准备 Python 运行环境",
        "stage_runtime_ready": "Python 运行环境已就绪",
        "stage_core": "正在校验并安装 AHA 核心",
        "stage_core_ready": "AHA 核心已安装",
        "stage_modules": "正在安装可选模块和 Agent 工具",
        "stage_modules_ready": "可选模块处理完成",
        "stage_configuration": "正在写入配置和 Web Token",
        "stage_service": "正在配置服务与启动项",
        "stage_integration": "正在创建快捷方式和最终集成",
        "stage_launch": "正在启动 AHA",
        "stage_uninstall": "正在移除登记的启动集成",
        "stage_uac": "正在等待管理员授权",
        "stage_complete": "已完成",
    },
}


def load_tkinter():
    global tk, ttk, messagebox
    if tk is None:
        import tkinter as tk_module
        from tkinter import messagebox as messagebox_module
        from tkinter import ttk as ttk_module

        tk = tk_module
        ttk = ttk_module
        messagebox = messagebox_module
    return tk, ttk, messagebox


def ui_language() -> str:
    if sys.platform == "win32":
        try:
            language_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            if language_id & 0x3FF == 0x04:
                return "zh"
        except (AttributeError, OSError, ValueError):
            pass
    language = str(locale.getlocale()[0] or "").lower()
    return "zh" if language.startswith("zh") else "en"


def payload_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", "")
    return Path(str(bundled)).resolve() if bundled else Path(__file__).resolve().parent


def bundled_payload(name: str) -> Path:
    path = payload_root() / "payload" / name
    if not path.is_file():
        raise FileNotFoundError(f"installer payload is missing: {path}")
    return path


def powershell_executable() -> str:
    system_root = str(os.environ.get("SystemRoot") or r"C:\Windows")
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate if candidate.is_file() else "powershell.exe")


def artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_version(value: object) -> str:
    text = str(value or "").strip()
    return text[4:].strip() if text.startswith("aha ") else text


def display_version(value: object) -> str:
    text = normalize_version(value)
    match = BUILD_VERSION_RE.fullmatch(text)
    if match:
        return f"v{match.group(1)}.{match.group(2)}.{match.group(3)}"
    return text[:24] or "-"


def compare_build_versions(left: object, right: object) -> int | None:
    left_match = BUILD_VERSION_RE.fullmatch(normalize_version(left))
    right_match = BUILD_VERSION_RE.fullmatch(normalize_version(right))
    if not left_match or not right_match:
        return None
    left_parts = tuple(int(left_match.group(index)) for index in range(1, 5))
    right_parts = tuple(int(right_match.group(index)) for index in range(1, 5))
    return 1 if left_parts > right_parts else -1 if left_parts < right_parts else 0


def onebin_version(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            source = archive.read("aha_cli/_build_version.py").decode("utf-8", errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return ""
    match = BUILD_VERSION_ASSIGNMENT_RE.search(source)
    return normalize_version(match.group(1)) if match else ""


def bundled_version() -> str:
    return onebin_version(bundled_payload("aha"))


def read_install_report(install_dir: object) -> dict:
    path = Path(str(install_dir or "")).expanduser() / "install-report.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def registered_installation() -> dict:
    if sys.platform != "win32":
        return {}
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\AHA") as key:
            def value(name: str) -> str:
                try:
                    return str(winreg.QueryValueEx(key, name)[0] or "").strip()
                except OSError:
                    return ""

            registration = {
                "installation_id": value("InstallationId"),
                "install_dir": value("InstallDir"),
                "install_bin": value("InstallBin"),
                "aha_home": value("AhaHome"),
                "python": value("Python"),
                "version": normalize_version(value("Version")),
            }
    except OSError:
        registration = {}
    if registration:
        return registration
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if not local_app_data:
        return {}
    legacy_dir = Path(local_app_data) / "AHA"
    report = read_install_report(legacy_dir)
    install_bin = Path(str(report.get("install_bin") or legacy_dir / "aha"))
    if not install_bin.is_file() and not report:
        return {}
    return {
        "installation_id": str(report.get("installation_id") or ""),
        "install_dir": str(legacy_dir),
        "install_bin": str(install_bin),
        "aha_home": str(Path(os.environ.get("USERPROFILE") or Path.home()) / ".aha"),
        "python": str(report.get("python") or ""),
        "version": normalize_version(report.get("version")),
        "legacy": True,
    }


def installation_action(install_dir: object, registered: dict | None = None) -> dict:
    directory = Path(str(install_dir or "")).expanduser()
    registration = registered or {}
    report = read_install_report(directory)
    install_bin = Path(str(registration.get("install_bin") or directory / "aha"))
    current = normalize_version(
        report.get("version")
        or registration.get("version")
        or onebin_version(install_bin)
    )
    target = bundled_version()
    installed = install_bin.is_file() or bool(report)
    if not installed:
        action = "install"
    elif not current or not target:
        action = "repair"
    else:
        comparison = compare_build_versions(target, current)
        action = "upgrade" if comparison == 1 else "downgrade" if comparison == -1 else "repair"
    return {
        "action": action,
        "current": current,
        "target": target,
        "install_bin": str(install_bin),
    }


def parse_installer_stage(line: object) -> tuple[int, str, str] | None:
    text = str(line or "").strip()
    if not text.startswith(INSTALL_STAGE_PREFIX):
        return None
    parts = text.split("|", 3)
    if len(parts) != 4:
        return None
    try:
        percent = max(0, min(100, int(parts[1])))
    except ValueError:
        return None
    return percent, parts[2].strip(), parts[3].strip()


def build_installer_command(
    args: argparse.Namespace,
    *,
    progress_file: Path | None = None,
) -> list[str]:
    installer = bundled_payload("install_windows.ps1")
    artifact = bundled_payload("aha")
    command = [
        powershell_executable(),
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(installer),
    ]
    if not args.uninstall:
        command.extend(["-Artifact", str(artifact), "-Sha256", artifact_sha256(artifact)])
    command.extend(["-Mode", args.mode, "-AgentBackend", args.agent_backend])
    for name, value in (
        ("-AhaDir", args.aha_dir),
        ("-AhaHome", args.aha_home),
        ("-Bind", args.bind),
    ):
        if value:
            command.extend([name, str(value)])
    if args.port:
        command.extend(["-Port", str(args.port)])
    if progress_file is not None:
        command.extend(["-ProgressFile", str(progress_file)])
    for enabled, switch in (
        (args.repair, "-Repair"),
        (args.strict_modules, "-StrictModules"),
        (args.with_browser, "-WithBrowser"),
        (args.skip_browser_download, "-SkipBrowserDownload"),
        (args.enable_startup, "-EnableStartup"),
        (args.allow_downgrade, "-AllowDowngrade"),
        (args.uninstall, "-Uninstall"),
        (args.no_shortcut, "-NoShortcut"),
        (args.no_start, "-NoStart"),
        (args.no_auth, "-NoAuth"),
        (args.allow_unsafe_bind, "-AllowUnsafeBind"),
    ):
        if enabled:
            command.append(switch)
    return command


def startup_task_exists() -> bool:
    try:
        completed = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", STARTUP_TASK_NAME],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def is_administrator() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def run_elevated(command: list[str]) -> int:
    if sys.platform != "win32":
        raise OSError("elevation is available only on Windows")
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = command[0]
    info.lpParameters = subprocess.list2cmdline(command[1:])
    info.lpDirectory = str(Path.home())
    info.nShow = 0
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        kernel32.WaitForSingleObject(info.hProcess, INFINITE)
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def requires_elevation(args: argparse.Namespace) -> bool:
    return bool(args.enable_startup or args.uninstall or startup_task_exists()) and not is_administrator()


def run_installer(args: argparse.Namespace, emit: Callable[[str], None] | None = None) -> int:
    if requires_elevation(args):
        with tempfile.NamedTemporaryFile(prefix="aha-installer-progress-", suffix=".log", delete=False) as handle:
            progress_path = Path(handle.name)
        command = build_installer_command(args, progress_file=progress_path)
        stop_monitor = threading.Event()

        def monitor_progress() -> None:
            delivered = 0
            while not stop_monitor.is_set():
                try:
                    lines = progress_path.read_text(encoding="utf-8-sig").splitlines()
                except OSError:
                    lines = []
                for line in lines[delivered:]:
                    if emit:
                        emit(line)
                delivered = len(lines)
                stop_monitor.wait(0.2)
            try:
                lines = progress_path.read_text(encoding="utf-8-sig").splitlines()
            except OSError:
                lines = []
            for line in lines[delivered:]:
                if emit:
                    emit(line)

        if emit:
            emit(f"{INSTALL_STAGE_PREFIX}8|uac|Waiting for administrator approval")
        monitor = threading.Thread(target=monitor_progress, daemon=True)
        monitor.start()
        try:
            return run_elevated(command)
        finally:
            stop_monitor.set()
            monitor.join(timeout=2)
            try:
                progress_path.unlink()
            except OSError:
                pass
    command = build_installer_command(args)
    process = subprocess.Popen(
        command,
        cwd=str(payload_root()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding=locale.getpreferredencoding(False) or "utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    assert process.stdout is not None
    for line in process.stdout:
        if emit:
            emit(line.rstrip())
    return int(process.wait())


class InstallerWizard:
    def __init__(self, args: argparse.Namespace) -> None:
        load_tkinter()
        self.args = args
        self.language = ui_language()
        self.t = TEXT[self.language]
        self.root = tk.Tk()
        self.root.title(self.t["title"])
        screen_width = max(640, self.root.winfo_screenwidth())
        screen_height = max(560, self.root.winfo_screenheight())
        window_width = max(640, min(760, screen_width - 60))
        window_height = max(540, min(680, screen_height - 80))
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(620, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        try:
            self.root.iconbitmap(default=str(bundled_payload("aha.ico")))
        except (OSError, tk.TclError):
            pass
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.running = False
        self.registration = registered_installation()
        self.mode = tk.StringVar(value=args.mode)
        self.backend = tk.StringVar(value=args.agent_backend)
        registered_dir = str(self.registration.get("install_dir") or "").strip()
        registered_home = str(self.registration.get("aha_home") or "").strip()
        self.aha_dir = tk.StringVar(
            value=registered_dir
            or args.aha_dir
            or str(Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AHA")
        )
        self.aha_home = tk.StringVar(value=args.aha_home or registered_home or str(Path.home() / ".aha"))
        self.port = tk.StringVar(value=str(args.port or 8788))
        self.with_browser = tk.BooleanVar(value=bool(args.with_browser))
        self.enable_startup = tk.BooleanVar(value=bool(args.enable_startup))
        self.create_shortcut = tk.BooleanVar(value=not args.no_shortcut)
        self.start_after = tk.BooleanVar(value=not args.no_start)
        self.strict_modules = tk.BooleanVar(value=bool(args.strict_modules))
        self.repair = tk.BooleanVar(value=bool(args.repair))
        self.status = tk.StringVar(value=self.t["ready"])
        self.install_action_text = tk.StringVar()
        self.action_info: dict = {}
        self._build()
        self.aha_dir.trace_add("write", lambda *_args: self._refresh_install_action())
        self._refresh_install_action()
        self.root.after(100, self._poll_messages)

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        canvas = tk.Canvas(self.root, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(self.root, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        outer = ttk.Frame(canvas, padding=(22, 18, 18, 14))
        canvas_window = canvas.create_window((0, 0), window=outer, anchor="nw")

        def sync_scroll_region(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def sync_content_width(event) -> None:
            canvas.itemconfigure(canvas_window, width=event.width)

        def scroll_content(event) -> None:
            if canvas.winfo_height() < outer.winfo_reqheight():
                canvas.yview_scroll(int(-event.delta / 120), "units")

        outer.bind("<Configure>", sync_scroll_region)
        canvas.bind("<Configure>", sync_content_width)
        canvas.bind_all("<MouseWheel>", scroll_content)

        ttk.Label(outer, text="AHA", font=("Segoe UI", 28, "bold")).pack(anchor="w")
        ttk.Label(outer, text=self.t["subtitle"], font=("Segoe UI", 11)).pack(anchor="w", pady=(0, 4))
        ttk.Label(outer, text=self.t["intro"]).pack(anchor="w", pady=(0, 18))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        ttk.Label(form, text=self.t["mode"]).grid(row=0, column=0, sticky="w", pady=5)
        ttk.Combobox(form, textvariable=self.mode, values=("Full", "Minimal"), state="readonly", width=20).grid(
            row=0, column=1, sticky="ew", padx=(16, 0), pady=5
        )
        ttk.Label(form, text=self.t["backend"]).grid(row=1, column=0, sticky="w", pady=5)
        ttk.Combobox(
            form,
            textvariable=self.backend,
            values=("Auto", "Codex", "Claude", "Both", "None"),
            state="readonly",
            width=20,
        ).grid(row=1, column=1, sticky="ew", padx=(16, 0), pady=5)
        ttk.Label(form, text=self.t["install_path"]).grid(row=2, column=0, sticky="w", pady=5)
        self.install_path_entry = ttk.Entry(form, textvariable=self.aha_dir)
        self.install_path_entry.grid(row=2, column=1, sticky="ew", padx=(16, 0), pady=5)
        if self.registration:
            self.install_path_entry.configure(state="readonly")
        ttk.Label(form, text=self.t["home_path"]).grid(row=3, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.aha_home).grid(row=3, column=1, sticky="ew", padx=(16, 0), pady=5)
        ttk.Label(form, text=self.t["port"]).grid(row=4, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.port, width=12).grid(row=4, column=1, sticky="w", padx=(16, 0), pady=5)
        form.columnconfigure(1, weight=1)
        if self.registration:
            ttk.Label(outer, text=self.t["registered_path"]).pack(anchor="w", pady=(6, 0))

        options = ttk.LabelFrame(outer, text=self.t["options"], padding=12)
        options.pack(fill="x", pady=(16, 12))
        ttk.Checkbutton(options, text=self.t["chromium"], variable=self.with_browser).grid(
            row=0, column=0, sticky="w", padx=(0, 24), pady=4
        )
        ttk.Checkbutton(options, text=self.t["startup"], variable=self.enable_startup).grid(
            row=0, column=1, sticky="w", pady=4
        )
        ttk.Checkbutton(options, text=self.t["shortcut"], variable=self.create_shortcut).grid(
            row=1, column=0, sticky="w", padx=(0, 24), pady=4
        )
        ttk.Checkbutton(options, text=self.t["start"], variable=self.start_after).grid(
            row=1, column=1, sticky="w", pady=4
        )
        ttk.Checkbutton(options, text=self.t["strict"], variable=self.strict_modules).grid(
            row=2, column=0, sticky="w", padx=(0, 24), pady=4
        )
        ttk.Checkbutton(options, text=self.t["repair"], variable=self.repair).grid(
            row=2, column=1, sticky="w", pady=4
        )
        options.columnconfigure(0, weight=1)
        options.columnconfigure(1, weight=1)

        ttk.Label(outer, text=self.t["details"]).pack(anchor="w")
        self.log = tk.Text(outer, height=8, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(fill="x", pady=(4, 0))

        footer = ttk.Frame(self.root, padding=(22, 10, 22, 18))
        footer.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=100, value=0)
        self.progress.pack(fill="x")
        ttk.Label(footer, textvariable=self.status).pack(anchor="w", pady=(5, 10))

        buttons = ttk.Frame(footer)
        buttons.pack(fill="x")
        self.install_button = ttk.Button(buttons, textvariable=self.install_action_text, command=self.install)
        self.install_button.pack(side="right")
        self.uninstall_button = ttk.Button(buttons, text=self.t["uninstall"], command=self.uninstall)
        self.uninstall_button.pack(side="left")
        self.close_button = ttk.Button(buttons, text=self.t["close"], command=self.close)
        self.close_button.pack(side="right", padx=(0, 8))

    def _refresh_install_action(self) -> None:
        self.action_info = installation_action(self.aha_dir.get(), self.registration)
        action = str(self.action_info.get("action") or "install")
        current = display_version(self.action_info.get("current"))
        target = display_version(self.action_info.get("target"))
        if action == "upgrade":
            label = self.t["action_upgrade"].format(current=current, target=target)
        elif action == "downgrade":
            label = self.t["action_downgrade"].format(current=current, target=target)
        elif action == "repair":
            label = self.t["action_repair"].format(version=current if current != "-" else target)
        else:
            label = self.t["action_install"].format(version=target)
        self.install_action_text.set(label)
        if not self.running:
            if action == "install":
                self.status.set(self.t["ready"])
            else:
                self.status.set(self.t["versions"].format(current=current, target=target))

    def _current_args(
        self,
        *,
        uninstall: bool = False,
        allow_downgrade: bool = False,
    ) -> argparse.Namespace:
        try:
            port = int(self.port.get())
        except ValueError as exc:
            raise ValueError(self.t["invalid_port"]) from exc
        if not 1 <= port <= 65535:
            raise ValueError(self.t["invalid_port"])
        return argparse.Namespace(
            mode=self.mode.get(),
            agent_backend=self.backend.get(),
            aha_dir=self.aha_dir.get().strip(),
            aha_home=self.aha_home.get().strip(),
            bind=self.args.bind or "127.0.0.1",
            port=port,
            repair=bool(self.repair.get() or self.action_info.get("action") == "repair"),
            strict_modules=bool(self.strict_modules.get()),
            with_browser=bool(self.with_browser.get()),
            skip_browser_download=bool(self.args.skip_browser_download),
            enable_startup=bool(self.enable_startup.get()),
            allow_downgrade=bool(allow_downgrade),
            uninstall=uninstall,
            no_shortcut=not bool(self.create_shortcut.get()),
            no_start=not bool(self.start_after.get()),
            no_auth=bool(self.args.no_auth),
            allow_unsafe_bind=bool(self.args.allow_unsafe_bind),
        )

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def install(self) -> None:
        allow_downgrade = False
        if self.action_info.get("action") == "downgrade":
            current = display_version(self.action_info.get("current"))
            target = display_version(self.action_info.get("target"))
            allow_downgrade = messagebox.askyesno(
                self.t["title"],
                self.t["confirm_downgrade"].format(current=current, target=target),
                parent=self.root,
            )
            if not allow_downgrade:
                return
        self._begin(uninstall=False, allow_downgrade=allow_downgrade)

    def uninstall(self) -> None:
        if not messagebox.askyesno(self.t["title"], self.t["confirm_uninstall"], parent=self.root):
            return
        self._begin(uninstall=True)

    def _begin(self, *, uninstall: bool, allow_downgrade: bool = False) -> None:
        if self.running:
            return
        try:
            args = self._current_args(uninstall=uninstall, allow_downgrade=allow_downgrade)
        except ValueError as exc:
            messagebox.showerror(self.t["title"], str(exc), parent=self.root)
            return
        self.running = True
        self.install_button.configure(state="disabled")
        self.uninstall_button.configure(state="disabled")
        self.progress.configure(value=0)
        self.status.set(self.t["uninstalling"] if uninstall else self.t["running"])
        thread = threading.Thread(target=self._worker, args=(args,), daemon=True)
        thread.start()

    def _worker(self, args: argparse.Namespace) -> None:
        def emit(text: str) -> None:
            stage = parse_installer_stage(text)
            self.messages.put(("stage", stage) if stage else ("log", text))

        try:
            code = run_installer(args, emit=emit)
        except Exception as exc:  # noqa: BLE001
            self.messages.put(("error", str(exc)))
            return
        self.messages.put(("done", (code, bool(args.uninstall))))

    def _poll_messages(self) -> None:
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(str(payload))
            elif kind == "stage":
                percent, name, fallback = payload
                self.progress.configure(value=int(percent))
                self.status.set(self.t.get(f"stage_{name}", str(fallback)))
            elif kind == "error":
                self._finish(1, False, str(payload))
            elif kind == "done":
                code, uninstall = payload
                self._finish(int(code), bool(uninstall))
        self.root.after(100, self._poll_messages)

    def _finish(self, code: int, uninstall: bool, details: str = "") -> None:
        self.running = False
        self.install_button.configure(state="normal")
        self.uninstall_button.configure(state="normal")
        if details:
            self.append_log(details)
        if code == 0:
            self.progress.configure(value=100)
            self.registration = registered_installation()
            registered_dir = str(self.registration.get("install_dir") or "").strip()
            if registered_dir:
                self.aha_dir.set(registered_dir)
                self.install_path_entry.configure(state="readonly")
            else:
                self.install_path_entry.configure(state="normal")
            self._refresh_install_action()
            message = self.t["removed"] if uninstall else self.t["done"]
            self.status.set(message)
            messagebox.showinfo(self.t["title"], message, parent=self.root)
        else:
            message = self.t["failed"].format(code=code)
            self.status.set(message)
            messagebox.showerror(self.t["title"], message, parent=self.root)

    def close(self) -> None:
        if self.running:
            messagebox.showinfo(self.t["title"], self.t["busy_close"], parent=self.root)
            return
        self.root.destroy()

    def run(self) -> int:
        self.root.mainloop()
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install AHA for the current Windows user.")
    parser.add_argument("--mode", choices=["Minimal", "Full"], default="Full")
    parser.add_argument("--agent-backend", choices=["Auto", "Codex", "Claude", "Both", "None"], default="Auto")
    parser.add_argument("--aha-dir", default="")
    parser.add_argument("--aha-home", default="")
    parser.add_argument("--bind", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--strict-modules", action="store_true")
    parser.add_argument("--with-browser", action="store_true")
    parser.add_argument("--skip-browser-download", action="store_true")
    parser.add_argument("--enable-startup", action="store_true")
    parser.add_argument("--allow-downgrade", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--no-shortcut", action="store_true")
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument("--allow-unsafe-bind", action="store_true")
    parser.add_argument("--silent", action="store_true", help="Run without the GUI")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.smoke_test:
        try:
            for name in ("aha", "install_windows.ps1", "aha.ico"):
                bundled_payload(name)
        except OSError:
            return 1
        return 0
    try:
        if args.silent:
            return run_installer(args)
        return InstallerWizard(args).run()
    except Exception as exc:  # noqa: BLE001
        try:
            tk_module, _, messagebox_module = load_tkinter()
            root = tk_module.Tk()
            root.withdraw()
            messagebox_module.showerror(TEXT[ui_language()]["title"], str(exc), parent=root)
            root.destroy()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
