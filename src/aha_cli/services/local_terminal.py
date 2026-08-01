from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass
import errno
import locale
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import threading
from typing import BinaryIO

from aha_cli import platform as _platform
from aha_cli.process_control import assign_parent_death


DEFAULT_TERMINAL_COLS = 100
DEFAULT_TERMINAL_ROWS = 28
LOCAL_TERMINAL_SHELL_IDS = ("auto", "pwsh", "powershell", "cmd", "wsl")


@dataclass(frozen=True)
class LocalTerminalShell:
    id: str
    label: str
    executable: str
    command: tuple[str, ...]


def default_shell() -> str:
    return _platform.default_shell()


def normalize_terminal_size(cols: object, rows: object) -> tuple[int, int]:
    try:
        normalized_cols = int(cols)
    except (TypeError, ValueError):
        normalized_cols = DEFAULT_TERMINAL_COLS
    try:
        normalized_rows = int(rows)
    except (TypeError, ValueError):
        normalized_rows = DEFAULT_TERMINAL_ROWS
    return max(20, min(normalized_cols, 240)), max(8, min(normalized_rows, 80))


def _existing_windows_executable(name: str, *candidates: str) -> str | None:
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and Path(value).is_file():
            return value
    resolved = shutil.which(name)
    return str(resolved) if resolved else None


def _wsl_has_distribution(executable: str) -> bool:
    try:
        result = subprocess.run(
            [executable, "--list", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2.0,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    # wsl.exe writes UTF-16 when stdout is redirected. Removing NUL bytes is
    # sufficient for the only question here: whether at least one distro name
    # was returned.
    return result.returncode == 0 and bool(bytes(result.stdout or b"").replace(b"\0", b"").strip())


def _windows_terminal_shells() -> dict[str, LocalTerminalShell]:
    system_root = str(os.environ.get("SystemRoot") or r"C:\Windows")
    program_files = str(os.environ.get("ProgramFiles") or r"C:\Program Files")
    detected: dict[str, LocalTerminalShell] = {}

    pwsh = _existing_windows_executable("pwsh.exe", str(Path(program_files) / "PowerShell" / "7" / "pwsh.exe"))
    if pwsh:
        detected["pwsh"] = LocalTerminalShell("pwsh", "PowerShell 7", pwsh, (pwsh, "-NoLogo"))

    powershell = _existing_windows_executable(
        "powershell.exe",
        str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
    )
    if powershell:
        detected["powershell"] = LocalTerminalShell(
            "powershell",
            "Windows PowerShell",
            powershell,
            (powershell, "-NoLogo"),
        )

    cmd = _existing_windows_executable(
        "cmd.exe",
        str(os.environ.get("COMSPEC") or ""),
        str(Path(system_root) / "System32" / "cmd.exe"),
    )
    if cmd:
        detected["cmd"] = LocalTerminalShell("cmd", "Command Prompt", cmd, (cmd, "/D", "/Q"))

    wsl = _existing_windows_executable("wsl.exe", str(Path(system_root) / "System32" / "wsl.exe"))
    if wsl and _wsl_has_distribution(wsl):
        detected["wsl"] = LocalTerminalShell("wsl", "WSL", wsl, (wsl,))
    return detected


def local_terminal_shell_options() -> dict:
    """Return safe, detected shell IDs for the Local Terminal picker."""
    if not _platform.is_windows():
        shell = default_shell()
        return {
            "default": "auto",
            "resolved": "default",
            "options": [{"id": "auto", "label": f"Default shell ({Path(shell).name})"}],
        }
    detected = _windows_terminal_shells()
    resolved = next((shell_id for shell_id in ("pwsh", "powershell", "cmd") if shell_id in detected), "")
    options = [{"id": "auto", "label": "Auto", "resolved": resolved}]
    options.extend({"id": shell.id, "label": shell.label} for shell in detected.values())
    return {"default": "auto", "resolved": resolved, "options": options}


def resolve_local_terminal_shell(shell_id: object) -> LocalTerminalShell:
    requested = str(shell_id or "auto").strip().lower() or "auto"
    if requested not in LOCAL_TERMINAL_SHELL_IDS:
        raise ValueError(f"unknown local terminal shell: {requested}")
    if not _platform.is_windows():
        if requested != "auto":
            raise ValueError(f"local terminal shell is not available on this host: {requested}")
        shell = default_shell()
        return LocalTerminalShell("default", f"Default shell ({Path(shell).name})", shell, (shell, "-i"))
    detected = _windows_terminal_shells()
    if requested == "auto":
        requested = next((shell_id for shell_id in ("pwsh", "powershell", "cmd") if shell_id in detected), "")
    selected = detected.get(requested)
    if selected is None:
        raise ValueError(f"local terminal shell is not available on this host: {requested or 'auto'}")
    return selected


class LocalTerminalSession:
    def __init__(self, *, cwd: Path | None = None, shell: str | None = None, shell_id: str = "auto") -> None:
        self.cwd = (cwd or Path.cwd()).expanduser().resolve(strict=False)
        self.requested_shell_id = str(shell_id or "auto").strip().lower() or "auto"
        self._windows_command: list[str] | None = None
        if shell is not None:
            self.shell = shell
            self.shell_id = "custom"
        elif _platform.is_windows():
            selected_shell = resolve_local_terminal_shell(self.requested_shell_id)
            self.shell = selected_shell.executable
            self.shell_id = selected_shell.id
            self._windows_command = list(selected_shell.command)
        else:
            self.shell = default_shell()
            self.shell_id = "default"
        self.master_fd: int | None = None
        self._slave_fd: int | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self._output_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader_attached = False
        self._reader_loop: asyncio.AbstractEventLoop | None = None
        self._reader_thread: threading.Thread | None = None
        self._windows_stdin: BinaryIO | None = None
        self._windows_stdout: BinaryIO | None = None
        self._windows_encoding = "utf-8"
        self._windows_conpty = False

    def start(self, *, cols: int = DEFAULT_TERMINAL_COLS, rows: int = DEFAULT_TERMINAL_ROWS) -> None:
        if self.process is not None:
            return
        if _platform.is_windows():
            self._start_windows(cols=cols, rows=rows)
            return
        import fcntl
        import pty

        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        self._slave_fd = slave_fd
        self.resize(cols=cols, rows=rows)
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        env = os.environ.copy()
        env.update({
            "AHA_LOCAL_TERMINAL": "1",
            "TERM": "xterm-256color",
        })
        self.process = subprocess.Popen(
            [self.shell, "-i"],
            cwd=str(self.cwd),
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        self._slave_fd = None

    def _start_windows(self, *, cols: int, rows: int) -> None:
        env = os.environ.copy()
        env.update({"AHA_LOCAL_TERMINAL": "1", "TERM": "xterm-256color"})
        command = list(self._windows_command or [])
        if not command:
            shell_name = self.shell.replace("\\", "/").rsplit("/", 1)[-1].lower()
            command = [self.shell, "/D", "/Q"] if shell_name in {"cmd", "cmd.exe"} else [self.shell]
        from aha_cli.windows_conpty import ConPtyUnavailable, WindowsConPtyProcess

        try:
            self.process = WindowsConPtyProcess(
                command,
                cwd=self.cwd,
                env=env,
                cols=cols,
                rows=rows,
            )
        except ConPtyUnavailable:
            self._start_windows_pipe(command=command, env=env)
            return
        assign_parent_death(self.process)
        self._windows_stdin = self.process.stdin
        self._windows_stdout = self.process.stdout
        self._windows_encoding = "utf-8"
        self._windows_conpty = True

    def _start_windows_pipe(self, *, command: list[str], env: dict[str, str]) -> None:
        creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        self.process = subprocess.Popen(
            command,
            cwd=str(self.cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=creationflags,
        )
        assign_parent_death(self.process)
        self._windows_stdin = self.process.stdin
        self._windows_stdout = self.process.stdout
        self._windows_encoding = locale.getpreferredencoding(False) or "utf-8"

    def attach_reader(self, loop: asyncio.AbstractEventLoop) -> None:
        if _platform.is_windows():
            if self._windows_stdout is None or self._reader_attached:
                return
            self._reader_attached = True
            self._reader_loop = loop
            self._reader_thread = threading.Thread(
                target=self._windows_read_loop,
                name="aha-local-terminal",
                daemon=True,
            )
            self._reader_thread.start()
            return
        if self.master_fd is None or self._reader_attached:
            return
        loop.add_reader(self.master_fd, self._read_ready)
        self._reader_attached = True

    def detach_reader(self, loop: asyncio.AbstractEventLoop) -> None:
        if _platform.is_windows():
            self._reader_attached = False
            return
        if self.master_fd is None or not self._reader_attached:
            return
        loop.remove_reader(self.master_fd)
        self._reader_attached = False

    def _windows_read_loop(self) -> None:
        output = self._windows_stdout
        loop = self._reader_loop
        if output is None or loop is None:
            return
        decoder = codecs.getincrementaldecoder(self._windows_encoding)(errors="replace")
        try:
            while True:
                chunk = output.read(4096)
                if not chunk:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        self._queue_windows_output(loop, tail.encode("utf-8"))
                    self._queue_windows_output(loop, None)
                    break
                text = decoder.decode(chunk)
                if text:
                    self._queue_windows_output(loop, text.encode("utf-8"))
        except (OSError, ValueError) as exc:
            self._queue_windows_output(loop, f"\r\n[AHA terminal read error: {exc}]\r\n".encode("utf-8"))
            self._queue_windows_output(loop, None)

    def _queue_windows_output(self, loop: asyncio.AbstractEventLoop, chunk: bytes | None) -> None:
        try:
            loop.call_soon_threadsafe(self._output_queue.put_nowait, chunk)
        except RuntimeError:
            pass

    def _read_ready(self) -> None:
        if self.master_fd is None:
            return
        while True:
            try:
                chunk = os.read(self.master_fd, 4096)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    self._output_queue.put_nowait(None)
                    break
                self._output_queue.put_nowait(f"\r\n[AHA terminal read error: {exc}]\r\n".encode("utf-8", errors="replace"))
                self._output_queue.put_nowait(None)
                break
            if not chunk:
                self._output_queue.put_nowait(None)
                break
            self._output_queue.put_nowait(chunk)

    async def read(self) -> bytes | None:
        return await self._output_queue.get()

    def write(self, data: str) -> None:
        if _platform.is_windows():
            if self._windows_stdin is None or not data:
                return
            try:
                self._windows_stdin.write(data.encode(self._windows_encoding, errors="replace"))
                self._windows_stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            return
        if self.master_fd is None or not data:
            return
        os.write(self.master_fd, data.encode("utf-8", errors="surrogatepass"))

    def resize(self, *, cols: object, rows: object) -> None:
        if _platform.is_windows():
            if self._windows_conpty and self.process is not None:
                normalized_cols, normalized_rows = normalize_terminal_size(cols, rows)
                self.process.resize(cols=normalized_cols, rows=normalized_rows)
            return
        if self.master_fd is None:
            return
        import fcntl
        import termios

        normalized_cols, normalized_rows = normalize_terminal_size(cols, rows)
        size = struct.pack("HHHH", normalized_rows, normalized_cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, size)

    async def wait(self) -> int:
        if self.process is None:
            return 0
        return int(await asyncio.to_thread(self.process.wait))

    async def terminate(self) -> None:
        process = self.process
        if _platform.is_windows():
            if self._windows_stdin is not None:
                try:
                    self._windows_stdin.close()
                except OSError:
                    pass
            self._windows_stdin = None
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
            close_pseudo_console = getattr(process, "close_pseudo_console", None)
            if callable(close_pseudo_console):
                close_pseudo_console()
            thread = self._reader_thread
            if thread is not None and thread.is_alive():
                await asyncio.to_thread(thread.join, 0.5)
            if self._windows_stdout is not None:
                try:
                    self._windows_stdout.close()
                except OSError:
                    pass
            self._windows_stdout = None
            close_process = getattr(process, "close", None)
            if callable(close_process):
                close_process()
            self._reader_thread = None
            self._reader_loop = None
            self._windows_conpty = False
            return
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
                await asyncio.to_thread(process.wait)
        if self._slave_fd is not None:
            try:
                os.close(self._slave_fd)
            except OSError:
                pass
            self._slave_fd = None
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
